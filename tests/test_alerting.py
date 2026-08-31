"""Alerting and de-duplication.

The behaviour under test: a fare that sits below threshold for two weeks must
produce one notification, not fourteen.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from src import alerting
from src.alerting import AlertState
from src.models import Quote
from tests.conftest import NOW, make_config, price_row


def quote(price: float, route_id: str = "dsm-mco-spring", observed_at=NOW) -> Quote:
    return Quote(
        route_id=route_id,
        source="serpapi",
        observed_at=observed_at,
        depart_date=NOW.date(),
        return_date=None,
        adults=2,
        children=4,
        infants=1,
        total_price=price,
        price_per_adult=None,
        currency="USD",
        carrier="Delta",
        stops=1,
        booking_url=None,
        raw_response_hash="deadbeef",
    )


def route_with(alert_on: list[dict]):
    config = make_config()
    raw = config.model_dump(by_alias=True)
    raw["routes"][0]["alert_on"] = alert_on
    return type(config).model_validate(raw).routes[0]


# --- trigger conditions -------------------------------------------------


def test_absolute_below_fires_under_the_threshold():
    route = route_with([{"absolute_below": 2800}])
    assert alerting.triggered_reasons(route, 2740, [], NOW)
    assert not alerting.triggered_reasons(route, 2800, [], NOW)   # not strictly below


def test_lowest_in_days_fires_on_a_new_low():
    route = route_with([{"lowest_in_days": 30}])
    history = [price_row(10, 2900), price_row(3, 2850)]
    assert alerting.triggered_reasons(route, 2700, history, NOW)
    assert not alerting.triggered_reasons(route, 2860, history, NOW)


def test_percent_drop_fires_only_past_the_configured_size():
    route = route_with([{"percent_drop": 10}])
    history = [price_row(1, 3000)]
    assert alerting.triggered_reasons(route, 2700, history, NOW)      # exactly 10%
    assert not alerting.triggered_reasons(route, 2800, history, NOW)  # 6.7%


def test_multiple_rules_report_every_reason_that_fired():
    route = route_with([{"absolute_below": 2800}, {"lowest_in_days": 30}, {"percent_drop": 5}])
    history = [price_row(1, 3000)]
    assert len(alerting.triggered_reasons(route, 2700, history, NOW)) == 3


# --- cooldown -----------------------------------------------------------


def test_first_alert_always_sends():
    send, why = alerting.should_notify(None, 2740, NOW)
    assert send and "first alert" in why


def test_repeat_inside_the_cooldown_is_suppressed():
    state = AlertState(last_alerted_at=NOW - timedelta(days=2), last_alerted_price=2740)
    send, why = alerting.should_notify(state, 2735, NOW)
    assert not send and "cooldown" in why


def test_repeat_after_the_cooldown_sends():
    state = AlertState(last_alerted_at=NOW - timedelta(days=8), last_alerted_price=2740)
    send, _ = alerting.should_notify(state, 2739, NOW)
    assert send


def test_a_further_five_percent_drop_breaks_the_cooldown():
    state = AlertState(last_alerted_at=NOW - timedelta(days=1), last_alerted_price=2740)
    assert alerting.should_notify(state, 2603, NOW)[0] is True     # exactly 5% lower
    assert alerting.should_notify(state, 2610, NOW)[0] is False    # 4.7%, still quiet


def test_fourteen_days_below_threshold_produce_two_alerts_not_fourteen():
    route = route_with([{"absolute_below": 2800}])
    state: dict[str, AlertState] = {}
    history: list = []
    sent = 0
    for day in range(14):
        moment = NOW + timedelta(days=day)
        current = quote(2740, observed_at=moment)
        alert, _ = alerting.evaluate(route, current, history, state, moment)
        if alert:
            alerting.record(state, alert, moment)
            sent += 1
        history.append(price_row(0, 2740, now=moment))
    assert sent == 2      # day 0, then once the 7-day cooldown elapses


# --- state round trip ---------------------------------------------------


def test_state_survives_a_save_and_load(tmp_path):
    path = tmp_path / "alert_state.json"
    original = {"dsm-mco-spring": AlertState(NOW, 2740.0, cooldown_days=7)}
    alerting.save_state(original, path)
    restored = alerting.load_state(path)
    assert restored["dsm-mco-spring"].last_alerted_price == 2740.0
    assert restored["dsm-mco-spring"].last_alerted_at == NOW
    assert restored["dsm-mco-spring"].cooldown_days == 7


def test_missing_state_file_loads_as_empty(tmp_path):
    assert alerting.load_state(tmp_path / "nope.json") == {}


def test_evaluate_reports_why_it_stayed_quiet():
    route = route_with([{"absolute_below": 2800}])
    alert, why = alerting.evaluate(route, quote(3000), [], {}, NOW)
    assert alert is None and why == "no trigger condition met"


def test_percent_drop_ignores_a_stale_previous_observation():
    """After a blind stretch the previous observation can be weeks old. A 5% rule
    meant to catch a two-day move must not fire on three weeks of drift."""
    route = route_with([{"percent_drop": 10}])
    fresh = [price_row(1, 3000)]
    stale = [price_row(21, 3000)]
    assert alerting.triggered_reasons(route, 2700, fresh, NOW)
    assert not alerting.triggered_reasons(route, 2700, stale, NOW)


def test_a_gap_does_not_affect_the_other_rules():
    """Only percent_drop is interval-sensitive; absolute and rolling-low are not."""
    route = route_with([{"absolute_below": 2800}, {"lowest_in_days": 30}])
    stale = [price_row(21, 3000)]
    assert len(alerting.triggered_reasons(route, 2700, stale, NOW)) == 2
