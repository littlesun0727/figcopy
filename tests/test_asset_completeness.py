"""Check clipping, missing content, OCR mismatch, bounded repair and paid-call caching."""

import copy

import pytest
from PIL import Image, ImageDraw

from collage.core.errors import CollageError
from collage.core.state import NodeCache
from collage.providers import GeneratedImage, ImageCapabilities, ProviderAudit
from collage.template.build.overlays import _provider_overlay
from collage.template.build.asset_validation import alpha_completeness

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


def test_raw_clipping_is_detected_before_padding_and_repaired_once(tmp_path):
    provider = Provider(clipped=1)
    result, *_ = _run(tmp_path, provider)
    assert provider.calls == 2 and provider.inspections == 1
    assert not alpha_completeness(result)["issues"]
    assert "OVERLAY_EDGE_CLIPPED" in provider.briefs[1]
    _run(tmp_path, provider)
    assert provider.calls == 2 and provider.inspections == 1


def test_missing_component_without_edge_contact_requires_semantic_repair(tmp_path):
    provider = Provider(complete=[False, True])
    _run(tmp_path, provider)
    assert provider.calls == 2 and provider.inspections == 2
    assert "OVERLAY_CONTENT_INCOMPLETE" in provider.briefs[1]


def test_wrong_text_does_not_pass_on_model_complete_flag(tmp_path):
    provider = Provider(texts=["hell", "hello"])
    _run(tmp_path, provider, {**OVERLAY, "text_content": "hello"})
    assert provider.calls == 2 and provider.inspections == 2
    assert "OVERLAY_TEXT_MISMATCH" in provider.briefs[1]


def test_persistent_missing_edges_stop_after_one_repair(tmp_path):
    provider = Provider(clipped=10)
    with pytest.raises(CollageError) as error:
        _run(tmp_path, provider)
    assert error.value.code == "OVERLAY_COMPLETENESS_FAILED"
    with pytest.raises(CollageError):
        _run(tmp_path, provider)
    assert provider.calls == 2


def test_ambiguous_transport_error_is_not_replayed(tmp_path):
    provider = Provider(timeout=True)
    with pytest.raises(CollageError):
        _run(tmp_path, provider)
    with pytest.raises(CollageError) as error:
        _run(tmp_path, provider)
    assert error.value.code == "OVERLAY_REQUEST_UNCERTAIN"
    assert provider.calls == 1


def test_corrupted_cached_png_cannot_reuse_unrelated_quality_evidence(tmp_path):
    provider = Provider()
    original, *_ = _run(tmp_path, provider)
    Image.new("RGBA", (100, 100), "black").save(tmp_path / "cache/overlay/test-key.png")
    restored, *_ = _run(tmp_path, provider)
    assert restored.tobytes() == original.tobytes()
    assert provider.calls == 1 and provider.inspections == 1


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


def test_chroma_build_preserves_single_pixel_stroke(tmp_path):
    provider = ChromaProvider()
    result, *_ = _run(tmp_path, provider)
    assert result.getpixel((50, 80)) == (255, 255, 255, 255)
    assert provider.calls == 1 and provider.inspections == 1


def test_processing_upgrade_reuses_raw_but_rechecks_semantics(tmp_path, monkeypatch):
    from collage.core.io import read_json
    from collage.template.build import overlays as overlay_build

    provider = ChromaProvider()
    _run(tmp_path, provider)
    monkeypatch.setattr(overlay_build, "CHROMA_PROCESSING_VERSION", "pyav-test-next")
    _run(tmp_path, provider)
    assert provider.calls == 1 and provider.inspections == 2
    record = read_json(tmp_path / "overlay_attempts/test-key/0.json")
    assert record["processing_version"] == "pyav-test-next"
    assert record["inspection_fingerprint"]
    _run(tmp_path, provider)
    assert provider.calls == 1 and provider.inspections == 2


def test_processing_upgrade_rechecks_rejected_raw_without_resetting_generation_cap(
    tmp_path, monkeypatch
):
    from collage.template.build import overlays as overlay_build

    provider = ChromaProvider(complete=[False, False, True])
    with pytest.raises(CollageError) as caught:
        _run(tmp_path, provider)
    assert caught.value.code == "OVERLAY_COMPLETENESS_FAILED"
    assert provider.calls == 2 and provider.inspections == 2

    monkeypatch.setattr(overlay_build, "CHROMA_PROCESSING_VERSION", "pyav-test-next")
    _run(tmp_path, provider)
    assert provider.calls == 2 and provider.inspections == 3


@pytest.mark.parametrize("technical_failure", [False, True])
def test_processing_upgrade_keeps_unknown_inspection_blocked(
    tmp_path, monkeypatch, technical_failure
):
    from collage.core.io import atomic_write_json, read_json
    from collage.template.build import overlays as overlay_build

    provider = ChromaProvider()
    _run(tmp_path, provider)
    record_path = tmp_path / "overlay_attempts/test-key/0.json"
    record = read_json(record_path)
    record["inspection_status"] = "pending"
    atomic_write_json(record_path, record)
    monkeypatch.setattr(overlay_build, "CHROMA_PROCESSING_VERSION", "pyav-test-next")

    if technical_failure:
        monkeypatch.setattr(
            overlay_build,
            "alpha_completeness",
            lambda image: {"issues": ["OVERLAY_EDGE_CLIPPED"]},
        )
    with pytest.raises(CollageError) as caught:
        _run(tmp_path, provider)
    assert caught.value.code == "OVERLAY_INSPECTION_UNCERTAIN"
    assert provider.calls == 1 and provider.inspections == 1


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
