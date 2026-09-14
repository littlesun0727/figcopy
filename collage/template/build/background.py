"""Build and protect the cleaned background asset for a template."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PIL import Image

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
    convert_remove_mask_polarity,
    load_mask,
    make_blend_mask,
    protected_background_compose,
)
from ...providers.selection import cache_configuration
from ...providers import GeneratedImage, ImageProvider, ProviderAudit
from .common import (
    _audit_from_cache,
    _call_with_retry,
    _choose_provider_size,
    _identity_transform,
    _import_audit,
    _pad_mask,
)

LOGGER = logging.getLogger(__name__)
BACKGROUND_PROMPT_VERSION = "clean-background/2"


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
    if cache_configuration(provider) and generated.raw_image is not None:
        atomic_save_image(generated.raw_image, cache.root.parent / "background_raw.png")
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
    composition_mode = config.get("composition_mode", "protected")
    if composition_mode == "full_candidate":
        # 整张重建必须是已确认的制作选择，不能绕过明确的局部保护范围。
        if allowed is not None and allowed.getextrema() != (255, 255):
            raise CollageError(
                "BACKGROUND_MODE_CONFLICT",
                "整张重建与局部 allowed_mask 冲突；请使用局部保护或允许整张编辑",
            )
        blend = Image.new("L", size, 255)
    else:
        blend = make_blend_mask(
            core,
            allowed_mask=allowed,
            expand_px=config["expand_px"],
            feather_px=config["feather_px"],
        )
    LOGGER.info("背景合成方式 | mode=%s", composition_mode)
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
    if composition_mode != "protected":
        key_data["composition_mode"] = composition_mode
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
                **cache_configuration(provider),
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
    transform_record = {**transform_record, "composition_mode": composition_mode}
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
