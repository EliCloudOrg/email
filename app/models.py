"""数据表定义。

延续 SSO 的约定：全 TEXT + ISO8601 UTC 字符串，JSON 以 TEXT 存，
因此 SQLite 换 PostgreSQL 时不需要改表结构。每个服务自己的数据库，
本服务只存验证请求与 callback outbox，不碰任何 SSO 用户数据。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .constants import ISO_FORMAT


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(ISO_FORMAT)


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, ISO_FORMAT).replace(tzinfo=timezone.utc)


def dumps_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def loads_json(raw: str | None) -> object:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


class Base(DeclarativeBase):
    pass


class VerificationRequest(Base):
    """一次邮箱确认请求。token 只存哈希，原文只在发信时使用。"""

    __tablename__ = "verification_requests"

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # ev_...
    email: Mapped[str] = mapped_column(Text, nullable=False)
    email_lower: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    callback_url: Mapped[str] = mapped_column(Text, nullable=False)
    # 属性不能叫 metadata（与 DeclarativeBase.metadata 冲突），列名仍为 metadata
    metadata_json: Mapped[str] = mapped_column("metadata", Text, nullable=False, default="{}")
    content_mode: Mapped[str] = mapped_column(Text, nullable=False)
    template_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    csrf_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[str] = mapped_column(Text, nullable=False)
    decided_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_ip: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_verification_status_expires", "status", "expires_at"),
        Index("ix_verification_email_created", "email_lower", "created_at"),
    )

    @property
    def metadata_dict(self) -> dict[str, object]:
        value = loads_json(self.metadata_json)
        return value if isinstance(value, dict) else {}


class CallbackDelivery(Base):
    """callback 的 outbox 行；同一 (request_id, status) 只会有一条。"""

    __tablename__ = "callback_deliveries"

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # dl_...
    request_id: Mapped[str] = mapped_column(
        Text, ForeignKey("verification_requests.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column("payload", Text, nullable=False)
    callback_url: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[str] = mapped_column(Text, nullable=False)
    last_attempt_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    failed_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("request_id", "status", name="uq_delivery_request_status"),
        Index("ix_delivery_next_attempt", "next_attempt_at"),
    )
