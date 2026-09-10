"""在 Renderer 外准备 cutout 客户素材，并对云端上传要求显式授权。"""

from __future__ import annotations

import logging
from pathlib import Path

from ..core.errors import CollageError
from ..core.io import atomic_save_image, atomic_write_json, sha256_file
from ..imaging.operations import alpha_is_meaningful, multiply_alpha, normalize_image
from ..providers import CutoutProvider

LOGGER = logging.getLogger(__name__)


def prepare_cutout(
    input_path: Path,
    output_path: Path,
    *,
    provider: CutoutProvider | None = None,
    allow_cloud_upload: bool = False,
) -> Path:
    """直接保留透明 PNG，或调用已配置且已获授权的抠图 provider。"""

    image = normalize_image(input_path).convert("RGBA")
    audit: dict[str, object]
    if alpha_is_meaningful(image):
        LOGGER.info("输入已包含有效 alpha，无需抠图 provider")
        result = image
        audit = {
            "name": "existing-alpha",
            "requested_model": None,
            "actual_model": None,
            "request_id": None,
            "fixture": False,
            "cache_hit": False,
            "elapsed_ms": 0,
        }
    else:
        if provider is None:
            raise CollageError(
                "CUTOUT_PROVIDER_UNAVAILABLE",
                "普通图片没有 alpha 且未配置抠图 provider；请提供透明 PNG",
            )
        if not provider.local_only and not allow_cloud_upload:
            raise CollageError(
                "CUSTOMER_UPLOAD_NOT_AUTHORIZED",
                "该 provider 会上传客户图片；必须显式传入 --allow-cloud-upload",
            )
        LOGGER.info(
            "调用抠图 provider | provider=%s local_only=%s",
            provider.name,
            provider.local_only,
        )
        subject_alpha, provider_audit = provider.cutout(image)
        if subject_alpha.size != image.size:
            raise CollageError(
                "MASK_SIZE_MISMATCH",
                "provider 返回的 subject_alpha 与规范化客户图尺寸不一致",
                details={"expected": image.size, "actual": subject_alpha.size},
            )
        result = multiply_alpha(image, [subject_alpha.convert("L")])
        if not alpha_is_meaningful(result):
            raise CollageError(
                "CUTOUT_EMPTY_OR_OPAQUE", "抠图结果没有有效的透明/不透明分区"
            )
        audit = provider_audit.as_dict()
    atomic_save_image(result, output_path)
    atomic_write_json(
        output_path.with_suffix(output_path.suffix + ".audit.json"),
        {
            "input_sha256": sha256_file(input_path),
            "output_sha256": sha256_file(output_path),
            "provider": audit,
        },
    )
    LOGGER.info("cutout 已保存 | output=%s", output_path.resolve())
    return output_path
