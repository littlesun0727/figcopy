"""把 ReviewedSpec 制作为可审核模板包，并管理缓存、状态、审计和发布。"""

from __future__ import annotations

import html
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from . import __version__
from .cache import NodeCache, WorkflowState
from .errors import CollageError
from .geometry import AffineRectTransform, pad_for_model, restore_from_model
from .io_utils import (
    atomic_save_image,
    atomic_write_bytes,
    atomic_write_json,
    decode_image,
    read_json,
    resolve_input_path,
    sha256_file,
    stable_hash,
)
from .prepare import (
    alpha_is_meaningful,
    choose_chroma_key,
    clean_chroma_edges,
    convert_remove_mask_polarity,
    crop_source,
    load_mask,
    make_blend_mask,
    parse_color,
    protected_background_compose,
    rect_to_box,
    remove_chroma_key,
    trim_transparent,
)
from .providers import GeneratedImage, ImageProvider, ProviderAudit
from .schema import validate_reviewed_spec, validate_template_spec
from .validate import validate_package

LOGGER = logging.getLogger(__name__)
BACKGROUND_PROMPT_VERSION = "clean-background/1"
OVERLAY_PROMPT_VERSION = "reference-overlay/1"
TRANSIENT_PROVIDER_CODES = {"RATE_LIMITED", "TEMPORARY_NETWORK_ERROR"}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _copy_atomic(source: Path, target: Path) -> None:
    atomic_write_bytes(target, source.read_bytes())


def _import_audit(name: str) -> ProviderAudit:
    return ProviderAudit(name, None, None, None, False, 0)


def _audit_from_cache(metadata: dict[str, Any]) -> ProviderAudit:
    raw = metadata["audit"]
    return ProviderAudit(
        name=raw["name"],
        requested_model=raw.get("requested_model"),
        actual_model=raw.get("actual_model"),
        request_id=raw.get("request_id"),
        fixture=bool(raw.get("fixture", False)),
        elapsed_ms=int(raw.get("elapsed_ms", 0)),
        cache_hit=True,
    )


def _call_with_retry(
    call: Any, *, operation: str, max_attempts: int = 3
) -> GeneratedImage:
    """只对明确的临时错误有限重试；超时、权限和参数错误不盲目重发。"""

    started = time.monotonic()
    for attempt in range(1, max_attempts + 1):
        try:
            return call()
        except CollageError as exc:
            if exc.code not in TRANSIENT_PROVIDER_CODES or attempt == max_attempts:
                raise
            if time.monotonic() - started > 20:
                raise CollageError(
                    "PROVIDER_RETRY_BUDGET_EXHAUSTED", f"{operation} 已超过重试总时长"
                ) from exc
            delay = 0.25 * (2 ** (attempt - 1))
            LOGGER.warning(
                "provider 临时失败，准备有限重试 | operation=%s attempt=%s/%s",
                operation,
                attempt,
                max_attempts,
            )
            time.sleep(delay)
    raise AssertionError("retry loop must return or raise")


def _choose_provider_size(
    source_size: tuple[int, int],
    supported_sizes: tuple[tuple[int, int], ...],
) -> tuple[int, int] | None:
    """选择宽高比最接近的 provider 尺寸；返回 None 表示无需补边。"""

    if not supported_sizes or source_size in supported_sizes:
        return None
    if any(min(size) <= 0 for size in supported_sizes):
        raise CollageError(
            "INVALID_PROVIDER_CAPABILITIES", "provider 声明了非法 output_sizes"
        )
    source_ratio = source_size[0] / source_size[1]
    return min(
        supported_sizes, key=lambda size: abs((size[0] / size[1]) - source_ratio)
    )


def _pad_mask(mask: Image.Image, transform: AffineRectTransform) -> Image.Image:
    """使用与参考图相同的几何变换补边，新增区域保持内部语义的黑=不编辑。"""

    resized = mask.convert("L").resize(transform.content_size, Image.Resampling.LANCZOS)
    output = Image.new("L", transform.target_size, 0)
    output.paste(resized, (round(transform.offset_x), round(transform.offset_y)))
    return output


