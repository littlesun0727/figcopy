"""Check single-generation candidates, raw reuse, local findings and foreground bounds."""

import copy

import pytest
from PIL import Image, ImageDraw

from collage.core.errors import CollageError
from collage.core.state import NodeCache
from collage.providers import GeneratedImage, ImageCapabilities, ProviderAudit
from collage.template.build.overlays import _provider_overlay

OVERLAY = {
    "id": "doodle",
    "generation_brief": "a complete doodle",
    "background_mode": "alpha",
    "chroma_key": None,
    "chroma_tolerance": 40,
}


def _asset(clipped=False):
    image = Image.new("RGBA", (100, 100))
    ImageDraw.Draw(image).ellipse((0 if clipped else 20, 20, 80, 80), fill="white")
    return image


class Provider:
    capabilities = ImageCapabilities(
        "mock-real", "fixture-image", True, False, True, "white_edit"
    )

    def __init__(self, *, clipped=0, texts=None, complete=None, timeout=False):
        self.calls = 0
        self.inspections = 0
        self.clipped = clipped
        self.texts = texts or ["", ""]
        self.complete = complete or [True, True]
        self.timeout = timeout
        self.briefs = []

    def make_overlay(self, image, **kwargs):
        self.calls += 1
        self.briefs.append(kwargs["brief"])
        if self.timeout:
            raise CollageError("PROVIDER_TIMEOUT", "timeout")
        return GeneratedImage(
            _asset(), self.audit(), raw_image=_asset(self.calls <= self.clipped)
        )

    def audit(self):
        return ProviderAudit(
            "mock-real", "fixture", "fixture", str(self.calls), True, 0
        )

    def inspect_overlay(self, reference, image, **kwargs):
        index = self.inspections
        self.inspections += 1
        return {
            "complete": self.complete[index],
            "matches_reference": True,
            "unwanted_content": False,
            "uncertain": False,
            "observed_text": self.texts[index],
            "issues": [],
        }, self.audit()


def _run(tmp_path, provider, overlay=None):
    return _provider_overlay(
        provider,
        Image.new("RGB", (100, 100), "gray"),
        overlay or copy.deepcopy(OVERLAY),
        NodeCache(tmp_path / "cache"),
        "test-key",
    )


class ChromaProvider(Provider):
    """Exercise actual local keying with fixture artwork and mocked semantic verdicts."""

    capabilities = ImageCapabilities(
        "mock-chroma", "fixture-image", True, False, False, "white_edit"
    )

    def __init__(self, *, complete=None):
        super().__init__(complete=complete, texts=[""] * 8)

    def make_overlay(self, image, **kwargs):
        self.calls += 1
        self.briefs.append(kwargs["brief"])
        raw = Image.new("RGB", image.size, kwargs["chroma_key"])
        draw = ImageDraw.Draw(raw)
        draw.rectangle((25, 25, 75, 65), fill="white")
        draw.line((10, 80, 90, 80), fill="white", width=1)
        return GeneratedImage(raw, self.audit(), raw_image=raw)


def test_clipping_is_advisory_and_does_not_trigger_review_or_retry(tmp_path):
    provider = Provider(clipped=10)
    result, *_rest, transform = _run(tmp_path, provider)
    assert "OVERLAY_EDGE_CLIPPED" in transform["technical"]["issues"]
    assert result.size == (100, 100)
    _run(tmp_path, provider)
    assert provider.calls == 1 and provider.inspections == 0


def test_text_is_in_generation_prompt_without_a_second_model_call(tmp_path):
    provider = Provider(texts=["wrong"])
    _run(tmp_path, provider, {**OVERLAY, "text_content": "hello"})
    assert "hello" in provider.briefs[0]
    assert provider.calls == 1 and provider.inspections == 0


def test_ambiguous_transport_error_is_not_replayed(tmp_path):
    provider = Provider(timeout=True)
    with pytest.raises(CollageError):
        _run(tmp_path, provider)
    with pytest.raises(CollageError) as error:
        _run(tmp_path, provider)
    assert error.value.code == "OVERLAY_REQUEST_UNCERTAIN"
    assert provider.calls == 1


