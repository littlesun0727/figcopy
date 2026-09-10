"""Run slow workflow operations in per-project background jobs."""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ...core.errors import CollageError

LOGGER = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class JobRegistry:
    """Keep transient job progress while durable stage state stays in project.json."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._latest_by_project: dict[str, str] = {}

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
            if previous is not None and previous["state"] in {"queued", "running"}:
                raise CollageError(
                    "PROJECT_BUSY",
                    "该项目已有任务正在执行，请等待完成后再操作",
                    details={"job_id": previous["id"]},
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
            return self._public(record) if record is not None else None

    def active_project_ids(self) -> set[str]:
        """Return project identifiers that currently have queued or running work."""

        with self._lock:
            return {
                record["project_id"]
                for record in self._jobs.values()
                if record["state"] in {"queued", "running"}
            }

    def _run(self, job_id: str, operation: Callable[[], Any]) -> None:
        self._update(job_id, state="running")
        try:
            operation()
        except CollageError as exc:
            LOGGER.warning(
                "工作台任务失败 | job=%s code=%s message=%s",
                job_id,
                exc.code,
                exc.message,
            )
            self._update(job_id, state="failed", error=exc.as_dict())
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
            self._update(job_id, state="succeeded", error=None)

    def _update(self, job_id: str, **changes: Any) -> None:
        with self._lock:
            record = self._jobs[job_id]
            record.update(changes)
            record["updated_at"] = _utc_now()

    @staticmethod
    def _public(record: dict[str, Any] | None) -> dict[str, Any]:
        if record is None:  # pragma: no cover - guarded by callers
            raise AssertionError("missing job")
        return {
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
        }
