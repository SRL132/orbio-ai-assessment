from datetime import date, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.adapters.config.yaml_loader import load_client_config
from app.domain.config import ClientConfig
from app.domain.models import Availability, Candidate, GeoLookup, GeoPlace, Schedule, Vehicle
from app.domain.validators import (
    ZONE_READBACK,
    distance_km,
    final_review_reasons,
    is_known_place,
    license_code,
    nearest_location,
    normalize,
    normalize_platforms,
    same_place,
    says_no_language,
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


MOSTOLES = GeoPlace(
    name="Móstoles", region="Comunidad de Madrid", country_code="ES", lat=40.3223, lon=-3.8649
)


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

    def test_nearby_town_given_as_the_zone_is_the_city(self, config: ClientConfig) -> None:
        # Some models read "Getafe" as a Madrid neighbourhood: still a borderline town, not a
        # missing city.
        outcome = validate_location(None, "Getafe", None, config)
        assert (outcome.kind, outcome.reason) == ("review", "borderline_zone")
        assert outcome.updates == {"city": "Getafe", "zone": None}

    def test_served_city_given_as_the_zone_asks_for_the_zone(self, config: ClientConfig) -> None:
        outcome = validate_location(None, "Madrid", None, config)
        assert (outcome.kind, outcome.reason) == ("invalid", "zone_missing")
        assert outcome.updates == {"city": "Madrid"}

    def test_a_neighbourhood_alone_is_read_back_with_its_city(self, config: ClientConfig) -> None:
        outcome = validate_location(None, "Chamberí", None, config)
        assert (outcome.kind, outcome.note) == ("valid", ZONE_READBACK)
        assert outcome.updates == {"location_id": "es-madrid", "city": "Madrid", "zone": "Chamberí"}

    def test_geocoded_town_within_the_radius_goes_to_review(self, config: ClientConfig) -> None:
        geo = GeoLookup(place="Móstoles", found=MOSTOLES)
        outcome = validate_location("Móstoles", None, None, config, geo)
        assert (outcome.kind, outcome.reason, outcome.note) == (
            "review",
            "borderline_zone",
            "geocoded",
        )
        assert outcome.updates["city"] == "Móstoles"

    def test_geocoded_town_beyond_the_radius_is_outside(self, config: ClientConfig) -> None:
        bilbao = GeoPlace(name="Bilbao", country_code="ES", lat=43.263, lon=-2.935)
        outcome = validate_location(
            "Bilbao", None, None, config, GeoLookup(place="Bilbao", found=bilbao)
        )
        assert (outcome.kind, outcome.reason, outcome.note) == (
            "knockout",
            "outside_area",
            "geocoded",
        )

    def test_a_place_the_geocoder_doesnt_know_is_asked_again(self, config: ClientConfig) -> None:
        outcome = validate_location("Narnia", None, None, config, GeoLookup(place="Narnia"))
        assert (outcome.kind, outcome.reason) == ("invalid", "unclear")

    def test_a_failed_lookup_goes_to_review(self, config: ClientConfig) -> None:
        geo = GeoLookup(place="Móstoles", failed=True)
        outcome = validate_location("Móstoles", None, None, config, geo)
        assert (outcome.kind, outcome.reason) == ("review", "location_unverified")

    def test_an_unlisted_place_given_as_zone_is_looked_up(self, config: ClientConfig) -> None:
        geo = GeoLookup(place="Móstoles", found=MOSTOLES)
        outcome = validate_location(None, "Móstoles", None, config, geo)
        assert (outcome.kind, outcome.reason) == ("review", "borderline_zone")

    def test_a_lookup_for_another_place_is_ignored(self, config: ClientConfig) -> None:
        geo = GeoLookup(place="Móstoles", found=MOSTOLES)
        assert validate_location("Bilbao", None, None, config, geo).kind == "knockout"

    def test_distances(self, config: ClientConfig) -> None:
        assert 495 < distance_km(40.4168, -3.7038, 41.3874, 2.1686) < 515  # Madrid-Barcelona
        nearest = nearest_location(MOSTOLES, config)
        assert nearest is not None
        assert nearest[0].id == "es-madrid"
        assert 15 < nearest[1] < 22

    def test_known_places(self, config: ClientConfig) -> None:
        assert all(is_known_place(p, config) for p in ["madrid", "CDMX", "Chamberí", "Getafe"])
        assert not is_known_place("Móstoles", config)

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


def test_choosing_a_vehicle_means_having_a_license() -> None:
    # Found by the live evals: models extract "Moto" as the vehicle without has_license.
    outcome = validate_license(None, Vehicle.MOTO)
    assert outcome.kind == "valid"
    assert outcome.updates == {"has_license": True, "vehicle": Vehicle.MOTO}


@pytest.mark.parametrize(
    ("written", "code"),
    [
        ("B", "B"),
        ("b", "B"),
        ("A2", "A2"),
        ("tipo A", "A"),
        ("clase B", "B"),
        ("BE", "BE"),
        ("AM", "AM"),
        ("moto", None),
        ("coche", None),
        ("carnet de moto", None),
        ("", None),
    ],
)
def test_license_type_is_kept_only_as_a_license_code(written: str, code: str | None) -> None:
    assert license_code(written) == code
    outcome = validate_license(True, Vehicle.MOTO, written)
    assert outcome.updates["license_type"] == code


@pytest.mark.parametrize(
    ("text", "neutral"),
    [
        ("madrid tetuan", True),
        ("Roma, CDMX", True),
        ("L'Hospitalet de Llobregat", True),
        ("Glovo, 2", True),
        ("Marta Ruiz", True),
        ("28020", True),
        ("flexible", True),  # same word in both languages
        ("vivo en madrid", False),
        ("i live in madrid", False),
        ("sí", False),
        ("Pedro", False),  # not the candidate's name: could be anything
    ],
)
def test_names_and_numbers_say_no_language(text: str, neutral: bool) -> None:
    assert says_no_language(text, CONFIG, ["Marta", "Ruiz"]) is neutral


@pytest.mark.parametrize("said", ["La Condesa", "colonia Condesa", "condesa", "la  condesa"])
def test_listed_zone_matches_with_an_article_or_prefix(said: str) -> None:
    outcome = validate_location("CDMX", said, None, CONFIG)
    assert outcome.kind == "valid"
    assert outcome.updates["location_id"] == "mx-cdmx"


def test_an_unlisted_zone_alone_is_kept_until_the_city_arrives(config: ClientConfig) -> None:
    outcome = validate_location(None, "Poblenou", None, config)
    assert outcome.kind == "invalid"
    assert outcome.problem == "missing"
    assert outcome.updates == {"zone": "Poblenou"}
    assert validate_location(None, None, None, config).updates == {}


def test_a_zone_two_cities_list_asks_for_the_city_instead_of_guessing(
    config: ClientConfig,
) -> None:
    locations = [
        loc.model_copy(update={"zones": [*loc.zones, "Sants"]}) if loc.id == "es-madrid" else loc
        for loc in config.locations
    ]
    both = config.model_copy(update={"locations": locations})
    for city, zone in ((None, "Sants"), ("Sants", None)):
        outcome = validate_location(city, zone, None, both)
        assert outcome.kind == "invalid"
        assert outcome.updates == {"zone": "Sants"}


@pytest.mark.parametrize(
    ("said", "kept"), [("Tetuán", "Tetuán"), ("tetuan", "tetuan"), ("La Condesa", "La Condesa")]
)
def test_a_listed_zone_read_as_a_city_is_kept_and_rejects_nobody(
    config: ClientConfig, said: str, kept: str
) -> None:
    outcome = validate_location(said, None, None, config)
    assert (outcome.kind, outcome.note) == ("valid", ZONE_READBACK)
    assert outcome.updates["zone"] == kept


def test_a_listed_zone_read_as_a_city_keeps_a_zone_already_given(config: ClientConfig) -> None:
    assert validate_location("Salamanca", "Centro", None, config).updates["zone"] == "Centro"


def test_a_served_city_named_like_a_zone_is_still_the_city(config: ClientConfig) -> None:
    assert validate_location("Madrid", "Tetuán", None, config).kind == "valid"


@pytest.mark.parametrize(
    ("one", "other", "same"),
    [
        ("La Condesa", "condesa", True),
        ("Tetuán", "tetuan", True),
        ("Tetuán", "Retiro", False),
    ],
)
def test_same_place_ignores_the_words_around_a_name(one: str, other: str, same: bool) -> None:
    assert same_place(one, other) is same
