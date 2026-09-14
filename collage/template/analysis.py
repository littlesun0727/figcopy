"""规范化参考图，并通过人工草稿或真实 VLM provider 生成可修正 Draft。"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import ImageDraw

from ..core.errors import CollageError
from ..core.diagnostics import analysis_attempt, analysis_phase, capture_evidence
from ..core.io import (
    atomic_save_image,
    atomic_write_bytes,
    atomic_write_json,
    read_json,
    sha256_file,
    stable_hash,
)
from ..imaging.operations import normalize_image, rect_to_box
from ..providers.selection import cache_configuration
from ..providers import ProviderAudit, VisionProvider
from ..schemas import validate_draft
from ..schemas.draft_prompt import DRAFT_PROMPT

LOGGER = logging.getLogger(__name__)
PROMPT_VERSION = "collage-draft/8"
ANALYSIS_PROMPT = DRAFT_PROMPT
DEFAULT_PRODUCT_POLICY: dict[str, Any] = {
    "goal": "fixed_layout_customer_content_replacement",
    "image_modes": ["photo", "photo_feather", "cutout", "unknown"],
    "overlay_actions": ["basic_shape", "reference_generate"],
    "allow_approximate_fixed_overlays": True,
    "exact_content_requires_original_asset": True,
}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _draw_draft_preview(
    reference_path: Path, draft: dict[str, Any], output_path: Path
) -> None:
    image = normalize_image(reference_path).convert("RGBA")
    draw = ImageDraw.Draw(image)
    colors = {"slot": "#00D4FF", "overlay": "#FF42C8"}
    for kind, items in (("slot", draft["slots"]), ("overlay", draft["overlays"])):
        for item in items:
            box = rect_to_box(item["target_rect"])
            draw.rectangle(
                box, outline=colors[kind], width=max(2, min(image.size) // 300)
            )
            draw.text(
                (box[0] + 3, box[1] + 3), f"{kind}:{item['id']}", fill=colors[kind]
            )
    atomic_save_image(image, output_path)


def analyze_reference(
    reference_path: Path,
    output_dir: Path,
    *,
    manual_draft_path: Path | None = None,
    provider: VisionProvider | None = None,
    product_policy: dict[str, Any] | None = None,
    force: bool = False,
) -> Path:
    """生成 work/draft.json；没有 provider 时只接受显式人工草稿。"""

    output_dir = output_dir.resolve()
    draft_path = output_dir / "draft.json"
    reviewed_path = output_dir / "reviewed.json"
    if reviewed_path.exists():
        raise CollageError(
            "REVIEWED_SPEC_PROTECTED",
            "已有 reviewed.json，不允许分析结果无声覆盖确认稿",
        )
    if draft_path.exists() and not force:
        raise CollageError(
            "OUTPUT_EXISTS", f"Draft 已存在：{draft_path}；如需重做请显式使用 --force"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    settings = getattr(provider, "settings", None)
    with analysis_attempt(
        output_dir,
        {
            "provider": getattr(provider, "name", "manual-import"),
            "model": getattr(provider, "requested_model", None),
            "fixture": bool(getattr(provider, "fixture", False)),
            "prompt_version": PROMPT_VERSION,
            "max_tokens": getattr(settings, "vlm_max_tokens", None),
            "reasoning_effort": getattr(settings, "vlm_reasoning_effort", None),
        },
        secrets=(getattr(settings, "api_key", ""),),
    ):
        return _analyze_reference(
            reference_path,
            output_dir,
            manual_draft_path=manual_draft_path,
            provider=provider,
            product_policy=product_policy,
        )


def _analyze_reference(
    reference_path, output_dir, *, manual_draft_path, provider, product_policy
):
    draft_path = output_dir / "draft.json"
    capture_evidence("prompt.txt", ANALYSIS_PROMPT)
    capture_evidence("policy.json", product_policy or DEFAULT_PRODUCT_POLICY)
    atomic_write_json(
        output_dir / "analysis_policy.json", product_policy or DEFAULT_PRODUCT_POLICY
    )
    normalized_path = output_dir / "reference.png"
    reference = normalize_image(reference_path, normalized_path)
    canvas = {
        "width": reference.width,
        "height": reference.height,
        "coordinate_space": "canvas_px",
        "rect_format": "xywh",
    }
    capture_evidence(
        "input.json", {"canvas": canvas, "source_sha256": sha256_file(normalized_path)}
    )

    if manual_draft_path is not None:
        LOGGER.info("导入人工草稿 | file=%s", manual_draft_path.name)
        capture_evidence(
            "response.txt", manual_draft_path.read_text(encoding="utf-8-sig")
        )
        raw = read_json(manual_draft_path)
        capture_evidence("candidate.json", raw)
        analysis_phase("validating_candidate")
        raw = validate_draft(raw, require_metadata=False)
        audit = ProviderAudit("manual-import", None, None, None, False, 0)
    elif provider is not None:
        policy = product_policy or DEFAULT_PRODUCT_POLICY
        cache_key = stable_hash(
            {
                "source_sha256": sha256_file(normalized_path),
                "prompt_version": PROMPT_VERSION,
                "product_policy": policy,
                "provider": provider.name,
                **cache_configuration(provider),
                "requested_model": provider.requested_model,
            }
        )
        cache_path = output_dir / "analysis_cache" / f"{cache_key}.json"
        cached: dict[str, Any] | None = None
        if cache_path.is_file():
            try:
                cached = read_json(cache_path)
                raw = validate_draft(cached["draft"], require_metadata=False)
                cached_audit = cached["audit"]
                audit = ProviderAudit(
                    cached_audit["name"],
                    cached_audit.get("requested_model"),
                    cached_audit.get("actual_model"),
                    cached_audit.get("request_id"),
                    bool(cached_audit.get("fixture", False)),
                    int(cached_audit.get("elapsed_ms", 0)),
                    cache_hit=True,
                )
                LOGGER.info("VLM Draft 缓存命中 | key=%s", cache_key[:12])
                capture_evidence("candidate.json", raw)
                capture_evidence("audit.json", audit.as_dict())
            except (CollageError, KeyError, TypeError, ValueError):
                cached = None
        if cached is None:
            LOGGER.info(
                "调用 VLM 生成候选草稿 | provider=%s model=%s",
                provider.name,
                provider.requested_model,
            )
            started = time.monotonic()
            analysis_phase("requesting_model")
            raw, audit = provider.analyze(
                normalized_path.read_bytes(),
                media_type="image/png",
                canvas=canvas,
                product_policy=policy,
                prompt=ANALYSIS_PROMPT,
            )
            if audit.elapsed_ms < 0:
                audit = ProviderAudit(
                    audit.name,
                    audit.requested_model,
                    audit.actual_model,
                    audit.request_id,
                    audit.fixture,
                    round((time.monotonic() - started) * 1000),
                )
            capture_evidence("candidate.json", raw)
            capture_evidence("audit.json", audit.as_dict())
            analysis_phase("validating_candidate")
            LOGGER.info("校验模型候选草稿")
            raw = validate_draft(raw, require_metadata=False)
    else:
        raise CollageError(
            "VISION_PROVIDER_UNAVAILABLE",
            "未配置 VLM；请使用 --manual-draft 导入人工草稿，或用 --provider 指定已实现的 provider",
        )

    # 应用元数据由程序生成，拒绝沿用 provider 伪造的路径、状态或尺寸。
    core_fields = {
        key: raw[key]
        for key in ("slots", "overlays", "background", "layer_order", "questions")
    }
    draft = {
        "version": "collage-draft/3",
        "status": "draft",
        "source": {
            "path": "reference.png",
            "sha256": sha256_file(normalized_path),
            "width": reference.width,
            "height": reference.height,
        },
        "canvas": canvas,
        "prompt_version": PROMPT_VERSION,
        "provider": audit.as_dict(),
        "created_at": _utc_now(),
        **core_fields,
    }
    capture_evidence("assembled_draft.json", draft)
    analysis_phase("validating_draft")
    validate_draft(draft)
    # Cache only after canvas-aware validation also succeeds.
    if provider is not None and manual_draft_path is None and cached is None:
        atomic_write_json(cache_path, {"draft": raw, "audit": audit.as_dict()})
    analysis_phase("saving_draft")
    atomic_write_json(draft_path, draft)
    atomic_write_bytes(
        output_dir / "analysis_prompt.txt", (ANALYSIS_PROMPT + "\n").encode("utf-8")
    )
    _draw_draft_preview(normalized_path, draft, output_dir / "draft_preview.png")
    LOGGER.info("候选草稿已生成 | file=%s", draft_path.name)
    return draft_path
