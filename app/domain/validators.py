"""Field validators: pure functions that turn proposed values into an outcome.

Rules come from `docs/process-design.md` (fields table) and are parameterised by the client
config. The LLM proposes values; these functions decide whether they are acceptable.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from app.domain.config import ClientConfig, ServiceLocation
from app.domain.models import (
    Availability,
    Candidate,
    GeoLookup,
    GeoPlace,
    InvalidReason,
    Reason,
    Schedule,
    Vehicle,
)

OutcomeKind = Literal["valid", "invalid", "knockout", "review"]

MAX_EXPERIENCE_YEARS = 60


@dataclass(frozen=True)
class Outcome:
    kind: OutcomeKind
    reason: Reason | InvalidReason | None = None
    updates: dict[str, object] = field(default_factory=dict)
    note: str | None = None

    @property
    def verdict(self) -> Reason:
        """The knockout or review reason. Only those outcomes have one."""
        if not isinstance(self.reason, Reason):
            raise ValueError(f"a {self.kind} outcome has no knockout or review reason")
        return self.reason

    @property
    def problem(self) -> InvalidReason:
        """Why an answer was not accepted. Only invalid outcomes have one."""
        if self.kind != "invalid" or self.reason is None or isinstance(self.reason, Reason):
            raise ValueError(f"a {self.kind} outcome has no invalid-answer reason")
        return self.reason

    @staticmethod
    def valid(note: str | None = None, **updates: object) -> Outcome:
        return Outcome("valid", updates=updates, note=note)

    @staticmethod
    def invalid(reason: InvalidReason, **partial: object) -> Outcome:
        return Outcome("invalid", reason=reason, updates=partial)

    @staticmethod
    def knockout(reason: Reason) -> Outcome:
        return Outcome("knockout", reason=reason)

    @staticmethod
    def review(reason: Reason, **updates: object) -> Outcome:
        return Outcome("review", reason=reason, updates=updates)


ZONE_READBACK = "zone_readback"  # note on a location whose city came from the config
COUNTRY_READBACK = "country_readback"  # note on a city another country also has


def normalize(text: str) -> str:
    """Lowercase, strip accents and collapse whitespace: 'Álvaro  Obregón' -> 'alvaro obregon'."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.lower().split())


_NAME_PARTICLES = frozenset({"de", "del", "la", "las", "los", "y", "da", "van", "von"})
_NAME_TOKEN = re.compile(r"[^\W\d_]+(?:[-'][^\W\d_]+)*", re.UNICODE)


def validate_full_name(full_name: str | None) -> Outcome:
    if not full_name or not full_name.strip():
        return Outcome.invalid("missing")
    tokens = _NAME_TOKEN.findall(full_name)
    if not tokens:
        return Outcome.invalid("not_a_name")
    if all(t == t.lower() for t in tokens):  # speech-to-text and quick typing drop capitals
        tokens = [t if t in _NAME_PARTICLES else t.capitalize() for t in tokens]
    cleaned = " ".join(tokens)
    if len(tokens) < 2:
        return Outcome.invalid("surname_missing", full_name=cleaned)
    return Outcome.valid(full_name=cleaned)


# A license category as written on the card: AM, A1, A2, A, B, C1, BE... (a word like "moto" or
# "coche" is a vehicle, not a category, and would wrongly fail the per-country license rules).
_LICENSE_CODE = re.compile(r"[A-Z]{1,2}[0-9]?(\+?E)?")
_LICENSE_WORDS = re.compile(r"^(tipo|clase|categoria|carnet|licencia|permiso|de)\s+")


def license_code(license_type: str | None) -> str | None:
    """'B' -> 'B', 'tipo A2' -> 'A2', 'moto' -> None."""
    if not license_type:
        return None
    text = normalize(license_type)
    while (stripped := _LICENSE_WORDS.sub("", text)) != text:
        text = stripped
    code = text.upper().replace(" ", "")
    return code if _LICENSE_CODE.fullmatch(code) else None


