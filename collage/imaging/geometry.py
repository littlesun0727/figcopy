"""提供 canvas_px 坐标、等比补边和可逆矩形映射。"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image

from ..core.errors import CollageError


@dataclass(frozen=True, slots=True)
class AffineRectTransform:
    """只含缩放和平移的二维变换，可精确逆向映射几何坐标。"""

    source_size: tuple[int, int]
    target_size: tuple[int, int]
    scale_x: float
    scale_y: float
    offset_x: float
    offset_y: float
    content_size: tuple[int, int]

    def map_point(self, x: float, y: float) -> tuple[float, float]:
        return x * self.scale_x + self.offset_x, y * self.scale_y + self.offset_y

    def inverse_point(self, x: float, y: float) -> tuple[float, float]:
        return (x - self.offset_x) / self.scale_x, (y - self.offset_y) / self.scale_y

    def map_rect(
        self, rect: list[float] | tuple[float, float, float, float]
    ) -> list[float]:
        x, y, width, height = rect
        mapped_x, mapped_y = self.map_point(x, y)
        return [mapped_x, mapped_y, width * self.scale_x, height * self.scale_y]

    def inverse_rect(
        self, rect: list[float] | tuple[float, float, float, float]
    ) -> list[float]:
        x, y, width, height = rect
        source_x, source_y = self.inverse_point(x, y)
        return [source_x, source_y, width / self.scale_x, height / self.scale_y]

    def as_dict(self) -> dict[str, object]:
        return {
            "source_size": list(self.source_size),
            "target_size": list(self.target_size),
            "scale": [self.scale_x, self.scale_y],
            "offset": [self.offset_x, self.offset_y],
            "content_size": list(self.content_size),
        }


def contain_transform(
    source_size: tuple[int, int], target_size: tuple[int, int]
) -> AffineRectTransform:
    """计算不改变宽高比的 contain + 居中补边变换。"""

    source_width, source_height = source_size
    target_width, target_height = target_size
    if min(source_width, source_height, target_width, target_height) <= 0:
        raise CollageError("INVALID_IMAGE_SIZE", "源尺寸和目标尺寸必须大于 0")
    scale = min(target_width / source_width, target_height / source_height)
    content_width = max(1, round(source_width * scale))
    content_height = max(1, round(source_height * scale))
    offset_x = (target_width - content_width) // 2
    offset_y = (target_height - content_height) // 2
    # 使用实际整数内容尺寸计算轴向比例，保证 map/inverse 与真实像素区域一致。
    return AffineRectTransform(
        source_size=source_size,
        target_size=target_size,
        scale_x=content_width / source_width,
        scale_y=content_height / source_height,
        offset_x=float(offset_x),
        offset_y=float(offset_y),
        content_size=(content_width, content_height),
    )


def pad_for_model(
    image: Image.Image,
    target_size: tuple[int, int],
    *,
    fill: tuple[int, int, int, int] = (0, 0, 0, 0),
) -> tuple[Image.Image, AffineRectTransform]:
    """等比缩放并补边，返回供审计和回裁的变换。"""

    transform = contain_transform(image.size, target_size)
    resized = image.resize(transform.content_size, Image.Resampling.LANCZOS)
    output = Image.new("RGBA", target_size, fill)
    output.alpha_composite(
        resized.convert("RGBA"), (round(transform.offset_x), round(transform.offset_y))
    )
    return output, transform


def restore_from_model(
    image: Image.Image, transform: AffineRectTransform
) -> Image.Image:
    """按记录的补边区域回裁，拒绝未知尺寸而不进行无声拉伸。"""

    if image.size != transform.target_size:
        raise CollageError(
            "PROVIDER_SIZE_MISMATCH",
            "provider 返回尺寸与已记录目标尺寸不一致，无法安全回裁",
            details={"expected": transform.target_size, "actual": image.size},
        )
    left = round(transform.offset_x)
    top = round(transform.offset_y)
    width, height = transform.content_size
    content = image.crop((left, top, left + width, top + height))
    return content.resize(transform.source_size, Image.Resampling.LANCZOS)
