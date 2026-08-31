"""Analysis against synthetic history. No API calls, no files."""

from __future__ import annotations

from datetime import timedelta

from src import analysis
from src.models import Passengers
from tests.conftest import NOW, price_row

PARTY = Passengers(2, 4, 1)


def test_route_rows_filters_by_route_and_sorts_oldest_first():
    rows = [
        price_row(1, 2800),
        price_row(5, 2900),
        price_row(2, 3000, route_id="other"),
    ]
    selected = analysis.route_rows(rows, "dsm-mco-spring")
    assert [r.total_price for r in selected] == [2900, 2800]


def test_single_adult_probes_are_excluded_from_party_history():
    """The split-booking probe is a fraction of the party price. If it leaked
    into the history it would win every 'lowest ever' comparison forever."""
    rows = [price_row(1, 2800), price_row(1, 348, adults=1, children=0, infants=0)]
    selected = analysis.route_rows(rows, "dsm-mco-spring", passengers=PARTY)
    assert [r.total_price for r in selected] == [2800]


def test_rolling_minimum_respects_the_window():
    rows = [price_row(45, 2100), price_row(10, 2600), price_row(2, 2750)]
    assert analysis.rolling_minimum(rows, 30, NOW) == 2600
    assert analysis.rolling_minimum(rows, 60, NOW) == 2100
    assert analysis.rolling_minimum([], 30, NOW) is None


def test_is_lowest_in_days_compares_against_prior_observations_only():
    history = [price_row(20, 2900), price_row(5, 2850)]
    assert analysis.is_lowest_in_days(2800, history, 30, NOW) is True
    assert analysis.is_lowest_in_days(2860, history, 30, NOW) is False


def test_first_ever_sighting_does_not_count_as_a_low():
    assert analysis.is_lowest_in_days(2800, [], 30, NOW) is False


def test_an_empty_window_does_not_count_as_a_low():
    """Only a stale observation outside the window: there is no recent baseline
    to beat, so this behaves like a first sighting and stays quiet."""
    history = [price_row(90, 1500)]
    assert analysis.is_lowest_in_days(2800, history, 30, NOW) is False
    assert analysis.is_lowest_in_days(2800, history, 120, NOW) is False   # 1500 still wins


def test_todays_own_row_is_not_used_as_its_own_benchmark():
    """main.py re-reads the CSV after writing, so the current observation is in
    the history it analyses. Comparing a price to itself would never be a low."""
    today = price_row(0, 2800)
    history = [price_row(10, 2900), today]
    assert analysis.is_lowest_in_days(2800, history, 30, today.observed_at) is True


def test_previous_observation_is_the_latest_before_now():
    history = [price_row(9, 2900), price_row(3, 2860), price_row(0, 2800)]
    previous = analysis.previous_observation(history, NOW)
    assert previous is not None and previous.total_price == 2860


def test_percent_change_and_drop():
    assert analysis.percent_change(2700, 3000) == -10.0
    assert analysis.percent_drop(2700, 3000) == 10.0
    assert analysis.percent_drop(3300, 3000) is None      # a rise is not a drop
    assert analysis.percent_drop(2700, None) is None
    assert analysis.percent_drop(2700, 0) is None


def test_daily_minimum_keeps_the_cheapest_row_per_day():
    rows = [price_row(1, 2900), price_row(1.4, 2750), price_row(0, 3000)]
    daily = analysis.daily_minimum(rows)
    assert sorted(daily.values()) == [2750, 3000]


def test_moving_average_uses_daily_minimums():
    rows = [price_row(d, 3000 - d * 10) for d in range(0, 6)]
    ma7 = analysis.moving_average(rows, 7, NOW)
    assert ma7 is not None and 2970 < ma7 < 3000
    assert analysis.moving_average([], 7, NOW) is None


def test_trend_reports_both_windows():
    rows = [price_row(2, 2800), price_row(25, 3200)]
    result = analysis.trend(rows, NOW)
    assert result["ma_7"] == 2800
    assert result["ma_30"] == 3000


def test_split_booking_delta_flags_a_cheaper_split():
    delta = analysis.split_booking_delta(group_total=2800.0, single_price=348.0, seats=6)
    assert delta["estimated_split_total"] == 2088.0
    assert delta["delta"] == 712.0
    assert round(delta["percent_cheaper_split"], 1) == 25.4


def test_split_booking_delta_is_negative_when_the_group_fare_wins():
    delta = analysis.split_booking_delta(group_total=2400.0, single_price=450.0, seats=6)
    assert delta["delta"] < 0


def test_a_lap_infant_is_free_on_a_domestic_route():
    """Seven travellers, six seats. Charging the infant a seventh fare would
    invent 348 dollars nobody pays and hide a real saving."""
    domestic = analysis.split_booking_delta(2800.0, 348.0, seats=6, infants=1)
    head_count = analysis.split_booking_delta(2800.0, 348.0, seats=7, infants=0)
    assert domestic["infant_cost"] == 0.0
    assert domestic["estimated_split_total"] == 2088.0
    assert head_count["estimated_split_total"] == 2436.0
    assert domestic["delta"] > head_count["delta"]


def test_an_international_lap_infant_adds_a_percentage_of_one_fare():
    delta = analysis.split_booking_delta(
        2800.0, 348.0, seats=6, infants=1, infant_fare_pct=10
    )
    assert round(delta["infant_cost"], 2) == 34.80
    assert round(delta["estimated_split_total"], 2) == 2122.80


def test_within_days_excludes_the_future():
    rows = [price_row(-1, 2500), price_row(1, 2800)]
    assert [r.total_price for r in analysis.within_days(rows, 30, NOW)] == [2800]


def test_history_is_scoped_to_the_itinerary_the_route_now_searches():
    """Route ids get reused when travel plans change. A January trip must not be
    compared against the March trip that once carried the same id."""
    from datetime import date
    january = price_row(1, 2955)
    march = price_row(2, 3327)
    object.__setattr__(january, "depart_date", date(2027, 1, 7))
    object.__setattr__(january, "return_date", date(2027, 1, 12))
    rows = [january, march]

    scoped = analysis.route_rows(
        rows, "dsm-mco-spring", itineraries={(date(2027, 1, 7), date(2027, 1, 12))}
    )
    assert [r.total_price for r in scoped] == [2955]

    assert len(analysis.route_rows(rows, "dsm-mco-spring")) == 2   # unscoped is unchanged


def test_a_windowed_route_keeps_every_departure_it_prices():
    from datetime import date
    pairs = {(date(2027, 3, 14), date(2027, 3, 21))}
    rows = [price_row(1, 2955), price_row(2, 3100)]
    assert len(analysis.route_rows(rows, "dsm-mco-spring", itineraries=pairs)) == 2
