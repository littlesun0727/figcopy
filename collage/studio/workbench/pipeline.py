"""Expose saved pipeline visuals through read-only, project-scoped endpoints."""

from __future__ import annotations

import io
import re
from urllib.parse import quote

from ...core.errors import CollageError
from ...core.io import decode_image, read_json, safe_package_path
from ...projects import ProjectPaths
from ..review_session import ReviewSession


def _json(project: ProjectPaths, relative: str) -> dict:
    path = safe_package_path(project.root, relative)
    return read_json(path) if path.is_file() else {}


def review_snapshot(project: ProjectPaths, source: str) -> ReviewSession:
    """Reuse the review renderer with the saved confirmed Draft and its actual options."""
    if source not in {"analysis", "confirmed"}:
        raise CollageError("ARTIFACT_NOT_FOUND", "未知识别结果")
    confirmed = source == "confirmed"
    draft = project.analysis / (
        "ui_confirmed_draft.json" if confirmed else "draft.json"
    )
    if not draft.is_file():
        # Never label the latest analysis as the historical confirmed view.
        raise CollageError("ARTIFACT_NOT_FOUND", "此项目未保存该阶段的框选结果")
    reviewed = _json(project, "review/reviewed.json") if confirmed else {}
    mask = project.review / "remove_mask.png"
    session = ReviewSession(
        draft,
        project.review / "reviewed.json",
        reviewer="",
        initial_mask_path=mask if confirmed and mask.is_file() else None,
        initial_review_options={},
    )
    if reviewed:
        for kind in ("slots", "overlays"):
            for item in reviewed[kind]:
                options = session.review_options[kind].get(item["id"])
                if options is not None:
                    options.update({key: item[key] for key in options if key in item})
        session.review_options["background"].update(
            {
                key: reviewed["background"][key]
                for key in session.review_options["background"]
                if key in reviewed["background"]
            }
        )
    # Snapshot URLs must not become an arbitrary local-file reader.
    try:
        session.reference_path.relative_to(project.root.resolve())
    except ValueError as exc:
        raise CollageError("UNSAFE_ARTIFACT_PATH", "参考图不在本项目目录中") from exc
    return session


