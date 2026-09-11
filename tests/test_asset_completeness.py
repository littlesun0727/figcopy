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
