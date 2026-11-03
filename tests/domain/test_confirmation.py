"""Confirming what we understood: low-confidence values and geocoded places wait for a yes."""

from datetime import date
from typing import Any

import pytest

from app.adapters.clock.clocks import FakeClock
from app.adapters.geo.fake import FakeGeocoder
from app.adapters.llm.fake import ScriptedExtractor, TemplateResponder
from app.domain.config import ClientConfig
from app.domain.models import (
    ConfirmField,
    ConversationState,
    EventType,
    ExtractionResult,
    FieldName,
    FieldStatus,
    ReAsk,
    Reason,
    ReviewReason,
    Say,
    Stage,
    Status,
    Vehicle,
)
from app.domain.policy import apply_confirmed, apply_extraction
from app.domain.ports import Ports
from tests.conftest import ex, said
from tests.domain.test_engine import Chat, answer_everything

TODAY = date(2026, 11, 2)


def unsure(confidence: float = 0.3, **fields: Any) -> ExtractionResult:
    """An extraction the model isn't sure of."""
    result = ex(**fields)
    for name in fields:
        getattr(result.fields, name).confidence = confidence
    return result


@pytest.fixture
def chat(
    state: ConversationState, config: ClientConfig, ports: Ports, extractor: ScriptedExtractor
) -> Chat:
    return Chat(state, config, ports, extractor)


@pytest.fixture
def geocoder() -> FakeGeocoder:
    return FakeGeocoder()


@pytest.fixture
def geo_chat(
    state: ConversationState,
    config: ClientConfig,
    extractor: ScriptedExtractor,
    clock: FakeClock,
    geocoder: FakeGeocoder,
) -> Chat:
    ports = Ports(extractor, TemplateResponder(), clock, geocoder=geocoder)
    chat = Chat(state, config, ports, extractor)
    chat.say(ex(yes_no=True))
    chat.say(ex(full_name="Ana López"))
    chat.say(ex(has_license=True, vehicle=Vehicle.MOTO))
    assert chat.state.awaiting == FieldName.LOCATION
    return chat


def events(chat: Chat) -> list[EventType]:
    return [e.type for e in chat.result.events]


# --- Policy -------------------------------------------------------------------------------------


def test_an_unsure_value_waits_for_confirmation(
    state: ConversationState, config: ClientConfig
) -> None:
    fields = unsure(full_name="Ana López").fields
    result = apply_extraction(state, fields, config, TODAY, message=said(fields))
    assert result.confirm == FieldName.FULL_NAME
    assert state.candidate.full_name is None  # nothing applied yet
    assert state.candidate.meta[FieldName.FULL_NAME].status == FieldStatus.MISSING
    assert state.confirming is not None
    assert state.confirming.values == {"full_name": "Ana López"}


def test_an_unsure_knockout_does_not_reject_until_confirmed(
    state: ConversationState, config: ClientConfig
) -> None:
    fields = unsure(has_license=False).fields
    result = apply_extraction(state, fields, config, TODAY, message=said(fields))
    assert result.knockout is None
    assert state.confirming is not None
    assert state.confirming.outcome == "knockout"
    assert apply_confirmed(state).knockout == Reason.NO_LICENSE
    assert state.candidate.has_license is False


def test_confirmed_values_survive_being_stored(
    state: ConversationState, config: ClientConfig
) -> None:
    """Between turns the state is JSON: dates and enums must come back typed."""
    fields = unsure(start_date=TODAY.replace(day=9)).fields
    apply_extraction(state, fields, config, TODAY, message=said(fields))
    stored = ConversationState.model_validate_json(state.model_dump_json())
    apply_confirmed(stored)
    assert stored.candidate.start_date == TODAY.replace(day=9)
    assert stored.candidate.meta[FieldName.START_DATE].status == FieldStatus.VALID
    assert stored.confirming is None


def test_sure_values_are_applied_as_before(state: ConversationState, config: ClientConfig) -> None:
    fields = ex(full_name="Ana López").fields  # confidence 0.9
    result = apply_extraction(state, fields, config, TODAY, message=said(fields))
    assert result.confirm is None
    assert state.candidate.full_name == "Ana López"


# --- Engine: low confidence ---------------------------------------------------------------------


