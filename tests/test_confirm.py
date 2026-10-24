from __future__ import annotations

from datetime import timedelta

from app.db import session_scope
from app.models import CallbackDelivery, VerificationRequest, to_iso, utcnow

from conftest import (
    auth_headers,
    create_request,
    csrf_from_page,
    link_from_mailer,
    token_from_link,
)


def _prepare(client):
    response = create_request(client)
    assert response.status_code == 202
    request_id = response.json()["id"]
    token = token_from_link(link_from_mailer(client))
    return request_id, token


def test_confirm_page_and_approve(client):
    request_id, token = _prepare(client)
    page = client.get("/v1/confirm", params={"token": token})
    assert page.status_code == 200
    csrf = csrf_from_page(page.text)

    db = session_scope()
    try:
        assert db.get(VerificationRequest, request_id).status == "pending"
    finally:
        db.close()

    result = client.post(
        "/v1/confirm",
        data={"token": token, "decision": "approve", "csrf": csrf},
    )
    assert result.status_code == 200, result.text
    assert "邮箱已确认" in result.text

    db = session_scope()
    try:
        record = db.get(VerificationRequest, request_id)
        assert record.status == "approved"
        assert record.decided_at is not None
        deliveries = (
            db.query(CallbackDelivery).filter_by(request_id=request_id).all()
        )
        assert len(deliveries) == 1
        assert deliveries[0].status == "approved"
        assert deliveries[0].payload_json
    finally:
        db.close()

    status = client.get(f"/v1/verify/{request_id}", headers=auth_headers())
    assert status.status_code == 200
    assert status.json()["status"] == "approved"
    assert status.json()["delivery"]["status"] == "pending"


def test_confirm_deny(client):
    request_id, token = _prepare(client)
    page = client.get("/v1/confirm", params={"token": token})
    csrf = csrf_from_page(page.text)
    result = client.post(
        "/v1/confirm",
        data={"token": token, "decision": "deny", "csrf": csrf},
    )
    assert result.status_code == 200
    assert "已拒绝" in result.text

    db = session_scope()
    try:
        record = db.get(VerificationRequest, request_id)
        assert record.status == "denied"
        delivery = db.query(CallbackDelivery).filter_by(request_id=request_id).one()
        assert delivery.status == "denied"
    finally:
        db.close()


def test_confirm_get_has_no_side_effect(client):
    request_id, token = _prepare(client)
    client.get("/v1/confirm", params={"token": token})
    db = session_scope()
    try:
        assert db.get(VerificationRequest, request_id).status == "pending"
    finally:
        db.close()


def test_confirm_requires_csrf(client):
    request_id, token = _prepare(client)
    missing = client.post("/v1/confirm", data={"token": token, "decision": "approve"})
    assert missing.status_code == 400
    wrong = client.post(
        "/v1/confirm",
        data={"token": token, "decision": "approve", "csrf": "wrong"},
    )
    assert wrong.status_code == 400
    db = session_scope()
    try:
        assert db.get(VerificationRequest, request_id).status == "pending"
    finally:
        db.close()


def test_confirm_csrf_rotates(client):
    _request_id, token = _prepare(client)
    first = csrf_from_page(client.get("/v1/confirm", params={"token": token}).text)
    second = csrf_from_page(client.get("/v1/confirm", params={"token": token}).text)
    assert first != second
    result = client.post(
        "/v1/confirm",
        data={"token": token, "decision": "approve", "csrf": first},
    )
    assert result.status_code == 400


def test_confirm_replay_is_idempotent(client):
    request_id, token = _prepare(client)
    csrf = csrf_from_page(client.get("/v1/confirm", params={"token": token}).text)
    first = client.post(
        "/v1/confirm", data={"token": token, "decision": "approve", "csrf": csrf}
    )
    second = client.post(
        "/v1/confirm", data={"token": token, "decision": "approve", "csrf": csrf}
    )
    assert first.status_code == 200
    assert second.status_code == 200
    db = session_scope()
    try:
        assert db.query(CallbackDelivery).filter_by(request_id=request_id).count() == 1
    finally:
        db.close()


def test_confirm_unknown_token(client):
    assert client.get("/v1/confirm", params={"token": "nope"}).status_code == 404
    result = client.post(
        "/v1/confirm", data={"token": "nope", "decision": "approve", "csrf": "x"}
    )
    assert result.status_code == 404


def test_expired_token(client):
    request_id, token = _prepare(client)
    db = session_scope()
    try:
        record = db.get(VerificationRequest, request_id)
        record.expires_at = to_iso(utcnow() - timedelta(seconds=5))
        db.commit()
    finally:
        db.close()

    page = client.get("/v1/confirm", params={"token": token})
    assert page.status_code == 410

    status = client.get(f"/v1/verify/{request_id}", headers=auth_headers())
    assert status.json()["status"] == "expired"
    db = session_scope()
    try:
        delivery = (
            db.query(CallbackDelivery)
            .filter_by(request_id=request_id, status="expired")
            .one()
        )
        assert delivery.payload_json
    finally:
        db.close()


def test_invalid_decision(client):
    _request_id, token = _prepare(client)
    csrf = csrf_from_page(client.get("/v1/confirm", params={"token": token}).text)
    result = client.post(
        "/v1/confirm",
        data={"token": token, "decision": "maybe", "csrf": csrf},
    )
    assert result.status_code == 400
