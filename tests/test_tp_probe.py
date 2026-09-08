"""The coverage probe's reasoning, which is the only part of it worth testing.

The calls themselves are one HTTP request each; what earns a test is the step
from a grid of counts to a verdict, because that verdict is what the dashboard
tells someone and what decides whether this source stays described as a
fallback.
"""

from __future__ import annotations

import pytest

from src.fetchers.travelpayouts import TravelpayoutsFetcher
from src.tp_probe import Cell, market_that_helps, near_month, probe, verdict

from .conftest import FakeSession, make_window_config

CONTROL = ("DSM", "DEN")
MONTHS = {"near": "2026-11", "far": "2027-01"}


def cell(pair, horizon, fares, market=None, error=None):
    origin, destination = pair
    return Cell(
        origin=origin,
        destination=destination,
        departure_at=MONTHS[horizon],
        horizon=horizon,
        market=market,
        control=(origin, destination) == CONTROL,
        fares=fares,
        error=error,
    )


def grid(control_near, control_far, tracked_near, tracked_far, market=None):
    """The four counts that drive every verdict, as probe() would return them."""
    return [
        cell(CONTROL, "near", control_near, market),
        cell(CONTROL, "far", control_far, market),
        cell(("DSM", "STT"), "near", tracked_near, market),
        cell(("DSM", "STT"), "far", tracked_far, market),
    ]


# --- verdicts -------------------------------------------------------------


def test_a_busy_route_empty_near_term_blames_our_query_not_the_routes():
    """If even DSM-DEN next month is empty, nothing has been learned about STT."""
    name, detail = verdict(grid(0, 0, 0, 0))
    assert name == "no_cache_at_all"
    assert "query or the account" in detail


def test_fares_near_but_not_far_is_a_horizon_limit():
    name, detail = verdict(grid(20, 0, 5, 0))
    assert name == "horizon"
    assert "Nothing to fix" in detail


def test_control_priced_in_the_window_but_tracked_routes_empty_is_route_thinness():
    """The finding that retires Travelpayouts as a fallback for these trips."""
    name, detail = verdict(grid(20, 12, 0, 0))
    assert name == "route_thin"
    assert "not serve as a fallback" in detail


def test_any_tracked_fare_in_the_window_is_healthy():
    name, detail = verdict(grid(20, 12, 6, 3))
    assert name == "healthy"
    # It must point at where the data already surfaces, or someone goes looking
    # for a dashboard change that is not needed.
    assert "pull table" in detail


def test_a_market_that_returns_fares_outranks_every_other_reading():
    """Our bug beats a fact about the API: it is the one thing we can fix."""
    cells = grid(0, 0, 0, 0) + grid(30, 14, 8, 4, market="us")
    name, detail = verdict(cells)
    assert name == "market_filter"
    assert "market=us" in detail
    assert "DEFAULT_MARKET" in detail


def test_a_market_is_only_credited_when_the_live_query_found_nothing():
    """Both arms returning fares is not evidence for the market parameter."""
    cells = grid(30, 14, 8, 4) + grid(30, 14, 8, 4, market="us")
    assert market_that_helps(cells) is None
    assert verdict(cells)[0] == "healthy"


def test_every_call_failing_reports_unreachable_rather_than_no_coverage():
    """An outage must never read as 'this route has no fares'."""
    cells = [
        cell(CONTROL, "near", None, error="travelpayouts HTTP 401: bad token"),
        cell(("DSM", "STT"), "far", None, error="travelpayouts HTTP 401: bad token"),
    ]
    name, detail = verdict(cells)
    assert name == "unreachable"
    assert "401" in detail


def test_no_cells_is_not_a_conclusion():
    assert verdict([])[0] == "no_data"


# --- the probe loop -------------------------------------------------------


def test_probe_counts_fares_per_cell_and_survives_one_failing_call():
    """One dead cell must not lose the other eleven."""
    config = make_window_config()
    payload = {"success": True, "currency": "USD", "data": [{"price": 1}, {"price": 2}]}
    rejected = {"success": False, "error": "unknown market"}

    def fetcher_for(market):
        session = FakeSession(payload if market is None else rejected)
        return TravelpayoutsFetcher(config, token="t", session=session, market=market)

    cells = probe(fetcher_for, [CONTROL, ("DSM", "STT")], MONTHS, CONTROL, "USD")

    assert len(cells) == 2 * 2 * 2                       # pairs x months x markets
    live = [c for c in cells if c.market is None]
    assert all(c.fares == 2 and c.error is None for c in live)
    failed = [c for c in cells if c.market == "us"]
    assert all(c.fares is None and "unknown market" in c.error for c in failed)


def test_probe_sends_the_market_only_when_one_is_set():
    """The no-market arm has to reproduce production exactly, or it proves nothing."""
    config = make_window_config()
    sessions = {}

    def fetcher_for(market):
        sessions[market] = FakeSession({"success": True, "currency": "USD", "data": []})
        return TravelpayoutsFetcher(config, token="t", session=sessions[market], market=market)

    probe(fetcher_for, [CONTROL], {"near": "2026-11"}, CONTROL, "USD")

    assert "market" not in sessions[None].calls[0]["params"]
    assert sessions["us"].calls[0]["params"]["market"] == "us"


def test_probe_asks_for_round_trips_in_the_configured_currency():
    config = make_window_config()
    session = FakeSession({"success": True, "currency": "USD", "data": []})

    def fetcher_for(market):
        return TravelpayoutsFetcher(config, token="t", session=session, market=market)

    probe(fetcher_for, [CONTROL], {"near": "2026-11"}, CONTROL, "USD")

    params = session.calls[0]["params"]
    assert params["one_way"] == "false"
    assert params["currency"] == "usd"
    assert params["departure_at"] == "2026-11"


@pytest.mark.parametrize(
    "today,expected",
    [
        ("2026-09-08", "2026-11"),
        ("2026-12-15", "2027-02"),          # rolls the year
    ],
)
def test_near_month_lands_about_two_months_out(today, expected):
    from datetime import date

    assert near_month(date.fromisoformat(today)) == expected
