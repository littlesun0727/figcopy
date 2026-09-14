"""Fork confirmed background reviews without requiring or overwriting a template."""

from __future__ import annotations

import copy
import logging
import shutil
import uuid
from pathlib import Path

from ...core.errors import CollageError
from ...core.io import (
    atomic_write_json,
    read_json,
    resolve_input_path,
    sha256_file,
    stable_hash,
)
from ...core.locking import project_edit_lock
from ...core.state import NodeCache
from ...projects import ProjectPaths, ProjectStore
from ...schemas import validate_draft, validate_reviewed_spec
from ...schemas.background import background_slot_id
from ...template.analysis import _draw_draft_preview
from ...template.build.asset_validation import asset_fingerprint
from ...template.validation import validate_package
from ...workflows.inputs import import_bindings
from ...workflows.model import new_workflow
from ...workflows.state import transition

LOGGER = logging.getLogger(__name__)
SLOT_FIELDS = {
    "id",
    "label",
    "type",
    "mode",
    "source_rect",
    "target_rect",
    "upload_hint",
    "review_notes",
    "default_text",
}
OVERLAY_FIELDS = {
    "id",
    "label",
    "source_rect",
    "target_rect",
    "action",
    "generation_brief",
    "requires_exact_content",
    "review_notes",
    "text_content",
    "shape",
    "attachment",
}


def revision_document(project: ProjectPaths) -> dict:
    """Bind a fork to durable source artifacts without exposing their contents."""
    reviewed = project.review / "reviewed.json"
    if not reviewed.is_file():
        raise CollageError("REVIEW_NOT_READY", "尚未确认的项目请直接在审核页切换背景")
    paths = [project.manifest, reviewed, project.analysis / "draft.json"]
    for path in (
        project.analysis / "ui_confirmed_draft.json",
        project.template / "template.json",
    ):
        if path.is_file():
            paths.append(path)
    hashes = {
        path.relative_to(project.root).as_posix(): sha256_file(path) for path in paths
    }
    return {"revision": stable_hash(hashes), "source_hashes": hashes}


def _confirmed_draft(project: ProjectPaths, reviewed: dict) -> tuple[dict, dict]:
    """Restore confirmed semantics, advanced options and any later layout edits."""
    draft = copy.deepcopy(validate_draft(read_json(project.analysis / "draft.json")))
    draft["source"] = {
        "path": "reference.png",
        "sha256": reviewed["reference"]["sha256"],
        "width": reviewed["canvas"]["width"],
        "height": reviewed["canvas"]["height"],
    }
    draft["canvas"] = copy.deepcopy(reviewed["canvas"])
    options = {"slots": {}, "overlays": {}, "background": {}}
    for kind, keys in (("slots", SLOT_FIELDS), ("overlays", OVERLAY_FIELDS)):
        draft[kind] = [
            {k: copy.deepcopy(v) for k, v in item.items() if k in keys}
            for item in reviewed[kind]
        ]
        options[kind] = {item["id"]: copy.deepcopy(item) for item in reviewed[kind]}
    photo = background_slot_id(reviewed)
    draft["background"] = (
        copy.deepcopy(reviewed["background"])
        if photo
        else {
            key: reviewed["background"][key]
            for key in ("background_brief", "review_notes")
        }
    )
    draft["version"] = "collage-draft/3"
    draft["layer_order"] = copy.deepcopy(reviewed["layer_order"])
    # This is a new manual revision of already accepted content, not a new VLM answer.
    # Original questions and approvals stay in the source evidence directory.
    draft["questions"] = []
    if not photo:
        options["background"] = {
            key: reviewed["background"][key]
            for key in ("expand_px", "feather_px", "composition_mode")
        }
    if (project.template / "template.json").is_file():
        template = validate_package(project.template, require_ready=False)
        if template["build"]["source_sha256"] != reviewed["reference"]["sha256"]:
            raise CollageError("SOURCE_HASH_MISMATCH", "模板与确认稿不是同一参考图")
        for kind in ("slots", "overlays"):
            current = {item["id"]: item for item in template[kind]}
            for item in draft[kind]:
                layout = current[item["id"]]
                item["target_rect"] = list(layout["rect"])
                options[kind][item["id"]]["rotation_deg"] = layout["rotation_deg"]
                if kind == "overlays":
                    item["attachment"] = copy.deepcopy(layout["attachment"])
        draft["layer_order"] = copy.deepcopy(template["layer_order"])
    return validate_draft(draft), options


