"""Define runtime paths that keep generated files outside the source tree."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..core.errors import CollageError

DATA_DIR_ENV = "FIGCOPY_DATA_DIR"
_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_WINDOWS_RESERVED_NAMES = {
    "aux",
    "com1",
    "com2",
    "com3",
    "com4",
    "com5",
    "com6",
    "com7",
    "com8",
    "com9",
    "con",
    "lpt1",
    "lpt2",
    "lpt3",
    "lpt4",
    "lpt5",
    "lpt6",
    "lpt7",
    "lpt8",
    "lpt9",
    "nul",
    "prn",
}


def resolve_data_root(value: Path | str | None = None) -> Path:
    """Resolve an explicit, environment, or OS-local Figcopy data root."""

    configured = str(value) if value is not None else os.environ.get(DATA_DIR_ENV)
    if configured:
        return Path(configured).expanduser().resolve()

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return (Path(local_app_data) / "Figcopy").resolve()

    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return (Path(xdg_data_home) / "figcopy").expanduser().resolve()
    return (Path.home() / ".local" / "share" / "figcopy").resolve()


def _validate_project_id(project_id: str) -> str:
    if not _PROJECT_ID.fullmatch(project_id):
        raise CollageError(
            "INVALID_PROJECT_ID",
            "项目 ID 只能包含英文字母、数字、点、下划线和连字符，长度为 1-64",
            details={"project_id": project_id},
        )
    if project_id.split(".", 1)[0].lower() in _WINDOWS_RESERVED_NAMES:
        raise CollageError(
            "INVALID_PROJECT_ID",
            "项目 ID 不能使用 Windows 保留名称",
            details={"project_id": project_id},
        )
    return project_id


@dataclass(frozen=True)
class ProjectPaths:
    """All persistent paths belonging to one Figcopy project."""

    project_id: str
    root: Path

    @property
    def manifest(self) -> Path:
        return self.root / "project.json"

    @property
    def inputs(self) -> Path:
        return self.root / "inputs"

    @property
    def analysis(self) -> Path:
        return self.root / "analysis"

    @property
    def review(self) -> Path:
        return self.root / "review"

    @property
    def template(self) -> Path:
        return self.root / "template"

    @property
    def renders(self) -> Path:
        return self.root / "renders"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def workspace(self) -> Path:
        return self.root / "workspace"

    def ensure(self) -> ProjectPaths:
        self.root.mkdir(parents=True, exist_ok=True)
        for directory in (
            self.inputs,
            self.analysis,
            self.review,
            self.template,
            self.renders,
            self.reports,
            self.workspace,
        ):
            directory.mkdir(exist_ok=True)
        return self


@dataclass(frozen=True)
class DataPaths:
    """Top-level locations for user projects and regenerable runtime data."""

    root: Path

    @classmethod
    def resolve(cls, value: Path | str | None = None) -> DataPaths:
        return cls(resolve_data_root(value))

    @property
    def projects(self) -> Path:
        return self.root / "projects"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def archive(self) -> Path:
        return self.root / "archive"

    def ensure(self) -> DataPaths:
        for directory in (
            self.root,
            self.projects,
            self.cache,
            self.logs,
            self.archive,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def project(self, project_id: str) -> ProjectPaths:
        safe_id = _validate_project_id(project_id)
        projects_root = self.projects.resolve()
        project_root = (projects_root / safe_id).resolve()
        try:
            project_root.relative_to(projects_root)
        except ValueError as exc:
            raise CollageError(
                "INVALID_PROJECT_ID",
                "项目目录必须位于 Figcopy 数据目录内",
                details={"project_id": project_id},
            ) from exc
        return ProjectPaths(safe_id, project_root)
