"""A model that isn't sure can't change what a candidate already answered.

From a real voice call: "touch no todo es correcto" at the recap came back as five invented values,
all at confidence 0.2. One waited for a yes, and the rest overwrote good answers. The experience
went from 6 years to 0.
"""

from datetime import date
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.adapters.clock.clocks import FakeClock
from app.adapters.config.yaml_loader import load_client_config
from app.adapters.llm.fake import ScriptedExtractor, TemplateResponder
from app.domain.config import ClientConfig
from app.domain.models import (
    Availability,
    ConversationState,
    EventType,
    ExtractedFields,
    ExtractionResult,
    FieldName,
    Schedule,
    Stage,
    Status,
    Vehicle,
)
from app.domain.policy import apply_extraction
from app.domain.ports import Ports
from tests.conftest import ex
from tests.domain.test_engine import Chat, answer_everything
from tests.domain.test_properties import START, fields

GARBLED = "touch no todo es correcto"
UNSURE = 0.2


def unsure(yes_no: bool | None = None, **values: Any) -> ExtractionResult:
    extraction = ex(yes_no=yes_no, **values)
    for name in values:
        extracted = getattr(extraction.fields, name)
        extracted.quote, extracted.confidence = "no todo es correcto", UNSURE
    return extraction


def invented(yes_no: bool | None = False) -> ExtractionResult:
    return unsure(
        yes_no=yes_no,
        has_license=False,
        availability=[Availability.PART_TIME],
        schedule=[Schedule.MORNING],
        experience_years=0,
        start_date=date(2026, 11, 3),
    )


@pytest.fixture
def chat(
    state: ConversationState, config: ClientConfig, ports: Ports, extractor: ScriptedExtractor
) -> Chat:
    return Chat(state, config, ports, extractor)


def answers(chat: Chat) -> dict[str, Any]:
    return chat.state.candidate.model_dump(exclude={"meta"})


def at_recap(chat: Chat) -> dict[str, Any]:
    answer_everything(chat)
    assert chat.state.stage == Stage.RECAP
    return answers(chat)


@pytest.mark.parametrize("yes_no", [False, None])
def test_a_garbled_no_at_the_recap_changes_nothing(chat: Chat, yes_no: bool | None) -> None:
    before = at_recap(chat)
    chat.say(invented(yes_no), text=GARBLED)
    assert answers(chat) == before
    assert chat.state.status == Status.IN_PROGRESS
    assert chat.state.confirming is None  # no "sin carnet, ¿correcto?" out of nowhere
    assert chat.result.replies == [chat.config.template("es", "ask_change")]


def test_a_sure_correction_at_the_recap_still_applies(chat: Chat) -> None:
    at_recap(chat)
    chat.say(ex(experience_years=6, platforms=["Glovo"]))
    assert chat.state.candidate.experience_years == 6
    assert chat.state.stage == Stage.RECAP


def test_a_second_unsure_value_waits_instead_of_being_saved(chat: Chat) -> None:
    """One value can wait for a yes at a time. The others mustn't skip that check."""
    chat.say(ex(yes_no=True))
    chat.say(ex(full_name="Ana López"))
    chat.say(ex(has_license=True, vehicle=Vehicle.MOTO))
    chat.say(ex(city="Madrid", zone="Chamberí"))
    chat.say(ex(availability=[Availability.FULL_TIME]))
    chat.say(unsure(has_license=False, availability=[Availability.PART_TIME]), text=GARBLED)
    assert chat.state.confirming is not None
    assert chat.state.candidate.availability == [Availability.FULL_TIME]