def validate_license(
    has_license: bool | None, vehicle: Vehicle | None, license_type: str | None = None
) -> Outcome:
    if has_license is None and vehicle is not None:
        has_license = True  # answering "Moto" / "Coche" / "Ambos" is a yes
    if has_license is None:
        return Outcome.invalid("missing")
    if not has_license:
        return Outcome.knockout(Reason.NO_LICENSE)
    if vehicle is None:
        return Outcome.invalid("vehicle_missing", has_license=True)
    if license_type is not None:
        return Outcome.valid(
            has_license=True, vehicle=vehicle, license_type=license_code(license_type)
        )
    return Outcome.valid(has_license=True, vehicle=vehicle)


def _match_city(city: str, locations: list[ServiceLocation]) -> ServiceLocation | None:
    wanted = normalize(city)
    for loc in locations:
        names = [loc.city, *loc.aliases]
        if any(normalize(name) == wanted for name in names):
            return loc
    return None


def _is_nearby(place: str, locations: list[ServiceLocation]) -> ServiceLocation | None:
    wanted = normalize(place)
    for loc in locations:
        if any(normalize(name) == wanted for name in loc.nearby):
            return loc
    return None


def is_known_place(place: str, config: ClientConfig) -> bool:
    """A served city (or alias), one of its zones, or a listed nearby town: no lookup needed."""
    wanted = normalize(place)
    return any(
        wanted in {normalize(n) for n in [loc.city, *loc.aliases, *loc.zones, *loc.nearby]}
        for loc in config.locations
    )


# Answers spelled the same in Spanish and English: on their own they don't switch the language.
_SAME_IN_BOTH = frozenset({"ok", "okay", "flexible", "no", "moto", "normal", "total"})


def says_no_language(text: str, config: ClientConfig, names: list[str]) -> bool:
    """The message is only proper names (served places, platforms, the candidate's own name),
    numbers and words spelled the same in both languages, e.g. "madrid tetuan", "Glovo, 2" or
    "flexible": it can't tell which language they're using."""

    def words(t: str) -> str:
        return " ".join(re.sub(r"[^a-z0-9]+", " ", normalize(t)).split())

    rest = f" {words(text)} "
    known = [
        *(n for loc in config.locations for n in [loc.city, *loc.aliases, *loc.zones, *loc.nearby]),
        *config.known_platforms,
        *names,
    ]
    for name in sorted({words(n) for n in known if n}, key=len, reverse=True):
        rest = rest.replace(f" {name} ", " ")
    return all(word.isdigit() or word in _SAME_IN_BOTH for word in rest.split())


_ZONE_PREFIX = re.compile(r"^(barrio|colonia|col|distrito|el|la|los|las|l|the)\b\s*")


def _zone_key(zone: str) -> str:
    """A zone without the words around its name: 'La Condesa', 'colonia Condesa' -> 'condesa'."""
    text = " ".join(re.sub(r"[^a-z0-9]+", " ", normalize(zone)).split())
    while (stripped := _ZONE_PREFIX.sub("", text)) != text:
        text = stripped
    return text or normalize(zone)


def _is_listed_zone(place: str, locations: list[ServiceLocation]) -> bool:
    wanted = _zone_key(place)
    return any(wanted == _zone_key(zone) for loc in locations for zone in loc.zones)


