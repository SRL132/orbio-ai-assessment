"""Exact outcomes: not just "valid" or "review", but the values and reasons they carry.

Mutation testing (`uv run mutmut run`, docs/quality.md) showed the other tests checked an
outcome's kind and missed changes to what it hands back: the location id an interview is booked
at, the reason a re-ask explains, the audit event's fields. These pin them down.
"""

from datetime import date, timedelta

import pytest

from app.domain.config import ClientConfig
from app.domain.models import (
    Availability,
    Candidate,
    ConversationState,
    EventType,
    FieldName,
    FieldStatus,
    Reason,
    ReviewReason,
    Schedule,
    Vehicle,
)
from app.domain.policy import apply_extraction, progress, register_failed_attempt
from app.domain.validators import (
    MAX_EXPERIENCE_YEARS,
    Outcome,
    final_review_reasons,
    validate_availability,
    validate_experience,
    validate_full_name,
    validate_license,
    validate_location,
    validate_schedule,
    validate_start_date,
)
from tests.conftest import ex

TODAY = date(2026, 11, 2)


@pytest.mark.parametrize(
    ("city", "zone", "postal", "expected"),
    [
        (
            "Madrid",
            None,
            "28013",
            Outcome.valid(location_id="es-madrid", city="Madrid", postal_code="28013", zone=None),
        ),
        (
            "Madrid",
            "Centro",
            "28013",
            Outcome.valid(
                location_id="es-madrid", city="Madrid", postal_code="28013", zone="Centro"
            ),
        ),
        (
            "madrid",
            "chamberí",
            None,
            Outcome.valid(location_id="es-madrid", city="Madrid", zone="chamberí"),
        ),
        (
            "Valencia",
            "Ruzafa",
            None,
            Outcome.valid(location_id="es-valencia", city="Valencia", zone="Ruzafa"),
        ),
        (
            "Madrid",
            "Getafe",
            None,
            Outcome.review(Reason.BORDERLINE_ZONE, city="Madrid", zone="Getafe"),
        ),
        (
            "Madrid",
            "Narnia",
            None,
            Outcome.review(Reason.ZONE_NOT_LISTED, city="Madrid", zone="Narnia"),
        ),
        (
            "Madrid",
            None,
            "41001",
            Outcome.review(Reason.POSTAL_CODE_NOT_LISTED, city="Madrid", postal_code="41001"),
        ),
        ("Madrid", None, None, Outcome.invalid("zone_missing", city="Madrid")),
        (
            "Getafe",
            "Centro",
            None,
            Outcome.review(Reason.BORDERLINE_ZONE, city="Getafe", zone="Centro"),
        ),
        ("Narnia", None, None, Outcome.knockout(Reason.OUTSIDE_AREA)),
        (None, None, "28013", Outcome.invalid("city_missing", postal_code="28013")),
        (None, None, None, Outcome.invalid("missing")),
    ],
)
def test_location_outcomes(
    config: ClientConfig, city: str | None, zone: str | None, postal: str | None, expected: Outcome
) -> None:
    assert validate_location(city, zone, postal, config) == expected


def test_other_field_outcomes(config: ClientConfig) -> None:
    assert validate_full_name(None) == Outcome.invalid("missing")
    assert validate_full_name("1234") == Outcome.invalid("not_a_name")
    assert validate_license(True, None) == Outcome.invalid("vehicle_missing", has_license=True)
    assert validate_availability([]) == Outcome.invalid("missing")
    assert validate_schedule([]) == Outcome.invalid("missing")
    both = [Availability.WEEKENDS, Availability.FULL_TIME, Availability.WEEKENDS]
    assert validate_availability(both) == Outcome.valid(
        availability=[Availability.FULL_TIME, Availability.WEEKENDS]
    )
    assert validate_experience(2.5, ["glovo", "uber eats"], config) == Outcome.valid(
        experience_years=2.5, platforms=["Glovo", "Uber Eats"]
    )


