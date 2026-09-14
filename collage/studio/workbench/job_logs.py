"""Persist bounded per-job logs and serve incremental history across workbench restarts."""

from __future__ import annotations

import copy
import logging
import re
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ...core.errors import CollageError
from ...core.diagnostics import JOB_ID
from ...core.io import atomic_write_json, read_json, safe_package_path
from ...core.privacy import safe_text, safe_value

MAX_EVENTS = 1000


class JobLogStore:
    """Keep the last 1000 events per attempt; never mix process-wide log streams."""

    def __init__(self, root: Path | None = None):
        self.root = root
        self._lock = threading.RLock()
        self._records: dict[str, dict] = {}

    def _path(self, project_id, job_id):
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise CollageError("INVALID_JOB_ID", "任务 ID 无效")
        return safe_package_path(self.root, f"{project_id}/{job_id}.json")

    def _persist(self, record):
        if self.root is None:
            return
        previous_error = record.pop("log_error", None)
        try:
            atomic_write_json(self._path(record["project_id"], record["id"]), record)
        except OSError:
            # Do not recursively log from a logging handler, or turn a completed
            # model request into an automatic retry because diagnostics failed.
            record["log_error"] = previous_error or {
                "code": "JOB_LOG_WRITE_FAILED",
                "message": "任务日志保存失败，请检查磁盘空间与权限",
            }

    def update(self, snapshot):
        with self._lock:
            record = self._records.setdefault(
                snapshot["id"], {"events": [], "last_seq": 0}
            )
            record.update(safe_value(snapshot))
            self._persist(record)

    def append(self, job_id, log_record):
        with self._lock:
            record = self._records[job_id]
            record["last_seq"] += 1
            record["events"].append(
                {
                    "seq": record["last_seq"],
                    "at": datetime.fromtimestamp(log_record.created, UTC).isoformat(),
                    "level": log_record.levelname,
                    "logger": log_record.name,
                    "message": safe_text(log_record.getMessage(), limit=8000),
                }
            )
            record["events"] = record["events"][-MAX_EVENTS:]
            self._persist(record)

    @contextmanager
    def capture(self, job_id):
        store = self
        owner = threading.get_ident()

        class Handler(logging.Handler):
            def emit(self, record):
                # A job runs in its own thread. Unrelated projects, HTTP requests
                # and background threads have no authority to append to this job.
                if record.thread == owner:
                    store.append(job_id, record)

        handler = Handler(logging.INFO)
        logger = logging.getLogger("collage")
        if logger.getEffectiveLevel() > logging.INFO:
            logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        token = JOB_ID.set(job_id)
        try:
            yield
        finally:
            logger.removeHandler(handler)
            JOB_ID.reset(token)
            handler.close()

    def _read(self, project_id, job_id):
        with self._lock:
            record = self._records.get(job_id)
            if record is not None and record["project_id"] == project_id:
                return copy.deepcopy(record)
        if self.root is None:
            raise CollageError("ARTIFACT_NOT_FOUND", "找不到该任务记录")
        path = self._path(project_id, job_id)
        if not path.is_file():
            raise CollageError("ARTIFACT_NOT_FOUND", "找不到该任务记录")
        record = safe_value(read_json(path))
        if (
            not isinstance(record, dict)
            or record.get("project_id") != project_id
            or record.get("id") != job_id
            or not {
                "state",
                "created_at",
                "updated_at",
                "kind",
                "error",
                "last_seq",
                "events",
            }.issubset(record)
            or not isinstance(record["events"], list)
        ):
            raise CollageError("INVALID_JOB_RECORD", "任务记录与项目不一致")
        if record["state"] in {"queued", "running"}:
            record.update(
                state="failed",
                error={
                    "code": "WORKBENCH_JOB_INTERRUPTED",
                    "message": "上次工作台进程在任务完成前停止，请检查记录后继续",
                    "details": {},
                },
            )
        return record

    def history(self, project_id):
        with self._lock:
            ids = {
                r["id"] for r in self._records.values() if r["project_id"] == project_id
            }
        if self.root is not None:
            directory = safe_package_path(self.root, project_id)
            if directory.is_dir():
                paths = sorted(
                    directory.glob("*.json"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                ids.update(
                    p.stem for p in paths[:30] if re.fullmatch(r"[a-f0-9]{32}", p.stem)
                )
        records = []
        for job_id in ids:
            try:
                record = self._read(project_id, job_id)
            except CollageError:
                continue
            records.append({k: v for k, v in record.items() if k != "events"})
        return sorted(records, key=lambda r: r["created_at"], reverse=True)[:30]

    def snapshot(self, project_id, job_id=None, after=0):
        history = self.history(project_id)
        selected = job_id or (history[0]["id"] if history else None)
        record = self._read(project_id, selected) if selected else None
        if record is not None:
            first_seq = record["events"][0]["seq"] if record["events"] else 1
            record["truncated"] = after < first_seq - 1
            record["events"] = [
                event for event in record["events"] if event["seq"] > after
            ]
        return {"jobs": history, "job": record}
