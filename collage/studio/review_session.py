"""Prepare and persist one Draft review session independently of HTTP transport."""

from __future__ import annotations

import base64
import copy
import io
import logging
from pathlib import Path
from typing import Any

from PIL import Image

from ..core.errors import CollageError
from ..core.io import (
    atomic_save_image,
    atomic_write_json,
    read_json,
    resolve_input_path,
)
from ..core.locking import project_edit_lock
from ..template.review.feedback import review_revision
from ..imaging.operations import load_mask
from ..schemas import validate_draft
from ..schemas.background import background_slot_id
from ..template.review import (
    automatic_remove_mask,
    automatic_review_notes,
    background_parameter,
    build_review_options,
    confirm_draft,
    question_resolution_notes,
    validate_override_map,
    validate_review_decisions,
)

LOGGER = logging.getLogger(__name__)


def _decode_mask(data_url: str, expected_size: tuple[int, int]) -> Image.Image:
    """Decode the browser's alpha-backed PNG mask and validate its dimensions."""

    prefix = "data:image/png;base64,"
    if not isinstance(data_url, str) or not data_url.startswith(prefix):
        raise CollageError("INVALID_MASK_PAYLOAD", "mask 必须是 PNG data URL")
    try:
        raw = base64.b64decode(data_url[len(prefix) :], validate=True)
        with Image.open(io.BytesIO(raw)) as source:
            source.load()
            mask = source.convert("RGBA").getchannel("A")
    except Exception as exc:
        raise CollageError("INVALID_MASK_PAYLOAD", "无法解码 mask PNG") from exc
    if mask.size != expected_size:
        raise CollageError("MASK_SIZE_MISMATCH", "界面保存的 mask 与工作画布尺寸不一致")
    return mask


