"""Maps source names in routes.yaml to fetcher classes."""

from __future__ import annotations

from ..config import Config
from .base import Fetcher
from .serpapi import SerpApiFetcher
from .travelpayouts import TravelpayoutsFetcher

FETCHERS: dict[str, type[Fetcher]] = {
    SerpApiFetcher.name: SerpApiFetcher,
    TravelpayoutsFetcher.name: TravelpayoutsFetcher,
}


def build_fetchers(config: Config, names: list[str]) -> dict[str, Fetcher]:
    unknown = [n for n in names if n not in FETCHERS]
    if unknown:
        raise KeyError(f"unknown sources in config: {unknown}; known: {sorted(FETCHERS)}")
    return {name: FETCHERS[name](config) for name in names}