def test_corrupt_cache_recovers_from_raw_without_model_calls(tmp_path):
    provider = Provider()
    original, *_ = _run(tmp_path, provider)
    Image.new("RGBA", (100, 100), "black").save(tmp_path / "cache/overlay/test-key.png")
    restored, *_ = _run(tmp_path, provider)
    assert restored.tobytes() == original.tobytes()
    assert provider.calls == 1 and provider.inspections == 0


def test_chroma_build_preserves_single_pixel_stroke(tmp_path):
    provider = ChromaProvider()
    result, *_ = _run(tmp_path, provider)
    assert result.getpixel((50, 80)) == (255, 255, 255, 255)
    assert provider.calls == 1 and provider.inspections == 0


def test_processing_upgrade_reuses_raw_without_vlm(tmp_path, monkeypatch):
    from collage.core.io import read_json
    from collage.template.build import overlays as overlay_build

    provider = ChromaProvider()
    _run(tmp_path, provider)
    original_record = (tmp_path / "overlay_attempts/test-key/0.json").read_bytes()
    monkeypatch.setattr(overlay_build, "CHROMA_PROCESSING_VERSION", "pyav-test-next")
    _run(tmp_path, provider)
    record = read_json(tmp_path / "overlay_attempts/test-key/0_processing.json")
    assert record["processing_version"] == "pyav-test-next"
    assert record["semantic_checked"] is False
    assert (
        tmp_path / "overlay_attempts/test-key/0.json"
    ).read_bytes() == original_record
    assert provider.calls == 1 and provider.inspections == 0


@pytest.mark.parametrize("legacy_status", ["rejected", "accepted"])
def test_legacy_outputs_and_pending_inspection_are_reused(tmp_path, legacy_status):
    from collage.core.io import atomic_write_json, sha256_file

    root = tmp_path / "overlay_attempts/test-key"
    root.mkdir(parents=True)
    provider = Provider(timeout=True)
    raw = _asset(clipped=True)
    raw.save(root / "1_raw.png")
    record = {
        "status": legacy_status,
        "inspection_status": "pending",
        "raw_sha256": sha256_file(root / "1_raw.png"),
        "audit": provider.audit().as_dict(),
    }
    atomic_write_json(root / "1.json", record)
    before = (root / "1.json").read_bytes()
    result, *_ = _run(tmp_path, provider)
    assert result.tobytes() == raw.tobytes()
    assert (root / "1.json").read_bytes() == before
    assert provider.calls == 0 and provider.inspections == 0


def test_chroma_backend_is_checked_before_generation(tmp_path, monkeypatch):
    from collage.template.build import overlays as overlay_build

    def missing():
        raise CollageError("CHROMA_DEPENDENCY_MISSING", "missing local PyAV")

    provider = ChromaProvider()
    monkeypatch.setattr(overlay_build, "require_chroma_backend", missing)
    with pytest.raises(CollageError) as caught:
        _run(tmp_path, provider)
    assert caught.value.code == "CHROMA_DEPENDENCY_MISSING"
    assert provider.calls == 0 and provider.inspections == 0


def test_content_bounds_exclude_screen_residue_but_keep_faint_punctuation():
    from collage.template.build.asset_validation import content_box

    raw = Image.new("RGBA", (200, 80), (0, 255, 0, 0))
    raw.putpixel((0, 0), (50, 150, 50, 100))
    ImageDraw.Draw(raw).line((30, 20, 140, 20), fill="white", width=1)
    raw.putpixel((170, 40), (255, 255, 255, 8))
    mapped = raw.copy()
    mapped.putpixel((0, 0), (50, 50, 50, 100))  # despilled residue
    assert content_box(mapped, (0, 255, 0), raw) == (29, 19, 172, 42)


def test_content_bounds_keep_dark_foreground_with_screen_hue():
    from collage.template.build.asset_validation import content_box

    raw = Image.new("RGBA", (100, 60), (0, 0, 255, 0))
    ImageDraw.Draw(raw).rectangle((20, 20, 80, 40), fill=(20, 30, 40, 255))
    assert content_box(raw, (0, 0, 255), raw) == (19, 19, 82, 42)
