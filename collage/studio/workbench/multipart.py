"""Parse bounded browser multipart requests without external web dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from typing import Any

from ...core.errors import CollageError


@dataclass(frozen=True, slots=True)
class UploadedFile:
    """An uploaded file held only until it is imported into a project."""

    filename: str
    content_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class MultipartForm:
    """Decoded scalar fields and uniquely named files."""

    fields: dict[str, str]
    files: dict[str, UploadedFile]


def parse_multipart(content_type: str, body: bytes) -> MultipartForm:
    """Decode one multipart/form-data body with strict field uniqueness."""

    if not content_type.lower().startswith("multipart/form-data"):
        raise CollageError("INVALID_CONTENT_TYPE", "请求必须使用 multipart/form-data")
    envelope = (
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode(
            "ascii", errors="strict"
        )
        + body
    )
    try:
        message = BytesParser(policy=policy.default).parsebytes(envelope)
    except Exception as exc:
        raise CollageError("INVALID_MULTIPART", "无法解析上传表单") from exc
    if not message.is_multipart():
        raise CollageError("INVALID_MULTIPART", "上传表单缺少 multipart boundary")

    fields: dict[str, str] = {}
    uploads: dict[str, UploadedFile] = {}
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        name = part.get_param("name", header="content-disposition")
        if not isinstance(name, str) or not name:
            raise CollageError("INVALID_MULTIPART", "上传字段缺少名称")
        raw = part.get_payload(decode=True)
        if not isinstance(raw, bytes):
            raise CollageError("INVALID_MULTIPART", f"上传字段 {name} 无法解码")
        filename = part.get_filename()
        if filename is not None:
            if name in uploads:
                raise CollageError("DUPLICATE_UPLOAD", f"上传文件字段重复：{name}")
            uploads[name] = UploadedFile(
                filename=filename,
                content_type=part.get_content_type(),
                data=raw,
            )
            continue
        if name in fields:
            raise CollageError("DUPLICATE_FIELD", f"表单字段重复：{name}")
        charset = part.get_content_charset("utf-8")
        try:
            fields[name] = raw.decode(charset)
        except (LookupError, UnicodeDecodeError) as exc:
            raise CollageError(
                "INVALID_FORM_ENCODING", f"字段 {name} 不是 UTF-8 文本"
            ) from exc
    return MultipartForm(fields, uploads)


def required_text(fields: dict[str, str], name: str, *, max_length: int = 160) -> str:
    """Read and bound one required scalar form field."""

    value = fields.get(name, "").strip()
    if not value:
        raise CollageError("MISSING_FORM_FIELD", f"缺少必填字段：{name}")
    if len(value) > max_length:
        raise CollageError("FORM_FIELD_TOO_LONG", f"字段 {name} 过长")
    return value


def optional_text(
    fields: dict[str, str], name: str, *, max_length: int = 500
) -> str | None:
    """Read a bounded optional field, treating whitespace as absent."""

    value = fields.get(name, "").strip()
    if not value:
        return None
    if len(value) > max_length:
        raise CollageError("FORM_FIELD_TOO_LONG", f"字段 {name} 过长")
    return value


def boolean_field(fields: dict[str, str], name: str, *, default: bool = False) -> bool:
    """Parse the checkbox strings emitted by browser forms."""

    value: Any = fields.get(name)
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise CollageError("INVALID_BOOLEAN", f"字段 {name} 必须是 boolean")
