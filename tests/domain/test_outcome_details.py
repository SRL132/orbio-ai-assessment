"""The details of a decision, not just its kind: the values kept, the evidence, the logged reason.

Mutation testing found these unchecked (docs/quality.md): the outcome could lose a value or log the
wrong reason and every other test still passed.
"""

from datetime import date

import pytest

from app.domain.config import ClientConfig
from app.domain.models import (
    Availability,
    ConversationState,
    EventType,
    Extracted,
    ExtractedFields,
    FieldName,
    FieldStatus,
    GeoLookup,
    GeoPlace,
    PendingConfirmation,
    Reason,
    ReviewReason,
    Stage,
    Vehicle,
)
from app.domain.policy import _days_only, apply_confirmed, apply_extraction
from app.domain.validators import (
    distance_km,
    license_code,
    nearest_location,
    says_no_language,
    validate_license,
    validate_location,
)
from tests.conftest import ex, said

TODAY = date(2026, 11, 2)
MOSTOLES = GeoPlace(name="Móstoles", country_code="es", lat=40.3228, lon=-3.8649)
FAR_AWAY = GeoPlace(name="Bilbao", country_code="es", lat=43.263, lon=-2.935)


def why_asked(result: object) -> list[object]:
    events = result.events  # type: ignore[attr-defined]
    return [e.data["why"] for e in events if e.type == EventType.CONFIRMATION_ASKED]


class TestLicenseDetails:
    def test_a_license_type_keeps_every_value(self) -> None:
        assert validate_license(True, Vehicle.MOTO, "tipo A2").updates == {
            "has_license": True,
            "vehicle": Vehicle.MOTO,
            "license_type": "A2",
        }

    def test_spaces_in_a_license_code_are_dropped(self) -> None:
        assert license_code("A 2") == "A2"

    def test_the_stored_license_type_is_the_cleaned_code(
        self, state: ConversationState, config: ClientConfig
    ) -> None:
        fields = ex(has_license=True, vehicle=Vehicle.MOTO, license_type="tipo A2").fields
        apply_extraction(state, fields, config, TODAY, message=said(fields))
        assert state.candidate.license_type == "A2"


class TestLanguageNeutralMessages:
    def test_the_longest_name_is_removed_first(self, config: ClientConfig) -> None:
        """Removing "Maria" first would leave "ana", which looks like a word of some language."""
        assert says_no_language("Ana Maria", config, ["Maria", "Ana Maria"])


class TestZones:
    def test_punctuation_after_a_prefix_is_ignored(self, config: ClientConfig) -> None:
        outcome = validate_location("Madrid", "Colonia, Retiro", None, config)
        assert (outcome.kind, outcome.updates["location_id"]) == ("valid", "es-madrid")

    def test_a_zone_that_is_only_a_prefix_word_goes_to_review(self, config: ClientConfig) -> None:
        outcome = validate_location("Madrid", "La", None, config)
        assert (outcome.kind, outcome.reason) == ("review", Reason.ZONE_NOT_LISTED)


class TestDistance:
    @pytest.mark.parametrize("end", [(1.0, 0.0), (0.0, 1.0)])
    def test_one_degree_at_the_equator(self, end: tuple[float, float]) -> None:
        assert distance_km(0.0, 0.0, *end) == pytest.approx(111.1949, abs=0.001)

    def test_a_location_missing_either_coordinate_is_skipped(self, config: ClientConfig) -> None:
        first, *others = config.locations
        half = config.model_copy(
            update={"locations": [first.model_copy(update={"lon": None}), *others]}
        )
        nearest = nearest_location(GeoPlace(name="x", country_code="es", lat=40.4, lon=-3.7), half)
        assert nearest is not None
        assert nearest[0].id != first.id

    def test_no_coordinates_means_no_nearest_location(self, config: ClientConfig) -> None:
        bare = config.model_copy(
            update={
                "locations": [
                    loc.model_copy(update={"lat": None, "lon": None}) for loc in config.locations
                ]
            }
        )
        assert nearest_location(MOSTOLES, bare) is None


