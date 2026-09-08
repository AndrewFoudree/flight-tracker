"""The coverage probe's reasoning, which is the only part of it worth testing.

The calls themselves are one HTTP request each; what earns a test is the step
from a grid of counts to a verdict, because that verdict is what the dashboard
tells someone and what decides whether this source stays described as a
fallback -- or whether the bug is ours.
"""

from __future__ import annotations

import pytest

from src.fetchers.travelpayouts import TravelpayoutsFetcher
from src.tp_probe import (
    PRODUCTION_MARKET,
    PRODUCTION_SHAPE,
    SHAPES,
    Cell,
    dates_for,
    near_month,
    probe,
    variant_that_helps,
    verdict,
)

from .conftest import FakeSession, make_window_config

CONTROL = ("DSM", "DEN")
MONTHS = {"near": "2026-11", "far": "2027-01"}
ONE_SHAPE = (SHAPES[0],)


def cell(pair, horizon, fares, shape=PRODUCTION_SHAPE, market=PRODUCTION_MARKET, error=None):
    origin, destination = pair
    return Cell(
        origin=origin,
        destination=destination,
        month=MONTHS[horizon],
        departure_at=MONTHS[horizon],
        return_at=None,
        horizon=horizon,
        shape=shape,
        market=market,
        control=(origin, destination) == CONTROL,
        fares=fares,
        error=error,
    )


def grid(control_near, control_far, tracked_near, tracked_far, **variant):
    """The four counts that drive every verdict, as probe() would return them."""
    return [
        cell(CONTROL, "near", control_near, **variant),
        cell(CONTROL, "far", control_far, **variant),
        cell(("DSM", "STT"), "near", tracked_near, **variant),
        cell(("DSM", "STT"), "far", tracked_far, **variant),
    ]


# --- verdicts -------------------------------------------------------------


def test_empty_in_every_shape_including_the_control_blames_the_account():
    """The 2026-09-08 result. Nothing here is a fact about STT."""
    cells = grid(0, 0, 0, 0) + grid(0, 0, 0, 0, shape="one_way_month")
    name, detail = verdict(cells)
    assert name == "no_cache_at_all"
    assert "entitled to this endpoint" in detail


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


# --- our bug outranks every fact about the API ----------------------------


def test_a_shape_that_returns_fares_names_itself_as_the_fix():
    cells = grid(0, 0, 0, 0) + grid(30, 14, 8, 4, shape="one_way_month")
    name, detail = verdict(cells)
    assert name == "query_shape"
    assert "one_way_month" in detail
    assert "src/fetchers/travelpayouts.py" in detail


def test_a_market_that_returns_fares_is_reported_as_the_market_filter():
    cells = grid(0, 0, 0, 0) + grid(30, 14, 8, 4, market="us")
    name, detail = verdict(cells)
    assert name == "market_filter"
    assert 'DEFAULT_MARKET to "us"' in detail


def test_a_variant_differing_in_both_reports_both_changes():
    cells = grid(0, 0, 0, 0) + grid(9, 9, 9, 9, shape="round_trip_dated", market="us")
    name, detail = verdict(cells)
    assert name == "query_shape"
    assert "round_trip_dated" in detail
    assert "DEFAULT_MARKET" in detail


def test_a_variant_is_only_credited_when_the_live_query_found_nothing():
    """Both arms returning fares is not evidence for either change."""
    cells = grid(30, 14, 8, 4) + grid(30, 14, 8, 4, shape="one_way_month")
    assert variant_that_helps(cells) is None
    assert verdict(cells)[0] == "healthy"


def test_coverage_verdicts_ignore_variants_that_also_found_nothing():
    """A dead variant must not drag a healthy live query into 'mixed'."""
    cells = grid(20, 12, 6, 3) + grid(0, 0, 0, 0, shape="one_way_dated")
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


# --- query shapes ---------------------------------------------------------


def test_the_production_shape_is_first_and_sends_a_bare_month():
    """Every other arm is read as a difference from this one."""
    assert SHAPES[0].name == PRODUCTION_SHAPE
    assert dates_for(SHAPES[0], "2026-11", nights=7) == ("2026-11", None)


