"""为昂贵制作节点提供内容寻址缓存和可观察的状态记录。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from .errors import CollageError
from .io_utils import atomic_save_image, atomic_write_json, decode_image, read_json


@dataclass(slots=True)
class CachedImage:
    """缓存命中的图片及其原始调用审计。"""

    image: Image.Image
    metadata: dict[str, Any]


class NodeCache:
    """按 kind/key 保存 PNG 与 JSON；损坏条目会被视为未命中。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def get(self, kind: str, key: str) -> CachedImage | None:
        image_path = self.root / kind / f"{key}.png"
        metadata_path = self.root / kind / f"{key}.json"
        if not image_path.is_file() or not metadata_path.is_file():
            return None
        try:
            return CachedImage(decode_image(image_path), read_json(metadata_path))
        except (CollageError, OSError, ValueError):
            # 缓存不是事实来源；损坏时安全地重新计算节点。
            return None

    def put(
        self, kind: str, key: str, image: Image.Image, metadata: dict[str, Any]
    ) -> None:
        directory = self.root / kind
        directory.mkdir(parents=True, exist_ok=True)
        atomic_save_image(image, directory / f"{key}.png")
        atomic_write_json(directory / f"{key}.json", metadata)


class WorkflowState:
    """将任务和各节点状态原子写入 work/state.json，便于断点定位。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        if path.is_file():
            self.data = read_json(path)
        else:
            self.data = {"status": "reviewed", "nodes": {}, "last_error": None}

    def transition(self, status: str) -> None:
        self.data["status"] = status
        self._save()

    def node(self, name: str, status: str, **fields: Any) -> None:
        record = dict(self.data["nodes"].get(name, {}))
        record.update({"status": status, **fields})
        self.data["nodes"][name] = record
        self._save()

    def fail(self, code: str, message: str, *, blocked: bool) -> None:
        self.data["status"] = "blocked" if blocked else "failed"
        self.data["last_error"] = {"code": code, "message": message}
        self._save()

    def _save(self) -> None:
        atomic_write_json(self.path, self.data)
