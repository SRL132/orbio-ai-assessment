"""End-to-end through HTTP with the offline keyword extractor (LLM_PROVIDER=fake)."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
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


def test_a_new_conversation_replaces_the_old_one(client: TestClient) -> None:
    first = client.post("/api/conversations").json()["id"]
    client.post("/api/conversations")
    assert client.get(f"/api/conversations/{first}/messages").status_code == 404


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
