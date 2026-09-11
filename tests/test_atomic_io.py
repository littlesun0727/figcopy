"""Keep atomic writes valid near filesystem filename and Windows path limits."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from collage.core.io import atomic_write_json, read_json


def test_atomic_write_accepts_a_valid_long_target_name(tmp_path: Path) -> None:
    length = 245
    if os.name == "nt":
        # 目标本身在传统 260 字符限制内；临时文件不能再附加整段目标名。
        length = min(length, 252 - len(str(tmp_path.resolve())) - 1)
    if length < 32:
        pytest.skip("test parent path leaves insufficient filename space")
    target = tmp_path / ("x" * (length - 5) + ".json")
    atomic_write_json(target, {"state": "first"})
    atomic_write_json(target, {"state": "replaced"})
    assert read_json(target) == {"state": "replaced"}
    assert list(tmp_path.iterdir()) == [target]