def test_yes_confirms_and_moves_on(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(unsure(full_name="Ana López"))
    assert chat.result.actions[0] == ConfirmField(field=FieldName.FULL_NAME)
    assert "Ana López" in chat.result.replies[0]
    chat.say(ex(yes_no=True))
    assert chat.state.candidate.full_name == "Ana López"
    assert chat.state.awaiting == FieldName.LICENSE
    assert EventType.CONFIRMATION_ANSWERED in events(chat)


def test_a_yes_the_message_does_not_contain_confirms_nothing(chat: Chat) -> None:
    # The extractor claims "yes" for a message with no words in it (found by
    # tests/domain/test_properties.py): nothing the candidate wrote backs the value.
    chat.say(ex(yes_no=True))
    chat.say(unsure(full_name="Ana López"))
    chat.say(ex(yes_no=True), text="…")
    assert chat.state.candidate.meta[FieldName.FULL_NAME].status != FieldStatus.VALID
    assert chat.state.confirming is not None
    assert ConfirmField(field=FieldName.FULL_NAME) in chat.result.actions  # asked again


def test_no_asks_the_question_again(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(unsure(full_name="Ana López"))
    chat.say(ex(yes_no=False))
    assert chat.state.confirming is None
    assert chat.state.candidate.full_name is None
    assert isinstance(chat.result.actions[0], ReAsk)
    assert chat.result.actions[0].field == FieldName.FULL_NAME


def test_a_correction_replaces_the_unsure_value(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(unsure(full_name="Ana Lope"))
    chat.say(ex(full_name="Ana López"))  # "no, Ana López"
    assert chat.state.candidate.full_name == "Ana López"
    assert chat.state.awaiting == FieldName.LICENSE


def test_an_unsure_no_license_is_confirmed_before_the_knockout(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(ex(full_name="Ana López"))
    chat.say(unsure(has_license=False))
    assert chat.state.status == Status.IN_PROGRESS
    chat.say(ex(yes_no=True))
    assert chat.state.status == Status.DISQUALIFIED
    assert chat.state.disqualify_reason == Reason.NO_LICENSE


def test_no_clear_answer_eventually_asks_the_field_afresh(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(unsure(full_name="Ana López"))
    chat.say(ex())  # neither yes nor no
    assert ConfirmField(field=FieldName.FULL_NAME) in chat.result.actions
    chat.say(ex())
    assert chat.state.confirming is None
    assert chat.state.awaiting == FieldName.FULL_NAME


# --- Engine: geocoded places --------------------------------------------------------------------


def test_a_nearby_unlisted_town_is_confirmed_then_reviewed(
    geo_chat: Chat, geocoder: FakeGeocoder
) -> None:
    geo_chat.say(ex(city="Móstoles"))
    assert geocoder.asked == ["Móstoles"]
    assert "Móstoles (Comunidad de Madrid)" in geo_chat.result.replies[0]
    geo_chat.say(ex(yes_no=True))
    meta = geo_chat.state.candidate.meta[FieldName.LOCATION]
    assert meta.status == FieldStatus.NEEDS_REVIEW
    assert ReviewReason(reason=Reason.BORDERLINE_ZONE, field=FieldName.LOCATION) in (
        geo_chat.state.review_reasons
    )
    assert geo_chat.state.status == Status.IN_PROGRESS  # screening goes on
    assert geo_chat.state.awaiting == FieldName.AVAILABILITY


def test_a_far_town_is_confirmed_then_outside_the_area(geo_chat: Chat) -> None:
    geo_chat.say(ex(city="Bilbao"))
    assert geo_chat.state.status == Status.IN_PROGRESS  # not before they confirm
    geo_chat.say(ex(yes_no=True))
    assert geo_chat.state.status == Status.DISQUALIFIED
    assert geo_chat.state.disqualify_reason == Reason.OUTSIDE_AREA


def test_a_wrongly_understood_place_is_asked_again(geo_chat: Chat) -> None:
    geo_chat.say(ex(city="Toledo"))
    geo_chat.say(ex(yes_no=False))
    assert geo_chat.state.status == Status.IN_PROGRESS
    assert geo_chat.state.awaiting == FieldName.LOCATION


def test_an_unknown_place_is_asked_again_not_rejected(geo_chat: Chat) -> None:
    geo_chat.say(ex(city="Narnia"))
    assert geo_chat.state.status == Status.IN_PROGRESS
    assert isinstance(geo_chat.result.actions[0], ReAsk)


def test_a_failed_lookup_goes_to_review(geo_chat: Chat, geocoder: FakeGeocoder) -> None:
    geocoder.fail = True
    geo_chat.say(ex(city="Móstoles"))
    assert geo_chat.state.status == Status.IN_PROGRESS
    assert ReviewReason(reason=Reason.LOCATION_UNVERIFIED, field=FieldName.LOCATION) in (
        geo_chat.state.review_reasons
    )


def test_listed_places_are_not_looked_up(geo_chat: Chat, geocoder: FakeGeocoder) -> None:
    geo_chat.say(ex(city="Madrid", zone="Chamberí"))
    geo_chat.say(ex(availability=[]))
    assert geocoder.asked == []


def test_confirming_in_the_recap_shows_the_recap_again(chat: Chat) -> None:
    answer_everything(chat)
    assert chat.state.stage == Stage.RECAP
    chat.say(unsure(full_name="Ana María López"))
    assert chat.result.actions[0] == ConfirmField(field=FieldName.FULL_NAME)
    chat.say(ex(yes_no=True))  # yes to the check, not to the recap
    assert chat.state.stage == Stage.RECAP
    assert chat.state.candidate.full_name == "Ana María López"
    chat.say(ex(yes_no=True))
    assert chat.state.status == Status.QUALIFIED


# --- Voice: names and places are always read back ------------------------------------------------


@pytest.fixture
def voice_chat(config: ClientConfig, ports: Ports, extractor: ScriptedExtractor) -> Chat:
    state = ConversationState(client_id=config.client_id, channel="voice")
    chat = Chat(state, config, ports, extractor)
    chat.say(ex(yes_no=True))
    return chat


def test_voice_reads_back_a_name_the_model_is_sure_of(voice_chat: Chat) -> None:
    # Speech recognition heard "Aleksandra Zdravkovic" as this; the model can't tell.
    voice_chat.say(ex(full_name="Alexandras Lazkoz"))
    assert voice_chat.state.candidate.full_name is None
    assert ConfirmField(field=FieldName.FULL_NAME) in voice_chat.result.actions
    asked = [e for e in voice_chat.result.events if e.type == EventType.CONFIRMATION_ASKED]
    assert asked[0].data == {"field": "full_name", "why": "channel"}


def test_voice_does_not_reject_a_misheard_town_without_a_check(voice_chat: Chat) -> None:
    voice_chat.say(ex(full_name="Aleksandra Zdravkovic"))
    voice_chat.say(ex(yes_no=True))
    voice_chat.say(ex(has_license=True, vehicle=Vehicle.CAR))
    voice_chat.say(ex(city="Ghost"))  # "Gostivar", misheard: not a served city
    assert voice_chat.state.status == Status.IN_PROGRESS
    assert ConfirmField(field=FieldName.LOCATION) in voice_chat.result.actions
    voice_chat.say(ex(yes_no=True))  # now it's their answer, not the recogniser's
    assert voice_chat.state.status == Status.DISQUALIFIED
    assert voice_chat.state.disqualify_reason == Reason.OUTSIDE_AREA


def test_voice_no_to_a_read_back_asks_to_type_it(voice_chat: Chat) -> None:
    voice_chat.say(ex(full_name="Alexandras Lazkoz"))
    voice_chat.say(ex(yes_no=False))
    assert Say(template="retype_hint") in voice_chat.result.actions
    [hint] = voice_chat.result.replies  # the hint is the question: no second "Perdona…"
    assert "cuadro de texto" in hint
    assert voice_chat.state.awaiting == FieldName.FULL_NAME
    voice_chat.say(ex(full_name="Aleksandra Zdravkovic"))  # typed in the box: read back too
    assert ConfirmField(field=FieldName.FULL_NAME) in voice_chat.result.actions
    voice_chat.say(ex(yes_no=True))
    assert voice_chat.state.candidate.full_name == "Aleksandra Zdravkovic"


def test_voice_applies_other_sure_answers_at_once(voice_chat: Chat) -> None:
    voice_chat.say(ex(full_name="Ana López"))
    voice_chat.say(ex(yes_no=True))
    voice_chat.say(ex(has_license=True, vehicle=Vehicle.MOTO))
    assert voice_chat.state.candidate.vehicle == Vehicle.MOTO
    assert voice_chat.state.confirming is None


def test_chat_no_to_a_read_back_just_asks_again(chat: Chat) -> None:
    chat.say(ex(yes_no=True))
    chat.say(unsure(full_name="Ana López"))
    chat.say(ex(yes_no=False))
    assert Say(template="retype_hint") not in chat.result.actions


def test_answering_the_next_question_is_not_a_no_to_the_read_back(voice_chat: Chat) -> None:
    voice_chat.say(ex(full_name="Luis Pérez"))
    no_license = ex(has_license=False)
    no_license.fields.has_license.quote = "no tengo carnet"  # type: ignore[union-attr]
    voice_chat.say(no_license, text="no tengo carnet")
    assert Say(template="retype_hint") not in voice_chat.result.actions
    assert voice_chat.state.status == Status.DISQUALIFIED
    assert voice_chat.state.disqualify_reason == Reason.NO_LICENSE


def test_voice_never_applies_a_place_unchecked_while_another_is_pending(
    voice_chat: Chat,
) -> None:
    voice_chat.say(ex(full_name="Aleksandra Zdravkovic"))
    voice_chat.say(ex(has_license=True, vehicle=Vehicle.CAR, city="Ghost"))
    assert voice_chat.state.status == Status.IN_PROGRESS  # not rejected on an unchecked place
    assert voice_chat.state.candidate.vehicle == Vehicle.CAR
    assert voice_chat.state.candidate.city is None
    assert ConfirmField(field=FieldName.FULL_NAME) in voice_chat.result.actions
