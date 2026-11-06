"""What the LLM may not say, license read-backs, deferrals, sensitive data, age, turn limits, night
reminders, failed deliveries and the backup model (docs/failure-modes.md, gaps 6-13 and 18)."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.adapters.clock.clocks import FakeClock
from app.adapters.llm.fake import ScriptedExtractor, TemplateResponder
from app.domain import engine
from app.domain.config import ClientConfig, QuietHours
from app.domain.models import (
    AskField,
    ConversationState,
    EventType,
    ExtractionResult,
    FieldName,
    FieldStatus,
    InboundMessage,
    Reason,
    ReviewReason,
    Stage,
    Status,
    Vehicle,
)
from app.domain.ports import Ports
from app.domain.render import check_reply, render, says_what_only_code_may
from app.domain.summary import build_summary
from app.domain.validators import has_sensitive_pattern
from tests.conftest import START, ex, said


class Chat:
    def __init__(self, config: ClientConfig, start: datetime = START, channel: str = "chat"):
        self.config = config
        self.clock = FakeClock(start)
        self.extractor = ScriptedExtractor()
        self.ports = Ports(self.extractor, TemplateResponder(), self.clock)
        state = ConversationState(client_id=config.client_id, channel=channel)
        self.result = engine.start(state, config, self.ports)

    @property
    def state(self) -> ConversationState:
        return self.result.state

    def say(self, extraction: ExtractionResult, text: str | None = None):
        self.extractor.push(extraction)
        message = InboundMessage(
            conversation_id=UUID(int=1),
            text=said(extraction.fields, extraction.yes_no) if text is None else text,
            received_at=self.clock.now(),
        )
        self.result = engine.turn(self.state, message, self.config, self.ports)
        return self.result

    def event_types(self) -> list[EventType]:
        return [event.type for event in self.result.events]

    def to_license(self) -> None:
        self.say(ex(yes_no=True))
        self.say(ex(full_name="Ana López"))
        if self.state.confirming is not None:
            self.say(ex(yes_no=True), text="sí")
        assert self.state.awaiting == FieldName.LICENSE


def no_license(quote: str) -> ExtractionResult:
    extraction = ex(has_license=False)
    extraction.fields.has_license.quote = quote  # type: ignore[union-attr]
    return extraction


@pytest.fixture
def chat(config: ClientConfig) -> Chat:
    return Chat(config)


# --- Gap 6: what an LLM-written reply may not say ------------------------------------------------


class TestForbiddenReplies:
    @pytest.mark.parametrize(
        "text",
        [
            "¡Estás contratada! ¿Cuándo empiezas?",
            "Te pagamos 2.000 € al mes. ¿Te va bien?",
            "Son 1500 pesos por semana.",
            "We pay $15 an hour.",
            "You're hired! When can you start?",
            "You qualify for the job.",
            "Soy una persona real. ¿Tienes carnet?",
            "No soy un bot. ¿Tienes carnet?",
            "I'm a real person, don't worry.",
            "Sadly you were rejected.",
            "Lo siento, has sido descartada.",
            "¿Cuál es tu nacionalidad?",
            "¿Tienes papeles?",
            "Are you an immigrant?",
        ],
    )
    def test_these_are_caught(self, config: ClientConfig, text: str) -> None:
        assert says_what_only_code_may(text, config)

    @pytest.mark.parametrize(
        "text",
        [
            "¿Tienes carnet? Moto · Coche · Ambos · No tengo",
            "¿En qué ciudad y barrio vives?",
            "¿Has repartido antes?",
            "¿Qué día podrías empezar? Por ejemplo, el 1 de marzo.",
            "Which area do you live in?",
            "How many years have you delivered for?",
            "Do you prefer mornings or evenings?",
        ],
    )
    def test_ordinary_questions_pass(self, config: ClientConfig, text: str) -> None:
        assert not says_what_only_code_may(text, config)

    def test_every_template_may_say_them_anyway(self, config: ClientConfig) -> None:
        """Approved text can say "cumples los requisitos": only the check on LLM text is strict."""
        assert check_reply(config.template("es", "qualified").format(name="Ana"), config)

    def test_a_forbidden_reply_gets_the_template_and_is_logged(self, config: ClientConfig) -> None:
        class Liar(TemplateResponder):
            def compose(self, action, state, config) -> str:
                return "¡Estás contratada! ¿Empezamos?"

        clock = FakeClock(START)
        ports = Ports(ScriptedExtractor(), Liar(), clock)
        state = ConversationState(client_id=config.client_id)
        replies, events = render([AskField(field=FieldName.LICENSE)], state, config, ports)
        assert replies == [config.template("es", "ask_license")]
        assert [e.type for e in events] == [EventType.REPLY_REJECTED]


# --- Gap 7: a license "no" is read back --------------------------------------------------------


class TestLicenseReadBack:
    @pytest.mark.parametrize("words", ["no tengo carnet", "me lo quitaron", "no"])
    def test_free_text_is_read_back_before_anyone_is_rejected(self, chat: Chat, words: str) -> None:
        chat.to_license()
        result = chat.say(no_license(words), text=words)
        assert chat.state.status == Status.IN_PROGRESS
        assert chat.state.confirming.field == FieldName.LICENSE
        assert chat.state.disqualify_reason is None
        assert result.replies == ["Solo para confirmar: sin carnet, ¿correcto?"]

    def test_yes_to_it_rejects(self, chat: Chat) -> None:
        chat.to_license()
        chat.say(no_license("no tengo carnet"), text="no tengo carnet")
        chat.say(ex(yes_no=True), text="sí")
        assert chat.state.status == Status.DISQUALIFIED
        assert chat.state.disqualify_reason == Reason.NO_LICENSE

    def test_no_to_it_asks_again(self, chat: Chat) -> None:
        chat.to_license()
        chat.say(no_license("no tengo carnet"), text="no tengo carnet")
        chat.say(ex(yes_no=False), text="no")
        assert chat.state.status == Status.IN_PROGRESS
        assert chat.state.awaiting == FieldName.LICENSE
        assert chat.state.candidate.has_license is None

    def test_correcting_it_applies_the_correction(self, chat: Chat) -> None:
        chat.to_license()
        chat.say(no_license("no tengo carnet"), text="no tengo carnet")
        chat.say(ex(has_license=True, vehicle=Vehicle.MOTO))
        assert chat.state.status == Status.IN_PROGRESS
        assert chat.state.candidate.vehicle == Vehicle.MOTO

    @pytest.mark.parametrize("option", ["No tengo", "no tengo.", "NO TENGO", " None "])
    def test_tapping_the_option_needs_no_read_back(self, chat: Chat, option: str) -> None:
        chat.to_license()
        chat.say(no_license(option.strip(" .")), text=option)
        assert chat.state.status == Status.DISQUALIFIED

    def test_on_a_call_even_the_option_is_read_back(self, config: ClientConfig) -> None:
        chat = Chat(config, channel="voice")
        chat.to_license()
        chat.say(no_license("No tengo"), text="No tengo")
        assert chat.state.status == Status.IN_PROGRESS
        assert chat.state.confirming.field == FieldName.LICENSE
        chat.say(ex(yes_no=True), text="sí")
        assert chat.state.status == Status.DISQUALIFIED

    def test_the_read_back_records_why(self, chat: Chat) -> None:
        chat.to_license()
        chat.say(no_license("no tengo carnet"), text="no tengo carnet")
        asked = next(e for e in chat.result.events if e.type == EventType.CONFIRMATION_ASKED)
        assert asked.data == {"field": "license", "why": "before_rejecting"}

    def test_only_punctuation_is_ignored_around_the_option(self, chat: Chat) -> None:
        """A typo or a stray letter isn't exactly the option: it's read back like anything else."""
        chat.to_license()
        chat.say(no_license("XNo tengoX"), text="XNo tengoX")
        assert chat.state.status == Status.IN_PROGRESS
        assert chat.state.confirming is not None

    def test_a_config_decides_what_the_option_is(self, config: ClientConfig) -> None:
        chat = Chat(config.model_copy(update={"license_no_options": ["Sin carnet"]}))
        chat.to_license()
        chat.say(no_license("No tengo"), text="No tengo")
        assert chat.state.status == Status.IN_PROGRESS


# --- Gap 8: "I can't talk now" -----------------------------------------------------------------


class TestDeferral:
    def test_it_costs_no_attempt_and_says_so(self, chat: Chat) -> None:
        chat.to_license()
        for _ in range(5):
            result = chat.say(ExtractionResult(intent="deferral"), text="estoy conduciendo")
            assert result.replies == ["Sin problema, aquí sigo. Escríbeme cuando puedas."]
        meta = chat.state.candidate.meta[FieldName.LICENSE]
        assert (meta.attempts, meta.status) == (0, FieldStatus.MISSING)
        assert chat.state.review_reasons == []
        assert chat.state.status == Status.IN_PROGRESS
        assert chat.state.awaiting == FieldName.LICENSE
        assert EventType.DEFERRED in chat.event_types()

    def test_reminders_count_from_it(self, chat: Chat) -> None:
        chat.to_license()
        chat.clock.advance(minutes=30)
        chat.say(ExtractionResult(intent="deferral"), text="luego te digo")
        assert chat.state.next_reminder_at == chat.clock.now() + timedelta(hours=2)

    def test_an_answer_in_the_same_message_still_counts(self, chat: Chat) -> None:
        chat.to_license()
        deferring = ex(has_license=True, vehicle=Vehicle.MOTO, intent="deferral")
        chat.say(deferring)
        assert chat.state.candidate.vehicle == Vehicle.MOTO
        assert EventType.DEFERRED not in chat.event_types()


# --- Gap 9: sensitive data the model doesn't flag -----------------------------------------------


class TestSensitivePatterns:
    @pytest.mark.parametrize(
        "text",
        [
            "mi DNI es 12345678Z",
            "12345678-z",
            "NIE X1234567L",
            "x-1234567-l",
            "CURP GOMC850101HDFRRR09",
            "ES91 2100 0418 4502 0005 1332",
            "es9121000418450200051332",
        ],
    )
    def test_id_like_numbers_match(self, config: ClientConfig, text: str) -> None:
        assert has_sensitive_pattern(text, config)

    @pytest.mark.parametrize(
        "text", ["28010", "Madrid 28010", "3 años", "el 1 de marzo", "Ana López", "1234567", "B"]
    )
    def test_ordinary_answers_do_not(self, config: ClientConfig, text: str) -> None:
        assert not has_sensitive_pattern(text, config)

    def test_the_message_is_redacted_though_the_model_missed_it(self, chat: Chat) -> None:
        chat.say(ex(yes_no=True))
        words = "Ana López, mi DNI es 12345678Z"
        extraction = ex(full_name="Ana López")
        result = chat.say(extraction, text=words)
        assert result.redact_inbound
        assert EventType.SENSITIVE_DATA_REDACTED in chat.event_types()
        assert chat.state.candidate.full_name == "Ana López"
        assert chat.state.candidate.meta[FieldName.FULL_NAME].quote is None

    def test_also_after_the_chat_has_closed(self, chat: Chat) -> None:
        chat.say(ex(yes_no=False), text="no")
        chat.state.stage = Stage.CLOSED
        assert chat.say(ExtractionResult(), text="12345678Z").redact_inbound

    def test_a_bad_pattern_is_refused_at_startup(self, config: ClientConfig) -> None:
        data = config.model_dump()
        data["sensitive_patterns"] = ["("]
        with pytest.raises(Exception, match=r"unterminated|missing"):
            ClientConfig.model_validate(data)


# --- Gap 10: minimum age ------------------------------------------------------------------------


class TestUnderage:
    def test_the_greeting_says_it(self, chat: Chat) -> None:
        assert "18 años o más" in chat.result.replies[0]

    def test_saying_so_goes_to_a_person_not_a_knockout(self, chat: Chat) -> None:
        chat.say(ex(yes_no=True))
        result = chat.say(ExtractionResult(says_underage=True), text="tengo 16 años")
        assert chat.state.status == Status.NEEDS_REVIEW
        assert chat.state.disqualify_reason is None
        assert ReviewReason(reason=Reason.UNDERAGE) in chat.state.review_reasons
        assert result.replies == [
            chat.config.template("es", "needs_review").replace(", {name}", "")
        ]
        assert "minimum age" in chat.state.summary

    def test_after_a_knockout_nothing_changes(self, chat: Chat) -> None:
        chat.to_license()
        chat.say(no_license("No tengo"), text="No tengo")
        chat.say(ExtractionResult(says_underage=True), text="tengo 16 años")
        assert chat.state.status == Status.DISQUALIFIED
        assert chat.state.review_reasons == []


# --- Gap 11: turn limit -------------------------------------------------------------------------


class TestTurnLimit:
    def over_limit(self, chat: Chat) -> None:
        chat.result.state.turns = chat.config.max_turns

    def test_below_it_nothing_changes(self, chat: Chat) -> None:
        chat.say(ex(yes_no=True))
        chat.result.state.turns = chat.config.max_turns - 1
        chat.say(ExtractionResult(intent="off_topic"), text="un chiste")
        assert chat.state.status == Status.IN_PROGRESS
        assert chat.extractor.seen[-1] == "un chiste"

    def test_a_person_takes_over_without_another_llm_call(self, chat: Chat) -> None:
        chat.say(ex(yes_no=True))
        self.over_limit(chat)
        seen = len(chat.extractor.seen)
        chat.extractor.push(ExtractionResult(intent="off_topic"))
        message = InboundMessage(
            conversation_id=UUID(int=1), text="otro chiste", received_at=chat.clock.now()
        )
        result = engine.turn(chat.state, message, chat.config, chat.ports)
        assert len(chat.extractor.seen) == seen
        assert result.state.status == Status.NEEDS_REVIEW
        assert result.state.stage == Stage.CLOSED
        assert ReviewReason(reason=Reason.TURN_LIMIT) in result.state.review_reasons
        assert result.replies == [chat.config.template("es", "turn_limit_reached")]
        assert EventType.TURN_LIMIT_REACHED in [e.type for e in result.events]
        assert not result.redact_inbound

    def test_once_closed_it_stays_quiet(self, chat: Chat) -> None:
        chat.say(ex(yes_no=False), text="no")
        chat.result.state.stage = Stage.CLOSED
        self.over_limit(chat)
        result = chat.say(ExtractionResult(), text="hola?")
        assert result.replies == []
        assert result.state.next_reminder_at is None

    def test_a_message_before_consent_stays_redacted(self, chat: Chat) -> None:
        self.over_limit(chat)
        assert chat.say(ExtractionResult(), text="mi nombre es Ana").redact_inbound

    def test_a_rejected_candidate_keeps_the_rejection(self, chat: Chat) -> None:
        chat.to_license()
        chat.say(no_license("No tengo"), text="No tengo")
        self.over_limit(chat)
        result = chat.say(ExtractionResult(), text="???")
        assert result.state.status == Status.DISQUALIFIED
        assert result.state.stage == Stage.CLOSED
        assert result.replies == [chat.config.template("es", "closed_disqualified")]


# --- Gap 12: a message that never arrived ------------------------------------------------------


class TestDeliveryFailed:
    def test_an_open_screening_reaches_a_person_and_isnt_a_drop_off(self, chat: Chat) -> None:
        chat.say(ex(yes_no=True))
        result = engine.on_delivery_failed(chat.state, chat.config, chat.ports, 131026)
        assert result.state.status == Status.NEEDS_REVIEW
        assert ReviewReason(reason=Reason.UNREACHABLE) in result.state.review_reasons
        assert not result.state.went_silent
        assert result.state.dropped_at is None
        assert result.state.next_reminder_at is None
        assert result.replies == []
        assert result.events[0].data == {"code": 131026}

    def test_a_finished_one_only_records_it(self, chat: Chat) -> None:
        chat.say(ex(yes_no=False), text="no")
        chat.result.state.stage = Stage.CLOSED
        status = chat.state.status
        result = engine.on_delivery_failed(chat.state, chat.config, chat.ports, None)
        assert result.state.status == status
        assert result.state.review_reasons == []
        assert [e.type for e in result.events] == [EventType.DELIVERY_FAILED]


# --- Gap 13: reminders at night ----------------------------------------------------------------


class TestQuietHours:
    @pytest.mark.parametrize(
        ("last_message", "first_reminder"),
        [
            # 11:00 Madrid + 2 h: daytime, unchanged.
            (datetime(2026, 11, 2, 10, 0, tzinfo=UTC), datetime(2026, 11, 2, 12, 0, tzinfo=UTC)),
            # 23:30 Madrid + 2 h is 01:30: waits for 09:00.
            (datetime(2026, 11, 2, 22, 30, tzinfo=UTC), datetime(2026, 11, 3, 8, 0, tzinfo=UTC)),
            # 20:00 Madrid + 2 h is exactly 22:00: quiet, so 09:00 next day.
            (datetime(2026, 11, 2, 19, 0, tzinfo=UTC), datetime(2026, 11, 3, 8, 0, tzinfo=UTC)),
            # 07:00 Madrid + 2 h is exactly 09:00: allowed.
            (datetime(2026, 11, 2, 6, 0, tzinfo=UTC), datetime(2026, 11, 2, 8, 0, tzinfo=UTC)),
            # 05:00 Madrid + 2 h is 07:00: the same morning at 09:00.
            (datetime(2026, 11, 2, 4, 0, tzinfo=UTC), datetime(2026, 11, 2, 8, 0, tzinfo=UTC)),
        ],
    )
    def test_the_reminder_waits_for_the_morning(
        self, config: ClientConfig, last_message: datetime, first_reminder: datetime
    ) -> None:
        chat = Chat(config, last_message)
        chat.say(ex(yes_no=True))
        assert chat.state.next_reminder_at == first_reminder

    def test_it_uses_the_candidates_timezone(self, config: ClientConfig) -> None:
        # 18:00 UTC is 12:00 in Monterrey: daytime there, though 19:00 in Madrid is close to night.
        chat = Chat(config, datetime(2026, 11, 2, 18, 0, tzinfo=UTC))
        chat.say(ex(yes_no=True))
        chat.say(ex(full_name="Ana López", has_license=True, vehicle=Vehicle.MOTO))
        chat.say(ex(city="Monterrey"))
        assert chat.state.next_reminder_at == datetime(2026, 11, 2, 20, 0, tzinfo=UTC)

    def test_never_past_whatsapps_window(self, config: ClientConfig) -> None:
        """The second reminder, 23 h after a 23:30 message, is 22:30: the morning would miss the
        window, so it goes just before the quiet hours start."""
        chat = Chat(config, datetime(2026, 11, 2, 22, 30, tzinfo=UTC), channel="whatsapp")
        chat.say(ex(yes_no=True))
        chat.clock._now = chat.state.next_reminder_at
        result = engine.on_silence(chat.state, config, chat.ports)
        assert result is not None
        assert result.replies
        assert result.state.next_reminder_at == datetime(2026, 11, 3, 20, 59, tzinfo=UTC)

    def test_if_that_evening_has_gone_it_keeps_the_time(self, config: ClientConfig) -> None:
        narrow = config.model_copy(update={"reply_window_hours": {"chat": 3.0}})
        chat = Chat(narrow, datetime(2026, 11, 2, 22, 30, tzinfo=UTC))
        chat.clock.advance(hours=1)  # 00:30: the evening before has passed
        chat.state.last_inbound_at = chat.clock.now() - timedelta(hours=1)
        chat.say(ex(yes_no=True))
        assert chat.state.next_reminder_at == chat.clock.now() + timedelta(hours=2)

    def test_the_early_hours_look_back_to_last_evening(self, config: ClientConfig) -> None:
        """01:30 with a window the morning would miss: 21:59 that evening has passed unless the
        conversation started before it, as here."""
        narrow = config.model_copy(update={"reply_window_hours": {"chat": 3.0}})
        chat = Chat(narrow, datetime(2026, 11, 2, 20, 30, tzinfo=UTC))  # 21:30 Madrid
        chat.say(ex(yes_no=True))
        # 2 h later is 23:30: the morning misses the 3 h window, and 21:59 is still ahead.
        assert chat.state.next_reminder_at == datetime(2026, 11, 2, 20, 59, tzinfo=UTC)

    def test_none_sends_at_any_hour(self, config: ClientConfig) -> None:
        chat = Chat(
            config.model_copy(update={"quiet_hours": None}),
            datetime(2026, 11, 2, 22, 30, tzinfo=UTC),
        )
        chat.say(ex(yes_no=True))
        assert chat.state.next_reminder_at == datetime(2026, 11, 3, 0, 30, tzinfo=UTC)

    def test_they_must_run_overnight(self) -> None:
        with pytest.raises(ValidationError, match="overnight"):
            QuietHours.model_validate({"start": "09:00", "end": "22:00"})


# --- Gaps 17 and 18: overrides and the backup model --------------------------------------------


class TestRecruiterSide:
    def test_an_override_waits_for_someone_to_tell_the_candidate(self, chat: Chat) -> None:
        state, _ = engine.override_status(
            chat.state, Status.QUALIFIED, "by phone", "rec", chat.config, chat.ports
        )
        assert state.tell_candidate
        told, event = engine.candidate_told(state, "rec")
        assert not told.tell_candidate
        assert state.tell_candidate  # the original is untouched
        assert (event.type, event.data) == (EventType.CANDIDATE_TOLD, {"actor": "rec"})

    def test_a_knockout_on_the_backup_model_is_marked(self, chat: Chat) -> None:
        chat.to_license()
        reading = no_license("No tengo").model_copy(update={"on_fallback": True})
        chat.say(reading, text="No tengo")
        assert chat.state.knockout_on_fallback
        assert EventType.EXTRACTED_ON_FALLBACK in chat.event_types()
        assert "backup model" in build_summary(chat.state, chat.config)

    def test_one_on_the_main_model_is_not(self, chat: Chat) -> None:
        chat.to_license()
        chat.say(no_license("No tengo"), text="No tengo")
        assert not chat.state.knockout_on_fallback
        assert EventType.EXTRACTED_ON_FALLBACK not in chat.event_types()

    def test_the_flag_is_not_in_the_models_schema(self) -> None:
        assert "on_fallback" not in ExtractionResult.model_json_schema()["properties"]
