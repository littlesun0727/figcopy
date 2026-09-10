"""集中处理哈希、安全路径、原子写入和图片解码。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from .errors import CollageError


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """流式计算文件哈希，避免把客户图片完整载入日志或内存副本。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def stable_hash(value: Any) -> str:
    """为可 JSON 序列化的节点输入生成稳定缓存键。"""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise CollageError("FILE_NOT_FOUND", f"找不到 JSON 文件：{path}") from exc
    except json.JSONDecodeError as exc:
        raise CollageError(
            "INVALID_JSON",
            f"JSON 解析失败：{path}",
            details={"line": exc.lineno, "column": exc.colno, "reason": exc.msg},
        ) from exc


def _atomic_target(path: Path) -> tuple[int, Path]:
    path.parent.mkdir(parents=True, exist_ok=True)
    return tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """先完整写入同目录临时文件，再原子替换目标。"""

    descriptor, temp_name = _atomic_target(path)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: Any) -> None:
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    atomic_write_bytes(path, data)


def atomic_save_image(
    image: Image.Image, path: Path, *, format_name: str = "PNG"
) -> None:
    """原子保存图片，避免中断后留下可见但损坏的文件。"""

    descriptor, temp_name = _atomic_target(path)
    os.close(descriptor)
    temp_path = Path(temp_name)
    try:
        image.save(temp_path, format=format_name)
        # Windows 的 fsync 需要可写句柄；rb+ 仍不会改变文件内容。
        with temp_path.open("rb+") as handle:
            os.fsync(handle.fileno())
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def decode_image(path: Path, *, mode: str | None = None) -> Image.Image:
    """完整解码图片并返回与文件句柄解耦的副本。"""

    try:
        with Image.open(path) as source:
            source.load()
            image = source.copy()
    except FileNotFoundError as exc:
        raise CollageError("FILE_NOT_FOUND", f"找不到图片：{path}") from exc
    except (UnidentifiedImageError, OSError) as exc:
        raise CollageError("IMAGE_DECODE_FAILED", f"图片无法解码：{path}") from exc
    return image.convert(mode) if mode else image


def safe_package_path(root: Path, relative: str) -> Path:
    """将包内 POSIX 相对路径解析到 root，并阻止绝对路径和目录穿越。"""

    if not isinstance(relative, str) or not relative.strip():
        raise CollageError("UNSAFE_PACKAGE_PATH", "模板包路径不能为空")
    normalized = relative.replace("\\", "/")
    candidate_path = Path(normalized)
    reserved_names = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
    has_reserved_name = any(
        part.split(".", 1)[0].upper() in reserved_names for part in candidate_path.parts
    )
    if (
        candidate_path.is_absolute()
        or candidate_path.drive
        or ".." in candidate_path.parts
        or ":" in normalized  # 阻止 Windows NTFS alternate data stream。
        or "\x00" in normalized
        or has_reserved_name
    ):
        raise CollageError("UNSAFE_PACKAGE_PATH", f"模板包路径不安全：{relative}")
    root_resolved = root.resolve()
    candidate = (root_resolved / candidate_path).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise CollageError(
            "UNSAFE_PACKAGE_PATH", f"模板包路径越界：{relative}"
        ) from exc
    return candidate


def resolve_input_path(spec_file: Path, value: str) -> Path:
    """解析制作端/Bindings 路径；相对路径相对于其 JSON 文件。"""

    path = Path(value)
    return path.resolve() if path.is_absolute() else (spec_file.parent / path).resolve()
