"""Prepare upload instructions and customer bindings for workflow rendering."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..core.errors import CollageError
from ..core.io import atomic_write_json, read_json, resolve_input_path
from ..imaging.operations import alpha_is_meaningful, normalize_image
from ..projects import ProjectPaths
from ..providers import CutoutProvider, load_provider
from ..rendering.cutout import prepare_cutout
from ..schemas import validate_bindings
from ..template.guide import create_upload_guide
from ..template.validation import validate_package


def ensure_upload_files(project: ProjectPaths) -> None:
    """Generate the guide and a starter Bindings file without overwriting user edits."""

    example_path = project.renders / "bindings.example.json"
    create_upload_guide(
        project.template,
        project.reports / "upload_guide.html",
        example_path,
        require_ready=False,
    )
    bindings_path = project.renders / "bindings.json"
    if not bindings_path.exists():
        atomic_write_json(bindings_path, read_json(example_path))


def bindings_wait_reason(project: ProjectPaths) -> dict[str, Any] | None:
    """Return an actionable wait reason until Bindings and local files are usable."""

    bindings_path = project.renders / "bindings.json"
    if not bindings_path.is_file():
        return {
            "code": "BINDINGS_REQUIRED",
            "message": "请提供客户素材 Bindings",
            "details": {"path": "renders/bindings.json"},
        }
    template = validate_package(project.template, require_ready=False)
    try:
        bindings = validate_bindings(read_json(bindings_path), template)
    except CollageError as exc:
        return exc.as_dict()

    missing: list[dict[str, str]] = []
    for slot in template["slots"]:
        if slot["type"] != "image":
            continue
        binding = bindings["slots"].get(slot["id"])
        if not isinstance(binding, dict):
            continue
        for field in ("path", "subject_alpha"):
            raw_path = binding.get(field)
            if raw_path is None:
                continue
            if not isinstance(raw_path, str) or raw_path.startswith("REPLACE_"):
                missing.append({"slot": slot["id"], "field": field})
                continue
            if not resolve_input_path(bindings_path, raw_path).is_file():
                missing.append({"slot": slot["id"], "field": field})
    if missing:
        return {
            "code": "BINDING_FILES_REQUIRED",
            "message": "请替换 Bindings 占位路径并提供对应客户素材",
            "details": {"missing": missing},
        }
    return None


def prepare_cutout_bindings(
    project: ProjectPaths,
    *,
    provider_spec: str | None,
    allow_cloud_upload: bool,
) -> None:
    """Automatically prepare opaque inputs used by cutout template slots."""

    bindings_path = project.renders / "bindings.json"
    template = validate_package(project.template, require_ready=False)
    bindings = validate_bindings(read_json(bindings_path), template)
    template_slots = {slot["id"]: slot for slot in template["slots"]}
    provider: CutoutProvider | None = None
    changed = False
    prepared_dir = project.inputs / "prepared"

    for slot_id, binding in bindings["slots"].items():
        slot = template_slots[slot_id]
        if slot["type"] != "image" or slot["mode"] != "cutout":
            continue
        if binding.get("subject_alpha") is not None:
            continue
        input_path = resolve_input_path(bindings_path, binding["path"])
        if alpha_is_meaningful(normalize_image(input_path).convert("RGBA")):
            continue
        if provider_spec is None:
            raise CollageError(
                "CUTOUT_PROVIDER_UNAVAILABLE",
                f"槽位 {slot_id} 的普通图片需要抠图 provider",
            )
        if provider is None:
            provider = load_provider(provider_spec, CutoutProvider)
        prepared_dir.mkdir(parents=True, exist_ok=True)
        output_path = prepared_dir / f"{slot_id}.png"
        prepare_cutout(
            input_path,
            output_path,
            provider=provider,
            allow_cloud_upload=allow_cloud_upload,
        )
        binding["path"] = Path(
            os.path.relpath(output_path.resolve(), bindings_path.parent.resolve())
        ).as_posix()
        changed = True

    if changed:
        atomic_write_json(bindings_path, bindings)
