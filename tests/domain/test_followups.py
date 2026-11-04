"""Findings from the hostile browser pass: requests after the outcome, the recap limit, stale review
flags, reminder wording and impossible experience."""

from datetime import date

import pytest

from app.adapters.llm.fake import ScriptedExtractor
from app.adapters.llm.fast_path import FastPathExtractor
from app.adapters.llm.keyword import KeywordExtractor
from app.domain.config import ClientConfig
from app.domain.models import (
    ConversationState,
    EventType,
    FieldName,
    FieldStatus,
    Reason,
    ReviewReason,
    Stage,
    Status,
)
from app.domain.policy import apply_extraction
from app.domain.ports import Ports
from tests.conftest import ex
from tests.domain.test_engine import Chat, answer_everything

TODAY = date(2026, 11, 2)
FAILED_DATE = ReviewReason(reason=Reason.FIELD_FAILED_TWICE, field=FieldName.START_DATE)


@pytest.fixture
def chat(
    state: ConversationState, config: ClientConfig, ports: Ports, extractor: ScriptedExtractor
) -> Chat:
    return Chat(state, config, ports, extractor)


def qualified(chat: Chat) -> Chat:
    answer_everything(chat)
    chat.say(ex(yes_no=True))
    assert chat.state.status == Status.QUALIFIED
    assert chat.state.stage == Stage.CLOSED  # no calendar here, so no slots to pick
    return chat


def event_types(chat: Chat) -> list[EventType]:
    return [e.type for e in chat.result.events]


def test_asking_to_change_the_interview_reaches_a_recruiter(chat: Chat) -> None:
    qualified(chat)
    ended_at = chat.state.ended_at
    chat.say(ex(intent="interview_change"), text="quiero cambiar la entrevista")
    assert chat.state.status == Status.NEEDS_REVIEW
    assert ReviewReason(reason=Reason.INTERVIEW_CHANGE) in chat.state.review_reasons
    assert EventType.INTERVIEW_CHANGE_REQUESTED in event_types(chat)
    assert chat.result.replies == [chat.config.template("es", "interview_change_noted")]
    assert chat.state.ended_at == ended_at  # analytics keep the time of the original outcome


