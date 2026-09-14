"""Run slow workflow operations in per-project background jobs."""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from pathlib import Path

from ...core.errors import CollageError, SpecValidationError
from ...core.privacy import safe_value
from .job_logs import JobLogStore

LOGGER = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class JobRegistry:
    """Keep transient job progress while durable stage state stays in project.json."""

    def __init__(self, logs_dir: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._latest_by_project: dict[str, str] = {}
        self._inline_projects: set[str] = set()
        self.logs = JobLogStore(logs_dir)

    def submit(
        self,
        project_id: str,
        kind: str,
        operation: Callable[[], Any],
    ) -> dict[str, Any]:
        """Start an operation unless the same project already has an active job."""

        with self._lock:
            previous_id = self._latest_by_project.get(project_id)
            previous = self._jobs.get(previous_id or "")
            if project_id in self._inline_projects or (
                previous is not None and previous["state"] in {"queued", "running"}
            ):
                raise CollageError(
                    "PROJECT_BUSY",
                    "该项目已有任务正在执行，请等待完成后再操作",
                    details={"job_id": previous["id"]}
                    if previous
                    else {"project_id": project_id},
                )
            now = _utc_now()
            job_id = uuid.uuid4().hex
            record: dict[str, Any] = {
                "id": job_id,
                "project_id": project_id,
                "kind": kind,
                "state": "queued",
                "created_at": now,
                "updated_at": now,
                "error": None,
            }
            self._jobs[job_id] = record
            self._latest_by_project[project_id] = job_id
            self.logs.update(record)

        thread = threading.Thread(
            target=self._run,
            args=(job_id, operation),
            name=f"figcopy-{kind}-{project_id}",
            daemon=True,
        )
        thread.start()
        return self._public(record)

    def latest(self, project_id: str) -> dict[str, Any] | None:
        """Return the latest job snapshot for a project."""

        with self._lock:
            job_id = self._latest_by_project.get(project_id)
            record = self._jobs.get(job_id or "")
            if record is not None:
                return self._public(record)
        history = self.logs.history(project_id)
        return self._public(history[0]) if history else None

    def active_project_ids(self) -> set[str]:
        """Return project identifiers that currently have queued or running work."""

        with self._lock:
            return self._inline_projects | {
                record["project_id"]
                for record in self._jobs.values()
                if record["state"] in {"queued", "running"}
            }

    def run_exclusive(self, project_id: str, operation: Callable[[], Any]) -> Any:
        """Reserve a project for a short local mutation without starting a model job."""
        with self._lock:
            latest = self._jobs.get(self._latest_by_project.get(project_id, ""))
            if project_id in self._inline_projects or (
                latest and latest["state"] in {"queued", "running"}
            ):
                raise CollageError("PROJECT_BUSY", "项目有任务运行，请完成后再另存")
            self._inline_projects.add(project_id)
        try:
            return operation()
        finally:
            with self._lock:
                self._inline_projects.discard(project_id)

    def _run(self, job_id: str, operation: Callable[[], Any]) -> None:
        with self.logs.capture(job_id):
            self._execute(job_id, operation)

    def _execute(self, job_id: str, operation: Callable[[], Any]) -> None:
        self._update(job_id, state="running")
        LOGGER.info(
            "工作台任务开始 | job=%s kind=%s", job_id, self._jobs[job_id]["kind"]
        )
        try:
            result = operation()
        except CollageError as exc:
            LOGGER.warning(
                "工作台任务失败 | job=%s code=%s message=%s",
                job_id,
                exc.code,
                exc.message,
            )
            if isinstance(exc, SpecValidationError):
                for issue in exc.issues:
                    LOGGER.warning(
                        "规格校验问题 | job=%s path=%s code=%s message=%s",
                        job_id,
                        issue.path,
                        issue.code,
                        issue.message,
                    )
            self._update(job_id, state="failed", error=safe_value(exc.as_dict()))
        except Exception as exc:  # pragma: no cover - final containment boundary
            LOGGER.exception("工作台任务发生未预期错误 | job=%s", job_id)
            self._update(
                job_id,
                state="failed",
                error={
                    "code": "WORKBENCH_JOB_FAILED",
                    "message": f"后台任务发生未预期错误：{type(exc).__name__}",
                    "details": {},
                },
            )
        else:
            # Workflow summaries may contain local paths; expose only the new
            # project destination from the explicit single-overlay operation.
            destination = (
                {"project_id": result["project_id"], "url": result["url"]}
                if self._jobs[job_id]["kind"] == "regenerate_overlay"
                and isinstance(result, dict)
                else None
            )
            LOGGER.info("工作台任务完成 | job=%s", job_id)
            self._update(job_id, state="succeeded", error=None, result=destination)

    def _update(self, job_id: str, **changes: Any) -> None:
        with self._lock:
            record = self._jobs[job_id]
            record.update(changes)
            record["updated_at"] = _utc_now()
            self.logs.update(record)

    @staticmethod
    def _public(record: dict[str, Any] | None) -> dict[str, Any]:
        if record is None:  # pragma: no cover - guarded by callers
            raise AssertionError("missing job")
        return {
            "result": record.get("result"),
            **{
                key: record[key]
                for key in (
                    "id",
                    "project_id",
                    "kind",
                    "state",
                    "created_at",
                    "updated_at",
                    "error",
                )
            },
        }
