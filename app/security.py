"""token、HMAC 与常量时间比较。"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
from datetime import datetime, timezone


def new_token() -> str:
    """确认链接里的高熵一次性 token。"""
    return secrets.token_urlsafe(32)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def sign_callback(secret: str, timestamp: int, body: bytes) -> str:
    """签名覆盖 ``timestamp + "." + raw_body``，防重放/篡改。"""
    message = f"{timestamp}.".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_callback(secret: str, timestamp: int, body: bytes, signature: str) -> bool:
    expected = sign_callback(secret, timestamp, body)
    received = signature.removeprefix("sha256=").strip()
    return hmac.compare_digest(expected, received)


def parse_bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = value.strip()
    return token or None


def normalize_ip(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip()
    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1 : candidate.index("]")]
    candidate = candidate.split("%", 1)[0]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def utc_timestamp(moment: datetime | None = None) -> int:
    return int((moment or datetime.now(timezone.utc)).timestamp())
