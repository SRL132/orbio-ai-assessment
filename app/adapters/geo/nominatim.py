"""Geocoding with OpenStreetMap's Nominatim (https://nominatim.org/release-docs/latest/api/Search/).

Used only for places the client config doesn't list, so a town just outside a served city is
measured instead of rejected. Only the place name is sent, never who asked, and nothing is
logged. The public server's usage policy applies: at most one request per second and an
identifying User-Agent (NOMINATIM_USER_AGENT). Answers are cached in memory, since the same
towns come up again and again. At high volume, point NOMINATIM_URL at a self-hosted instance.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import httpx

from app.domain.models import GeoPlace
from app.domain.validators import normalize

BASE_URL = "https://nominatim.openstreetmap.org"
# Most specific first: what a candidate would call the place they live in.
_NAME_KEYS = ("city", "town", "village", "municipality", "suburb", "city_district", "county")


class NominatimGeocoder:
    def __init__(
        self,
        user_agent: str,
        base_url: str = BASE_URL,
        timeout: float = 5.0,
        min_interval: float = 1.0,
        transport: httpx.BaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not user_agent.strip():
            raise ValueError("NOMINATIM_USER_AGENT is not set (Nominatim's usage policy)")
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": user_agent},
        )
        self._min_interval = min_interval
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_call: float | None = None
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, tuple[str, ...]], GeoPlace | None] = {}

    def locate(self, place: str, country_codes: list[str]) -> GeoPlace | None:
        key = (normalize(place), tuple(sorted(c.lower() for c in country_codes)))
        with self._lock:  # one request at a time per process, spaced out (usage policy)
            if key in self._cache:
                return self._cache[key]
            if self._last_call is not None:
                wait = self._min_interval - (self._monotonic() - self._last_call)
                if wait > 0:
                    self._sleep(wait)
            try:
                response = self._client.get(
                    "/search",
                    params={
                        "q": place,
                        "countrycodes": ",".join(key[1]),
                        "format": "jsonv2",
                        "addressdetails": 1,
                        "limit": 1,
                        "accept-language": "es",
                    },
                )
            finally:
                self._last_call = self._monotonic()
            response.raise_for_status()
            found = _parse(response.json())
            self._cache[key] = found
            return found


def _parse(results: object) -> GeoPlace | None:
    if not isinstance(results, list) or not results:
        return None
    first = results[0]
    address = first.get("address") or {}
    name = next((address[k] for k in _NAME_KEYS if address.get(k)), None) or first.get("name")
    if not name:
        return None
    return GeoPlace(
        name=name,
        region=address.get("state"),
        country_code=str(address.get("country_code", "")).upper(),
        lat=float(first["lat"]),
        lon=float(first["lon"]),
    )
