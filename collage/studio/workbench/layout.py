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
from ...template.layout import compile_layers, transform_attachments
from ...template.validation import validate_package
from ...workflows.model import validate_workflow
from ...workflows.state import transition

LOGGER = logging.getLogger(__name__)


def layer_items(template: dict) -> list[dict]:
    """Expose geometry and IDs without exposing filesystem paths or asset mutation."""
    assets = {item["id"]: item for item in template["assets"]}
    slots = {item["id"]: item for item in template["slots"]}
    overlays = {item["id"]: item for item in template["overlays"]}
    items = []
    for layer in compile_layers(template, include_missing=True):
        is_asset = layer["type"] == "asset"
        identifier = layer["asset_id"] if is_asset else layer["slot_id"]
        source = layer if is_asset else slots[identifier]
        background = (
            is_asset and assets.get(identifier, {}).get("role") == "background"
        ) or (not is_asset and identifier == background_slot_id(template))
        items.append(
            {
                "id": ("asset:" if is_asset else "slot:") + identifier,
                "label": identifier if is_asset else source["label"],
                "rect": list(source["rect"]),
                "rotation_deg": source["rotation_deg"],
                "fit": layer.get("fit", "contain"),
                "anchor": layer.get("anchor", [0.5, 0.5]),
                "editable": not background,
                "attachment": overlays.get(identifier, {}).get("attachment")
                if is_asset
                else None,
                "missing": is_asset and identifier not in assets,
                "grouped": any(
                    child["attachment"] and child["attachment"]["slot_id"] == identifier
                    for child in template["overlays"]
                )
                if not is_asset
                else False,
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
    result = copy.deepcopy(template)
    slots = {item["id"]: item for item in result["slots"]}
    overlays = {item["id"]: item for item in result["overlays"]}
    roots = []
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
            raise CollageError("LAYOUT_LAYER_LOCKED", "背景位置已锁定")
        identifier = item["id"].split(":", 1)[1]
        if original["background"]:
            roots.append(
                {"type": "slot", "id": identifier}
                if item["id"].startswith("slot:")
                else {"type": "background"}
            )
        elif item["id"].startswith("slot:"):
            slots[identifier].update(rect=list(rect), rotation_deg=rotation)
            roots.append({"type": "slot", "id": identifier})
        else:
            overlays[identifier].update(rect=list(rect), rotation_deg=rotation)
            if overlays[identifier]["attachment"] is None:
                roots.append({"type": "overlay", "id": identifier})
    if "root_order" in payload:
        root_map = {
            (
                "slot:" + root["id"]
                if root["type"] == "slot"
                else "asset:" + root["id"]
                if root["type"] == "overlay"
                else next(key for key, value in known.items() if value["background"])
            ): root
            for root in roots
        }
        order = payload["root_order"]
        if not isinstance(order, list) or any(
            not isinstance(key, str) or key not in root_map for key in order
        ):
            raise CollageError("INVALID_LAYOUT", "未知排版单元")
        roots = [root_map[key] for key in order]
    result["layer_order"] = roots
    change = payload.get("change")
    if change is not None:
        if not isinstance(change, dict) or change.get("id") not in known:
            raise CollageError("INVALID_LAYOUT", "没有这个可编辑元素")
        original = known[change["id"]]
        if not original["editable"]:
            raise CollageError("LAYOUT_LAYER_LOCKED", "背景位置已锁定")
        identifier = change["id"].split(":", 1)[1]
        source = (
            slots[identifier]
            if change["id"].startswith("slot:")
            else overlays[identifier]
        )
        rect, rotation = change.get("rect"), change.get("rotation_deg")
        if (
            not isinstance(rect, list)
            or len(rect) != 4
            or any(
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not math.isfinite(v)
                for v in [*rect, rotation]
            )
            or min(rect[2:]) < 1
            or max(abs(v) for v in rect) > limit
            or abs(rotation) > 3600
        ):
            raise CollageError("INVALID_LAYOUT", "位置、尺寸或旋转角度不合法")
        if change["id"].startswith("slot:"):
            transform_attachments(
                result["overlays"],
                identifier,
                source["rect"],
                rect,
                source["rotation_deg"],
                rotation,
            )
        source.update(rect=list(rect), rotation_deg=rotation)
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
        # Partial uploads and generated REPLACE paths should still show a layout.
        # Prepare each local binding independently, then fill unavailable slots below.
        for slot in template["slots"]:
            partial = {**template, "slots": [{**slot, "required": False}]}
            binding = bindings.get("slots", {}).get(slot["id"])
            if binding is not None:
                try:
                    prepared.update(
                        prepare_bindings(
                            partial, {**bindings, "slots": {slot["id"]: binding}}, path
                        )
                    )
                except CollageError:
                    pass
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
            (value for value in template["assets"] if value["id"] == identifier[6:]),
            None,
        )
        if asset is None:
            return png_bytes(Image.new("RGBA", (1, 1)))
        return png_bytes(
            decode_image(
                safe_package_path(project.template, asset["path"]), mode="RGBA"
            )
        )
    slot = next(value for value in template["slots"] if value["id"] == identifier[5:])
    isolated = copy.deepcopy(template)
    width, height = [max(1, round(value)) for value in slot["rect"][2:]]
    isolated["canvas"].update(width=width, height=height)
    isolated["layer_order"] = [{"type": "slot", "id": slot["id"]}]
    isolated["overlays"] = []
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


def _replace_overlay(
    target: ProjectPaths,
    template: dict,
    identifier: str,
    provider,
) -> None:
    """Regenerate one decoration in a revision while keeping every other asset."""
    from ...core.state import NodeCache
    from ...imaging.operations import crop_source
    from ...template.build.overlays import _build_overlay
    from ...template.build.asset_validation import overlay_warning

    spec_path = target.review / "reviewed.json"
    spec = read_json(spec_path)
    overlay = next(item for item in spec["overlays"] if item["id"] == identifier)
    overlay = {**overlay, "prepared_asset": None}
    reference = decode_image(target.analysis / "reference.png")
    x, y, width, height = overlay["source_rect"]
    padding = max(2, round(min(width, height) * 0.15))
    left, top = max(0, x - padding), max(0, y - padding)
    crop = crop_source(
        reference,
        [
            left,
            top,
            min(reference.width, x + width + padding) - left,
            min(reference.height, y + height + padding) - top,
        ],
    )
    previous = next(
        (asset for asset in template["assets"] if asset["id"] == identifier), None
    )
    warnings = [
        item
        for item in template["build"].get("warnings", [])
        if item["overlay_id"] != identifier
    ]
    try:
        asset, audit, _box, findings = _build_overlay(
            overlay,
            spec,
            spec_path,
            crop,
            target.template,
            target.workspace,
            NodeCache(target.workspace / "cache"),
            provider,
        )
    except CollageError as exc:
        finding = overlay_warning(overlay, exc.code, skipped=previous is None)
        if previous is not None:
            finding["message"] = "本次重做未完成，沿用上一版素材"
        warnings.append(finding)
        LOGGER.warning("单件重做未完成 | id=%s code=%s", identifier, exc.code)
    else:
        template["assets"] = [
            a for a in template["assets"] if a["id"] != identifier
        ] + [asset]
        template["build"]["providers"] = [
            item
            for item in template["build"]["providers"]
            if item["node"] != f"overlay:{identifier}"
        ] + [audit.as_dict(node=f"overlay:{identifier}")]
        template["build"]["fixture_used"] = (
            template["build"]["fixture_used"] or audit.fixture
        )
        warnings.extend(findings)
    template["build"]["warnings"] = warnings


def _sync_reviewed_layout(project: ProjectPaths, template: dict) -> None:
    """Keep revision geometry current while preserving original source/audit evidence."""
    path = project.review / "reviewed.json"
    if not path.is_file():
        # An imported portable template can be laid out without its production project.
        return
    spec = read_json(path)
    for kind in ("slots", "overlays"):
        current = {item["id"]: item for item in template[kind]}
        for item in spec[kind]:
            layout = current[item["id"]]
            item.update(
                target_rect=list(layout["rect"]), rotation_deg=layout["rotation_deg"]
            )
            if kind == "overlays":
                item["attachment"] = copy.deepcopy(layout["attachment"])
    spec["layer_order"] = copy.deepcopy(template["layer_order"])
    atomic_write_json(path, spec)


def fork_layout(
    store: ProjectStore,
    project_id: str,
    payload: Any,
    *,
    overlay_id: str | None = None,
    image_provider=None,
    image_provider_spec: str | None = None,
) -> dict:
    """Create a new package and local render; all prior assets and approvals stay immutable."""
    source = store.open(project_id)
    with project_edit_lock(source.root):
        LOGGER.info("开始保存布局新版本 | project=%s", project_id)
        template = apply_layout(source, payload)
        manifest = store.get_manifest(project_id)
        workflow = copy.deepcopy(validate_workflow(manifest.get("workflow")))
        if image_provider_spec is not None:
            workflow["options"].update(
                image_provider=image_provider_spec, fixture_provider=False
            )
        kind = "overlay" if overlay_id else "layout"
        new_id = project_id[:38] + f"-{kind}-" + uuid.uuid4().hex[:10]
        target = store.create(
            new_id,
            name=manifest.get("name", project_id)
            + ("（装饰重做）" if overlay_id else "（布局修订）"),
        )
        for name in ("inputs", "analysis", "review", "template"):
            shutil.copytree(source.root / name, target.root / name, dirs_exist_ok=True)
        atomic_write_json(target.template / "template.json", template)
        _sync_reviewed_layout(target, template)
        (target.template / "preview.png").unlink(missing_ok=True)
        if overlay_id:
            _replace_overlay(target, template, overlay_id, image_provider)
        # Import bindings rather than retaining source-project absolute references.
        bindings = source.renders / "bindings.json"
        if bindings.exists():
            from ...workflows.inputs import import_bindings

            from ...workflows.bindings import bindings_wait_reason

            if bindings_wait_reason(source) is None:
                import_bindings(bindings, target.renders / "bindings.json", target)
            else:
                # Managed uploads use ../inputs paths and are copied with inputs.
                atomic_write_json(target.renders / "bindings.json", read_json(bindings))
        atomic_write_json(target.template / "template.json", template)
        evidence = {
            "source_project": project_id,
            "source_revision": payload["revision"],
            "revision": sha256_file(target.template / "template.json"),
            "fixed_assets_regenerated": overlay_id is not None,
            "regenerated_overlay": overlay_id,
            "items": layer_items(template),
        }
        if overlay_id is None:
            evidence["network_calls"] = 0
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
