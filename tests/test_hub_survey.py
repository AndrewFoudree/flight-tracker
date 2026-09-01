"""Hub survey: the budget guard, the leg shape, and the through-fare baseline.

Nothing here calls SerpAPI. The point of the guard is that it decides before any
search is billed, so it has to be testable without spending one.
"""

from __future__ import annotations

from datetime import date

from src import hub_survey
from src.models import Passengers
from tests.conftest import make_config, make_window_config, price_row


def test_a_leg_is_one_fixed_date_round_trip():
    """Two searches per hub depends on each leg costing exactly one."""
    config = hub_survey.build_leg_config(
        make_config(), "DSM", "MCO", date(2027, 1, 2), date(2027, 1, 9)
    )
    route = config.routes[0]
    assert route.origin == "DSM" and route.destination == "MCO"
    assert config.search_dates_for(route) == [(date(2027, 1, 2), date(2027, 1, 9))]


def test_a_leg_inherits_the_party_rather_than_pricing_one_adult():
    config = hub_survey.build_leg_config(
        make_config(), "DSM", "MCO", date(2027, 1, 2), date(2027, 1, 9)
    )
    assert config.passengers_for(config.routes[0]).party_size == 7


def test_per_run_cost_counts_the_split_booking_probe():
    """make_config has one fixed-date route with compare_split_booking on."""
    assert hub_survey.per_run_cost(make_config()) == 2
    # A window route without the probe is one search per sampled departure:
    # Jun 1, 8, 15, 22, 29 at a 7-day step, plus the far edge on Jun 30.
    assert hub_survey.per_run_cost(make_window_config()) == 6


def test_it_refuses_when_the_balance_cannot_be_read(monkeypatch):
    """A manual spend against a shared budget never guesses at the balance."""
    monkeypatch.setattr(hub_survey, "searches_left", lambda: None)
    ok, why = hub_survey.affordable(make_config(), needed=24, reserve_runs=4)
    assert not ok
    assert "could not read the plan balance" in why


def test_it_refuses_when_the_scheduled_runs_would_be_starved(monkeypatch):
    """2 a run x 4 runs + 20 reserve = 28 protected, leaving 12 of 40 spare."""
    monkeypatch.setattr(hub_survey, "searches_left", lambda: 40)
    ok, why = hub_survey.affordable(make_config(), needed=24, reserve_runs=4)
    assert not ok
    assert "12 spare against 24 needed" in why


def test_it_runs_when_there_is_headroom_past_the_protected_runs(monkeypatch):
    monkeypatch.setattr(hub_survey, "searches_left", lambda: 200)
    ok, why = hub_survey.affordable(make_config(), needed=24, reserve_runs=4)
    assert ok
    assert "172 spare against 24 needed" in why


def test_reserving_more_runs_protects_more(monkeypatch):
    monkeypatch.setattr(hub_survey, "searches_left", lambda: 60)
    assert hub_survey.affordable(make_config(), 24, reserve_runs=4)[0]
    assert not hub_survey.affordable(make_config(), 24, reserve_runs=9)[0]


PARTY = Passengers(adults=2, children=4, infants=1)


def test_the_baseline_is_the_cheapest_whole_party_through_fare():
    history = [price_row(1, 3200.0), price_row(0, 2916.0), price_row(0, 3400.0)]
    assert hub_survey.through_fare(
        history, "DSM", "MCO", date(2027, 3, 14), PARTY
    ) == 2916.0


def test_the_baseline_ignores_single_adult_probes():
    """A probe row is a fraction of the party price and would fake a huge saving."""
    history = [price_row(0, 2916.0), price_row(0, 486.0, adults=1, children=0, infants=0)]
    assert hub_survey.through_fare(
        history, "DSM", "MCO", date(2027, 3, 14), PARTY
    ) == 2916.0


def test_no_baseline_when_the_departure_was_never_priced():
    assert hub_survey.through_fare(
        [price_row(0, 2916.0)], "DSM", "MCO", date(2027, 1, 2), PARTY
    ) is None


def test_no_baseline_for_a_different_destination():
    assert hub_survey.through_fare(
        [price_row(0, 2916.0)], "DSM", "SJU", date(2027, 3, 14), PARTY
    ) is None


def test_the_summary_reports_a_saving_and_the_separate_ticket_caveat():
    results = [{
        "depart": date(2027, 1, 2), "hub": "MCO",
        "leg1": 1200.0, "leg2": 900.0, "combo": 2100.0, "through": 2916.0,
        "carriers": "Frontier + Frontier",
    }]
    out = hub_survey.summarise(results, "USD")
    assert "Best saving: USD 816 via MCO" in out
    assert "no protection" in out


def test_the_summary_says_so_when_no_hub_beats_the_through_fare():
    results = [{
        "depart": date(2027, 1, 2), "hub": "MCO",
        "leg1": 1800.0, "leg2": 1400.0, "combo": 3200.0, "through": 2916.0,
        "carriers": "Delta + American",
    }]
    assert "No hub combination beat the through-fare" in hub_survey.summarise(results, "USD")
