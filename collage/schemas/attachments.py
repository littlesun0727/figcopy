"""Validate the single-level attachment relationship shared by all layout specs."""

from collections.abc import Mapping

from .common import _enum, _identifier, _issue, _keys, _object


def validate_attachments(overlays, slots, background_slot, issues):
    """Return independent IDs; only foreground image slots can own attachments."""
    owners = {
        slot.get("id")
        for slot in slots or []
        if isinstance(slot, Mapping)
        and isinstance(slot.get("id"), str)
        and slot.get("type") == "image"
        and slot.get("id") != background_slot
    }
    independent = set()
    for index, overlay in enumerate(overlays or []):
        if not isinstance(overlay, Mapping):
            continue
        path = f"$.overlays[{index}].attachment"
        attachment = overlay.get("attachment")
        if attachment is None:
            if isinstance(overlay.get("id"), str):
                independent.add(overlay["id"])
            continue
        attachment = _object(attachment, path, issues)
        if attachment is None:
            continue
        _keys(
            attachment,
            required={"slot_id", "position"},
            optional=set(),
            path=path,
            issues=issues,
        )
        if _identifier(attachment.get("slot_id"), f"{path}.slot_id", issues):
            if attachment["slot_id"] not in owners:
                _issue(
                    issues,
                    f"{path}.slot_id",
                    "附属物必须属于非背景图片槽",
                    "INVALID_ATTACHMENT",
                )
        _enum(
            attachment.get("position"), {"above", "below"}, f"{path}.position", issues
        )
    return independent