class ReviewSession:
    """Expose Draft review assets and one thread-safe save operation."""

    def __init__(
        self,
        draft_path: Path,
        output_path: Path,
        *,
        reviewer: str,
        initial_mask_path: Path | None = None,
        allowed_mask_path: Path | None = None,
        background_candidate_path: Path | None = None,
        slot_overrides_path: Path | None = None,
        overlay_overrides_path: Path | None = None,
        background_expand_px: int = 0,
        background_feather_px: int = 0,
        background_composition_mode: str = "protected",
    ) -> None:
        self.draft_path = draft_path.resolve()
        self.output_path = output_path.resolve()
        self.reviewer = reviewer
        self.allowed_mask_path = allowed_mask_path
        self.background_candidate_path = background_candidate_path
        self.slot_overrides_path = slot_overrides_path
        self.overlay_overrides_path = overlay_overrides_path
        self.background_expand_px = background_expand_px
        self.background_feather_px = background_feather_px
        self.background_composition_mode = background_composition_mode

        self.draft = validate_draft(read_json(self.draft_path))
        self.reference_path = resolve_input_path(
            self.draft_path, self.draft["source"]["path"]
        )
        self.canvas_size = (
            self.draft["canvas"]["width"],
            self.draft["canvas"]["height"],
        )
        if background_slot_id(self.draft):
            initial_mask = Image.new("L", self.canvas_size, 0)
            initial_mask_source = "not_required"
        elif initial_mask_path is not None:
            initial_mask = load_mask(
                initial_mask_path, self.canvas_size, name="初始 remove_mask"
            )
            initial_mask_source = "provided_file"
            LOGGER.info("使用用户提供的初始清版蒙版 | path=%s", initial_mask_path)
        else:
            initial_mask = automatic_remove_mask(self.draft)
            initial_mask_source = "draft_rects"
        mask_buffer = io.BytesIO()
        initial_mask.save(mask_buffer, format="PNG")
        self.mask_png = mask_buffer.getvalue()

        self.review_options = build_review_options(
            self.draft,
            slot_overrides_path,
            overlay_overrides_path,
            background_expand_px=background_expand_px,
            background_feather_px=background_feather_px,
            background_composition_mode=background_composition_mode,
        )
        self.review_options["initial_mask_source"] = initial_mask_source
        self.review_options["revision"] = review_revision(self.draft)
        feather_count = sum(
            1
            for slot in self.draft["slots"]
            if slot["type"] == "image" and slot["mode"] == "photo_feather"
        )
        LOGGER.info(
            "确认页制作参数已准备 | slots=%s overlays=%s photo_feather=%s questions=%s",
            len(self.draft["slots"]),
            len(self.draft["overlays"]),
            feather_count,
            len(self.draft["questions"]),
        )

    def browser_state(self) -> dict[str, Any]:
        """Return one coherent review revision including its generated mask."""
        return {
            "draft": self.draft,
            "review_options": self.review_options,
            "mask_data_url": "data:image/png;base64,"
            + base64.b64encode(self.mask_png).decode("ascii"),
        }

    def save(self, payload: Any) -> dict[str, Any]:
        """Validate browser edits and create the canonical ReviewedSpec."""

        if not isinstance(payload, dict) or not isinstance(payload.get("draft"), dict):
            raise CollageError("INVALID_REQUEST", "保存请求格式错误")
        with project_edit_lock(self.draft_path.parent):
            if self.output_path.exists():
                raise CollageError("REVIEW_ALREADY_SAVED", "该识别结果已经确认")
            revision = review_revision(validate_draft(read_json(self.draft_path)))
            if payload.get("revision") != revision or revision != review_revision(
                self.draft
            ):
                raise CollageError(
                    "REVIEW_REVISION_CONFLICT", "识别结果已更新，请重新载入后复核"
                )
            if payload.get("final_confirmed") is not True:
                raise CollageError(
                    "HUMAN_REVIEW_REQUIRED", "请手动确认当前识别结果没有问题"
                )
            if self.draft["questions"] or payload.get("defer_questions") is True:
                raise CollageError(
                    "REVIEW_CORRECTION_REQUIRED",
                    "请先回答疑问并提交给 VLM 纠正，再确认新结果",
                )
            if payload.get("other_feedback") or payload.get("question_resolutions"):
                raise CollageError(
                    "REVIEW_CORRECTION_REQUIRED", "填写的反馈尚未提交纠正"
                )
            edited = copy.deepcopy(payload["draft"])
            # The browser may edit semantics but never program-owned provenance.
            for field in (
                "version",
                "status",
                "source",
                "canvas",
                "prompt_version",
                "provider",
                "created_at",
            ):
                edited[field] = self.draft[field]
            validate_draft(edited)
            slot_overrides = validate_override_map(
                payload.get("slot_overrides", {}), source="确认页面 slot 覆盖"
            )
            overlay_overrides = validate_override_map(
                payload.get("overlay_overrides", {}),
                source="确认页面 overlay 覆盖",
            )
            validate_review_decisions(edited, slot_overrides, overlay_overrides)
            if payload.get("defer_questions") is True:
                question_notes = ""
            else:
                question_notes = question_resolution_notes(
                    self.draft["questions"], payload.get("question_resolutions", [])
                )
            automatic_notes = automatic_review_notes(
                self.draft,
                slot_overrides,
                overlay_overrides,
                questions_deferred=payload.get("defer_questions") is True,
            )
            notes = "\n\n".join(
                part for part in (question_notes, automatic_notes) if part
            )
            LOGGER.info(
                "确认页自动策略已应用 | inferred_text=%s fallback_fonts=%s "
                "approximate_overlays=%s deferred_questions=%s",
                sum(
                    1
                    for source in self.draft["slots"]
                    if source["type"] == "text"
                    and source.get("default_text") is None
                    and slot_overrides.get(source["id"], {}).get("default_text")
                ),
                sum(
                    1
                    for source in self.draft["slots"]
                    if source["type"] == "text"
                    and slot_overrides.get(source["id"], {}).get("fallback_approved")
                    is True
                ),
                sum(
                    1
                    for source in self.draft["overlays"]
                    if source.get("requires_exact_content") is True
                    and overlay_overrides.get(source["id"], {}).get(
                        "requires_exact_content"
                    )
                    is False
                ),
                (
                    len(self.draft["questions"])
                    if payload.get("defer_questions") is True
                    else 0
                ),
            )
            mask = None
            selected_expand_px = 0
            selected_feather_px = 0
            selected_composition_mode = "protected"
            if background_slot_id(edited) is None:
                mask = _decode_mask(payload.get("mask_png", ""), self.canvas_size)
                if mask.getbbox() is None:
                    if payload.get("empty_mask_approved") is not True:
                        raise CollageError(
                            "EMPTY_REMOVE_MASK",
                            "删除蒙版为空；请画出需要清版的旧内容，或明确确认无需删除",
                        )
                    LOGGER.warning("模板作者明确接受空删除蒙版")
                else:
                    LOGGER.info("删除蒙版已确认 | bbox=%s", mask.getbbox())
                selected_expand_px = background_parameter(
                    payload, "background_expand_px", self.background_expand_px
                )
                selected_feather_px = background_parameter(
                    payload, "background_feather_px", self.background_feather_px
                )
                selected_composition_mode = payload.get(
                    "background_composition_mode", self.background_composition_mode
                )
                if selected_composition_mode not in ("protected", "full_candidate"):
                    raise CollageError("INVALID_REQUEST", "不支持的背景合成方式")
                if (
                    selected_composition_mode == "full_candidate"
                    and self.allowed_mask_path
                ):
                    allowed = load_mask(
                        self.allowed_mask_path, self.canvas_size, name="allowed_mask"
                    )
                    if allowed.getextrema() != (255, 255):
                        raise CollageError(
                            "BACKGROUND_MODE_CONFLICT",
                            "整张重建与局部 allowed_mask 冲突",
                        )
            # Keep the edited Draft beside the original so its source-relative
            # reference path remains valid when project review output lives elsewhere.
            ui_draft_path = self.draft_path.parent / "ui_confirmed_draft.json"
            ui_mask_path = (
                self.output_path.parent / "remove_mask.png"
                if mask is not None
                else None
            )
            atomic_write_json(ui_draft_path, edited)
            if mask is not None:
                atomic_save_image(mask, ui_mask_path)
            confirm_draft(
                ui_draft_path,
                self.output_path,
                remove_mask_path=ui_mask_path,
                reviewer=self.reviewer,
                allowed_mask_path=self.allowed_mask_path,
                background_candidate_path=self.background_candidate_path,
                slot_overrides_path=self.slot_overrides_path,
                overlay_overrides_path=self.overlay_overrides_path,
                slot_overrides_data=slot_overrides,
                overlay_overrides_data=overlay_overrides,
                background_expand_px=selected_expand_px,
                background_feather_px=selected_feather_px,
                background_composition_mode=selected_composition_mode,
                notes=notes,
            )
            atomic_write_json(
                self.output_path.parent / "confirmation.json",
                {
                    "revision": revision,
                    "final_confirmed": True,
                    "reviewer": self.reviewer,
                    "confirmed_draft": "ui_confirmed_draft.json",
                },
            )
            return {"ok": True, "path": str(self.output_path)}
