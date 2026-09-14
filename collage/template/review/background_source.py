"""Apply explicit background choices while preserving trusted review provenance."""

from __future__ import annotations

import copy
from typing import Any

from ...core.errors import CollageError
from ...core.io import stable_hash
from ...schemas import validate_draft
from ...schemas.background import background_slot_id

CORE_FIELDS = ("slots", "overlays", "background", "layer_order", "questions")


def background_source(draft: dict[str, Any]) -> dict[str, str]:
    """Describe the machine-declared source, never infer customer intent."""
    slot_id = background_slot_id(draft)
    return {"mode": "slot", "slot_id": slot_id} if slot_id else {"mode": "fixed"}


def edited_review_draft(current: dict[str, Any], payload: dict[str, Any]) -> dict:
    """Validate edits before converting only an explicitly selected background."""
    edited = copy.deepcopy(payload.get("draft", current))
    if not isinstance(edited, dict):
        raise CollageError("INVALID_REQUEST", "当前编辑稿必须是 JSON object")
    for field in set(current) - set(CORE_FIELDS):
        edited[field] = copy.deepcopy(current[field])
    edited["questions"] = list(current["questions"])
    selection = payload.get("background_selection")
    if selection is None:
        # Older clients retain the original schema and cannot silently switch modes.
        return validate_draft(edited)
    if (
        not isinstance(selection, dict)
        or selection.get("mode") not in ("fixed", "slot")
        or set(selection)
        != ({"mode", "slot_id"} if selection.get("mode") == "slot" else {"mode"})
        or (selection["mode"] == "slot" and not isinstance(selection["slot_id"], str))
    ):
        raise CollageError("INVALID_BACKGROUND_SLOT", "背景来源选择无效")
    # Validate the submitted order first: conversion must not swallow duplicate,
    # missing or injected layers. Both pre-conversion and UI-converted drafts work.
    edited["version"] = "collage-draft/3"
    validate_draft(edited)
    if background_source(edited) != selection:
        if selection["mode"] == "slot":
            edited["background"] = {
                **selection,
                "review_notes": "用户选择客户满版照片提供背景，固定装饰仍独立制作",
            }
            edited["layer_order"] = [
                {"type": "slot", "id": selection["slot_id"]},
                *(
                    layer
                    for layer in edited["layer_order"]
                    if layer["type"] != "background"
                    and layer != {"type": "slot", "id": selection["slot_id"]}
                ),
            ]
        else:
            fixed = payload.get("fixed_background")
            if fixed is None and background_slot_id(current) is None:
                fixed = current["background"]
            if not isinstance(fixed, dict):
                raise CollageError(
                    "BACKGROUND_MODE_CONFLICT",
                    "切回固定底板时请填写清版说明并复核删除区域",
                )
            edited["background"] = copy.deepcopy(fixed)
            edited["layer_order"] = [{"type": "background"}, *edited["layer_order"]]
    edited["version"] = "collage-draft/3"
    validate_draft(edited)
    if (
        background_slot_id(current)
        and selection["mode"] == "fixed"
        and not edited["background"]["background_brief"].strip()
    ):
        raise CollageError(
            "BACKGROUND_MODE_CONFLICT", "切回固定底板时请填写清版说明并复核删除区域"
        )
    return edited


def background_decision(current: dict, edited: dict, payload: dict) -> dict:
    """Record human choice separately from model recognition and final approval."""
    return {
        "before": background_source(current),
        "after": background_source(edited),
        "source": "human_selection"
        if payload.get("background_selection") is not None
        else "accepted_current",
        "before_revision": stable_hash(current),
        "edited_revision": stable_hash(edited),
    }
