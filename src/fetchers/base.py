"""The abstract Fetcher interface.

The API landscape moves. Sources are pluggable so that replacing one is a new
file plus a line in the registry, never a change to main.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import Config, Route
from ..models import Passengers, Quote


class FetcherError(RuntimeError):
    """A source failed. main.py catches this per fetcher and carries on."""


class Fetcher(ABC):
    name: str

    def __init__(self, config: Config) -> None:
        self.config = config
        self._searches = 0

    @abstractmethod
    def search(self, route: Route, passengers: Passengers) -> list[Quote]:
        ...

    @abstractmethod
    def searches_consumed(self) -> int:
        ...

    def estimate_searches(self, route: Route) -> int:
        """Searches this route will cost, so the budget guard can decide up front."""
        return len(self.config.search_dates_for(route))

    # Helpers shared by concrete fetchers.

    def _count_search(self, n: int = 1) -> None:
        self._searches += n

    def _expect_currency(self, route: Route, got: str) -> None:
        """A silent switch to another currency would ruin the history. Fail loudly."""
        want = self.config.currency_for(route)
        if got.upper() != want.upper():
            raise FetcherError(
                f"{self.name}: route {route.id} expected {want} but the response quoted {got}"
            )

    def _require(self, payload: dict, key: str, context: str):
        """Scraper-backed responses change shape without notice. Validate on parse."""
        if key not in payload:
            raise FetcherError(f"{self.name}: {context} is missing '{key}' (response shape changed?)")
        return payload[key]