def _identity_transform(
    size: tuple[int, int], *, kind: str = "identity"
) -> dict[str, Any]:
    return {
        "kind": kind,
        "source_size": list(size),
        "target_size": list(size),
        "scale": [1.0, 1.0],
        "offset": [0.0, 0.0],
        "content_size": list(size),
    }


def _provider_background(
    provider: ImageProvider,
    reference: Image.Image,
    remove_mask: Image.Image,
    brief: str,
    cache: NodeCache,
    cache_key: str,
) -> GeneratedImage:
    capabilities = provider.capabilities
    if not capabilities.supports_reference_image or not capabilities.supports_mask_edit:
        raise CollageError(
            "IMAGE_PROVIDER_CAPABILITY_MISSING",
            "图片 provider 不同时支持参考图和 mask 编辑",
            details={"provider": capabilities.name},
        )
    cached = cache.get("background", cache_key)
    if cached is not None:
        LOGGER.info("背景候选缓存命中 | key=%s", cache_key[:12])
        return GeneratedImage(
            cached.image,
            _audit_from_cache(cached.metadata),
            cached.metadata.get("transform"),
        )
    provider_reference = reference
    internal_mask = remove_mask
    transform_record = _identity_transform(reference.size)
    target_size = _choose_provider_size(reference.size, capabilities.output_sizes)
    if target_size is not None:
        provider_reference, transform = pad_for_model(reference, target_size)
        internal_mask = _pad_mask(remove_mask, transform)
        transform_record = {"kind": "contain_padding", **transform.as_dict()}
        LOGGER.info(
            "背景输入已等比补边 | source=%s target=%s", reference.size, target_size
        )
    provider_mask = convert_remove_mask_polarity(
        internal_mask, capabilities.mask_polarity
    )
    LOGGER.info(
        "调用图片 provider 制作背景 | provider=%s model=%s",
        capabilities.name,
        capabilities.requested_model,
    )
    generated = _call_with_retry(
        lambda: provider.edit_background(
            provider_reference, provider_mask, brief=brief
        ),
        operation="background",
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
    cache.put(
        "background",
        cache_key,
        mapped,
        {"audit": generated.audit.as_dict(), "transform": transform_record},
    )
    return GeneratedImage(mapped, generated.audit, transform_record)


def _build_background(
    spec: dict[str, Any],
    spec_path: Path,
    reference: Image.Image,
    output_dir: Path,
    work_dir: Path,
    cache: NodeCache,
    provider: ImageProvider | None,
) -> tuple[dict[str, Any], ProviderAudit]:
    config = spec["background"]
    size = reference.size
    core = load_mask(
        resolve_input_path(spec_path, config["remove_mask"]), size, name="remove_mask"
    )
    allowed = (
        load_mask(
            resolve_input_path(spec_path, config["allowed_mask"]),
            size,
            name="allowed_mask",
        )
        if config["allowed_mask"] is not None
        else None
    )
    blend = make_blend_mask(
        core,
        allowed_mask=allowed,
        expand_px=config["expand_px"],
        feather_px=config["feather_px"],
    )
    atomic_save_image(core, work_dir / "remove_mask.png")
    atomic_save_image(blend, work_dir / "blend_mask.png")
    key_data: dict[str, Any] = {
        "source_sha256": spec["reference"]["sha256"],
        "remove_mask_sha256": sha256_file(
            resolve_input_path(spec_path, config["remove_mask"])
        ),
        "allowed_mask_sha256": sha256_file(
            resolve_input_path(spec_path, config["allowed_mask"])
        )
        if config["allowed_mask"]
        else None,
        "brief": config["background_brief"],
        "prompt_version": BACKGROUND_PROMPT_VERSION,
        "expand_px": config["expand_px"],
        "feather_px": config["feather_px"],
    }
    if config["candidate_path"] is not None:
        candidate_path = resolve_input_path(spec_path, config["candidate_path"])
        key_data["imported_sha256"] = sha256_file(candidate_path)
        candidate = decode_image(candidate_path).convert("RGBA")
        audit = _import_audit("imported-background")
        LOGGER.info("使用导入的背景候选 | file=%s", candidate_path.name)
    else:
        if provider is None:
            raise CollageError(
                "IMAGE_PROVIDER_UNAVAILABLE",
                "没有背景候选且未配置图片编辑 provider；不能用原图或空白图伪装清版结果",
            )
        capabilities = provider.capabilities
        key_data.update(
            {
                "provider": capabilities.name,
                "requested_model": capabilities.requested_model,
                "mask_polarity": capabilities.mask_polarity,
                "output_sizes": [list(size) for size in capabilities.output_sizes],
            }
        )
        generated = _provider_background(
            provider,
            reference,
            blend,
            config["background_brief"],
            cache,
            stable_hash(key_data),
        )
        candidate, audit = generated.image.convert("RGBA"), generated.audit
        transform_record = generated.transform or _identity_transform(size)
    if config["candidate_path"] is not None:
        transform_record = _identity_transform(size, kind="imported_exact_canvas")
    if candidate.size != size:
        raise CollageError(
            "BACKGROUND_MAPPING_FAILED",
            "背景候选没有可逆映射到参考画布；拒绝直接拉伸",
            details={"expected": size, "actual": candidate.size},
        )
    background = protected_background_compose(reference, candidate, blend)
    atomic_save_image(candidate, work_dir / "background_candidate.png")
    atomic_write_json(work_dir / "background_transform.json", transform_record)
    atomic_save_image(background, work_dir / "background.png")
    asset_path = output_dir / "assets" / "background.png"
    atomic_save_image(background, asset_path)
    asset = {
        "id": "bg",
        "path": "assets/background.png",
        "role": "background",
        "requires_alpha": False,
        "sha256": sha256_file(asset_path),
    }
    return asset, audit


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


def _package_slots(
    spec: dict[str, Any], spec_path: Path, output_dir: Path
) -> list[dict[str, Any]]:
    slots: list[dict[str, Any]] = []
    for source in spec["slots"]:
        common = {
            "id": source["id"],
            "type": source["type"],
            "label": source["label"],
            "required": source["required"],
            "upload_hint": source["upload_hint"],
            "rect": source["target_rect"],
            "rotation_deg": source["rotation_deg"],
        }
        if source["type"] == "image":
            clip_path = None
            if source["clip_mask"] is not None:
                input_path = resolve_input_path(spec_path, source["clip_mask"])
                box = rect_to_box(source["target_rect"])
                mask = load_mask(
                    input_path,
                    (box[2] - box[0], box[3] - box[1]),
                    name=f"{source['id']} clip_mask",
                )
                clip_path = f"masks/{source['id']}_clip.png"
                atomic_save_image(mask, output_dir / clip_path)
            slot = {
                **common,
                "mode": source["mode"],
                "fit": source["fit"],
                "anchor": source["anchor"],
                "clip_mask": clip_path,
                "edge_fade_px": source["edge_fade_px"],
            }
        else:
            font_path = None
            if source["font_path"] is not None:
                input_path = resolve_input_path(spec_path, source["font_path"])
                if not input_path.is_file():
                    raise CollageError(
                        "FILE_NOT_FOUND", f"字体文件不存在：{input_path}"
                    )
                safe_suffix = (
                    input_path.suffix.lower()
                    if input_path.suffix.lower() in {".ttf", ".otf", ".ttc"}
                    else ".font"
                )
                font_path = f"assets/fonts/{source['id']}{safe_suffix}"
                _copy_atomic(input_path, output_dir / font_path)
            slot = {
                **common,
                "default_text": source["default_text"],
                "font_path": font_path,
                "font_size": source["font_size"],
                "fallback_approved": source["fallback_approved"],
                "color": source["color"],
                "align": source["align"],
                "max_lines": source["max_lines"],
                "line_spacing": source["line_spacing"],
            }
        slots.append(slot)
    return slots


def _template_layers(spec: dict[str, Any]) -> list[dict[str, Any]]:
    overlays = {overlay["id"]: overlay for overlay in spec["overlays"]}
    canvas = spec["canvas"]
    layers: list[dict[str, Any]] = []
    for layer in spec["layer_order"]:
        if layer["type"] == "background":
            layers.append(
                {
                    "type": "asset",
                    "asset_id": "bg",
                    "rect": [0, 0, canvas["width"], canvas["height"]],
                    "rotation_deg": 0,
                    "fit": "contain",
                    "anchor": [0.5, 0.5],
                }
            )
        elif layer["type"] == "slot":
            layers.append({"type": "slot", "slot_id": layer["id"]})
        else:
            overlay = overlays[layer["id"]]
            layers.append(
                {
                    "type": "asset",
                    "asset_id": overlay["id"],
                    "rect": overlay["target_rect"],
                    "rotation_deg": overlay["rotation_deg"],
                    "fit": "contain",
                    "anchor": [0.5, 0.5],
                }
            )
    return layers


def _write_inspection_report(
    work_dir: Path,
    output_dir: Path,
    spec: dict[str, Any],
    audits: list[dict[str, Any]],
) -> None:
    overlay_rows = "".join(
        f"<li>{html.escape(overlay['id'])}: <a href='previews/overlay_{html.escape(overlay['id'])}_edges.png'>浅/深底边缘预览</a></li>"
        for overlay in spec["overlays"]
    )
    provider_rows = "".join(
        f"<li>{html.escape(item['node'])}: {html.escape(item['name'])}, fixture={item['fixture']}, cache_hit={item['cache_hit']}</li>"
        for item in audits
    )
    package_rel = Path(os.path.relpath(output_dir, work_dir)).as_posix()
    document = f"""<!doctype html>
<!-- 本文件汇总模板制作产物，供人工视觉验收。 -->
<meta charset="utf-8"><title>Collage build inspection</title>
<style>body{{font:16px/1.5 system-ui;max-width:900px;margin:32px auto}}img{{max-width:44%;border:1px solid #aaa;margin:8px}}</style>
<h1>模板制作检查</h1>
<p>状态：needs_review。必须用新客户素材生成预览后，再执行 approve。</p>
<h2>背景保护</h2>
<img src="remove_mask.png" alt="remove mask"><img src="blend_mask.png" alt="blend mask">
<img src="background_candidate.png" alt="background candidate"><img src="background.png" alt="protected background">
<h2>Overlay 边缘</h2><ul>{overlay_rows or "<li>无 overlay</li>"}</ul>
<h2>调用审计</h2><ul>{provider_rows}</ul>
<p>模板清单：<a href="{html.escape(package_rel)}/template.json">template.json</a></p>
"""
    atomic_write_bytes(work_dir / "inspection.html", document.encode("utf-8"))


def build_template(
    spec_path: Path,
    output_dir: Path,
    *,
    work_dir: Path | None = None,
    image_provider: ImageProvider | None = None,
    force: bool = False,
) -> Path:
    """构建 needs_review 模板包；只有 approve 能把状态改成 ready。"""

    spec_path = spec_path.resolve()
    output_dir = output_dir.resolve()
    work_dir = (
        work_dir.resolve()
        if work_dir
        else output_dir.parent / f"{output_dir.name}.work"
    )
    manifest_path = output_dir / "template.json"
    if manifest_path.is_file():
        existing = read_json(manifest_path)
        if existing.get("status") == "ready":
            raise CollageError(
                "READY_TEMPLATE_PROTECTED", "已发布模板不能被 build 无声覆盖"
            )
        if not force:
            raise CollageError(
                "OUTPUT_EXISTS", "模板构建结果已存在；断点重建请显式使用 --force"
            )
    spec = validate_reviewed_spec(read_json(spec_path))
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "assets").mkdir(parents=True, exist_ok=True)
    (output_dir / "masks").mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "crops").mkdir(parents=True, exist_ok=True)
    (work_dir / "previews").mkdir(parents=True, exist_ok=True)
    state = WorkflowState(work_dir / "state.json")
    cache = NodeCache(work_dir / "cache")
    state.transition("building")
    LOGGER.info("开始构建模板 | spec=%s out=%s", spec_path, output_dir)
    try:
        reference_path = resolve_input_path(spec_path, spec["reference"]["path"])
        if sha256_file(reference_path) != spec["reference"]["sha256"]:
            raise CollageError(
                "SOURCE_HASH_MISMATCH", "参考工作图哈希与 ReviewedSpec 不一致"
            )
        reference = decode_image(reference_path).convert("RGBA")
        expected_size = (spec["canvas"]["width"], spec["canvas"]["height"])
        if reference.size != expected_size:
            raise CollageError(
                "SOURCE_SIZE_MISMATCH", "参考工作图尺寸与 ReviewedSpec canvas 不一致"
            )

        # 必须先从原参考图保存所有复杂装饰 crop，再开始任何背景清版。
        crops: dict[str, Image.Image] = {}
        for overlay in spec["overlays"]:
            if overlay["action"] == "reference_generate":
                crop = crop_source(reference, overlay["source_rect"])
                crops[overlay["id"]] = crop
                atomic_save_image(crop, work_dir / "crops" / f"{overlay['id']}.png")
        state.node("overlay_crops", "complete", count=len(crops))
        LOGGER.info("原图 overlay crop 已保存 | count=%s", len(crops))

        state.node("background", "running", input_sha256=spec["reference"]["sha256"])
        background_asset, background_audit = _build_background(
            spec, spec_path, reference, output_dir, work_dir, cache, image_provider
        )
        audits = [background_audit.as_dict(node="background")]
        state.node(
            "background",
            "complete",
            audit=audits[-1],
            outputs=[
                "background_candidate.png",
                "background.png",
                "remove_mask.png",
                "blend_mask.png",
            ],
        )
        assets = [background_asset]
        visible_regions: dict[str, list[int] | None] = {}
        for overlay in spec["overlays"]:
            state.node(
                f"overlay:{overlay['id']}",
                "running",
                source_rect=overlay["source_rect"],
            )
            asset, audit, visible_bbox = _build_overlay(
                overlay,
                spec,
                spec_path,
                crops.get(overlay["id"]),
                output_dir,
                work_dir,
                cache,
                image_provider,
            )
            assets.append(asset)
            audits.append(audit.as_dict(node=f"overlay:{overlay['id']}"))
            visible_regions[overlay["id"]] = (
                list(visible_bbox) if visible_bbox else None
            )
            state.node(
                f"overlay:{overlay['id']}",
                "complete",
                audit=audits[-1],
                visible_bbox=visible_regions[overlay["id"]],
                output=asset["path"],
            )
        slots = _package_slots(spec, spec_path, output_dir)
        template = {
            "version": "collage-template/1",
            "status": "needs_review",
            "canvas": spec["canvas"],
            "assets": assets,
            "slots": slots,
            "layers": _template_layers(spec),
            "build": {
                "source_sha256": spec["reference"]["sha256"],
                "created_at": _utc_now(),
                "tool_version": __version__,
                "fixture_used": any(item["fixture"] for item in audits),
                "providers": audits,
            },
            "review": {
                "visual_approved": False,
                "reviewer": None,
                "reviewed_at": None,
                "notes": "",
                "evidence_sha256": [],
            },
        }
        validate_template_spec(template, require_ready=False)
        atomic_write_json(manifest_path, template)
        validate_package(output_dir, require_ready=False)
        _write_inspection_report(work_dir, output_dir, spec, audits)
        state.transition("needs_review")
        LOGGER.info(
            "模板构建完成，等待人工视觉验收 | manifest=%s report=%s",
            manifest_path,
            work_dir / "inspection.html",
        )
        return manifest_path
    except CollageError as exc:
        blocked_codes = {
            "IMAGE_PROVIDER_UNAVAILABLE",
            "IMAGE_PROVIDER_CAPABILITY_MISSING",
            "EXACT_CONTENT_REQUIRED",
        }
        node_status = "blocked" if exc.code in blocked_codes else "failed"
        for name, record in list(state.data["nodes"].items()):
            if record.get("status") == "running":
                state.node(
                    name, node_status, error={"code": exc.code, "message": exc.message}
                )
        state.fail(exc.code, exc.message, blocked=exc.code in blocked_codes)
        LOGGER.error("模板构建中止 | code=%s message=%s", exc.code, exc.message)
        raise
    except Exception as exc:
        for name, record in list(state.data["nodes"].items()):
            if record.get("status") == "running":
                state.node(
                    name,
                    "failed",
                    error={"code": "UNEXPECTED_ERROR", "message": type(exc).__name__},
                )
        state.fail("UNEXPECTED_ERROR", type(exc).__name__, blocked=False)
        LOGGER.exception("模板构建发生未预期错误")
        raise


