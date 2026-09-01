"""Shared types. Every fetcher speaks Quote, and nothing else."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict
from datetime import date, datetime, timezone


def utcnow() -> datetime:
    """Timezone-aware UTC. Never use naive datetimes anywhere in this project."""
    return datetime.now(timezone.utc)


def hash_payload(payload: object) -> str:
    """Short digest of a raw API response, for debugging without storing it."""
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


@dataclass(frozen=True)
class Passengers:
    adults: int
    children: int
    infants: int

    @property
    def party_size(self) -> int:
        return self.adults + self.children + self.infants

    @property
    def seated(self) -> int:
        """Lap infants do not occupy a seat."""
        return self.adults + self.children

    @classmethod
    def single_adult(cls) -> "Passengers":
        return cls(adults=1, children=0, infants=0)


@dataclass(frozen=True)
class Quote:
    route_id: str
    source: str
    observed_at: datetime
    depart_date: date
    return_date: date | None
    adults: int
    children: int
    infants: int
    total_price: float
    price_per_adult: float | None
    currency: str
    carrier: str | None
    stops: int | None
    booking_url: str | None
    raw_response_hash: str        # for debugging without storing full payloads
    # Google's own attribute strings for the fare, e.g. 'Carry-on bag not
    # included'. There is no fare-brand field in the search response -- the
    # Booking Options endpoint has one and costs a search per itinerary -- so
    # these strings are the only free signal that a fare is Basic Economy.
    fare_notes: str | None = None

    @property
    def party_size(self) -> int:
        return self.adults + self.children + self.infants

    def is_group(self, passengers: Passengers) -> bool:
        """True when this quote priced the whole party, not a single-adult probe."""
        return (
            self.adults == passengers.adults
            and self.children == passengers.children
            and self.infants == passengers.infants
        )

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Alert:
    route_id: str
    reasons: list[str]
    quote: Quote
    previous_price: float | None
    rolling_min: float | None
