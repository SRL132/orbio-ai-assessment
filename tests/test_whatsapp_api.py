"""WhatsApp webhook endpoints: Meta's handshake, signature check, end to end with a fake client."""

import hashlib
import hmac
import json
import secrets
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.bootstrap import Container, build
from app.settings import Settings
from tests.worker.test_whatsapp_channel import FakeWhatsApp

SECRET = secrets.token_hex(16)
VERIFY = secrets.token_hex(8)
TOKEN = secrets.token_hex(8)


def settings(tmp_path: Path, **extra: object) -> Settings:
    return Settings(  # type: ignore[arg-type]
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'w.db'}",
        debounce_seconds=0,
        whatsapp_access_token=TOKEN,
        whatsapp_phone_number_id="123",
        whatsapp_verify_token=VERIFY,
        whatsapp_app_secret=SECRET,
        **extra,
    )


@pytest.fixture
def container(tmp_path: Path) -> Container:
    c = build(settings(tmp_path))
    assert c.whatsapp is not None
    c.whatsapp.client = FakeWhatsApp()  # type: ignore[assignment]
    return c


def signed(body: dict) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"X-Hub-Signature-256": sig, "Content-Type": "application/json"}


def test_verification_handshake(container: Container) -> None:
    with TestClient(create_app(container)) as client:
        ok = client.get(
            "/webhooks/whatsapp",
            params={"hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "42"},
        )
        assert (ok.status_code, ok.text) == (200, "42")
        bad = client.get(
            "/webhooks/whatsapp",
            params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"},
        )
        assert bad.status_code == 403


def test_unsigned_or_tampered_requests_are_rejected(container: Container) -> None:
    raw, headers = signed({"entry": []})
    with TestClient(create_app(container)) as client:
        assert client.post("/webhooks/whatsapp", content=raw).status_code == 401
        tampered = client.post("/webhooks/whatsapp", content=raw + b" ", headers=headers)
        assert tampered.status_code == 401
        assert (
            client.post("/webhooks/whatsapp", content=b"{", headers=signed({})[1]).status_code
            == 401
        )


def test_message_end_to_end(container: Container) -> None:
    fake = container.whatsapp.client  # type: ignore[union-attr]
    message = {"from": "346", "id": "wamid.1", "type": "text", "text": {"body": "hola"}}
    raw, headers = signed({"entry": [{"changes": [{"value": {"messages": [message]}}]}]})
    with TestClient(create_app(container)) as client:
        assert client.post("/webhooks/whatsapp", content=raw, headers=headers).json() == {
            "status": "ok"
        }
        container.whatsapp.dispatch_once()  # type: ignore[union-attr]
    assert "Lucía" in fake.sent[0][1]


def inbound(number: str, message_id: str, text: str) -> tuple[bytes, dict[str, str]]:
    message = {"from": number, "id": message_id, "type": "text", "text": {"body": text}}
    return signed({"entry": [{"changes": [{"value": {"messages": [message]}}]}]})


def test_a_web_process_leaves_turns_to_the_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = build(settings(tmp_path, role="web"))
    assert c.whatsapp is not None
    c.whatsapp.client = FakeWhatsApp()  # type: ignore[assignment]
    notified: list[UUID] = []
    monkeypatch.setattr(c.runner, "notify", notified.append)
    with TestClient(create_app(c)) as client:
        for message_id, text in (("wamid.1", "hola"), ("wamid.2", "sí")):
            raw, headers = inbound("346", message_id, text)
            assert (
                client.post("/webhooks/whatsapp", content=raw, headers=headers).status_code == 200
            )
        assert notified == []  # no debounce timer here: the turn won't run in this process
        cid = c.service.repo.conversation_for("whatsapp", "346")
        assert cid is not None
        assert [m.text for m in c.service.repo.pending_inbound(cid)] == ["sí"]


def test_a_single_process_runs_its_own_turns(
    container: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    notified: list[UUID] = []
    monkeypatch.setattr(container.runner, "notify", notified.append)
    with TestClient(create_app(container)) as client:
        for message_id, text in (("wamid.1", "hola"), ("wamid.2", "sí")):
            raw, headers = inbound("346", message_id, text)
            client.post("/webhooks/whatsapp", content=raw, headers=headers)
    cid = container.service.repo.conversation_for("whatsapp", "346")
    assert notified == [cid]


def test_not_configured_is_404(tmp_path: Path) -> None:
    c = build(Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'n.db'}"))
    with TestClient(create_app(c)) as client:
        assert client.get("/webhooks/whatsapp").status_code == 404


def test_partial_configuration_fails_fast(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="WHATSAPP_VERIFY_TOKEN"):
        build(
            Settings(
                _env_file=None,
                database_url=f"sqlite:///{tmp_path / 'p.db'}",
                whatsapp_access_token=TOKEN,
                whatsapp_phone_number_id="123",
            )
        )


def status_update(status: str, **extra: object) -> tuple[bytes, dict[str, str]]:
    report = {"id": "wamid.SECRET", "recipient_id": "34600000001", "status": status, **extra}
    return signed({"entry": [{"changes": [{"value": {"statuses": [report]}}]}]})


def test_a_failed_delivery_is_logged_with_its_reason_but_no_number(
    container: Container, caplog: pytest.LogCaptureFixture
) -> None:
    error = {"code": 131042, "title": "Payment issue for 34600000001"}
    raw, headers = status_update("failed", errors=[error])
    with TestClient(create_app(container)) as client, caplog.at_level("INFO", "app.whatsapp"):
        assert client.post("/webhooks/whatsapp", content=raw, headers=headers).status_code == 200
    assert "whatsapp_delivery_failed code=131042 title=Payment issue for <number>" in caplog.text
    assert "34600000001" not in caplog.text
    assert "wamid" not in caplog.text


def test_a_successful_delivery_is_logged_without_an_error(
    container: Container, caplog: pytest.LogCaptureFixture
) -> None:
    raw, headers = status_update("delivered")
    with TestClient(create_app(container)) as client, caplog.at_level("INFO", "app.whatsapp"):
        assert client.post("/webhooks/whatsapp", content=raw, headers=headers).status_code == 200
    assert "whatsapp_delivery status=delivered" in caplog.text
    assert "whatsapp_delivery_failed" not in caplog.text


def test_a_failure_without_details_is_still_logged(
    container: Container, caplog: pytest.LogCaptureFixture
) -> None:
    raw, headers = status_update("failed")
    with TestClient(create_app(container)) as client, caplog.at_level("INFO", "app.whatsapp"):
        assert client.post("/webhooks/whatsapp", content=raw, headers=headers).status_code == 200
    assert "whatsapp_delivery_failed code=None" in caplog.text


def test_an_invite_code_through_the_webhook_starts_the_screening(tmp_path: Path) -> None:
    c = build(settings(tmp_path, session_secret=SECRET, whatsapp_invite_number="15551868329"))
    assert c.whatsapp is not None
    assert c.whatsapp.invites is not None
    fake = FakeWhatsApp()
    c.whatsapp.client = fake  # type: ignore[assignment]
    cid = c.service.start_conversation("whatsapp", invited_by="rec", application_ref="APP-1")
    message = {
        "from": "346",
        "id": "wamid.invite",
        "type": "text",
        "text": {"body": f"Hola {c.whatsapp.invites.code(cid)}"},
    }
    raw, headers = signed({"entry": [{"changes": [{"value": {"messages": [message]}}]}]})
    with TestClient(create_app(c)) as client:
        assert client.post("/webhooks/whatsapp", content=raw, headers=headers).status_code == 200
    assert c.service.repo.conversation_for("whatsapp", "346") == cid
    assert c.whatsapp.dispatch_once() == 1
    assert fake.sent[0][0] == "346"


@pytest.mark.parametrize(
    "extra",
    [{"invite_only": True}, {"whatsapp_invite_number": "15551868329"}],
)
def test_the_webhook_ignores_a_new_number_with_no_code(
    tmp_path: Path, extra: dict[str, object]
) -> None:
    c = build(settings(tmp_path, session_secret=SECRET, **extra))
    assert c.whatsapp is not None
    fake = FakeWhatsApp()
    c.whatsapp.client = fake  # type: ignore[assignment]
    message = {"from": "346", "id": "wamid.x", "type": "text", "text": {"body": "hola"}}
    raw, headers = signed({"entry": [{"changes": [{"value": {"messages": [message]}}]}]})
    with TestClient(create_app(c)) as client:
        # 200, or Meta keeps retrying for 36 hours
        assert client.post("/webhooks/whatsapp", content=raw, headers=headers).status_code == 200
    assert c.service.repo.conversation_for("whatsapp", "346") is None
    assert c.whatsapp.dispatch_once() == 0
    assert fake.sent == []
