"""Shared test helpers. Nothing here touches the network."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.config import Config
from src.storage import PriceRow

FIXTURES = Path(__file__).parent / "fixtures"

NOW = datetime(2026, 8, 31, 13, 0, tzinfo=timezone.utc)

BASE_CONFIG = {
    "defaults": {
        "passengers": {"adults": 2, "children": 4, "infants": 1},
        "currency": "USD",
        "cabin": "economy",
        "sources": ["serpapi"],
    },
    "budget": {"serpapi_monthly_searches": 250, "reserve": 20},
    "routes": [
        {
            "id": "dsm-mco-spring",
            "origin": "DSM",
            "destination": "MCO",
            "depart": "2027-03-14",
            "return": "2027-03-21",
            "threshold_usd": 2800,
            "alert_on": [{"absolute_below": 2800}],
            "compare_split_booking": True,
        }
    ],
}


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_config(**overrides) -> Config:
    raw = json.loads(json.dumps(BASE_CONFIG))
    raw.update(overrides)
    return Config.model_validate(raw)


def price_row(
    days_ago: float,
    price: float,
    route_id: str = "dsm-mco-spring",
    adults: int = 2,
    children: int = 4,
    infants: int = 1,
    source: str = "serpapi",
    now: datetime = NOW,
) -> PriceRow:
    """A synthetic observation `days_ago` before `now`."""
    return PriceRow(
        observed_at=now - timedelta(days=days_ago),
        route_id=route_id,
        source=source,
        origin="DSM",
        destination="MCO",
        depart_date=datetime(2027, 3, 14).date(),
        return_date=datetime(2027, 3, 21).date(),
        adults=adults,
        children=children,
        infants=infants,
        total_price=price,
        currency="USD",
        carrier="Delta",
        stops=1,
        booking_url=None,
    )


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


class FakeSession:
    """Replays recorded payloads in order and records the params it was given."""

    def __init__(self, *payloads: dict, status_code: int = 200) -> None:
        self.payloads = list(payloads)
        self.status_code = status_code
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params or {}})
        payload = self.payloads.pop(0) if len(self.payloads) > 1 else self.payloads[0]
        return FakeResponse(payload, self.status_code)


@pytest.fixture
def now() -> datetime:
    return NOW
