from __future__ import annotations

import json
from datetime import timedelta

import httpx

from app import constants as c
from app.callbacks import deliver_due
from app.db import session_scope
from app.models import CallbackDelivery, VerificationRequest, to_iso, utcnow
from app.security import verify_callback
from app.service import expire_due

from conftest import (
    TEST_SECRET,
    auth_headers,
    create_request,
    csrf_from_page,
    link_from_mailer,
    token_from_link,
)


def _approve(client):
    response = create_request(client)
    assert response.status_code == 202
    request_id = response.json()["id"]
    token = token_from_link(link_from_mailer(client))
    csrf = csrf_from_page(client.get("/v1/confirm", params={"token": token}).text)
    result = client.post(
        "/v1/confirm", data={"token": token, "decision": "approve", "csrf": csrf}
    )
    assert result.status_code == 200
    return request_id


def _delivery(request_id):
    db = session_scope()
    try:
        return db.query(CallbackDelivery).filter_by(request_id=request_id).one()
    finally:
        db.close()


def _isolate(request_id: str) -> None:
    """把其它测试遗留的 pending delivery 推到未来，避免 MockTransport 串台。"""
    db = session_scope()
    try:
        db.query(CallbackDelivery).filter(
            CallbackDelivery.request_id != request_id
        ).update(
            {CallbackDelivery.next_attempt_at: "2099-01-01T00:00:00Z"},
            synchronize_session=False,
        )
        db.commit()
    finally:
        db.close()


def test_callback_headers_signature_and_retry(client, settings):
    request_id = _approve(client)
    _isolate(request_id)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500 if len(calls) == 1 else 200)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    db = session_scope()
    try:
        first = deliver_due(db, settings, http_client)
    finally:
        db.close()
    assert first == {"delivered": 0, "retrying": 1, "failed": 0}

    delivery = _delivery(request_id)
    assert delivery.attempts == 1
    assert delivery.delivered_at is None
    assert delivery.last_status_code == 500

    request = calls[0]
    timestamp = int(request.headers["x-elicloud-timestamp"])
    signature = request.headers["x-elicloud-signature"]
    assert verify_callback(TEST_SECRET, timestamp, request.content, signature)
    assert request.headers["x-elicloud-event"] == "email.verification"
    assert request.headers["idempotency-key"] == f"{request_id}:approved"
    payload = json.loads(request.content)
    assert payload["request_id"] == request_id
    assert payload["status"] == "approved"
    assert payload["metadata"] == {"user_id": "user_0001"}

    db = session_scope()
    try:
        row = db.query(CallbackDelivery).filter_by(request_id=request_id).one()
        row.next_attempt_at = to_iso(utcnow() - timedelta(seconds=1))
        db.commit()
    finally:
        db.close()

    db = session_scope()
    try:
        second = deliver_due(db, settings, http_client)
    finally:
        db.close()
    assert second == {"delivered": 1, "retrying": 0, "failed": 0}
    delivery = _delivery(request_id)
    assert delivery.delivered_at is not None
    assert delivery.attempts == 2

    status = client.get(f"/v1/verify/{request_id}", headers=auth_headers())
    assert status.json()["delivery"]["status"] == "delivered"


def test_callback_retry_exhausted(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "callback_max_attempts", 2)
    monkeypatch.setattr(settings, "callback_retry_schedule", "0,0")
    request_id = _approve(client)
    _isolate(request_id)

    http_client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(500))
    )
    db = session_scope()
    try:
        assert deliver_due(db, settings, http_client)["retrying"] == 1
    finally:
        db.close()

    db = session_scope()
    try:
        row = db.query(CallbackDelivery).filter_by(request_id=request_id).one()
        row.next_attempt_at = to_iso(utcnow() - timedelta(seconds=1))
        db.commit()
    finally:
        db.close()

    db = session_scope()
    try:
        assert deliver_due(db, settings, http_client)["failed"] == 1
    finally:
        db.close()
    delivery = _delivery(request_id)
    assert delivery.failed_at is not None
    assert delivery.attempts == 2


def test_callback_tampered_url_is_rejected(client):
    request_id = _approve(client)
    _isolate(request_id)
    db = session_scope()
    try:
        row = db.query(CallbackDelivery).filter_by(request_id=request_id).one()
        row.callback_url = "http://evil.example/cb"
        db.commit()
    finally:
        db.close()

    http_client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200))
    )
    db = session_scope()
    try:
        result = deliver_due(db, client.app.state.settings, http_client)
    finally:
        db.close()
    assert result["failed"] == 1
    delivery = _delivery(request_id)
    assert delivery.failed_at is not None
    assert "rejected" in (delivery.last_error or "")


def test_expire_due_creates_expired_callback(client, settings):
    response = create_request(client)
    request_id = response.json()["id"]
    db = session_scope()
    try:
        row = db.get(VerificationRequest, request_id)
        row.expires_at = to_iso(utcnow() - timedelta(seconds=5))
        db.commit()
    finally:
        db.close()

    db = session_scope()
    try:
        assert expire_due(db, settings) >= 1
    finally:
        db.close()

    delivery = _delivery(request_id)
    assert delivery.status == c.STATUS_EXPIRED
    assert json.loads(delivery.payload_json)["status"] == "expired"