@pytest.mark.parametrize(
    "shape_name,expected",
    [
        ("one_way_month", ("2026-11", None)),
        ("round_trip_dated", ("2026-11-15", "2026-11-22")),
        ("one_way_dated", ("2026-11-15", None)),
    ],
)
def test_each_shape_asks_for_the_dates_it_promises(shape_name, expected):
    shape = next(s for s in SHAPES if s.name == shape_name)
    assert dates_for(shape, "2026-11", nights=7) == expected


def test_a_one_way_shape_never_sends_a_return_date():
    for shape in SHAPES:
        if shape.one_way:
            assert dates_for(shape, "2026-11", nights=7)[1] is None


# --- the probe loop -------------------------------------------------------


def test_probe_covers_every_pair_horizon_and_shape():
    config = make_window_config()

    def fetcher_for(market):
        return TravelpayoutsFetcher(
            config, token="t", market=market,
            session=FakeSession({"success": True, "currency": "USD", "data": [{"price": 1}]}),
        )

    cells = probe(fetcher_for, [CONTROL, ("DSM", "STT")], MONTHS, CONTROL, "USD")
    assert len(cells) == 2 * 2 * len(SHAPES)
    assert len({c.shape for c in cells}) == len(SHAPES)
    assert sum(1 for c in cells if c.is_production) == 4


def test_probe_survives_one_failing_call():
    """One dead arm must not lose the others."""
    config = make_window_config()
    good = {"success": True, "currency": "USD", "data": [{"price": 1}, {"price": 2}]}
    bad = {"success": False, "error": "unknown market"}

    def fetcher_for(market):
        return TravelpayoutsFetcher(
            config, token="t", market=market,
            session=FakeSession(good if market is None else bad),
        )

    cells = probe(
        fetcher_for, [CONTROL], MONTHS, CONTROL, "USD", markets=(None, "us"),
    )
    live = [c for c in cells if c.market is None]
    dead = [c for c in cells if c.market == "us"]
    assert all(c.fares == 2 and c.error is None for c in live)
    assert all(c.fares is None and "unknown market" in c.error for c in dead)


def test_probe_sends_the_market_only_when_one_is_set():
    """The live arm has to reproduce production exactly, or it proves nothing."""
    config = make_window_config()
    sessions = {}

    def fetcher_for(market):
        sessions[market] = FakeSession({"success": True, "currency": "USD", "data": []})
        return TravelpayoutsFetcher(config, token="t", session=sessions[market], market=market)

    probe(
        fetcher_for, [CONTROL], {"near": "2026-11"}, CONTROL, "USD",
        shapes=ONE_SHAPE, markets=(None, "us"),
    )
    assert "market" not in sessions[None].calls[0]["params"]
    assert sessions["us"].calls[0]["params"]["market"] == "us"


def test_the_live_arm_reproduces_the_weekly_query():
    """A round trip over a whole month with no return date -- what production sends."""
    config = make_window_config()
    session = FakeSession({"success": True, "currency": "USD", "data": []})

    def fetcher_for(market):
        return TravelpayoutsFetcher(config, token="t", session=session, market=market)

    probe(fetcher_for, [CONTROL], {"near": "2026-11"}, CONTROL, "USD", shapes=ONE_SHAPE)

    params = session.calls[0]["params"]
    assert params["one_way"] == "false"
    assert params["currency"] == "usd"
    assert params["departure_at"] == "2026-11"
    assert "return_at" not in params
    assert "market" not in params


def test_the_dated_round_trip_arm_sends_a_return_date():
    """The arm that tests whether a missing return_at is why the cache stays empty."""
    config = make_window_config()
    session = FakeSession({"success": True, "currency": "USD", "data": []})
    dated = tuple(s for s in SHAPES if s.name == "round_trip_dated")

    def fetcher_for(market):
        return TravelpayoutsFetcher(config, token="t", session=session, market=market)

    probe(fetcher_for, [CONTROL], {"near": "2026-11"}, CONTROL, "USD", shapes=dated, nights=7)

    params = session.calls[0]["params"]
    assert params["departure_at"] == "2026-11-15"
    assert params["return_at"] == "2026-11-22"
    assert params["one_way"] == "false"


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
