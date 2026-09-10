"""导出 VLM、图片编辑与抠图 provider 的最小可插拔接口。"""

from .base import (
    CutoutProvider,
    GeneratedImage,
    ImageCapabilities,
    ImageProvider,
    ProviderAudit,
    VisionProvider,
    load_provider,
)
from .birefnet import BiRefNetLiteMattingProvider, BiRefNetSettings
from .fixture import DeterministicFixtureImageProvider
from .yibu import YibuImageProvider, YibuSettings, YibuVisionProvider

__all__ = [
    "BiRefNetLiteMattingProvider",
    "BiRefNetSettings",
    "CutoutProvider",
    "DeterministicFixtureImageProvider",
    "GeneratedImage",
    "ImageCapabilities",
    "ImageProvider",
    "ProviderAudit",
    "VisionProvider",
    "YibuImageProvider",
    "YibuSettings",
    "YibuVisionProvider",
    "load_provider",
]
