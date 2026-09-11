"""Check untrimmed generated artwork and keep bounded repair evidence."""

from __future__ import annotations

import unicodedata
import hashlib
from typing import Any

from PIL import Image

from ...core.errors import CollageError
from ...imaging.operations import alpha_is_meaningful


def asset_fingerprint(image: Image.Image) -> str:
    """Bind cached quality evidence to dimensions and actual decoded RGBA pixels."""
    rgba = image.convert("RGBA")
    return hashlib.sha256(str(rgba.size).encode("ascii") + rgba.tobytes()).hexdigest()


def alpha_completeness(image: Image.Image) -> dict[str, Any]:
    """Detect empty, opaque and boundary-clipped assets before padding or trimming."""
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    # Ignore tiny interpolation noise, but count faint handwriting as visible.
    visible = alpha.point(lambda value: 255 if value >= 16 else 0)
    box = visible.getbbox()
    issues: list[str] = []
    if box is None:
        issues.append("EMPTY_OVERLAY")
    elif not alpha_is_meaningful(rgba):
        issues.append("OPAQUE_OVERLAY")
    clearance = max(1, round(min(image.size) * 0.015))
    if box and (
        box[0] < clearance
        or box[1] < clearance
        or box[2] > image.width - clearance
        or box[3] > image.height - clearance
    ):
        issues.append("OVERLAY_EDGE_CLIPPED")
    return {
        "bbox": list(box) if box else None,
        "size": list(image.size),
        "required_clearance_px": clearance,
        "issues": issues,
    }


def normalized_text(value: str) -> str:
    """Ignore layout whitespace; preserve every character and punctuation mark."""
    return "".join(unicodedata.normalize("NFC", value).split())


def semantic_completeness(
    provider, crop: Image.Image, candidate: Image.Image, overlay: dict[str, Any]
) -> dict[str, Any]:
    """Require explicit semantic evidence from real providers; fixture evidence is labeled."""
    inspector = getattr(provider, "inspect_overlay", None)
    if not callable(inspector):
        if provider.capabilities.fixture:
            return {"status": "fixture_only", "issues": [], "fixture": True}
        raise CollageError(
            "OVERLAY_INSPECTION_UNAVAILABLE", "当前图片 provider 未配置素材完整性检查"
        )
    result, audit = inspector(
        crop,
        candidate,
        brief=overlay["generation_brief"],
        text_content=overlay.get("text_content"),
    )
    required = {"complete", "matches_reference", "unwanted_content", "uncertain"}
    if not isinstance(result, dict) or any(
        type(result.get(key)) is not bool for key in required
    ):
        raise CollageError("OVERLAY_INSPECTION_INVALID", "素材检查缺少明确的完整性结论")
    if not isinstance(result.get("observed_text"), str) or not isinstance(
        result.get("issues"), list
    ):
        raise CollageError("OVERLAY_INSPECTION_INVALID", "素材检查缺少文字或问题记录")
    issues = []
    if result["complete"] is not True:
        issues.append("OVERLAY_CONTENT_INCOMPLETE")
    if not result["matches_reference"] or result["unwanted_content"]:
        issues.append("OVERLAY_CONTENT_MISMATCH")
    if result["uncertain"]:
        issues.append("OVERLAY_CONTENT_UNCERTAIN")
    expected = overlay.get("text_content")
    if not expected and normalized_text(result["observed_text"]):
        issues.append("OVERLAY_UNCONFIRMED_TEXT")
    if expected and normalized_text(result["observed_text"]) != normalized_text(
        expected
    ):
        issues.append("OVERLAY_TEXT_MISMATCH")
    return {
        "status": "failed" if issues else "passed",
        "issues": issues,
        "result": result,
        "audit": audit.as_dict(),
        "fixture": audit.fixture,
    }
