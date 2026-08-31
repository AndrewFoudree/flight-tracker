"""Config validation. A typo must fail here, before any API call is spent."""

from __future__ import annotations

import json
from datetime import date

import pytest
from pydantic import ValidationError

from src.config import Config, load_config
from tests.conftest import BASE_CONFIG, make_config


def mutate(**route_changes) -> dict:
    raw = json.loads(json.dumps(BASE_CONFIG))
    raw["routes"][0].update(route_changes)
    return raw


def test_shipped_config_is_valid():
    config = load_config("config/routes.yaml")
    assert [r.id for r in config.routes] == ["dsm-mco-spring", "dsm-den-flex"]


def test_defaults_resolve_per_route():
    config = make_config()
    route = config.routes[0]
    assert config.passengers_for(route).party_size == 7
    assert config.passengers_for(route).seated == 6
    assert config.currency_for(route) == "USD"
    assert config.sources_for(route) == ["serpapi"]


def test_lowercase_airport_code_is_rejected():
    with pytest.raises(ValidationError, match="origin"):
        Config.model_validate(mutate(origin="dsm"))


def test_four_letter_airport_code_is_rejected():
    with pytest.raises(ValidationError):
        Config.model_validate(mutate(origin="KDSM"))


def test_same_origin_and_destination_is_rejected():
    with pytest.raises(ValidationError, match="same airport"):
        Config.model_validate(mutate(destination="DSM"))


def test_return_before_depart_is_rejected():
    with pytest.raises(ValidationError, match="return is before depart"):
        Config.model_validate(mutate(**{"return": "2027-03-01"}))


def test_unknown_key_is_rejected():
    with pytest.raises(ValidationError):
        Config.model_validate(mutate(threshhold_usd=2800))


def test_alert_rule_needs_exactly_one_key():
    with pytest.raises(ValidationError, match="exactly one"):
        Config.model_validate(mutate(alert_on=[{"absolute_below": 100, "percent_drop": 5}]))
    with pytest.raises(ValidationError, match="exactly one"):
        Config.model_validate(mutate(alert_on=[{}]))


def test_route_needs_fixed_or_flexible_dates_not_both():
    with pytest.raises(ValidationError, match="not both"):
        Config.model_validate(
            mutate(depart_window={"earliest": "2027-06-01", "latest": "2027-06-30"}, nights=7)
        )


def test_flexible_route_requires_nights():
    raw = mutate()
    raw["routes"][0].pop("depart")
    raw["routes"][0].pop("return")
    raw["routes"][0]["depart_window"] = {"earliest": "2027-06-01", "latest": "2027-06-30"}
    with pytest.raises(ValidationError, match="need nights"):
        Config.model_validate(raw)


def test_infants_may_not_outnumber_adults():
    raw = json.loads(json.dumps(BASE_CONFIG))
    raw["defaults"]["passengers"] = {"adults": 1, "children": 0, "infants": 2}
    with pytest.raises(ValidationError, match="lap infant"):
        Config.model_validate(raw)


def test_reserve_must_be_smaller_than_allowance():
    raw = json.loads(json.dumps(BASE_CONFIG))
    raw["budget"] = {"serpapi_monthly_searches": 250, "reserve": 250}
    with pytest.raises(ValidationError, match="reserve"):
        Config.model_validate(raw)


def test_duplicate_route_ids_are_rejected():
    raw = json.loads(json.dumps(BASE_CONFIG))
    raw["routes"].append(json.loads(json.dumps(raw["routes"][0])))
    with pytest.raises(ValidationError, match="duplicate route ids"):
        Config.model_validate(raw)


def test_fixed_route_yields_one_search_date():
    config = make_config()
    assert config.search_dates_for(config.routes[0]) == [(date(2027, 3, 14), date(2027, 3, 21))]


def test_window_expands_and_always_includes_the_far_edge():
    config = load_config("config/routes.yaml")
    pairs = config.search_dates_for(config.route_by_id("dsm-den-flex"))
    departures = [d for d, _ in pairs]
    assert departures[0] == date(2027, 6, 1)
    assert departures[-1] == date(2027, 6, 30)
    assert all(r - d == (date(2027, 6, 8) - date(2027, 6, 1)) for d, r in pairs)


def test_budget_spendable_only_meters_serpapi():
    config = make_config()
    assert config.budget.spendable("serpapi") == 230
    assert config.budget.spendable("travelpayouts") is None
