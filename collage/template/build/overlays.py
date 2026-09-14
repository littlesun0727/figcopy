"""Create basic-shape and provider-generated overlay assets."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from ...core.errors import CollageError
from ...core.io import (
    atomic_save_image,
    atomic_write_json,
    decode_image,
    read_json,
    resolve_input_path,
    sha256_file,
    stable_hash,
)
from ...core.state import NodeCache
from ...imaging.geometry import pad_for_model
from ...imaging.chroma import CHROMA_PROCESSING_VERSION, require_chroma_backend
from ...imaging.operations import (
    alpha_is_meaningful,
    choose_chroma_key,
    parse_color,
    rect_to_box,
    remove_chroma_background,
)
from ...providers.selection import cache_configuration
from ...providers import ImageProvider, ProviderAudit
from .common import (
    _audit_from_cache,
    _choose_provider_size,
    _identity_transform,
    _import_audit,
)

from .asset_validation import (
    alpha_completeness,
    asset_fingerprint,
    content_box,
    overlay_warning,
)

LOGGER = logging.getLogger(__name__)
OVERLAY_PROMPT_VERSION = "reference-overlay/5"
OVERLAY_PIPELINE_VERSION = "candidate-overlay/1"


def _draw_dashed_rectangle(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    *,
    fill: tuple[int, int, int, int],
    width: int,
    dash: int,
    gap: int,
) -> None:
    left, top, right, bottom = box
    step = max(1, dash + gap)
    for x in range(left, right + 1, step):
        draw.line((x, top, min(x + dash, right), top), fill=fill, width=width)
        draw.line((x, bottom, min(x + dash, right), bottom), fill=fill, width=width)
    for y in range(top, bottom + 1, step):
        draw.line((left, y, left, min(y + dash, bottom)), fill=fill, width=width)
        draw.line((right, y, right, min(y + dash, bottom)), fill=fill, width=width)


def _make_basic_shape(overlay: dict[str, Any]) -> Image.Image:
    box = rect_to_box(overlay["target_rect"])
    size = (box[2] - box[0], box[3] - box[1])
    shape = overlay["shape"]
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    bounds = (0, 0, size[0] - 1, size[1] - 1)
    fill = parse_color(shape["fill"]) if shape["fill"] is not None else None
    outline = parse_color(shape["outline"]) if shape["outline"] is not None else None
    width = shape["width"]
    if shape["kind"] == "rectangle":
        draw.rectangle(bounds, fill=fill, outline=outline, width=width)
    elif shape["kind"] == "rounded_rectangle":
        draw.rounded_rectangle(
            bounds, radius=shape["radius"], fill=fill, outline=outline, width=width
        )
    elif shape["kind"] == "ellipse":
        draw.ellipse(bounds, fill=fill, outline=outline, width=width)
    else:
        if fill is not None:
            draw.rectangle(bounds, fill=fill)
        if outline is not None and width > 0:
            _draw_dashed_rectangle(
                draw,
                bounds,
                fill=outline,
                width=width,
                dash=shape["dash"],
                gap=shape["gap"],
            )
    return image


def _provider_overlay(
    provider: ImageProvider,
    crop: Image.Image,
    overlay: dict[str, Any],
    cache: NodeCache,
    cache_key: str,
) -> tuple[
    Image.Image, ProviderAudit, str, tuple[int, int, int] | None, dict[str, Any]
]:
    """Generate once or reuse saved pixels; local findings never trigger model repair."""
    capabilities = provider.capabilities
    if not capabilities.supports_reference_image:
        raise CollageError(
            "IMAGE_PROVIDER_CAPABILITY_MISSING", "图片 provider 不支持参考图片输入"
        )
    effective_mode = (
        overlay["background_mode"]
        if capabilities.supports_transparency
        else "chroma_key"
    )
    processing_version = (
        CHROMA_PROCESSING_VERSION if effective_mode == "chroma_key" else None
    )
    cached = cache.get("overlay", cache_key)
    if (
        cached is not None
        and cached.metadata.get("pipeline_version") == OVERLAY_PIPELINE_VERSION
        and cached.metadata.get("processing_version") == processing_version
        and cached.metadata.get("image_fingerprint") == asset_fingerprint(cached.image)
    ):
        metadata = cached.metadata
        return (
            cached.image,
            _audit_from_cache(metadata),
            metadata["background_mode"],
            tuple(metadata["chroma_key"]) if metadata.get("chroma_key") else None,
            metadata["transform"],
        )

    key = None
    if effective_mode == "chroma_key":
        require_chroma_backend()
        key = (
            tuple(overlay["chroma_key"])
            if overlay.get("chroma_key")
            else choose_chroma_key(crop)
        )
    provider_crop = crop
    transform_record = _identity_transform(crop.size)
    target_size = _choose_provider_size(crop.size, capabilities.output_sizes)
    if target_size is not None:
        fill = (*key, 255) if key else (0, 0, 0, 0)
        provider_crop, transform = pad_for_model(crop, target_size, fill=fill)
        transform_record = {"kind": "contain_padding", **transform.as_dict()}

    attempt_root = cache.root.parent / "overlay_attempts" / cache_key
    attempt_root.mkdir(parents=True, exist_ok=True)
    records = sorted(
        (path for path in attempt_root.glob("*.json") if path.stem.isdigit()),
        key=lambda path: int(path.stem),
        reverse=True,
    )
    # Old rejected outputs are still usable candidates. Reuse the latest saved
    # output, even if its optional VLM inspection was interrupted.
    saved = next(
        (path for path in records if path.with_name(path.stem + "_raw.png").is_file()),
        None,
    )
    already_processed = False
    if saved is not None:
        record = read_json(saved)
        if record.get("status") not in {"generated", "accepted", "rejected"}:
            raise CollageError(
                "OVERLAY_REQUEST_UNCERTAIN", "素材请求尚无已确认的完整输出"
            )
        raw_path = saved.with_name(saved.stem + "_raw.png")
        if record.get("raw_sha256") != sha256_file(raw_path):
            raise CollageError("OVERLAY_EVIDENCE_CHANGED", "素材原始输出哈希不一致")
        raw = decode_image(raw_path)
        audit = _audit_from_cache(record)
        attempt = int(saved.stem)
        LOGGER.info("复用已保存素材 | id=%s attempt=%s", overlay["id"], attempt + 1)
    elif records and read_json(records[0]).get("status") not in {
        "not_started",
        "failed",
    }:
        raise CollageError(
            "OVERLAY_REQUEST_UNCERTAIN",
            "该件请求已有记录但无完整输出，请检查记录或手动重做",
        )
    elif cached is not None:
        # Historical packages may only retain their normalized cache image.
        raw, audit = cached.image, _audit_from_cache(cached.metadata)
        already_processed = bool(cached.metadata.get("normalized"))
        attempt = 0
    else:
        attempt = max((int(path.stem) for path in records), default=-1) + 1
        brief = (
            overlay["generation_brief"]
            + "\n只制作这一件完整独立素材。补全遮挡或截断的部分，禁止附带相邻照片、边框和背景残片。"
            "保留所有细线，四周至少留 8% 空白。"
        )
        if overlay.get("text_content"):
            brief += "\n必须逐字包含且仅包含客户确认的文字：" + overlay["text_content"]
        record = {"status": "pending", "attempt": attempt, "prompt": brief}
        record_path = attempt_root / f"{attempt}.json"
        atomic_write_json(record_path, record)
        LOGGER.info(
            "生成单件装饰 | id=%s provider=%s", overlay["id"], capabilities.name
        )
        try:
            generated = provider.make_overlay(
                provider_crop,
                brief=brief,
                background_mode=effective_mode,
                chroma_key=key,
            )
        except CollageError as exc:
            # New direct transports distinguish a rejected request from an unknown result.
            # Keep the legacy pending behavior when the provider does not report certainty.
            request_state = exc.details.get("request_state")
            if request_state in {"not_started", "failed", "unknown"}:
                record.update(
                    status="uncertain" if request_state == "unknown" else request_state,
                    error_code=exc.code,
                )
                atomic_write_json(record_path, record)
            raise
        raw = (
            generated.raw_image if generated.raw_image is not None else generated.image
        )
        audit = generated.audit
        raw_path = attempt_root / f"{attempt}_raw.png"
        atomic_save_image(raw, raw_path)
        record.update(
            status="generated",
            audit=audit.as_dict(),
            raw_sha256=sha256_file(raw_path),
            provider_transform=generated.transform,
        )
        atomic_write_json(record_path, record)

    mapped = raw.convert("RGBA")
    if effective_mode == "chroma_key" and not already_processed:
        mapped = remove_chroma_background(
            mapped, key, tolerance=overlay["chroma_tolerance"]
        )
    technical = alpha_completeness(mapped)
    transform_record.update(
        content_box=content_box(mapped, key if not already_processed else None, raw),
        technical=technical,
    )
    # Preserve full resolution. Template placement will use the foreground bounds,
    # while this uncropped evidence remains available for visual review.
    atomic_save_image(mapped, attempt_root / f"{attempt}_processed.png")
    atomic_write_json(
        attempt_root / f"{attempt}_processing.json",
        {
            "pipeline_version": OVERLAY_PIPELINE_VERSION,
            "processing_version": processing_version,
            "status": "candidate",
            "semantic_checked": False,
            **transform_record,
        },
    )
    cache.put(
        "overlay",
        cache_key,
        mapped,
        {
            "audit": audit.as_dict(),
            "background_mode": effective_mode,
            "chroma_key": list(key) if key else None,
            "transform": transform_record,
            "normalized": True,
            "pipeline_version": OVERLAY_PIPELINE_VERSION,
            "processing_version": processing_version,
            "image_fingerprint": asset_fingerprint(mapped),
        },
    )
    return mapped, audit, effective_mode, key, transform_record


def _overlay_edge_preview(image: Image.Image, path: Path) -> None:
    image = image.copy()
    image.thumbnail((800, 600), Image.Resampling.LANCZOS)
    margin = 16
    width = image.width * 2 + margin * 3
    height = image.height + margin * 2
    preview = Image.new("RGBA", (width, height), "white")
    dark = Image.new("RGBA", (image.width, image.height), "#202020")
    light = Image.new("RGBA", (image.width, image.height), "#F5F5F5")
    light.alpha_composite(image)
    dark.alpha_composite(image)
    preview.alpha_composite(light, (margin, margin))
    preview.alpha_composite(dark, (image.width + margin * 2, margin))
    atomic_save_image(preview, path)


def _build_overlay(
    overlay: dict[str, Any],
    spec: dict[str, Any],
    spec_path: Path,
    crop: Image.Image | None,
    output_dir: Path,
    work_dir: Path,
    cache: NodeCache,
    provider: ImageProvider | None,
) -> tuple[dict[str, Any], ProviderAudit, tuple[int, int, int, int] | None, list[dict]]:
    key: tuple[int, int, int] | None = None
    if overlay["action"] == "basic_shape":
        generated = _make_basic_shape(overlay)
        audit = _import_audit("local-basic-shape")
        transform_record = _identity_transform(generated.size, kind="local_basic_shape")
    elif overlay["prepared_asset"] is not None:
        generated = decode_image(
            resolve_input_path(spec_path, overlay["prepared_asset"])
        )
        audit = _import_audit("imported-overlay")
        transform_record = _identity_transform(generated.size, kind="imported_asset")
    else:
        if provider is None:
            raise CollageError(
                "IMAGE_PROVIDER_UNAVAILABLE",
                f"overlay {overlay['id']} 没有 prepared_asset 且未配置图片 provider",
            )
        if crop is None:
            raise CollageError(
                "OVERLAY_CROP_MISSING", f"overlay {overlay['id']} 缺少原参考 crop"
            )
        capabilities = provider.capabilities
        # Retain the legacy key fields so existing paid outputs remain discoverable;
        # the inspection model is not invoked by the candidate pipeline.
        cache_key = stable_hash(
            {
                "source_sha256": spec["reference"]["sha256"],
                "text_content": overlay.get("text_content"),
                "inspection_model": getattr(
                    getattr(provider, "settings", None), "vlm_model", None
                ),
                "inspection_reasoning": getattr(
                    getattr(provider, "settings", None), "vlm_reasoning_effort", None
                ),
                "source_rect": overlay["source_rect"],
                "brief": overlay["generation_brief"],
                "prompt_version": OVERLAY_PROMPT_VERSION,
                "background_mode": overlay["background_mode"],
                "chroma_key": overlay["chroma_key"],
                "chroma_tolerance": overlay["chroma_tolerance"],
                "provider": capabilities.name,
                **cache_configuration(provider),
                "requested_model": capabilities.requested_model,
                "supports_transparency": capabilities.supports_transparency,
                "output_sizes": [list(size) for size in capabilities.output_sizes],
            }
        )
        generated, audit, _effective_mode, key, transform_record = _provider_overlay(
            provider, crop, overlay, cache, cache_key
        )

    original = generated
    if (
        overlay["action"] == "reference_generate"
        and overlay["prepared_asset"] is not None
        and overlay["background_mode"] == "chroma_key"
    ):
        key = (
            tuple(overlay["chroma_key"])
            if overlay["chroma_key"]
            else choose_chroma_key(crop)
        )
        generated = remove_chroma_background(
            generated, key, tolerance=overlay["chroma_tolerance"]
        )
    rgba = generated.convert("RGBA")
    warnings = []
    if overlay["action"] in {"reference_generate", "preserve"}:
        technical = transform_record.get("technical") or alpha_completeness(rgba)
        warnings = [overlay_warning(overlay, code) for code in technical["issues"]]
    visible_bbox = rgba.getchannel("A").getbbox()
    if overlay["action"] == "reference_generate":
        visible_bbox = (
            transform_record["content_box"]
            if "content_box" in transform_record
            else content_box(rgba, key, original)
        )
        atomic_save_image(rgba, work_dir / f"overlay_{overlay['id']}_full.png")
        if visible_bbox is not None:
            transform_record["content_box"] = list(visible_bbox)
            # The target rect describes the subject, not model-generated margins.
            # Crop only the template copy; keep the full-resolution evidence above.
            rgba = rgba.crop(visible_bbox)
    if visible_bbox is None:
        raise CollageError(
            "EMPTY_OVERLAY", f"固定叠加素材 {overlay['id']} 没有可见内容"
        )
    output_path = output_dir / "assets" / f"overlay_{overlay['id']}.png"
    atomic_save_image(rgba, output_path)
    atomic_write_json(
        work_dir / f"overlay_{overlay['id']}_transform.json", transform_record
    )
    _overlay_edge_preview(
        rgba, work_dir / "previews" / f"overlay_{overlay['id']}_edges.png"
    )
    asset = {
        "id": overlay["id"],
        "path": f"assets/overlay_{overlay['id']}.png",
        "role": "overlay",
        "requires_alpha": alpha_is_meaningful(rgba),
        "sha256": sha256_file(output_path),
    }
    return asset, audit, visible_bbox, warnings
