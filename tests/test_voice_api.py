"""Voice page, immediate processing for voice, server TTS endpoint, channel in the dashboard."""

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.bootstrap import Container, build
from app.settings import Settings


@pytest.fixture
def container(tmp_path: Path) -> Container:
    # A long debounce proves voice messages don't wait for it.
    return build(
        Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'v.db'}", debounce_seconds=30)
    )


@pytest.fixture
def client(container: Container) -> TestClient:
    with TestClient(create_app(container)) as client:
        yield client


def test_voice_page_renders(client: TestClient) -> None:
    page = client.get("/voice").text
    assert "Start call" in page
    assert "SERVER_TTS = false" in page


def test_voice_messages_skip_the_debounce(client: TestClient) -> None:
    cid = client.post("/api/conversations", json={"channel": "voice"}).json()["id"]
    client.post(f"/api/conversations/{cid}/messages", json={"text": "sí"})
    data = client.get(f"/api/conversations/{cid}/messages").json()
    assert data["channel"] == "voice"
    assert [m["direction"] for m in data["messages"]] == ["out", "in", "out"]
    assert "no guardamos el audio" in data["messages"][0]["text"]


def test_chat_messages_still_wait_for_the_debounce(client: TestClient) -> None:
    cid = client.post("/api/conversations").json()["id"]
    client.post(f"/api/conversations/{cid}/messages", json={"text": "sí"})
    data = client.get(f"/api/conversations/{cid}/messages").json()
    assert data["channel"] == "chat"
    assert [m["direction"] for m in data["messages"]] == ["out", "in"]


def test_tts_is_404_without_elevenlabs(client: TestClient) -> None:
    assert client.post("/api/tts", json={"text": "hola"}).status_code == 404


class _StubTTS:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def synthesize(self, text: str) -> bytes:
        if self.fail:
            raise httpx.ConnectError("down")
        return b"mp3:" + text.encode()


def test_tts_returns_audio_when_configured(container: Container) -> None:
    container.tts = _StubTTS()  # type: ignore[assignment]
    with TestClient(create_app(container)) as client:
        response = client.post("/api/tts", json={"text": "hola"})
        assert response.headers["content-type"] == "audio/mpeg"
        assert response.content == b"mp3:hola"
        assert "SERVER_TTS = true" in client.get("/voice").text


def test_tts_provider_failure_is_502(container: Container) -> None:
    container.tts = _StubTTS(fail=True)  # type: ignore[assignment]
    with TestClient(create_app(container)) as client:
        assert client.post("/api/tts", json={"text": "hola"}).status_code == 502


def test_elevenlabs_is_built_from_settings(tmp_path: Path) -> None:
    container = build(
        Settings(
            _env_file=None,
            database_url=f"sqlite:///{tmp_path / 'e.db'}",
            elevenlabs_api_key="k",
            elevenlabs_voice_id="v",
        )
    )
    assert container.tts is not None
    container.close()


def test_empty_settings_mean_unset() -> None:
    settings = Settings(_env_file=None, session_secret="", elevenlabs_api_key="  ")
    assert settings.session_secret is None
    assert settings.elevenlabs_api_key is None


@pytest.mark.parametrize("page", ["/chat", "/voice"])
def test_offline_mode_says_so(client: TestClient, page: str) -> None:
    assert "Offline mode: keyword rules, no LLM" in client.get(page).text


@pytest.mark.parametrize("page", ["/chat", "/voice"])
def test_no_offline_notice_with_a_real_llm(tmp_path: Path, page: str) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'live.db'}",
        llm_provider="openrouter",
        openrouter_api_key="test",
        openrouter_model="test/model",
    )
    with TestClient(create_app(build(settings))) as live:
        assert "Offline mode" not in live.get(page).text
