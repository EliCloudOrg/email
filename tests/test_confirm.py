from __future__ import annotations

import json
from datetime import timedelta

from app.db import session_scope
from app.models import CallbackDelivery, VerificationRequest, to_iso, utcnow

from conftest import (
    auth_headers,
    create_request,
    link_from_mailer,
    token_from_link,
)


def _prepare(client):
    response = create_request(client)
    assert response.status_code == 202
    return response.json()["id"], token_from_link(link_from_mailer(client))


def _delivery(request_id: str) -> CallbackDelivery:
    db = session_scope()
    try:
        return db.query(CallbackDelivery).filter_by(request_id=request_id).one()
    finally:
        db.close()


def test_confirm_link_approves_directly(client):
    request_id, token = _prepare(client)
    result = client.get("/v1/confirm", params={"token": token})
    assert result.status_code == 200
    assert result.text == "邮箱确认成功。"
    assert result.headers["content-type"].startswith("text/plain")

    db = session_scope()
    try:
        record = db.get(VerificationRequest, request_id)
        assert record.status == "approved"
        assert record.decided_by == "user"
        assert record.decided_at is not None
    finally:
        db.close()

    delivery = _delivery(request_id)
    payload = json.loads(delivery.payload_json)
    assert delivery.status == "approved"
    assert payload["status"] == "approved"
    assert payload["reason"] == "user_confirm"
    assert payload["decided_by"] == "user"

    status = client.get(f"/v1/verify/{request_id}", headers=auth_headers())
    assert status.status_code == 200
    data = status.json()
    assert data["status"] == "approved"
    assert data["decided_by"] == "user"
    assert data["delivery"]["status"] == "pending"


def test_confirm_link_is_idempotent(client):
    request_id, token = _prepare(client)
    first = client.get("/v1/confirm", params={"token": token})
    second = client.get("/v1/confirm", params={"token": token})
    assert first.status_code == 200
    assert second.status_code == 200
    db = session_scope()
    try:
        assert db.query(CallbackDelivery).filter_by(request_id=request_id).count() == 1
    finally:
        db.close()


def test_confirm_after_timeout_is_denied(client):
    request_id, token = _prepare(client)
    db = session_scope()
    try:
        record = db.get(VerificationRequest, request_id)
        record.expires_at = to_iso(utcnow() - timedelta(seconds=5))
        db.commit()
    finally:
        db.close()

    result = client.get("/v1/confirm", params={"token": token})
    assert result.status_code == 200
    assert result.text == "确认链接已超时，已视为拒绝。"
    assert result.headers["content-type"].startswith("text/plain")

    db = session_scope()
    try:
        record = db.get(VerificationRequest, request_id)
        assert record.status == "denied"
        assert record.decided_by == "timeout"
        assert record.decided_at is not None
    finally:
        db.close()

    delivery = _delivery(request_id)
    payload = json.loads(delivery.payload_json)
    assert delivery.status == "denied"
    assert payload["status"] == "denied"
    assert payload["reason"] == "timeout"
    assert payload["decided_by"] == "timeout"

    status = client.get(f"/v1/verify/{request_id}", headers=auth_headers())
    assert status.json()["status"] == "denied"
    assert status.json()["decided_by"] == "timeout"


def test_confirm_unknown_token(client):
    response = client.get("/v1/confirm", params={"token": "nope"})
    assert response.status_code == 404


def test_confirm_missing_token(client):
    response = client.get("/v1/confirm")
    assert response.status_code == 400


def test_confirm_post_is_gone(client):
    assert client.post("/v1/confirm", data={"whatever": "x"}).status_code == 405


def test_confirm_rate_limit(client):
    for _ in range(10):
        assert client.get("/v1/confirm", params={"token": "bogus"}).status_code == 404
    blocked = client.get("/v1/confirm", params={"token": "bogus"})
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers
