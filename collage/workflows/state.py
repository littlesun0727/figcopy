"""Persist workflow transitions, failures, and frontend-friendly summaries."""

from __future__ import annotations

import logging
import re
from typing import Any

from ..core.errors import CollageError
from ..core.privacy import safe_value
from ..projects import ProjectPaths, ProjectStore
from ..template.validation import validate_package
from .model import project_status_for_stage, utc_now, validate_workflow

LOGGER = logging.getLogger(__name__)

_WINDOWS_ABSOLUTE_PATH = re.compile(r"(?i)\b[A-Z]:[\\/][^\r\n，。；]*")
_POSIX_ABSOLUTE_PATH = re.compile(r"(?<![:\w])/(?:[^ \t\r\n，。；]+)")

_BLOCKING_ERROR_CODES = {
    "CUSTOMER_UPLOAD_NOT_AUTHORIZED",
    "CUTOUT_PROVIDER_UNAVAILABLE",
    "EXACT_CONTENT_REQUIRED",
    "FIXTURE_APPROVAL_BLOCKED",
    "IMAGE_PROVIDER_CAPABILITY_MISSING",
    "IMAGE_PROVIDER_UNAVAILABLE",
    "VISION_PROVIDER_UNAVAILABLE",
}


def _is_blocking_error(code: str) -> bool:
    return code in _BLOCKING_ERROR_CODES or code.startswith(
        ("BIREFNET_", "PROVIDER_", "YIBU_")
    )


def _safe_error_message(message: str, project: ProjectPaths) -> str:
    """Remove absolute filesystem locations before writing project.json."""

    sanitized = message.replace(str(project.root), "<project>")
    sanitized = _WINDOWS_ABSOLUTE_PATH.sub("<path>", sanitized)
    return _POSIX_ABSOLUTE_PATH.sub("<path>", sanitized)


def transition(
    store: ProjectStore,
    project: ProjectPaths,
    workflow: dict[str, Any],
    stage: str,
    *,
    wait: dict[str, Any] | None = None,
) -> None:
    """Persist one successful or waiting stage transition."""

    previous = workflow["stage"]
    timestamp = utc_now()
    workflow["stage"] = stage
    workflow["resume_stage"] = None
    workflow["stage_updated_at"] = timestamp
    workflow["wait"] = wait
    workflow["last_error"] = None
    if previous != stage:
        workflow.setdefault("history", []).append({"stage": stage, "at": timestamp})
    validate_workflow(workflow)
    store.update_workflow(
        project.project_id,
        workflow,
        status=project_status_for_stage(stage),
    )
    LOGGER.info(
        "项目工作流阶段更新 | project=%s stage=%s",
        project.project_id,
        stage,
    )


def infer_resume_stage(project: ProjectPaths) -> str:
    """Infer the safest resumable stage from durable artifacts."""

    manifest_path = project.template / "template.json"
    if manifest_path.is_file():
        template = validate_package(project.template, require_ready=False)
        if template["status"] == "ready":
            return "complete"
        if (project.renders / "result.png").is_file():
            return "awaiting_approval"
        return "awaiting_bindings"
    if (project.review / "reviewed.json").is_file():
        return "reviewed"
    if (project.analysis / "draft.json").is_file():
        return "awaiting_review"
    return "analyzing"


def record_failure(
    store: ProjectStore,
    project: ProjectPaths,
    workflow: dict[str, Any],
    error: CollageError,
) -> None:
    """Record a safe error and the stage from which resume should continue."""

    current = workflow.get("stage", "analyzing")
    if current in {"blocked", "failed"}:
        current = workflow.get("resume_stage") or infer_resume_stage(project)
    failure_stage = "blocked" if _is_blocking_error(error.code) else "failed"
    timestamp = utc_now()
    workflow["stage"] = failure_stage
    workflow["resume_stage"] = current
    workflow["stage_updated_at"] = timestamp
    workflow["wait"] = None
    # Preserve every field issue and transport detail after sanitizing their values.
    workflow["last_error"] = safe_value(error.as_dict())
    workflow["last_error"]["message"] = _safe_error_message(
        workflow["last_error"]["message"], project
    )
    workflow.setdefault("history", []).append(
        {"stage": failure_stage, "at": timestamp, "code": error.code}
    )
    store.update_workflow(
        project.project_id,
        workflow,
        status=project_status_for_stage(failure_stage),
    )


def summary(
    project: ProjectPaths,
    manifest: dict[str, Any],
    workflow: dict[str, Any],
) -> dict[str, Any]:
    """Build a status response for the CLI and future local frontend."""

    stage = workflow["stage"]
    next_actions = {
        "analyzing": "继续分析参考图",
        "awaiting_review": "运行 resume 打开审核页并保存确认结果",
        "reviewed": "运行 resume 构建模板",
        "building": "运行 resume 继续构建模板",
        "awaiting_bindings": "编辑 renders/bindings.json，或用 resume --bindings 导入",
        "rendering": "运行 resume 继续生成预览",
        "awaiting_approval": "检查 renders/result.png 后运行 resume --approve",
        "complete": "模板已发布，可以继续用本地 Renderer 生成结果",
        "blocked": "修复 last_error 后运行 resume",
        "failed": "检查 last_error 和项目文件后运行 resume",
    }
    artifacts = {
        name: {
            "path": str(project.root / relative),
            "exists": (project.root / relative).is_file(),
        }
        for name, relative in workflow["artifacts"].items()
    }
    return {
        "project_id": project.project_id,
        "name": manifest["name"],
        "status": manifest["status"],
        "stage": stage,
        "root": str(project.root),
        "next_action": next_actions[stage],
        "wait": workflow.get("wait"),
        "last_error": workflow.get("last_error"),
        "artifacts": artifacts,
    }


def current_summary(
    store: ProjectStore,
    project: ProjectPaths,
    workflow: dict[str, Any],
) -> dict[str, Any]:
    """Read the current manifest before building a status summary."""

    return summary(project, store.get_manifest(project.project_id), workflow)
