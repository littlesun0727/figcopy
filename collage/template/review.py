"""把已人工修正的 Draft 与删除 mask 固化为不可被分析覆盖的 ReviewedSpec。"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..core.errors import CollageError
from ..core.io import atomic_write_json, read_json, resolve_input_path, sha256_file
from ..imaging.operations import load_mask
from ..schemas import draft_has_release_blockers, validate_draft, validate_reviewed_spec

LOGGER = logging.getLogger(__name__)
EDGE_FADE_RATIO = 0.08
EDGE_FADE_MAX_PX = 128
TEXT_SIZE_RATIO = 0.55
TEXT_SIZE_MIN_PX = 12
TEXT_SIZE_MAX_PX = 256
_QUOTED_TEXT_PATTERN = re.compile(r"[「『“\"]\s*(.+?)\s*[」』”\"]")


def _relative(path: Path, output_file: Path) -> str:
    return Path(
        os.path.relpath(path.resolve(), output_file.parent.resolve())
    ).as_posix()


def validate_override_map(value: object, *, source: str) -> dict[str, dict[str, Any]]:
    """校验来自 JSON 文件或确认页面的 id -> object 覆盖表。"""

    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, dict) for key, item in value.items()
    ):
        raise CollageError("INVALID_OVERRIDE", f"覆盖数据必须是 id -> object：{source}")
    return {key: dict(item) for key, item in value.items()}


def load_override_map(path: Path | None) -> dict[str, dict[str, Any]]:
    """读取可选覆盖文件；未提供文件时返回空表。"""

    if path is None:
        return {}
    return validate_override_map(read_json(path), source=str(path))


def suggested_edge_fade_px(target_rect: list[Any]) -> int:
    """按槽位短边的 8% 生成羽化建议，并限制极端尺寸。"""

    short_side = min(int(target_rect[2]), int(target_rect[3]))
    proportional = max(1, round(short_side * EDGE_FADE_RATIO))
    half_side = max(1, short_side // 2)
    return min(proportional, half_side, EDGE_FADE_MAX_PX)


def suggested_font_size_px(target_rect: list[Any]) -> int:
    """按文字框高度生成可编辑的初始字号。"""

    suggested = round(int(target_rect[3]) * TEXT_SIZE_RATIO)
    return max(TEXT_SIZE_MIN_PX, min(suggested, TEXT_SIZE_MAX_PX))


def infer_default_text(source: dict[str, Any]) -> str | None:
    """从 Draft 已有字段回填可编辑文字，不再次识图或猜测内容。"""

    current = source.get("default_text")
    if isinstance(current, str):
        return current
    label = source.get("label")
    if not isinstance(label, str):
        return None
    match = _QUOTED_TEXT_PATTERN.search(label)
    return match.group(1).strip() if match and match.group(1).strip() else None


def default_slot_review_fields(source: dict[str, Any]) -> dict[str, Any]:
    """生成 Draft 槽位进入人工确认时的尺寸自适应初值。"""

    if source["type"] == "image":
        return {
            "required": True,
            "rotation_deg": 0,
            "fit": "contain" if source["mode"] == "cutout" else "cover",
            "anchor": [0.5, 0.5],
            "clip_mask": None,
            "edge_fade_px": suggested_edge_fade_px(source["target_rect"])
            if source["mode"] == "photo_feather"
            else 0,
        }
    return {
        "required": True,
        "rotation_deg": 0,
        "default_text": infer_default_text(source),
        "font_path": None,
        "font_size": suggested_font_size_px(source["target_rect"]),
        # 确认页默认使用 Pillow 的本地通用字体，避免要求用户寻找字体文件。
        "fallback_approved": True,
        "color": "#000000",
        "align": "left",
        "max_lines": 3,
        "line_spacing": 4,
    }


def default_overlay_review_fields() -> dict[str, Any]:
    """生成固定装饰进入人工确认时的非语义默认字段。"""

    return {
        "rotation_deg": 0,
        "prepared_asset": None,
        "background_mode": "alpha",
        "chroma_key": None,
        "chroma_tolerance": 40,
        "shape": None,
    }


def _merge_override_maps(
    path: Path | None,
    inline: dict[str, dict[str, Any]] | None,
    *,
    source: str,
) -> dict[str, dict[str, Any]]:
    """合并文件与页面覆盖值；页面中的显式选择优先。"""

    merged = load_override_map(path)
    if inline is None:
        return merged
    for item_id, fields in validate_override_map(inline, source=source).items():
        merged.setdefault(item_id, {}).update(fields)
    return merged


def confirm_draft(
    draft_path: Path,
    output_path: Path,
    *,
    remove_mask_path: Path,
    reviewer: str,
    allowed_mask_path: Path | None = None,
    background_candidate_path: Path | None = None,
    slot_overrides_path: Path | None = None,
    overlay_overrides_path: Path | None = None,
    slot_overrides_data: dict[str, dict[str, Any]] | None = None,
    overlay_overrides_data: dict[str, dict[str, Any]] | None = None,
    background_expand_px: int = 0,
    background_feather_px: int = 0,
    notes: str = "",
    force: bool = False,
) -> Path:
    """确认 Draft；unknown 或未清空的 questions 会阻止生成 reviewed.json。"""

    LOGGER.info("开始固化人工确认稿 | draft=%s", draft_path)
    if output_path.exists() and not force:
        raise CollageError(
            "OUTPUT_EXISTS", f"确认稿已存在：{output_path}；如需覆盖请显式使用 --force"
        )
    draft = validate_draft(read_json(draft_path))
    blockers = draft_has_release_blockers(draft)
    if blockers:
        LOGGER.warning("人工确认尚未完成 | blockers=%s", len(blockers))
        raise CollageError(
            "HUMAN_REVIEW_REQUIRED", "草稿仍有待确认项", details={"blockers": blockers}
        )
    source_path = resolve_input_path(draft_path, draft["source"]["path"])
    if sha256_file(source_path) != draft["source"]["sha256"]:
        raise CollageError("SOURCE_HASH_MISMATCH", "规范化参考图与 Draft 哈希不一致")
    canvas_size = (draft["canvas"]["width"], draft["canvas"]["height"])
    load_mask(remove_mask_path, canvas_size, name="remove_mask")
    if allowed_mask_path is not None:
        load_mask(allowed_mask_path, canvas_size, name="allowed_mask")
    slot_overrides = _merge_override_maps(
        slot_overrides_path, slot_overrides_data, source="确认页面 slot 覆盖"
    )
    overlay_overrides = _merge_override_maps(
        overlay_overrides_path, overlay_overrides_data, source="确认页面 overlay 覆盖"
    )
    slot_ids = {item["id"] for item in draft["slots"]}
    overlay_ids = {item["id"] for item in draft["overlays"]}
    if unknown := set(slot_overrides) - slot_ids:
        raise CollageError(
            "UNKNOWN_OVERRIDE_ID", f"slot 覆盖包含未知 ID：{', '.join(sorted(unknown))}"
        )
    if unknown := set(overlay_overrides) - overlay_ids:
        raise CollageError(
            "UNKNOWN_OVERRIDE_ID",
            f"overlay 覆盖包含未知 ID：{', '.join(sorted(unknown))}",
        )

    slots: list[dict[str, Any]] = []
    for source in draft["slots"]:
        slot = {**source, **default_slot_review_fields(source)}
        if source["type"] == "image":
            slot.pop("default_text", None)
        slot.update(slot_overrides.get(source["id"], {}))
        if source["mode"] == "photo_feather" and not any(
            field in slot_overrides.get(source["id"], {})
            for field in ("edge_fade_px", "clip_mask")
        ):
            LOGGER.info(
                "应用尺寸自适应槽位羽化 | slot=%s edge_fade_px=%s",
                source["id"],
                slot["edge_fade_px"],
            )
        slots.append(slot)

    overlays: list[dict[str, Any]] = []
    for source in draft["overlays"]:
        overlay = {**source, **default_overlay_review_fields()}
        overlay.update(overlay_overrides.get(source["id"], {}))
        overlays.append(overlay)

    reviewed = {
        "version": "collage-reviewed/1",
        "status": "reviewed",
        "reference": {
            "path": _relative(source_path, output_path),
            "sha256": draft["source"]["sha256"],
        },
        "canvas": draft["canvas"],
        "slots": slots,
        "overlays": overlays,
        "background": {
            **draft["background"],
            "remove_mask": _relative(remove_mask_path, output_path),
            "allowed_mask": _relative(allowed_mask_path, output_path)
            if allowed_mask_path
            else None,
            "candidate_path": _relative(background_candidate_path, output_path)
            if background_candidate_path
            else None,
            "expand_px": background_expand_px,
            "feather_px": background_feather_px,
        },
        "layer_order": draft["layer_order"],
        "review": {
            "reviewer": reviewer,
            "reviewed_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "notes": notes,
            "questions_resolved": True,
        },
        "audit": {"source_draft_sha256": sha256_file(draft_path)},
    }
    validate_reviewed_spec(reviewed)
    atomic_write_json(output_path, reviewed)
    LOGGER.info(
        "ReviewedSpec 已保存 | output=%s reviewer=%s", output_path.resolve(), reviewer
    )
    return output_path
