"""Rate limits on login and the web chat, counted in the database (shared by all workers)."""

import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.adapters.clock.clocks import FakeClock
from app.adapters.storage.rate_limit import SqlRateLimiter
from app.adapters.storage.sql import make_engine
from app.api.auth import hash_password
from app.api.main import create_app
from app.bootstrap import build
from app.settings import Settings

PASSWORD = secrets.token_urlsafe(16)


def test_sliding_window() -> None:
    engine = make_engine("sqlite:///:memory:")
    clock = FakeClock(datetime(2026, 11, 2, tzinfo=UTC))
    limiter = SqlRateLimiter(engine, clock, "c")
    window = timedelta(minutes=1)
    assert [limiter.allow("k", 2, window) for _ in range(3)] == [True, True, False]
    assert limiter.allow("other", 2, window)  # keys are counted separately
    assert not SqlRateLimiter(engine, clock, "c").allow("k", 2, window)  # another worker agrees
    assert SqlRateLimiter(engine, clock, "another_client").allow("k", 2, window)
    clock.advance(seconds=61)
    assert limiter.allow("k", 2, window)  # the old hits left the window
    engine.dispose()


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'r.db'}",
        debounce_seconds=0,
        recruiter_users=f"rec@sazon.test:{hash_password(PASSWORD)}",
        session_secret=secrets.token_hex(16),
        login_attempts_per_ip_per_15_min=6,
        login_failures_per_email_per_15_min=3,
        chat_starts_per_ip_per_hour=2,
        chat_messages_per_conversation_per_minute=3,
    )
    with TestClient(create_app(build(settings))) as client:
        yield client


def login(client: TestClient, password: str, email: str = "rec@sazon.test") -> int:
    response = client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )
    return response.status_code


def test_failed_logins_lock_the_account_for_a_while(client: TestClient) -> None:
    assert [login(client, "wrong") for _ in range(3)] == [401, 401, 401]
    assert login(client, PASSWORD) == 429  # even the right password, until the window passes
    assert login(client, "x", email="other@sazon.test") == 401  # other accounts aren't affected


def test_successful_logins_do_not_count_against_the_account(client: TestClient) -> None:
    assert [login(client, PASSWORD) for _ in range(4)] == [303] * 4


def test_one_address_cannot_try_many_accounts(client: TestClient) -> None:
    codes = [login(client, "x", email=f"user{n}@sazon.test") for n in range(7)]
    assert codes == [401] * 6 + [429]


def test_chat_starts_are_limited_per_address(client: TestClient) -> None:
    assert [client.post("/api/conversations").status_code for _ in range(3)] == [201, 201, 429]
    response = client.post("/api/conversations")
    assert response.headers["Retry-After"] == "3600"


def test_messages_are_limited_per_conversation(client: TestClient) -> None:
    cid = client.post("/api/conversations").json()["id"]
    codes = [
        client.post(f"/api/conversations/{cid}/messages", json={"text": "hola"}).status_code
        for _ in range(4)
    ]
    assert codes == [202, 202, 202, 429]
