"""Prepare and persist one Draft review session independently of HTTP transport."""

from __future__ import annotations

import base64
import copy
import math
import io
import logging
from pathlib import Path
from typing import Any

from PIL import Image

from ..core.errors import CollageError, SpecValidationError
from ..core.io import (
    atomic_save_image,
    atomic_write_json,
    read_json,
    resolve_input_path,
)
from ..core.locking import project_edit_lock
from ..imaging.operations import load_mask
from ..schemas import validate_draft
from ..schemas.background import background_slot_id, validate_slot_background
from ..template.layout import expanded_order, transform_attachments
from ..template.structure_preview import render_structure
from ..template.review import (
    automatic_remove_mask,
    automatic_review_notes,
    background_parameter,
    build_review_options,
    confirm_draft,
    validate_override_map,
    validate_review_decisions,
)
from ..template.review.background_source import background_decision, edited_review_draft
from ..template.review.feedback import review_revision
from ..template.review.service import default_slot_review_fields, suggested_edge_fade_px

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
        initial_review_options: dict[str, Any] | None = None,
    ) -> None:
        self.draft_path = draft_path.resolve()
        self.output_path = output_path.resolve()
        restored_path = self.output_path.parent / "revision_options.json"
        if initial_review_options is None and restored_path.is_file():
            initial_review_options = read_json(restored_path)
        if initial_review_options:
            background_options = initial_review_options.get("background", {})
            background_expand_px = background_options.get(
                "expand_px", background_expand_px
            )
            background_feather_px = background_options.get(
                "feather_px", background_feather_px
            )
            background_composition_mode = background_options.get(
                "composition_mode", background_composition_mode
            )
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
        if initial_review_options:
            for kind in ("slots", "overlays"):
                for item in self.draft[kind]:
                    restored = initial_review_options.get(kind, {}).get(item["id"], {})
                    # A later model correction may replace semantic fields. Do not
                    # reapply an old text/shape or its confirmation to new content.
                    fields = {
                        k: v
                        for k, v in restored.items()
                        if k not in item
                        and k not in {"shape", "text_content", "default_text"}
                    }
                    if kind == "overlays" and any(
                        restored.get(key) != item.get(key)
                        for key in (
                            "source_rect",
                            "action",
                            "generation_brief",
                            "text_content",
                        )
                    ):
                        fields.pop("prepared_asset", None)
                    if kind == "overlays" and restored.get("text_content") != item.get(
                        "text_content"
                    ):
                        fields["text_confirmed"] = False
                    self.review_options[kind][item["id"]].update(fields)
        self.fixed_mask_png = self.mask_png
        if background_slot_id(self.draft):
            fallback = (
                load_mask(initial_mask_path, self.canvas_size, name="初始 remove_mask")
                if initial_mask_path
                else automatic_remove_mask(self.draft)
            )
            buffer = io.BytesIO()
            fallback.save(buffer, format="PNG")
            self.fixed_mask_png = buffer.getvalue()
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

    def layout(self, payload: dict) -> dict:
        """Edit one in-memory structural revision; no files or model requests."""
        if payload.get("revision") != self.review_options["revision"]:
            raise CollageError("REVIEW_REVISION_CONFLICT", "识别结果已更新，请重新载入")
        draft = edited_review_draft(self.draft, payload)
        options = copy.deepcopy(self.review_options)
        for kind in ("slots", "overlays"):
            overrides = validate_override_map(
                payload.get(kind[:-1] + "_overrides", {}), source="结构预览"
            )
            for identifier, fields in overrides.items():
                if identifier not in options[kind]:
                    raise CollageError("UNKNOWN_OVERRIDE_ID", "预览引用了未知元素")
                options[kind][identifier].update(fields)
        change = payload.get("change")
        if change is not None:
            if not isinstance(change, dict):
                raise CollageError("INVALID_LAYOUT", "布局修改必须是 object")
            kind = change.get("kind")
            if kind not in {"slot", "overlay"}:
                raise CollageError("INVALID_LAYOUT", "未知元素类型")
            item = next(
                (item for item in draft[kind + "s"] if item["id"] == change.get("id")),
                None,
            )
            if item is None or item["id"] == background_slot_id(draft):
                raise CollageError("LAYOUT_LAYER_LOCKED", "请选择非背景元素")
            if "attachment" in change:
                if kind != "overlay":
                    raise CollageError("INVALID_ATTACHMENT", "只有装饰可以设置归属")
                previous = item["attachment"]
                item["attachment"] = change["attachment"]
                reference = {"type": "overlay", "id": item["id"]}
                if item["attachment"] is not None:
                    draft["layer_order"] = [
                        layer for layer in draft["layer_order"] if layer != reference
                    ]
                elif reference not in draft["layer_order"]:
                    parent = {"type": "slot", "id": previous["slot_id"]}
                    draft["layer_order"].insert(
                        draft["layer_order"].index(parent) + 1, reference
                    )
            if "rect" in change or "rotation_deg" in change:
                fields = options[kind + "s"][item["id"]]
                rect = change.get("rect", item["target_rect"])
                rotation = change.get("rotation_deg", fields["rotation_deg"])
                if (
                    not isinstance(rect, list)
                    or len(rect) != 4
                    or any(
                        isinstance(v, bool)
                        or not isinstance(v, (int, float))
                        or not math.isfinite(v)
                        for v in [*rect, rotation]
                    )
                    or min(rect[2:]) < 1
                    or abs(rotation) > 3600
                ):
                    raise CollageError("INVALID_LAYOUT", "位置、尺寸或角度不合法")
                if kind == "slot":
                    children = [
                        {
                            **child,
                            "rotation_deg": options["overlays"][child["id"]][
                                "rotation_deg"
                            ],
                        }
                        for child in draft["overlays"]
                    ]
                    transform_attachments(
                        children,
                        item["id"],
                        item["target_rect"],
                        rect,
                        fields["rotation_deg"],
                        rotation,
                        rect_key="target_rect",
                    )
                    for child, transformed in zip(
                        draft["overlays"], children, strict=True
                    ):
                        child["target_rect"] = transformed["target_rect"]
                        options["overlays"][child["id"]]["rotation_deg"] = transformed[
                            "rotation_deg"
                        ]
                if kind == "slot" and item["type"] == "image":
                    old_default = suggested_edge_fade_px(item["target_rect"])
                    new_default = suggested_edge_fade_px(rect)
                    if (
                        item["mode"] == "photo_feather"
                        and fields["edge_fade_px"] == old_default
                    ):
                        fields["edge_fade_px"] = new_default
                    options["edge_fade_suggestions"][item["id"]] = new_default
                item["target_rect"] = list(rect)
                fields["rotation_deg"] = rotation
        draft = validate_draft(draft)
        # Reuse existing numeric constraints before allocating preview rasters.
        for kind in ("slots", "overlays"):
            for item in draft[kind]:
                rotation = options[kind][item["id"]]["rotation_deg"]
                if (
                    isinstance(rotation, bool)
                    or not isinstance(rotation, (int, float))
                    or not math.isfinite(rotation)
                    or abs(rotation) > 3600
                ):
                    raise CollageError("INVALID_LAYOUT", "旋转角度不合法")
        buffer = io.BytesIO()
        render_structure(draft, options).save(buffer, format="PNG")
        return {
            "draft": draft,
            "review_options": options,
            "draw_order": expanded_order(draft),
            "structure_preview": "data:image/png;base64,"
            + base64.b64encode(buffer.getvalue()).decode("ascii"),
        }

    def browser_state(self) -> dict[str, Any]:
        """Return one coherent review revision including its generated mask."""
        return {
            **self.layout(
                {"draft": self.draft, "revision": self.review_options["revision"]}
            ),
            "mask_data_url": "data:image/png;base64,"
            + base64.b64encode(self.mask_png).decode("ascii"),
            "fixed_mask_data_url": "data:image/png;base64,"
            + base64.b64encode(self.fixed_mask_png).decode("ascii"),
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
            other = payload.get("other_feedback", "")
            answers = payload.get("question_resolutions", [])
            if (
                not isinstance(other, str)
                or not isinstance(answers, list)
                or any(
                    not isinstance(item, dict)
                    or not isinstance(item.get("answer"), str)
                    for item in answers
                )
            ):
                raise CollageError("INVALID_REQUEST", "反馈必须是文字和问答列表")
            if other.strip() or any(item["answer"].strip() for item in answers):
                raise CollageError(
                    "REVIEW_CORRECTION_REQUIRED", "填写的反馈尚未提交纠正"
                )
            text_review_opened = payload.get("overlay_text_review_opened", True)
            if type(text_review_opened) is not bool:
                raise CollageError("INVALID_REQUEST", "文字复核展开状态必须是布尔值")
            edited = edited_review_draft(self.draft, payload)
            background_evidence = background_decision(self.draft, edited, payload)
            slot_overrides = validate_override_map(
                payload.get("slot_overrides", {}), source="确认页面 slot 覆盖"
            )
            overlay_overrides = validate_override_map(
                payload.get("overlay_overrides", {}),
                source="确认页面 overlay 覆盖",
            )
            # Restored project options are authoritative defaults even for older
            # clients that submit only a partial override map.
            for kind, overrides in (
                ("slots", slot_overrides),
                ("overlays", overlay_overrides),
            ):
                for item in edited[kind]:
                    defaults = dict(self.review_options[kind].get(item["id"], {}))
                    defaults.update({key: item[key] for key in defaults if key in item})
                    overrides[item["id"]] = {
                        **defaults,
                        **overrides.get(item["id"], {}),
                    }
            if background_slot_id(edited):
                issues = []
                validate_slot_background(
                    edited["background"],
                    [
                        {
                            **item,
                            **default_slot_review_fields(item),
                            **slot_overrides.get(item["id"], {}),
                        }
                        for item in edited["slots"]
                    ],
                    self.canvas_size,
                    issues,
                )
                if issues:
                    raise SpecValidationError(issues)
            accepted_default_text = self._accept_default_text(
                edited, overlay_overrides, opened=text_review_opened
            )
            validate_review_decisions(edited, slot_overrides, overlay_overrides)
            automatic_notes = automatic_review_notes(
                self.draft,
                slot_overrides,
                overlay_overrides,
                questions_deferred=bool(self.draft["questions"]),
            )
            notes = automatic_notes
            if accepted_default_text:
                notes += "\n- 整体确认时接受默认识别文字，未逐字核对：" + ", ".join(
                    accepted_default_text
                )
            edited["questions"] = []
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
                len(self.draft["questions"]),
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
                    "questions_accepted_as_is": self.draft["questions"],
                    "default_text_accepted": accepted_default_text,
                    "background_decision": background_evidence,
                },
            )
            return {"ok": True, "path": str(self.output_path)}

    def _accept_default_text(
        self,
        edited: dict[str, Any],
        overrides: dict[str, dict[str, Any]],
        *,
        opened: bool,
    ) -> list[str]:
        """Accept unchanged hidden text through the explicit overall confirmation."""
        accepted: list[str] = []
        if opened:
            return accepted
        originals = {item["id"]: item for item in self.draft["overlays"]}
        for item in edited["overlays"]:
            original = originals.get(item["id"])
            if original is None:
                continue
            defaults = self.review_options["overlays"][item["id"]]
            fields = {**defaults, **overrides.get(item["id"], {})}
            if (
                defaults.get("text_content")
                and fields.get("text_content") == defaults["text_content"]
                and item.get("text_content") == original.get("text_content")
                and fields.get("text_confirmed") is not True
            ):
                # text_confirmed means accepted for generation; the evidence below
                # distinguishes accepting defaults from an explicit character check.
                fields["text_confirmed"] = True
                overrides[item["id"]] = fields
                accepted.append(item["id"])
        return accepted
