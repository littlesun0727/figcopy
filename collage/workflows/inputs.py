"""Import workflow inputs into a self-contained project directory."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..core.errors import CollageError
from ..core.io import (
    atomic_save_image,
    atomic_write_json,
    decode_image,
    read_json,
    resolve_input_path,
)
from ..imaging.operations import normalize_image
from ..projects import ProjectPaths


def import_json(source: Path, destination: Path) -> None:
    """Parse and atomically copy JSON so projects never depend on the source file."""

    atomic_write_json(destination, read_json(source.resolve()))


def import_image(source: Path, destination: Path) -> None:
    """Normalize an image and atomically stage it inside the project."""

    image = normalize_image(source.resolve())
    atomic_save_image(image, destination)


def import_mask(source: Path, destination: Path) -> None:
    """Decode a mask to a portable single-channel PNG."""

    atomic_save_image(decode_image(source.resolve(), mode="L"), destination)


def _project_relative(path: Path, base_file: Path) -> str:
    return Path(os.path.relpath(path.resolve(), base_file.parent.resolve())).as_posix()


def import_bindings(
    source: Path,
    destination: Path,
    project: ProjectPaths,
) -> Path:
    """Copy a Bindings file and its referenced images into project inputs."""

    source = source.resolve()
    value = read_json(source)
    if not isinstance(value, dict) or not isinstance(value.get("slots"), dict):
        raise CollageError(
            "INVALID_BINDINGS_INPUT",
            "Bindings 必须包含 slots JSON object",
        )
    imported: dict[str, Any] = {**value, "slots": {}}
    customer_dir = project.inputs / "customer"
    customer_dir.mkdir(parents=True, exist_ok=True)
    for index, slot_id in enumerate(sorted(value["slots"]), start=1):
        raw_binding = value["slots"][slot_id]
        if not isinstance(raw_binding, dict):
            imported["slots"][slot_id] = raw_binding
            continue
        binding = dict(raw_binding)
        for field, suffix in (("path", "image"), ("subject_alpha", "alpha")):
            raw_path = binding.get(field)
            if not isinstance(raw_path, str) or raw_path.startswith("REPLACE_"):
                continue
            input_path = resolve_input_path(source, raw_path)
            output_path = customer_dir / f"{index:03d}_{suffix}.png"
            if field == "subject_alpha":
                import_mask(input_path, output_path)
            else:
                import_image(input_path, output_path)
            binding[field] = _project_relative(output_path, destination)
        imported["slots"][slot_id] = binding
    atomic_write_json(destination, imported)
    return destination
