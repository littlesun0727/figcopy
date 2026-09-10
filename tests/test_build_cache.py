"""验证背景构建缓存、失败状态、fixture 标识与端到端本地演示。"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_reviewed_spec
from PIL import Image

from collage.build import approve_template, build_template
from collage.demo import create_demo
from collage.errors import CollageError
from collage.io_utils import atomic_save_image, atomic_write_json, read_json
from collage.providers import GeneratedImage, ImageCapabilities, ProviderAudit
from collage.validate import validate_package


class CountingImageProvider:
    """记录背景调用次数，用于验证内容寻址缓存。"""

    def __init__(self, output_sizes: tuple[tuple[int, int], ...] = ()) -> None:
        self.calls = 0
        self.output_sizes = output_sizes
        self.received_sizes: list[tuple[int, int]] = []

    @property
    def capabilities(self) -> ImageCapabilities:
        return ImageCapabilities(
            name="counting-provider",
            requested_model="count-v1",
            supports_reference_image=True,
            supports_mask_edit=True,
            supports_transparency=True,
            mask_polarity="white_edit",
            output_sizes=self.output_sizes,
        )

    def edit_background(
        self, reference: Image.Image, provider_mask: Image.Image, *, brief: str
    ) -> GeneratedImage:
        self.calls += 1
        self.received_sizes.append(reference.size)
        return GeneratedImage(
            Image.new("RGBA", reference.size, (30 + self.calls, 90, 150, 255)),
            ProviderAudit(
                "counting-provider",
                "count-v1",
                "count-v1",
                f"req-{self.calls}",
                False,
                3,
            ),
        )

    def make_overlay(self, reference_crop: Image.Image, **kwargs) -> GeneratedImage:
        raise AssertionError("本测试没有 overlay")


class ChromaOnlyOverlayProvider:
    """模拟不支持 alpha、但能在同一次请求中输出指定纯色背景的 provider。"""

    def __init__(self) -> None:
        self.calls = 0
        self.received_mode: str | None = None

    @property
    def capabilities(self) -> ImageCapabilities:
        return ImageCapabilities(
            name="chroma-only",
            requested_model="chroma-v1",
            supports_reference_image=True,
            supports_mask_edit=False,
            supports_transparency=False,
            mask_polarity="white_edit",
        )

    def edit_background(
        self, reference: Image.Image, provider_mask: Image.Image, *, brief: str
    ) -> GeneratedImage:
        raise AssertionError("测试使用导入背景")

    def make_overlay(
        self,
        reference_crop: Image.Image,
        *,
        brief: str,
        background_mode: str,
        chroma_key: tuple[int, int, int] | None,
    ) -> GeneratedImage:
        self.calls += 1
        self.received_mode = background_mode
        assert chroma_key is not None
        image = Image.new("RGB", reference_crop.size, chroma_key)
        from PIL import ImageDraw

        ImageDraw.Draw(image).ellipse(
            (2, 2, image.width - 3, image.height - 3), fill=(20, 30, 40)
        )
        return GeneratedImage(
            image,
            ProviderAudit(
                "chroma-only",
                "chroma-v1",
                "chroma-v1",
                f"overlay-{self.calls}",
                False,
                2,
            ),
        )


def test_expensive_background_call_is_cached_and_brief_invalidates(
    tmp_path: Path,
) -> None:
    spec_path = make_reviewed_spec(tmp_path, candidate=False)
    provider = CountingImageProvider()
    package = tmp_path / "template"
    work = tmp_path / "work"
    build_template(spec_path, package, work_dir=work, image_provider=provider)
    assert provider.calls == 1
    build_template(
        spec_path, package, work_dir=work, image_provider=provider, force=True
    )
    assert provider.calls == 1
    assert (
        read_json(package / "template.json")["build"]["providers"][0]["cache_hit"]
        is True
    )
    spec = read_json(spec_path)
    spec["background"]["background_brief"] = "改为更细的纸纹"
    atomic_write_json(spec_path, spec)
    build_template(
        spec_path, package, work_dir=work, image_provider=provider, force=True
    )
    assert provider.calls == 2
    remove_mask = Image.open(tmp_path / "remove.png").convert("L")
    remove_mask.putpixel((0, 0), 255)
    atomic_save_image(remove_mask, tmp_path / "remove.png")
    build_template(
        spec_path, package, work_dir=work, image_provider=provider, force=True
    )
    assert provider.calls == 3
    provider.output_sizes = ((32, 32),)
    build_template(
        spec_path, package, work_dir=work, image_provider=provider, force=True
    )
    assert provider.calls == 4


def test_missing_image_provider_is_blocked_without_fake_success(tmp_path: Path) -> None:
    spec_path = make_reviewed_spec(tmp_path, candidate=False)
    with pytest.raises(CollageError) as caught:
        build_template(spec_path, tmp_path / "template", work_dir=tmp_path / "work")
    assert caught.value.code == "IMAGE_PROVIDER_UNAVAILABLE"
    assert read_json(tmp_path / "work" / "state.json")["status"] == "blocked"
    assert not (tmp_path / "template" / "template.json").exists()


def test_demo_creates_reviewable_deterministic_package_in_chinese_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "中文演示"
    result = create_demo(root)
    assert result.is_file()
    template = validate_package(root / "template", require_ready=False)
    assert template["status"] == "needs_review"
    assert template["build"]["fixture_used"] is False
    assert (root / "work" / "inspection.html").is_file()
    assert (root / "work" / "background_candidate.png").is_file()
    assert (root / "work" / "previews" / "overlay_star_edges.png").is_file()
    assert read_json(root / "work" / "state.json")["status"] == "needs_review"
    approve_template(
        root / "template",
        [result],
        reviewer="test-simulated-reviewer",
        work_dir=root / "work",
    )
    assert validate_package(root / "template")["status"] == "ready"
    assert read_json(root / "work" / "state.json")["status"] == "ready"


def test_fixture_build_requires_explicit_approval(
    asset_package_factory, tmp_path: Path
) -> None:
    package = asset_package_factory(tmp_path)
    spec = read_json(package / "template.json")
    spec["build"]["fixture_used"] = True
    atomic_write_json(package / "template.json", spec)
    evidence = tmp_path / "evidence.png"
    Image.new("RGBA", (20, 20), "white").save(evidence)
    with pytest.raises(CollageError) as caught:
        approve_template(package, [evidence], reviewer="tester")
    assert caught.value.code == "FIXTURE_APPROVAL_BLOCKED"


def test_provider_size_constraint_is_padded_and_reversibly_restored(
    tmp_path: Path,
) -> None:
    spec_path = make_reviewed_spec(tmp_path, candidate=False)
    provider = CountingImageProvider(output_sizes=((32, 32),))
    package = tmp_path / "template"
    build_template(
        spec_path, package, work_dir=tmp_path / "work", image_provider=provider
    )
    assert provider.received_sizes == [(32, 32)]
    assert Image.open(package / "assets" / "background.png").size == (24, 16)
    transform = read_json(tmp_path / "work" / "background_transform.json")
    assert transform["kind"] == "contain_padding"


def test_basic_shape_is_drawn_locally_without_provider(tmp_path: Path) -> None:
    spec_path = make_reviewed_spec(tmp_path, candidate=True)
    spec = read_json(spec_path)
    spec["overlays"] = [
        {
            "id": "frame",
            "label": "虚线框",
            "source_rect": [0, 0, 12, 10],
            "target_rect": [2, 2, 12, 10],
            "action": "basic_shape",
            "generation_brief": "",
            "requires_exact_content": False,
            "review_notes": "",
            "rotation_deg": 0,
            "prepared_asset": None,
            "background_mode": "alpha",
            "chroma_key": None,
            "chroma_tolerance": 40,
            "shape": {
                "kind": "dashed_rectangle",
                "fill": None,
                "outline": "#FF0000",
                "width": 1,
                "radius": 0,
                "dash": 2,
                "gap": 1,
            },
        }
    ]
    spec["layer_order"].append({"type": "overlay", "id": "frame"})
    atomic_write_json(spec_path, spec)
    package = tmp_path / "template"
    build_template(spec_path, package, work_dir=tmp_path / "work")
    asset = Image.open(package / "assets" / "overlay_frame.png").convert("RGBA")
    assert asset.getchannel("A").getextrema() == (0, 255)
    assert (
        read_json(package / "template.json")["build"]["providers"][-1]["name"]
        == "local-basic-shape"
    )


def test_overlay_falls_back_to_single_call_chroma_and_caches_normalized_result(
    tmp_path: Path,
) -> None:
    spec_path = make_reviewed_spec(tmp_path, candidate=True)
    spec = read_json(spec_path)
    spec["overlays"] = [
        {
            "id": "sticker",
            "label": "贴纸",
            "source_rect": [0, 0, 12, 12],
            "target_rect": [4, 2, 12, 12],
            "action": "reference_generate",
            "generation_brief": "生成一个深色圆形贴纸",
            "requires_exact_content": False,
            "review_notes": "",
            "rotation_deg": 0,
            "prepared_asset": None,
            "background_mode": "alpha",
            "chroma_key": None,
            "chroma_tolerance": 30,
            "shape": None,
        }
    ]
    spec["layer_order"].append({"type": "overlay", "id": "sticker"})
    atomic_write_json(spec_path, spec)
    provider = ChromaOnlyOverlayProvider()
    package = tmp_path / "template"
    work = tmp_path / "work"
    build_template(spec_path, package, work_dir=work, image_provider=provider)
    assert provider.calls == 1
    assert provider.received_mode == "chroma_key"
    output = Image.open(package / "assets" / "overlay_sticker.png").convert("RGBA")
    assert output.getchannel("A").getextrema() == (0, 255)
    build_template(
        spec_path, package, work_dir=work, image_provider=provider, force=True
    )
    assert provider.calls == 1
    assert (
        read_json(package / "template.json")["build"]["providers"][-1]["cache_hit"]
        is True
    )
