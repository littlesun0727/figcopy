"""Edit fixed layer placement and fork a reviewable local project revision."""

from __future__ import annotations

import copy
import io
import math
import logging
import shutil
import uuid
from typing import Any

from PIL import Image, ImageDraw

from ...core.errors import CollageError
from ...core.io import (
    atomic_write_json,
    decode_image,
    read_json,
    safe_package_path,
    sha256_file,
)
from ...core.locking import project_edit_lock
from ...projects import ProjectPaths, ProjectStore
from ...rendering.bindings import prepare_bindings
from ...rendering.model import PreparedBinding
from ...rendering.service import _render_layers, render_from_files
from ...schemas import validate_template_spec
from ...schemas.background import background_slot_id
from ...template.guide import create_upload_guide
from ...template.validation import validate_package
from ...workflows.model import validate_workflow
from ...workflows.state import transition

LOGGER = logging.getLogger(__name__)


def layer_items(template: dict) -> list[dict]:
    """Expose geometry and IDs without exposing filesystem paths or asset mutation."""
    assets = {item["id"]: item for item in template["assets"]}
    slots = {item["id"]: item for item in template["slots"]}
    items = []
    for layer in template["layers"]:
        is_asset = layer["type"] == "asset"
        identifier = layer["asset_id"] if is_asset else layer["slot_id"]
        source = layer if is_asset else slots[identifier]
        background = (is_asset and assets[identifier]["role"] == "background") or (
            not is_asset and identifier == background_slot_id(template)
        )
        items.append(
            {
                "id": ("asset:" if is_asset else "slot:") + identifier,
                "label": identifier if is_asset else source["label"],
                "rect": list(source["rect"]),
                "rotation_deg": source["rotation_deg"],
                "fit": layer.get("fit", "contain"),
                "anchor": layer.get("anchor", [0.5, 0.5]),
                "editable": is_asset and not background,
                "background": background,
            }
        )
    return items


def layout_document(project: ProjectPaths) -> dict:
    template = validate_package(project.template, require_ready=False)
    items = layer_items(template)
    reviewed_path = project.review / "reviewed.json"
    if reviewed_path.is_file():
        labels = {
            item["id"]: item["label"]
            for item in read_json(reviewed_path).get("overlays", [])
        }
        for item in items:
            if item["id"].startswith("asset:"):
                item["label"] = labels.get(item["id"][6:], item["label"])
    return {
        "revision": sha256_file(project.template / "template.json"),
        "canvas": template["canvas"],
        "items": items,
    }


def apply_layout(project: ProjectPaths, payload: Any) -> dict:
    """Accept a complete permutation and bounded transforms; reject stale or injected IDs."""
    template = validate_package(project.template, require_ready=False)
    if not isinstance(payload, dict) or payload.get("revision") != sha256_file(
        project.template / "template.json"
    ):
        raise CollageError(
            "LAYOUT_REVISION_CONFLICT", "模板已更新，请重新打开图层编辑页"
        )
    items = payload.get("items")
    known = {item["id"]: item for item in layer_items(template)}
    if not isinstance(items, list) or len(items) != len(known):
        raise CollageError("INVALID_LAYOUT", "图层列表不完整")
    if any(
        not isinstance(item, dict) or not isinstance(item.get("id"), str)
        for item in items
    ):
        raise CollageError("INVALID_LAYOUT", "图层格式不正确")
    ids = [item["id"] for item in items]
    if (
        len(set(ids)) != len(ids)
        or set(ids) != set(known)
        or not known[ids[0]]["background"]
    ):
        raise CollageError(
            "INVALID_LAYOUT", "不能删除、重复或伪造图层；背景必须在最底层"
        )
    layers = {
        item["id"]: layer
        for item, layer in zip(layer_items(template), template["layers"], strict=True)
    }
    result = copy.deepcopy(template)
    result["layers"] = []
    limit = max(template["canvas"]["width"], template["canvas"]["height"]) * 4
    for item in items:
        original = known[item["id"]]
        rect, rotation = item.get("rect"), item.get("rotation_deg")
        values = [*(rect if isinstance(rect, list) else []), rotation]
        if (
            not isinstance(rect, list)
            or len(rect) != 4
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                for value in values
            )
            or min(rect[2:]) < 1
            or max(abs(value) for value in rect) > limit
            or abs(rotation) > 3600
        ):
            raise CollageError("INVALID_LAYOUT", "位置、尺寸或旋转角度不合法")
        if not original["editable"] and (
            rect != original["rect"] or rotation != original["rotation_deg"]
        ):
            raise CollageError(
                "LAYOUT_LAYER_LOCKED", "背景和客户照片框的位置请在结构复核阶段调整"
            )
        layer = copy.deepcopy(layers[item["id"]])
        if original["editable"]:
            layer.update(rect=rect, rotation_deg=rotation)
        result["layers"].append(layer)
    result["status"] = (
        "needs_validation"
        if result.get("provenance", {}).get("kind")
        in {"automatic", "fixture", "diagnostic"}
        else "needs_review"
    )
    result["review"] = {
        "visual_approved": False,
        "reviewer": None,
        "reviewed_at": None,
        "notes": "",
        "evidence_sha256": [],
    }
    return validate_template_spec(result, require_ready=False)


