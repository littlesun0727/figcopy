"""Validate customer bindings against a template manifest."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..core.errors import SpecValidationError, ValidationIssue
from .common import _issue, _keys, _number, _object, _pair, _string


def validate_bindings(value: Any, template: Mapping[str, Any]) -> dict[str, Any]:
    """校验客户输入绑定，不触碰文件系统或调用 provider。"""

    issues: list[ValidationIssue] = []
    bindings = _object(value, "$", issues)
    if bindings is None:
        raise SpecValidationError(issues)
    _keys(
        bindings, required={"version", "slots"}, optional=set(), path="$", issues=issues
    )
    if bindings.get("version") != "collage-bindings/1":
        _issue(issues, "$.version", "必须是 collage-bindings/1")
    values = _object(bindings.get("slots"), "$.slots", issues)
    template_slots = {
        slot["id"]: slot
        for slot in template.get("slots", [])
        if isinstance(slot, Mapping) and "id" in slot
    }
    if values is not None:
        for slot_id in sorted(values):
            path = f"$.slots.{slot_id}"
            if slot_id not in template_slots:
                _issue(issues, path, "模板中不存在该槽位", "UNKNOWN_SLOT")
                continue
            binding = _object(values[slot_id], path, issues)
            if binding is None:
                continue
            slot = template_slots[slot_id]
            if slot.get("type") == "image":
                _keys(
                    binding,
                    required={"path"},
                    optional={"subject_alpha", "scale", "offset_px"},
                    path=path,
                    issues=issues,
                )
                _string(binding.get("path"), f"{path}.path", issues)
                if binding.get("subject_alpha") is not None:
                    _string(
                        binding.get("subject_alpha"), f"{path}.subject_alpha", issues
                    )
                if "scale" in binding:
                    _number(
                        binding.get("scale"),
                        f"{path}.scale",
                        issues,
                        minimum=0.05,
                        maximum=20,
                    )
                if "offset_px" in binding:
                    _pair(
                        binding.get("offset_px"),
                        f"{path}.offset_px",
                        issues,
                        minimum=-100000,
                        maximum=100000,
                    )
            else:
                _keys(
                    binding, required=set(), optional={"text"}, path=path, issues=issues
                )
                if "text" in binding:
                    _string(
                        binding.get("text"), f"{path}.text", issues, allow_empty=True
                    )
        for slot_id, slot in template_slots.items():
            has_confirmed_default = (
                slot.get("type") == "text" and slot.get("default_text") is not None
            )
            if (
                slot.get("required")
                and slot_id not in values
                and not has_confirmed_default
            ):
                _issue(issues, f"$.slots.{slot_id}", "缺少必填槽位", "MISSING_BINDING")
    if issues:
        raise SpecValidationError(issues, "Bindings 校验失败")
    return dict(bindings)
