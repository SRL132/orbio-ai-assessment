"""End-to-end through HTTP with the offline keyword extractor (LLM_PROVIDER=fake)."""

import json
import secrets
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import delete

from app.adapters.storage.sql import ConversationRow, MessageRow
from app.api.main import MAX_OWNED_CONVERSATIONS, create_app
from app.bootstrap import build
from app.settings import Settings


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        _env_file=None,
        llm_provider="fake",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        debounce_seconds=0,
        demo_mode=True,
    )
    with TestClient(create_app(build(settings))) as client:
        yield client


def send(client: TestClient, cid: str, text: str) -> list[str]:
    before = client.get(f"/api/conversations/{cid}/messages").json()["messages"]
    response = client.post(f"/api/conversations/{cid}/messages", json={"text": text})
    assert response.status_code == 202
    after = client.get(f"/api/conversations/{cid}/messages?after={before[-1]['seq']}").json()
    return [m["text"] for m in after["messages"] if m["direction"] == "out"]


def test_full_screening_over_http(client: TestClient) -> None:
    cid = client.post("/api/conversations").json()["id"]
    greeting = client.get(f"/api/conversations/{cid}/messages").json()["messages"][0]
    assert "Lucía" in greeting["text"]

    for text in [
        "Sí",
        "Ana López",
        "Moto",
        "Madrid, Chamberí",
        "Jornada completa",
        "noches",
        "2 años en Glovo",
        "mañana",
    ]:
        replies = send(client, cid, text)
        assert replies, f"no reply to {text!r}"
    assert "Repasemos" in replies[-1]

    send(client, cid, "Sí")
    data = client.get(f"/api/conversations/{cid}/messages").json()
    assert data["status"] == "qualified"


def test_demo_clock_sends_reminder(client: TestClient) -> None:
    cid = client.post("/api/conversations").json()["id"]
    response = client.post("/api/demo/advance", json={"hours": 2})
    assert response.json()["conversations_changed"] >= 1
    messages = client.get(f"/api/conversations/{cid}/messages").json()["messages"]
    assert "¡Hola de nuevo!" in messages[-1]["text"]


def test_unknown_conversation_is_404(client: TestClient) -> None:
    unknown = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/api/conversations/{unknown}/messages").status_code == 404
    response = client.post(f"/api/conversations/{unknown}/messages", json={"text": "hola"})
    assert response.status_code == 404


def test_only_the_browser_that_started_a_conversation_can_read_it(client: TestClient) -> None:
    cid = client.post("/api/conversations").json()["id"]
    assert client.get(f"/api/conversations/{cid}/messages").status_code == 200
    stranger = TestClient(client.app)  # knows the id (e.g. from a log line), not the cookie
    assert stranger.get(f"/api/conversations/{cid}/messages").status_code == 404
    reply = stranger.post(f"/api/conversations/{cid}/messages", json={"text": "hola"})
    assert reply.status_code == 404
    stranger.cookies.set("sazon_chat", client.cookies["sazon_chat"] + "x")  # tampered
    assert stranger.get(f"/api/conversations/{cid}/messages").status_code == 404


def test_a_second_tab_does_not_lock_out_the_first(client: TestClient) -> None:
    # A chat tab and a voice tab share the cookie: starting the second keeps the first readable.
    first = client.post("/api/conversations").json()["id"]
    second = client.post("/api/conversations", json={"channel": "voice"}).json()["id"]
    assert client.get(f"/api/conversations/{first}/messages").status_code == 200
    assert client.get(f"/api/conversations/{second}/messages").status_code == 200
    reply = client.post(f"/api/conversations/{first}/messages", json={"text": "sí"})
    assert reply.status_code == 202


def test_the_browser_keeps_only_its_latest_conversations(client: TestClient) -> None:
    oldest = client.post("/api/conversations").json()["id"]
    for _ in range(MAX_OWNED_CONVERSATIONS):
        client.post("/api/conversations")
    assert client.get(f"/api/conversations/{oldest}/messages").status_code == 404


def test_a_single_conversation_cookie_still_works(tmp_path: Path) -> None:
    # Cookies issued before the cookie held a list named one conversation as a plain string.
    secret = secrets.token_urlsafe(16)
    settings = Settings(
        _env_file=None,
        llm_provider="fake",
        database_url=f"sqlite:///{tmp_path / 'legacy.db'}",
        debounce_seconds=0,
        demo_mode=True,
        session_secret=secret,
    )
    with TestClient(create_app(build(settings))) as client:
        cid = client.post("/api/conversations").json()["id"]
        signer = URLSafeTimedSerializer(secret, salt="chat-conversation")
        client.cookies.clear()
        client.cookies.set("sazon_chat", signer.dumps(cid))
        assert client.get(f"/api/conversations/{cid}/messages").status_code == 200


def test_empty_message_is_rejected(client: TestClient) -> None:
    cid = client.post("/api/conversations").json()["id"]
    assert client.post(f"/api/conversations/{cid}/messages", json={"text": ""}).status_code == 422