def _prepared(project: ProjectPaths, template: dict) -> dict[str, PreparedBinding]:
    path = project.renders / "bindings.json"
    prepared = {}
    if path.exists():
        bindings = read_json(path)
        # Imported project media stays local. A missing cutout is not sent to a provider.
        prepared = prepare_bindings(template, bindings, path)
    for index, slot in enumerate(template["slots"], 1):
        if slot["id"] not in prepared:
            if slot["type"] == "text":
                prepared[slot["id"]] = PreparedBinding(
                    text=slot.get("default_text") or ""
                )
            else:
                placeholder = Image.new("RGBA", (320, 320), "#627E96")
                draw = ImageDraw.Draw(placeholder)
                draw.rectangle((12, 12, 307, 307), outline="white", width=3)
                draw.text((24, 24), f"PHOTO {index}", fill="white", font_size=28)
                prepared[slot["id"]] = PreparedBinding(image=placeholder)
    return prepared


def png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def layer_image(project: ProjectPaths, identifier: str) -> bytes:
    """Return a known local asset or locally rendered slot, never an arbitrary path."""
    template = validate_package(project.template, require_ready=False)
    items = layer_items(template)
    item = next((value for value in items if value["id"] == identifier), None)
    if item is None:
        raise CollageError("LAYOUT_LAYER_NOT_FOUND", "模板没有这个图层")
    if identifier.startswith("asset:"):
        asset = next(
            value for value in template["assets"] if value["id"] == identifier[6:]
        )
        return png_bytes(
            decode_image(
                safe_package_path(project.template, asset["path"]), mode="RGBA"
            )
        )
    slot = next(value for value in template["slots"] if value["id"] == identifier[5:])
    isolated = copy.deepcopy(template)
    width, height = [max(1, round(value)) for value in slot["rect"][2:]]
    isolated["canvas"].update(width=width, height=height)
    isolated["layers"] = [{"type": "slot", "slot_id": slot["id"]}]
    for value in isolated["slots"]:
        if value["id"] == slot["id"]:
            value.update(rect=[0, 0, width, height], rotation_deg=0)
    return png_bytes(
        _render_layers(project.template, isolated, _prepared(project, template))
    )


def preview_layout(project: ProjectPaths, payload: Any) -> bytes:
    template = apply_layout(project, payload)
    return png_bytes(
        _render_layers(project.template, template, _prepared(project, template))
    )


def fork_layout(store: ProjectStore, project_id: str, payload: Any) -> dict:
    """Create a new package and local render; all prior assets and approvals stay immutable."""
    source = store.open(project_id)
    with project_edit_lock(source.root):
        LOGGER.info("开始保存布局新版本 | project=%s", project_id)
        template = apply_layout(source, payload)
        manifest = store.get_manifest(project_id)
        workflow = copy.deepcopy(validate_workflow(manifest.get("workflow")))
        new_id = project_id[:38] + "-layout-" + uuid.uuid4().hex[:10]
        target = store.create(
            new_id, name=manifest.get("name", project_id) + "（布局修订）"
        )
        for name in ("inputs", "analysis", "review", "template"):
            shutil.copytree(source.root / name, target.root / name, dirs_exist_ok=True)
        # Import bindings rather than retaining source-project absolute references.
        bindings = source.renders / "bindings.json"
        if bindings.exists():
            from ...workflows.inputs import import_bindings

            import_bindings(bindings, target.renders / "bindings.json", target)
        atomic_write_json(target.template / "template.json", template)
        # A prior accepted preview belongs to the source revision, not this new layout.
        (target.template / "preview.png").unlink(missing_ok=True)
        evidence = {
            "source_project": project_id,
            "source_revision": payload["revision"],
            "revision": sha256_file(target.template / "template.json"),
            "fixed_assets_regenerated": False,
            "network_calls": 0,
            "items": layer_items(template),
        }
        atomic_write_json(target.reports / "layout_revision.json", evidence)
        create_upload_guide(
            target.template,
            target.reports / "upload_guide.html",
            target.renders / "bindings.example.json",
            require_ready=False,
        )
        workflow.update(
            stage="awaiting_bindings", resume_stage=None, last_error=None, history=[]
        )
        transition(
            store,
            target,
            workflow,
            "awaiting_bindings",
            wait={
                "code": "BINDINGS_REQUIRED",
                "message": "布局新版本已保存，请放入素材并检查效果",
            },
        )
        if (target.renders / "bindings.json").exists() or not template["slots"]:
            if not (target.renders / "bindings.json").exists():
                atomic_write_json(
                    target.renders / "bindings.json",
                    {"version": "collage-bindings/1", "slots": {}},
                )
            try:
                render_from_files(
                    target.template,
                    target.renders / "bindings.json",
                    target.renders / "result.png",
                    require_ready=False,
                )
            except CollageError as exc:
                transition(
                    store,
                    target,
                    workflow,
                    "awaiting_bindings",
                    wait={
                        "code": exc.code,
                        "message": "新版本已保存；请补全本地素材后生成预览",
                    },
                )
            else:
                transition(
                    store,
                    target,
                    workflow,
                    "awaiting_approval",
                    wait={
                        "code": "VISUAL_APPROVAL_REQUIRED",
                        "message": "新布局已在本地合成，请重新验收",
                    },
                )
        LOGGER.info("布局新版本已保存 | project=%s source=%s", new_id, project_id)
        return {
            "ok": True,
            "project_id": new_id,
            "url": f"/projects/{new_id}",
            **evidence,
        }
