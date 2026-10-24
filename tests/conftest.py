"""测试环境：环境变量必须在任何 app 模块导入前写好。"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEST_DIR = Path(tempfile.mkdtemp(prefix="email-tests-"))
TEST_TOKEN = "test-internal-token-0123456789abcdef"
TEST_SECRET = "test-callback-secret-0123456789abcdef"

os.environ.update(
    {
        "PUBLIC_BASE_URL": "http://testserver/email",
        "DATABASE_URL": f"sqlite:///{(TEST_DIR / 'email-test.db').as_posix()}",
        "EMAIL_INTERNAL_TOKEN": TEST_TOKEN,
        "CALLBACK_SIGNING_SECRET": TEST_SECRET,
        "CALLBACK_ALLOWED_HOSTS": "127.0.0.1,localhost,testserver",
        "MAIL_MODE": "log",
        "MAIL_FROM": "no-reply@example.com",
        "MAIL_FROM_NAME": "EliCloud Test",
        "WORKER_ENABLED": "false",
        "LOG_LEVEL": "warning",
        "TRUST_PROXY_HEADERS": "false",
        "VERIFICATION_TTL_SECONDS": "900",
        "CALLBACK_MAX_ATTEMPTS": "6",
        "CALLBACK_RETRY_SCHEDULE": "0,30,120,600,3600,21600",
        "CALLBACK_ON_EXPIRY": "true",
        "RETENTION_DAYS": "30",
        "MAX_ACTIVE_PER_EMAIL": "3",
        "CREATE_COOLDOWN_SECONDS": "60",
        "CONFIRM_RATE_LIMIT_PER_MINUTE": "10",
    }
)

from app.errors import limiter  # noqa: E402
from app.main import create_app  # noqa: E402
from app.mailer import MailDeliveryError  # noqa: E402


class FakeMailer:
    def __init__(self) -> None:
        self.messages: list[tuple[str, object]] = []
        self.fail = False

    def send(self, to: str, rendered) -> None:
        if self.fail:
            raise MailDeliveryError("simulated smtp failure")
        self.messages.append((to, rendered))


@pytest.fixture(scope="session")
def client():
    app = create_app(mailer=FakeMailer())
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def settings(client):
    return client.app.state.settings


@pytest.fixture(autouse=True)
def _reset_runtime(client):
    limiter.clear()
    mailer = client.app.state.mailer
    mailer.messages.clear()
    mailer.fail = False
    yield
    limiter.clear()


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


def unique_email() -> str:
    return f"user-{uuid.uuid4().hex[:10]}@example.com"


def create_request(client, *, email: str | None = None, **overrides):
    body = {
        "email": email or unique_email(),
        "purpose": "generic",
        "source": "sso",
        "callback_url": "http://127.0.0.1:9001/cb",
        "metadata": {"user_id": "user_0001"},
        "expires_in": 900,
    }
    body.update(overrides)
    return client.post("/v1/verify", json=body, headers=auth_headers())


def link_from_mailer(client) -> str:
    _to, rendered = client.app.state.mailer.messages[-1]
    body = rendered.text or rendered.html or ""
    match = re.search(r"https?://\S+/v1/confirm\?token=[A-Za-z0-9_-]+", body)
    assert match, body[:500]
    return match.group(0)


def token_from_link(link: str) -> str:
    return parse_qs(urlparse(link).query)["token"][0]


def csrf_from_page(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, html[:500]
    return match.group(1)


def record_id_from_response(response) -> str:
    return response.json()["id"]