def _image_paths(project: ProjectPaths) -> tuple[dict, list, list, dict, dict]:
    """Build an allowlist from known IDs; callers never supply filesystem paths."""
    reviewed = _json(project, "review/reviewed.json")
    template = _json(project, "template/template.json")
    state = _json(project, "workspace/state.json")
    paths = {}
    legacy_records = {}

    def add(kind, identifier, variant, relative):
        path = safe_package_path(project.root, relative)
        if path.is_file():
            paths[(kind, identifier, variant)] = path

    add("background", "background", "candidate", "workspace/background_candidate.png")
    add("background", "background", "mask", "review/remove_mask.png")
    assets = {item["id"]: item for item in template.get("assets", [])}
    for asset in assets.values():
        if asset.get("role") == "background":
            add("background", "background", "final", "template/" + asset["path"])
    if not template:
        add("background", "background", "final", "workspace/background.png")
    overlays = []
    for overlay in reviewed.get("overlays", []):
        identifier = overlay["id"]
        asset = assets.get(identifier)
        node = state.get("nodes", {}).get("overlay:" + identifier, {})
        work = "workspace"
        if asset:
            # In-place regeneration stores evidence in its own short attempt directory.
            match = re.search(r"_(r[0-9a-f]{8})[.]png$", asset["path"])
            if match:
                work += "/" + match[1]
            add("overlay", identifier, "final", "template/" + asset["path"])
        elif not template:
            add(
                "overlay",
                identifier,
                "final",
                f"template/assets/overlay_{identifier}.png",
            )
        add("overlay", identifier, "reference", f"workspace/crops/{identifier}.png")
        add("overlay", identifier, "processed", f"{work}/overlay_{identifier}_full.png")
        transform = _json(project, f"{work}/overlay_{identifier}_transform.json")
        cache_key = transform.get("evidence_cache_key", "")
        if re.fullmatch(r"[0-9a-f]{64}", cache_key):
            directory = safe_package_path(
                project.root, f"{work}/overlay_attempts/{cache_key}"
            )
            records = sorted(
                (p for p in directory.glob("*.json") if p.stem.isdigit()),
                key=lambda p: int(p.stem),
                reverse=True,
            )
            raw = next(
                (
                    p.with_name(p.stem + "_raw.png")
                    for p in records
                    if p.with_name(p.stem + "_raw.png").is_file()
                ),
                None,
            )
            if raw:
                add(
                    "overlay",
                    identifier,
                    "raw",
                    raw.relative_to(project.root).as_posix(),
                )
        if ("overlay", identifier, "raw") not in paths:
            audit = next(
                (
                    entry
                    for entry in template.get("build", {}).get("providers", [])
                    if entry.get("node") == "overlay:" + identifier
                ),
                node.get("audit", {}),
            )
            request_id = audit.get("request_id")
            if request_id:
                # Older projects lack the cache-key link. Only an unambiguous saved
                # provider request may establish which raw image belongs to this piece.
                if work not in legacy_records:
                    directory = safe_package_path(
                        project.root, work + "/overlay_attempts"
                    )
                    records = []
                    for record_path in directory.glob("*/*.json"):
                        if not record_path.stem.isdigit():
                            continue
                        relative = record_path.relative_to(project.root).as_posix()
                        record = _json(project, relative)
                        raw = record_path.with_name(record_path.stem + "_raw.png")
                        if raw.is_file():
                            records.append((record.get("audit", {}), raw))
                    legacy_records[work] = records
                candidates = [
                    raw
                    for saved_audit, raw in legacy_records[work]
                    if saved_audit.get("request_id") == request_id
                    and saved_audit.get("name") == audit.get("name")
                ]
                if len(candidates) == 1:
                    add(
                        "overlay",
                        identifier,
                        "raw",
                        candidates[0].relative_to(project.root).as_posix(),
                    )
        has_final = ("overlay", identifier, "final") in paths
        # During a retry old files may still exist. Only the current run's node
        # completion makes those results available as newly finished materials.
        building = state.get("status") == "building"
        status = (
            node.get("status", "pending")
            if building
            else ("complete" if has_final else node.get("status", "pending"))
        )
        if building and status != "complete":
            for variant in ("raw", "processed", "final"):
                paths.pop(("overlay", identifier, variant), None)
        if template and not has_final and not building:
            status = "missing"
        if status == "running" and state.get("status") in {"failed", "blocked"}:
            status = "interrupted"
        overlays.append(
            {
                "id": identifier,
                "label": overlay["label"],
                "status": status,
                "action": overlay["action"],
                "started_at": node.get("started_at"),
                "completed_at": node.get("completed_at"),
                "cache_hit": bool((node.get("audit") or {}).get("cache_hit")),
                "text": overlay.get("text_content"),
                "rect": overlay["target_rect"],
                "warnings": (
                    [node["error"]["message"]]
                    if node.get("error") and not template
                    else []
                )
                + [
                    item["message"]
                    for item in template.get("build", {}).get("warnings", [])
                    if item.get("overlay_id") == identifier
                ],
            }
        )
    bindings = _json(project, "renders/bindings.json").get("slots", {})
    slots = []
    for slot in template.get("slots", []):
        current = bindings.get(slot["id"], {})
        if slot["type"] == "image":
            relative = current.get("path", "")
            if relative and not relative.startswith("REPLACE_"):
                # Resolve paths relative to bindings.json, but require project containment.
                path = (project.renders / relative).resolve()
                try:
                    relative = path.relative_to(project.root.resolve()).as_posix()
                except ValueError:
                    relative = ""
                if relative:
                    add("slot", slot["id"], "input", relative)
        slots.append(
            {
                "id": slot["id"],
                "label": slot["label"],
                "type": slot["type"],
                "text": current.get("text", slot.get("default_text", "")),
                "has_image": ("slot", slot["id"], "input") in paths,
                "required": slot.get("required", False),
            }
        )
    return paths, overlays, slots, reviewed, state


