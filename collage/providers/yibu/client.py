"""通过本机审计代理发送 yibu 请求并归一化响应元数据。"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from ...core.errors import CollageError
from .constants import MAX_RESPONSE_BYTES
from .settings import YibuSettings, _safe_error_text, _validated_audit_base_url

LOGGER = logging.getLogger(__name__)


class _YibuAuditClient:
    """仅通过审计代理发送 JSON 请求，并限制响应体大小。"""

    def __init__(self, settings: YibuSettings) -> None:
        self.settings = settings
        self.base_url = _validated_audit_base_url(settings.audit_base_url)
        self._health_checked = False

    def _ensure_audit_proxy(self) -> None:
        if self._health_checked:
            return
        request = urllib.request.Request(
            f"{self.base_url}/_yibu_audit/health",
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=min(10, self.settings.timeout_seconds)
            ) as response:
                payload = json.loads(response.read(1024 * 1024))
        except Exception as exc:
            raise CollageError(
                "YIBU_AUDIT_PROXY_UNAVAILABLE",
                "yibu 审计代理不可用；请先启动 D:\\codes\\yibu-audit-proxy",
            ) from exc
        upstream_host = urlsplit(str(payload.get("upstream", ""))).hostname
        if payload.get("status") != "ok" or not (
            upstream_host == "yibuapi.com"
            or (upstream_host and upstream_host.endswith(".yibuapi.com"))
        ):
            raise CollageError(
                "YIBU_AUDIT_PROXY_INVALID",
                "审计健康检查未指向 yibuapi.com，已拒绝模型调用",
            )
        self._health_checked = True
        LOGGER.info("yibu 审计代理健康检查通过 | endpoint=%s", self.base_url)

    def post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        model: str,
        operation: str,
        auth_style: str,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        self._ensure_audit_proxy()
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if auth_style == "google":
            headers["x-goog-api-key"] = self.settings.api_key
        else:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        LOGGER.info(
            "通过审计代理调用 yibu | operation=%s model=%s path=%s",
            operation,
            model,
            path,
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.settings.timeout_seconds
            ) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise CollageError(
                        "PROVIDER_RESPONSE_TOO_LARGE", "yibu 响应超过 64 MiB 限制"
                    )
                response_headers = {
                    name.lower(): value for name, value in response.headers.items()
                }
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(4096).decode("utf-8", "replace")
            except OSError:
                detail = str(exc.reason)
            # 上游可能回显任意格式的凭据；不能只依赖 sk- 前缀脱敏。
            if self.settings.api_key:
                detail = detail.replace(self.settings.api_key, "<api-key>")
            detail = _safe_error_text(detail)
            message = f"yibu 请求失败：HTTP {exc.code}"
            if exc.code == 429:
                code = "RATE_LIMITED"
            elif exc.code in {500, 502, 503, 504}:
                code = "TEMPORARY_NETWORK_ERROR"
            elif exc.code in {401, 403}:
                code = "PROVIDER_AUTH_FAILED"
                permission_hint = "及模型访问权限" if exc.code == 403 else ""
                # 日志使用固定指引，上游正文仅保留在脱敏后的 details 中。
                message = (
                    f"yibu 鉴权失败：HTTP {exc.code}；请在 Provider 设置或启动环境中"
                    f"检查或更新 API Key{permission_hint}；若审计代理设置了 "
                    "YIBU_UPSTREAM_API_KEY，该凭据会覆盖工作台 Key，需检查代理配置"
                )
            else:
                code = "PROVIDER_REQUEST_FAILED"
            raise CollageError(
                code,
                message,
                details={
                    "operation": operation,
                    "response": detail,
                    "http_status": exc.code,
                },
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CollageError(
                "TEMPORARY_NETWORK_ERROR",
                f"yibu 审计请求失败：{type(exc).__name__}",
                details={"operation": operation},
            ) from exc
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            raise CollageError(
                "PROVIDER_INVALID_RESPONSE", "yibu 返回的不是有效 JSON"
            ) from exc
        if not isinstance(decoded, dict):
            raise CollageError(
                "PROVIDER_INVALID_RESPONSE", "yibu 顶层响应必须是 JSON object"
            )
        return decoded, response_headers


def _request_id(response: dict[str, Any], headers: dict[str, str]) -> str | None:
    for value in (
        response.get("responseId"),
        response.get("response_id"),
        response.get("id"),
        headers.get("x-request-id"),
        headers.get("request-id"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
