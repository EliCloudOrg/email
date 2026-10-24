"""统一错误结构、进程内限流与客户端 IP 提取。"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from collections.abc import Iterable

from .config import Settings
from .security import normalize_ip


class ServiceError(Exception):
    """业务错误；routers 把它映射成 JSON 或 HTML 错误页。"""

    def __init__(
        self,
        status_code: int,
        error: str,
        description: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(description)
        self.status_code = status_code
        self.error = error
        self.description = description
        self.headers = headers or {}


class RateLimiter:
    """进程内滑动窗口。单容器单 worker 足够；多副本时再换 Redis。"""

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: float) -> float | None:
        """允许则返回 None；拒绝则返回建议的 Retry-After 秒数。"""
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                retry_after = max(0.0, events[0] + window_seconds - now)
                return max(1.0, retry_after)
            events.append(now)
            return None

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


limiter = RateLimiter()


def client_ip(request, settings: Settings) -> str | None:
    """Caddy 注入 X-Real-IP；本地/直连时用 socket 地址。"""
    if settings.trust_proxy_headers:
        real_ip = request.headers.get("x-real-ip")
        normalized = normalize_ip(real_ip)
        if normalized:
            return normalized
    if request.client:
        return normalize_ip(request.client.host) or request.client.host
    return None


def is_internal_source(ip: str | None) -> bool:
    """Caddy 之外的第二层判断（本地/测试用；生产由 Caddy remote_ip 拦）。"""
    normalized = normalize_ip(ip)
    if not normalized:
        return False
    import ipaddress

    address = ipaddress.ip_address(normalized)
    return address.is_private or address.is_loopback


def first_error(errors: Iterable[dict]) -> dict:
    return next(iter(errors), {})
