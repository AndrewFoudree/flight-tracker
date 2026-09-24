"""Comparing a threshold only against the product that set it.

The failure this guards: on 2027-01-16 San Juan showed $2,650 against St.
Thomas' $3,375 and looked $725 cheaper. The $2,650 was Basic with no cabin
bag; the cheapest San Juan fare that carried one was $3,546, so like for like
San Juan was $171 DEARER. The DSM thresholds were calibrated on bag-inclusive
fares, so letting a Basic fare clear them compares two different products.
"""

from __future__ import annotations

from src import alerting, analysis
from src.models import Quote
from tests.conftest import NOW, make_config, price_row

CARRY_ON = "Checked baggage for a fee"
BASIC = "Carry-on bag not included"


def quote(price: float, fare_notes: str | None, route_id: str = "dsm-mco-spring") -> Quote:
    return Quote(
        route_id=route_id, source="serpapi", observed_at=NOW, depart_date=NOW.date(),
        return_date=None, adults=3, children=3, infants=1, total_price=price,
        price_per_adult=None, currency="USD", carrier="United", stops=1,
        booking_url=None, raw_response_hash="deadbeef", fare_notes=fare_notes,
    )


def route_with(basis: str, alert_on: list[dict] | None = None):
    config = make_config()
    raw = config.model_dump(by_alias=True)
    raw["routes"][0]["threshold_basis"] = basis
    raw["routes"][0]["alert_on"] = alert_on or [{"absolute_below": 2800}]
    return type(config).model_validate(raw).routes[0]


# --- classification -----------------------------------------------------


def test_basic_economy_reads_as_excluded():
    assert analysis.bag_status(price_row(1, 2700, fare_notes=BASIC)) == "excluded"


def test_checked_bag_fee_means_the_cabin_bag_is_included():
    assert analysis.bag_status(price_row(1, 2700, fare_notes=CARRY_ON)) == "included"


def test_silence_is_unknown_not_permission():
    """Most rows before 2026-09-20 carry no conditions; that is not 'has bags'."""
    assert analysis.bag_status(price_row(1, 2700, fare_notes=None)) == "unknown"
    assert analysis.bag_status(price_row(1, 2700, fare_notes="")) == "unknown"


def test_an_unrelated_note_does_not_imply_a_bag():
    row = price_row(1, 2700, fare_notes="Overnight layover (ORD, MIA)")
    assert analysis.bag_status(row) == "unknown"


def test_the_return_flight_qualifier_does_not_downgrade():
    """It qualifies the hold bag and fare rules, not the cabin bag statement."""
    row = price_row(1, 2700, fare_notes=f"{CARRY_ON}; Bag and fare conditions depend on the return flight")
    assert analysis.bag_status(row) == "included"


def test_status_reads_a_live_quote_too():
    assert analysis.bag_status(quote(2700, BASIC)) == "excluded"


# --- filtering ----------------------------------------------------------


def test_bag_inclusive_drops_basic_and_unknown_alike():
    rows = [
        price_row(3, 2400, fare_notes=BASIC),
        price_row(2, 2500, fare_notes=None),
        price_row(1, 2900, fare_notes=CARRY_ON),
    ]
    kept = analysis.with_bag_basis(rows, "bag_inclusive")
    assert [r.total_price for r in kept] == [2900]


def test_any_basis_keeps_everything():
    rows = [price_row(1, 2400, fare_notes=BASIC), price_row(1, 2500, fare_notes=None)]
    assert analysis.with_bag_basis(rows, "any") == rows


# --- the alert gate -----------------------------------------------------


def test_a_basic_fare_cannot_clear_a_bag_inclusive_bar():
    route = route_with("bag_inclusive")
    alert, why = alerting.evaluate(route, quote(2400, BASIC), [], {}, NOW)
    assert alert is None
    assert "cabin bag excluded" in why


def test_an_unknown_fare_cannot_clear_it_either():
    route = route_with("bag_inclusive")
    alert, why = alerting.evaluate(route, quote(2400, None), [], {}, NOW)
    assert alert is None
    assert "cabin bag unknown" in why


def test_a_bag_inclusive_fare_still_fires():
    route = route_with("bag_inclusive")
    alert, _ = alerting.evaluate(route, quote(2400, CARRY_ON), [], {}, NOW)
    assert alert is not None


def test_any_basis_leaves_the_old_behaviour_untouched():
    route = route_with("any")
    alert, _ = alerting.evaluate(route, quote(2400, BASIC), [], {}, NOW)
    assert alert is not None


def test_the_basis_filters_history_not_just_the_quote():
    """A Basic low must not decide whether a bag-inclusive fare is a new low.

    Without this, a $2,400 Basic fare last week makes today's $2,600 fare with
    a bag look like no improvement, when against its own product class it is.
    """
    route = route_with("bag_inclusive", [{"lowest_in_days": 30}])
    history = [
        price_row(7, 2400, fare_notes=BASIC),      # cheaper, but a different product
        price_row(7, 2900, fare_notes=CARRY_ON),
    ]
    alert, _ = alerting.evaluate(route, quote(2600, CARRY_ON), history, {}, NOW)
    assert alert is not None, "should be a new low among bag-inclusive fares"

    # Under "any" the Basic fare is in scope and $2,600 is not a new low.
    assert alerting.evaluate(route_with("any", [{"lowest_in_days": 30}]),
                             quote(2600, CARRY_ON), history, {}, NOW)[0] is None


def test_default_basis_is_any_so_existing_routes_do_not_change():
    assert make_config().routes[0].threshold_basis == "any"


# --- choosing which fare the bar judges ---------------------------------


def test_a_basic_fare_undercutting_the_cabin_fare_does_not_silence_the_route():
    """The first cut of this got it wrong and is worth pinning down.

    On a bag_inclusive route the cheapest fare is usually Basic. Handing that
    one to the alerting code made the route report "no alert" when the right
    answer was "judge the cheapest fare that carries a bag instead".
    """
    from src.main import best_quote_for_basis
    from src.models import Passengers

    route = route_with("bag_inclusive")
    party = Passengers(3, 3, 1)
    quotes = [
        quote(2400, BASIC, route.id),        # cheapest overall, not eligible
        quote(2750, CARRY_ON, route.id),     # cheapest eligible
        quote(2900, CARRY_ON, route.id),
    ]
    chosen = best_quote_for_basis(quotes, route, party)
    assert chosen.total_price == 2750

    alert, _ = alerting.evaluate(route, chosen, [], {}, NOW)
    assert alert is not None, "the $2,750 bag-inclusive fare is under the $2,800 bar"


def test_any_basis_still_judges_the_outright_cheapest():
    from src.main import best_quote_for_basis
    from src.models import Passengers

    route = route_with("any")
    quotes = [quote(2400, BASIC, route.id), quote(2750, CARRY_ON, route.id)]
    assert best_quote_for_basis(quotes, route, Passengers(3, 3, 1)).total_price == 2400


def test_no_eligible_fare_yields_none_rather_than_a_basic_one():
    from src.main import best_quote_for_basis
    from src.models import Passengers

    route = route_with("bag_inclusive")
    quotes = [quote(2400, BASIC, route.id), quote(2500, None, route.id)]
    assert best_quote_for_basis(quotes, route, Passengers(3, 3, 1)) is None
