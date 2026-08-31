"""One-off survey: price the same trip across a whole year to find the cheap weeks.

The daily tracker answers "is this itinerary cheap right now?". This answers the
prior question, "which itinerary should I be tracking at all?", by sweeping
departure dates at a coarse step and ranking what comes back.

Run once, read the table, then configure routes.yaml on the winners. It is not a
scheduled job: a year-wide sweep every day would exhaust the month's allowance in
an afternoon.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from .config import Config, load_config
from .fetchers.serpapi import SerpApiFetcher
from .models import Quote, utcnow
from .storage import PRICE_COLUMNS, _append, append_usage, quote_to_row

log = logging.getLogger("survey")

SURVEY_PATH = Path("data/survey.csv")
SURVEY_ID = "survey"


def build_survey_config(
    base: Config, origin: str, destination: str, earliest: date, latest: date,
    nights: int, step: int,
) -> Config:
    """A single windowed route, so the SerpAPI fetcher sweeps it date by date."""
    raw = base.model_dump(by_alias=True, mode="json")
    raw["routes"] = [
        {
            "id": SURVEY_ID,
            "origin": origin,
            "destination": destination,
            "depart_window": {"earliest": earliest.isoformat(), "latest": latest.isoformat()},
            "nights": nights,
            "window_step_days": step,
            "threshold_usd": 1,                 # unused; the survey never alerts
            "alert_on": [{"absolute_below": 1}],
        }
    ]
    return Config.model_validate(raw)


def summarise(quotes: list[Quote], currency: str) -> str:
    """Rank departures by price, then roll the same numbers up by month."""
    if not quotes:
        return "No fares returned. Dates beyond about 11 months are often not yet loaded."

    by_departure: dict[date, float] = {}
    for quote in quotes:
        current = by_departure.get(quote.depart_date)
        if current is None or quote.total_price < current:
            by_departure[quote.depart_date] = quote.total_price

    cheapest = min(by_departure.values())
    lines = ["", "Cheapest fare per departure date, best first:", ""]
    for departure, price in sorted(by_departure.items(), key=lambda kv: kv[1]):
        delta = price - cheapest
        marker = "  <-- cheapest" if delta == 0 else f"  (+{delta:,.0f})"
        lines.append(f"  {departure:%a %d %b %Y}   {currency} {price:>9,.0f}{marker}")

    by_month: dict[str, list[float]] = defaultdict(list)
    for departure, price in by_departure.items():
        by_month[f"{departure:%Y-%m}"].append(price)

    lines += ["", "By month, cheapest departure in each:", ""]
    for month in sorted(by_month):
        low = min(by_month[month])
        bar = "#" * max(1, round((low / max(min(v) for v in by_month.values())) * 12))
        lines.append(f"  {month}   {currency} {low:>9,.0f}  {bar}")

    spread = max(by_departure.values()) - cheapest
    lines += [
        "",
        f"Spread across the year: {currency} {spread:,.0f} "
        f"({spread / cheapest * 100:.0f}% above the cheapest departure).",
    ]
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    base = load_config(args.config)
    config = build_survey_config(
        base, args.origin, args.destination,
        date.fromisoformat(args.earliest), date.fromisoformat(args.latest),
        args.nights, args.step,
    )
    route = config.routes[0]
    passengers = config.passengers_for(route)
    pairs = config.search_dates_for(route)

    log.info(
        "surveying %s-%s, %s departures every %s days, %s adults %s children %s infant(s)",
        args.origin, args.destination, len(pairs), args.step,
        passengers.adults, passengers.children, passengers.infants,
    )
    if len(pairs) > args.max_searches:
        log.error(
            "that is %s searches and --max-searches is %s. Widen --step or narrow the range.",
            len(pairs), args.max_searches,
        )
        return 2

    fetcher = SerpApiFetcher(config)
    try:
        quotes = fetcher.search(route, passengers)
    except Exception as exc:
        log.error("survey failed after %s searches: %s", fetcher.searches_consumed(), exc)
        return 1
    finally:
        # Record spend even on failure; the searches were billed either way.
        spent = fetcher.searches_consumed()
        if spent:
            append_usage("serpapi", SURVEY_ID, spent, utcnow())
            log.info("recorded %s searches against this month's budget", spent)

    path = Path(args.out)
    rows = [quote_to_row(q, args.origin, args.destination) for q in quotes]
    _append(path, PRICE_COLUMNS, rows)
    log.info("wrote %s rows to %s", len(rows), path)
    print(summarise(quotes, config.currency_for(route)))
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--origin", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--earliest", required=True, help="first departure date, YYYY-MM-DD")
    parser.add_argument("--latest", required=True, help="last departure date, YYYY-MM-DD")
    parser.add_argument("--nights", type=int, default=7)
    parser.add_argument("--step", type=int, default=14, help="days between sampled departures")
    parser.add_argument("--max-searches", type=int, default=40, help="refuse to run past this")
    parser.add_argument("--config", default="config/routes.yaml", help="source of party defaults")
    parser.add_argument("--out", default=str(SURVEY_PATH))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
