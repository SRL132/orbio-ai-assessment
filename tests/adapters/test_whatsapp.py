import hashlib
import hmac
import json

import httpx
import pytest

from app.adapters.channels.quick_replies import quick_replies
from app.adapters.channels.whatsapp import (
    WhatsAppClient,
    build_message,
    build_template,
    parse_webhook,
    valid_signature,
)


def test_text_message() -> None:
    body = build_message("34600111222", "¿Cómo te llamas?")
    assert body["type"] == "text"
    assert body["text"] == {"body": "¿Cómo te llamas?", "preview_url": False}
    assert body["messaging_product"] == "whatsapp"


def test_up_to_three_short_options_are_reply_buttons() -> None:
    body = build_message("1", "¿Empezamos?", ["Sí", "No"])
    interactive = body["interactive"]
    assert interactive["type"] == "button"
    assert [b["reply"]["title"] for b in interactive["action"]["buttons"]] == ["Sí", "No"]


def test_more_options_become_a_list() -> None:
    body = build_message("1", "¿Carnet?", ["Moto", "Coche", "Ambos", "No tengo"])
    interactive = body["interactive"]
    assert interactive["type"] == "list"
    rows = interactive["action"]["sections"][0]["rows"]
    assert [r["title"] for r in rows] == ["Moto", "Coche", "Ambos", "No tengo"]


def test_long_option_falls_back_to_text() -> None:
    assert build_message("1", "x", ["a" * 30])["type"] == "text"


def test_template() -> None:
    body = build_template("1", "screening_reminder", "es")
    assert body["template"] == {"name": "screening_reminder", "language": {"code": "es"}}


def _payload(*messages: dict) -> dict:
    return {"entry": [{"changes": [{"value": {"messages": list(messages), "statuses": [{}]}}]}]}


def test_parse_text_buttons_lists_and_media() -> None:
    found = parse_webhook(
        _payload(
            {"from": "346", "id": "wamid.1", "type": "text", "text": {"body": "hola"}},
            {
                "from": "346",
                "id": "wamid.2",
                "type": "interactive",
                "interactive": {
                    "type": "button_reply",
                    "button_reply": {"id": "Sí", "title": "Sí"},
                },
            },
            {
                "from": "346",
                "id": "wamid.3",
                "type": "interactive",
                "interactive": {
                    "type": "list_reply",
                    "list_reply": {"id": "Moto", "title": "Moto"},
                },
            },
            {"from": "346", "id": "wamid.4", "type": "audio", "audio": {"id": "x"}},
        )
    )
    assert [(m.message_id, m.text) for m in found] == [
        ("wamid.1", "hola"),
        ("wamid.2", "Sí"),
        ("wamid.3", "Moto"),
        ("wamid.4", None),
    ]
    assert parse_webhook({"entry": [{"changes": [{"value": {"statuses": [{}]}}]}]}) == []


def test_signature() -> None:
    raw = b'{"a": 1}'
    good = "sha256=" + hmac.new(b"secret", raw, hashlib.sha256).hexdigest()
    assert valid_signature("secret", raw, good)
    assert not valid_signature("secret", raw + b" ", good)
    assert not valid_signature("secret", raw, None)
    assert not valid_signature("secret", raw, "md5=abc")


def test_client_posts_to_graph_api() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.out"}]})

    client = WhatsAppClient("token", "12345", transport=httpx.MockTransport(handler))
    assert client.send("346", "hola", ["Sí", "No"]) == "wamid.out"
    assert client.send_template("346", "reminder", "es") == "wamid.out"
    assert seen[0].url.path == "/v26.0/12345/messages"
    assert seen[0].headers["authorization"] == "Bearer token"
    assert json.loads(seen[0].content)["type"] == "interactive"


def test_client_errors_raise() -> None:
    client = WhatsAppClient(
        "t", "1", transport=httpx.MockTransport(lambda _r: httpx.Response(400, json={}))
    )
    with pytest.raises(httpx.HTTPStatusError):
        client.send("346", "hola")


def test_phone_number_id_is_required() -> None:
    with pytest.raises(ValueError, match="WHATSAPP_PHONE_NUMBER_ID"):
        WhatsAppClient("t", "")


@pytest.mark.parametrize(
    ("text", "options"),
    [
        (
            "¿Tienes carnet? Responde: Moto · Coche · Ambos · No tengo",
            ["Moto", "Coche", "Ambos", "No tengo"],
        ),
        ("Bla. ¿Empezamos?", ["Sí", "No"]),
        ("Shall we start? (yes / no)", ["Yes", "No"]),
        (
            "Elige:\n1) mar 3/11 11:00\n2) mar 3/11 13:00\nResponde con el número.",
            ["1", "2", "Ninguno"],
        ),
        ("¿Cómo te llamas?", []),
    ],
)
def test_quick_replies(text: str, options: list[str]) -> None:
    assert quick_replies(text) == options
