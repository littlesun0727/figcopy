"""Recover saved model corrections locally, preserving the original response and review boundaries."""

from __future__ import annotations

import copy
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ...core.errors import CollageError
from ...core.io import read_json, resolve_input_path, sha256_file
from ...core.locking import project_edit_lock
from ...schemas import validate_draft
from ...schemas.background import background_slot_id
from .feedback import _corrected_draft, _persist_correction, review_revision

LOGGER = logging.getLogger(__name__)
_REQUEST_ID = re.compile(r"^[0-9a-f]{64}$")


def _suggest_background_slot(
    raw: dict[str, Any], current: dict[str, Any]
) -> str | None:
    """Suggest a legacy conversion for explicit acceptance, never silently migrate old files."""
    layers = raw.get("layer_order")
    if not isinstance(layers, list) or not layers or not isinstance(layers[0], dict):
        return None
    if any(
        isinstance(layer, dict) and layer.get("type") == "background"
        for layer in layers
    ):
        return None
    if layers[0].get("type") != "slot":
        return None
    slots = raw.get("slots")
    if not isinstance(slots, list):
        return None
    candidates = [
        slot
        for slot in slots
        if isinstance(slot, dict)
        and slot.get("id") == layers[0].get("id")
        and slot.get("type") == "image"
        and slot.get("mode") == "photo"
        and slot.get("target_rect")
        == [0, 0, current["canvas"]["width"], current["canvas"]["height"]]
    ]
    return candidates[0]["id"] if len(candidates) == 1 else None


def available_recoveries(draft_path: Path) -> list[dict[str, Any]]:
    """Describe saved responses for the currently loaded revision without reading images."""
    if not draft_path.is_file():
        return []
    current = validate_draft(read_json(draft_path))
    revision = review_revision(current)
    options = []
    for request_path in (draft_path.parent / "feedback").glob("*/request.json"):
        if not _REQUEST_ID.fullmatch(request_path.parent.name):
            continue
        try:
            request = read_json(request_path)
            if (
                request.get("status") not in {"failed", "pending"}
                or request.get("base_revision") != revision
            ):
                continue
            response = read_json(request_path.parent / "response.json")
            raw = response.get("result")
            if not isinstance(raw, dict):
                continue
            options.append(
                {
                    "request_id": request_path.parent.name,
                    "submitted_at": request.get("submitted_at", ""),
                    "background_slot_id": (
                        None
                        if background_slot_id(raw)
                        else _suggest_background_slot(raw, current)
                    ),
                }
            )
        except (CollageError, OSError, ValueError, TypeError, KeyError):
            continue
    return sorted(options, key=lambda item: item["submitted_at"], reverse=True)


def recover_saved_correction(
    draft_path: Path,
    request_id: str,
    *,
    background_slot: str | None = None,
    reviewed_path: Path | None = None,
) -> dict[str, Any]:
    """Apply one saved response without a provider; explicit conversion never edits raw evidence."""
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        raise CollageError("INVALID_REVIEW_REQUEST_ID", "纠正记录 ID 无效")
    draft_path = draft_path.resolve()
    with project_edit_lock(draft_path.parent):
        # Recovery restores a Draft only; it cannot authorize or overwrite a confirmed template.
        standard_review = draft_path.parent.parent / "review" / "reviewed.json"
        project_confirmed = (
            draft_path.parent.name == "analysis"
            and (draft_path.parent.parent / "project.json").is_file()
            and standard_review.exists()
        )
        if (
            project_confirmed
            or (reviewed_path and reviewed_path.exists())
            or (draft_path.parent / "reviewed.json").exists()
        ):
            raise CollageError(
                "REVIEW_ALREADY_SAVED", "已确认的识别结果不能恢复为其他草稿"
            )
        current = validate_draft(read_json(draft_path))
        feedback_dir = (draft_path.parent / "feedback").resolve()
        request_dir = (feedback_dir / request_id).resolve()
        if not request_dir.is_relative_to(feedback_dir):
            raise CollageError("INVALID_REVIEW_REQUEST_ID", "纠正记录路径超出当前项目")
        request_path = request_dir / "request.json"
        response_path = request_dir / "response.json"
        if not request_path.is_file() or not response_path.is_file():
            raise CollageError(
                "REVIEW_RESPONSE_UNAVAILABLE",
                "没有可恢复的模型响应；未发起新的模型请求",
            )
        request = read_json(request_path)
        current_revision = review_revision(current)
        if (
            request.get("status") == "complete"
            and request.get("revision") == current_revision
        ):
            return {
                "ok": True,
                "revision": current_revision,
                "questions": len(current["questions"]),
                "network_calls": 0,
            }
        if request.get("status") not in {"failed", "pending"}:
            raise CollageError(
                "REVIEW_RECOVERY_NOT_AVAILABLE", "该纠正记录当前不可恢复"
            )
        before = validate_draft(read_json(request_dir / "before.json"))
        if (
            request.get("base_revision") != current_revision
            or review_revision(before) != current_revision
        ):
            raise CollageError(
                "REVIEW_REVISION_CONFLICT", "草稿已变更，不能用旧模型结果覆盖"
            )
        reference = resolve_input_path(draft_path, current["source"]["path"])
        if sha256_file(reference) != current["source"]["sha256"]:
            raise CollageError(
                "SOURCE_HASH_MISMATCH", "参考图已变更，不能恢复旧纠正结果"
            )
        response = read_json(response_path)
        raw = copy.deepcopy(response.get("result"))
        if background_slot is not None:
            # Changing background semantics requires an explicit selection, not a guessed missing field.
            if (
                not isinstance(raw, dict)
                or _suggest_background_slot(raw, current) != background_slot
            ):
                raise CollageError(
                    "INVALID_BACKGROUND_SLOT",
                    "所选背景必须是返回结果中位于首层的全屏普通照片槽",
                )
            raw["background"] = {
                "mode": "slot",
                "slot_id": background_slot,
                "review_notes": "背景由客户上传的全屏照片提供，无需制作固定背景或清版旧照片。",
            }
        corrected = _corrected_draft(
            current, raw, response.get("audit"), prompt_version=request["version"]
        )
        request["recovery"] = {
            "method": "saved_response",
            "recovered_at": datetime.now(UTC).isoformat(),
            "previous_status": request["status"],
            "response_sha256": sha256_file(response_path),
            "background_slot_id": background_slot,
            "network_calls": 0,
        }
        result = _persist_correction(
            draft_path, reference, corrected, request_path, request
        )
        LOGGER.info(
            "已从保存的模型响应恢复改稿 | network_calls=0 questions=%s",
            result["questions"],
        )
        return {**result, "network_calls": 0}