def approve_template(
    template_dir: Path,
    evidence_paths: list[Path],
    *,
    reviewer: str,
    notes: str = "",
    allow_fixture: bool = False,
    work_dir: Path | None = None,
) -> Path:
    """记录人工验收证据并发布；不允许用“文件存在”替代视觉确认。"""

    root = template_dir.resolve()
    manifest_path = root / "template.json"
    template = validate_package(root, require_ready=False)
    if template["status"] != "needs_review":
        raise CollageError("INVALID_STATE", "只有 needs_review 模板可以批准")
    if template["build"]["fixture_used"] and not allow_fixture:
        raise CollageError(
            "FIXTURE_APPROVAL_BLOCKED",
            "模板含 fixture 生成素材；真实发布需重建，演示批准须显式 --allow-fixture",
        )
    if not evidence_paths:
        raise CollageError(
            "VISUAL_EVIDENCE_REQUIRED", "至少需要一张使用新客户素材生成的视觉验收图"
        )
    evidence_hashes: list[str] = []
    first_image: Image.Image | None = None
    canvas_size = (template["canvas"]["width"], template["canvas"]["height"])
    for path in evidence_paths:
        image = decode_image(path)
        if image.size != canvas_size:
            raise CollageError(
                "PREVIEW_SIZE_MISMATCH",
                f"验收图尺寸不匹配：{path}",
                details={"expected": canvas_size, "actual": image.size},
            )
        first_image = first_image or image.convert("RGBA")
        evidence_hashes.append(sha256_file(path))
    assert first_image is not None
    original = read_json(manifest_path)
    atomic_save_image(first_image, root / "preview.png")
    template["status"] = "ready"
    template["review"] = {
        "visual_approved": True,
        "reviewer": reviewer,
        "reviewed_at": _utc_now(),
        "notes": notes,
        "evidence_sha256": evidence_hashes,
    }
    validate_template_spec(template, require_ready=True)
    atomic_write_json(manifest_path, template)
    try:
        validate_package(root, require_ready=True)
    except Exception:
        atomic_write_json(manifest_path, original)
        raise
    resolved_work = (
        work_dir.resolve() if work_dir else root.parent / f"{root.name}.work"
    )
    state_path = resolved_work / "state.json"
    if state_path.is_file():
        WorkflowState(state_path).transition("ready")
    LOGGER.info("模板已通过人工验收并发布 | path=%s reviewer=%s", root, reviewer)
    return manifest_path
