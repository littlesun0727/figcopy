"""Small direct HTTP transport for explicitly configured internal model services."""

from __future__ import annotations

import json
import logging
import os
import socket
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from ..core.errors import CollageError
from ..core.diagnostics import capture_evidence
from ..core.privacy import safe_text, safe_value

LOGGER = logging.getLogger(__name__)
MAX_RESPONSE_BYTES = 64 * 1024 * 1024


def service_url(value: str) -> str:
    """Allow internal HTTP endpoints, keeping credentials out of URLs."""
    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        parsed.port  # Trigger urllib's port validation before attempting a request.
    except ValueError as exc:
        raise CollageError(
            "PROVIDER_CONFIG_INVALID", "服务地址或端口格式不正确"
        ) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise CollageError(
            "PROVIDER_CONFIG_INVALID", "服务地址必须是无凭据的 HTTP 或 HTTPS 地址"
        )
    return value


def positive_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
        if value > 0:
            return value
    except ValueError:
        pass
    raise CollageError("PROVIDER_CONFIG_INVALID", f"{name} 必须为正整数")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not forward reference images or credentials to a redirected service."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ServiceClient:
    """Send to a configured service; never log credentials or echoed request data."""

    def __init__(self, base_url: str, timeout_seconds: int, api_key: str = ""):
        self.base_url = service_url(base_url)
        self.timeout_seconds = timeout_seconds
        self.api_key = api_key
        self._opener = urllib.request.build_opener(_NoRedirect)

    def request(self, path, payload=None, *, timeout=None, operation="health"):
        headers = {"Accept": "application/json, image/png"}
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers=headers,
            method="POST" if payload is not None else "GET",
        )
        LOGGER.info("调用内网服务 | operation=%s", operation)
        try:
            with self._opener.open(
                request, timeout=timeout or self.timeout_seconds
            ) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                response_headers = {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as exc:
            error_body = ""
            try:
                error_body = exc.read(16385).decode("utf-8", "replace")
                error = json.loads(error_body)
            except (ValueError, OSError):
                error = {}
            if (
                exc.code == 503
                and isinstance(error, dict)
                and error.get("error") == "MODEL_BUSY"
            ):
                raise CollageError(
                    "PROVIDER_BUSY",
                    "图片服务正在处理其他任务，请稍后重试",
                    details={"request_state": "not_started"},
                ) from exc
            code = (
                "PROVIDER_AUTH_FAILED"
                if exc.code in {401, 403}
                else "PROVIDER_REQUEST_FAILED"
            )
            raise CollageError(
                code,
                f"内网服务返回 HTTP {exc.code}",
                details={
                    "http_status": exc.code,
                    "request_state": "failed",
                    "operation": operation,
                    "request_id": safe_text(
                        exc.headers.get("x-request-id", ""), secrets=(self.api_key,)
                    ),
                    "response": safe_text(
                        json.dumps(
                            safe_value(error, secrets=(self.api_key,)),
                            ensure_ascii=False,
                        )
                        if error
                        else error_body,
                        secrets=(self.api_key,),
                        limit=16384,
                    ),
                },
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            not_started = isinstance(reason, (ConnectionRefusedError, socket.gaierror))
            raise CollageError(
                "PROVIDER_UNAVAILABLE" if not_started else "PROVIDER_REQUEST_UNCERTAIN",
                "无法连接内网服务"
                if not_started
                else "请求中断或超时，服务可能仍在执行；请检查服务后手动重试",
                details={"request_state": "not_started" if not_started else "unknown"},
            ) from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise CollageError("PROVIDER_INVALID_RESPONSE", "服务响应超过 64 MiB")
        return raw, response_headers

    def post_json(self, path, payload, *, model, operation, auth_style):
        raw, headers = self.request(path, payload, operation=operation)
        capture_evidence(
            "transport_response.txt",
            safe_text(raw.decode("utf-8", "replace"), secrets=(self.api_key,)),
        )
        try:
            result = json.loads(raw)
        except ValueError as exc:
            raise CollageError(
                "PROVIDER_INVALID_RESPONSE", "服务未返回有效 JSON"
            ) from exc
        if not isinstance(result, dict):
            raise CollageError(
                "PROVIDER_INVALID_RESPONSE", "服务响应必须为 JSON object"
            )
        return result, headers
