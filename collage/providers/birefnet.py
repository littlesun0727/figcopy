"""用本地 BiRefNet_lite-matting 为客户图片生成软主体 Alpha。"""

from __future__ import annotations

import importlib
import logging
import os
import re
import threading
import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from PIL import Image

from ..errors import CollageError
from .base import ProviderAudit

LOGGER = logging.getLogger(__name__)

DEFAULT_MODEL_ID = "ZhengPeng7/BiRefNet_lite-matting"
# 固定到已兼容 Transformers 5 的官方提交，避免远程自定义代码静默变化。
DEFAULT_MODEL_REVISION = "99c33412e3f58e1f33187abdc8c435c645243690"
DEFAULT_INPUT_SIZE = 1024
_CUDA_DEVICE_RE = re.compile(r"cuda(?::(\d+))?", re.IGNORECASE)
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}
_OPTIONAL_DEPENDENCIES = (
    "torch",
    "torchvision",
    "transformers",
    "timm",
    "kornia",
    "einops",
    "numpy",
    "safetensors",
)


def _boolean_env(name: str, default: bool = False) -> bool:
    """读取显式布尔环境变量，拒绝容易误解的拼写。"""

    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise CollageError(
        "BIREFNET_CONFIG_INVALID",
        f"{name} 必须是 true/false 或 1/0",
    )


def _model_label(source: str, *, is_local: bool) -> str:
    """审计本地模型时只记录目录名，避免泄露制作端绝对路径。"""

    if is_local:
        return f"local:{Path(source).name}"
    return source


@dataclass(frozen=True, slots=True)
class BiRefNetSettings:
    """BiRefNet 本地推理配置；默认值可由环境变量覆盖。"""

    model_source: str = DEFAULT_MODEL_ID
    model_revision: str | None = DEFAULT_MODEL_REVISION
    device: str = "auto"
    cache_dir: Path | None = None
    local_files_only: bool = False
    is_local_model: bool = False
    input_size: int = DEFAULT_INPUT_SIZE

    @property
    def model_label(self) -> str:
        return _model_label(self.model_source, is_local=self.is_local_model)

    @property
    def resolved_model_label(self) -> str:
        """返回可复现的实际模型标识，同时不暴露本地绝对路径。"""

        if self.model_revision is None:
            return self.model_label
        return f"{self.model_label}@{self.model_revision}"

    @classmethod
    def from_env(cls) -> BiRefNetSettings:
        """从环境读取模型 ID/本地目录、缓存位置和推理设备。"""

        model_path_value = os.environ.get("COLLAGE_BIREFNET_MODEL_PATH", "").strip()
        model_id = os.environ.get("COLLAGE_BIREFNET_MODEL", DEFAULT_MODEL_ID).strip()
        revision_value = os.environ.get(
            "COLLAGE_BIREFNET_REVISION", DEFAULT_MODEL_REVISION
        ).strip()
        device = os.environ.get("COLLAGE_BIREFNET_DEVICE", "auto").strip().lower()
        cache_value = os.environ.get("COLLAGE_BIREFNET_CACHE_DIR", "").strip()

        if device != "auto" and device != "cpu" and not _CUDA_DEVICE_RE.fullmatch(
            device
        ):
            raise CollageError(
                "BIREFNET_CONFIG_INVALID",
                "COLLAGE_BIREFNET_DEVICE 只支持 auto、cpu、cuda 或 cuda:N",
            )

        if model_path_value:
            model_path = Path(model_path_value).expanduser().resolve()
            if not model_path.is_dir():
                raise CollageError(
                    "BIREFNET_MODEL_NOT_FOUND",
                    "COLLAGE_BIREFNET_MODEL_PATH 指向的模型目录不存在",
                )
            model_source = str(model_path)
            model_revision = None
            is_local_model = True
            local_files_only = True
        else:
            if not model_id:
                raise CollageError(
                    "BIREFNET_CONFIG_INVALID",
                    "COLLAGE_BIREFNET_MODEL 不能为空",
                )
            model_source = model_id
            model_revision = revision_value or None
            is_local_model = False
            local_files_only = _boolean_env(
                "COLLAGE_BIREFNET_LOCAL_FILES_ONLY", False
            )

        cache_dir = Path(cache_value).expanduser().resolve() if cache_value else None
        return cls(
            model_source=model_source,
            model_revision=model_revision,
            device=device,
            cache_dir=cache_dir,
            local_files_only=local_files_only,
            is_local_model=is_local_model,
        )


class _MattingBackend(Protocol):
    """隔离重型推理依赖，便于延迟加载和无模型单元测试。"""

    device: str
    dtype_name: str

    def predict_alpha(self, image: Image.Image) -> Image.Image: ...


