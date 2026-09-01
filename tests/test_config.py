"""Config validation. A typo must fail here, before any API call is spent."""

from __future__ import annotations

import json
from datetime import date

import pytest
from pydantic import ValidationError

from src.config import Config, load_config
from tests.conftest import BASE_CONFIG, make_config, make_window_config


def mutate(**route_changes) -> dict:
    raw = json.loads(json.dumps(BASE_CONFIG))
    raw["routes"][0].update(route_changes)
    return raw


def runs_per_month(workflow: str = ".github/workflows/check-prices.yml") -> int:
    """How often the schedule fires, read from the workflow's cron.

    Spend is routes x searches x cadence, so a budget check that assumes daily
    runs is wrong the moment the schedule changes. Weekly rounds up to five: a
    month can hold five Saturdays and the budget has to survive that month too.
    """
    import re
    from pathlib import Path

    text = Path(workflow).read_text(encoding="utf-8")
    cron = re.search(r'- cron: "([^"]+)"', text)
    assert cron, "no cron found in the price workflow"
    fields = cron.group(1).split()
    day_of_month, day_of_week = fields[2], fields[4]
    if day_of_week != "*":                           # one run a week per named day
        return 5 * len(day_of_week.split(","))
    every = re.match(r"\*/(\d+)$", day_of_month)
    if every:
        return 31 // int(every.group(1)) + 1
    if re.match(r"^\d+(,\d+)*$", day_of_month):      # an explicit list of days
        return len(day_of_month.split(","))
    return 31                                        # every day


def test_shipped_config_is_valid():
    """The live config must parse and fit a full billing cycle.

    A depleted cycle is handled at runtime by recording NA, so this checks the
    steady state: a schedule that cannot fit even a fresh allowance is a config
    error, not a temporary shortage."""
    config = load_config("config/routes.yaml")
    assert config.routes
    per_run = sum(
        len(config.search_dates_for(r)) * (2 if r.compare_split_booking else 1)
        for r in config.routes
    )
    monthly = per_run * runs_per_month()
    assert monthly <= config.budget.spendable("serpapi"), (
        f"{per_run} searches a run at {runs_per_month()} runs a month is {monthly}, "
        f"over the {config.budget.spendable('serpapi')} cap"
    )


def test_a_daily_schedule_would_not_fit_the_current_routes():
    """Guards the reason the schedule is not daily."""
    config = load_config("config/routes.yaml")
    per_run = sum(
        len(config.search_dates_for(r)) * (2 if r.compare_split_booking else 1)
        for r in config.routes
    )
    assert per_run * 31 > config.budget.spendable("serpapi")


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
    config = make_window_config()
    pairs = config.search_dates_for(config.route_by_id("dsm-den-flex"))
    departures = [d for d, _ in pairs]
    assert departures[0] == date(2027, 6, 1)
    assert departures[-1] == date(2027, 6, 30)
    assert all(r - d == (date(2027, 6, 8) - date(2027, 6, 1)) for d, r in pairs)


def test_budget_spendable_only_meters_serpapi():
    config = make_config()
    assert config.budget.spendable("serpapi") == 230
    assert config.budget.spendable("travelpayouts") is None


# --- lap-infant eligibility ---------------------------------------------

BORN = "2025-05-20"          # turns two on 2027-05-20


def with_birthdate(born: str = BORN, **route_changes) -> Config:
    raw = mutate(**route_changes)
    raw["defaults"]["passengers"]["infant_birthdates"] = [born]
    return Config.model_validate(raw)


def test_birthdates_must_match_the_infant_count():
    raw = mutate()
    raw["defaults"]["passengers"]["infant_birthdates"] = [BORN, "2024-01-01"]
    with pytest.raises(ValidationError, match="infant_birthdates has 2 entries"):
        Config.model_validate(raw)


def test_an_infant_still_under_two_keeps_the_lap_seat():
    config = with_birthdate()                       # March 2027 trip, he is 21 months
    passengers = config.passengers_for(config.routes[0])
    assert (passengers.adults, passengers.children, passengers.infants) == (3, 3, 1)
    assert passengers.seated == 6
    assert config.infant_notes(config.routes[0]) == []


def test_an_infant_who_has_aged_out_is_priced_as_a_child_with_a_seat():
    config = with_birthdate(depart="2027-08-01", **{"return": "2027-08-08"})
    passengers = config.passengers_for(config.routes[0])
    assert (passengers.adults, passengers.children, passengers.infants) == (3, 4, 0)
    assert passengers.seated == 7
    assert "before departure" in config.infant_notes(config.routes[0])[0]


def test_a_birthday_mid_trip_prices_the_whole_trip_with_a_seat():
    """He flies out at 23 months and home at two. The return needs a seat, so the
    itinerary has to be priced with one."""
    config = with_birthdate(depart="2027-05-15", **{"return": "2027-05-25"})
    assert config.passengers_for(config.routes[0]).seated == 7
    assert "mid-trip" in config.infant_notes(config.routes[0])[0]


def test_a_flexible_window_is_classified_on_its_last_possible_travel_day():
    raw = mutate()
    raw["routes"][0].pop("depart")
    raw["routes"][0].pop("return")
    raw["routes"][0]["depart_window"] = {"earliest": "2027-05-01", "latest": "2027-05-30"}
    raw["routes"][0]["nights"] = 7
    raw["defaults"]["passengers"]["infant_birthdates"] = [BORN]
    config = Config.model_validate(raw)
    assert config.travel_end_date(config.routes[0]).isoformat() == "2027-06-06"
    assert config.passengers_for(config.routes[0]).seated == 7


def test_a_leap_day_birthday_ages_out_on_the_first_of_march():
    from src.config import turns_two_on
    from datetime import date as d
    assert turns_two_on(d(2024, 2, 29)) == d(2026, 3, 1)
    assert turns_two_on(d(2025, 5, 20)) == d(2027, 5, 20)


def test_without_birthdates_the_configured_counts_are_used_silently():
    """Optional feature: with no birth date the config is taken as written and
    the run stays quiet. Notes are only for a reclassification actually made."""
    config = make_config()
    assert config.passengers_for(config.routes[0]).infants == 1
    assert config.infant_notes(config.routes[0]) == []
