"""Create and discover file-backed Figcopy projects without a database."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..errors import CollageError
from ..io_utils import atomic_write_json, read_json
from .paths import DataPaths, ProjectPaths

PROJECT_VERSION = "figcopy-project/1"
PROJECT_STATUSES = frozenset(
    {
        "new",
        "draft",
        "reviewed",
        "building",
        "needs_review",
        "ready",
        "blocked",
        "failed",
    }
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class ProjectStore:
    """Manage project manifests beneath one configured data root."""

    def __init__(self, paths: DataPaths):
        self.paths = paths

    def create(
        self,
        project_id: str,
        *,
        name: str | None = None,
        exist_ok: bool = False,
    ) -> ProjectPaths:
        project = self.paths.project(project_id)
        if project.manifest.exists():
            if not exist_ok:
                raise CollageError(
                    "PROJECT_ALREADY_EXISTS",
                    "项目已经存在",
                    details={"project_id": project_id},
                )
            self._read_manifest(project)
            return project.ensure()
        if project.root.exists() and any(project.root.iterdir()):
            raise CollageError(
                "PROJECT_DIRECTORY_NOT_EMPTY",
                "项目目录已存在但缺少 project.json",
                details={"project_id": project_id},
            )

        self.paths.ensure()
        project.ensure()
        timestamp = _utc_now()
        atomic_write_json(
            project.manifest,
            {
                "version": PROJECT_VERSION,
                "id": project_id,
                "name": name or project_id,
                "status": "new",
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        )
        return project

    def open(self, project_id: str) -> ProjectPaths:
        project = self.paths.project(project_id)
        self._read_manifest(project)
        return project

    def list(self) -> list[dict[str, Any]]:
        if not self.paths.projects.exists():
            return []
        manifests: list[dict[str, Any]] = []
        for directory in sorted(self.paths.projects.iterdir(), key=lambda item: item.name):
            if not directory.is_dir():
                continue
            project = self.paths.project(directory.name)
            if project.manifest.is_file():
                manifests.append(self._read_manifest(project))
        return manifests

    def set_status(self, project_id: str, status: str) -> dict[str, Any]:
        if status not in PROJECT_STATUSES:
            raise CollageError(
                "INVALID_PROJECT_STATUS",
                "项目状态不受支持",
                details={"project_id": project_id, "status": status},
            )
        project = self.open(project_id)
        manifest = self._read_manifest(project)
        manifest["status"] = status
        manifest["updated_at"] = _utc_now()
        atomic_write_json(project.manifest, manifest)
        return manifest

    @staticmethod
    def _read_manifest(project: ProjectPaths) -> dict[str, Any]:
        if not project.manifest.is_file():
            raise CollageError(
                "PROJECT_NOT_FOUND",
                "项目不存在或缺少 project.json",
                details={"project_id": project.project_id},
            )
        value = read_json(project.manifest)
        if (
            not isinstance(value, dict)
            or value.get("version") != PROJECT_VERSION
            or value.get("id") != project.project_id
        ):
            raise CollageError(
                "INVALID_PROJECT_MANIFEST",
                "project.json 格式或项目 ID 不正确",
                details={"project_id": project.project_id},
            )
        return value
