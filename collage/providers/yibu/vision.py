"""Route the shared Draft protocol through the existing Yibu audit proxy."""

from ..chat import (
    ChatVisionProvider,
    _assistant_text,  # noqa: F401 -- retained imports for existing integrations
    _chat_finish_reason,  # noqa: F401
    _parse_json_object,  # noqa: F401
)
from .client import _YibuAuditClient
from .settings import YibuSettings


class YibuVisionProvider(ChatVisionProvider):
    """Keep Yibu credentials, defaults, payload and audited transport unchanged."""

    name = "yibu-audit-vlm"

    def __init__(self, settings: YibuSettings | None = None) -> None:
        self.settings = settings or YibuSettings.from_env()
        self.requested_model = self.settings.vlm_model
        self._client = _YibuAuditClient(self.settings)