def _dependency_error(module_name: str, exc: BaseException) -> CollageError:
    return CollageError(
        "BIREFNET_DEPENDENCY_MISSING",
        "BiRefNet 本地依赖不可用；请运行 python -m pip install -e \".[birefnet]\"",
        details={"module": module_name, "reason": type(exc).__name__},
    )


def _import_runtime_dependencies() -> dict[str, Any]:
    """仅在真正需要抠普通图片时导入 PyTorch 相关依赖。"""

    modules: dict[str, Any] = {}
    with warnings.catch_warnings():
        # Kornia 在新 PyTorch 导入阶段仍触发旧 JIT API 的弃用提示；不影响推理。
        warnings.filterwarnings(
            "ignore",
            message=r"`torch\.jit\.script` is deprecated.*",
            category=FutureWarning,
        )
        for module_name in _OPTIONAL_DEPENDENCIES:
            try:
                modules[module_name] = importlib.import_module(module_name)
            except (ImportError, OSError, RuntimeError) as exc:
                raise _dependency_error(module_name, exc) from exc
    return modules


def _safe_failure_reason(exc: BaseException, *private_paths: str) -> str:
    """保留可调试原因，同时从异常文本中移除可能出现的本地模型路径。"""

    reason = " ".join(str(exc).split())
    replacements = {
        str(Path.home()): "<home>",
        str(Path.cwd()): "<workspace>",
        **{value: "<configured-path>" for value in private_paths if value},
    }
    # 先替换长路径，避免其父目录提前匹配后留下敏感后缀。
    for path_value in sorted(replacements, key=len, reverse=True):
        reason = reason.replace(path_value, replacements[path_value])
    return reason[:400] or type(exc).__name__


def _select_device(torch: Any, requested: str) -> str:
    """解析设备并在显式请求的 CUDA 不可用时尽早失败。"""

    if requested == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"
    if requested == "cpu":
        return "cpu"

    match = _CUDA_DEVICE_RE.fullmatch(requested)
    if match is None or not torch.cuda.is_available():
        raise CollageError(
            "BIREFNET_DEVICE_UNAVAILABLE",
            f"请求的 BiRefNet 设备不可用：{requested}",
        )
    index_value = match.group(1)
    if index_value is not None and int(index_value) >= torch.cuda.device_count():
        raise CollageError(
            "BIREFNET_DEVICE_UNAVAILABLE",
            f"请求的 CUDA 设备不存在：{requested}",
        )
    return requested


