"""Contract tests: the Nominatim adapter and its fake answer the same way."""

import json

import httpx
import pytest

from app.adapters.geo.fake import FakeGeocoder
from app.adapters.geo.nominatim import NominatimGeocoder
from app.domain.models import GeoPlace

MOSTOLES_JSON = [
    {
        "lat": "40.3223",
        "lon": "-3.8649",
        "name": "Móstoles",
        "address": {"town": "Móstoles", "state": "Comunidad de Madrid", "country_code": "es"},
    }
]


class FakeNominatim:
    """Answers like the Nominatim search API for Móstoles, nothing for anything else."""

    def __init__(self, status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status)
        found = request.url.params["q"].lower() == "móstoles"
        return httpx.Response(200, content=json.dumps(MOSTOLES_JSON if found else []))


def nominatim(server: FakeNominatim, sleeps: list[float] | None = None) -> NominatimGeocoder:
    return NominatimGeocoder(
        "sazon-tests (dev@example.com)",
        transport=httpx.MockTransport(server),
        monotonic=lambda: 0.0,
        sleep=(sleeps.append if sleeps is not None else lambda _: None),
    )


@pytest.fixture(params=["nominatim", "fake"])
def geocoder(request: pytest.FixtureRequest) -> NominatimGeocoder | FakeGeocoder:
    return nominatim(FakeNominatim()) if request.param == "nominatim" else FakeGeocoder()


def test_finds_a_known_town(geocoder: NominatimGeocoder | FakeGeocoder) -> None:
    place = geocoder.locate("Móstoles", ["ES", "MX"])
    assert isinstance(place, GeoPlace)
    assert (place.name, place.region, place.country_code) == (
        "Móstoles",
        "Comunidad de Madrid",
        "ES",
    )
    assert place.label() == "Móstoles (Comunidad de Madrid)"
    assert 40 < place.lat < 41


def test_unknown_place_is_none(geocoder: NominatimGeocoder | FakeGeocoder) -> None:
    assert geocoder.locate("Narnia", ["ES", "MX"]) is None


def test_failures_raise() -> None:
    with pytest.raises(httpx.HTTPStatusError):
        nominatim(FakeNominatim(status=503)).locate("Móstoles", ["ES"])
    fake = FakeGeocoder(fail=True)
    with pytest.raises(ConnectionError):
        fake.locate("Móstoles", ["ES"])


def test_fake_respects_the_countries() -> None:
    assert FakeGeocoder().locate("Móstoles", ["MX"]) is None


def test_nominatim_follows_the_usage_policy() -> None:
    server, sleeps = FakeNominatim(), []
    geo = nominatim(server, sleeps)
    geo.locate("Móstoles", ["ES", "MX"])
    geo.locate("Narnia", ["ES", "MX"])
    geo.locate("móstoles", ["MX", "ES"])  # cached: same place, same countries
    assert len(server.requests) == 2
    assert server.requests[0].headers["User-Agent"] == "sazon-tests (dev@example.com)"
    assert server.requests[0].url.params["countrycodes"] == "es,mx"
    assert sleeps == [1.0]  # the second request waited (max 1 per second)


def test_nominatim_needs_a_user_agent() -> None:
    with pytest.raises(ValueError, match="NOMINATIM_USER_AGENT"):
        NominatimGeocoder(" ")
