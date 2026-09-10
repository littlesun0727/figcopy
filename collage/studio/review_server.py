"""Serve the reusable Draft review session on a loopback-only HTTP server."""

from __future__ import annotations

import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any

from ..core.errors import CollageError
from .review_session import ReviewSession

LOGGER = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 64 * 1024 * 1024


def _read_text_resource(*parts: str) -> str:
    """读取随包分发的审核页面资源。"""

    return files("collage.studio").joinpath(*parts).read_text(encoding="utf-8")


HTML = _read_text_resource("templates", "review.html")
REVIEW_CSS = _read_text_resource("static", "review.css")
REVIEW_JS = _read_text_resource("static", "review.js")


def render_review_html(
    *, api_base: str = "", return_url: str = "", csrf_token: str = ""
) -> str:
    """Inject transport-specific endpoints into the shared review page."""

    return (
        HTML.replace("__REVIEW_API_BASE__", api_base)
        .replace("__REVIEW_RETURN_URL__", return_url)
        .replace("__FIGCOPY_CSRF_TOKEN__", csrf_token)
    )


def serve_review_ui(
    draft_path: Path,
    output_path: Path,
    *,
    reviewer: str,
    initial_mask_path: Path | None = None,
    allowed_mask_path: Path | None = None,
    background_candidate_path: Path | None = None,
    slot_overrides_path: Path | None = None,
    overlay_overrides_path: Path | None = None,
    background_expand_px: int = 0,
    background_feather_px: int = 0,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    """启动本地确认页；成功保存后自动停止服务。"""

    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise CollageError("UNSAFE_REVIEW_HOST", "确认界面只允许监听本机回环地址")
    session = ReviewSession(
        draft_path,
        output_path,
        reviewer=reviewer,
        initial_mask_path=initial_mask_path,
        allowed_mask_path=allowed_mask_path,
        background_candidate_path=background_candidate_path,
        slot_overrides_path=slot_overrides_path,
        overlay_overrides_path=overlay_overrides_path,
        background_expand_px=background_expand_px,
        background_feather_px=background_feather_px,
    )

    class Handler(BaseHTTPRequestHandler):
        server_version = "CollageReview/1"

        def log_message(self, format_string: str, *args: Any) -> None:
            LOGGER.debug("review-ui | " + format_string, *args)

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/":
                self._send(
                    HTTPStatus.OK,
                    "text/html; charset=utf-8",
                    render_review_html().encode("utf-8"),
                )
            elif self.path == "/static/review.css":
                self._send(
                    HTTPStatus.OK,
                    "text/css; charset=utf-8",
                    REVIEW_CSS.encode("utf-8"),
                )
            elif self.path == "/static/review.js":
                self._send(
                    HTTPStatus.OK,
                    "text/javascript; charset=utf-8",
                    REVIEW_JS.encode("utf-8"),
                )
            elif self.path == "/draft":
                self._send(
                    HTTPStatus.OK,
                    "application/json",
                    json.dumps(session.draft, ensure_ascii=False).encode("utf-8"),
                )
            elif self.path == "/review-options":
                self._send(
                    HTTPStatus.OK,
                    "application/json",
                    json.dumps(session.review_options, ensure_ascii=False).encode(
                        "utf-8"
                    ),
                )
            elif self.path == "/reference":
                self._send(
                    HTTPStatus.OK, "image/png", session.reference_path.read_bytes()
                )
            elif self.path == "/mask":
                self._send(HTTPStatus.OK, "image/png", session.mask_png)
            else:
                self._send(
                    HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"not found"
                )

        def do_POST(self) -> None:
            if self.path != "/save":
                self._send(
                    HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"not found"
                )
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_REQUEST_BYTES:
                    raise CollageError("REQUEST_TOO_LARGE", "保存请求为空或超过 64 MiB")
                payload = json.loads(self.rfile.read(length))
                result = session.save(payload)
                body = json.dumps(result, ensure_ascii=False).encode("utf-8")
                self._send(HTTPStatus.OK, "application/json", body)
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            except (CollageError, KeyError, json.JSONDecodeError) as exc:
                error = (
                    exc
                    if isinstance(exc, CollageError)
                    else CollageError("INVALID_REQUEST", "保存请求格式错误")
                )
                body = json.dumps(error.as_dict(), ensure_ascii=False).encode("utf-8")
                self._send(HTTPStatus.BAD_REQUEST, "application/json", body)

    server = ThreadingHTTPServer((host, port), Handler)
    LOGGER.info("人工确认页已启动 | url=http://%s:%s", host, port)
    LOGGER.info("浏览器成功保存 reviewed.json 后服务会自动停止；也可按 Ctrl+C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("人工确认页已停止")
    finally:
        server.server_close()
