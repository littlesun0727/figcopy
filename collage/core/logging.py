"""提供统一、无敏感载荷的阶段日志格式。"""

from __future__ import annotations

import logging
import sys

from .privacy import safe_text


class _SafeFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return safe_text(super().format(record))


# 第三方 HTTP debug 可能包含临时授权头；Pillow/filelock 的逐块日志也会淹没阶段信息。
_NOISY_LOGGERS = (
    "PIL",
    "filelock",
    "hf_xet",
    "httpcore",
    "httpx",
    "huggingface_hub",
    "urllib3",
)


def configure_logging(verbose: bool = False) -> None:
    """初始化 CLI 日志；重复调用不会叠加 handler。"""

    # Windows 旧代码页会让中文阶段日志变成乱码；支持时统一终端流为 UTF-8。
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except OSError:
                pass
    level = logging.DEBUG if verbose else logging.INFO
    logging.getLogger("collage").setLevel(level)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    for handler in logging.getLogger().handlers:
        handler.setFormatter(
            _SafeFormatter(
                "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
                datefmt="%H:%M:%S",
            )
        )
    for logger_name in _NOISY_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)