def _copy_resource(source: Path, destination: Path) -> None:
    """Copy exact resource bytes; do not normalize a mask or prepared alpha asset."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def fork_background_review(
    store: ProjectStore, project_id: str, payload: object
) -> dict:
    """Create an unconfirmed revision and leave every source artifact unchanged."""
    source = store.open(project_id)
    with project_edit_lock(source.root), project_edit_lock(source.analysis):
        evidence = revision_document(source)
        if (
            not isinstance(payload, dict)
            or payload.get("revision") != evidence["revision"]
        ):
            raise CollageError("REVIEW_REVISION_CONFLICT", "源项目已更新，请刷新后另存")
        reviewed_path = source.review / "reviewed.json"
        reviewed = validate_reviewed_spec(read_json(reviewed_path))
        draft, options = _confirmed_draft(source, reviewed)
        reference = resolve_input_path(reviewed_path, reviewed["reference"]["path"])
        if sha256_file(reference) != reviewed["reference"]["sha256"]:
            raise CollageError("SOURCE_HASH_MISMATCH", "原参考图已变化，不能另存修订")
        manifest = store.get_manifest(project_id)
        workflow_options = manifest["workflow"]["options"]
        workflow = new_workflow(**workflow_options)
        target = store.create(
            project_id[:36] + "-background-" + uuid.uuid4().hex[:10],
            name=manifest["name"] + "（背景修订）",
        )
        LOGGER.info(
            "开始另存背景复核 | source=%s project=%s", project_id, target.project_id
        )
        # Failed copies must not become resumable drafts. Mark the new project as
        # incomplete first; no source files are removed even on failure.
        workflow.update(
            stage="failed",
            resume_stage=None,
            last_error={
                "code": "BACKGROUND_REVISION_INCOMPLETE",
                "message": "背景修订复制尚未完成，请从源项目重新另存",
            },
        )
        store.update_workflow(target.project_id, workflow, status="failed")
        try:
            for directory in ("inputs", "analysis", "review"):
                shutil.copytree(
                    source.root / directory,
                    target.reports / "parent_evidence" / directory,
                    ignore=shutil.ignore_patterns(".editing.lock"),
                )
            _copy_resource(reference, target.analysis / "reference.png")
            _copy_resource(reference, target.inputs / "reference.png")
            policy = source.analysis / "analysis_policy.json"
            if policy.is_file():
                _copy_resource(policy, target.analysis / "analysis_policy.json")
            for kind, fields in (
                ("slots", ("clip_mask", "font_path")),
                ("overlays", ("prepared_asset",)),
            ):
                for index, item in enumerate(options[kind].values()):
                    for field in fields:
                        if item.get(field):
                            path = resolve_input_path(reviewed_path, item[field])
                            relative = f"resources/{kind}_{index}_{field}{path.suffix}"
                            _copy_resource(path, target.review / relative)
                            item[field] = relative
            for field, filename in (
                ("remove_mask", "initial_remove_mask.png"),
                ("allowed_mask", "allowed_mask.png"),
                ("candidate_path", "background_candidate.png"),
            ):
                if reviewed["background"].get(field):
                    _copy_resource(
                        resolve_input_path(
                            reviewed_path, reviewed["background"][field]
                        ),
                        target.inputs / filename,
                    )
            # Carry cache candidates, not successful node states or prepared assets.
            # Existing build-time keys, prompt versions and fingerprints decide reuse.
            source_cache = NodeCache(source.workspace / "cache")
            target_cache = NodeCache(target.workspace / "cache")
            copied_cache = 0
            attempts = source.workspace / "overlay_attempts"
            if attempts.is_dir():
                # Preserve request uncertainty and bounded repair counts across a
                # background-only fork. A missing cache is not permission to resend
                # a previously pending paid request with the same semantic key.
                shutil.copytree(attempts, target.workspace / "overlay_attempts")
            for path in (source.workspace / "cache" / "overlay").glob("*.json"):
                if len(path.stem) != 64 or any(
                    c not in "0123456789abcdef" for c in path.stem
                ):
                    continue
                cached = source_cache.get("overlay", path.stem)
                if (
                    cached
                    and isinstance(cached.metadata, dict)
                    and cached.metadata.get("image_fingerprint")
                    == asset_fingerprint(cached.image)
                ):
                    target_cache.put(
                        "overlay", path.stem, cached.image, cached.metadata
                    )
                    copied_cache += 1
            bindings = source.renders / "bindings.json"
            if bindings.is_file():
                import_bindings(bindings, target.renders / "bindings.json", target)
            atomic_write_json(target.review / "revision_options.json", options)
            _draw_draft_preview(
                target.analysis / "reference.png",
                draft,
                target.analysis / "draft_preview.png",
            )
            evidence.update(
                source_project=project_id,
                new_project=target.project_id,
                kind="human_review_revision",
                copied_overlay_cache=copied_cache,
                network_calls=0,
                final_confirmed=False,
            )
            atomic_write_json(target.reports / "background_revision.json", evidence)
            if (
                revision_document(source)["revision"] != evidence["revision"]
                or sha256_file(target.analysis / "reference.png")
                != reviewed["reference"]["sha256"]
            ):
                raise CollageError(
                    "REVIEW_REVISION_CONFLICT", "复制期间源项目发生变化，请重新另存"
                )
            atomic_write_json(target.analysis / "draft.json", draft)
            transition(
                store,
                target,
                workflow,
                "awaiting_review",
                wait={
                    "code": "HUMAN_REVIEW_REQUIRED",
                    "message": "背景修订已另存，请选择背景来源并重新确认",
                },
            )
        except Exception as exc:
            error = CollageError(
                "BACKGROUND_REVISION_INCOMPLETE",
                "背景修订未完成，原项目未修改；请从源项目重新另存",
                details={"project_id": target.project_id},
            )
            workflow.update(
                stage="failed",
                resume_stage=None,
                last_error={"code": error.code, "message": error.message},
            )
            store.update_workflow(target.project_id, workflow, status="failed")
            raise error from exc
        LOGGER.info("背景复核新版本已保存 | project=%s", target.project_id)
        return {
            "ok": True,
            "project_id": target.project_id,
            "url": f"/projects/{target.project_id}/review",
        }