class TestGeocodedOutcomes:
    def test_a_failed_lookup_keeps_what_they_said_for_the_recruiter(
        self, config: ClientConfig
    ) -> None:
        geo = GeoLookup(place="Móstoles", failed=True)
        outcome = validate_location("Móstoles", "Centro", None, config, geo)
        assert (outcome.kind, outcome.reason) == ("review", Reason.LOCATION_UNVERIFIED)
        assert outcome.updates == {"city": "Móstoles", "zone": "Centro"}

    def test_a_failed_lookup_of_a_place_given_as_zone_keeps_it_as_the_city(
        self, config: ClientConfig
    ) -> None:
        geo = GeoLookup(place="Móstoles", failed=True)
        outcome = validate_location(None, "Móstoles", None, config, geo)
        assert outcome.updates == {"city": "Móstoles", "zone": None}

    def test_exactly_at_the_radius_is_still_review(self, config: ClientConfig) -> None:
        nearest = nearest_location(MOSTOLES, config)
        assert nearest is not None
        at_edge = config.model_copy(update={"geo_review_radius_km": nearest[1]})
        geo = GeoLookup(place="Móstoles", found=MOSTOLES)
        assert validate_location("Móstoles", None, None, at_edge, geo).kind == "review"

    def test_a_town_beyond_the_radius_keeps_the_name_found(self, config: ClientConfig) -> None:
        geo = GeoLookup(place="Bilbao", found=FAR_AWAY)
        assert validate_location("Bilbao", None, None, config, geo).updates == {"city": "Bilbao"}


def test_a_town_given_as_the_zone_keeps_its_postal_code(config: ClientConfig) -> None:
    outcome = validate_location(None, "Madrid", "28010", config)
    assert outcome.kind == "valid"
    assert outcome.updates["postal_code"] == "28010"


class TestWhyConfirm:
    def test_geocoded(self, state: ConversationState, config: ClientConfig) -> None:
        fields = ex(city="Móstoles").fields
        geo = GeoLookup(place="Móstoles", found=MOSTOLES)
        result = apply_extraction(state, fields, config, TODAY, message=said(fields), geo=geo)
        assert why_asked(result) == ["geocoded"]

    def test_low_confidence(self, state: ConversationState, config: ClientConfig) -> None:
        fields = ex(full_name="Ana López").fields
        fields.full_name.confidence = config.confirm_below_confidence - 0.1  # type: ignore[union-attr]
        result = apply_extraction(state, fields, config, TODAY, message=said(fields))
        assert why_asked(result) == ["low_confidence"]

    def test_exactly_at_the_threshold_counts_without_asking(
        self, state: ConversationState, config: ClientConfig
    ) -> None:
        fields = ex(full_name="Ana López").fields
        fields.full_name.confidence = config.confirm_below_confidence  # type: ignore[union-attr]
        result = apply_extraction(state, fields, config, TODAY, message=said(fields))
        assert result.confirm is None
        assert state.candidate.full_name == "Ana López"

    def test_a_change_nobody_asked_about(
        self, state: ConversationState, config: ClientConfig
    ) -> None:
        first = ex(full_name="Ana López").fields
        apply_extraction(state, first, config, TODAY, message=said(first))
        state.stage, state.awaiting = Stage.COLLECTING, FieldName.EXPERIENCE
        fields = ex(full_name="Mai Ruiz").fields
        result = apply_extraction(state, fields, config, TODAY, message=said(fields))
        assert why_asked(result) == ["unasked_change"]

    def test_the_same_answer_cleaned_up_is_not_a_change(
        self, state: ConversationState, config: ClientConfig
    ) -> None:
        """Compared after the validator cleans it: "ana lópez" is the "Ana López" we have."""
        first = ex(full_name="Ana López").fields
        apply_extraction(state, first, config, TODAY, message=said(first))
        state.stage, state.awaiting = Stage.COLLECTING, FieldName.EXPERIENCE
        fields = ex(full_name="ana lópez").fields
        result = apply_extraction(state, fields, config, TODAY, message=said(fields))
        assert result.confirm is None


