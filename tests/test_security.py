from __future__ import annotations

import logging
from datetime import timedelta

from app import constants as c
from app.db import init_db, session_scope
from app.models import CallbackDelivery, VerificationRequest, to_iso, utcnow
from app.security import sha256_hex, sign_callback, verify_callback
from app.service import cleanup

from conftest import (
    auth_headers,
    create_request,
    csrf_from_page,
    link_from_mailer,
    token_from_link,
    unique_email,
)


def test_token_is_hashed_at_rest(client):
    response = create_request(client)
    request_id = response.json()["id"]
    token = token_from_link(link_from_mailer(client))
    db = session_scope()
    try:
        record = db.get(VerificationRequest, request_id)
        assert record.token_hash == sha256_hex(token)
        assert record.token_hash != token
    finally:
        db.close()


def test_no_token_in_logs(client, caplog):
    caplog.set_level(logging.INFO, logger="email")
    create_request(client)
    token = token_from_link(link_from_mailer(client))
    assert token not in caplog.text


def test_security_headers_on_page(client):
    response = create_request(client)
    token = token_from_link(link_from_mailer(client))
    page = client.get("/v1/confirm", params={"token": token})
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert page.headers["x-frame-options"] == "DENY"
    assert "default-src 'none'" in page.headers["content-security-policy"]


def test_confirm_rate_limit(client):
    create_request(client)
    token = token_from_link(link_from_mailer(client))
    csrf = csrf_from_page(client.get("/v1/confirm", params={"token": token}).text)
    for _ in range(10):
        response = client.post(
            "/v1/confirm",
            data={"token": token, "decision": "approve", "csrf": "wrong"},
        )
        assert response.status_code == 400
    blocked = client.post(
        "/v1/confirm",
        data={"token": token, "decision": "approve", "csrf": "wrong"},
    )
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers


def test_healthz(client):
    response = client.get("/v1/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_init_db_is_idempotent(client, settings):
    assert init_db(settings) is init_db(settings)


def test_cleanup_removes_old_records(client, settings):
    response = create_request(client)
    request_id = response.json()["id"]
    token = token_from_link(link_from_mailer(client))
    csrf = csrf_from_page(client.get("/v1/confirm", params={"token": token}).text)
    assert (
        client.post(
            "/v1/confirm", data={"token": token, "decision": "approve", "csrf": csrf}
        ).status_code
        == 200
    )

    db = session_scope()
    try:
        record = db.get(VerificationRequest, request_id)
        record.decided_at = to_iso(utcnow() - timedelta(days=settings.retention_days + 1))
        db.commit()
    finally:
        db.close()

    db = session_scope()
    try:
        assert cleanup(db, settings) >= 1
    finally:
        db.close()

    db = session_scope()
    try:
        assert db.get(VerificationRequest, request_id) is None
        assert (
            db.query(CallbackDelivery).filter_by(request_id=request_id).count() == 0
        )
    finally:
        db.close()


def test_callback_signature_detects_tampering():
    body = b'{"hello":"world"}'
    signature = sign_callback("secret" * 8, 1700000000, body)
    assert verify_callback("secret" * 8, 1700000000, body, f"sha256={signature}")
    assert not verify_callback("secret" * 8, 1700000000, body + b"x", f"sha256={signature}")
    assert not verify_callback("secret" * 8, 1700000001, body, f"sha256={signature}")


def test_invalid_email_and_metadata(client):
    response = create_request(client, email="not-an-email")
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"

    response = client.post(
        "/v1/verify",
        json={
            "email": unique_email(),
            "callback_url": "http://127.0.0.1:9001/cb",
            "metadata": "not-an-object",
        },
        headers=auth_headers(),
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_unknown_route(client):
    assert client.get("/nope").status_code == 404
