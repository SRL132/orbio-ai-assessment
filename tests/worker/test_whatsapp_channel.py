"""WhatsApp channel end to end on a fake client: webhook in, outbox out, reminders, failures."""

from datetime import timedelta

import httpx
import pytest

from app.adapters.calendar.sql_calendar import SqlCalendar
from app.adapters.clock.clocks import FakeClock
from app.adapters.llm.fake import TemplateResponder
from app.adapters.llm.keyword import KeywordExtractor
from app.adapters.storage.sql import SqlRepository, make_engine
from app.domain.config import ClientConfig
from app.domain.models import Status
from app.domain.ports import Ports
from app.worker.service import ScreeningService
from app.worker.whatsapp import MAX_ATTEMPTS, WhatsAppChannel

PHONE = "34600111222"


class FakeWhatsApp:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, list[str] | None]] = []
        self.templates: list[tuple[str, str, str]] = []
        self.fail_with: int | None = None  # HTTP status to fail with

    def _maybe_fail(self) -> None:
        if self.fail_with is not None:
            request = httpx.Request("POST", "https://graph.facebook.com")
            raise httpx.HTTPStatusError(
                "fail", request=request, response=httpx.Response(self.fail_with, request=request)
            )

    def send(self, to: str, text: str, options: list[str] | None = None) -> str:
        self._maybe_fail()
        self.sent.append((to, text, options))
        return "wamid.out"

    def send_template(self, to: str, name: str, language_code: str) -> str:
        self._maybe_fail()
        self.templates.append((to, name, language_code))
        return "wamid.tpl"


@pytest.fixture
def setup(config: ClientConfig, clock: FakeClock):
    engine = make_engine("sqlite:///:memory:")
    ports = Ports(
        KeywordExtractor(), TemplateResponder(), clock, SqlCalendar(engine, config, clock)
    )
    service = ScreeningService(SqlRepository(engine, clock), config, ports)
    fake = FakeWhatsApp()
    channel = WhatsAppChannel(service, fake)  # type: ignore[arg-type]
    yield service, channel, fake, clock
    engine.dispose()


_seq = iter(range(10_000))


def incoming(text: str | None, phone: str = PHONE) -> dict:
    message: dict = {"from": phone, "id": f"wamid.{next(_seq)}"}
    message |= {"type": "text", "text": {"body": text}} if text is not None else {"type": "image"}
    return {"entry": [{"changes": [{"value": {"messages": [message]}}]}]}


def say(service: ScreeningService, channel: WhatsAppChannel, text: str) -> None:
    for cid in channel.handle(incoming(text)):
        service.process_pending(cid)
    channel.dispatch_once()


def test_first_message_opens_a_conversation_with_buttons(setup) -> None:
    _service, channel, fake, _ = setup
    assert channel.handle(incoming("hola")) == []  # the greeting asks for consent first
    assert channel.dispatch_once() == 1
    to, text, options = fake.sent[0]
    assert to == PHONE
    assert "Lucía" in text
    assert options == ["Sí", "No"]
    assert channel.dispatch_once() == 0  # nothing left in the outbox


def test_screening_over_whatsapp(setup) -> None:
    service, channel, fake, _ = setup
    channel.handle(incoming("hola"))
    for text in ["Sí", "Ana López", "Moto"]:
        say(service, channel, text)
    assert fake.sent[-1][1].startswith("¿En qué ciudad")
    assert fake.sent[-2][2] == ["Moto", "Coche", "Ambos", "No tengo"]


def test_duplicate_deliveries_are_ignored(setup) -> None:
    _service, channel, _, _ = setup
    channel.handle(incoming("hola"))
    payload = incoming("Sí")
    assert len(channel.handle(payload)) == 1
    assert channel.handle(payload) == []  # Meta retried the same message


def test_reminders_reach_the_phone_inside_the_window(setup) -> None:
    service, channel, fake, clock = setup
    channel.handle(incoming("hola"))
    say(service, channel, "Sí")
    sent_before = len(fake.sent)
    clock.advance(hours=2)
    service.send_due_reminders()
    channel.dispatch_once()
    assert "Solo nos faltan" in fake.sent[-1][1]
    clock.advance(hours=21)  # 23 h after the last message: the WhatsApp schedule
    service.send_due_reminders()
    channel.dispatch_once()
    assert len(fake.sent) == sent_before + 2
    assert fake.templates == []


def test_outside_the_window_needs_a_template(setup) -> None:
    _service, channel, fake, clock = setup
    channel.handle(incoming("hola"))
    clock.advance(hours=25)  # greeting never sent: now outside the 24 h window
    assert channel.dispatch_once() == 0
    assert fake.sent == []
    channel.handle(incoming("hola", phone="34600999888"))
    channel.reminder_template = "screening_reminder"
    clock.advance(hours=25)
    channel.dispatch_once()
    assert fake.templates == [("34600999888", "screening_reminder", "es")]


def test_non_text_messages_get_a_text_only_reply(setup) -> None:
    _service, channel, fake, _ = setup
    channel.handle(incoming("hola"))
    channel.handle(incoming(None))
    channel.dispatch_once()
    assert "solo puedo leer mensajes de texto" in fake.sent[-1][1]


def test_hello_after_a_finished_conversation_starts_again(setup) -> None:
    service, channel, _fake, _ = setup
    channel.handle(incoming("hola"))
    for text in ["Sí", "Luis Pérez", "No tengo", "No"]:
        say(service, channel, text)
    first = service.repo.conversation_for("whatsapp", PHONE)
    assert first is not None
    state = service.repo.get(first)
    assert state is not None
    assert state.status == Status.DISQUALIFIED
    channel.handle(incoming("hola"))
    assert service.repo.conversation_for("whatsapp", PHONE) != first


def test_server_errors_are_retried_then_given_up(setup) -> None:
    _service, channel, fake, _ = setup
    channel.handle(incoming("hola"))
    fake.fail_with = 503
    for _ in range(MAX_ATTEMPTS):
        assert channel.dispatch_once() == 0
    fake.fail_with = None
    assert channel.dispatch_once() == 0  # gave up after MAX_ATTEMPTS
    assert fake.sent == []


def test_client_errors_are_not_retried(setup) -> None:
    _service, channel, fake, _ = setup
    channel.handle(incoming("hola"))
    fake.fail_with = 400
    channel.dispatch_once()
    fake.fail_with = None
    assert channel.dispatch_once() == 0


def test_window_clock(setup) -> None:
    _, _, _, clock = setup
    start = clock.now()
    clock.advance(hours=1)
    assert clock.now() - start == timedelta(hours=1)
