"""校验模板包的 schema、路径、图片、透明通道、图层与发布状态。"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from ..core.errors import CollageError, SpecValidationError, ValidationIssue
from ..core.io import decode_image, read_json, safe_package_path, sha256_file
from ..imaging.operations import alpha_is_meaningful, parse_color, rect_to_box
from ..schemas import validate_template_spec

LOGGER = logging.getLogger(__name__)
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_SENSITIVE_KEYS = {"api_key", "apikey", "secret", "token", "authorization", "password"}


def _scan_sensitive(value: Any, path: str, issues: list[ValidationIssue]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key.lower() in _SENSITIVE_KEYS:
                issues.append(
                    ValidationIssue(
                        child_path, "模板包禁止保存凭据字段", "SENSITIVE_DATA"
                    )
                )
            _scan_sensitive(child, child_path, issues)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_sensitive(child, f"{path}[{index}]", issues)


def _check_relative_path(
    root: Path, raw: Any, path: str, issues: list[ValidationIssue]
) -> Path | None:
    if not isinstance(raw, str):
        return None
    if _WINDOWS_ABSOLUTE.match(raw) or raw.startswith(("/", "\\\\", "file://")):
        issues.append(
            ValidationIssue(path, "模板包内禁止绝对路径", "UNSAFE_PACKAGE_PATH")
        )
        return None
    try:
        return safe_package_path(root, raw)
    except CollageError as exc:
        issues.append(ValidationIssue(path, exc.message, exc.code))
        return None


def validate_package(
    template_dir: Path, *, require_ready: bool = True
) -> dict[str, Any]:
    """完整验证模板目录，成功时返回已解析的 TemplateSpec。"""

    root = template_dir.resolve()
    manifest_path = root / "template.json"
    LOGGER.info("校验模板包 | path=%s require_ready=%s", root, require_ready)
    spec = validate_template_spec(read_json(manifest_path), require_ready=require_ready)
    issues: list[ValidationIssue] = []
    _scan_sensitive(spec, "$", issues)
    canvas_size = (spec["canvas"]["width"], spec["canvas"]["height"])
    assets_by_id = {asset["id"]: asset for asset in spec["assets"]}
    background_count = sum(asset["role"] == "background" for asset in spec["assets"])
    if background_count != 1:
        issues.append(
            ValidationIssue(
                "$.assets",
                "模板包必须且只能有一个 background asset",
                "INVALID_BACKGROUND_COUNT",
            )
        )

    for index, asset in enumerate(spec["assets"]):
        field_path = f"$.assets[{index}].path"
        asset_path = _check_relative_path(root, asset["path"], field_path, issues)
        if asset_path is None:
            continue
        if asset_path.suffix.lower() != ".png":
            issues.append(
                ValidationIssue(
                    field_path, "模板图片素材必须是 PNG", "INVALID_ASSET_FORMAT"
                )
            )
        try:
            image = decode_image(asset_path)
        except CollageError as exc:
            issues.append(ValidationIssue(field_path, exc.message, exc.code))
            continue
        if sha256_file(asset_path) != asset["sha256"]:
            issues.append(
                ValidationIssue(
                    field_path, "素材哈希与清单不一致", "ASSET_HASH_MISMATCH"
                )
            )
        if asset["role"] == "background" and image.size != canvas_size:
            issues.append(
                ValidationIssue(
                    field_path,
                    "背景素材必须与画布尺寸完全一致",
                    "BACKGROUND_SIZE_MISMATCH",
                )
            )
        if asset["requires_alpha"] and not alpha_is_meaningful(image.convert("RGBA")):
            issues.append(
                ValidationIssue(
                    field_path, "素材虽可解码但没有真实透明像素", "OPAQUE_OVERLAY"
                )
            )

    for index, slot in enumerate(spec["slots"]):
        width = rect_to_box(slot["rect"])[2] - rect_to_box(slot["rect"])[0]
        height = rect_to_box(slot["rect"])[3] - rect_to_box(slot["rect"])[1]
        if slot["type"] == "image" and slot["clip_mask"] is not None:
            mask_path = _check_relative_path(
                root, slot["clip_mask"], f"$.slots[{index}].clip_mask", issues
            )
            if mask_path is not None:
                try:
                    mask = decode_image(mask_path, mode="L")
                    if mask.size != (width, height):
                        issues.append(
                            ValidationIssue(
                                f"$.slots[{index}].clip_mask",
                                "slot clip mask 必须使用槽位局部尺寸",
                                "MASK_SIZE_MISMATCH",
                            )
                        )
                except CollageError as exc:
                    issues.append(
                        ValidationIssue(
                            f"$.slots[{index}].clip_mask", exc.message, exc.code
                        )
                    )
        if slot["type"] == "text" and slot["font_path"] is not None:
            font_path = _check_relative_path(
                root, slot["font_path"], f"$.slots[{index}].font_path", issues
            )
            if font_path is not None and not font_path.is_file():
                issues.append(
                    ValidationIssue(
                        f"$.slots[{index}].font_path",
                        "字体文件不存在",
                        "FILE_NOT_FOUND",
                    )
                )
            elif font_path is not None:
                try:
                    from PIL import ImageFont

                    ImageFont.truetype(str(font_path), slot["font_size"])
                except OSError:
                    issues.append(
                        ValidationIssue(
                            f"$.slots[{index}].font_path",
                            "字体文件无法加载",
                            "FONT_LOAD_FAILED",
                        )
                    )
        if slot["type"] == "text":
            try:
                parse_color(slot["color"])
            except CollageError as exc:
                issues.append(
                    ValidationIssue(f"$.slots[{index}].color", exc.message, exc.code)
                )

    first_layer = spec["layers"][0] if spec["layers"] else None
    if first_layer and first_layer.get("type") == "asset":
        first_asset = assets_by_id.get(first_layer.get("asset_id"))
        if first_asset and first_asset["role"] == "background":
            expected_rect = [0, 0, canvas_size[0], canvas_size[1]]
            if first_layer["rect"] != expected_rect or first_layer["rotation_deg"] != 0:
                issues.append(
                    ValidationIssue(
                        "$.layers[0]",
                        "背景层必须无旋转并覆盖完整画布",
                        "INVALID_BACKGROUND_LAYER",
                    )
                )

    if require_ready or spec["status"] == "ready":
        preview_path = root / "preview.png"
        try:
            preview = decode_image(preview_path)
            if preview.size != canvas_size:
                issues.append(
                    ValidationIssue(
                        "preview.png",
                        "发布预览尺寸与画布不一致",
                        "PREVIEW_SIZE_MISMATCH",
                    )
                )
        except CollageError as exc:
            issues.append(ValidationIssue("preview.png", exc.message, exc.code))

    if issues:
        raise SpecValidationError(issues, "模板包文件校验失败")
    LOGGER.info(
        "模板包校验通过 | assets=%s slots=%s layers=%s",
        len(spec["assets"]),
        len(spec["slots"]),
        len(spec["layers"]),
    )
    return spec
