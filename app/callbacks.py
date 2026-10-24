"""Callback outbox：签名、重试与投递。

投递语义是 at-least-once；调用方按 ``Idempotency-Key``（request_id:status）去重。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import __version__, constants as c
from .config import Settings
from .errors import ServiceError
from .models import CallbackDelivery, to_iso, utcnow
from .security import sign_callback
from .urls import validate_callback_url

logger = logging.getLogger("email.callbacks")


def _delivery_headers(
    secret: str,
    delivery: CallbackDelivery,
    payload: dict,
    timestamp: int,
    body: bytes,
) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "User-Agent": f"elicloud-email/{__version__}",
        "X-EliCloud-Event": c.CALLBACK_EVENT,
        "X-EliCloud-Timestamp": str(timestamp),
        "X-EliCloud-Delivery-Id": delivery.id,
        "X-EliCloud-Signature": f"sha256={sign_callback(secret, timestamp, body)}",
        "Idempotency-Key": f"{delivery.request_id}:{delivery.status}",
    }


def _mark_failed(
    delivery: CallbackDelivery, now_iso: str, error: str
) -> None:
    delivery.attempts += 1
    delivery.last_attempt_at = now_iso
    delivery.last_status_code = None
    delivery.last_error = error[:500]
    delivery.failed_at = now_iso


def deliver_due(
    session: Session,
    settings: Settings,
    http_client: httpx.Client,
    *,
    now: datetime | None = None,
    limit: int = 20,
) -> dict[str, int]:
    """投递所有到期的 callback；返回 {delivered, retrying, failed}。"""
    moment = now or utcnow()
    now_iso = to_iso(moment)
    due = list(
        session.scalars(
            select(CallbackDelivery)
            .where(
                CallbackDelivery.delivered_at.is_(None),
                CallbackDelivery.failed_at.is_(None),
                CallbackDelivery.next_attempt_at <= now_iso,
            )
            .order_by(CallbackDelivery.next_attempt_at)
            .limit(limit)
        )
    )
    result = {"delivered": 0, "retrying": 0, "failed": 0}
    if not due:
        return result

    schedule = settings.retry_schedule_seconds
    timestamp = int(moment.timestamp())

    for delivery in due:
        try:
            validate_callback_url(settings, delivery.callback_url)
        except ServiceError as exc:
            _mark_failed(delivery, now_iso, f"callback URL rejected: {exc.description}")
            result["failed"] += 1
            continue

        payload = json.loads(delivery.payload_json)
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        headers = _delivery_headers(
            settings.callback_signing_secret, delivery, payload, timestamp, body
        )

        response: httpx.Response | None = None
        error: str | None = None
        try:
            response = http_client.post(
                delivery.callback_url,
                content=body,
                headers=headers,
                timeout=settings.callback_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - 网络异常一律进入重试
            error = f"{type(exc).__name__}: {exc}"

        delivery.attempts += 1
        delivery.last_attempt_at = now_iso
        delivery.last_status_code = response.status_code if response is not None else None

        if response is not None and 200 <= response.status_code < 300:
            delivery.delivered_at = now_iso
            delivery.last_error = None
            result["delivered"] += 1
            logger.info(
                "callback delivered request=%s status=%s attempt=%s",
                delivery.request_id,
                delivery.status,
                delivery.attempts,
            )
            continue

        if error is None and response is not None:
            error = f"HTTP {response.status_code}"
        delivery.last_error = (error or "unknown error")[:500]

        if delivery.attempts >= settings.callback_max_attempts:
            delivery.failed_at = now_iso
            result["failed"] += 1
            logger.warning(
                "callback failed permanently request=%s status=%s attempts=%s",
                delivery.request_id,
                delivery.status,
                delivery.attempts,
            )
        else:
            index = min(delivery.attempts, len(schedule) - 1)
            delay = schedule[index]
            delivery.next_attempt_at = to_iso(moment + timedelta(seconds=delay))
            result["retrying"] += 1
            logger.info(
                "callback retry scheduled request=%s status=%s attempt=%s delay=%ss",
                delivery.request_id,
                delivery.status,
                delivery.attempts,
                delay,
            )

    session.commit()
    return result
