"""提供离线且确定性的测试图片 provider；产物始终标记为 fixture。"""

from __future__ import annotations

import time
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFilter

from .base import GeneratedImage, ImageCapabilities, ProviderAudit


class DeterministicFixtureImageProvider:
    """用于自动测试和链路演示，绝不代表真实清版或装饰生成效果。"""

    @property
    def capabilities(self) -> ImageCapabilities:
        return ImageCapabilities(
            name="deterministic-fixture",
            requested_model="fixture-v1",
            supports_reference_image=True,
            supports_mask_edit=True,
            supports_transparency=True,
            mask_polarity="white_edit",
            fixture=True,
        )

    def _audit(self, started: float) -> ProviderAudit:
        return ProviderAudit(
            name=self.capabilities.name,
            requested_model=self.capabilities.requested_model,
            actual_model="fixture-v1",
            request_id=f"fixture-{uuid4().hex[:12]}",
            fixture=True,
            elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
        )

    def edit_background(
        self,
        reference: Image.Image,
        provider_mask: Image.Image,
        *,
        brief: str,
    ) -> GeneratedImage:
        started = time.monotonic()
        # 用模糊图只验证 mask/保护合成链路；它不是语义清版。
        candidate = reference.convert("RGBA").filter(
            ImageFilter.GaussianBlur(radius=12)
        )
        return GeneratedImage(candidate, self._audit(started))

    def make_overlay(
        self,
        reference_crop: Image.Image,
        *,
        brief: str,
        background_mode: str,
        chroma_key: tuple[int, int, int] | None,
    ) -> GeneratedImage:
        started = time.monotonic()
        source = reference_crop.convert("RGBA")
        if background_mode == "alpha":
            alpha = Image.new("L", source.size, 0)
            draw = ImageDraw.Draw(alpha)
            inset = max(1, min(source.size) // 16)
            draw.rounded_rectangle(
                (inset, inset, source.width - inset - 1, source.height - inset - 1),
                radius=max(1, min(source.size) // 5),
                fill=255,
            )
            source.putalpha(alpha)
            output = source
        else:
            key = chroma_key or (255, 0, 255)
            output = Image.new("RGB", source.size, key)
            inset = max(1, min(source.size) // 16)
            cut = source.crop(
                (inset, inset, source.width - inset, source.height - inset)
            ).convert("RGB")
            output.paste(cut, (inset, inset))
        return GeneratedImage(output, self._audit(started))
