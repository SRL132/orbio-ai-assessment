from datetime import date

import pytest

from app.domain import engine
from app.domain.config import ClientConfig
from app.domain.models import (
    Availability,
    ConversationState,
    FieldName,
    Reason,
    ReviewReason,
    Schedule,
    Status,
    Vehicle,
)
from app.domain.ports import Ports
from app.domain.summary import build_summary, describe_reason


def filled(state: ConversationState) -> ConversationState:
    c = state.candidate
    c.full_name, c.vehicle, c.city, c.zone = "Ana López", Vehicle.MOTO, "Madrid", "Chamberí"
    c.availability, c.schedule = [Availability.FULL_TIME], [Schedule.EVENING]
    c.experience_years, c.platforms, c.start_date = 2, ["Glovo"], date(2026, 11, 9)
    return state


def test_qualified_summary(state: ConversationState, config: ClientConfig) -> None:
    state = filled(state)
    state.status = Status.QUALIFIED
    lines = build_summary(state, config).splitlines()
    assert lines[0] == "Qualified: meets every hard requirement."
    assert lines[1] == "Ana López · Chamberí, Madrid · moto license"
    assert "Start: 09/11/2026" in lines[2]
    assert lines[3] == "Experience: 2 years (Glovo)"


def test_review_summary_explains_reasons(state: ConversationState, config: ClientConfig) -> None:
    state.status = Status.NEEDS_REVIEW
    state.review_reasons = ["borderline_zone:location", "human_requested"]
    headline = build_summary(state, config).splitlines()[0]
    assert headline == "Needs review: borderline zone (location); asked to talk to a person."


def test_flags(state: ConversationState, config: ClientConfig) -> None:
    state = filled(state)
    state.status = Status.DISQUALIFIED
    state.disqualify_reason = "outside_area"
    state.keep_on_file = True
    state.candidate.meta[FieldName.FULL_NAME].note = "surname_missing"
    summary = build_summary(state, config)
    assert summary.startswith("Disqualified: lives outside the service area.")
    assert "Flags: first name only; keep on file: yes" in summary


def test_abandoned_says_where(state: ConversationState, config: ClientConfig) -> None:
    state.status = Status.ABANDONED
    state.awaiting = FieldName.START_DATE
    assert build_summary(state, config).startswith("Abandoned at: start date.")


def test_unknown_reason_is_readable() -> None:
    assert describe_reason("something_new") == "something new"


def test_override_keeps_history(
    state: ConversationState, config: ClientConfig, ports: Ports
) -> None:
    state.status = Status.NEEDS_REVIEW
    new_state, events = engine.override_status(
        state, Status.QUALIFIED, "Zone confirmed by phone", "rec@sazon.test", config, ports
    )
    assert new_state.status == Status.QUALIFIED
    assert new_state.summary is not None
    assert new_state.summary.startswith("Qualified")
    assert events[0].data == {
        "from": "needs_review",
        "to": "qualified",
        "reason": "Zone confirmed by phone",
        "actor": "rec@sazon.test",
    }
    assert state.status == Status.NEEDS_REVIEW  # the input is not mutated


@pytest.mark.parametrize(
    ("status", "reason"), [(Status.QUALIFIED, "  "), (Status.IN_PROGRESS, "because")]
)
def test_override_needs_reason_and_final_status(
    state: ConversationState, config: ClientConfig, ports: Ports, status: Status, reason: str
) -> None:
    with pytest.raises(ValueError, match=r"reason|cannot override"):
        engine.override_status(state, status, reason, "rec", config, ports)


def test_in_progress_and_more_flags(state: ConversationState, config: ClientConfig) -> None:
    state = filled(state)
    meta = state.candidate.meta
    meta[FieldName.START_DATE].note = "start_date_far"
    meta[FieldName.SCHEDULE].status = meta[FieldName.SCHEDULE].status.VALID
    meta[FieldName.SCHEDULE].confidence = 0.4
    summary = build_summary(state, config)
    assert summary.startswith("In progress.")
    assert "start date more than 60 days away" in summary
    assert "low-confidence answers: schedule" in summary


def test_abandoned_before_any_question(state: ConversationState, config: ClientConfig) -> None:
    state.status = Status.ABANDONED
    assert build_summary(state, config).startswith("Abandoned at: consent.")


def test_review_reasons_read_their_text_form() -> None:
    # States saved before review reasons were typed, the projection and eval scenarios use it.
    parsed = ReviewReason.model_validate("borderline_zone:location")
    assert parsed == ReviewReason(reason=Reason.BORDERLINE_ZONE, field=FieldName.LOCATION)
    assert str(parsed) == "borderline_zone:location"
    assert ReviewReason.model_validate("human_requested").field is None
    assert describe_reason(parsed) == describe_reason("borderline_zone:location")
