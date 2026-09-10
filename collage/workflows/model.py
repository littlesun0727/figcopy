"""Define the persistent project workflow contract and stage mappings."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..core.errors import CollageError

WORKFLOW_VERSION = "figcopy-workflow/1"
DEFAULT_VISION_PROVIDER = "collage.providers.yibu:YibuVisionProvider"
DEFAULT_IMAGE_PROVIDER = "collage.providers.yibu:YibuImageProvider"
DEFAULT_CUTOUT_PROVIDER = "collage.providers.birefnet:BiRefNetLiteMattingProvider"

WORKFLOW_STAGES = frozenset(
    {
        "analyzing",
        "awaiting_review",
        "reviewed",
        "building",
        "awaiting_bindings",
        "rendering",
        "awaiting_approval",
        "complete",
        "blocked",
        "failed",
    }
)

ARTIFACT_PATHS = {
    "reference": "inputs/reference.png",
    "manual_draft": "inputs/manual_draft.json",
    "product_policy": "inputs/product_policy.json",
    "background_candidate": "inputs/background_candidate.png",
    "initial_mask": "inputs/initial_remove_mask.png",
    "allowed_mask": "inputs/allowed_mask.png",
    "draft": "analysis/draft.json",
    "draft_preview": "analysis/draft_preview.png",
    "reviewed": "review/reviewed.json",
    "remove_mask": "review/remove_mask.png",
    "template_manifest": "template/template.json",
    "inspection_report": "workspace/inspection.html",
    "upload_guide": "reports/upload_guide.html",
    "bindings_example": "renders/bindings.example.json",
    "bindings": "renders/bindings.json",
    "render": "renders/result.png",
    "render_audit": "renders/result.png.render.json",
}

_PROJECT_STATUS_BY_STAGE = {
    "analyzing": "new",
    "awaiting_review": "draft",
    "reviewed": "reviewed",
    "building": "building",
    "awaiting_bindings": "needs_review",
    "rendering": "building",
    "awaiting_approval": "needs_review",
    "complete": "ready",
    "blocked": "blocked",
    "failed": "failed",
}


def utc_now() -> str:
    """Return an ISO timestamp suitable for persisted workflow events."""

    return datetime.now(UTC).isoformat()


def project_status_for_stage(stage: str) -> str:
    """Map a detailed workflow stage to the coarse project status."""

    try:
        return _PROJECT_STATUS_BY_STAGE[stage]
    except KeyError as exc:
        raise CollageError(
            "INVALID_WORKFLOW_STATE",
            f"未知工作流阶段：{stage}",
        ) from exc


def new_workflow(
    *,
    reviewer: str,
    vision_provider: str | None,
    image_provider: str | None,
    cutout_provider: str | None,
    fixture_provider: bool,
    allow_cloud_upload: bool,
    review_port: int,
) -> dict[str, Any]:
    """Create a serializable workflow state without external absolute paths."""

    timestamp = utc_now()
    workflow = {
        "version": WORKFLOW_VERSION,
        "stage": "analyzing",
        "resume_stage": None,
        "started_at": timestamp,
        "stage_updated_at": timestamp,
        "options": {
            "reviewer": reviewer,
            "vision_provider": vision_provider,
            "image_provider": image_provider,
            "cutout_provider": cutout_provider,
            "fixture_provider": fixture_provider,
            "allow_cloud_upload": allow_cloud_upload,
            "review_port": review_port,
        },
        "artifacts": dict(ARTIFACT_PATHS),
        "wait": None,
        "last_error": None,
        "history": [{"stage": "analyzing", "at": timestamp}],
    }
    return validate_workflow(workflow)


def validate_workflow(value: Any) -> dict[str, Any]:
    """Validate the durable subset needed to resume a project safely."""

    if not isinstance(value, dict) or value.get("version") != WORKFLOW_VERSION:
        raise CollageError(
            "INVALID_WORKFLOW_STATE",
            "project.json 缺少受支持的 workflow 状态",
        )
    stage = value.get("stage")
    if stage not in WORKFLOW_STAGES:
        raise CollageError("INVALID_WORKFLOW_STATE", "workflow.stage 不受支持")
    resume_stage = value.get("resume_stage")
    if resume_stage is not None and resume_stage not in WORKFLOW_STAGES - {
        "blocked",
        "failed",
    }:
        raise CollageError("INVALID_WORKFLOW_STATE", "workflow.resume_stage 不受支持")
    options = value.get("options")
    artifacts = value.get("artifacts")
    if not isinstance(options, dict) or not isinstance(artifacts, dict):
        raise CollageError(
            "INVALID_WORKFLOW_STATE",
            "workflow.options 和 workflow.artifacts 必须是 JSON object",
        )
    reviewer = options.get("reviewer")
    review_port = options.get("review_port")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise CollageError("INVALID_WORKFLOW_STATE", "workflow 缺少 reviewer")
    if isinstance(review_port, bool) or not isinstance(review_port, int):
        raise CollageError("INVALID_WORKFLOW_STATE", "workflow.review_port 必须是整数")
    if not 1 <= review_port <= 65535:
        raise CollageError(
            "INVALID_WORKFLOW_STATE", "workflow.review_port 必须在 1-65535 之间"
        )
    for field in ("vision_provider", "image_provider", "cutout_provider"):
        provider = options.get(field)
        if provider is not None and (
            not isinstance(provider, str) or not provider.strip()
        ):
            raise CollageError("INVALID_WORKFLOW_STATE", f"workflow.{field} 格式不正确")
    for field in ("fixture_provider", "allow_cloud_upload"):
        if not isinstance(options.get(field), bool):
            raise CollageError(
                "INVALID_WORKFLOW_STATE", f"workflow.{field} 必须是 boolean"
            )
    if any(
        artifacts.get(name) != relative_path
        for name, relative_path in ARTIFACT_PATHS.items()
    ):
        raise CollageError(
            "INVALID_WORKFLOW_STATE",
            "workflow.artifacts 与当前项目目录约定不一致",
        )
    return value
