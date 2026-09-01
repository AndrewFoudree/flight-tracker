"""Fetcher parsing, driven entirely by recorded fixtures.

The tests below never open a socket, so the suite costs nothing to run.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.fetchers.base import FetcherError
from src.fetchers.registry import build_fetchers
from src.fetchers.serpapi import SerpApiFetcher
from src.fetchers.travelpayouts import TravelpayoutsFetcher
from src.models import Passengers
from tests.conftest import FakeSession, load_fixture, make_config, make_window_config

PARTY = Passengers(2, 4, 1)


@pytest.fixture
def windowed():
    """A flexible-date route, which is what the Travelpayouts month query needs."""
    return make_window_config()


# --- SerpAPI ------------------------------------------------------------


def serpapi(config, payload, **kwargs):
    return SerpApiFetcher(config, api_key="test-key", session=FakeSession(payload, **kwargs))


def test_serpapi_parses_best_and_other_flights():
    config = make_config()
    fetcher = serpapi(config, load_fixture("serpapi_dsm_mco.json"))
    quotes = fetcher.search(config.routes[0], PARTY)
    assert sorted(q.total_price for q in quotes) == [2745.0, 2980.0, 3120.0]


def test_serpapi_records_party_counts_and_dates():
    config = make_config()
    fetcher = serpapi(config, load_fixture("serpapi_dsm_mco.json"))
    quote = min(fetcher.search(config.routes[0], PARTY), key=lambda q: q.total_price)
    assert (quote.adults, quote.children, quote.infants) == (2, 4, 1)
    assert quote.depart_date == date(2027, 3, 14)
    assert quote.return_date == date(2027, 3, 21)
    assert quote.carrier == "Delta"
    assert quote.stops == 1
    assert quote.currency == "USD"
    assert quote.raw_response_hash


def test_serpapi_counts_a_nonstop_as_zero_stops():
    config = make_config()
    fetcher = serpapi(config, load_fixture("serpapi_dsm_mco.json"))
    nonstop = next(q for q in fetcher.search(config.routes[0], PARTY) if q.carrier == "Allegiant Air")
    assert nonstop.stops == 0


def test_serpapi_sends_lap_infants_separately_from_children():
    config = make_config()
    session = FakeSession(load_fixture("serpapi_dsm_mco.json"))
    fetcher = SerpApiFetcher(config, api_key="test-key", session=session)
    fetcher.search(config.routes[0], PARTY)
    params = session.calls[0]["params"]
    assert params["adults"] == 2 and params["children"] == 4
    assert params["infants_on_lap"] == 1 and params["infants_in_seat"] == 0
    assert params["type"] == 1                      # round trip
    assert params["return_date"] == "2027-03-21"


def test_serpapi_single_adult_probe_sets_price_per_adult():
    config = make_config()
    fetcher = serpapi(config, load_fixture("serpapi_single_adult.json"))
    (quote,) = fetcher.search(config.routes[0], Passengers.single_adult())
    assert quote.total_price == 348.0
    assert quote.price_per_adult == 348.0


def test_serpapi_counts_every_billed_search():
    config = make_config()
    fetcher = serpapi(config, load_fixture("serpapi_dsm_mco.json"))
    fetcher.search(config.routes[0], PARTY)
    assert fetcher.searches_consumed() == 1


def test_serpapi_counts_a_search_even_when_the_response_is_useless():
    """The call is billed whether or not it parses. Miscounting would overspend."""
    config = make_config()
    fetcher = serpapi(config, {"search_metadata": {}}, status_code=500)
    with pytest.raises(FetcherError, match="HTTP 500"):
        fetcher.search(config.routes[0], PARTY)
    assert fetcher.searches_consumed() == 1


def test_serpapi_rejects_a_currency_switch():
    """A silent switch to EUR would ruin the history."""
    payload = load_fixture("serpapi_dsm_mco.json")
    payload["search_parameters"]["currency"] = "EUR"
    config = make_config()
    with pytest.raises(FetcherError, match="expected USD"):
        serpapi(config, payload).search(config.routes[0], PARTY)


def test_serpapi_fails_loudly_when_the_response_shape_changes():
    payload = load_fixture("serpapi_dsm_mco.json")
    del payload["best_flights"][0]["price"]
    payload["other_flights"] = []
    payload["best_flights"] = payload["best_flights"][:1]
    config = make_config()
    with pytest.raises(FetcherError, match="missing 'price'"):
        serpapi(config, payload).search(config.routes[0], PARTY)


def test_serpapi_missing_key_is_an_error_not_a_silent_skip():
    config = make_config()
    fetcher = SerpApiFetcher(config, api_key="", session=FakeSession({}))
    with pytest.raises(FetcherError, match="SERPAPI_KEY"):
        fetcher.search(config.routes[0], PARTY)


# --- Travelpayouts ------------------------------------------------------


def test_travelpayouts_quotes_one_adult_never_a_fabricated_party_total(windowed):
    """The v3 prices API takes no passenger parameters. Multiplying its fare by
    seven is exactly the fare-bucket error, so it must not claim a party total."""
    session = FakeSession(load_fixture("travelpayouts_dsm_den.json"))
    fetcher = TravelpayoutsFetcher(windowed, token="t", session=session)
    quotes = fetcher.search(windowed.route_by_id("dsm-den-flex"), PARTY)
    assert quotes
    assert all((q.adults, q.children, q.infants) == (1, 0, 0) for q in quotes)
    assert all(q.price_per_adult == q.total_price for q in quotes)


def test_travelpayouts_filters_to_the_window_and_the_requested_nights(windowed):
    session = FakeSession(load_fixture("travelpayouts_dsm_den.json"))
    fetcher = TravelpayoutsFetcher(windowed, token="t", session=session)
    quotes = fetcher.search(windowed.route_by_id("dsm-den-flex"), PARTY)
    prices = sorted(q.total_price for q in quotes)
    assert prices == [214.0, 268.0]        # July fare and the 3-night fare are dropped
    assert all(q.depart_date.month == 6 for q in quotes)
    assert all((q.return_date - q.depart_date).days == 7 for q in quotes)


def test_travelpayouts_builds_an_absolute_booking_url(windowed):
    session = FakeSession(load_fixture("travelpayouts_dsm_den.json"))
    fetcher = TravelpayoutsFetcher(windowed, token="t", session=session)
    quote = min(fetcher.search(windowed.route_by_id("dsm-den-flex"), PARTY),
                key=lambda q: q.total_price)
    assert quote.booking_url.startswith("https://www.aviasales.com/search/")


def test_travelpayouts_costs_one_call_per_month_of_the_window(windowed):
    route = windowed.route_by_id("dsm-den-flex")
    fetcher = TravelpayoutsFetcher(windowed, token="t", session=FakeSession({"success": True, "data": []}))
    assert fetcher.estimate_searches(route) == 1        # June only
    fetcher.search(route, PARTY)
    assert fetcher.searches_consumed() == 1


def test_travelpayouts_rejects_an_unsuccessful_response(windowed):
    session = FakeSession({"success": False, "error": "bad token"})
    fetcher = TravelpayoutsFetcher(windowed, token="t", session=session)
    with pytest.raises(FetcherError, match="bad token"):
        fetcher.search(windowed.route_by_id("dsm-den-flex"), PARTY)


# --- registry -----------------------------------------------------------


def test_registry_builds_known_sources(windowed):
    built = build_fetchers(windowed, ["serpapi", "travelpayouts"])
    assert sorted(built) == ["serpapi", "travelpayouts"]


def test_registry_rejects_an_unknown_source(windowed):
    with pytest.raises(KeyError, match="amadeus"):
        build_fetchers(windowed, ["amadeus"])


# --- fare attributes ------------------------------------------------------


def _with_extensions(payload, option_notes, leg_notes=None):
    option = payload["best_flights"][0]
    option["extensions"] = option_notes
    if leg_notes is not None:
        option["flights"][0]["extensions"] = leg_notes
    return payload


def test_serpapi_captures_the_fare_attributes_google_returns():
    """The search response has no fare-brand field. These strings are the only
    free signal that a fare is Basic Economy, so they are kept verbatim."""
    config = make_config()
    payload = _with_extensions(
        load_fixture("serpapi_dsm_mco.json"),
        ["Carry-on bag not included", "Checked baggage for a fee"],
    )
    quotes = serpapi(config, payload).search(config.routes[0], Passengers(2, 4, 1))
    assert quotes[0].fare_notes == "Carry-on bag not included; Checked baggage for a fee"


def test_serpapi_keeps_fare_conditions_and_drops_the_amenity_catalogue():
    """A live response on 2026-09-01 returned mostly legroom, Wi-Fi, power and
    carbon estimates, repeated per leg -- ~300 characters a row saying nothing
    about the fare, in a CSV the dashboard fetches whole on every page load."""
    config = make_config()
    payload = _with_extensions(
        load_fixture("serpapi_dsm_mco.json"),
        ["Carry-on bag not included", "Average legroom (31 in)"],
        [
            "Below average legroom (29 in)",
            "Free Wi-Fi",
            "Carbon emissions estimate: 542 kg",
            "Checked baggage for a fee",
            "Carry-on bag not included",
        ],
    )
    quotes = serpapi(config, payload).search(config.routes[0], Passengers(2, 4, 1))
    # Deduplicated, option level first, not re-sorted: Google puts the
    # conditions before the amenities and that ordering is the useful part.
    assert quotes[0].fare_notes == (
        "Carry-on bag not included; Checked baggage for a fee"
    )


def test_serpapi_says_nothing_rather_than_nothing_known():
    """No extensions means Google said nothing, not that the fare is unrestricted."""
    config = make_config()
    quotes = serpapi(config, load_fixture("serpapi_dsm_mco.json")).search(
        config.routes[0], Passengers(2, 4, 1)
    )
    assert quotes[0].fare_notes is None
