"""Travelpayouts cheap-fares (Aviasales Data API v3).

Secondary source, and effectively unmetered, so it costs nothing to leave on.

Important limitation, verified against the v3 prices endpoints: this API returns
*cached one-adult fares* and accepts no passenger parameters. It therefore emits
quotes with adults=1, children=0, infants=0 and never a fabricated party total --
multiplying a single fare by the party size is precisely the error that fare
buckets make wrong. SerpAPI is the source of truth for real party pricing.

What this is not, settled 2026-09-08: a fallback, or the free half of the
split-booking comparison. Both were the intent -- an unmetered source already
priced per adult is exactly what that comparison wants, which is why main.py
asks this fetcher for the party query only and never a second single-adult one --
and neither survived contact with these routes. The cache is built from real
Aviasales searches and holds nothing for DSM-STT or DSM-SJU, so it contributes no
fares at all, and the single-adult side is bought from SerpAPI like the party
side.

It stays enabled because it is free and the cache may fill as departure nears,
in which case fares arrive through the weekly run with no work here. The evidence
is in src/tp_probe.py and the README's *Budget* section; whether it has changed
is in data/source_probe.json.
"""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta

import requests

from ..config import Route
from ..models import Passengers, Quote, hash_payload, utcnow
from .base import Fetcher, FetcherError

log = logging.getLogger(__name__)

ENDPOINT = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
BOOKING_HOST = "https://www.aviasales.com"
TIMEOUT = 30

# The endpoint serves a cache of real Aviasales user searches and filters it by
# market. None means "send no market parameter", which is what production has
# always done. src/tp_probe.py exists to find out whether that is why these
# routes come back empty; if it is, this constant is the fix.
DEFAULT_MARKET: str | None = None


class TravelpayoutsFetcher(Fetcher):
    name = "travelpayouts"

    def __init__(
        self, config, token: str | None = None, session=None, market: str | None = DEFAULT_MARKET
    ) -> None:
        super().__init__(config)
        self.token = token if token is not None else os.environ.get("TRAVELPAYOUTS_TOKEN", "")
        self.session = session or requests.Session()
        self.market = market

    def searches_consumed(self) -> int:
        return self._searches

    def search(self, route: Route, passengers: Passengers) -> list[Quote]:
        """`passengers` is accepted for interface parity; this API prices one adult."""
        if not self.token:
            raise FetcherError("TRAVELPAYOUTS_TOKEN is not set")
        quotes: list[Quote] = []
        for departure_at in self._departure_keys(route):
            payload = self._call(route, departure_at)
            quotes.extend(self._parse(payload, route))
        return quotes

    def estimate_searches(self, route: Route) -> int:
        return len(self._departure_keys(route))

    def _departure_keys(self, route: Route) -> list[str]:
        """Fixed routes ask for one date; windowed routes ask per month (one call each)."""
        if route.depart is not None:
            return [route.depart.isoformat()]
        window = route.depart_window
        assert window is not None
        months: list[str] = []
        cursor = window.earliest.replace(day=1)
        while cursor <= window.latest:
            months.append(cursor.strftime("%Y-%m"))
            cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        return months

    # --- HTTP -------------------------------------------------------------

    def _call(self, route: Route, departure_at: str) -> dict:
        return self.request(
            origin=route.origin,
            destination=route.destination,
            departure_at=departure_at,
            currency=self.config.currency_for(route).lower(),
            one_way=not self._is_round_trip(route),
            return_at=(
                route.return_.isoformat()
                if route.depart is not None and route.return_ is not None
                else None
            ),
        )

    def request(
        self,
        *,
        origin: str,
        destination: str,
        departure_at: str,
        currency: str,
        one_way: bool,
        return_at: str | None = None,
    ) -> dict:
        """One call to the prices endpoint, for any pair of airports.

        Public because the coverage probe has to send the query this fetcher
        actually sends. A probe that rebuilt the params itself would drift from
        production and then answer a question nobody asked.
        """
        params = {
            "origin": origin,
            "destination": destination,
            "departure_at": departure_at,
            "currency": currency,
            "sorting": "price",
            "limit": 30,
            "one_way": "true" if one_way else "false",
            "token": self.token,
        }
        if self.market:
            params["market"] = self.market
        if return_at:
            params["return_at"] = return_at

        response = self.session.get(ENDPOINT, params=params, timeout=TIMEOUT)
        self._count_search()
        if response.status_code != 200:
            raise FetcherError(f"travelpayouts HTTP {response.status_code}: {response.text[:200]}")
        body = response.json()
        if not body.get("success", False):
            raise FetcherError(f"travelpayouts rejected the query: {body.get('error') or body}")
        return body

    @staticmethod
    def _is_round_trip(route: Route) -> bool:
        return route.return_ is not None or route.nights is not None

    # --- parsing ----------------------------------------------------------

    def _parse(self, payload: dict, route: Route) -> list[Quote]:
        data = self._require(payload, "data", f"response for {route.id}")
        if not isinstance(data, list):
            raise FetcherError(f"travelpayouts: 'data' is {type(data).__name__}, expected a list")
        currency = (payload.get("currency") or self.config.currency_for(route)).upper()
        self._expect_currency(route, currency)

        digest = hash_payload(payload)
        observed_at = utcnow()
        single = Passengers.single_adult()
        quotes: list[Quote] = []

        for entry in data:
            depart = _as_date(self._require(entry, "departure_at", f"fare for {route.id}"))
            return_date = _as_date(entry.get("return_at")) if entry.get("return_at") else None
            if not self._in_scope(route, depart, return_date):
                continue
            price = self._require(entry, "price", f"fare for {route.id}")
            if not isinstance(price, (int, float)):
                raise FetcherError(f"travelpayouts: non-numeric price {price!r} for {route.id}")
            link = entry.get("link")
            quotes.append(
                Quote(
                    route_id=route.id,
                    source=self.name,
                    observed_at=observed_at,
                    depart_date=depart,
                    return_date=return_date,
                    adults=single.adults,
                    children=single.children,
                    infants=single.infants,
                    total_price=float(price),
                    price_per_adult=float(price),
                    currency=currency,
                    carrier=entry.get("airline"),
                    stops=entry.get("transfers"),
                    booking_url=f"{BOOKING_HOST}{link}" if link else None,
                    raw_response_hash=digest,
                )
            )

        if not quotes:
            # Distinguish an empty cache from an over-strict filter: the first is
            # the API having no fares, the second is our own bug.
            if not data:
                log.warning(
                    "travelpayouts: %s returned no cached fares at all for %s",
                    route.origin + "-" + route.destination, route.id,
                )
            else:
                sample = ", ".join(
                    f"{str(e.get('departure_at'))[:10]}->{str(e.get('return_at'))[:10]}"
                    for e in data[:5]
                )
                log.warning(
                    "travelpayouts: %s fares returned for %s but none in scope; got %s",
                    len(data), route.id, sample,
                )
        else:
            log.info(
                "travelpayouts: %s of %s returned fares are in scope for %s",
                len(quotes), len(data), route.id,
            )
        return quotes

    @staticmethod
    def _in_scope(route: Route, depart: date, return_date: date | None) -> bool:
        if route.depart is not None:
            return depart == route.depart
        window = route.depart_window
        assert window is not None
        if not (window.earliest <= depart <= window.latest):
            return False
        if route.nights is not None:
            if return_date is None:
                return False
            return (return_date - depart).days == route.nights
        return True


def _as_date(value: str) -> date:
    """Travelpayouts returns ISO datetimes with an offset; we only want the date."""
    return date.fromisoformat(str(value)[:10])
