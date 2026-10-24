"""业务逻辑：创建验证、确认页、批准/拒绝、过期、清理。"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from . import constants as c
from .config import Settings
from .content import ContentError, describe_content_mode, render_email
from .errors import ServiceError
from .mailer import MailDeliveryError, Mailer
from .models import CallbackDelivery, VerificationRequest, dumps_json, parse_iso, to_iso, utcnow
from .schemas import EmailContent, VerifyCreate, VerifyStatus
from .security import constant_time_equals, new_csrf_token, new_token, sha256_hex
from .urls import validate_callback_url

logger = logging.getLogger("email.service")

MAX_ID_LEN = 64


def _new_request_id() -> str:
    return f"ev_{secrets.token_hex(12)}"


def _new_delivery_id() -> str:
    return f"dl_{secrets.token_hex(12)}"


def _email_lower(email: str) -> str:
    return email.strip().lower()


def _active_count(session: Session, email_lower: str, now_iso: str) -> int:
    return session.scalar(
        select(func.count())
        .select_from(VerificationRequest)
        .where(
            VerificationRequest.email_lower == email_lower,
            VerificationRequest.status == c.STATUS_PENDING,
            VerificationRequest.expires_at > now_iso,
        )
    ) or 0


def _check_create_limits(
    session: Session, settings: Settings, email_lower: str, now: datetime
) -> None:
    now_iso = to_iso(now)
    active = _active_count(session, email_lower, now_iso)
    if active >= settings.max_active_per_email:
        raise ServiceError(
            429,
            "too_many_pending",
            f"该邮箱已有 {active} 个待确认请求，请稍后再试",
            headers={"Retry-After": "60"},
        )
    if settings.create_cooldown_seconds > 0:
        latest_raw = session.scalar(
            select(func.max(VerificationRequest.created_at)).where(
                VerificationRequest.email_lower == email_lower
            )
        )
        if latest_raw:
            latest = parse_iso(latest_raw)
            elapsed = (now - latest).total_seconds()
            if elapsed < settings.create_cooldown_seconds:
                retry_after = int(settings.create_cooldown_seconds - elapsed) + 1
                raise ServiceError(
                    429,
                    "rate_limited",
                    "创建过于频繁，请稍后再试",
                    headers={"Retry-After": str(retry_after)},
                )


def create_verification(
    session: Session,
    settings: Settings,
    mailer: Mailer,
    payload: VerifyCreate,
) -> VerificationRequest:
    now = utcnow()
    now_iso = to_iso(now)
    email = str(payload.email).strip()
    email_lower = _email_lower(email)
    purpose = (payload.purpose or "generic").strip()
    source = payload.source.strip() if payload.source else None
    expires_in = payload.expires_in or settings.verification_ttl_seconds
    expires_at = now + timedelta(seconds=expires_in)

    callback_url = validate_callback_url(settings, payload.callback_url)
    metadata_json = dumps_json(payload.metadata)
    if len(metadata_json.encode("utf-8")) > settings.metadata_max_bytes:
        raise ServiceError(
            400,
            "metadata_too_large",
            f"metadata 不能超过 {settings.metadata_max_bytes} 字节",
        )

    content = payload.content or EmailContent()
    try:
        content_mode, template_name = describe_content_mode(settings, content, purpose)
    except ContentError as exc:
        raise ServiceError(400, "invalid_content", str(exc)) from exc

    _check_create_limits(session, settings, email_lower, now)

    request_id = _new_request_id()
    token = new_token()
    try:
        rendered = render_email(
            settings,
            request_id=request_id,
            token=token,
            email=email,
            purpose=purpose,
            source=source,
            content=content,
            created_at=now,
            expires_at=expires_at,
        )
    except ContentError as exc:
        raise ServiceError(400, "invalid_content", str(exc)) from exc

    record = VerificationRequest(
        id=request_id,
        email=email,
        email_lower=email_lower,
        purpose=purpose,
        source=source,
        callback_url=callback_url,
        metadata_json=metadata_json,
        content_mode=content_mode,
        template_name=template_name,
        token_hash=sha256_hex(token),
        status=c.STATUS_PENDING,
        csrf_hash=None,
        failure_reason=None,
        created_at=now_iso,
        expires_at=to_iso(expires_at),
        decided_at=None,
        decided_ip=None,
    )
    session.add(record)
    session.commit()

    try:
        mailer.send(email, rendered)
    except MailDeliveryError as exc:
        record.status = c.STATUS_FAILED
        record.failure_reason = f"mail delivery failed: {exc}"[:500]
        session.commit()
        logger.warning("邮件发送失败 request=%s err=%s", request_id, type(exc).__name__)
        raise ServiceError(
            502,
            "email_delivery_failed",
            "确认邮件发送失败，请稍后重试",
        ) from exc

    logger.info(
        "verification created id=%s purpose=%s mode=%s ttl=%s",
        request_id,
        purpose,
        content_mode,
        expires_in,
    )
    return record


def get_request_by_id(session: Session, request_id: str) -> VerificationRequest:
    record = session.get(VerificationRequest, request_id.strip())
    if record is None:
        raise ServiceError(404, "not_found", "验证请求不存在")
    return record


def get_request_by_token(session: Session, token: str) -> VerificationRequest:
    token_hash = sha256_hex(token.strip())
    record = session.scalar(
        select(VerificationRequest).where(VerificationRequest.token_hash == token_hash)
    )
    if record is None:
        raise ServiceError(404, "invalid_token", "确认链接无效或已过期")
    return record


def _expire_record(
    session: Session, settings: Settings, record: VerificationRequest, now: datetime
) -> bool:
    """把仍在 pending 且已过期的记录原子地改成 expired；成功返回 True。"""
    now_iso = to_iso(now)
    result = session.execute(
        update(VerificationRequest)
        .where(
            VerificationRequest.id == record.id,
            VerificationRequest.status == c.STATUS_PENDING,
            VerificationRequest.expires_at <= now_iso,
        )
        .values(status=c.STATUS_EXPIRED, decided_at=now_iso, csrf_hash=None)
    )
    if result.rowcount == 0:
        return False
    if settings.callback_on_expiry:
        enqueue_callback(session, settings, record, c.STATUS_EXPIRED, now=now)
    return True


def confirm_page(
    session: Session, settings: Settings, token: str
) -> tuple[str, VerificationRequest, str | None]:
    """返回 (state, record, csrf)。state: pending/approved/denied/expired/failed。"""
    record = get_request_by_token(session, token)
    now = utcnow()
    if record.status != c.STATUS_PENDING:
        return record.status, record, None
    if record.expires_at <= to_iso(now):
        _expire_record(session, settings, record, now)
        session.commit()
        session.refresh(record)
        return record.status, record, None
    csrf = new_csrf_token()
    record.csrf_hash = sha256_hex(csrf)
    session.commit()
    return c.STATUS_PENDING, record, csrf


def decide(
    session: Session,
    settings: Settings,
    token: str,
    decision: str,
    csrf: str,
    decided_ip: str | None,
) -> tuple[str, VerificationRequest]:
    choice = decision.strip().lower()
    if choice not in {"approve", "deny"}:
        raise ServiceError(400, "invalid_decision", "decision 只允许 approve 或 deny")
    target = c.STATUS_APPROVED if choice == "approve" else c.STATUS_DENIED
    record = get_request_by_token(session, token)
    now = utcnow()
    now_iso = to_iso(now)

    if record.status != c.STATUS_PENDING:
        return record.status, record
    if record.expires_at <= now_iso:
        _expire_record(session, settings, record, now)
        session.commit()
        session.refresh(record)
        return record.status, record

    if not record.csrf_hash or not constant_time_equals(
        record.csrf_hash, sha256_hex(csrf.strip())
    ):
        raise ServiceError(400, "invalid_csrf", "表单校验失败，请刷新页面后重试")

    result = session.execute(
        update(VerificationRequest)
        .where(
            VerificationRequest.id == record.id,
            VerificationRequest.status == c.STATUS_PENDING,
            VerificationRequest.expires_at > now_iso,
        )
        .values(
            status=target,
            decided_at=now_iso,
            decided_ip=decided_ip,
            csrf_hash=None,
        )
    )
    if result.rowcount == 0:
        session.rollback()
        fresh = session.get(VerificationRequest, record.id)
        if fresh is not None and fresh.status == c.STATUS_PENDING:
            _expire_record(session, settings, fresh, now)
            session.commit()
            session.refresh(fresh)
            return fresh.status, fresh
        if fresh is not None:
            return fresh.status, fresh
        raise ServiceError(404, "not_found", "验证请求不存在")

    enqueue_callback(session, settings, record, target, now=now)
    session.commit()
    logger.info("verification decided id=%s status=%s", record.id, target)
    return target, record


def expire_due(session: Session, settings: Settings, now: datetime | None = None) -> int:
    moment = now or utcnow()
    now_iso = to_iso(moment)
    records = list(
        session.scalars(
            select(VerificationRequest)
            .where(
                VerificationRequest.status == c.STATUS_PENDING,
                VerificationRequest.expires_at <= now_iso,
            )
            .limit(100)
        )
    )
    expired = 0
    for record in records:
        if _expire_record(session, settings, record, moment):
            expired += 1
    if expired:
        session.commit()
        logger.info("expired %s verification request(s)", expired)
    return expired


def cleanup(session: Session, settings: Settings, now: datetime | None = None) -> int:
    if settings.retention_days <= 0:
        return 0
    moment = now or utcnow()
    cutoff = to_iso(moment - timedelta(days=settings.retention_days))
    ids = list(
        session.scalars(
            select(VerificationRequest.id).where(
                VerificationRequest.status.in_(tuple(c.TERMINAL_STATUSES)),
                func.coalesce(VerificationRequest.decided_at, VerificationRequest.created_at)
                < cutoff,
            )
        )
    )
    if not ids:
        return 0
    session.execute(delete(CallbackDelivery).where(CallbackDelivery.request_id.in_(ids)))
    session.execute(delete(VerificationRequest).where(VerificationRequest.id.in_(ids)))
    session.commit()
    logger.info("cleaned up %s old verification request(s)", len(ids))
    return len(ids)


def enqueue_callback(
    session: Session,
    settings: Settings,
    record: VerificationRequest,
    status: str,
    *,
    now: datetime | None = None,
) -> CallbackDelivery:
    """为某个终态入队一条 callback；同一 (request_id, status) 幂等。"""
    existing = session.scalar(
        select(CallbackDelivery).where(
            CallbackDelivery.request_id == record.id,
            CallbackDelivery.status == status,
        )
    )
    if existing is not None:
        return existing

    moment = now or utcnow()
    payload = {
        "event": c.CALLBACK_EVENT,
        "request_id": record.id,
        "status": status,
        "email": record.email,
        "purpose": record.purpose,
        "source": record.source,
        "metadata": record.metadata_dict,
        "verified_at": to_iso(moment),
    }
    delivery = CallbackDelivery(
        id=_new_delivery_id(),
        request_id=record.id,
        status=status,
        payload_json=dumps_json(payload),
        callback_url=record.callback_url,
        attempts=0,
        next_attempt_at=to_iso(moment),
        last_attempt_at=None,
        last_status_code=None,
        last_error=None,
        delivered_at=None,
        failed_at=None,
        created_at=to_iso(moment),
    )
    session.add(delivery)
    return delivery


def status_for(session: Session, record: VerificationRequest) -> VerifyStatus:
    delivery = session.scalar(
        select(CallbackDelivery)
        .where(CallbackDelivery.request_id == record.id)
        .order_by(CallbackDelivery.created_at.desc())
        .limit(1)
    )
    delivery_info = None
    if delivery is not None:
        if delivery.delivered_at:
            state = c.DELIVERY_DELIVERED
        elif delivery.failed_at:
            state = c.DELIVERY_FAILED
        else:
            state = c.DELIVERY_PENDING
        delivery_info = {
            "status": state,
            "attempts": delivery.attempts,
            "last_status_code": delivery.last_status_code,
            "last_error": delivery.last_error,
            "delivered_at": delivery.delivered_at,
        }
    return VerifyStatus(
        id=record.id,
        status=record.status,
        email=record.email,
        purpose=record.purpose,
        source=record.source,
        metadata=record.metadata_dict,
        created_at=record.created_at,
        expires_at=record.expires_at,
        decided_at=record.decided_at,
        delivery=delivery_info,
    )