class TestPendingConfirmation:
    def test_a_geocoded_place_waits_with_its_evidence(
        self, state: ConversationState, config: ClientConfig
    ) -> None:
        fields = ex(city="Móstoles").fields
        geo = GeoLookup(place="Móstoles", found=MOSTOLES)
        apply_extraction(state, fields, config, TODAY, message=said(fields), geo=geo)
        pending = state.confirming
        assert pending is not None
        assert pending.outcome == "review"
        assert pending.reason == Reason.BORDERLINE_ZONE
        assert pending.note == "geocoded"
        assert pending.quote == "Móstoles"
        assert pending.confidence == 0.9
        assert pending.place == MOSTOLES.label()

    def test_a_geocoded_knockout_waits_with_its_reason(
        self, state: ConversationState, config: ClientConfig
    ) -> None:
        fields = ex(city="Bilbao").fields
        geo = GeoLookup(place="Bilbao", found=FAR_AWAY)
        apply_extraction(state, fields, config, TODAY, message=said(fields), geo=geo)
        assert state.confirming is not None
        assert state.confirming.outcome == "knockout"
        assert state.confirming.reason == Reason.OUTSIDE_AREA

    def test_a_valid_value_waits_with_no_reason(
        self, state: ConversationState, config: ClientConfig
    ) -> None:
        fields = ex(full_name="Ana López").fields
        fields.full_name.confidence = 0.1  # type: ignore[union-attr]
        apply_extraction(state, fields, config, TODAY, message=said(fields))
        assert state.confirming is not None
        assert state.confirming.outcome == "valid"
        assert state.confirming.reason is None

    def test_an_unsure_value_doesnt_name_a_place_it_didnt_look_up(
        self, state: ConversationState, config: ClientConfig
    ) -> None:
        fields = ex(full_name="Ana López").fields
        fields.full_name.confidence = 0.1  # type: ignore[union-attr]
        geo = GeoLookup(place="Móstoles", found=MOSTOLES)
        apply_extraction(state, fields, config, TODAY, message=said(fields), geo=geo)
        assert state.confirming is not None
        assert state.confirming.field == FieldName.FULL_NAME
        assert state.confirming.place is None

    def test_a_channel_confirmation_waiting_doesnt_stop_later_fields(
        self, config: ClientConfig
    ) -> None:
        """On voice the name waits its turn; the availability in the same message still counts."""
        state = ConversationState(client_id=config.client_id, channel="voice")
        state.confirming = PendingConfirmation(
            field=FieldName.LOCATION, values={"city": "Móstoles"}, outcome="review"
        )
        fields = ex(full_name="Ana López", availability=[Availability.WEEKENDS]).fields
        apply_extraction(state, fields, config, TODAY, message=said(fields))
        assert state.candidate.full_name is None
        assert state.candidate.availability == [Availability.WEEKENDS]


class TestAfterConfirming:
    def test_a_confirmed_place_keeps_its_note_and_counts_as_grounded(
        self, state: ConversationState, config: ClientConfig
    ) -> None:
        fields = ex(city="Móstoles").fields
        geo = GeoLookup(place="Móstoles", found=MOSTOLES)
        apply_extraction(state, fields, config, TODAY, message=said(fields), geo=geo)
        result = apply_confirmed(state)
        assert state.candidate.meta[FieldName.LOCATION].note == "geocoded"
        assert state.review_reasons == [
            ReviewReason(reason=Reason.BORDERLINE_ZONE, field=FieldName.LOCATION)
        ]
        extracted = [e for e in result.events if e.type == EventType.FIELD_EXTRACTED]
        assert [e.data["grounded"] for e in extracted] == [True]


class TestUngrounded:
    def test_an_ungrounded_review_records_both_reasons_on_the_field(
        self, state: ConversationState, config: ClientConfig
    ) -> None:
        apply_extraction(state, ex(city="Getafe").fields, config, TODAY, message="otra cosa")
        assert state.review_reasons == [
            ReviewReason(reason=Reason.BORDERLINE_ZONE, field=FieldName.LOCATION),
            ReviewReason(reason=Reason.UNGROUNDED_ANSWER, field=FieldName.LOCATION),
        ]

    def test_an_ungrounded_value_is_kept_as_the_validator_cleaned_it(
        self, state: ConversationState, config: ClientConfig
    ) -> None:
        apply_extraction(state, ex(full_name="ana lópez").fields, config, TODAY, message="hola")
        assert state.candidate.full_name == "Ana López"
        assert state.candidate.meta[FieldName.FULL_NAME].status == FieldStatus.NEEDS_REVIEW


class TestAvailabilityRule:
    def test_no_availability_proposed_is_not_days_only(self, config: ClientConfig) -> None:
        assert _days_only(ExtractedFields(), config) is False

    def test_a_client_with_no_phrases_never_rejects_for_days_only(
        self, config: ClientConfig
    ) -> None:
        no_rule = config.model_copy(
            update={
                "availability_days_only": config.availability_days_only.model_copy(
                    update={"phrases": []}
                )
            }
        )
        fields = ExtractedFields(
            availability=Extracted(
                value=[Availability.PART_TIME], quote="entre semana", confidence=0.9
            )
        )
        assert _days_only(fields, no_rule) is False


def test_junk_at_the_license_question_is_rejected_as_unclear(
    state: ConversationState, config: ClientConfig
) -> None:
    fields = ExtractedFields.model_validate(
        {"has_license": {"value": False, "quote": "000", "confidence": 0.9}}
    )
    result = apply_extraction(state, fields, config, TODAY, message="000")
    assert result.rejected[FieldName.LICENSE] == "unclear"
