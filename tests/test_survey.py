"""The one-off year survey. Fixture-driven, so it spends nothing."""

from __future__ import annotations

from datetime import date

import pytest

from src import survey
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
