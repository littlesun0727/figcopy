"""Validate the origin of build decisions separately from human approval."""

from __future__ import annotations

import re
from typing import Any

from ..core.errors import ValidationIssue
from .common import _enum, _issue, _keys, _list, _object, _string

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def validate_sha256(value: Any, path: str, issues: list[ValidationIssue]) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        _issue(issues, path, "必须是小写 SHA-256", "INVALID_EVIDENCE_HASH")


def validate_provenance(value: Any, path: str, issues: list[ValidationIssue]) -> None:
    """检查决策来源；有未决问题的自动规格不能进入制作。"""

    record = _object(value, path, issues)
    if record is None:
        return
    _keys(
        record,
        required={"kind", "policy_version", "evidence_sha256", "unresolved"},
        optional={"continuation"},
        path=path,
        issues=issues,
    )
    _enum(
        record.get("kind"),
        {"human", "automatic", "fixture", "diagnostic"},
        f"{path}.kind",
        issues,
    )
    _string(record.get("policy_version"), f"{path}.policy_version", issues)
    evidence = _list(record.get("evidence_sha256"), f"{path}.evidence_sha256", issues)
    if evidence is not None:
        for index, digest in enumerate(evidence):
            validate_sha256(digest, f"{path}.evidence_sha256[{index}]", issues)
        if not evidence:
            _issue(issues, f"{path}.evidence_sha256", "制作决策必须绑定来源证据")
    unresolved = _list(record.get("unresolved"), f"{path}.unresolved", issues)
    diagnostic = record.get("kind") == "diagnostic"
    if diagnostic:
        continuation = _object(
            record.get("continuation"), f"{path}.continuation", issues
        )
        if continuation is not None:
            _keys(
                continuation,
                required={"instruction_source", "reason", "structure_sha256"},
                optional=set(),
                path=f"{path}.continuation",
                issues=issues,
            )
            _enum(
                continuation.get("instruction_source"),
                {"explicit_user_instruction"},
                f"{path}.continuation.instruction_source",
                issues,
            )
            _string(continuation.get("reason"), f"{path}.continuation.reason", issues)
            validate_sha256(
                continuation.get("structure_sha256"),
                f"{path}.continuation.structure_sha256",
                issues,
            )
    elif "continuation" in record:
        _issue(
            issues, f"{path}.continuation", "继续未决结构必须明确标记为 diagnostic 来源"
        )
    if unresolved is not None:
        for index, item in enumerate(unresolved):
            _string(item, f"{path}.unresolved[{index}]", issues)
    # An explicitly requested diagnostic keeps uncertainties in the package; it is never release approval.
    if unresolved and not diagnostic:
        _issue(
            issues,
            f"{path}.unresolved",
            "关键制作决策尚未解决",
            "UNRESOLVED_BUILD_DECISION",
        )