def test_boundaries(config: ClientConfig) -> None:
    limit = TODAY + timedelta(days=config.far_start_days)
    assert validate_start_date(limit, TODAY, config) == Outcome.valid(start_date=limit)
    later = limit + timedelta(days=1)
    assert validate_start_date(later, TODAY, config) == Outcome.valid(
        note="start_date_far", start_date=later
    )
    assert validate_experience(MAX_EXPERIENCE_YEARS, [], config).kind == "valid"
    assert validate_experience(MAX_EXPERIENCE_YEARS + 0.5, [], config).kind == "invalid"


def _moto(license_type: str, years: float | None, location: str | None = "es-madrid") -> Candidate:
    return Candidate(
        vehicle=Vehicle.MOTO, license_type=license_type, license_years=years, location_id=location
    )


@pytest.mark.parametrize(
    ("candidate", "reasons"),
    [
        (_moto("B", 3), []),  # exactly the minimum years: accepted
        (_moto("B", 2.9), [Reason.LICENSE_UNCLEAR]),
        (_moto("B", None), [Reason.LICENSE_UNCLEAR]),
        (_moto("C", 10), [Reason.LICENSE_UNCLEAR]),  # no rule for C at all
        (_moto("a1", None), []),  # accepted type, any case
        (_moto("B", 1, location=None), []),  # no location yet: nothing to check against
        (Candidate(vehicle=Vehicle.CAR, license_type="B", location_id="es-madrid"), []),
    ],
)
def test_license_rules(config: ClientConfig, candidate: Candidate, reasons: list[Reason]) -> None:
    assert final_review_reasons(candidate, config) == reasons


def test_a_country_with_only_conditional_rules_is_still_checked(config: ClientConfig) -> None:
    rules = config.license_rules["ES"].model_copy(update={"accepted_for_moto": []})
    only_conditional = config.model_copy(
        update={"license_rules": {**config.license_rules, "ES": rules}}
    )
    assert final_review_reasons(_moto("B", 1), only_conditional) == [Reason.LICENSE_UNCLEAR]


# --- apply_extraction ------------------------------------------------------------------------


def test_events_record_which_field_and_why(config: ClientConfig) -> None:
    state = ConversationState(client_id=config.client_id)
    result = apply_extraction(
        state,
        ex(start_date=TODAY - timedelta(days=3), schedule=[Schedule.MORNING]).fields,
        config,
        TODAY,
    )
    by_type = {e.type: e.data for e in result.events}
    assert by_type[EventType.FIELD_REJECTED] == {"field": "start_date", "reason": "in_past"}
    assert by_type[EventType.FIELD_EXTRACTED] == {
        "field": "schedule",
        "status": "valid",
        "confidence": 0.9,
    }
    assert result.rejected == {FieldName.START_DATE: "in_past"}


def test_a_rejected_answer_does_not_stop_the_others(config: ClientConfig) -> None:
    # "Empiezo ayer, por las mañanas": the bad date is rejected, the schedule still counts.
    state = ConversationState(client_id=config.client_id)
    apply_extraction(
        state,
        ex(full_name="1234", schedule=[Schedule.MORNING], experience_years=2).fields,
        config,
        TODAY,
    )
    assert state.candidate.schedule == [Schedule.MORNING]
    assert state.candidate.experience_years == 2


def test_knockout_event_and_value(config: ClientConfig) -> None:
    state = ConversationState(client_id=config.client_id)
    result = apply_extraction(state, ex(has_license=False).fields, config, TODAY)
    assert result.knockout == Reason.NO_LICENSE
    assert result.events[-1].data == {"field": "license", "reason": "no_license"}
    assert state.candidate.has_license is False  # the answer is kept, for the record


def test_review_outcome_notes_and_evidence(config: ClientConfig) -> None:
    state = ConversationState(client_id=config.client_id)
    fields = ex(city="Madrid", zone="Getafe").fields
    fields.city.quote, fields.zone.quote = "vivo en Madrid", "en Getafe"  # type: ignore[union-attr]
    fields.zone.confidence = 0.4  # type: ignore[union-attr]
    result = apply_extraction(state, fields, config, TODAY)
    meta = state.candidate.meta[FieldName.LOCATION]
    assert meta.note == "borderline_zone"
    assert meta.quote == "vivo en Madrid / en Getafe"  # each quote once, joined
    assert meta.confidence == 0.4  # the least sure part decides
    assert result.added == {FieldName.LOCATION}
    assert state.review_reasons == [
        ReviewReason(reason=Reason.BORDERLINE_ZONE, field=FieldName.LOCATION)
    ]


