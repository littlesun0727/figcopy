"""提供仅监听本机的一页式 Draft 参数、层序和 remove mask 人工确认界面。"""

from __future__ import annotations

import base64
import io
import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any

from PIL import Image

from ..core.errors import CollageError
from ..core.io import (
    atomic_save_image,
    atomic_write_json,
    read_json,
    resolve_input_path,
)
from ..imaging.operations import load_mask
from ..schemas import validate_draft
from ..template.review import (
    automatic_remove_mask,
    automatic_review_notes,
    background_parameter,
    build_review_options,
    confirm_draft,
    question_resolution_notes,
    validate_override_map,
    validate_review_decisions,
)

LOGGER = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 64 * 1024 * 1024


def _read_text_resource(*parts: str) -> str:
    """读取随包分发的审核页面资源。"""

    return files("collage.studio").joinpath(*parts).read_text(encoding="utf-8")


HTML = _read_text_resource("templates", "review.html")
REVIEW_CSS = _read_text_resource("static", "review.css")
REVIEW_JS = _read_text_resource("static", "review.js")


def _decode_mask(data_url: str, expected_size: tuple[int, int]) -> Image.Image:
    prefix = "data:image/png;base64,"
    if not data_url.startswith(prefix):
        raise CollageError("INVALID_MASK_PAYLOAD", "mask 必须是 PNG data URL")
    try:
        raw = base64.b64decode(data_url[len(prefix) :], validate=True)
        with Image.open(io.BytesIO(raw)) as source:
            source.load()
            rgba = source.convert("RGBA")
            # 浏览器编辑画布以 alpha 表示删除强度，RGB 仅用于白色预览。
            mask = rgba.getchannel("A")
    except Exception as exc:
        raise CollageError("INVALID_MASK_PAYLOAD", "无法解码 mask PNG") from exc
    if mask.size != expected_size:
        raise CollageError("MASK_SIZE_MISMATCH", "界面保存的 mask 与工作画布尺寸不一致")
    return mask


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
    draft_path = draft_path.resolve()
    output_path = output_path.resolve()
    draft = validate_draft(read_json(draft_path))
    reference_path = resolve_input_path(draft_path, draft["source"]["path"])
    canvas_size = (draft["canvas"]["width"], draft["canvas"]["height"])
    if initial_mask_path is not None:
        initial_mask = load_mask(
            initial_mask_path, canvas_size, name="初始 remove_mask"
        )
        initial_mask_source = "provided_file"
        LOGGER.info("使用用户提供的初始清版蒙版 | path=%s", initial_mask_path)
    else:
        initial_mask = automatic_remove_mask(draft)
        initial_mask_source = "draft_rects"
    mask_bytes = io.BytesIO()
    initial_mask.save(mask_bytes, format="PNG")
    review_options = build_review_options(
        draft,
        slot_overrides_path,
        overlay_overrides_path,
        background_expand_px=background_expand_px,
        background_feather_px=background_feather_px,
    )
    review_options["initial_mask_source"] = initial_mask_source
    feather_count = sum(
        1
        for slot in draft["slots"]
        if slot["type"] == "image" and slot["mode"] == "photo_feather"
    )
    LOGGER.info(
        "确认页制作参数已准备 | slots=%s overlays=%s photo_feather=%s questions=%s",
        len(draft["slots"]),
        len(draft["overlays"]),
        feather_count,
        len(draft["questions"]),
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
                    HTTPStatus.OK, "text/html; charset=utf-8", HTML.encode("utf-8")
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
                    json.dumps(draft, ensure_ascii=False).encode("utf-8"),
                )
            elif self.path == "/review-options":
                self._send(
                    HTTPStatus.OK,
                    "application/json",
                    json.dumps(review_options, ensure_ascii=False).encode("utf-8"),
                )
            elif self.path == "/reference":
                self._send(HTTPStatus.OK, "image/png", reference_path.read_bytes())
            elif self.path == "/mask":
                self._send(HTTPStatus.OK, "image/png", mask_bytes.getvalue())
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
                edited = payload["draft"]
                # 重新绑定程序元数据，页面只能修改 Draft 的语义字段。
                for field in (
                    "version",
                    "status",
                    "source",
                    "canvas",
                    "prompt_version",
                    "provider",
                    "created_at",
                ):
                    edited[field] = draft[field]
                validate_draft(edited)
                slot_overrides = validate_override_map(
                    payload.get("slot_overrides", {}), source="确认页面 slot 覆盖"
                )
                overlay_overrides = validate_override_map(
                    payload.get("overlay_overrides", {}),
                    source="确认页面 overlay 覆盖",
                )
                validate_review_decisions(edited, slot_overrides, overlay_overrides)
                if payload.get("defer_questions") is True:
                    question_notes = ""
                else:
                    question_notes = question_resolution_notes(
                        draft["questions"], payload.get("question_resolutions", [])
                    )
                automatic_notes = automatic_review_notes(
                    draft,
                    slot_overrides,
                    overlay_overrides,
                    questions_deferred=payload.get("defer_questions") is True,
                )
                notes = "\n\n".join(
                    part for part in (question_notes, automatic_notes) if part
                )
                LOGGER.info(
                    "确认页自动策略已应用 | inferred_text=%s fallback_fonts=%s "
                    "approximate_overlays=%s deferred_questions=%s",
                    sum(
                        1
                        for source in draft["slots"]
                        if source["type"] == "text"
                        and source.get("default_text") is None
                        and slot_overrides.get(source["id"], {}).get("default_text")
                    ),
                    sum(
                        1
                        for source in draft["slots"]
                        if source["type"] == "text"
                        and slot_overrides.get(source["id"], {}).get(
                            "fallback_approved"
                        )
                        is True
                    ),
                    sum(
                        1
                        for source in draft["overlays"]
                        if source.get("requires_exact_content") is True
                        and overlay_overrides.get(source["id"], {}).get(
                            "requires_exact_content"
                        )
                        is False
                    ),
                    len(draft["questions"])
                    if payload.get("defer_questions") is True
                    else 0,
                )
                mask = _decode_mask(payload["mask_png"], canvas_size)
                if mask.getbbox() is None:
                    if payload.get("empty_mask_approved") is not True:
                        raise CollageError(
                            "EMPTY_REMOVE_MASK",
                            "删除蒙版为空；请画出需要清版的旧内容，或明确确认无需删除",
                        )
                    LOGGER.warning("模板作者明确接受空删除蒙版")
                else:
                    LOGGER.info("删除蒙版已确认 | bbox=%s", mask.getbbox())
                selected_expand_px = background_parameter(
                    payload,
                    "background_expand_px",
                    background_expand_px,
                )
                selected_feather_px = background_parameter(
                    payload,
                    "background_feather_px",
                    background_feather_px,
                )
                ui_draft_path = output_path.parent / "ui_confirmed_draft.json"
                ui_mask_path = output_path.parent / "remove_mask.png"
                atomic_write_json(ui_draft_path, edited)
                atomic_save_image(mask, ui_mask_path)
                confirm_draft(
                    ui_draft_path,
                    output_path,
                    remove_mask_path=ui_mask_path,
                    reviewer=reviewer,
                    allowed_mask_path=allowed_mask_path,
                    background_candidate_path=background_candidate_path,
                    slot_overrides_path=slot_overrides_path,
                    overlay_overrides_path=overlay_overrides_path,
                    slot_overrides_data=slot_overrides,
                    overlay_overrides_data=overlay_overrides,
                    background_expand_px=selected_expand_px,
                    background_feather_px=selected_feather_px,
                    notes=notes,
                )
                body = json.dumps(
                    {"ok": True, "path": str(output_path)}, ensure_ascii=False
                ).encode("utf-8")
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