class _TorchBiRefNetBackend:
    """封装官方 Transformers 权重加载、预处理和 Alpha 后处理。"""

    def __init__(self, settings: BiRefNetSettings) -> None:
        started = time.perf_counter()
        modules = _import_runtime_dependencies()
        self._torch = modules["torch"]
        self._numpy = modules["numpy"]
        transformers = modules["transformers"]
        self._input_size = settings.input_size
        self.device = _select_device(self._torch, settings.device)
        self._dtype = (
            self._torch.float16 if self.device.startswith("cuda") else self._torch.float32
        )
        self.dtype_name = "float16" if self._dtype == self._torch.float16 else "float32"

        load_options: dict[str, Any] = {
            "trust_remote_code": True,
            "local_files_only": settings.local_files_only,
        }
        if settings.model_revision is not None:
            load_options["revision"] = settings.model_revision
        if settings.cache_dir is not None:
            settings.cache_dir.mkdir(parents=True, exist_ok=True)
            load_options["cache_dir"] = str(settings.cache_dir)

        LOGGER.info(
            "加载本地 BiRefNet 模型 | model=%s revision=%s device=%s",
            settings.model_label,
            settings.model_revision or "local",
            self.device,
        )
        try:
            model_class = transformers.AutoModelForImageSegmentation
            model = model_class.from_pretrained(settings.model_source, **load_options)
            # CPU 上强制 FP32，避免部分算子不支持半精度；CUDA 使用 FP16 节省显存。
            self._model = model.to(device=self.device, dtype=self._dtype).eval()
        except CollageError:
            raise
        except Exception as exc:
            raise CollageError(
                "BIREFNET_MODEL_LOAD_FAILED",
                "BiRefNet 模型加载失败",
                details={
                    "model": settings.model_label,
                    "reason": _safe_failure_reason(
                        exc,
                        settings.model_source,
                        str(settings.cache_dir) if settings.cache_dir else "",
                    ),
                    "exception": type(exc).__name__,
                },
            ) from exc
        LOGGER.info(
            "BiRefNet 模型加载完成 | device=%s dtype=%s elapsed_ms=%s",
            self.device,
            self.dtype_name,
            round((time.perf_counter() - started) * 1000),
        )

    def _input_tensor(self, image: Image.Image) -> Any:
        """按官方 1024 方形输入和 ImageNet 均值方差构造张量。"""

        resized = image.convert("RGB").resize(
            (self._input_size, self._input_size), Image.Resampling.BILINEAR
        )
        array = self._numpy.asarray(resized, dtype=self._numpy.float32) / 255.0
        tensor = self._torch.from_numpy(array).permute(2, 0, 1).unsqueeze(0)
        mean = self._torch.tensor((0.485, 0.456, 0.406)).view(1, 3, 1, 1)
        std = self._torch.tensor((0.229, 0.224, 0.225)).view(1, 3, 1, 1)
        normalized = (tensor - mean) / std
        return normalized.to(device=self.device, dtype=self._dtype)

    @staticmethod
    def _prediction_tensor(output: Any) -> Any:
        """取得官方 BiRefNet 最后一层预测，同时容忍标准 ModelOutput。"""

        value = output
        if hasattr(value, "logits"):
            value = value.logits
        while isinstance(value, (list, tuple)):
            if not value:
                raise CollageError(
                    "BIREFNET_INVALID_OUTPUT", "BiRefNet 返回了空预测列表"
                )
            value = value[-1]
        if not hasattr(value, "sigmoid"):
            raise CollageError(
                "BIREFNET_INVALID_OUTPUT", "BiRefNet 返回值中没有可用预测张量"
            )
        return value

    def predict_alpha(self, image: Image.Image) -> Image.Image:
        started = time.perf_counter()
        LOGGER.info(
            "BiRefNet 推理开始 | input=%sx%s model_input=%sx%s device=%s",
            image.width,
            image.height,
            self._input_size,
            self._input_size,
            self.device,
        )
        try:
            input_tensor = self._input_tensor(image)
            with self._torch.inference_mode():
                output = self._model(input_tensor)
                alpha = self._prediction_tensor(output).sigmoid()
            alpha = alpha.detach().float().cpu().squeeze()
            if alpha.ndim != 2 or not bool(self._torch.isfinite(alpha).all().item()):
                raise CollageError(
                    "BIREFNET_INVALID_OUTPUT",
                    "BiRefNet 返回的 Alpha 维度或数值无效",
                    details={"shape": list(alpha.shape)},
                )
            alpha_array = (
                alpha.clamp(0, 1).mul(255).round().to(self._torch.uint8).numpy()
            )
            alpha_image = Image.fromarray(alpha_array).resize(
                image.size, Image.Resampling.BILINEAR
            )
        except CollageError:
            raise
        except Exception as exc:
            raise CollageError(
                "BIREFNET_INFERENCE_FAILED",
                "BiRefNet 抠图推理失败",
                details={
                    "reason": _safe_failure_reason(exc, ""),
                    "exception": type(exc).__name__,
                },
            ) from exc
        LOGGER.info(
            "BiRefNet 推理完成 | elapsed_ms=%s alpha_range=%s",
            round((time.perf_counter() - started) * 1000),
            alpha_image.getextrema(),
        )
        return alpha_image


class BiRefNetLiteMattingProvider:
    """延迟加载并复用官方 BiRefNet_lite-matting 的本地 provider。"""

    name = "local-birefnet-lite-matting"
    local_only = True

    def __init__(
        self,
        settings: BiRefNetSettings | None = None,
        *,
        backend_factory: Callable[[BiRefNetSettings], _MattingBackend] | None = None,
    ) -> None:
        self._settings = settings
        self._backend_factory = backend_factory or _TorchBiRefNetBackend
        self._backend: _MattingBackend | None = None
        self._load_lock = threading.RLock()

    @property
    def settings(self) -> BiRefNetSettings:
        """延迟读取配置，使已有 Alpha 的 PNG 不依赖模型配置。"""

        with self._load_lock:
            if self._settings is None:
                self._settings = BiRefNetSettings.from_env()
            return self._settings

    def _get_backend(self) -> _MattingBackend:
        if self._backend is None:
            with self._load_lock:
                if self._backend is None:
                    self._backend = self._backend_factory(self.settings)
        return self._backend

    def cutout(
        self, customer_image: Image.Image
    ) -> tuple[Image.Image, ProviderAudit]:
        """返回与客户原图对齐的单通道软 Alpha，不上传图片。"""

        started = time.perf_counter()
        backend = self._get_backend()
        subject_alpha = backend.predict_alpha(customer_image.convert("RGB")).convert("L")
        if subject_alpha.size != customer_image.size:
            raise CollageError(
                "MASK_SIZE_MISMATCH",
                "BiRefNet 返回的 subject_alpha 与客户图尺寸不一致",
                details={
                    "expected": customer_image.size,
                    "actual": subject_alpha.size,
                },
            )
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        return subject_alpha, ProviderAudit(
            name=self.name,
            requested_model=self.settings.model_label,
            actual_model=self.settings.resolved_model_label,
            request_id=None,
            fixture=False,
            elapsed_ms=elapsed_ms,
        )
