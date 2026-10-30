from datetime import date, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.adapters.config.yaml_loader import load_client_config
from app.domain.config import ClientConfig
from app.domain.models import Availability, Candidate, Schedule, Vehicle
from app.domain.validators import (
    final_review_reasons,
    normalize,
    normalize_platforms,
    validate_availability,
    validate_experience,
    validate_full_name,
    validate_license,
    validate_location,
    validate_schedule,
    validate_start_date,
)

TODAY = date(2026, 11, 2)
CONFIG = load_client_config("grupo_sazon")
letters = st.text(alphabet=st.characters(categories=["Lu", "Ll"]), min_size=1, max_size=12)


class TestFullName:
    def test_first_name_and_surname_is_valid(self) -> None:
        outcome = validate_full_name("  Ana   López ")
        assert outcome.kind == "valid"
        assert outcome.updates == {"full_name": "Ana López"}

    def test_first_name_only_keeps_partial_and_asks_for_surname(self) -> None:
        outcome = validate_full_name("Ana")
        assert outcome.kind == "invalid"
        assert outcome.reason == "surname_missing"
        assert outcome.updates == {"full_name": "Ana"}

    @pytest.mark.parametrize("value", [None, "", "   ", "1234", "!!"])
    def test_missing_or_not_a_name(self, value: str | None) -> None:
        assert validate_full_name(value).kind == "invalid"

    def test_hyphens_and_apostrophes_are_kept(self) -> None:
        assert validate_full_name("María-José O'Neill").updates == {
            "full_name": "María-José O'Neill"
        }

    @given(first=letters, last=letters)
    def test_any_two_words_of_letters_are_valid(self, first: str, last: str) -> None:
        assert validate_full_name(f"{first} {last}").kind == "valid"


class TestLicense:
    def test_no_license_is_a_knockout(self) -> None:
        outcome = validate_license(False, None)
        assert (outcome.kind, outcome.reason) == ("knockout", "no_license")

    def test_license_without_vehicle_asks_again(self) -> None:
        outcome = validate_license(True, None)
        assert (outcome.kind, outcome.reason) == ("invalid", "vehicle_missing")

    def test_license_with_vehicle_is_valid(self) -> None:
        assert validate_license(True, Vehicle.MOTO).kind == "valid"

    def test_unknown_is_missing(self) -> None:
        assert validate_license(None, None).reason == "missing"


class TestLocation:
    def test_served_city_and_zone(self, config: ClientConfig) -> None:
        outcome = validate_location("Madrid", "Chamberí", None, config)
        assert outcome.kind == "valid"
        assert outcome.updates["location_id"] == "es-madrid"

    def test_accents_and_case_are_ignored(self, config: ClientConfig) -> None:
        assert validate_location("madrid", "CHAMBERI", None, config).kind == "valid"

    def test_city_with_zones_but_no_zone_asks_for_zone(self, config: ClientConfig) -> None:
        outcome = validate_location("Madrid", None, None, config)
        assert (outcome.kind, outcome.reason) == ("invalid", "zone_missing")

    def test_city_plus_matching_postal_code(self, config: ClientConfig) -> None:
        outcome = validate_location("Madrid", None, "28010", config)
        assert outcome.kind == "valid"
        assert outcome.updates["postal_code"] == "28010"

    def test_postal_code_alone_asks_for_city(self, config: ClientConfig) -> None:
        # 06700 is a Mexico City code but also Badajoz (Spain): the city must decide.
        outcome = validate_location(None, None, "06700", config)
        assert (outcome.kind, outcome.reason) == ("invalid", "city_missing")

    def test_unlisted_zone_in_served_city_goes_to_review(self, config: ClientConfig) -> None:
        outcome = validate_location("Madrid", "Vallecas", None, config)
        assert (outcome.kind, outcome.reason) == ("review", "zone_not_listed")

    def test_nearby_town_goes_to_review(self, config: ClientConfig) -> None:
        outcome = validate_location("Getafe", None, None, config)
        assert (outcome.kind, outcome.reason) == ("review", "borderline_zone")

    def test_city_clearly_outside_is_a_knockout(self, config: ClientConfig) -> None:
        outcome = validate_location("Bilbao", None, None, config)
        assert (outcome.kind, outcome.reason) == ("knockout", "outside_area")

    def test_city_served_as_a_whole(self, config: ClientConfig) -> None:
        assert validate_location("València", None, None, config).kind == "valid"

    @pytest.mark.parametrize("alias", ["CDMX", "Mexico City", "Ciudad de Mexico"])
    def test_mexico_city_aliases(self, config: ClientConfig, alias: str) -> None:
        outcome = validate_location(alias, "Roma Norte", None, config)
        assert outcome.kind == "valid"
        assert outcome.updates["city"] == "Ciudad de México"

    def test_nothing_given(self, config: ClientConfig) -> None:
        assert validate_location(None, None, None, config).reason == "missing"