def _zone_alone(zone: str, config: ClientConfig) -> Outcome:
    """A neighbourhood with no city. The config knows the city if only one has that name, and the
    candidate confirms it (note ZONE_READBACK). Otherwise keep it: "vivo en Sants" then "en
    Barcelona" gives the whole location."""
    wanted = _zone_key(zone)
    owners = [loc for loc in config.locations if any(wanted == _zone_key(z) for z in loc.zones)]
    if len(owners) == 1:
        return Outcome.valid(
            note=ZONE_READBACK, location_id=owners[0].id, city=owners[0].city, zone=zone
        )
    return Outcome.invalid("missing", zone=zone)


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def nearest_location(place: GeoPlace, config: ClientConfig) -> tuple[ServiceLocation, float] | None:
    """The served city closest to a geocoded place, and how far it is."""
    found = [
        (loc, distance_km(place.lat, place.lon, loc.lat, loc.lon))
        for loc in config.locations
        if loc.lat is not None and loc.lon is not None
    ]
    return min(found, key=lambda pair: pair[1]) if found else None


def _geocoded(city: str, zone: str | None, geo: GeoLookup, config: ClientConfig) -> Outcome:
    """A place the config doesn't list, decided by distance instead of an automatic knockout.
    Every geocoded outcome is confirmed with the candidate first (note "geocoded")."""
    if geo.failed:  # the lookup didn't work: a recruiter decides, nobody is rejected for it
        return Outcome.review(Reason.LOCATION_UNVERIFIED, city=city, zone=zone)
    if geo.found is None:  # no such place: ask again (a typo, or not a place at all)
        return Outcome.invalid("unclear")
    nearest = nearest_location(geo.found, config)
    if nearest is not None and nearest[1] <= config.geo_review_radius_km:
        return Outcome(
            "review",
            reason=Reason.BORDERLINE_ZONE,
            updates={"city": geo.found.name, "zone": zone},
            note="geocoded",
        )
    return Outcome(
        "knockout", reason=Reason.OUTSIDE_AREA, updates={"city": geo.found.name}, note="geocoded"
    )


