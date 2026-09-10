"""声明 provider 能力、返回值与显式插件加载约定。"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from PIL import Image

from ..core.errors import CollageError


@dataclass(frozen=True, slots=True)
class ProviderAudit:
    """不含密钥和原始图片数据的单次调用审计。"""

    name: str
    requested_model: str | None
    actual_model: str | None
    request_id: str | None
    fixture: bool
    elapsed_ms: int
    cache_hit: bool = False

    def as_dict(self, *, node: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "requested_model": self.requested_model,
            "actual_model": self.actual_model,
            "request_id": self.request_id,
            "fixture": self.fixture,
            "cache_hit": self.cache_hit,
            "elapsed_ms": self.elapsed_ms,
        }
        if node is not None:
            result = {"node": node, **result}
        return result


@dataclass(frozen=True, slots=True)
class ImageCapabilities:
    """图片 provider 必须主动声明的能力，不按厂商名称猜测。"""

    name: str
    requested_model: str | None
    supports_reference_image: bool
    supports_mask_edit: bool
    supports_transparency: bool
    mask_polarity: str
    output_sizes: tuple[tuple[int, int], ...] = field(default_factory=tuple)
    supports_request_status: bool = False
    fixture: bool = False


@dataclass(slots=True)
class GeneratedImage:
    """provider 的解码后图片和服务侧审计字段。"""

    image: Image.Image
    audit: ProviderAudit
    transform: dict[str, Any] | None = None


@runtime_checkable
class VisionProvider(Protocol):
    """VLM 只返回候选 Draft，不执行任何制作动作。"""

    name: str
    requested_model: str | None
    fixture: bool

    def analyze(
        self,
        reference_bytes: bytes,
        *,
        media_type: str,
        canvas: dict[str, Any],
        product_policy: dict[str, Any],
        prompt: str,
    ) -> tuple[dict[str, Any], ProviderAudit]: ...


@runtime_checkable
class ImageProvider(Protocol):
    """图片编辑 provider；制作期调用，运行端不依赖它。"""

    @property
    def capabilities(self) -> ImageCapabilities: ...

    def edit_background(
        self,
        reference: Image.Image,
        provider_mask: Image.Image,
        *,
        brief: str,
    ) -> GeneratedImage: ...

    def make_overlay(
        self,
        reference_crop: Image.Image,
        *,
        brief: str,
        background_mode: str,
        chroma_key: tuple[int, int, int] | None,
    ) -> GeneratedImage: ...


@runtime_checkable
class CutoutProvider(Protocol):
    """普通客户图抠图接口；调用前必须另行确认数据授权。"""

    name: str
    local_only: bool

    def cutout(
        self, customer_image: Image.Image
    ) -> tuple[Image.Image, ProviderAudit]: ...


def load_provider(spec: str, expected_protocol: type[Any]) -> Any:
    """从 ``module:object`` 显式加载 provider 类、工厂或实例。"""

    if ":" not in spec:
        raise CollageError(
            "INVALID_PROVIDER_SPEC", "provider 必须使用 module:object 格式"
        )
    module_name, object_name = spec.split(":", 1)
    try:
        module = importlib.import_module(module_name)
        target = getattr(module, object_name)
        instance = target() if callable(target) else target
    except (ImportError, AttributeError, TypeError) as exc:
        raise CollageError(
            "PROVIDER_LOAD_FAILED", f"无法加载 provider：{spec}"
        ) from exc
    if not isinstance(instance, expected_protocol):
        raise CollageError("INVALID_PROVIDER", f"provider 未实现约定接口：{spec}")
    return instance
