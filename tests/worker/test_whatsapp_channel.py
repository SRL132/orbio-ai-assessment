"""WhatsApp channel end to end on a fake client: webhook in, outbox out, reminders, failures."""

import threading
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from prometheus_client import REGISTRY

from app.adapters.calendar.sql_calendar import SqlCalendar
from app.adapters.channels.whatsapp_invites import WhatsAppInvites
from app.adapters.clock.clocks import FakeClock
from app.adapters.llm.fake import TemplateResponder
from app.adapters.llm.keyword import KeywordExtractor
from app.adapters.storage.rate_limit import SqlRateLimiter
from app.adapters.storage.sql import SqlRepository, make_engine
from app.domain.config import ClientConfig
from app.domain.models import Channel, Stage, Status
from app.domain.ports import Ports
from app.worker import whatsapp
from app.worker.service import ScreeningService
from app.worker.whatsapp import MAX_ATTEMPTS, ContactBusy, WhatsAppChannel, contact_lock_name

PHONE = "34600111222"


class FakeWhatsApp:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, list[str] | None]] = []
        self.templates: list[tuple[str, str, str]] = []
        self.fail_with: int | None = None  # HTTP status to fail with
        self.typing: list[str] = []

    def show_typing(self, message_id: str) -> None:
        self._maybe_fail()
        self.typing.append(message_id)

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
    service = ScreeningService(SqlRepository(engine, clock, config.client_id), config, ports)
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
    sent = REGISTRY.get_sample_value("sazon_whatsapp_messages_total", {"result": "sent"}) or 0
    assert channel.handle(incoming("hola")) == []  # the greeting asks for consent first
    assert channel.dispatch_once() == 1
    assert REGISTRY.get_sample_value("sazon_whatsapp_messages_total", {"result": "sent"}) == (
        sent + 1
    )
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


def test_typing_shows_only_for_a_message_that_gets_a_turn(setup) -> None:
    _, channel, fake, _ = setup
    channel.handle(incoming("hola"))  # opens the conversation: the greeting needs no LLM turn
    assert fake.typing == []
    answer = incoming("Sí")
    channel.handle(answer)
    assert fake.typing == [answer["entry"][0]["changes"][0]["value"]["messages"][0]["id"]]


def test_a_failed_typing_indicator_does_not_lose_the_message(setup) -> None:
    service, channel, fake, _ = setup
    channel.handle(incoming("hola"))
    fake.fail_with = 500
    to_process = channel.handle(incoming("Sí"))
    fake.fail_with = None
    assert len(to_process) == 1
    service.process_pending(to_process[0])
    assert fake.typing == []
    assert service.get(to_process[0]).stage == Stage.COLLECTING


def test_duplicate_deliveries_are_ignored(setup) -> None:
    _service, channel, _, _ = setup
    channel.handle(incoming("hola"))
    payload = incoming("Sí")
    assert len(channel.handle(payload)) == 1
    assert channel.handle(payload) == []  # Meta retried the same message


def test_a_retried_first_message_is_not_taken_as_an_answer(setup) -> None:
    service, channel, _, _ = setup
    first = incoming("hola")
    channel.handle(first)
    assert channel.handle(first) == []  # without the claim, "hola" would answer the consent
    cid = service.repo.conversation_for("whatsapp", PHONE)
    assert service.repo.pending_inbound(cid) == []
    assert channel.dispatch_once() == 1  # one greeting, not two


def test_a_retried_restart_opens_one_conversation(setup) -> None:
    service, channel, _, _ = setup
    channel.handle(incoming("hola"))
    say(service, channel, "No")  # declines consent: the conversation ends
    old = service.repo.conversation_for("whatsapp", PHONE)
    restart = incoming("hola")
    channel.handle(restart)
    new = service.repo.conversation_for("whatsapp", PHONE)
    channel.handle(restart)
    assert old != new
    assert service.repo.conversation_for("whatsapp", PHONE) == new


def test_a_retried_photo_gets_one_text_only_notice(setup) -> None:
    service, channel, fake, _ = setup
    channel.handle(incoming("hola"))
    say(service, channel, "Sí")
    before = len(fake.sent)
    photo = incoming(None)
    channel.handle(photo)
    channel.handle(photo)
    channel.dispatch_once()
    assert len(fake.sent) == before + 1


