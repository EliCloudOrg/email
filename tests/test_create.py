from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import session_scope
from app.main import create_app
from app.models import VerificationRequest

from conftest import (
    FakeMailer,
    auth_headers,
    create_request,
    link_from_mailer,
    token_from_link,
    unique_email,
)


def test_internal_auth_required(client):
    body = {
        "email": unique_email(),
        "callback_url": "http://127.0.0.1:9001/cb",
    }
    assert client.post("/v1/verify", json=body).status_code == 401
    assert (
        client.post(
            "/v1/verify", json=body, headers={"Authorization": "Bearer wrong-token"}
        ).status_code
        == 401
    )
    assert client.get("/v1/verify/nope").status_code == 401


def test_create_success_sends_mail(client):
    email = unique_email()
    response = create_request(client, email=email)
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["id"].startswith("ev_")
    assert payload["status"] == "pending"
    assert payload["expires_in"] == 900
    assert len(client.app.state.mailer.messages) == 1
    to, rendered = client.app.state.mailer.messages[0]
    assert to == email
    assert "确认" in rendered.subject
    link = link_from_mailer(client)
    token = token_from_link(link)

    db = session_scope()
    try:
        record = db.get(VerificationRequest, payload["id"])
        assert record is not None
        assert record.token_hash != token
        assert record.email_lower == email.lower()
        assert record.status == "pending"
    finally:
        db.close()


def test_create_rejects_bad_callback(client):
    for callback_url, error in [
        ("http://evil.example/cb", "callback_not_allowed"),
        ("http://user:pass@127.0.0.1/cb", "invalid_callback_url"),
        ("ftp://127.0.0.1/cb", "invalid_callback_url"),
        ("http://127.0.0.1/cb#frag", "invalid_callback_url"),
    ]:
        response = create_request(client, callback_url=callback_url)
        assert response.status_code == 400, response.text
        assert response.json()["error"] == error


def test_create_rejects_large_metadata(client):
    response = create_request(client, metadata={"blob": "x" * 9000})
    assert response.status_code == 400
    assert response.json()["error"] == "metadata_too_large"


def test_create_mail_failure_marks_failed(client):
    email = unique_email()
    client.app.state.mailer.fail = True
    response = create_request(client, email=email)
    assert response.status_code == 502
    assert response.json()["error"] == "email_delivery_failed"

    db = session_scope()
    try:
        record = (
            db.query(VerificationRequest)
            .filter_by(email_lower=email.lower())
            .one()
        )
        assert record.status == "failed"
        assert record.failure_reason
    finally:
        db.close()


def test_active_limit(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "create_cooldown_seconds", 0)
    email = unique_email()
    for _ in range(3):
        assert create_request(client, email=email).status_code == 202
    response = create_request(client, email=email)
    assert response.status_code == 429
    assert response.json()["error"] == "too_many_pending"


def test_create_cooldown(client):
    email = unique_email()
    assert create_request(client, email=email).status_code == 202
    response = create_request(client, email=email)
    assert response.status_code == 429
    assert response.json()["error"] == "rate_limited"


def test_status_endpoint(client):
    response = create_request(client)
    assert response.status_code == 202
    request_id = response.json()["id"]
    status = client.get(f"/v1/verify/{request_id}", headers=auth_headers())
    assert status.status_code == 200
    data = status.json()
    assert data["id"] == request_id
    assert data["status"] == "pending"
    assert data["metadata"] == {"user_id": "user_0001"}
    assert data["delivery"] is None
    assert client.get("/v1/verify/ev_missing", headers=auth_headers()).status_code == 404


def test_only_v1_routes(client):
    assert client.get("/verify").status_code == 404
    assert client.post("/verify", json={}).status_code == 404


def test_internal_disabled_without_token(client, settings):
    other = create_app(
        settings=settings.model_copy(update={"email_internal_token": ""}),
        mailer=FakeMailer(),
    )
    with TestClient(other) as other_client:
        response = other_client.post(
            "/v1/verify",
            json={"email": unique_email(), "callback_url": "http://127.0.0.1:9001/cb"},
            headers=auth_headers(),
        )
        assert response.status_code == 503
        assert response.json()["error"] == "internal_api_disabled"
