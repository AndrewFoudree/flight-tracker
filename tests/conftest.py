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
        "passengers": {"adults": 3, "children": 3, "infants": 1},
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


# A flexible-date route, kept here rather than read from config/routes.yaml so
# the suite does not break every time the live config changes.
WINDOW_CONFIG = {
    "defaults": {
        "passengers": {"adults": 3, "children": 3, "infants": 1},
        "currency": "USD",
        "cabin": "economy",
        "sources": ["serpapi", "travelpayouts"],
        "window_step_days": 7,
    },
    "budget": {"serpapi_monthly_searches": 250, "reserve": 20},
    "routes": [
        {
            "id": "dsm-den-flex",
            "origin": "DSM",
            "destination": "DEN",
            "depart_window": {"earliest": "2027-06-01", "latest": "2027-06-30"},
            "nights": 7,
            "threshold_usd": 1900,
            "alert_on": [{"lowest_in_days": 45}],
        }
    ],
}


@pytest.fixture(autouse=True)
def party(monkeypatch):
    """config/routes.yaml reads the split from PARTY, which is a repository
    variable in Actions and deliberately not in the repo. Tests supply a
    synthetic one with the same seat count so the shipped config still parses."""
    monkeypatch.setenv("PARTY", "3,3,1")


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_config(**overrides) -> Config:
    raw = json.loads(json.dumps(BASE_CONFIG))
    raw.update(overrides)
    return Config.model_validate(raw)


def make_window_config(**overrides) -> Config:
    raw = json.loads(json.dumps(WINDOW_CONFIG))
    raw.update(overrides)
    return Config.model_validate(raw)


def price_row(
    days_ago: float,
    price: float,
    route_id: str = "dsm-mco-spring",
    seats: int = 6,
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
        seats=seats,
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