def build_progress(overlays: list[dict], state: dict, background_status: str) -> dict:
    """Report completed work units, without inventing provider percentages or ETAs."""
    complete = sum(item["status"] == "complete" for item in overlays)
    skipped = sum(
        item["status"] in {"skipped", "missing", "failed", "blocked", "interrupted"}
        for item in overlays
    )
    current = next(
        (
            {
                "id": item["id"],
                "label": item["label"],
                "index": index,
                "started_at": item.get("started_at"),
            }
            for index, item in enumerate(overlays, start=1)
            if item["status"] == "running"
        ),
        None,
    )
    if state.get("status") in {"failed", "blocked"}:
        phase = "failed"
    elif background_status not in {"complete", "skipped"}:
        phase = "background" if background_status == "running" else "preparing"
    elif complete + skipped < len(overlays):
        phase = "materials"
    elif state.get("status") == "building":
        phase = "assembling"
    else:
        phase = "complete"
    return {
        "phase": phase,
        "total": len(overlays),
        "completed": complete,
        "skipped": skipped,
        "processed": complete + skipped,
        "remaining": len(overlays) - complete - skipped,
        "generated_total": sum(
            item["action"] == "reference_generate" for item in overlays
        ),
        "current": current,
        "started_at": state.get("build_started_at"),
    }


def pipeline_document(project: ProjectPaths) -> dict:
    paths, overlays, slots, reviewed, build_state = _image_paths(project)
    base = f"/api/projects/{quote(project.project_id)}/pipeline"

    def images(kind, identifier):
        return {
            variant: f"{base}/images/{kind}/{quote(identifier)}/{variant}?v={path.stat().st_mtime_ns}"
            for (item_kind, item_id, variant), path in paths.items()
            if (item_kind, item_id) == (kind, identifier)
        }

    for item in overlays:
        item["images"] = images("overlay", item["id"])
    for item in slots:
        item["images"] = images("slot", item["id"])
    background_node = build_state.get("nodes", {}).get("background", {})
    background_images = images("background", "background")
    background_status = background_node.get("status", "pending")
    if reviewed.get("background", {}).get("mode") == "slot":
        background_status = "skipped"
    elif build_state.get("status") != "building" and background_images.get("final"):
        background_status = "complete"
    if build_state.get("status") == "building" and background_status != "complete":
        background_images = {
            key: value for key, value in background_images.items() if key == "mask"
        }
    progress = build_progress(overlays, build_state, background_status)
    return {
        "progress": progress,
        "overlays": overlays,
        "slots": slots,
        "background": {
            "mode": reviewed.get("background", {}).get("mode", "fixed"),
            "slot_id": reviewed.get("background", {}).get("slot_id"),
            "images": background_images,
            "status": background_status,
            "started_at": background_node.get("started_at"),
            "completed_at": background_node.get("completed_at"),
            "cache_hit": bool((background_node.get("audit") or {}).get("cache_hit")),
        },
        "analysis_available": (project.analysis / "draft.json").is_file(),
        "confirmed_available": (project.analysis / "ui_confirmed_draft.json").is_file()
        and bool(reviewed),
        "confirmation": reviewed.get("review", {}),
        "template_available": (project.template / "template.json").is_file(),
    }


def pipeline_image(
    project: ProjectPaths, kind: str, identifier: str, variant: str
) -> bytes:
    paths, _, _, _, _ = _image_paths(project)
    path = paths.get((kind, identifier, variant))
    if path is None:
        raise CollageError("ARTIFACT_NOT_FOUND", "这一步尚未保存对应图片")
    # Normalize uploaded customer images to a real PNG response.
    buffer = io.BytesIO()
    decode_image(path).save(buffer, format="PNG")
    return buffer.getvalue()