def test_changing_an_answer_records_it(config: ClientConfig) -> None:
    state = ConversationState(client_id=config.client_id)
    apply_extraction(state, ex(schedule=[Schedule.MORNING]).fields, config, TODAY)
    result = apply_extraction(state, ex(schedule=[Schedule.EVENING]).fields, config, TODAY)
    assert result.changed == {FieldName.SCHEDULE}
    assert result.added == set()
    assert {"field": "schedule"} in [
        e.data for e in result.events if e.type == EventType.FIELD_CHANGED
    ]


def test_progress_counts_leading_answered_fields(config: ClientConfig) -> None:
    candidate = Candidate()
    assert progress(candidate, config) == 0
    order = config.field_priority
    candidate.meta[order[0]].status = FieldStatus.VALID
    candidate.meta[order[1]].status = FieldStatus.NEEDS_REVIEW
    candidate.meta[order[3]].status = FieldStatus.VALID  # after a gap: not counted
    assert progress(candidate, config) == 2


def test_a_field_gives_up_after_the_configured_attempts(config: ClientConfig) -> None:
    state = ConversationState(client_id=config.client_id)
    tries = [
        register_failed_attempt(state, FieldName.SCHEDULE, config)
        for _ in range(config.max_attempts)
    ]
    assert tries == [False] * (config.max_attempts - 1) + [True]
    meta = state.candidate.meta[FieldName.SCHEDULE]
    assert (meta.status, meta.note, meta.attempts) == (
        FieldStatus.NEEDS_REVIEW,
        "failed_twice",
        config.max_attempts,
    )


def test_postal_code_reaches_the_validator(config: ClientConfig) -> None:
    state = ConversationState(client_id=config.client_id)
    apply_extraction(state, ex(city="Madrid", postal_code="28013").fields, config, TODAY)
    assert (state.candidate.location_id, state.candidate.postal_code) == ("es-madrid", "28013")


def test_empty_quotes_are_not_evidence(config: ClientConfig) -> None:
    state = ConversationState(client_id=config.client_id)
    fields = ex(city="Madrid", zone="Centro").fields
    fields.city.quote = ""  # type: ignore[union-attr]
    fields.zone.quote = "en el Centro"  # type: ignore[union-attr]
    apply_extraction(state, fields, config, TODAY)
    assert state.candidate.meta[FieldName.LOCATION].quote == "en el Centro"
    fields.zone.quote = ""  # type: ignore[union-attr]
    other = ConversationState(client_id=config.client_id)
    apply_extraction(other, fields, config, TODAY)
    assert other.candidate.meta[FieldName.LOCATION].quote is None


def test_a_first_name_alone_is_accepted_when_the_name_gives_up(config: ClientConfig) -> None:
    state = ConversationState(client_id=config.client_id)
    state.candidate.full_name = "Ana"  # the validator kept the first name
    tries = [
        register_failed_attempt(state, FieldName.FULL_NAME, config)
        for _ in range(config.max_attempts)
    ]
    assert tries[-1] is True  # moves on
    meta = state.candidate.meta[FieldName.FULL_NAME]
    assert (meta.status, meta.note) == (FieldStatus.VALID, "surname_missing")
    assert state.review_reasons == []


def test_reasons_are_only_read_from_the_right_outcomes() -> None:
    with pytest.raises(ValueError, match="no knockout or review reason"):
        _ = Outcome.invalid("missing").verdict
    with pytest.raises(ValueError, match="no invalid-answer reason"):
        _ = Outcome.knockout(Reason.NO_LICENSE).problem
    assert Outcome.review(Reason.ZONE_NOT_LISTED).verdict == Reason.ZONE_NOT_LISTED
    assert Outcome.invalid("in_past").problem == "in_past"
