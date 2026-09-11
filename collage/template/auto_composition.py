"""Inspect customer focal regions and search bounded local crops against actual slot visibility."""

from __future__ import annotations

from pathlib import Path
import os

from PIL import Image, ImageStat

from ..core.errors import CollageError
from ..core.io import atomic_write_json
from ..imaging.operations import normalize_image
from ..rendering.model import PreparedBinding
from .auto_geometry import normalized_rect


def inspect_materials(root: Path) -> dict:
    """Detect faces entirely locally; replacement photographs never enter a remote request."""
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise CollageError(
            "AUTO_LOCAL_FACE_UNAVAILABLE", "本地人像定位需要安装 auto-trial 可选依赖"
        ) from exc

    records = []
    from ..projects.paths import resolve_data_root

    weights = Path(
        os.environ.get(
            "FIGCOPY_FACE_MODEL",
            str(
                resolve_data_root()
                / "cache/face_detection/face_detection_yunet_2026may.onnx"
            ),
        )
    )
    if not weights.is_file() or not hasattr(cv2, "FaceDetectorYN"):
        raise CollageError(
            "AUTO_LOCAL_FACE_UNAVAILABLE", "本地 YuNet 人脸检测权重或接口不可用"
        )
    detector = cv2.FaceDetectorYN.create(str(weights), "", (320, 320), 0.8, 0.3, 5000)
    for path in sorted((root / "materials").glob("*.png")):
        image = normalize_image(path).convert("RGB")
        factor = min(1.0, 960 / max(image.size))
        resized = image.resize(
            (round(image.width * factor), round(image.height * factor)),
            Image.Resampling.LANCZOS,
        )
        bgr = cv2.cvtColor(np.asarray(resized), cv2.COLOR_RGB2BGR)
        detector.setInputSize(resized.size)
        _, detections = detector.detect(bgr)
        if detections is None or not len(detections):
            raise CollageError("AUTO_SUBJECT_NOT_FOUND", "本地检测未找到可用人脸")
        choices = sorted(
            ((face[:4], face[-1]) for face in detections),
            key=lambda item: float(item[1]) * float(item[0][2] * item[0][3]),
            reverse=True,
        )
        (x, y, w, h), strength = choices[0]
        if len(choices) > 1 and choices[1][0][2] * choices[1][0][3] > w * h * 0.65:
            raise CollageError(
                "AUTO_SUBJECT_AMBIGUOUS", "检测到多个相近大小的人脸，主体不明确"
            )
        x, y, w, h = (float(v) / factor for v in (x, y, w, h))
        left, top = max(0, x - w * 0.35), max(0, y - h * 0.65)
        right, bottom = min(image.width, x + w * 1.35), min(image.height, y + h * 1.20)
        records.append(
            {
                "id": path.stem,
                "face_box": [
                    x / image.width,
                    y / image.height,
                    w / image.width,
                    h / image.height,
                ],
                "head_box": [
                    left / image.width,
                    top / image.height,
                    (right - left) / image.width,
                    (bottom - top) / image.height,
                ],
                "subject_box": None,
                "detector_strength": float(strength),
                "head_box_source": "conservative expansion of local face detector; not semantic hair segmentation",
                "network_calls": 0,
            }
        )
    from ..core.io import sha256_file

    result = {
        "version": "local-customer-focus/1",
        "provider": "opencv-yunet-2026may",
        "opencv_version": cv2.__version__,
        "weights_sha256": sha256_file(weights),
        "network_calls": 0,
        "materials": records,
    }
    atomic_write_json(root / "material_focus.json", result)
    return result


def _visible_fraction(
    mask: Image.Image, box: tuple[float, float, float, float]
) -> float:
    left, top, right, bottom = (round(v) for v in box)
    if right <= left or bottom <= top:
        return 0.0
    return ImageStat.Stat(mask.crop((left, top, right, bottom))).mean[0] / 255


def fit_visible_head(
    source: Image.Image, slot: dict, focus: dict, visibility: Image.Image
) -> tuple[PreparedBinding, dict]:
    """Search cover-preserving offsets; keep head visibility ahead of preferred centering and zoom."""
    x, y, w, h = (round(v) for v in slot["rect"])
    hx, hy, hw, hh = normalized_rect(focus["head_box"], source.size)
    base = max(w / source.width, h / source.height)
    mini_scale = min(1, 400 / max(visibility.size))
    mini = visibility.resize(
        (round(visibility.width * mini_scale), round(visibility.height * mini_scale)),
        Image.Resampling.BOX,
    )
    best = None
    for zoom in (1.0, 1.12, 1.25, 1.4, 1.6, 1.85, 2.1):
        rw, rh = round(source.width * base * zoom), round(source.height * base * zoom)
        sx, sy = rw / source.width, rh / source.height
        excess_x, excess_y = rw - w, rh - h
        for gx in range(9):
            local_x = round(-excess_x * gx / 8)
            for gy in range(9):
                local_y = round(-excess_y * gy / 8)
                head = (
                    x + local_x + hx * sx,
                    y + local_y + hy * sy,
                    x + local_x + (hx + hw) * sx,
                    y + local_y + (hy + hh) * sy,
                )
                fraction = _visible_fraction(mini, tuple(v * mini_scale for v in head))
                centered_penalty = abs(gx - 4) + abs(gy - 4)
                rank = (
                    fraction >= 0.995,
                    round(fraction, 4) if fraction < 0.995 else 1,
                    -zoom,
                    -centered_penalty,
                )
                if best is None or rank > best[0]:
                    best = (rank, zoom, local_x, local_y, rw, rh, head)
    assert best is not None
    _, zoom, local_x, local_y, rw, rh, head = best
    # These offsets remain inside the full-cover range; moving a photo cannot expose the old background.
    offset = (
        local_x - round((w - rw) * slot["anchor"][0]),
        local_y - round((h - rh) * slot["anchor"][1]),
    )
    fraction = _visible_fraction(visibility, head)
    binding = PreparedBinding(image=source, scale=zoom, offset_px=offset)
    info = {
        "version": "visible-head-cover-search/1",
        "zoom": zoom,
        "offset_px": list(offset),
        "head_box_canvas": list(head),
        "head_visible_fraction": fraction,
        "cover_complete": local_x <= 0
        and local_y <= 0
        and local_x + rw >= w
        and local_y + rh >= h,
        "resample_scale": base * zoom,
        "measurement_source": "local_face_expansion_and_measured_slot_visibility",
        "production_threshold_calibrated": False,
    }
    return binding, info


def material_groups(structure: dict, focus: dict) -> list[dict]:
    """Use three distinct combinations from the available pool without claiming aspect-ratio diversity."""
    records = sorted(
        focus["materials"], key=lambda r: (r["head_box"][2] * r["head_box"][3], r["id"])
    )
    main = next(s["id"] for s in structure["slots"] if s["role"] == "main")
    insets = [s["id"] for s in structure["slots"] if s["role"] != "main"]
    if len(records) < len(insets) + 1:
        raise CollageError(
            "AUTO_MATERIALS_MISSING", "素材数量不足以给同一组合中的照片槽分配不同照片"
        )
    groups = []
    for index in range(3):
        main_id = records[index % len(records)]["id"]
        pool = [r["id"] for r in records if r["id"] != main_id]
        pool = pool[index % len(pool) :] + pool[: index % len(pool)]
        groups.append(
            {
                "id": f"G{index + 1}",
                "bindings": {main: main_id, **dict(zip(insets, pool))},
            }
        )
    return groups
