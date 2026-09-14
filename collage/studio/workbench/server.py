"""Serve the Figcopy project workbench on a loopback-only HTTP endpoint."""

from __future__ import annotations

import json
import logging
import mimetypes
import secrets
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from ...core.errors import CollageError
from ...projects import DataPaths
from ..review_server import REVIEW_CSS, REVIEW_JS, render_review_html
from .application import WorkbenchApplication
from .multipart import parse_multipart

LOGGER = logging.getLogger(__name__)
MAX_UPLOAD_BYTES = 128 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024 * 1024
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _read_text_resource(*parts: str) -> str:
    return files("collage.studio").joinpath(*parts).read_text(encoding="utf-8")


WORKBENCH_HTML = _read_text_resource("templates", "workbench.html")
WORKBENCH_CSS = _read_text_resource("static", "workbench.css")
WORKBENCH_JS = _read_text_resource("static", "workbench.js")


def _hostname(value: str) -> str | None:
    """Extract a hostname from Host or Origin without accepting credentials."""

    candidate = value.strip()
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = f"http://{candidate}"
    try:
        return urlsplit(candidate).hostname
    except ValueError:
        return None


def _content_type(path: Path) -> str:
    guessed, _encoding = mimetypes.guess_type(path.name)
    if path.suffix.lower() == ".json":
        return "application/json; charset=utf-8"
    if path.suffix.lower() == ".html":
        return "text/html; charset=utf-8"
    return guessed or "application/octet-stream"


def _error_status(error: CollageError) -> HTTPStatus:
    if error.code in {"PROJECT_NOT_FOUND", "ARTIFACT_NOT_FOUND"}:
        return HTTPStatus.NOT_FOUND
    if error.code in {"PROJECT_ALREADY_EXISTS", "PROJECT_BUSY", "REVIEW_ALREADY_SAVED"}:
        return HTTPStatus.CONFLICT
    if error.code == "REQUEST_TOO_LARGE":
        return HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    if error.code in {"INVALID_CSRF_TOKEN", "UNSAFE_REQUEST_HOST", "UNSAFE_ORIGIN"}:
        return HTTPStatus.FORBIDDEN
    return HTTPStatus.BAD_REQUEST


