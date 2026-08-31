"""main.py end to end, with the network replaced by a stub fetcher.

Exercises the path the workflow takes: fetch, write, re-read, analyse, alert,
persist state.
"""

from __future__ import annotations

import json

import pytest
import yaml

from src import main, storage
from src.alerting import load_state
from src.fetchers.base import Fetcher, FetcherError
from src.models import Passengers, Quote, utcnow
from tests.conftest import BASE_CONFIG


class StubFetcher(Fetcher):
    name = "serpapi"
    prices: list[float] = [2745.0]
    fail = False

    def searches_consumed(self) -> int:
        return self._searches

    def search(self, route, passengers) -> list[Quote]:
        self._count_search()
        if self.fail:
            raise FetcherError("stub source is down")
        return [
            Quote(
                route_id=route.id,
                source=self.name,
                observed_at=utcnow(),
                depart_date=route.depart,
                return_date=route.return_,
                adults=passengers.adults,
                children=passengers.children,
                infants=passengers.infants,
                total_price=(
                    price if passengers != Passengers.single_adult() else price / 10
                ),
                price_per_adult=None,
                currency="USD",
                carrier="Delta",
                stops=1,
                booking_url=None,
                raw_response_hash="stub",
            )
            for price in self.prices
        ]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A scratch repo: config on disk, cwd moved, network stubbed out."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    raw = json.loads(json.dumps(BASE_CONFIG))
    (tmp_path / "config/routes.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setattr(main, "build_fetchers", lambda config, names: {"serpapi": StubFetcher(config)})
    monkeypatch.setattr(StubFetcher, "prices", [2745.0])
    monkeypatch.setattr(StubFetcher, "fail", False)
    return tmp_path


def run(*argv: str) -> int:
    return main.run(main.parse_args(["--no-notify", *argv]))


def test_a_full_run_writes_history_usage_and_state(workspace):
    assert run() == 0
    rows = storage.read_history(storage.PRICES_PATH)
    # One party quote plus one single-adult probe (compare_split_booking is on).
    assert len(rows) == 2
    assert {r.is_single_adult() for r in rows} == {True, False}
    assert storage.searches_used("serpapi", utcnow()) == 2
    assert load_state()["dsm-mco-spring"].last_alerted_price == 2745.0


def test_a_second_run_appends_and_does_not_re_alert(workspace):
    assert run() == 0
    assert run() == 0
    assert len(storage.read_history(storage.PRICES_PATH)) == 4
    state = load_state()
    # Still one alert: the cooldown suppressed the repeat.
    assert state["dsm-mco-spring"].last_alerted_price == 2745.0


def test_a_failing_source_does_not_end_the_run(workspace, monkeypatch):
    monkeypatch.setattr(StubFetcher, "fail", True)
    assert run() == 0
    assert storage.read_history(storage.PRICES_PATH) == []
    assert load_state() == {}


def test_an_exhausted_budget_skips_the_search(workspace):
    storage.append_usage("serpapi", "dsm-mco-spring", 230, utcnow())
    assert run() == 0
    assert storage.read_history(storage.PRICES_PATH) == []


def test_dry_run_calls_nothing_and_writes_no_history(workspace):
    assert run("--dry-run") == 0
    assert storage.read_history(storage.PRICES_PATH) == []
    assert storage.searches_used("serpapi", utcnow()) == 0


def test_unknown_route_filter_is_an_error(workspace):
    assert run("--route", "nope") == 2


def test_price_above_threshold_stores_but_stays_quiet(workspace, monkeypatch):
    monkeypatch.setattr(StubFetcher, "prices", [3400.0])
    assert run() == 0
    assert len(storage.read_history(storage.PRICES_PATH)) == 2
    assert load_state() == {}
