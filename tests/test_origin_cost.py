"""Reaching a non-home origin, priced as a range rather than a number.

Driving to ORD is $169 counting fuel, a cheap lot and tolls, or $611 counting
vehicle wear at the IRS rate and the economy lot -- a 3.6x spread with no
honest midpoint. The range exists so a comparison can be tested at both ends
instead of resting on a figure nobody can pin down.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.conftest import make_config


def config_with(origins: dict, origin: str | None = None):
    config = make_config()
    raw = config.model_dump(by_alias=True)
    if origin is not None:
        raw["routes"][0]["origin"] = origin
    raw["origins"] = origins
    return type(config).model_validate(raw)


def test_a_range_is_accepted_and_read_back():
    config = config_with({"DSM": {"low": 169, "high": 611, "note": "drive to ORD"}})
    cost = config.origin_cost_for(config.routes[0])
    assert (cost.low, cost.high) == (169, 611)


def test_high_below_low_is_rejected():
    with pytest.raises(ValidationError, match="high is below low"):
        config_with({"DSM": {"low": 611, "high": 169}})


def test_equal_bounds_are_fine():
    """A cost genuinely known to the dollar needs no spread."""
    config = config_with({"DSM": {"low": 40, "high": 40}})
    assert config.origin_cost_for(config.routes[0]).low == 40


def test_negative_cost_is_rejected():
    with pytest.raises(ValidationError):
        config_with({"DSM": {"low": -10, "high": 100}})


def test_an_unreferenced_origin_is_allowed():
    """survey.py and hub_survey.py swap in their own single route.

    Rejecting the now-unreferenced origins would break every one-off sweep, and
    an unused entry is inert: it shows a band on no route at all.
    """
    config = config_with({"ORD": {"low": 169, "high": 611}})
    assert config.origin_cost_for(config.routes[0]) is None


def test_a_malformed_airport_code_is_rejected():
    with pytest.raises(ValidationError):
        config_with({"OHare": {"low": 169, "high": 611}})


def test_an_unlisted_origin_costs_nothing():
    """The home airport is free, and a missing entry must not invent a number."""
    config = make_config()
    assert config.origin_cost_for(config.routes[0]) is None


def test_origins_defaults_to_empty():
    assert make_config().origins == {}
