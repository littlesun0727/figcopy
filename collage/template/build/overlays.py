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
    resolve_input_path,
    sha256_file,
    stable_hash,
)
from ...core.state import NodeCache
from ...imaging.geometry import pad_for_model, restore_from_model
from ...imaging.operations import (
    alpha_is_meaningful,
    choose_chroma_key,
    clean_chroma_edges,
    parse_color,
    rect_to_box,
    remove_chroma_key,
    trim_transparent,
)
from ...providers import ImageProvider, ProviderAudit
from .common import (
    _audit_from_cache,
    _call_with_retry,
    _choose_provider_size,
    _identity_transform,
    _import_audit,
)

LOGGER = logging.getLogger(__name__)
OVERLAY_PROMPT_VERSION = "reference-overlay/1"


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
    capabilities = provider.capabilities
    if not capabilities.supports_reference_image:
        raise CollageError(
            "IMAGE_PROVIDER_CAPABILITY_MISSING",
            "图片 provider 不支持参考图片输入，无法制作 overlay",
            details={"provider": capabilities.name},
        )
    cached = cache.get("overlay", cache_key)
    if cached is not None and cached.metadata.get("normalized") is True:
        metadata = cached.metadata
        key = tuple(metadata["chroma_key"]) if metadata.get("chroma_key") else None
        return (
            cached.image,
            _audit_from_cache(metadata),
            metadata["background_mode"],
            key,
            metadata["transform"],
        )
    requested_mode = overlay["background_mode"]
    effective_mode = (
        requested_mode
        if requested_mode == "chroma_key" or capabilities.supports_transparency
        else "chroma_key"
    )
    key = None
    if effective_mode == "chroma_key":
        key = (
            tuple(overlay["chroma_key"])
            if overlay["chroma_key"]
            else choose_chroma_key(crop)
        )
    provider_crop = crop
    transform_record = _identity_transform(crop.size)
    target_size = _choose_provider_size(crop.size, capabilities.output_sizes)
    if target_size is not None:
        fill = (
            (*key, 255)
            if effective_mode == "chroma_key" and key is not None
            else (0, 0, 0, 0)
        )
        provider_crop, transform = pad_for_model(crop, target_size, fill=fill)
        transform_record = {"kind": "contain_padding", **transform.as_dict()}
    LOGGER.info(
        "调用图片 provider 制作 overlay | id=%s provider=%s mode=%s",
        overlay["id"],
        capabilities.name,
        effective_mode,
    )
    generated = _call_with_retry(
        lambda: provider.make_overlay(
            provider_crop,
            brief=overlay["generation_brief"],
            background_mode=effective_mode,
            chroma_key=key,
        ),
        operation=f"overlay:{overlay['id']}",
    )
    if generated.transform is not None:
        transform_record = {
            **transform_record,
            "provider_output": generated.transform,
        }
    mapped = (
        restore_from_model(generated.image, transform)
        if target_size is not None
        else generated.image
    )
    if effective_mode == "chroma_key":
        mapped = remove_chroma_key(
            mapped, key or (255, 0, 255), tolerance=overlay["chroma_tolerance"]
        )
    else:
        mapped = mapped.convert("RGBA")
    if not alpha_is_meaningful(mapped.convert("RGBA")):
        raise CollageError(
            "OPAQUE_OVERLAY",
            f"overlay {overlay['id']} 的 provider 结果没有真实透明像素",
        )
    cache.put(
        "overlay",
        cache_key,
        mapped,
        {
            "audit": generated.audit.as_dict(),
            "background_mode": effective_mode,
            "chroma_key": list(key) if key else None,
            "transform": transform_record,
            "normalized": True,
        },
    )
    return mapped, generated.audit, effective_mode, key, transform_record


def _overlay_edge_preview(image: Image.Image, path: Path) -> None:
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
) -> tuple[dict[str, Any], ProviderAudit, tuple[int, int, int, int] | None]:
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
        cache_key = stable_hash(
            {
                "source_sha256": spec["reference"]["sha256"],
                "source_rect": overlay["source_rect"],
                "brief": overlay["generation_brief"],
                "prompt_version": OVERLAY_PROMPT_VERSION,
                "background_mode": overlay["background_mode"],
                "chroma_key": overlay["chroma_key"],
                "chroma_tolerance": overlay["chroma_tolerance"],
                "provider": capabilities.name,
                "requested_model": capabilities.requested_model,
                "supports_transparency": capabilities.supports_transparency,
                "output_sizes": [list(size) for size in capabilities.output_sizes],
            }
        )
        generated, audit, _effective_mode, key, transform_record = _provider_overlay(
            provider, crop, overlay, cache, cache_key
        )

    if (
        overlay["action"] == "reference_generate"
        and overlay["prepared_asset"] is not None
        and overlay["background_mode"] == "chroma_key"
    ):
        if crop is None:
            raise CollageError(
                "OVERLAY_CROP_MISSING", f"overlay {overlay['id']} 缺少原参考 crop"
            )
        key = (
            tuple(overlay["chroma_key"])
            if overlay["chroma_key"]
            else choose_chroma_key(crop)
        )
        generated = remove_chroma_key(
            generated, key, tolerance=overlay["chroma_tolerance"]
        )
    rgba = generated.convert("RGBA")
    if overlay["action"] == "reference_generate" and not alpha_is_meaningful(rgba):
        raise CollageError(
            "OPAQUE_OVERLAY",
            f"overlay {overlay['id']} 的 alpha 全白；不能把扩展名或棋盘格当作透明通道",
        )
    if overlay["action"] == "reference_generate" and key is not None:
        before_bbox = rgba.getchannel("A").getbbox()
        rgba = clean_chroma_edges(rgba)
        after_bbox = rgba.getchannel("A").getbbox()
        LOGGER.debug(
            "已清理 overlay 色键边缘 | id=%s before_bbox=%s after_bbox=%s",
            overlay["id"],
            before_bbox,
            after_bbox,
        )
    if overlay["action"] == "reference_generate":
        rgba, visible_bbox = trim_transparent(rgba)
    else:
        visible_bbox = rgba.getchannel("A").getbbox()
        if visible_bbox is None:
            raise CollageError(
                "EMPTY_OVERLAY", f"basic_shape {overlay['id']} 没有可见像素"
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
        "requires_alpha": overlay["action"] == "reference_generate"
        or alpha_is_meaningful(rgba),
        "sha256": sha256_file(output_path),
    }
    return asset, audit, visible_bbox
