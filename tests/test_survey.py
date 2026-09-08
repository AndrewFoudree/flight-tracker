"""The one-off year survey. Fixture-driven, so it spends nothing."""

from __future__ import annotations

from datetime import date

import pytest

from src import budget, survey
from src.fetchers.serpapi import SerpApiFetcher
from src.models import Passengers
from tests.conftest import FakeSession, load_fixture, make_config


def survey_config(step: int = 14):
    return survey.build_survey_config(
        make_config(), "DSM", "STT",
        date(2026, 9, 15), date(2027, 7, 15), nights=7, step=step,
    )


def test_survey_config_inherits_the_party_from_the_live_config():
    config = survey_config()
    passengers = config.passengers_for(config.routes[0])
    assert (passengers.adults, passengers.children, passengers.infants) == (3, 3, 1)
    assert config.currency_for(config.routes[0]) == "USD"


def test_step_controls_the_number_of_searches():
    assert len(survey_config(14).search_dates_for(survey_config(14).routes[0])) == 23
    coarse = survey_config(30)
    assert len(coarse.search_dates_for(coarse.routes[0])) < 23


def test_every_sampled_trip_is_the_requested_length():
    config = survey_config()
    for depart, return_date in config.search_dates_for(config.routes[0]):
        assert (return_date - depart).days == 7


def test_the_sweep_costs_one_search_per_sampled_date():
    config = survey_config(step=90)
    route = config.routes[0]
    expected = len(config.search_dates_for(route))
    fetcher = SerpApiFetcher(
        config, api_key="k", session=FakeSession(load_fixture("serpapi_dsm_mco.json"))
    )
    fetcher.search(route, Passengers(3, 3, 1))
    assert fetcher.searches_consumed() == expected


def test_summary_ranks_cheapest_first_and_reports_the_spread():
    config = survey_config(step=90)
    route = config.routes[0]
    fetcher = SerpApiFetcher(
        config, api_key="k", session=FakeSession(load_fixture("serpapi_dsm_mco.json"))
    )
    quotes = fetcher.search(route, Passengers(3, 3, 1))
    text = survey.summarise(quotes, "USD")
    assert "Cheapest fare per departure date" in text
    assert "<-- cheapest" in text
    assert "By month" in text
    assert "Spread across the year" in text


def test_summary_explains_an_empty_result_rather_than_crashing():
    assert "not yet loaded" in survey.summarise([], "USD")


def test_run_refuses_to_exceed_the_search_cap():
    """The guard fires before the fetcher is built, so nothing is billed."""
    args = survey.parse_args([
        "--origin", "DSM", "--destination", "STT",
        "--earliest", "2026-09-15", "--latest", "2027-07-15",
        "--step", "14", "--max-searches", "5",
    ])
    assert survey.run(args) == 2


# --- the balance guard ----------------------------------------------------
#
# --max-searches only caps what the sweep asks for. On 2026-08-31 a sweep well
# inside its own cap still took 192 of 250 searches and left the weekly tracker
# blind until the plan renewed, because nothing asked what the scheduled runs
# still needed. These cover the check that would have stopped it.


def sweep_args(**overrides):
    argv = {
        "--origin": "DSM", "--destination": "STT",
        "--earliest": "2026-09-15", "--latest": "2026-11-15",
        "--step": "14", "--max-searches": "40",
    }
    argv.update(overrides)
    return survey.parse_args([v for pair in argv.items() for v in pair])


def test_a_sweep_inside_its_own_cap_is_still_refused_when_the_cycle_is_short(monkeypatch):
    """The 2026-08-31 failure, in miniature."""
    monkeypatch.setattr(budget, "searches_left", lambda: 40)
    called = []
    monkeypatch.setattr(survey, "SerpApiFetcher", lambda *a, **k: called.append(1))
    assert survey.run(sweep_args()) == 3
    assert not called, "refusing must happen before a single search is spent"


def test_a_sweep_runs_when_the_scheduled_runs_are_still_funded(monkeypatch):
    monkeypatch.setattr(budget, "searches_left", lambda: 250)
    fetcher = SerpApiFetcher(
        survey_config(step=14), api_key="k",
        session=FakeSession(load_fixture("serpapi_dsm_mco.json")),
    )
    monkeypatch.setattr(survey, "SerpApiFetcher", lambda *a, **k: fetcher)
    monkeypatch.setattr(survey, "append_usage", lambda *a, **k: 0)
    monkeypatch.setattr(survey, "_append", lambda *a, **k: 0)
    assert survey.run(sweep_args()) == 0


def test_a_sweep_refuses_when_the_balance_cannot_be_read(monkeypatch):
    """Unknown is not the same as fine: a manual spend never guesses."""
    monkeypatch.setattr(budget, "searches_left", lambda: None)
    assert survey.run(sweep_args()) == 3


def test_reserving_more_runs_makes_the_survey_refuse_sooner(monkeypatch):
    """Same balance, same sweep. Only the number of protected runs changes.

    The two reserve figures are deliberately far apart rather than tuned to the
    live per-run cost: this asserts that the reserve is what decides, not that
    a route count in config/routes.yaml still happens to be what it was.
    """
    monkeypatch.setattr(budget, "searches_left", lambda: 1000)
    monkeypatch.setattr(survey, "append_usage", lambda *a, **k: 0)
    monkeypatch.setattr(survey, "_append", lambda *a, **k: 0)
    fetcher = SerpApiFetcher(
        survey_config(step=14), api_key="k",
        session=FakeSession(load_fixture("serpapi_dsm_mco.json")),
    )
    monkeypatch.setattr(survey, "SerpApiFetcher", lambda *a, **k: fetcher)

    assert survey.run(sweep_args(**{"--reserve-runs": "1"})) == 0
    assert survey.run(sweep_args(**{"--reserve-runs": "100"})) == 3
