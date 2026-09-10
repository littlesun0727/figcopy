"""定义可被 CLI 稳定识别和展示的业务错误。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """一条带 JSON 路径的校验问题。"""

    path: str
    message: str
    code: str = "INVALID_VALUE"

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message, "code": self.code}


class CollageError(RuntimeError):
    """所有预期业务失败的基类，code 可供脚本可靠判断。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class SpecValidationError(CollageError):
    """聚合 schema 或模板包校验问题。"""

    def __init__(
        self, issues: list[ValidationIssue], message: str = "规格校验失败"
    ) -> None:
        super().__init__(
            "SPEC_VALIDATION_FAILED",
            message,
            details={"issues": [issue.as_dict() for issue in issues]},
        )
        self.issues = issues
