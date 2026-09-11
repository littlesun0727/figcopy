"""审核界面的默认值、自动决策和门禁规则。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from ...core.errors import CollageError
from .service import (
    EDGE_FADE_MAX_PX,
    EDGE_FADE_RATIO,
    default_overlay_review_fields,
    default_slot_review_fields,
    load_override_map,
    suggested_edge_fade_px,
)

LOGGER = logging.getLogger(__name__)
AUTO_MASK_PADDING_RATIO = 0.015
AUTO_MASK_MIN_PADDING_PX = 2
AUTO_MASK_MAX_PADDING_PX = 24


def automatic_remove_mask(draft: dict[str, Any]) -> Image.Image:
    """用 Draft 的源图矩形自动生成白=删除的初始清版蒙版。"""

    width = int(draft["canvas"]["width"])
    height = int(draft["canvas"]["height"])
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    regions = [*draft["slots"], *draft["overlays"]]
    drawn = 0
    for item in regions:
        rect = item.get("source_rect")
        if not isinstance(rect, list) or len(rect) != 4:
            continue
        x, y, rect_width, rect_height = (float(value) for value in rect)
        padding = min(
            AUTO_MASK_MAX_PADDING_PX,
            max(
                AUTO_MASK_MIN_PADDING_PX,
                round(min(rect_width, rect_height) * AUTO_MASK_PADDING_RATIO),
            ),
        )
        left = max(0, int(x) - padding)
        top = max(0, int(y) - padding)
        right = min(width, round(x + rect_width) + padding)
        bottom = min(height, round(y + rect_height) + padding)
        if right <= left or bottom <= top:
            continue
        # Pillow 的矩形右下角为闭区间，因此减一保持计算后的尺寸不多一像素。
        draw.rectangle((left, top, right - 1, bottom - 1), fill=255)
        drawn += 1

    histogram = mask.histogram()
    covered_pixels = sum(histogram[1:])
    coverage = covered_pixels / (width * height) if width and height else 0.0
    LOGGER.info(
        "已根据 Draft 自动涂清版蒙版 | regions=%s bbox=%s coverage=%.1f%%",
        drawn,
        mask.getbbox(),
        coverage * 100,
    )
    return mask


def build_review_options(
    draft: dict[str, Any],
    slot_overrides_path: Path | None,
    overlay_overrides_path: Path | None,
    *,
    background_expand_px: int,
    background_feather_px: int,
    background_composition_mode: str = "protected",
) -> dict[str, Any]:
    """生成确认页初值，同时保留命令行传入的高级覆盖配置。"""

    file_slot_overrides = load_override_map(slot_overrides_path)
    file_overlay_overrides = load_override_map(overlay_overrides_path)
    slot_ids = {item["id"] for item in draft["slots"]}
    overlay_ids = {item["id"] for item in draft["overlays"]}
    if unknown := set(file_slot_overrides) - slot_ids:
        raise CollageError(
            "UNKNOWN_OVERRIDE_ID",
            f"slot 覆盖包含未知 ID：{', '.join(sorted(unknown))}",
        )
    if unknown := set(file_overlay_overrides) - overlay_ids:
        raise CollageError(
            "UNKNOWN_OVERRIDE_ID",
            f"overlay 覆盖包含未知 ID：{', '.join(sorted(unknown))}",
        )

    slots: dict[str, dict[str, Any]] = {}
    suggestions: dict[str, int] = {}
    for source in draft["slots"]:
        fields = default_slot_review_fields(source)
        fields.update(file_slot_overrides.get(source["id"], {}))
        slots[source["id"]] = fields
        if (
            source["type"] == "text"
            and source.get("default_text") is None
            and fields.get("default_text")
        ):
            LOGGER.info(
                "从 Draft 已有识别结果回填默认文字 | slot=%s text=%s",
                source["id"],
                fields["default_text"],
            )
        if source["type"] == "image":
            suggestions[source["id"]] = suggested_edge_fade_px(source["target_rect"])

    overlays: dict[str, dict[str, Any]] = {}
    for source in draft["overlays"]:
        file_override = file_overlay_overrides.get(source["id"], {})
        fields = {
            **default_overlay_review_fields(),
            "requires_exact_content": source["requires_exact_content"],
            "text_content": source.get("text_content"),
            "text_confirmed": False,
            "shape": source.get("shape"),
        }
        fields.update(file_override)
        overlays[source["id"]] = fields

    return {
        "slots": slots,
        "overlays": overlays,
        "edge_fade_ratio": EDGE_FADE_RATIO,
        "edge_fade_max_px": EDGE_FADE_MAX_PX,
        "edge_fade_suggestions": suggestions,
        "background": {
            "expand_px": background_expand_px,
            "feather_px": background_feather_px,
            "composition_mode": background_composition_mode,
        },
    }


def question_resolution_notes(questions: list[str], value: object) -> str:
    """要求逐题填写决定，并把问答保存到 ReviewedSpec 的审核说明。"""

    if not questions:
        return ""
    if not isinstance(value, list) or len(value) != len(questions):
        raise CollageError(
            "HUMAN_REVIEW_REQUIRED",
            "必须逐项回答 Draft 中的待确认问题",
            details={"question_count": len(questions)},
        )
    answers: list[str] = []
    for index, question in enumerate(questions):
        item = value[index]
        answer = item.get("answer") if isinstance(item, dict) else None
        echoed_question = item.get("question") if isinstance(item, dict) else None
        if (
            echoed_question != question
            or not isinstance(answer, str)
            or not answer.strip()
        ):
            raise CollageError(
                "HUMAN_REVIEW_REQUIRED",
                "必须逐项回答 Draft 中的待确认问题",
                details={"missing_answer_index": index},
            )
        answers.append(answer.strip())
    lines = ["确认页问题记录："]
    for question, answer in zip(questions, answers, strict=True):
        lines.extend((f"Q: {question}", f"A: {answer}"))
    return "\n".join(lines)


def automatic_review_notes(
    draft: dict[str, Any],
    slot_overrides: dict[str, dict[str, Any]],
    overlay_overrides: dict[str, dict[str, Any]],
    *,
    questions_deferred: bool,
) -> str:
    """记录确认页代用户完成的非关键默认决策，便于后续审计和 debug。"""

    lines = ["确认页自动处理记录："]
    inferred_text = [
        source["id"]
        for source in draft["slots"]
        if source["type"] == "text"
        and source.get("default_text") is None
        and slot_overrides.get(source["id"], {}).get("default_text")
    ]
    fallback_fonts = [
        source["id"]
        for source in draft["slots"]
        if source["type"] == "text"
        and not slot_overrides.get(source["id"], {}).get("font_path")
        and slot_overrides.get(source["id"], {}).get("fallback_approved") is True
    ]
    approximate_overlays = [
        source["id"]
        for source in draft["overlays"]
        if source.get("requires_exact_content") is True
        and overlay_overrides.get(source["id"], {}).get("requires_exact_content")
        is False
    ]
    incomplete_basic_shapes = [
        source["id"]
        for source in draft["overlays"]
        if source.get("action") == "basic_shape"
        and overlay_overrides.get(source["id"], {}).get("shape") is None
    ]
    if inferred_text:
        lines.append("- 已从 Draft 槽位名称回填默认文字：" + ", ".join(inferred_text))
    if fallback_fonts:
        lines.append(
            "- 未指定字体文件，已批准使用本地通用字体：" + ", ".join(fallback_fonts)
        )
    if approximate_overlays:
        lines.append(
            "- 缺少精确透明素材，已改为近似制作：" + ", ".join(approximate_overlays)
        )
    if incomplete_basic_shapes:
        lines.append(
            "- basic_shape 缺少可执行参数，已改为参考图近似制作："
            + ", ".join(incomplete_basic_shapes)
        )
    if questions_deferred and draft.get("questions"):
        lines.append(
            f"- {len(draft['questions'])} 个 Draft 待确认问题按当前设置处理，未要求逐题填写。"
        )
    if len(lines) == 1:
        lines.append("- 无需额外自动决策。")
    return "\n".join(lines)


def validate_review_decisions(
    draft: dict[str, Any],
    slot_overrides: dict[str, dict[str, Any]],
    overlay_overrides: dict[str, dict[str, Any]],
) -> None:
    """在写中间文件前检查页面可解决的 ReviewedSpec 门禁。"""

    blockers: list[str] = []
    slot_ids = {item["id"] for item in draft["slots"]}
    overlay_ids = {item["id"] for item in draft["overlays"]}
    if unknown := set(slot_overrides) - slot_ids:
        blockers.append(f"未知 slot：{', '.join(sorted(unknown))}")
    if unknown := set(overlay_overrides) - overlay_ids:
        blockers.append(f"未知 overlay：{', '.join(sorted(unknown))}")

    for source in draft["slots"]:
        fields = default_slot_review_fields(source)
        fields.update(slot_overrides.get(source["id"], {}))
        if source["type"] == "image" and source["mode"] == "photo_feather":
            fade = fields.get("edge_fade_px")
            if not (isinstance(fade, int) and fade > 0) and not fields.get("clip_mask"):
                blockers.append(f"{source['id']} 需要正数羽化宽度或 clip mask")
        if (
            source["type"] == "text"
            and not fields.get("font_path")
            and fields.get("fallback_approved") is not True
        ):
            blockers.append(f"{source['id']} 需要字体或明确批准 fallback")

    for source in draft["overlays"]:
        fields = {
            **default_overlay_review_fields(),
            "requires_exact_content": source["requires_exact_content"],
        }
        fields.update(overlay_overrides.get(source["id"], {}))
        if (source.get("text_content") or fields.get("text_content")) and (
            not fields.get("text_content") or fields.get("text_confirmed") is not True
        ):
            blockers.append(f"{source['id']} 的完整文字需要逐字确认")
        if (
            fields.get("requires_exact_content") is True
            and not fields.get("prepared_asset")
            and not (
                fields.get("text_content") and fields.get("text_confirmed") is True
            )
        ):
            blockers.append(f"{source['id']} 的精确内容需要 prepared asset")

    if blockers:
        raise CollageError(
            "HUMAN_REVIEW_REQUIRED",
            "确认页面仍有未完成的制作决策",
            details={"blockers": blockers},
        )


def background_parameter(payload: dict[str, Any], name: str, default: int) -> int:
    """读取确认页中的非负背景像素参数。"""

    value = payload.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 4096:
        raise CollageError("INVALID_REQUEST", f"{name} 必须是 0 到 4096 之间的整数")
    return value