def test_saying_the_opposite_is_not_a_yes_to_a_knockout(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(ex(full_name="Ana López"))
    chat.say(unsure(has_license=False), text=GARBLED)
    assert chat.state.confirming is not None

    chat.say(
        ex(yes_no=True, has_license=True, vehicle=Vehicle.BOTH),
        text=f"sí True {Vehicle.BOTH} carnet moto y coche",
    )
    assert chat.state.status == Status.IN_PROGRESS
    assert chat.state.disqualify_reason is None
    assert chat.state.candidate.has_license is True


def test_a_plain_yes_still_confirms_a_knockout(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(ex(full_name="Ana López"))
    chat.say(unsure(has_license=False), text=GARBLED)
    chat.say(ex(yes_no=True), text="sí")
    assert chat.state.status == Status.DISQUALIFIED
    assert EventType.KNOCKOUT in [e.type for e in chat.result.events]


def test_a_bare_yes_read_as_having_a_license_still_confirms_the_knockout(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(ex(full_name="Ana López"))
    chat.say(unsure(has_license=False), text=GARBLED)
    chat.say(ex(yes_no=True, has_license=True), text="sí")
    assert chat.state.status == Status.DISQUALIFIED


def test_one_unsure_change_at_the_recap_is_still_confirmed(chat: Chat) -> None:
    before = at_recap(chat)
    chat.say(unsure(experience_years=6), text=GARBLED)
    assert chat.state.confirming is not None
    assert answers(chat) == before


def test_a_no_with_one_unsure_value_asks_what_to_change(chat: Chat) -> None:
    before = at_recap(chat)
    chat.say(unsure(yes_no=False, experience_years=0), text=GARBLED)
    assert answers(chat) == before
    assert chat.state.confirming is None
    assert chat.result.replies == [chat.config.template("es", "ask_change")]


def test_two_unsure_changes_at_the_recap_are_ignored_and_logged(chat: Chat) -> None:
    before = at_recap(chat)
    chat.say(unsure(schedule=[Schedule.MORNING], experience_years=0), text=GARBLED)
    assert answers(chat) == before
    assert chat.state.confirming is None
    assert [e.data for e in chat.result.events if e.type == EventType.FIELD_REJECTED] == [
        {"field": "schedule", "reason": "unsure"},
        {"field": "experience", "reason": "unsure"},
    ]


def test_a_sure_value_still_counts_next_to_ignored_ones(chat: Chat) -> None:
    at_recap(chat)
    message = unsure(availability=[Availability.PART_TIME], schedule=[Schedule.MORNING])
    message.fields.experience_years = ex(experience_years=6).fields.experience_years
    message.fields.experience_years.quote = "no todo es correcto"  # type: ignore[union-attr]
    chat.say(message, text=GARBLED)
    assert chat.state.candidate.experience_years == 6
    assert chat.state.candidate.availability == [Availability.FULL_TIME]


def test_values_at_the_confidence_limit_count_as_sure(chat: Chat) -> None:
    at_recap(chat)
    message = unsure(schedule=[Schedule.MORNING], experience_years=0)
    for name in ("schedule", "experience_years"):
        getattr(message.fields, name).confidence = chat.config.confirm_below_confidence
    chat.say(message, text=GARBLED)
    assert chat.state.candidate.experience_years == 0
    assert chat.state.candidate.schedule == [Schedule.MORNING]


def test_a_message_only_disagrees_when_the_engine_says_so(chat: Chat) -> None:
    at_recap(chat)
    result = apply_extraction(
        chat.state,
        unsure(experience_years=0).fields,
        chat.config,
        date(2026, 11, 2),
        message=GARBLED,
    )
    assert result.confirm == FieldName.EXPERIENCE
    assert not result.doubted


CONFIG = load_client_config("grupo_sazon")


def all_unsure(found: ExtractedFields) -> ExtractedFields:
    for name in type(found).model_fields:
        extracted = getattr(found, name)
        if extracted is not None:
            extracted.confidence = UNSURE
            extracted.quote = "unsure"
    return found


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(proposed=fields.map(all_unsure), yes_no=st.sampled_from([False, None]))
def test_no_unsure_value_replaces_an_answer_at_the_recap(
    proposed: ExtractedFields, yes_no: bool | None
) -> None:
    extractor = ScriptedExtractor()
    ports = Ports(extractor=extractor, responder=TemplateResponder(), clock=FakeClock(START))
    chat = Chat(ConversationState(client_id=CONFIG.client_id), CONFIG, ports, extractor)
    before = at_recap(chat)
    chat.say(ExtractionResult(fields=proposed, language="es", yes_no=yes_no), text="unsure")
    assert answers(chat) == before
    assert chat.state.status == Status.IN_PROGRESS
