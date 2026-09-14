"""Retain each analysis attempt and pre-validation model evidence for local diagnosis."""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

from .errors import CollageError
from .io import atomic_write_bytes, atomic_write_json
from .privacy import safe_text, safe_value

LOGGER = logging.getLogger(__name__)
_ATTEMPT: ContextVar[AnalysisAttempt | None] = ContextVar(
    "analysis_attempt", default=None
)
JOB_ID: ContextVar[str | None] = ContextVar("diagnostic_job_id", default=None)


class AnalysisAttempt:
    """One immutable-identity attempt; diagnostic candidates never replace the valid Draft."""

    def __init__(self, output_dir: Path, metadata: dict, secrets: tuple[str, ...]):
        now = datetime.now(UTC)
        # Keep identifiers short enough for nested revision copies on Windows.
        self.id = now.strftime("%y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:12]
        self.directory = output_dir / "attempts" / self.id
        self.secrets = secrets
        self.record = {
            "id": self.id,
            "started_at": now.isoformat(),
            "status": "running",
            "phase": "preparing",
            "files": [],
            "job_id": JOB_ID.get(),
            **metadata,
        }
        self.save("attempt.json", self.record)

    def save(self, name: str, value) -> None:
        try:
            if name.endswith(".txt"):
                atomic_write_bytes(
                    self.directory / name,
                    safe_text(value, secrets=self.secrets).encode("utf-8"),
                )
            else:
                atomic_write_json(
                    self.directory / name, safe_value(value, secrets=self.secrets)
                )
        except OSError as exc:
            raise CollageError(
                "DIAGNOSTIC_WRITE_FAILED",
                "无法保存分析诊断文件，请检查磁盘空间与写入权限",
            ) from exc
        if name != "attempt.json" and name not in self.record["files"]:
            self.record["files"].append(name)

    def phase(self, name: str) -> None:
        self.record["phase"] = name
        self.save("attempt.json", self.record)


def capture_evidence(name: str, value) -> None:
    """Use the current call context without changing existing provider interfaces."""
    attempt = _ATTEMPT.get()
    if attempt is not None:
        attempt.save(name, value)


def analysis_phase(name: str) -> None:
    attempt = _ATTEMPT.get()
    if attempt is not None:
        attempt.phase(name)


@contextmanager
def analysis_attempt(
    output_dir: Path, metadata: dict, *, secrets: tuple[str, ...] = ()
):
    attempt = AnalysisAttempt(output_dir, metadata, secrets)
    token = _ATTEMPT.set(attempt)
    LOGGER.info("开始分析尝试 | attempt=%s", attempt.id)
    try:
        yield attempt
    except Exception as exc:
        error = (
            exc.as_dict()
            if isinstance(exc, CollageError)
            else {
                "code": "ANALYSIS_FAILED",
                "message": f"分析发生未预期错误：{type(exc).__name__}",
                "details": {},
            }
        )
        if isinstance(exc, CollageError):
            exc.details["attempt_id"] = attempt.id
        attempt.record.update(
            status="failed", error=error, finished_at=datetime.now(UTC).isoformat()
        )
        try:
            attempt.save(
                "validation.json",
                {"valid": False, "phase": attempt.record["phase"], "error": error},
            )
            attempt.save("attempt.json", attempt.record)
        except CollageError:
            LOGGER.error(
                "分析诊断保存失败 | code=DIAGNOSTIC_WRITE_FAILED attempt=%s", attempt.id
            )
        LOGGER.warning(
            "分析失败 | attempt=%s phase=%s code=%s",
            attempt.id,
            attempt.record["phase"],
            error["code"],
        )
        raise
    else:
        attempt.record.update(
            status="succeeded", finished_at=datetime.now(UTC).isoformat()
        )
        attempt.save("validation.json", {"valid": True, "issues": []})
        attempt.save("attempt.json", attempt.record)
    finally:
        _ATTEMPT.reset(token)
