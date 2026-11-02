"""A geocoder with a fixed list of places, for tests and offline demos."""

from __future__ import annotations

from app.domain.models import GeoPlace
from app.domain.validators import normalize

# A few real towns around served cities (and some far away), so fake mode behaves plausibly.
DEMO_PLACES: dict[str, GeoPlace] = {
    "mostoles": GeoPlace(
        name="Móstoles", region="Comunidad de Madrid", country_code="ES", lat=40.3223, lon=-3.8649
    ),
    "toledo": GeoPlace(
        name="Toledo", region="Castilla-La Mancha", country_code="ES", lat=39.8628, lon=-4.0273
    ),
    "sabadell": GeoPlace(
        name="Sabadell", region="Cataluña", country_code="ES", lat=41.5433, lon=2.1094
    ),
    "bilbao": GeoPlace(
        name="Bilbao", region="País Vasco", country_code="ES", lat=43.2630, lon=-2.9350
    ),
    "ecatepec": GeoPlace(
        name="Ecatepec de Morelos",
        region="Estado de México",
        country_code="MX",
        lat=19.6012,
        lon=-99.0507,
    ),
}


class FakeGeocoder:
    def __init__(self, places: dict[str, GeoPlace] | None = None, fail: bool = False) -> None:
        self._places = {normalize(k): v for k, v in (places or DEMO_PLACES).items()}
        self.fail = fail
        self.asked: list[str] = []

    def locate(self, place: str, country_codes: list[str]) -> GeoPlace | None:
        self.asked.append(place)
        if self.fail:
            raise ConnectionError("geocoder unavailable")
        found = self._places.get(normalize(place))
        if found is None or found.country_code not in {c.upper() for c in country_codes}:
            return None
        return found
