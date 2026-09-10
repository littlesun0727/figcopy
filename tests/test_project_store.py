"""Verify external data-root resolution and safe file-backed projects."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from collage.cli import app as cli
from collage.core.errors import CollageError
from collage.projects import DATA_DIR_ENV, DataPaths, ProjectStore, resolve_data_root


def test_explicit_data_root_wins_over_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment_root = tmp_path / "environment"
    explicit_root = tmp_path / "explicit"
    monkeypatch.setenv(DATA_DIR_ENV, str(environment_root))

    assert resolve_data_root(explicit_root) == explicit_root.resolve()


def test_environment_data_root_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = tmp_path / "configured"
    monkeypatch.setenv(DATA_DIR_ENV, str(configured))

    assert resolve_data_root() == configured.resolve()


def test_project_store_creates_standard_layout(tmp_path: Path) -> None:
    data_paths = DataPaths.resolve(tmp_path / "figcopy-data")
    store = ProjectStore(data_paths)

    project = store.create("sample-project", name="示例项目")

    assert project.manifest.is_file()
    assert project.inputs.is_dir()
    assert project.analysis.is_dir()
    assert project.review.is_dir()
    assert project.template.is_dir()
    assert project.renders.is_dir()
    assert project.reports.is_dir()
    assert project.workspace.is_dir()
    manifest = json.loads(project.manifest.read_text(encoding="utf-8"))
    assert manifest["id"] == "sample-project"
    assert manifest["name"] == "示例项目"
    assert str(tmp_path) not in json.dumps(manifest, ensure_ascii=False)
    assert store.open("sample-project") == project
    assert store.list() == [manifest]

    updated = store.set_status("sample-project", "needs_review")
    assert updated["status"] == "needs_review"
    assert updated["updated_at"] != manifest["updated_at"]
    assert store.list() == [updated]


@pytest.mark.parametrize(
    "project_id", ["../escape", "nested/path", r"nested\path", "项目", "CON"]
)
def test_project_store_rejects_unsafe_project_ids(
    tmp_path: Path, project_id: str
) -> None:
    store = ProjectStore(DataPaths.resolve(tmp_path / "figcopy-data"))

    with pytest.raises(CollageError) as caught:
        store.create(project_id)

    assert caught.value.code == "INVALID_PROJECT_ID"


def test_project_store_does_not_adopt_nonempty_unknown_directory(
    tmp_path: Path,
) -> None:
    paths = DataPaths.resolve(tmp_path / "figcopy-data")
    unknown = paths.projects / "unknown"
    unknown.mkdir(parents=True)
    (unknown / "user-file.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(CollageError) as caught:
        ProjectStore(paths).create("unknown")

    assert caught.value.code == "PROJECT_DIRECTORY_NOT_EMPTY"
    assert (unknown / "user-file.txt").read_text(encoding="utf-8") == "keep"


def test_demo_cli_uses_project_under_configured_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "figcopy-data"
    observed: dict[str, Path] = {}

    def fake_demo(output_dir: Path) -> Path:
        observed["output_dir"] = output_dir
        return output_dir / "renders" / "result.png"

    monkeypatch.setattr(cli, "create_demo", fake_demo)
    args = cli._parser().parse_args(
        [
            "demo",
            "--data-dir",
            str(data_root),
            "--project",
            "cli-demo",
        ]
    )

    result = cli._run(args)

    expected_root = data_root.resolve() / "projects" / "cli-demo"
    assert observed["output_dir"] == expected_root
    assert result == expected_root / "renders" / "result.png"
    manifest = ProjectStore(DataPaths.resolve(data_root)).list()[0]
    assert manifest["status"] == "needs_review"
