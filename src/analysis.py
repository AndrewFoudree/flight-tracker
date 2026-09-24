"""Analysis over the stored history. Never issues an API call.

A fixed dollar threshold says what you want to pay. Your own history says
whether the price in front of you is actually good.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from statistics import fmean

from .models import Passengers, Quote
from .storage import PriceRow

# What Google states about the cabin bag, in its own words. "Carry-on bag not
# included" is Basic Economy; "Checked baggage for a fee" means the carry-on is
# included and only the hold bag costs extra.
_NO_CARRY_ON = "carry-on bag not included"
_CHECKED_FOR_FEE = "checked baggage for a fee"


def bag_status(row: "PriceRow | Quote") -> str:
    """"included", "excluded" or "unknown" -- never a guess.

    Takes a stored row or a live quote; both carry Google's own `fare_notes`
    strings, and the question is the same either way.

    The third state is the point of this function. Google saying nothing is not
    Google saying the fare is unrestricted: 100% of the 2026-08-31 rows and 90%
    of 2026-09-01's carry no conditions at all, because the parser that reads
    them landed later. The 2026-09-20 run was the first at full coverage.

    So "unknown" has to stay distinct from "excluded". Collapsing the two would
    quietly disqualify most of the history, and collapsing it into "included"
    would do the thing this exists to prevent -- letting a Basic fare clear a
    bar that a fare with a carry-on set.

    The "Bag and fare conditions depend on the return flight" qualifier that
    accompanies many of these notes is about the hold bag and the fare rules
    differing between legs. It does not contradict the carry-on statement, so it
    is not treated as a downgrade.
    """
    notes = (row.fare_notes or "").lower()
    if _NO_CARRY_ON in notes:
        return "excluded"
    if _CHECKED_FOR_FEE in notes:
        return "included"
    return "unknown"


def with_bag_basis(rows: list[PriceRow], basis: str) -> list[PriceRow]:
    """Rows eligible under a threshold's basis.

    "any" keeps everything. "bag_inclusive" keeps only fares Google states carry
    a cabin bag, which deliberately drops "unknown" as well as Basic: a bar
    calibrated on bag-inclusive fares must not be cleared by a fare nobody can
    confirm included one.
    """
    if basis == "any":
        return rows
    return [row for row in rows if bag_status(row) == "included"]


def route_rows(
    rows: list[PriceRow],
    route_id: str,
    passengers: Passengers | None = None,
    source: str | None = None,
    itineraries: set[tuple[date, date | None]] | None = None,
) -> list[PriceRow]:
    """Rows for one route, oldest first.

    Passing `passengers` keeps whole-party quotes only. That matters: the
    single-adult split-booking probes sit in the same file at a fraction of the
    price, and would otherwise win every "lowest ever" comparison.

    Passing `itineraries` keeps only rows for the dates the route currently
    searches. Route ids get reused when travel plans change, and comparing a
    January trip against the March trip that once held the same id would make
    every trend meaningless.
    """
    selected = [r for r in rows if r.route_id == route_id]
    if passengers is not None:
        selected = [r for r in selected if r.is_group(passengers)]
    if source is not None:
        selected = [r for r in selected if r.source == source]
    if itineraries is not None:
        selected = [r for r in selected if (r.depart_date, r.return_date) in itineraries]
    return sorted(selected, key=lambda r: r.observed_at)


def before(rows: list[PriceRow], moment: datetime) -> list[PriceRow]:
    """History strictly older than `moment` -- i.e. excluding the current run."""
    return [r for r in rows if r.observed_at < moment]


def within_days(rows: list[PriceRow], days: int, now: datetime) -> list[PriceRow]:
    cutoff = now - timedelta(days=days)
    return [r for r in rows if cutoff <= r.observed_at <= now]


def rolling_minimum(rows: list[PriceRow], days: int, now: datetime) -> float | None:
    """Cheapest price seen in the last `days`, or None with no history in range."""
    window = within_days(rows, days, now)
    return min((r.total_price for r in window), default=None)


def is_lowest_in_days(price: float, history: list[PriceRow], days: int, now: datetime) -> bool:
    """True when `price` beats everything observed in the preceding `days`.

    With no prior observation in the window there is nothing to beat, so the
    first sighting of a route does not fire an alert.
    """
    prior = rolling_minimum(before(history, now), days, now)
    return prior is not None and price < prior


def previous_observation(
    history: list[PriceRow], now: datetime, max_age_days: int | None = None
) -> PriceRow | None:
    """The cheapest quote from the most recent day before `now` that has data.

    Not the last row. One run writes a row per itinerary and per carrier, so
    `prior[-1]` is whichever quote happened to be written last -- on 2026-09-01
    that was a $5,364 Delta fare sitting behind a $2,796 cheapest, and the
    percent-drop rule read a 48% fall that never happened. The rule compares
    cheapest now against cheapest then, so that is what this returns.

    `max_age_days` guards the percent-drop rule across a blind stretch. After a
    gap, "the previous observation" can be weeks old, and a 5% rule meant to
    catch a two-day move would instead fire on three weeks of drift. Too old and
    there is no meaningful comparison to make.
    """
    prior = before(history, now)
    if not prior:
        return None
    latest_day = max(row.observed_at.date() for row in prior)
    cheapest = min(
        (row for row in prior if row.observed_at.date() == latest_day),
        key=lambda row: row.total_price,
    )
    if max_age_days is not None and (now - cheapest.observed_at) > timedelta(days=max_age_days):
        return None
    return cheapest


def percent_change(current: float, previous: float | None) -> float | None:
    """Signed change against the previous observation. Negative means cheaper."""
    if previous is None or previous <= 0:
        return None
    return (current - previous) / previous * 100.0


def percent_drop(current: float, previous: float | None) -> float | None:
    """Magnitude of a drop, or None when the price did not fall."""
    change = percent_change(current, previous)
    if change is None or change >= 0:
        return None
    return -change


def daily_minimum(rows: list[PriceRow]) -> dict[date, float]:
    """One number per UTC day: the cheapest observation that day."""
    by_day: dict[date, float] = {}
    for row in rows:
        day = row.observed_at.date()
        if day not in by_day or row.total_price < by_day[day]:
            by_day[day] = row.total_price
    return dict(sorted(by_day.items()))


def moving_average(rows: list[PriceRow], days: int, now: datetime) -> float | None:
    """Mean of the daily minimums over the trailing `days`."""
    window = daily_minimum(within_days(rows, days, now))
    return fmean(window.values()) if window else None


def trend(rows: list[PriceRow], now: datetime) -> dict[str, float | None]:
    return {
        "ma_7": moving_average(rows, 7, now),
        "ma_30": moving_average(rows, 30, now),
    }


def split_booking_delta(
    group_total: float,
    single_price: float,
    seats: int,
    infants: int = 0,
    infant_fare_pct: float = 0.0,
) -> dict[str, float]:
    """Compare one booking for the party against booking each traveller separately.

    Counts *seats*, not people: a lap infant does not buy one. Multiplying a
    single fare by the head count would invent a fare that nobody pays and hide
    a real saving. `infant_fare_pct` covers international routes, where a lap
    infant is typically about 10% of the adult fare plus taxes; it defaults to 0
    because domestic US lap infants travel free.

    An approximation, not a quote: it flags routes worth checking by hand. See
    the split-booking caveats in the README before acting on it.
    """
    infant_cost = single_price * infants * (infant_fare_pct / 100.0)
    estimated = single_price * seats + infant_cost
    return {
        "group_total": group_total,
        "estimated_split_total": estimated,
        "seats": float(seats),
        "infant_cost": infant_cost,
        "delta": group_total - estimated,
        "percent_cheaper_split": (
            (group_total - estimated) / group_total * 100.0 if group_total > 0 else 0.0
        ),
    }