def create_workbench_server(
    data_dir: Path | str | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    application: WorkbenchApplication | None = None,
) -> ThreadingHTTPServer:
    """Create a testable workbench server without starting its serve loop."""

    if host not in _LOOPBACK_HOSTS:
        raise CollageError("UNSAFE_WORKBENCH_HOST", "工作台只允许监听本机回环地址")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise CollageError("INVALID_WORKBENCH_PORT", "工作台端口必须在 0-65535 之间")
    app = application or WorkbenchApplication(DataPaths.resolve(data_dir))
    csrf_token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        server_version = "FigcopyWorkbench/1"

        def log_message(self, format_string: str, *args: Any) -> None:
            LOGGER.debug("workbench | " + format_string, *args)

        def _send(
            self,
            status: int,
            content_type: str,
            body: bytes,
            *,
            disposition: str | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data: blob:; "
                "script-src 'self'; style-src 'self'; connect-src 'self'; "
                "base-uri 'none'; frame-ancestors 'none'",
            )
            if disposition is not None:
                self.send_header("Content-Disposition", disposition)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: Any) -> None:
            self._send(
                status,
                "application/json; charset=utf-8",
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )

        def _not_found(self) -> None:
            self._json(
                HTTPStatus.NOT_FOUND,
                {"code": "NOT_FOUND", "message": "页面或接口不存在", "details": {}},
            )

        def _guard_request_host(self) -> None:
            request_host = _hostname(self.headers.get("Host", ""))
            if request_host not in _LOOPBACK_HOSTS:
                raise CollageError("UNSAFE_REQUEST_HOST", "请求 Host 不是本机回环地址")

        def _guard_mutation(self) -> None:
            self._guard_request_host()
            if self.headers.get("X-Figcopy-Token") != csrf_token:
                raise CollageError("INVALID_CSRF_TOKEN", "工作台请求令牌无效")
            origin = self.headers.get("Origin")
            if origin and _hostname(origin) not in _LOOPBACK_HOSTS:
                raise CollageError("UNSAFE_ORIGIN", "拒绝非本机页面发起的写入请求")

        def _read_body(self, maximum: int) -> bytes:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise CollageError(
                    "INVALID_CONTENT_LENGTH", "Content-Length 无效"
                ) from exc
            if length <= 0:
                raise CollageError("EMPTY_REQUEST", "请求内容为空")
            if length > maximum:
                raise CollageError(
                    "REQUEST_TOO_LARGE",
                    f"请求超过 {maximum // (1024 * 1024)} MiB 限制",
                )
            body = self.rfile.read(length)
            if len(body) != length:
                raise CollageError("INCOMPLETE_REQUEST", "请求内容未完整接收")
            return body

        def _read_json(self) -> Any:
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
            if content_type.lower() != "application/json":
                raise CollageError(
                    "INVALID_CONTENT_TYPE", "请求必须使用 application/json"
                )
            try:
                return json.loads(self._read_body(MAX_JSON_BYTES))
            except json.JSONDecodeError as exc:
                raise CollageError("INVALID_REQUEST", "请求不是有效 JSON") from exc

        def _project_parts(self, path: str) -> tuple[str, list[str]] | None:
            prefix = "/api/projects/"
            if not path.startswith(prefix):
                return None
            parts = [unquote(part) for part in path[len(prefix) :].split("/")]
            if not parts or not parts[0]:
                return None
            return parts[0], parts[1:]

        def do_GET(self) -> None:
            try:
                self._guard_request_host()
                if urlsplit(self.path).path.rstrip("/") == "/api/provider-settings":
                    self._json(
                        HTTPStatus.OK,
                        app.provider_status(probe_audit=True),
                    )
                    return
                parsed = urlsplit(self.path)
                path = parsed.path.rstrip("/") or "/"
                if path == "/" or (
                    path.startswith("/projects/")
                    and not path.endswith(("/review", "/layers"))
                ):
                    page = WORKBENCH_HTML.replace("__FIGCOPY_CSRF_TOKEN__", csrf_token)
                    self._send(
                        HTTPStatus.OK,
                        "text/html; charset=utf-8",
                        page.encode("utf-8"),
                    )
                    return
                if path.startswith("/projects/") and path.endswith("/layers"):
                    project_id = unquote(
                        path.removeprefix("/projects/")[: -len("/layers")]
                    )
                    app.layout(project_id)
                    page = _read_text_resource("templates", "layers.html").replace(
                        "__FIGCOPY_CSRF_TOKEN__", csrf_token
                    )
                    self._send(
                        HTTPStatus.OK, "text/html; charset=utf-8", page.encode("utf-8")
                    )
                    return
                if path == "/static/layers.js":
                    self._send(
                        HTTPStatus.OK,
                        "text/javascript; charset=utf-8",
                        _read_text_resource("static", "layers.js").encode("utf-8"),
                    )
                    return
                if path == "/static/workbench.css":
                    self._send(
                        HTTPStatus.OK,
                        "text/css; charset=utf-8",
                        WORKBENCH_CSS.encode("utf-8"),
                    )
                    return
                if path == "/static/workbench.js":
                    self._send(
                        HTTPStatus.OK,
                        "text/javascript; charset=utf-8",
                        WORKBENCH_JS.encode("utf-8"),
                    )
                    return
                if path == "/static/review.css":
                    self._send(
                        HTTPStatus.OK,
                        "text/css; charset=utf-8",
                        REVIEW_CSS.encode("utf-8"),
                    )
                    return
                if path == "/static/review.js":
                    self._send(
                        HTTPStatus.OK,
                        "text/javascript; charset=utf-8",
                        REVIEW_JS.encode("utf-8"),
                    )
                    return
                if path == "/api/config":
                    self._json(
                        HTTPStatus.OK,
                        {"data_dir": str(app.paths.root), "max_upload_mib": 128},
                    )
                    return
                if path == "/api/projects":
                    self._json(HTTPStatus.OK, {"projects": app.list_projects()})
                    return
                if path.startswith("/api/tasks/"):
                    project_id = unquote(path.removeprefix("/api/tasks/"))
                    self._json(
                        HTTPStatus.OK,
                        {"task": app.latest_job(project_id)},
                    )
                    return
                if path.startswith("/projects/") and path.endswith("/review"):
                    project_id = unquote(
                        path.removeprefix("/projects/")[: -len("/review")]
                    )
                    app.review_session(project_id)
                    page = render_review_html(
                        api_base=f"/api/projects/{project_id}/review",
                        return_url=f"/projects/{project_id}",
                        csrf_token=csrf_token,
                    )
                    self._send(
                        HTTPStatus.OK,
                        "text/html; charset=utf-8",
                        page.encode("utf-8"),
                    )
                    return

                routed = self._project_parts(path)
                if routed is None:
                    self._not_found()
                    return
                project_id, rest = routed
                if rest == ["layout", "preview"]:
                    self._send(
                        HTTPStatus.OK,
                        "image/png",
                        app.preview_layout(project_id, app.layout(project_id)),
                    )
                    return
                if rest == ["review", "recoveries"]:
                    self._json(
                        HTTPStatus.OK, {"recoveries": app.review_recoveries(project_id)}
                    )
                    return
                if rest == ["review", "correction-status"]:
                    self._json(HTTPStatus.OK, {"task": app.latest_job(project_id)})
                    return
                if not rest:
                    self._json(HTTPStatus.OK, app.project_status(project_id))
                    return
                if rest == ["layout"]:
                    self._json(HTTPStatus.OK, app.layout(project_id))
                    return
                if rest == ["background-revision"]:
                    self._json(HTTPStatus.OK, app.background_revision(project_id))
                    return
                if len(rest) == 3 and rest[:2] == ["layout", "layers"]:
                    self._send(
                        HTTPStatus.OK,
                        "image/png",
                        app.layout_layer(project_id, rest[2]),
                    )
                    return
                if rest == ["slots"]:
                    self._json(
                        HTTPStatus.OK,
                        {"slots": app.project_slots(project_id)},
                    )
                    return
                if len(rest) == 2 and rest[0] == "artifacts":
                    artifact = app.artifact_path(project_id, rest[1])
                    disposition = None
                    if parsed.query == "download=1":
                        disposition = f'attachment; filename="{artifact.name}"'
                    self._send(
                        HTTPStatus.OK,
                        _content_type(artifact),
                        artifact.read_bytes(),
                        disposition=disposition,
                    )
                    return
                if len(rest) == 2 and rest[0] == "review":
                    session = app.review_session(project_id)
                    if rest[1] == "session":
                        self._json(HTTPStatus.OK, session.browser_state())
                    elif rest[1] == "draft":
                        self._json(HTTPStatus.OK, session.draft)
                    elif rest[1] == "review-options":
                        self._json(HTTPStatus.OK, session.review_options)
                    elif rest[1] == "reference":
                        self._send(
                            HTTPStatus.OK,
                            "image/png",
                            session.reference_path.read_bytes(),
                        )
                    elif rest[1] == "mask":
                        self._send(HTTPStatus.OK, "image/png", session.mask_png)
                    else:
                        self._not_found()
                    return
                self._not_found()
            except CollageError as exc:
                self._json(_error_status(exc), exc.as_dict())
            except (BrokenPipeError, ConnectionResetError):
                LOGGER.debug("浏览器在响应完成前关闭连接")
            except Exception:
                LOGGER.exception("工作台 GET 请求失败")
                self._json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "code": "WORKBENCH_SERVER_ERROR",
                        "message": "工作台读取请求失败",
                        "details": {},
                    },
                )

        def do_POST(self) -> None:
            try:
                self._guard_mutation()
                if urlsplit(self.path).path.rstrip("/") == "/api/provider-settings":
                    self._json(
                        HTTPStatus.OK,
                        app.configure_providers(self._read_json()),
                    )
                    return
                path = urlsplit(self.path).path.rstrip("/") or "/"
                if path == "/api/projects":
                    form = parse_multipart(
                        self.headers.get("Content-Type", ""),
                        self._read_body(MAX_UPLOAD_BYTES),
                    )
                    self._json(
                        HTTPStatus.ACCEPTED,
                        {"task": app.start_project(form)},
                    )
                    return
                routed = self._project_parts(path)
                if routed is None:
                    self._not_found()
                    return
                project_id, rest = routed
                if rest == ["providers"]:
                    self._json(
                        HTTPStatus.OK,
                        app.configure_project_providers(project_id, self._read_json()),
                    )
                    return
                if rest == ["layout", "edit"]:
                    self._json(
                        HTTPStatus.OK, app.edit_layout(project_id, self._read_json())
                    )
                    return
                if rest == ["layout", "save"]:
                    self._json(
                        HTTPStatus.OK, app.save_layout(project_id, self._read_json())
                    )
                    return
                if rest == ["background-revision"]:
                    self._json(
                        HTTPStatus.OK,
                        app.fork_background(project_id, self._read_json()),
                    )
                    return
                if rest == ["layout", "preview"]:
                    self._send(
                        HTTPStatus.OK,
                        "image/png",
                        app.preview_layout(project_id, self._read_json()),
                    )
                    return
                if rest == ["overlays", "regenerate"]:
                    self._json(
                        HTTPStatus.ACCEPTED,
                        {"task": app.regenerate_overlay(project_id, self._read_json())},
                    )
                    return
                if rest == ["review", "recover"]:
                    self._json(
                        HTTPStatus.ACCEPTED,
                        {"task": app.recover_review(project_id, self._read_json())},
                    )
                    return
                if rest == ["review", "revise"]:
                    self._json(
                        HTTPStatus.ACCEPTED,
                        {"task": app.revise_review(project_id, self._read_json())},
                    )
                    return
                if rest == ["review", "layout"]:
                    self._json(
                        HTTPStatus.OK,
                        app.review_session(project_id).layout(self._read_json()),
                    )
                    return
                if rest == ["review", "save"]:
                    result = app.save_review(project_id, self._read_json())
                    self._json(HTTPStatus.OK, result)
                    return
                if rest == ["bindings"]:
                    form = parse_multipart(
                        self.headers.get("Content-Type", ""),
                        self._read_body(MAX_UPLOAD_BYTES),
                    )
                    self._json(
                        HTTPStatus.ACCEPTED,
                        {"task": app.submit_bindings(project_id, form)},
                    )
                    return
                if rest == ["retry"]:
                    self._json(
                        HTTPStatus.ACCEPTED,
                        {"task": app.retry_project(project_id, self._read_json())},
                    )
                    return
                if rest == ["approve"]:
                    self._json(
                        HTTPStatus.ACCEPTED,
                        {"task": app.approve_project(project_id, self._read_json())},
                    )
                    return
                self._not_found()
            except CollageError as exc:
                self._json(_error_status(exc), exc.as_dict())
            except (BrokenPipeError, ConnectionResetError):
                LOGGER.debug("浏览器在响应完成前关闭连接")
            except Exception:
                LOGGER.exception("工作台 POST 请求失败")
                self._json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "code": "WORKBENCH_SERVER_ERROR",
                        "message": "工作台写入请求失败",
                        "details": {},
                    },
                )

    server = ThreadingHTTPServer((host, port), Handler)
    server.figcopy_application = app  # type: ignore[attr-defined]
    return server


def serve_workbench(
    data_dir: Path | str | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    open_browser: bool = True,
) -> None:
    """Run the local workbench until interrupted."""

    server = create_workbench_server(data_dir, host=host, port=port)
    actual_port = int(server.server_address[1])
    url = f"http://{host}:{actual_port}/"
    LOGGER.info("Figcopy 工作台已启动 | url=%s", url)
    LOGGER.info("运行数据目录 | path=%s", DataPaths.resolve(data_dir).root)
    if open_browser:
        threading.Timer(0.15, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Figcopy 工作台已停止")
    finally:
        server.server_close()