class TestAvailabilityAndSchedule:
    def test_empty_is_invalid(self) -> None:
        assert validate_availability([]).kind == "invalid"
        assert validate_schedule([]).kind == "invalid"

    def test_duplicates_are_removed(self) -> None:
        outcome = validate_availability([Availability.WEEKENDS, Availability.WEEKENDS])
        assert outcome.updates == {"availability": [Availability.WEEKENDS]}

    @given(st.lists(st.sampled_from(list(Schedule)), min_size=1))
    def test_any_non_empty_schedule_is_valid(self, schedule: list[Schedule]) -> None:
        assert validate_schedule(schedule).kind == "valid"


class TestExperience:
    def test_zero_years_is_valid(self, config: ClientConfig) -> None:
        assert validate_experience(0, [], config).kind == "valid"

    @pytest.mark.parametrize("years", [-1, 61])
    def test_out_of_range(self, config: ClientConfig, years: float) -> None:
        assert validate_experience(years, [], config).reason == "out_of_range"

    def test_missing_years(self, config: ClientConfig) -> None:
        assert validate_experience(None, ["Glovo"], config).reason == "missing"

    def test_platforms_are_normalized(self, config: ClientConfig) -> None:
        platforms = normalize_platforms(
            ["glovo", "uber", "Mi tío", "GLOVO"], config.known_platforms
        )
        assert platforms == ["Glovo", "Uber Eats", "other"]


class TestStartDate:
    def test_past_date_is_invalid(self, config: ClientConfig) -> None:
        outcome = validate_start_date(TODAY - timedelta(days=1), TODAY, config)
        assert (outcome.kind, outcome.reason) == ("invalid", "in_past")

    def test_far_date_is_valid_but_flagged(self, config: ClientConfig) -> None:
        outcome = validate_start_date(TODAY + timedelta(days=90), TODAY, config)
        assert (outcome.kind, outcome.note) == ("valid", "start_date_far")

    def test_missing(self, config: ClientConfig) -> None:
        assert validate_start_date(None, TODAY, config).reason == "missing"

    @given(st.integers(min_value=0, max_value=60))
    def test_today_or_later_within_window_is_valid(self, days: int) -> None:
        # Hypothesis can't use function-scoped fixtures, so this test uses the module config.
        outcome = validate_start_date(TODAY + timedelta(days=days), TODAY, CONFIG)
        assert (outcome.kind, outcome.note) == ("valid", None)


class TestFinalReviewReasons:
    def _candidate(self, location_id: str, license_type: str, years: float | None) -> Candidate:
        return Candidate(
            vehicle=Vehicle.MOTO,
            location_id=location_id,
            license_type=license_type,
            license_years=years,
        )

    def test_spanish_car_license_under_3_years_for_moto(self, config: ClientConfig) -> None:
        candidate = self._candidate("es-madrid", "B", 2)
        assert final_review_reasons(candidate, config) == ["license_unclear"]

    def test_spanish_car_license_over_3_years_for_moto(self, config: ClientConfig) -> None:
        assert final_review_reasons(self._candidate("es-madrid", "B", 4), config) == []

    def test_moto_license_is_fine(self, config: ClientConfig) -> None:
        assert final_review_reasons(self._candidate("es-madrid", "a2", None), config) == []

    def test_no_rules_for_mexico(self, config: ClientConfig) -> None:
        assert final_review_reasons(self._candidate("mx-cdmx", "B", 1), config) == []


def test_normalize() -> None:
    assert normalize("  Álvaro   OBREGÓN ") == "alvaro obregon"
