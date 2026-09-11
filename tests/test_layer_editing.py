"""Verify independent layer edits, local previews and immutable accepted versions."""

import copy
import io

import pytest
from PIL import Image

from collage.core.errors import CollageError
from collage.core.io import atomic_write_json, read_json, sha256_file
from collage.projects import DataPaths, ProjectStore
from collage.studio.workbench.layout import (
    apply_layout,
    fork_layout,
    layout_document,
    preview_layout,
)
from collage.workflows.model import new_workflow


def _project(tmp_path, asset_package_factory):
    store = ProjectStore(DataPaths.resolve(tmp_path / "data"))
    project = store.create("original")
    asset_package_factory(project.root)
    workflow = new_workflow(
        reviewer="tester",
        vision_provider=None,
        image_provider=None,
        cutout_provider=None,
        fixture_provider=False,
        allow_cloud_upload=False,
        review_port=8765,
    )
    workflow["stage"] = "complete"
    template = read_json(project.template / "template.json")
    Image.new("RGB", (20, 20), "white").save(project.template / "preview.png")
    template["status"] = "ready"
    template["review"].update(
        visual_approved=True,
        reviewer="tester",
        reviewed_at="2026-09-11",
        evidence_sha256=["abc"],
    )
    atomic_write_json(project.template / "template.json", template)
    store.update_workflow("original", workflow, status="ready")
    return store, project


def test_moving_one_asset_changes_pixels_without_changing_any_asset_or_prior_approval(
    tmp_path, asset_package_factory
):
    store, project = _project(tmp_path, asset_package_factory)
    payload = layout_document(project)
    before_manifest = sha256_file(project.template / "template.json")
    original_assets = {
        p.name: sha256_file(p) for p in (project.template / "assets").glob("*.png")
    }
    before = Image.open(io.BytesIO(preview_layout(project, payload))).tobytes()
    red = next(item for item in payload["items"] if item["id"] == "asset:red")
    red.update(rect=[0, 0, 6, 6], rotation_deg=20)
    payload["items"][1], payload["items"][2] = payload["items"][2], payload["items"][1]
    after = Image.open(io.BytesIO(preview_layout(project, payload))).tobytes()
    assert before != after
    result = fork_layout(store, "original", payload)
    target = store.open(result["project_id"])
    assert result["network_calls"] == 0 and result["fixed_assets_regenerated"] is False
    assert sha256_file(project.template / "template.json") == before_manifest
    assert (
        read_json(project.template / "template.json")["review"]["visual_approved"]
        is True
    )
    assert (
        read_json(target.template / "template.json")["review"]["visual_approved"]
        is False
    )
    assert {
        p.name: sha256_file(p) for p in (target.template / "assets").glob("*.png")
    } == original_assets
    assert (target.renders / "result.png").exists()
    assert (
        store.get_manifest(result["project_id"])["workflow"]["stage"]
        == "awaiting_approval"
    )


@pytest.mark.parametrize(
    "problem", ["stale", "duplicate", "background", "nan", "huge", "locked"]
)
def test_layout_rejects_stale_or_invalid_mutations(
    tmp_path, asset_package_factory, problem
):
    _store, project = _project(tmp_path, asset_package_factory)
    payload = copy.deepcopy(layout_document(project))
    if problem == "stale":
        payload["revision"] = "stale"
    elif problem == "duplicate":
        payload["items"][1]["id"] = payload["items"][2]["id"]
    elif problem == "background":
        payload["items"].reverse()
    elif problem == "nan":
        payload["items"][1]["rotation_deg"] = float("nan")
    elif problem == "huge":
        payload["items"][1]["rect"][2] = 100000
    else:
        payload["items"][0]["rect"][0] = 2
    with pytest.raises(CollageError):
        apply_layout(project, payload)