def test_asking_to_change_an_interview_after_a_knockout_changes_nothing(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(ex(full_name="Ana López", has_license=False))
    chat.say(ex(yes_no=False))
    chat.say(ex(intent="interview_change"), text="quiero cambiar la entrevista")
    assert chat.state.status == Status.DISQUALIFIED
    assert chat.result.replies == [chat.config.template("es", "closed_disqualified")]


def test_asking_for_a_person_after_qualifying_reaches_a_recruiter(chat: Chat) -> None:
    qualified(chat)
    chat.say(ex(intent="human_request"), text="quiero hablar con una persona")
    assert chat.state.status == Status.NEEDS_REVIEW
    assert ReviewReason(reason=Reason.HUMAN_REQUESTED) in chat.state.review_reasons
    assert chat.result.replies == [chat.config.template("es", "handoff_human")]


def test_asking_for_a_person_after_a_closed_knockout_keeps_the_knockout(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(ex(full_name="Ana López", has_license=False))
    chat.say(ex(yes_no=False))
    chat.say(ex(intent="human_request"), text="quiero hablar con una persona")
    assert chat.state.status == Status.DISQUALIFIED
    assert chat.state.human_after_knockout is True
    assert chat.result.replies == [chat.config.template("es", "handoff_disqualified")]


def test_a_thank_you_after_qualifying_changes_nothing(chat: Chat) -> None:
    qualified(chat)
    chat.say(ex(intent="other"), text="gracias")
    assert chat.state.status == Status.QUALIFIED
    assert chat.result.replies == [chat.config.template("es", "closed")]


def test_a_second_request_in_review_stays_in_review_once(chat: Chat) -> None:
    qualified(chat)
    chat.say(ex(intent="interview_change"), text="quiero cambiar la entrevista")
    chat.say(ex(intent="interview_change"), text="quiero cambiar la entrevista")
    assert chat.state.review_reasons.count(ReviewReason(reason=Reason.INTERVIEW_CHANGE)) == 1
    assert EventType.STATUS_CHANGED not in event_types(chat)


def test_refusing_the_recap_repeatedly_goes_to_a_recruiter(chat: Chat) -> None:
    answer_everything(chat)
    chat.say(ex(yes_no=False))
    assert chat.result.replies == [chat.config.template("es", "ask_change")]
    chat.say(ex(intent="other"), text="todo mal")
    assert chat.state.status == Status.IN_PROGRESS
    chat.say(ex(yes_no=False))
    assert chat.state.status == Status.NEEDS_REVIEW
    assert ReviewReason(reason=Reason.RECAP_UNCONFIRMED) in chat.state.review_reasons


def test_a_correction_restarts_the_recap_count(chat: Chat) -> None:
    answer_everything(chat)
    chat.say(ex(yes_no=False))
    chat.say(ex(intent="other"), text="todo mal")
    chat.say(ex(start_date=date(2026, 11, 10)))
    assert chat.state.recap_rounds == 0
    chat.say(ex(yes_no=False))
    assert chat.state.status == Status.IN_PROGRESS


def test_questions_at_the_recap_do_not_count_as_refusals(chat: Chat) -> None:
    answer_everything(chat)
    for _ in range(chat.config.max_recap_rounds + 1):
        chat.say(ex(intent="question", question="¿cuánto pagan?"), text="¿cuánto pagan?")
    assert chat.state.status == Status.IN_PROGRESS
    assert chat.state.recap_rounds == 0


def test_a_valid_answer_clears_the_flag_on_its_own_field_and_is_logged(
    state: ConversationState, config: ClientConfig
) -> None:
    other = ReviewReason(reason=Reason.BORDERLINE_ZONE, field=FieldName.LOCATION)
    state.review_reasons = [FAILED_DATE, other]
    state.candidate.meta[FieldName.START_DATE].status = FieldStatus.NEEDS_REVIEW
    result = apply_extraction(
        state,
        ex(start_date=date(2026, 11, 3)).fields,
        config,
        TODAY,
        message="2026-11-03",
    )
    assert state.review_reasons == [other]
    assert state.candidate.meta[FieldName.START_DATE].status == FieldStatus.VALID
    cleared = [e for e in result.events if e.type == EventType.REVIEW_REASON_CLEARED]
    assert [e.data for e in cleared] == [{"field": "start_date", "reason": "field_failed_twice"}]


def test_an_answer_not_in_their_words_keeps_the_flag(
    state: ConversationState, config: ClientConfig
) -> None:
    state.review_reasons = [FAILED_DATE]
    apply_extraction(
        state, ex(start_date=date(2026, 11, 3)).fields, config, TODAY, message="no sé, ya veremos"
    )
    assert FAILED_DATE in state.review_reasons


def test_a_corrected_date_after_two_failures_still_qualifies(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(ex(full_name="Ana López"))
    chat.say(ex(has_license=True, vehicle="moto"))
    chat.say(ex(city="Madrid", zone="Chamberí"))
    chat.say(ex(availability=["full_time"]))
    chat.say(ex(schedule=["evening"]))
    chat.say(ex(experience_years=0))
    chat.say(ex(start_date=date(2026, 11, 1)))  # in the past
    chat.say(ex(start_date=date(2026, 11, 1)))  # gives up, on to the recap
    assert FAILED_DATE in chat.state.review_reasons
    chat.say(ex(start_date=date(2026, 11, 3)))
    chat.say(ex(yes_no=True))
    assert chat.state.status == Status.QUALIFIED


def test_reminder_at_the_recap_asks_for_the_confirmation(chat: Chat, clock) -> None:
    answer_everything(chat)
    clock.advance(hours=3)
    assert chat.silence() is not None
    assert chat.result.replies == [chat.config.template("es", "reminder_1_recap")]


@pytest.mark.parametrize("language", ["es", "en"])
def test_reminder_with_one_question_left_is_singular(chat: Chat, clock, language: str) -> None:
    answer_everything(chat)
    chat.state.language = language
    chat.state.stage = Stage.COLLECTING
    meta = chat.state.candidate.meta[FieldName.START_DATE]
    meta.status = FieldStatus.MISSING
    clock.advance(hours=3)
    assert chat.silence() is not None
    assert chat.result.replies == [chat.config.template(language, "reminder_1_one")]
    assert "1" in chat.result.replies[0]


def test_reminder_with_several_questions_left_gives_the_number(chat: Chat, clock) -> None:
    chat.say(ex(yes_no=True))
    clock.advance(hours=3)
    assert chat.silence() is not None
    assert "7 preguntas" in chat.result.replies[0]


@pytest.mark.parametrize("message", ["-3 años en Glovo", "- 3 años", "(-3) años"])
def test_negative_experience_is_rejected_even_if_the_sign_is_lost(
    state: ConversationState, config: ClientConfig, message: str
) -> None:
    fields = ex(experience_years=3).fields.model_copy(deep=True)
    assert fields.experience_years is not None
    fields.experience_years.quote = message
    result = apply_extraction(state, fields, config, TODAY, message=message)
    assert result.rejected[FieldName.EXPERIENCE] == "out_of_range"
    assert state.candidate.experience_years is None


def test_a_range_of_years_is_not_negative(state: ConversationState, config: ClientConfig) -> None:
    fields = ex(experience_years=3).fields.model_copy(deep=True)
    assert fields.experience_years is not None
    fields.experience_years.quote = "2-3 años"
    result = apply_extraction(state, fields, config, TODAY, message="2-3 años")
    assert FieldName.EXPERIENCE not in result.rejected
    assert state.candidate.experience_years == 3


@pytest.mark.parametrize(
    "message",
    ["quiero cambiar la entrevista al viernes", "can I move my interview?", "cancelar la cita"],
)
def test_keyword_reader_sees_an_interview_change(
    state: ConversationState, config: ClientConfig, message: str
) -> None:
    assert KeywordExtractor().extract(state, message, config, TODAY).intent == "interview_change"


def test_a_plain_yes_after_closing_needs_no_llm(
    state: ConversationState, config: ClientConfig, extractor: ScriptedExtractor
) -> None:
    state.stage = Stage.CLOSED
    assert FastPathExtractor(extractor).extract(state, "ok", config, TODAY).yes_no is True
    assert extractor.seen == []


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("¿necesito carnet si voy en bici?", "license_needed"),
        ("do you speak English?", "languages"),
    ],
)
def test_keyword_reader_picks_the_new_faq_entries(
    state: ConversationState, config: ClientConfig, question: str, expected: str
) -> None:
    assert KeywordExtractor().extract(state, question, config, TODAY).faq_id == expected
