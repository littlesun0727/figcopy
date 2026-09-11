"""Persist bounded real-trial evidence and prevent ambiguous paid requests from replaying."""

from __future__ import annotations

import hashlib
import logging
import time
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from ..core.errors import CollageError
from ..core.io import atomic_write_bytes, atomic_write_json, read_json, stable_hash
from ..providers.yibu.inspection import YibuInspectionProvider
from ..providers.yibu.settings import YibuSettings

LOGGER = logging.getLogger(__name__)


class TrialVision:
    """Resume completed inspections only when all actual inputs still match."""

    def __init__(
        self,
        root: Path,
        settings: YibuSettings,
        *,
        max_calls: int = 18,
        max_seconds: int = 3600,
    ):
        self.root, self.settings = root, settings
        self.max_calls, self.max_seconds = max_calls, max_seconds
        self.started = time.monotonic()
        existing = [read_json(p) for p in (root / "requests").glob("*.json")]
        first = min(
            (datetime.fromisoformat(r["started_at"]) for r in existing),
            default=datetime.now(UTC),
        )
        self.prior_seconds = max(0.0, (datetime.now(UTC) - first).total_seconds())
        self.provider = YibuInspectionProvider(settings)

    def inspect(
        self,
        name: str,
        images: list[tuple[str, Image.Image]],
        prompt: str,
        contract: str,
    ) -> dict:
        if not name.replace("_", "").replace("-", "").isalnum():
            raise CollageError("AUTO_NODE_INVALID", "试验节点名称无效")
        path = self.root / "requests" / (name + ".json")
        key = stable_hash(
            {
                "prompt": prompt,
                "contract": contract,
                "model": self.settings.vlm_model,
                "reasoning_effort": self.settings.vlm_reasoning_effort,
                "max_tokens": self.settings.vlm_max_tokens,
                "inputs": [
                    {
                        "label": label,
                        "size": im.size,
                        "pixels": hashlib.sha256(
                            im.convert("RGB").tobytes()
                        ).hexdigest(),
                    }
                    for label, im in images
                ],
            }
        )
        if path.exists():
            cached = read_json(path)
            if cached["input_sha256"] != key:
                raise CollageError(
                    "AUTO_NODE_INPUT_CHANGED", "已记录节点输入变化，请创建新节点或试验"
                )
            if cached["status"] != "complete":
                raise CollageError(
                    "AUTO_REQUEST_NOT_REPLAYED",
                    "既有请求未确认完成，禁止恢复时盲目重发",
                )
            LOGGER.info("复用真实检查证据 | node=%s", name)
            return cached["result"]
        if len(list((self.root / "requests").glob("*.json"))) >= self.max_calls:
            raise CollageError("AUTO_CALL_LIMIT", "真实试验达到调用次数上限")
        remaining = (
            self.max_seconds - self.prior_seconds - (time.monotonic() - self.started)
        )
        if remaining <= 0:
            raise CollageError("AUTO_TIME_LIMIT", "真实试验达到执行时长上限")
        record = {
            "node": name,
            "status": "running",
            "input_sha256": key,
            "started_at": datetime.now(UTC).isoformat(),
            "fixture": False,
            "request_parameters": {
                "model": self.settings.vlm_model,
                "reasoning_effort": self.settings.vlm_reasoning_effort,
                "max_tokens": self.settings.vlm_max_tokens,
                "timeout_seconds": max(
                    1, min(self.settings.timeout_seconds, int(remaining))
                ),
                "image_count": len(images),
            },
            "cost_cny": None,
            "cost_source": "not_reported",
        }
        atomic_write_json(path, record)
        atomic_write_bytes(
            path.with_suffix(".prompt.txt"),
            (prompt + "\n\n" + contract).encode("utf-8"),
        )
        LOGGER.info(
            "启动真实视觉检查 | node=%s model=%s reasoning=%s",
            name,
            self.settings.vlm_model,
            self.settings.vlm_reasoning_effort,
        )
        node_started = time.monotonic()
        try:
            self.provider._client.settings = replace(
                self.settings,
                timeout_seconds=max(
                    1, min(self.settings.timeout_seconds, int(remaining))
                ),
            )
            result, audit, usage = self.provider.inspect(
                images, prompt=prompt, contract=contract, operation=name
            )
            record.update(
                status="complete",
                result=result,
                audit=audit.as_dict(),
                usage=usage,
                fixture=audit.fixture,
                finished_at=datetime.now(UTC).isoformat(),
                elapsed_ms=round((time.monotonic() - node_started) * 1000),
            )
            atomic_write_json(path, record)
            LOGGER.info(
                "真实视觉检查完成 | node=%s elapsed_ms=%s", name, audit.elapsed_ms
            )
            return result
        except Exception as exc:
            record.update(
                status="failed_or_uncertain",
                error_code=getattr(exc, "code", "AUTO_EXECUTION_FAILED"),
                http_status=getattr(exc, "details", {}).get("http_status"),
                finished_at=datetime.now(UTC).isoformat(),
                elapsed_ms=round((time.monotonic() - node_started) * 1000),
            )
            atomic_write_json(path, record)
            raise


class PrivatePathFilter(logging.Filter):
    """Keep the command's private data locations out of ordinary stage logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = re.sub(
            r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s|]+",
            "<local-file>",
            record.getMessage(),
        )
        record.args = ()
        return True
