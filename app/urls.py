"""对外 URL 校验（callback SSRF 白名单）。"""

from __future__ import annotations

from urllib.parse import urlsplit

from .config import Settings
from .errors import ServiceError


def validate_callback_url(settings: Settings, value: str) -> str:
    """只允许 http/https + host 白名单；防 SSRF。"""
    raw = value.strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"}:
        raise ServiceError(400, "invalid_callback_url", "callback_url 只允许 http/https")
    if parsed.username or parsed.password:
        raise ServiceError(400, "invalid_callback_url", "callback_url 不能包含用户信息")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ServiceError(400, "invalid_callback_url", "callback_url 缺少主机名")
    if host not in settings.callback_allowed_host_set:
        raise ServiceError(
            400,
            "callback_not_allowed",
            f"callback host {host!r} 不在 CALLBACK_ALLOWED_HOSTS 内",
        )
    if parsed.fragment:
        raise ServiceError(400, "invalid_callback_url", "callback_url 不能包含 fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ServiceError(400, "invalid_callback_url", "callback_url 端口不合法") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ServiceError(400, "invalid_callback_url", "callback_url 端口不合法")
    return raw
