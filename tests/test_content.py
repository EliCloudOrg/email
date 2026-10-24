from __future__ import annotations

from app.db import session_scope
from app.models import VerificationRequest

from conftest import create_request, link_from_mailer


def test_default_template_subject(client):
    assert create_request(client).status_code == 202
    _to, rendered = client.app.state.mailer.messages[-1]
    assert rendered.subject == "EliCloud 邮箱确认"
    assert "确认" in rendered.text
    assert "/v1/confirm?token=" in rendered.html


def test_register_template(client):
    response = create_request(
        client,
        content={"mode": "template", "template": "register"},
    )
    assert response.status_code == 202, response.text
    _to, rendered = client.app.state.mailer.messages[-1]
    assert "注册" in rendered.subject
    assert "完成 EliCloud 注册" in rendered.html

    db = session_scope()
    try:
        record = db.get(VerificationRequest, response.json()["id"])
        assert record.content_mode == "template"
        assert record.template_name == "register"
    finally:
        db.close()


def test_unknown_template_rejected(client):
    response = create_request(
        client, content={"mode": "template", "template": "no-such-template"}
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_content"


def test_custom_content(client):
    response = create_request(
        client,
        content={
            "mode": "custom",
            "subject": "Hello {{email}}",
            "text": "Open {{link}} within {{expires_minutes}} minutes.",
            "html": "<p>Hi {{name}}, <a href=\"{{link}}\">confirm</a></p>",
            "variables": {"name": "Alice"},
        },
    )
    assert response.status_code == 202, response.text
    _to, rendered = client.app.state.mailer.messages[-1]
    assert rendered.subject.startswith("Hello ")
    assert "Open http://testserver/email/v1/confirm?token=" in rendered.text
    assert "Hi Alice" in rendered.html
    assert "/v1/confirm?token=" in rendered.html

    db = session_scope()
    try:
        record = db.get(VerificationRequest, response.json()["id"])
        assert record.content_mode == "custom"
        assert record.template_name is None
    finally:
        db.close()


def test_custom_html_escapes_variables(client):
    response = create_request(
        client,
        content={
            "mode": "custom",
            "subject": "escape",
            "text": "{{name}}",
            "html": "<p>{{name}}</p>",
            "variables": {"name": "<script>alert(1)</script>"},
        },
    )
    assert response.status_code == 202
    _to, rendered = client.app.state.mailer.messages[-1]
    assert "&lt;script&gt;" in rendered.html
    assert "<script>" not in rendered.html


def test_custom_unknown_placeholder(client):
    response = create_request(
        client,
        content={
            "mode": "custom",
            "subject": "x",
            "text": "hello {{nope}}",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_content"
    assert "nope" in response.json()["error_description"]


def test_custom_reserved_variable(client):
    response = create_request(
        client,
        content={
            "mode": "custom",
            "subject": "x",
            "text": "{{link}}",
            "variables": {"link": "http://evil.example"},
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_content"


def test_custom_subject_newline_rejected(client):
    response = create_request(
        client,
        content={"mode": "custom", "subject": "a\nb", "text": "x"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_custom_missing_body_rejected(client):
    response = create_request(
        client, content={"mode": "custom", "subject": "only subject"}
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_custom_too_large_rejected(client):
    response = create_request(
        client,
        content={"mode": "custom", "subject": "x", "text": "a" * 20001},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_custom_link_matches_email(client):
    response = create_request(
        client,
        content={
            "mode": "custom",
            "subject": "link",
            "text": "{{link}}",
            "html": "<a href=\"{{link}}\">go</a>",
        },
    )
    assert response.status_code == 202
    link = link_from_mailer(client)
    assert "token=" in link
