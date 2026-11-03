from datetime import date
from uuid import UUID

import pytest

from app.adapters.clock.clocks import FakeClock
from app.adapters.llm.fake import ScriptedExtractor, TemplateResponder
from app.domain import engine
from app.domain.config import ClientConfig
from app.domain.models import (
    AnswerQuestion,
    AskField,
    Availability,
    ConversationState,
    ExtractionResult,
    FieldName,
    FieldStatus,
    InboundMessage,
    ReAsk,
    Reason,
    ReviewReason,
    Schedule,
    Stage,
    Status,
    TurnResult,
    Vehicle,
)
from app.domain.ports import Ports
from tests.conftest import ex, said


class Chat:
    """Drives the engine like the app would: start, then one turn per candidate message."""

    def __init__(
        self,
        state: ConversationState,
        config: ClientConfig,
        ports: Ports,
        extractor: ScriptedExtractor,
    ) -> None:
        self.config, self.ports, self.extractor = config, ports, extractor
        self.result = engine.start(state, config, ports)

    @property
    def state(self) -> ConversationState:
        return self.result.state

    def say(self, extraction: ExtractionResult, text: str | None = None) -> TurnResult:
        self.extractor.push(extraction)
        message = InboundMessage(
            conversation_id=UUID(int=1),
            text=said(extraction.fields, extraction.yes_no) if text is None else text,
            received_at=self.ports.clock.now(),
        )
        self.result = engine.turn(self.state, message, self.config, self.ports)
        return self.result

    def silence(self) -> TurnResult | None:
        result = engine.on_silence(self.state, self.config, self.ports)
        if result is not None:
            self.result = result
        return result


@pytest.fixture
def chat(
    state: ConversationState, config: ClientConfig, ports: Ports, extractor: ScriptedExtractor
) -> Chat:
    return Chat(state, config, ports, extractor)