def test_a_failed_delivery_is_handled_on_the_retry(setup, monkeypatch) -> None:
    service, channel, _, _ = setup
    first = incoming("hola")

    def broken(channel_name: str) -> None:
        raise RuntimeError("database down")

    monkeypatch.setattr(service, "start_conversation", broken)
    with pytest.raises(RuntimeError):
        channel.handle(first)  # the webhook answers 500, so Meta retries
    monkeypatch.undo()
    channel.handle(first)
    assert service.repo.conversation_for("whatsapp", PHONE) is not None
    assert channel.dispatch_once() == 1


def test_a_message_stored_before_claims_existed_is_still_deduplicated(setup) -> None:
    service, channel, _, _ = setup
    channel.handle(incoming("hola"))
    answer = incoming("Sí")
    channel.handle(answer)
    message_id = answer["entry"][0]["changes"][0]["value"]["messages"][0]["id"]
    service.repo.release_delivery("whatsapp", message_id)  # as if stored before the upgrade
    assert channel.handle(answer) == []  # the messages table still catches it


def test_two_first_messages_at_once_open_one_conversation(
    tmp_path, config: ClientConfig, clock: FakeClock, monkeypatch
) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'race.db'}")  # a file: threads share it for real
    ports = Ports(KeywordExtractor(), TemplateResponder(), clock)
    service = ScreeningService(SqlRepository(engine, clock, config.client_id), config, ports)
    channel = WhatsAppChannel(service, FakeWhatsApp())  # type: ignore[arg-type]
    start = service.start_conversation

    def slow_start(channel_name: Channel) -> UUID:
        time.sleep(0.2)  # both messages are past the first lookup before either links the number
        return start(channel_name)

    monkeypatch.setattr(service, "start_conversation", slow_start)
    barrier = threading.Barrier(2)
    errors: list[str] = []

    def deliver(text: str) -> None:
        barrier.wait()
        try:
            channel.handle(incoming(text))
        except Exception as exc:
            errors.append(type(exc).__name__)

    threads = [threading.Thread(target=deliver, args=(t,)) for t in ("hola", "soy Ana")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    with engine.connect() as connection:
        conversations = connection.scalar(sa.text("SELECT count(*) FROM conversations"))
        leases = connection.scalar(sa.text("SELECT count(*) FROM job_leases"))
    cid = service.repo.conversation_for("whatsapp", PHONE)
    pending = [m.text for m in service.repo.pending_inbound(cid)] if cid else []
    engine.dispose()
    assert errors == []
    assert conversations == 1
    assert len(pending) == 1  # the message that lost the race joined the new conversation
    assert leases == 0  # the lock row is gone once released


def test_a_contact_lock_held_too_long_fails_so_meta_retries(setup, monkeypatch) -> None:
    service, channel, _, _ = setup
    monkeypatch.setattr(whatsapp, "CONTACT_LOCK_WAIT", timedelta(seconds=0.1))
    name = contact_lock_name(PHONE)
    assert service.repo.try_job_lease(name, "other", timedelta(minutes=1))
    first = incoming("hola")
    with pytest.raises(ContactBusy):
        channel.handle(first)
    service.repo.release_job_lease(name, "other")
    channel.handle(first)  # the retry: its claim was given back
    assert service.repo.conversation_for("whatsapp", PHONE) is not None


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


def test_only_one_process_dispatches_the_outbox(setup) -> None:
    service, channel, fake, clock = setup
    other_process = WhatsAppChannel(service, fake)  # type: ignore[arg-type]
    channel.handle(incoming("hola"))
    assert channel.dispatch_once() == 1  # this process holds the dispatcher lease
    channel.handle(incoming("hola", phone="34600000002"))
    assert other_process.dispatch_once() == 0  # so the other one doesn't send anything
    assert channel.dispatch_once() == 1
    clock.advance(seconds=31)  # the first process stopped renewing (it died)
    channel.handle(incoming("hola", phone="34600000003"))
    assert other_process.dispatch_once() == 1
    assert len(fake.sent) == 3  # each greeting sent once


# --- Invites with a code ---------------------------------------------------------------------


@pytest.fixture
def invited(setup):
    service, channel, fake, clock = setup
    channel.invites = WhatsAppInvites("a-test-secret", "+1 (555) 186-8329")
    channel.invite_only = True
    cid = service.start_conversation("whatsapp", invited_by="rec", application_ref="APP-1")
    return service, channel, fake, clock, cid


def typed_in(channel: WhatsAppChannel, cid: UUID) -> str:
    return parse_qs(urlparse(channel.invites.url(cid)).query)["text"][0]


def test_invite_link_opens_whatsapp_with_the_code_typed_in(invited) -> None:
    _, channel, _, _, cid = invited
    url = channel.invites.url(cid)
    assert url.startswith("https://wa.me/15551868329?text=Hola%20inv-")
    assert typed_in(channel, cid) == f"Hola {channel.invites.code(cid)}"
    assert channel.invites.chat_url() == "https://wa.me/15551868329"


def test_sending_the_code_ties_the_number_to_the_application_and_starts_the_screening(
    invited,
) -> None:
    service, channel, fake, clock, cid = invited
    assert channel.dispatch_once() == 0  # the greeting waits: there's nobody to send it to yet

    clock.advance(hours=48)  # applied two days ago, and the 24 h window only starts now
    assert channel.handle(incoming(typed_in(channel, cid))) == []
    assert service.repo.conversation_for("whatsapp", PHONE) == cid
    state = service.get(cid)
    assert state.invite_pending is False
    assert state.application_ref == "APP-1"
    assert channel.dispatch_once() == 1
    assert fake.sent[0][0] == PHONE
    assert "Lucía" in fake.sent[0][1]

    say(service, channel, "Sí")
    assert service.get(cid).stage != Stage.CONSENT
    assert all("inv-" not in m.text for m in service.messages(cid))


def test_a_code_works_once(invited) -> None:
    service, channel, _, _, cid = invited
    channel.handle(incoming(typed_in(channel, cid)))
    channel.handle(incoming(typed_in(channel, cid), phone="34600999888"))
    assert service.repo.conversation_for("whatsapp", "34600999888") is None
    assert service.repo.conversation_for("whatsapp", PHONE) == cid


def test_tapping_the_link_again_keeps_the_conversation_and_hides_the_code(invited) -> None:
    service, channel, fake, _, cid = invited
    channel.handle(incoming(typed_in(channel, cid)))
    channel.dispatch_once()
    say(service, channel, "Sí")  # past consent, so their messages are kept as written
    asked = len(fake.sent)
    say(service, channel, typed_in(channel, cid))
    assert service.repo.conversation_for("whatsapp", PHONE) == cid
    assert [m.text for m in service.messages(cid) if m.direction == "in"][-1] == "Hola"
    assert len(fake.sent) > asked  # they're asked again rather than left in silence


def test_a_message_that_is_only_a_used_code_still_gets_an_answer(invited) -> None:
    service, channel, _, _, cid = invited
    channel.handle(incoming(typed_in(channel, cid)))
    channel.handle(incoming(channel.invites.code(cid)))
    assert [m.text for m in service.messages(cid) if m.direction == "in"] == ["Hola"]


@pytest.mark.parametrize("bad", ["forged", "expired", "erased", "web chat"])
def test_a_code_that_cannot_be_claimed_gets_no_conversation_and_no_reply(
    invited, bad: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, channel, fake, _, cid = invited
    target = cid
    if bad == "erased":
        target = uuid4()
    elif bad == "web chat":
        target = service.start_conversation("chat", invited_by="rec")
    text = typed_in(channel, target)
    if bad == "forged":
        text = text[:-3] + ("aaa" if not text.endswith("aaa") else "bbb")
    elif bad == "expired":
        real = time.time()
        monkeypatch.setattr("itsdangerous.timed.time.time", lambda: real + 8 * 24 * 3600)

    assert channel.handle(incoming(text)) == []
    assert service.repo.conversation_for("whatsapp", PHONE) is None
    channel.dispatch_once()
    assert fake.sent == []
    if bad != "erased":
        assert service.get(target).invite_pending is True


def test_codes_are_ordinary_text_when_invites_are_off(setup) -> None:
    service, channel, _, _ = setup
    cid = service.start_conversation("whatsapp", invited_by="rec")
    code = WhatsAppInvites("a-test-secret", "1").code(cid)
    channel.handle(incoming(f"Hola {code}"))
    assert service.get(cid).invite_pending is True


@pytest.mark.parametrize("text", ["hola", None, "x" * 4000])
def test_a_new_number_with_no_code_costs_nothing(invited, text: str | None) -> None:
    service, channel, fake, _, _ = invited
    with service.repo._sessions() as session:
        before = session.scalar(sa.text("select count(*) from conversations"))
    ignored = REGISTRY.get_sample_value("sazon_whatsapp_no_invite_total") or 0
    message = incoming(text)
    assert channel.handle(message) == []
    assert channel.dispatch_once() == 0
    assert (fake.sent, fake.typing, fake.templates) == ([], [], [])
    assert service.repo.conversation_for("whatsapp", PHONE) is None
    with service.repo._sessions() as session:
        assert session.scalar(sa.text("select count(*) from conversations")) == before
    assert REGISTRY.get_sample_value("sazon_whatsapp_no_invite_total") == ignored + 1


def test_invite_only_ignores_new_numbers_even_while_invites_are_web_links(setup) -> None:
    service, channel, fake, _ = setup
    channel.invite_only = True
    assert channel.handle(incoming("hola")) == []
    assert channel.dispatch_once() == 0
    assert fake.sent == []
    assert service.repo.conversation_for("whatsapp", PHONE) is None


def test_invite_only_lets_a_conversation_already_under_way_carry_on(invited) -> None:
    service, channel, fake, _, cid = invited
    channel.handle(incoming(typed_in(channel, cid)))
    channel.dispatch_once()
    say(service, channel, "Sí")
    say(service, channel, "Ana López")
    assert service.get(cid).candidate.full_name is not None
    assert len(fake.sent) >= 3


def test_invite_only_gives_a_finished_candidate_no_new_conversation(invited) -> None:
    service, channel, _, _, cid = invited
    channel.handle(incoming(typed_in(channel, cid)))
    state = service.get(cid)
    state.stage, state.status = Stage.CLOSED, Status.QUALIFIED
    service.repo.save_turn(state, [], [], [], lease_owner=None)
    assert channel.handle(incoming("hola")) == []
    assert service.repo.conversation_for("whatsapp", PHONE) == cid


# --- docs/failure-modes.md, gaps 11, 12, 14 and 15 ------------------------------------------------


def _typed(kind: str, phone: str = PHONE) -> dict:
    payload = incoming(None, phone)
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["type"] = kind
    return payload


def _status(status: str, phone: str = PHONE, code: int = 131026) -> dict:
    report = {"status": status, "recipient_id": phone, "errors": [{"code": code, "title": "x"}]}
    return {"entry": [{"changes": [{"value": {"statuses": [report]}}]}]}


def test_a_reaction_is_not_a_message(setup) -> None:
    service, channel, fake, _ = setup
    channel.handle(incoming("hola"))
    channel.dispatch_once()
    sent = len(fake.sent)
    assert channel.handle(_typed("reaction")) == []
    channel.dispatch_once()
    assert len(fake.sent) == sent
    cid = service.repo.conversation_for("whatsapp", PHONE)
    assert all(e.type != "non_text_message" for e in service.repo.events(cid))


def test_a_reaction_opens_no_conversation(setup) -> None:
    service, channel, _fake, _ = setup
    channel.handle(_typed("reaction"))
    assert service.repo.conversation_for("whatsapp", PHONE) is None


def test_a_burst_of_photos_gets_one_paid_notice_an_hour(setup) -> None:
    service, channel, fake, clock = setup
    channel.handle(incoming("hola"))
    channel.dispatch_once()
    sent = len(fake.sent)
    for _ in range(3):
        channel.handle(incoming(None))
    channel.dispatch_once()
    assert len(fake.sent) == sent + 1
    cid = service.repo.conversation_for("whatsapp", PHONE)
    noticed = [e for e in service.repo.events(cid) if e.type == "non_text_message"]
    assert len(noticed) == 3  # still counted, for the voice-note decision

    clock.advance(minutes=59)
    channel.handle(incoming(None))
    channel.dispatch_once()
    assert len(fake.sent) == sent + 1
    clock.advance(minutes=1)
    channel.handle(incoming(None))
    channel.dispatch_once()
    assert len(fake.sent) == sent + 2


def test_a_failed_delivery_sends_the_candidate_to_review(setup) -> None:
    service, channel, _fake, _ = setup
    channel.handle(incoming("hola"))
    channel.handle_delivery_failures(_status("failed"))
    state = service.repo.get(service.repo.conversation_for("whatsapp", PHONE))
    assert state.status == Status.NEEDS_REVIEW
    assert [r.reason for r in state.review_reasons] == ["unreachable"]
    assert not state.went_silent
    failed = next(
        e for e in service.repo.events(state.conversation_id) if e.type == "delivery_failed"
    )
    assert failed.data == {"code": 131026}


def test_other_delivery_reports_change_nothing(setup) -> None:
    service, channel, _fake, _ = setup
    channel.handle(incoming("hola"))
    channel.handle_delivery_failures(_status("delivered"))
    channel.handle_delivery_failures(_status("failed", phone="34999999999"))  # nobody we know
    state = service.repo.get(service.repo.conversation_for("whatsapp", PHONE))
    assert state.status == Status.IN_PROGRESS


def test_a_send_we_give_up_on_sends_the_candidate_to_review(setup) -> None:
    service, channel, fake, _ = setup
    channel.handle(incoming("hola"))
    fake.fail_with = 400  # not retryable
    channel.dispatch_once()
    state = service.repo.get(service.repo.conversation_for("whatsapp", PHONE))
    assert state.status == Status.NEEDS_REVIEW
    assert [r.reason for r in state.review_reasons] == ["unreachable"]


def test_a_send_that_will_be_retried_changes_nothing(setup) -> None:
    service, channel, fake, _ = setup
    channel.handle(incoming("hola"))
    fake.fail_with = 503
    channel.dispatch_once()
    state = service.repo.get(service.repo.conversation_for("whatsapp", PHONE))
    assert state.status == Status.IN_PROGRESS


def _finished_with_interview(service, channel, at) -> UUID:
    channel.handle(incoming("hola"))
    cid = service.repo.conversation_for("whatsapp", PHONE)
    state = service.repo.get(cid)
    state.status, state.stage, state.interview_at = Status.QUALIFIED, Stage.CLOSED, at
    service.repo.save_turn(state, [], [], [])
    return cid


def test_hello_before_a_booked_interview_keeps_the_conversation(setup) -> None:
    service, channel, _fake, clock = setup
    cid = _finished_with_interview(service, channel, clock.now() + timedelta(days=2))
    channel.handle(incoming("hola"))
    assert service.repo.conversation_for("whatsapp", PHONE) == cid


def test_hello_after_the_interview_has_passed_starts_again(setup) -> None:
    service, channel, _fake, clock = setup
    cid = _finished_with_interview(service, channel, clock.now() - timedelta(days=2))
    channel.handle(incoming("hola"))
    assert service.repo.conversation_for("whatsapp", PHONE) != cid


def test_a_flood_from_one_number_is_cut_off(config: ClientConfig, clock: FakeClock) -> None:
    engine = make_engine("sqlite:///:memory:")
    ports = Ports(KeywordExtractor(), TemplateResponder(), clock)
    service = ScreeningService(SqlRepository(engine, clock, config.client_id), config, ports)
    limiter = SqlRateLimiter(engine, clock, config.client_id)
    channel = WhatsAppChannel(
        service,
        FakeWhatsApp(),  # type: ignore[arg-type]
        limiter=limiter,
        messages_per_minute=2,
    )
    before = REGISTRY.get_sample_value("sazon_rate_limited_total", {"endpoint": "whatsapp"}) or 0
    channel.handle(incoming("hola"))
    assert channel.handle(incoming("sí"))
    assert channel.handle(incoming("otro")) == []
    assert channel.handle(incoming("uno", phone="34600999888")) == []  # another number: its first
    assert service.repo.conversation_for("whatsapp", "34600999888") is not None
    assert REGISTRY.get_sample_value("sazon_rate_limited_total", {"endpoint": "whatsapp"}) == (
        before + 1
    )
    clock.advance(minutes=1, seconds=1)
    assert channel.handle(incoming("ya")) != []
    engine.dispose()
