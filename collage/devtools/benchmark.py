"""Record sample fingerprints and local capabilities without exporting source images."""

from __future__ import annotations

import hashlib
import importlib.metadata
import logging
import os
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageOps

from ..core.errors import CollageError
from ..core.io import atomic_write_json, read_json, safe_package_path, sha256_file
from ..providers.yibu.constants import (
    DEFAULT_IMAGE_MODEL,
    DEFAULT_VLM_MODEL,
    KIMI_K3_REASONING_EFFORT,
)

LOGGER = logging.getLogger(__name__)


def _git(root: Path, *args: str) -> bytes:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), *args],
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CollageError("BASELINE_GIT_UNAVAILABLE", "无法读取仓库基线") from exc


def _workspace_fingerprint(root: Path) -> dict:
    status = _git(root, "status", "--porcelain=v1", "-z")
    # 补丁可能含私人内容；只持久化指纹，不将补丁或未跟踪文件正文写进报告。
    digest = hashlib.sha256(_git(root, "diff", "HEAD", "--binary"))
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    names = [name.decode("utf-8") for name in untracked.split(b"\0") if name]
    for name in sorted(names):
        path = safe_package_path(root, name)
        if path.is_file():
            digest.update(name.encode("utf-8"))
            digest.update(bytes.fromhex(sha256_file(path)))
    return {
        "head": _git(root, "rev-parse", "HEAD").decode("ascii").strip(),
        "dirty": bool(status),
        "status_sha256": hashlib.sha256(status).hexdigest(),
        "working_tree_sha256": digest.hexdigest(),
        "untracked_file_count": len(names),
    }


def local_capabilities() -> dict:
    """只盘点安装版本及配置存在性；不导入凭据模块或触发权重下载。"""

    packages = {}
    for name in (
        "Pillow",
        "pytest",
        "torch",
        "torchvision",
        "transformers",
        "timm",
        "kornia",
        "einops",
        "numpy",
        "safetensors",
    ):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {
        "python": platform.python_version(),
        "packages": packages,
        "default_models": {
            "vision": DEFAULT_VLM_MODEL,
            "reasoning_effort": KIMI_K3_REASONING_EFFORT,
            "image": DEFAULT_IMAGE_MODEL,
        },
        "credential_config_present_in_process": any(
            bool(os.environ.get(name, "").strip())
            for name in ("YIBU_API_KEY", "YIBU_CREDENTIALS_FILE", "YIBU_SHARED_PATH")
        ),
        "local_weights_path_configured": bool(
            os.environ.get("COLLAGE_BIREFNET_MODEL_PATH", "").strip()
        ),
        "model_loading_tested": False,
        "real_provider_tested": False,
        "network_calls": 0,
    }


def record_baseline(
    source_dir: Path,
    catalog_path: Path,
    output_path: Path,
    *,
    repository: Path,
) -> Path:
    """核对原始 JPG 哈希并记录 EXIF 后像素指纹；不保存图片或制作端绝对路径。"""

    if output_path.exists():
        raise CollageError("OUTPUT_EXISTS", "基线记录已存在，请使用新的文件名")
    catalog = read_json(catalog_path)
    if (
        not isinstance(catalog, dict)
        or catalog.get("version") != "auto-rebuild-catalog/1"
        or not isinstance(catalog.get("samples"), list)
        or not catalog["samples"]
    ):
        raise CollageError("BASELINE_CATALOG_INVALID", "样板清单格式无效")
    samples = []
    identifiers: set[str] = set()
    filenames: set[str] = set()
    LOGGER.info("开始登记样板基准 | count=%s", len(catalog["samples"]))
    for entry in catalog["samples"]:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("id"), str)
            or not isinstance(entry.get("filename"), str)
            or entry["id"] in identifiers
            or entry["filename"] in filenames
        ):
            raise CollageError("BASELINE_CATALOG_INVALID", "样板 ID 和文件名必须唯一")
        identifiers.add(entry["id"])
        filenames.add(entry["filename"])
        path = safe_package_path(source_dir.resolve(), entry["filename"])
        actual = sha256_file(path)
        if actual != entry.get("sha256"):
            raise CollageError(
                "BASELINE_SOURCE_CHANGED",
                "样板文件指纹已变化",
                details={"sample_id": entry["id"]},
            )
        try:
            with Image.open(path) as image:
                source_size = image.size
                orientation = image.getexif().get(274, 1)
                normalized = ImageOps.exif_transpose(image).convert("RGBA")
        except (OSError, ValueError) as exc:
            raise CollageError(
                "BASELINE_IMAGE_INVALID",
                "样板无法解码",
                details={"sample_id": entry["id"]},
            ) from exc
        sample = {
            "id": entry["id"],
            "filename": entry["filename"],
            "source_sha256": actual,
            "source_bytes": path.stat().st_size,
            "source_size": list(source_size),
            "exif_orientation": orientation,
            "normalized_size": list(normalized.size),
            "normalized_mode": normalized.mode,
            "normalized_pixels_sha256": hashlib.sha256(
                normalized.tobytes()
            ).hexdigest(),
            "transform": "EXIF transpose; no resizing; RGBA",
            "annotation_status": entry.get("evaluation", {}).get("status", "pending"),
        }
        samples.append(sample)
        LOGGER.debug("样板指纹已核对 | sample=%s size=%s", entry["id"], normalized.size)
    record = {
        "version": "auto-rebuild-baseline/1",
        "created_at": datetime.now(UTC).isoformat(),
        "catalog_sha256": sha256_file(catalog_path),
        "repository": _workspace_fingerprint(repository.resolve()),
        "capabilities": local_capabilities(),
        "samples": samples,
        "real_model_results": [],
        "automatic_rebuild_coverage": None,
        "blockers": [
            "REAL_TRIAL_AUTHORIZATION_AND_BUDGET_PENDING",
            "EVALUATION_ANNOTATIONS_PENDING",
        ],
    }
    if not record["capabilities"]["credential_config_present_in_process"]:
        record["blockers"].append("YIBU_CREDENTIAL_MISSING_IN_PROCESS")
    atomic_write_json(output_path, record)
    LOGGER.info("样板基准已登记 | verified=%s model_calls=0", len(samples))
    return output_path