def test_chat_page_renders(client: TestClient) -> None:
    response = client.get("/chat")
    assert response.status_code == 200
    assert "Grupo Sazón" in response.text


def test_replies_are_pushed_over_server_sent_events(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="fake",
        database_url=f"sqlite:///{tmp_path / 'sse.db'}",
        debounce_seconds=0,
        demo_mode=True,
        chat_stream_seconds=0.3,  # a short stream: it ends and the browser would reconnect
    )
    with TestClient(create_app(build(settings))) as client:
        cid = client.post("/api/conversations").json()["id"]
        client.post(f"/api/conversations/{cid}/messages", json={"text": "sí"})
        with client.stream("GET", f"/api/conversations/{cid}/stream") as response:
            assert response.headers["content-type"].startswith("text/event-stream")
            body = "".join(response.iter_text())
        events = [b for b in body.split("\n\n") if b.startswith("id:")]
        first = json.loads(events[0].split("data: ", 1)[1])
        assert [m["direction"] for m in first["messages"]] == ["out", "in", "out"]
        assert events[0].startswith(f"id: {first['messages'][-1]['seq']}")
        resumed = client.get(
            f"/api/conversations/{cid}/stream", headers={"Last-Event-ID": "3"}
        ).text
        assert '"messages": []' in resumed  # nothing after the last one seen
        stranger = TestClient(client.app)
        assert stranger.get(f"/api/conversations/{cid}/stream").status_code == 404


def test_a_stream_ends_when_its_conversation_is_erased(tmp_path: Path) -> None:
    container = build(
        Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'e.db'}", demo_mode=True)
    )
    with TestClient(create_app(container)) as client:
        cid = client.post("/api/conversations").json()["id"]
        with container.db.begin() as connection:  # the retention job erased it meanwhile
            connection.execute(delete(MessageRow).where(MessageRow.conversation_id == cid))
            connection.execute(delete(ConversationRow).where(ConversationRow.id == cid))
        body = client.get(f"/api/conversations/{cid}/stream").text
    assert body.strip() == "retry: 1000"


def test_root_goes_to_the_chat(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (307, "/chat")


def _ats_client(tmp_path: Path, token: str | None) -> TestClient:
    settings = Settings(
        _env_file=None,
        llm_provider="fake",
        database_url=f"sqlite:///{tmp_path / 'ats.db'}",
        debounce_seconds=0,
        demo_mode=True,
        ats_token=token,
    )
    return TestClient(create_app(build(settings)))


def test_ats_registers_an_application_and_the_candidate_opens_the_link(tmp_path: Path) -> None:
    with _ats_client(tmp_path, "ats-secret") as client:
        applied = "2026-10-30T09:00:00+00:00"
        response = client.post(
            "/api/v1/applications",
            json={"applied_at": applied},
            headers={"Authorization": "Bearer ats-secret"},
        )
        assert response.status_code == 201
        created = response.json()
        state = client.app.state.container.service.repo.get(UUID(created["id"]))
        assert state.applied_at.isoformat() == applied

        # A different browser can't read it until it opens the invite link.
        cid = created["id"]
        assert client.get(f"/api/conversations/{cid}/messages").status_code == 404
        opened = client.get(created["chat_url"], follow_redirects=False)
        assert opened.status_code == 303
        assert opened.headers["location"] == f"/chat?conversation={cid}"
        messages = client.get(f"/api/conversations/{cid}/messages").json()["messages"]
        assert messages[0]["direction"] == "out"  # the greeting is waiting for them


@pytest.mark.parametrize(
    ("token", "header", "body", "code"),
    [
        (None, "Bearer x", {"applied_at": "2026-10-30T09:00:00+00:00"}, 404),  # disabled
        ("ats-secret", "Bearer wrong", {"applied_at": "2026-10-30T09:00:00+00:00"}, 401),
        ("ats-secret", "Bearer ats-secret", {"applied_at": "2026-10-30T09:00:00"}, 422),  # naive
        ("ats-secret", "Bearer ats-secret", {"applied_at": "2999-01-01T00:00:00+00:00"}, 422),
    ],
)
def test_application_endpoint_refuses(
    tmp_path: Path, token: str | None, header: str, body: dict[str, str], code: int
) -> None:
    with _ats_client(tmp_path, token) as client:
        response = client.post("/api/v1/applications", json=body, headers={"Authorization": header})
        assert response.status_code == code


def test_a_forged_invite_is_404(client: TestClient) -> None:
    assert client.get("/chat/invite/not-a-token", follow_redirects=False).status_code == 404


def test_consent_message_links_to_a_privacy_notice_that_exists(client: TestClient) -> None:
    cid = client.post("/api/conversations").json()["id"]
    greeting = client.get(f"/api/conversations/{cid}/messages").json()["messages"][0]["text"]
    assert "[" not in greeting  # no unfilled placeholder
    assert "/privacy" in greeting
    page = client.get("/privacy")
    assert page.status_code == 200
    assert "6 meses" in page.text
    assert "6 months" in client.get("/privacy?lang=en").text
