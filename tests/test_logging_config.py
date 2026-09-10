"""验证 verbose 模式不会打开可能泄露请求头的第三方底层日志。"""

from __future__ import annotations

import logging

from collage.core.logging import configure_logging


def test_verbose_keeps_project_debug_but_suppresses_noisy_http_logs() -> None:
    configure_logging(True)

    assert logging.getLogger("collage.providers.birefnet").getEffectiveLevel() <= logging.DEBUG
    assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("PIL").getEffectiveLevel() == logging.WARNING
