"""A provider outage: the candidate's message is held, retried, and never dropped silently."""

from datetime import date, timedelta

import pytest

from app import metrics
from app.adapters.clock.clocks import FakeClock
from app.adapters.llm.fake import ScriptedExtractor
from app.domain import engine
from app.domain.config import ClientConfig
from app.domain.models import (
    ConversationState,
    EventType,
    ExtractionResult,
    InboundMessage,
    Stage,
)
from app.domain.ports import LLMUnavailable, Ports
from tests.conftest import ex
from tests.worker.test_service import make_service


class Outage(ScriptedExtractor):
    """Down for the next `failures` calls, then answers from the script."""

    def __init__(self, failures: int = 0, error: Exception | None = None) -> None:
        super().__init__()
        self.failures = failures
        self.error = error or LLMUnavailable("extract: ConnectError")

    def extract(
        self, state: ConversationState, text: str, config: ClientConfig, today: date
    ) -> ExtractionResult:
        if self.failures:
            self.failures -= 1
            self.seen.append(text)
            raise self.error
        return super().extract(state, text, config, today)


@pytest.fixture
def outage(ports: Ports) -> tuple[Outage, Ports]:
    down = Outage()
    return down, Ports(extractor=down, responder=ports.responder, clock=ports.clock)


def in_progress(config: ClientConfig) -> ConversationState:
    return ConversationState(client_id=config.client_id, stage=Stage.COLLECTING)


def turn(state: ConversationState, config: ClientConfig, ports: Ports) -> engine.TurnResult:
    message = InboundMessage(
        conversation_id=state.conversation_id, text="tengo moto", received_at=ports.clock.now()
    )
    return engine.turn(state, message, config, ports)


# --- Engine ---------------------------------------------------------------------------------


def test_first_failure_holds_the_message_with_one_holding_reply(
    config: ClientConfig, outage: tuple[Outage, Ports], clock: FakeClock
) -> None:
    down, ports = outage
    down.failures = 1
    result = turn(in_progress(config), config, ports)
    assert result.hold_inbound
    assert result.replies == [config.template("es", "technical_hold")]
    assert result.state.llm_failures == 1
    first_delay = timedelta(seconds=config.llm_retry_seconds[0])
    assert result.state.retry_turn_at == clock.now() + first_delay
    failed = [e for e in result.events if e.type == EventType.EXTRACTOR_FAILED]
    assert failed[0].data == {"error": "LLMUnavailable", "attempt": 1, "gave_up": False}


def test_later_failures_back_off_without_repeating_the_holding_reply(
    config: ClientConfig, outage: tuple[Outage, Ports], clock: FakeClock
) -> None:
    down, ports = outage
    down.failures = 2
    state = turn(in_progress(config), config, ports).state
    result = turn(state, config, ports)
    assert result.hold_inbound
    assert result.replies == []  # told them once already
    assert result.state.retry_turn_at == clock.now() + timedelta(
        seconds=config.llm_retry_seconds[1]
    )


def test_when_retries_run_out_the_candidate_is_told_and_the_message_released(
    config: ClientConfig, outage: tuple[Outage, Ports]
) -> None:
    down, ports = outage
    retries = len(config.llm_retry_seconds)
    down.failures = retries + 1
    state = in_progress(config)
    for _ in range(retries):
        state = turn(state, config, ports).state
    result = turn(state, config, ports)
    assert not result.hold_inbound
    assert result.replies == [config.template("es", "technical_unavailable")]
    assert result.state.llm_failures == 0
    assert result.state.retry_turn_at is None
    assert result.state.next_reminder_at is not None  # reminders still follow
    assert any(e.data.get("gave_up") for e in result.events)


def test_a_bad_answer_is_asked_again_at_once_not_held(config: ClientConfig, ports: Ports) -> None:
    bad = Outage(failures=1, error=ValueError("Invalid JSON"))
    result = turn(
        in_progress(config),
        config,
        Ports(extractor=bad, responder=ports.responder, clock=ports.clock),
    )
    assert not result.hold_inbound
    assert result.replies == [config.template("es", "technical_retry")]


def test_success_after_an_outage_clears_the_hold(
    config: ClientConfig, outage: tuple[Outage, Ports]
) -> None:
    down, ports = outage
    down.failures = 1
    state = turn(in_progress(config), config, ports).state
    result = turn(state, config, ports)
    assert not result.hold_inbound
    assert result.state.llm_failures == 0
    assert result.state.retry_turn_at is None


# --- Service --------------------------------------------------------------------------------


def test_held_message_is_retried_after_the_delay_and_then_processed(
    config: ClientConfig, outage: tuple[Outage, Ports], clock: FakeClock
) -> None:
    down, ports = outage
    service = make_service(config, ports, clock)
    cid = service.start_conversation()
    down.failures = 1
    service.receive(cid, "sí, acepto")
    held_before = metrics.TURNS_HELD.labels("held")._value.get()

    assert service.process_pending(cid) is not None
    assert [m.text for m in service.repo.pending_inbound(cid)] == ["sí, acepto"]  # kept
    assert metrics.TURNS_HELD.labels("held")._value.get() == held_before + 1
    assert service.messages(cid)[-1].text == config.template("es", "technical_hold")

    assert service.process_pending(cid) is None  # not before the retry time
    assert down.seen == ["sí, acepto"]

    clock.advance(seconds=config.llm_retry_seconds[0])
    down.push(ex(yes_no=True))
    assert service.process_pending(cid) is not None
    assert service.repo.pending_inbound(cid) == []
    state = service.get(cid)
    assert state is not None
    assert state.stage != Stage.CONSENT  # the held answer was used
    assert state.turns == 1  # the held attempt doesn't count as a turn


def test_a_message_sent_while_held_joins_the_retry(
    config: ClientConfig, outage: tuple[Outage, Ports], clock: FakeClock
) -> None:
    down, ports = outage
    service = make_service(config, ports, clock)
    cid = service.start_conversation()
    down.failures = 1
    service.receive(cid, "sí")
    service.process_pending(cid)
    service.receive(cid, "¿hola?")
    clock.advance(seconds=config.llm_retry_seconds[0])
    service.process_pending(cid)
    assert down.seen[-1] == "sí\n¿hola?"


def test_giving_up_is_counted(
    config: ClientConfig, outage: tuple[Outage, Ports], clock: FakeClock
) -> None:
    down, ports = outage
    service = make_service(config, ports, clock)
    cid = service.start_conversation()
    down.failures = len(config.llm_retry_seconds) + 1
    service.receive(cid, "sí")
    gave_up_before = metrics.TURNS_HELD.labels("gave_up")._value.get()
    service.process_pending(cid)
    for delay in config.llm_retry_seconds:
        clock.advance(seconds=delay)
        service.process_pending(cid)
    assert service.repo.pending_inbound(cid) == []
    assert metrics.TURNS_HELD.labels("gave_up")._value.get() == gave_up_before + 1
    assert service.messages(cid)[-1].text == config.template("es", "technical_unavailable")
