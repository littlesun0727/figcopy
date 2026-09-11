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
    chroma_alpha_is_clean,
    parse_color,
    rect_to_box,
    remove_chroma_background,
)
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
    semantic_completeness,
)

LOGGER = logging.getLogger(__name__)
OVERLAY_PROMPT_VERSION = "reference-overlay/5"


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
    requested_mode = overlay["background_mode"]
    effective_mode = (
        requested_mode
        if requested_mode == "chroma_key" or capabilities.supports_transparency
        else "chroma_key"
    )
    processing_version = (
        CHROMA_PROCESSING_VERSION if effective_mode == "chroma_key" else None
    )
    cached = cache.get("overlay", cache_key)
    if (
        cached is not None
        and cached.metadata.get("validated_version") == OVERLAY_PROMPT_VERSION
        and cached.metadata.get("processing_version") == processing_version
        and cached.metadata.get("image_fingerprint") == asset_fingerprint(cached.image)
    ):
        metadata = cached.metadata
        key = tuple(metadata["chroma_key"]) if metadata.get("chroma_key") else None
        return (
            cached.image,
            _audit_from_cache(metadata),
            metadata["background_mode"],
            key,
            metadata["transform"],
        )
    key = None
    if effective_mode == "chroma_key":
        require_chroma_backend()
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
    attempt_root = cache.root.parent / "overlay_attempts" / cache_key
    attempt_root.mkdir(parents=True, exist_ok=True)
    issues: list[str] = []
    semantic = {}
    for attempt in range(2):
        record_path = attempt_root / f"{attempt}.json"
        raw_path = attempt_root / f"{attempt}_raw.png"
        request_brief = (
            overlay["generation_brief"]
            + "\n只制作这一件完整独立素材。补全遮挡或截断的部分，禁止附带相邻照片、边框和背景残片。"
            "保留所有细线，四周至少留 8% 空白。"
        )
        if overlay.get("text_content"):
            request_brief += (
                "\n必须逐字包含且仅包含客户确认的文字：" + overlay["text_content"]
            )
        if attempt:
            request_brief += "\n上一张检查失败，请重新生成完整素材并修正：" + ", ".join(
                issues
            )
            request_brief += "\n检查观察：" + str(
                semantic.get("result", {}).get("issues", [])
            )
        if record_path.exists():
            record = read_json(record_path)
            # Keep uncertain requests blocked even if a new local keyer fails a technical gate.
            if record.get("inspection_status") == "pending":
                raise CollageError(
                    "OVERLAY_INSPECTION_UNCERTAIN",
                    "素材视觉检查已发送但未落盘，停止自动重复请求",
                )
            if (
                record.get("status") == "rejected"
                and record.get("processing_version") == processing_version
            ):
                issues = record["issues"]
                semantic = record.get("semantic", {})
                continue
            # A completed output may be reused; a transport timeout cannot safely be replayed.
            if (
                record.get("status") not in {"generated", "accepted", "rejected"}
                or not raw_path.exists()
            ):
                raise CollageError(
                    "OVERLAY_REQUEST_UNCERTAIN",
                    "该素材请求已发送但结果未落盘，停止自动重复计费",
                )
            if record.get("raw_sha256") != sha256_file(raw_path):
                raise CollageError(
                    "OVERLAY_EVIDENCE_CHANGED",
                    "素材原始输出与已检查证据不一致，停止复用",
                )
            raw = decode_image(raw_path)
            audit = _audit_from_cache(record)
        else:
            record = {"status": "pending", "attempt": attempt, "prompt": request_brief}
            atomic_write_json(record_path, record)
            generated = provider.make_overlay(
                provider_crop,
                brief=request_brief,
                background_mode=effective_mode,
                chroma_key=key,
            )
            raw = (
                generated.raw_image
                if generated.raw_image is not None
                else generated.image
            )
            audit = generated.audit
            atomic_save_image(raw, raw_path)
            record.update(
                status="generated",
                audit=audit.as_dict(),
                raw_sha256=sha256_file(raw_path),
                provider_transform=generated.transform,
            )
            atomic_write_json(record_path, record)
        LOGGER.info(
            "检查素材完整性与透明边缘 | id=%s attempt=%s", overlay["id"], attempt + 1
        )
        mapped = raw.convert("RGBA")
        if effective_mode == "chroma_key":
            mapped = remove_chroma_background(
                mapped, key or (255, 0, 255), tolerance=overlay["chroma_tolerance"]
            )
        technical = alpha_completeness(mapped)
        issues = list(technical["issues"])
        if effective_mode == "chroma_key" and not chroma_alpha_is_clean(mapped):
            issues.append("OPAQUE_OVERLAY")
        # PyAV supplies the soft matte and corrected colors. Erosion would change
        # the user-reviewed output and can remove an entire one-pixel stroke.
        cleaned = mapped
        retained = 1.0
        # Inspect the actual file pixels at their final resolution, before trimming.
        if cleaned.size != crop.size:
            cleaned, output_transform = pad_for_model(cleaned, crop.size)
            transform_record["output_mapping"] = {
                "kind": "contain_padding",
                **output_transform.as_dict(),
            }
        if cleaned.getchannel("A").getbbox() is None:
            issues.append("EMPTY_OVERLAY")
        atomic_save_image(cleaned, attempt_root / f"{attempt}_candidate.png")
        semantic = {}
        candidate_fingerprint = asset_fingerprint(cleaned)
        if not issues:
            # A different local keyer must not inherit a verdict for old candidate pixels.
            # Keep the generation cache key unchanged so raw outputs and the two-call cap survive.
            if (
                record.get("semantic")
                and record.get("processing_version") == processing_version
                and (
                    processing_version is None
                    or record.get("inspection_fingerprint") == candidate_fingerprint
                )
            ):
                semantic = record["semantic"]
            else:
                record["inspection_status"] = "pending"
                atomic_write_json(record_path, record)
                semantic = semantic_completeness(provider, crop, cleaned, overlay)
            issues.extend(semantic["issues"])
        record.update(
            status="rejected" if issues else "accepted",
            issues=issues,
            technical=technical,
            alpha_mass_retained=retained,
            semantic=semantic,
            inspection_status="complete",
            processing_version=processing_version,
            inspection_fingerprint=candidate_fingerprint,
        )
        atomic_write_json(record_path, record)
        if issues:
            LOGGER.warning(
                "素材检查未通过 | id=%s attempt=%s codes=%s",
                overlay["id"],
                attempt + 1,
                issues,
            )
            continue
        mapped = cleaned
        transform_record["completeness"] = {
            "attempt": attempt + 1,
            "technical": technical,
            "semantic": semantic,
            "alpha_mass_retained": retained,
            "processing_version": processing_version,
        }
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
                "validated_version": OVERLAY_PROMPT_VERSION,
                "processing_version": processing_version,
                "image_fingerprint": asset_fingerprint(mapped),
            },
        )
        return mapped, audit, effective_mode, key, transform_record
    raise CollageError(
        "OVERLAY_COMPLETENESS_FAILED",
        "素材在一次局部重试后仍不完整，请检查单件素材记录",
        details={"overlay_id": overlay["id"], "issues": issues},
    )


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
        generated = remove_chroma_background(
            generated, key, tolerance=overlay["chroma_tolerance"]
        )
    rgba = generated.convert("RGBA")
    if overlay["action"] in {"reference_generate", "preserve"}:
        if key is not None and not chroma_alpha_is_clean(rgba):
            raise CollageError(
                "OPAQUE_OVERLAY",
                f"overlay {overlay['id']} 的色键背景仍覆盖边缘；请重试或提供透明 PNG",
            )
        if key is None and not alpha_is_meaningful(rgba):
            raise CollageError(
                "OPAQUE_OVERLAY",
                f"overlay {overlay['id']} 的 alpha 全白；不能把扩展名或棋盘格当作透明通道",
            )
    # Keep the validated transparent margins. Trimming would hide boundary failures
    # and change the scale relationship between the isolated subject and its layer.
    visible_bbox = rgba.getchannel("A").getbbox()
    if visible_bbox is None:
        raise CollageError(
            "EMPTY_OVERLAY", f"固定叠加素材 {overlay['id']} 没有可见像素"
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