def _digits(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


def _served(location: ServiceLocation, zone: str | None) -> Outcome:
    """A served city or zone. The name alone can't tell Guadalajara, Spain from Guadalajara,
    Mexico, so the candidate confirms the country of a city another country also has."""
    return Outcome.valid(
        note=COUNTRY_READBACK if location.shared_name else None,
        location_id=location.id,
        city=location.city,
        zone=zone,
    )


def validate_location(
    city: str | None,
    zone: str | None,
    postal_code: str | None,
    config: ClientConfig,
    geo: GeoLookup | None = None,
) -> Outcome:
    """`geo` is the geocoder's answer for a city (or zone given alone) the config doesn't
    list. Without it, an unlisted city is outside the area."""
    # The city comes first: Spanish and Mexican postal codes overlap (e.g. 06xxx is Badajoz and
    # also Mexico City), so a postal code alone never decides the location.
    pc = _digits(postal_code) if postal_code else ""
    if city:
        served_city = _match_city(city, config.locations)
        if served_city is not None:
            if pc and any(pc.startswith(p) for p in served_city.postal_prefixes):
                return Outcome.valid(
                    location_id=served_city.id, city=served_city.city, postal_code=pc, zone=zone
                )
            if not served_city.zones:
                if (
                    pc
                ):  # the code belongs elsewhere: maybe a city of the same name in another country
                    return Outcome.review(
                        Reason.POSTAL_CODE_NOT_LISTED,
                        city=served_city.city,
                        postal_code=pc,
                        zone=zone,
                    )
                return _served(served_city, zone)
            if zone:
                listed = {_zone_key(z) for z in served_city.zones}
                if _zone_key(zone) in listed:  # "La Condesa" is the listed "Condesa"
                    return _served(served_city, zone)
                if _is_nearby(zone, [served_city]):
                    return Outcome.review(Reason.BORDERLINE_ZONE, city=served_city.city, zone=zone)
                return Outcome.review(Reason.ZONE_NOT_LISTED, city=served_city.city, zone=zone)
            if pc:
                return Outcome.review(
                    Reason.POSTAL_CODE_NOT_LISTED, city=served_city.city, postal_code=pc
                )
            return Outcome.invalid("zone_missing", city=served_city.city)
        nearby = _is_nearby(city, config.locations)
        if nearby is not None:
            return Outcome.review(Reason.BORDERLINE_ZONE, city=city, zone=zone)
        if _is_listed_zone(city, config.locations):
            # "vivo en Tetuán" read as a city: a neighbourhood we serve rejects nobody. Someone
            # really from a city named like one says so at the read-back, and goes to review.
            return _zone_alone(zone or city, config)
        if geo is not None and normalize(geo.place) == normalize(city):
            return _geocoded(city, zone, geo, config)
        return Outcome.knockout(Reason.OUTSIDE_AREA)

    if zone and (_match_city(zone, config.locations) or _is_nearby(zone, config.locations)):
        # A town given as the zone, with no city ("Getafe" read as a Madrid neighbourhood): it is
        # the city. Only names from the config, so a real neighbourhood still asks for the city.
        return validate_location(zone, None, postal_code, config)
    if zone and geo is not None and normalize(geo.place) == normalize(zone):
        # An unlisted place given alone: look it up like a city ("Móstoles", "Vallecas").
        return _geocoded(zone, None, geo, config)
    if pc:
        return Outcome.invalid("city_missing", postal_code=pc)
    if zone:
        return _zone_alone(zone, config)
    return Outcome.invalid("missing")


def validate_availability(availability: list[Availability]) -> Outcome:
    if not availability:
        return Outcome.invalid("missing")
    return Outcome.valid(availability=sorted(set(availability)))


def validate_schedule(schedule: list[Schedule]) -> Outcome:
    if not schedule:
        return Outcome.invalid("missing")
    return Outcome.valid(schedule=sorted(set(schedule)))


def normalize_platforms(platforms: list[str], known: list[str]) -> list[str]:
    result: list[str] = []
    for raw in platforms:
        name = normalize(raw)
        match = next((k for k in known if normalize(k) in name or name in normalize(k)), None)
        value = match if match is not None else "other"
        if name and value not in result:
            result.append(value)
    return result


def validate_experience(
    experience_years: float | None, platforms: list[str], config: ClientConfig
) -> Outcome:
    if experience_years is None:
        return Outcome.invalid("missing")
    if experience_years < 0 or experience_years > MAX_EXPERIENCE_YEARS:
        return Outcome.invalid("out_of_range")
    return Outcome.valid(
        experience_years=experience_years,
        platforms=normalize_platforms(platforms, config.known_platforms),
    )


def validate_start_date(start_date: date | None, today: date, config: ClientConfig) -> Outcome:
    if start_date is None:
        return Outcome.invalid("missing")
    if start_date < today:
        return Outcome.invalid("in_past")
    if (start_date - today).days > config.far_start_days:
        return Outcome.valid(note="start_date_far", start_date=start_date)
    return Outcome.valid(start_date=start_date)


def final_review_reasons(candidate: Candidate, config: ClientConfig) -> list[Reason]:
    """Checks that need several fields at once, run before qualifying (e.g. license by country)."""
    reasons: list[Reason] = []
    loc = config.location(candidate.location_id) if candidate.location_id else None
    rules = config.license_rules.get(loc.country) if loc else None
    rides_moto = candidate.vehicle in (Vehicle.MOTO, Vehicle.BOTH)
    # A country with no moto rules (e.g. Mexico, where categories vary by state) gets no check.
    if rules is None or not (rules.accepted_for_moto or rules.conditional):
        return reasons
    if rides_moto and candidate.license_type:
        license_type = candidate.license_type.strip().upper()
        if license_type not in {t.upper() for t in rules.accepted_for_moto}:
            conditional = next(
                (
                    r
                    for r in rules.conditional
                    if r.vehicle == Vehicle.MOTO and r.license_type.upper() == license_type
                ),
                None,
            )
            years = candidate.license_years
            if conditional is None or years is None or years < conditional.min_years:
                reasons.append(Reason.LICENSE_UNCLEAR)
    return reasons
