"""SerpAPI's Google Flights engine.

Primary source. Roughly 250 searches a month on the free plan, so every call is
counted and the budget guard in main.py can stop before the cap.

Never scrape Google Flights directly: heavy JavaScript, unstable markup, active
anti-bot measures, and a terms-of-service violation. SerpAPI exists so we do not
own that problem.
"""

from __future__ import annotations

import logging
import os

import requests

from ..config import Route
from ..models import Passengers, Quote, hash_payload, utcnow
from .base import Fetcher, FetcherError

log = logging.getLogger(__name__)

ENDPOINT = "https://serpapi.com/search.json"
TIMEOUT = 45

CABIN_CODES = {"economy": 1, "premium_economy": 2, "business": 3, "first": 4}


class SerpApiFetcher(Fetcher):
    name = "serpapi"

    def __init__(self, config, api_key: str | None = None, session=None) -> None:
        super().__init__(config)
        self.api_key = api_key if api_key is not None else os.environ.get("SERPAPI_KEY", "")
        self.session = session or requests.Session()

    def searches_consumed(self) -> int:
        return self._searches

    def search(self, route: Route, passengers: Passengers) -> list[Quote]:
        if not self.api_key:
            raise FetcherError("SERPAPI_KEY is not set")
        quotes: list[Quote] = []
        for depart, return_date in self.config.search_dates_for(route):
            payload = self._call(route, passengers, depart, return_date)
            quotes.extend(self._parse(payload, route, passengers, depart, return_date))
        return quotes

    # --- HTTP -------------------------------------------------------------

    def _call(self, route: Route, passengers: Passengers, depart, return_date) -> dict:
        cabin = self.config.cabin_for(route)
        params = {
            "engine": "google_flights",
            "api_key": self.api_key,
            "departure_id": route.origin,
            "arrival_id": route.destination,
            "outbound_date": depart.isoformat(),
            "currency": self.config.currency_for(route),
            "hl": "en",
            "adults": passengers.adults,
            "children": passengers.children,
            "infants_on_lap": passengers.infants,
            "infants_in_seat": 0,
            "travel_class": CABIN_CODES.get(cabin, 1),
            "type": 1 if return_date else 2,     # 1 round trip, 2 one way
            "deep_search": "true",
        }
        if return_date:
            params["return_date"] = return_date.isoformat()

        response = self.session.get(ENDPOINT, params=params, timeout=TIMEOUT)
        # The search is billed whether or not we like the answer.
        self._count_search()
        if response.status_code != 200:
            raise FetcherError(f"serpapi HTTP {response.status_code}: {response.text[:200]}")
        body = response.json()
        if body.get("error"):
            raise FetcherError(f"serpapi error: {body['error']}")
        return body

    # --- parsing ----------------------------------------------------------

    @staticmethod
    def _fare_notes(itinerary: dict, legs: list) -> str | None:
        """Google's attribute strings for this fare, option level and per leg.

        Deduplicated in first-seen order rather than sorted, because Google puts
        the restrictive ones first and that ordering is worth keeping. Absent on
        many results, which is not an error: it means Google said nothing, not
        that the fare is unrestricted.
        """
        seen: list[str] = []
        sources = [itinerary.get("extensions")] + [leg.get("extensions") for leg in legs]
        for group in sources:
            if not isinstance(group, list):
                continue
            for note in group:
                text = str(note).strip()
                if text and text not in seen:
                    seen.append(text)
        return "; ".join(seen) or None

    def _parse(self, payload: dict, route, passengers, depart, return_date) -> list[Quote]:
        itineraries = list(payload.get("best_flights") or []) + list(payload.get("other_flights") or [])
        if not itineraries:
            log.warning("serpapi: no itineraries for %s on %s", route.id, depart)
            return []

        currency = (payload.get("search_parameters") or {}).get("currency") or self.config.currency_for(route)
        self._expect_currency(route, currency)

        digest = hash_payload(payload)
        observed_at = utcnow()
        booking_url = (payload.get("search_metadata") or {}).get("google_flights_url")

        quotes: list[Quote] = []
        for itinerary in itineraries:
            price = self._require(itinerary, "price", f"itinerary for {route.id}")
            if not isinstance(price, (int, float)):
                raise FetcherError(f"serpapi: non-numeric price {price!r} for {route.id}")
            legs = itinerary.get("flights") or []
            carrier = legs[0].get("airline") if legs else None
            fare_notes = self._fare_notes(itinerary, legs)
            layovers = itinerary.get("layovers")
            stops = len(layovers) if isinstance(layovers, list) else (len(legs) - 1 if legs else None)
            quotes.append(
                Quote(
                    route_id=route.id,
                    source=self.name,
                    observed_at=observed_at,
                    depart_date=depart,
                    return_date=return_date,
                    adults=passengers.adults,
                    children=passengers.children,
                    infants=passengers.infants,
                    total_price=float(price),
                    price_per_adult=(
                        float(price) if passengers == Passengers.single_adult() else None
                    ),
                    currency=currency,
                    carrier=carrier,
                    stops=stops,
                    booking_url=booking_url,
                    raw_response_hash=digest,
                    fare_notes=fare_notes,
                )
            )
        return quotes
