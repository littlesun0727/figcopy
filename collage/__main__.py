"""支持通过 ``python -m collage`` 调用命令行工具。"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":  # pragma: no cover - 由 Python 模块入口触发
    raise SystemExit(main())