def answer_everything(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(ex(full_name="Ana López"))
    chat.say(ex(has_license=True, vehicle=Vehicle.MOTO))
    chat.say(ex(city="Madrid", zone="Chamberí"))
    chat.say(ex(availability=[Availability.FULL_TIME]))
    chat.say(ex(schedule=[Schedule.EVENING]))
    chat.say(ex(experience_years=2, platforms=["Glovo"]))
    chat.say(ex(start_date=date(2026, 11, 9)))


def test_opening_discloses_assistant_and_asks_consent(chat: Chat) -> None:
    assert chat.state.stage == Stage.CONSENT
    assert "Lucía" in chat.result.replies[0]
    assert "asistente virtual" in chat.result.replies[0]
    assert chat.state.next_reminder_at is not None


def test_happy_path_ends_qualified(chat: Chat) -> None:
    answer_everything(chat)
    assert chat.state.stage == Stage.RECAP
    assert "Repasemos" in chat.result.replies[-1]
    assert "Chamberí, Madrid" in chat.result.replies[-1]

    chat.say(ex(yes_no=True))
    assert chat.state.status == Status.QUALIFIED
    assert chat.state.stage == Stage.CLOSED
    assert chat.state.next_reminder_at is None
    assert "Ana" in chat.result.replies[0]
    # No calendar wired in: a recruiter will set up the interview.
    assert chat.result.replies[-1] == chat.config.template("es", "qualified_no_slots")


def test_questions_follow_priority_order(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    asked = []
    for extraction in [
        ex(full_name="Ana López"),
        ex(has_license=True, vehicle=Vehicle.CAR),
        ex(city="Sevilla"),
    ]:
        chat.say(extraction)
        asked.append(next(a.field for a in chat.result.actions if isinstance(a, AskField)))
    assert asked == [FieldName.LICENSE, FieldName.LOCATION, FieldName.AVAILABILITY]


def test_several_answers_skip_questions(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(ex(full_name="Ana López", has_license=True, vehicle=Vehicle.MOTO, city="Valencia"))
    assert chat.state.awaiting == FieldName.AVAILABILITY


def test_no_license_is_disqualified_kindly(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(ex(full_name="Marta Ruiz"))
    chat.say(ex(has_license=False))
    assert chat.state.status == Status.DISQUALIFIED
    assert chat.state.disqualify_reason == "no_license"
    assert chat.state.stage == Stage.KEEP_ON_FILE
    assert chat.result.replies[-1].startswith("Gracias, Marta.")

    chat.say(ex(yes_no=True))
    assert chat.state.keep_on_file is True
    assert chat.state.stage == Stage.CLOSED
    assert chat.state.status == Status.DISQUALIFIED


def test_outside_area_is_disqualified(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(ex(full_name="Ana López", has_license=True, vehicle=Vehicle.MOTO))
    chat.say(ex(city="Bilbao"))
    assert chat.state.disqualify_reason == "outside_area"


def test_borderline_zone_continues_then_goes_to_review(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(ex(full_name="Ana López", has_license=True, vehicle=Vehicle.MOTO))
    chat.say(ex(city="Getafe"))
    assert chat.state.status == Status.IN_PROGRESS
    assert chat.state.awaiting == FieldName.AVAILABILITY
    chat.say(ex(availability=[Availability.WEEKENDS], schedule=[Schedule.MORNING]))
    chat.say(ex(experience_years=0, start_date=date(2026, 11, 3)))
    chat.say(ex(yes_no=True))
    assert chat.state.status == Status.NEEDS_REVIEW
    assert chat.state.review_reasons == [
        ReviewReason(reason=Reason.BORDERLINE_ZONE, field=FieldName.LOCATION)
    ]


def test_invalid_answer_is_asked_again_then_sent_to_review(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(ex(full_name="Ana López", has_license=True, vehicle=Vehicle.MOTO, city="Sevilla"))
    chat.say(ex(availability=[Availability.FULL_TIME], schedule=[Schedule.FLEXIBLE]))
    chat.say(ex(experience_years=1))
    chat.say(ex(start_date=date(2025, 1, 1)))  # in the past
    reask = chat.result.actions[0]
    assert isinstance(reask, ReAsk)
    assert (reask.field, reask.reason) == (FieldName.START_DATE, "in_past")

    chat.say(ex())  # still no usable answer: give up and move on
    meta = chat.state.candidate.meta[FieldName.START_DATE]
    assert meta.status == FieldStatus.NEEDS_REVIEW
    assert chat.state.stage == Stage.RECAP


def test_reply_follows_language_switch(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(ex(full_name="Ana López", language="en"))
    assert chat.state.language == "en"
    assert chat.result.replies[-1] == chat.config.template("en", "ask_license")


def test_human_request_hands_off(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(ex(intent="human_request"))
    assert chat.state.status == Status.NEEDS_REVIEW
    assert ReviewReason(reason=Reason.HUMAN_REQUESTED) in chat.state.review_reasons
    assert chat.result.replies[-1] == chat.config.template("es", "handoff_human")


def test_repeated_abuse_ends_politely(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(ex(intent="abuse"))
    assert chat.state.status == Status.IN_PROGRESS
    assert chat.result.replies[0] == chat.config.template("es", "abuse_warning")
    chat.say(ex(intent="abuse"))
    assert chat.state.status == Status.NEEDS_REVIEW
    assert ReviewReason(reason=Reason.FLAGGED_BEHAVIOUR) in chat.state.review_reasons


def test_sensitive_data_is_flagged_for_redaction(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    result = chat.say(ex(full_name="Ana López", sensitive_data=True), text="Ana López, DNI 1234")
    assert result.redact_inbound is True
    assert result.replies[0] == chat.config.template("es", "sensitive_data")


def test_declining_consent_ends_without_data(chat: Chat) -> None:
    chat.say(ex(yes_no=False))
    assert chat.state.status == Status.ABANDONED
    assert chat.state.candidate.full_name is None


def test_no_data_is_stored_before_consent(chat: Chat) -> None:
    chat.say(ex(full_name="Ana López"))
    assert chat.state.stage == Stage.CONSENT
    assert chat.state.candidate.full_name is None


def test_job_question_is_answered_then_flow_continues(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(ex(intent="question", question="¿Cuánto pagan?"))
    kinds = [type(a) for a in chat.result.actions]
    assert kinds[:2] == [AnswerQuestion, AskField]
    assert chat.state.candidate.meta[FieldName.FULL_NAME].attempts == 0


def test_frustration_offers_a_human_once(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(ex(full_name="Ana López", sentiment="frustrated"))
    offer = chat.config.template("es", "offer_human")
    assert offer in chat.result.replies
    chat.say(ex(has_license=True, vehicle=Vehicle.MOTO, sentiment="frustrated"))
    assert offer not in chat.result.replies


def test_changing_an_answer_at_recap_shows_recap_again(chat: Chat) -> None:
    answer_everything(chat)
    chat.say(ex(yes_no=False))
    assert chat.result.replies[-1] == chat.config.template("es", "ask_change")
    chat.say(ex(start_date=date(2026, 11, 16)))
    assert chat.state.stage == Stage.RECAP
    assert "16/11/2026" in chat.result.replies[-1]


def test_confirming_the_recap_qualifies_even_if_known_answers_are_repeated(chat: Chat) -> None:
    # "Sí, soy Ana López y todo está bien": the extractor sees the name again. Nothing changed,
    # so this is a confirmation, not a correction (found by tests/domain/test_properties.py).
    answer_everything(chat)
    chat.say(ex(yes_no=True, full_name="Ana López", city="Madrid", zone="Chamberí"))
    assert chat.state.status == Status.QUALIFIED


def test_closed_conversation_gets_closing_message(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(ex(intent="human_request"))
    chat.say(ex(full_name="Ana López"))
    assert chat.result.replies == [chat.config.template("es", "closed")]


def test_closed_after_a_knockout_does_not_promise_a_recruiter(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(ex(full_name="Ana López", has_license=False))
    chat.say(ex(yes_no=False))  # don't keep my details
    chat.say(ex(intent="other"), text="vale")
    assert chat.result.replies == [chat.config.template("es", "closed_disqualified")]


def test_a_corrected_location_after_a_knockout_is_checked_again(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(ex(full_name="Ana López", has_license=True, vehicle=Vehicle.MOTO))
    chat.say(ex(city="Bilbao"))
    assert chat.state.status == Status.DISQUALIFIED
    chat.say(ex(city="Madrid", zone="Chamberí"))
    assert chat.state.status == Status.IN_PROGRESS
    assert chat.state.disqualify_reason is None
    assert chat.state.candidate.city == "Madrid"
    assert chat.state.awaiting == FieldName.AVAILABILITY
    reopened = {
        "from": "disqualified",
        "to": "in_progress",
        "reason": "knockout_corrected:outside_area",
    }
    assert reopened in [e.data for e in chat.result.events]


def test_a_correction_that_is_still_outside_knocks_out_again(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(ex(full_name="Ana López", has_license=True, vehicle=Vehicle.MOTO))
    chat.say(ex(city="Bilbao"))
    chat.say(ex(city="Zaragoza"))
    assert chat.state.status == Status.DISQUALIFIED
    assert chat.state.stage == Stage.KEEP_ON_FILE
    assert chat.state.candidate.city == "Zaragoza"


def test_the_same_answer_after_a_knockout_is_not_a_correction(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(ex(full_name="Ana López", has_license=False))
    chat.say(ex(yes_no=True, has_license=False))  # "sí, guardadlos; no tengo carnet"
    assert chat.state.status == Status.DISQUALIFIED
    assert chat.state.keep_on_file is True


def test_reminders_then_abandoned(chat: Chat, clock: FakeClock) -> None:
    chat.say(ex(yes_no=True))
    chat.say(ex(full_name="Ana López"))
    assert chat.silence() is None  # too early

    clock.advance(hours=2)
    assert chat.silence() is not None
    assert "6 preguntas" in chat.result.replies[0]

    clock.advance(hours=22)
    assert chat.silence() is not None
    assert chat.state.reminders_sent == 2

    clock.advance(hours=24)
    result = chat.silence()
    assert result is not None
    assert chat.state.status == Status.ABANDONED
    abandoned = next(e for e in result.events if e.type == "abandoned")
    assert abandoned.data["drop_off"] == "license"


def _abandon(chat: Chat, clock: FakeClock) -> None:
    for hours in (2, 22, 24):
        clock.advance(hours=hours)
        chat.silence()
    assert chat.state.status == Status.ABANDONED


def test_reply_after_abandoned_resumes_at_the_missing_field(chat: Chat, clock: FakeClock) -> None:
    chat.say(ex(yes_no=True))
    chat.say(ex(full_name="Ana López"))
    _abandon(chat, clock)

    result = chat.say(ex(has_license=True, vehicle=Vehicle.MOTO))
    assert chat.state.status == Status.IN_PROGRESS
    assert chat.state.candidate.vehicle == Vehicle.MOTO
    assert chat.state.awaiting == FieldName.LOCATION
    assert chat.state.dropped_at is None
    assert chat.state.ended_at is None
    assert chat.state.next_reminder_at is not None
    reopened = next(e for e in result.events if e.type == "status_changed")
    assert reopened.data == {
        "from": "abandoned",
        "to": "in_progress",
        "reason": "resumed_after_silence:license",
    }


def test_reply_after_silence_at_consent_asks_for_consent_again(
    chat: Chat, clock: FakeClock
) -> None:
    _abandon(chat, clock)
    chat.say(ex(yes_no=True))
    assert chat.state.status == Status.IN_PROGRESS
    assert chat.state.stage == Stage.COLLECTING
    assert chat.state.awaiting == FieldName.FULL_NAME


def test_declined_consent_can_be_given_later(chat: Chat) -> None:
    # The decline message says "if you change your mind, write to us": writing back reopens
    # the consent question, and nothing is collected until they say yes.
    chat.say(ex(yes_no=False))
    chat.say(ex(intent="question", question="¿Qué hacéis con mis datos?"))
    assert chat.state.status == Status.IN_PROGRESS
    assert chat.state.stage == Stage.CONSENT
    assert chat.config.template("es", "consent_reask") in chat.result.replies
    chat.say(ex(yes_no=True))
    assert chat.state.stage == Stage.COLLECTING
    assert chat.state.awaiting == FieldName.FULL_NAME


def test_declining_twice_stays_declined(chat: Chat) -> None:
    chat.say(ex(yes_no=False))
    chat.say(ex(yes_no=False))
    assert chat.state.status == Status.ABANDONED
    assert chat.result.replies == [chat.config.template("es", "consent_declined")]


def test_reply_resets_reminders(chat: Chat, clock: FakeClock) -> None:
    chat.say(ex(yes_no=True))
    clock.advance(hours=2)
    chat.silence()
    chat.say(ex(full_name="Ana López"))
    assert chat.state.reminders_sent == 0
    assert chat.state.next_reminder_at is not None
    assert chat.state.next_reminder_at == clock.now().replace(hour=14)


class _BrokenResponder:
    def compose(self, *_: object) -> str:
        raise TimeoutError


class _ChattyResponder:
    def compose(self, *_: object) -> str:
        return "¿Cómo te llamas? ¿Y dónde vives? " * 10


@pytest.mark.parametrize(
    ("responder", "event"),
    [(_BrokenResponder(), "responder_failed"), (_ChattyResponder(), "reply_rejected")],
)
def test_bad_llm_reply_falls_back_to_template(
    state: ConversationState,
    config: ClientConfig,
    extractor: ScriptedExtractor,
    clock: FakeClock,
    responder: object,
    event: str,
) -> None:
    ports = Ports(extractor=extractor, responder=responder, clock=clock)  # type: ignore[arg-type]
    chat = Chat(state, config, ports, extractor)
    chat.say(ex(yes_no=True))
    assert chat.result.replies == [config.template("es", "ask_full_name")]
    assert any(e.type == event for e in chat.result.events)


class _BrokenExtractor:
    def extract(self, *_: object) -> None:
        raise TimeoutError


def test_extractor_failure_asks_again_without_penalty(
    state: ConversationState, config: ClientConfig, clock: FakeClock, extractor: ScriptedExtractor
) -> None:
    ports = Ports(extractor=_BrokenExtractor(), responder=TemplateResponder(), clock=clock)  # type: ignore[arg-type]
    started = engine.start(state, config, ports)
    message = InboundMessage(conversation_id=UUID(int=1), text="hola", received_at=clock.now())
    result = engine.turn(started.state, message, config, ports)
    assert result.replies == [config.template("es", "technical_retry")]
    assert any(e.type == "extractor_failed" for e in result.events)
    assert result.state.stage == Stage.CONSENT


def test_asking_for_a_person_after_a_knockout_keeps_the_knockout(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(ex(full_name="Ana López", has_license=False))
    chat.say(ex(intent="human_request"))
    assert chat.state.status == Status.DISQUALIFIED
    assert chat.state.stage == Stage.KEEP_ON_FILE  # the keep-on-file question is asked again
    assert chat.state.human_after_knockout is True
    assert chat.result.replies == [
        chat.config.template("es", "handoff_disqualified"),
        chat.config.template("es", "keep_on_file_reask"),
    ]
    assert "asked to talk to a person after the knockout" in (chat.state.summary or "")
    assert {"after_knockout": True} in [e.data for e in chat.result.events]


def test_a_new_answer_to_another_field_is_confirmed_first(chat: Chat) -> None:
    # "Mai" ("never" in Catalan) to the experience question, read as a name: not applied unasked.
    chat.say(ex(yes_no=True))
    chat.say(ex(full_name="Marta Puig", has_license=True, vehicle=Vehicle.MOTO))
    chat.say(ex(city="Barcelona", zone="Gràcia"))
    chat.say(ex(availability=[Availability.FULL_TIME], schedule=[Schedule.EVENING]))
    assert chat.state.awaiting == FieldName.EXPERIENCE
    chat.say(ex(full_name="Mai Mai"))
    assert chat.state.candidate.full_name == "Marta Puig"
    assert chat.state.confirming is not None
    assert chat.state.confirming.field == FieldName.FULL_NAME
    chat.say(ex(yes_no=False))
    assert chat.state.candidate.full_name == "Marta Puig"


def test_the_answer_to_the_question_asked_is_applied_at_once(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(ex(full_name="Ana"))  # no surname: the name question is asked again
    assert chat.state.awaiting == FieldName.FULL_NAME
    chat.say(ex(full_name="Ana García"))
    assert chat.state.candidate.full_name == "Ana García"
    assert chat.state.confirming is None
