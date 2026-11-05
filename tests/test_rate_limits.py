"""Rate limits on login and the web chat, counted in the database (shared by all workers)."""

import re
import secrets
import threading
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
from tests.conftest import submit

PASSWORD = secrets.token_urlsafe(16)


def test_sliding_window() -> None:
    engine = make_engine("sqlite:///:memory:")
    clock = FakeClock(datetime(2026, 11, 2, tzinfo=UTC))
    limiter = SqlRateLimiter(engine, clock, "c")
    window = timedelta(minutes=1)
    assert [limiter.allow("k", 2, window) is not None for _ in range(3)] == [True, True, False]
    assert limiter.allow("other", 2, window)  # keys are counted separately
    assert SqlRateLimiter(engine, clock, "c").allow("k", 2, window) is None  # other workers agree
    assert SqlRateLimiter(engine, clock, "another_client").allow("k", 2, window)
    clock.advance(seconds=61)
    assert limiter.allow("k", 2, window)  # the old hits left the window


def test_a_forgotten_hit_frees_its_slot() -> None:
    engine = make_engine("sqlite:///:memory:")
    limiter = SqlRateLimiter(engine, FakeClock(datetime(2026, 11, 2, tzinfo=UTC)), "c")
    window = timedelta(minutes=1)
    first = limiter.allow("k", 1, window)
    assert first is not None
    assert limiter.allow("k", 1, window) is None
    limiter.forget(first)
    assert limiter.allow("k", 1, window) is not None
    engine.dispose()


def test_concurrent_requests_never_exceed_the_limit(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'r.db'}")  # a file: threads share it for real
    limiter = SqlRateLimiter(engine, FakeClock(datetime(2026, 11, 2, tzinfo=UTC)), "c")
    barrier = threading.Barrier(12)
    allowed: list[str] = []

    def request() -> None:
        barrier.wait()
        if hit := limiter.allow("burst", 5, timedelta(minutes=1)):
            allowed.append(hit)

    threads = [threading.Thread(target=request) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(allowed) == 5
    engine.dispose()
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
    response = submit(
        client, "/login", {"email": email, "password": password}, follow_redirects=False
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


def proxied(tmp_path: Path, header: str = "True-Client-IP") -> TestClient:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'p.db'}",
        recruiter_users=f"rec@sazon.test:{hash_password(PASSWORD)}",
        session_secret=secrets.token_hex(16),
        login_attempts_per_ip_per_15_min=2,
        client_ip_header=header,
    )
    return TestClient(create_app(build(settings)))


def attempt(client: TestClient, headers: dict[str, str], number: int) -> int:
    page = client.get("/login").text
    token = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert token is not None
    return client.post(
        "/login",
        data={
            "email": f"user{number}@sazon.test",
            "password": "wrong",
            "csrf_token": token.group(1),
        },
        headers=headers,
        follow_redirects=False,
    ).status_code


def test_a_new_x_forwarded_for_each_time_does_not_get_round_the_limit(tmp_path: Path) -> None:
    with proxied(tmp_path) as client:
        codes = [
            attempt(
                client, {"True-Client-IP": "198.51.100.1", "X-Forwarded-For": f"203.0.113.{n}"}, n
            )
            for n in range(3)
        ]
    assert codes == [401, 401, 429]


def test_two_callers_behind_the_proxy_have_their_own_limits(tmp_path: Path) -> None:
    with proxied(tmp_path) as client:
        first = [attempt(client, {"True-Client-IP": "198.51.100.1"}, n) for n in range(3)]
        assert first == [401, 401, 429]
        assert attempt(client, {"True-Client-IP": "198.51.100.2"}, 9) == 401


@pytest.mark.parametrize("bad", ["not-an-address", "x" * 500, "1.2.3.4, 5.6.7.8"])
def test_a_header_that_is_not_an_address_is_not_used_as_one(tmp_path: Path, bad: str) -> None:
    with proxied(tmp_path) as client:
        codes = [attempt(client, {"True-Client-IP": f"{bad}{n}"}, n) for n in range(3)]
    assert codes == [401, 401, 429]  # all counted against the connection instead


def test_with_no_header_set_the_callers_headers_are_ignored(tmp_path: Path) -> None:
    with proxied(tmp_path, header="") as client:
        codes = [attempt(client, {"True-Client-IP": f"198.51.100.{n}"}, n) for n in range(3)]
    assert codes == [401, 401, 429]
