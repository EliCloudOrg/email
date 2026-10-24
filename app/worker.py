"""后台 worker：过期扫描 + callback 重试 + 定期清理。

单容器单 uvicorn worker；SQLite 单写者。多副本需要把 outbox/限流挪到共享存储。
"""

from __future__ import annotations

import logging
import threading
import time

import httpx

from .callbacks import deliver_due
from .config import Settings
from .db import get_session_factory
from .service import cleanup, expire_due

logger = logging.getLogger("email.worker")

CLEANUP_INTERVAL_SECONDS = 3600


def run_worker(settings: Settings, stop_event: threading.Event, http_client: httpx.Client) -> None:
    session_factory = get_session_factory()
    last_cleanup = 0.0
    logger.info("worker started interval=%ss", settings.worker_interval_seconds)
    while not stop_event.is_set():
        try:
            with session_factory() as session:
                expire_due(session, settings)
                deliver_due(session, settings, http_client)
                now = time.monotonic()
                if now - last_cleanup >= CLEANUP_INTERVAL_SECONDS:
                    cleanup(session, settings)
                    last_cleanup = now
        except Exception:  # noqa: BLE001 - worker 不能因为单次异常退出
            logger.exception("worker iteration failed")
        if stop_event.wait(settings.worker_interval_seconds):
            break
    logger.info("worker stopped")
