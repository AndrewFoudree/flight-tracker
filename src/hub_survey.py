"""One-off: does routing through a hub on two tickets beat the through-fare?

Budget carriers never appear in the tracked searches, and that is not a gap in
the data. Frontier serves DSM and serves SJU, but not as one network, and
low-cost carriers do not interline -- so no through-itinerary exists for Google
Flights to return. The only way their fares reach this trip is two separate
tickets: origin to a hub, then hub to the destination.

This prices that combination once so the saving can be measured instead of
guessed. It is deliberately not part of the weekly tracker and never writes to
prices.csv: a two-ticket itinerary is not the same product as a through-fare and
must not land in the same series or the same charts. Read the table, decide
whether the saving is worth the separate-ticket risk, and act by hand.

What the number here does not include: checked bags for the whole party on a
carrier that prices them separately, an overnight in the hub when the legs do not
line up, and the absence of any missed-connection protection between two
unconnected tickets. Treat the saving as a ceiling, not a quote.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from .config import Config, load_config
from .fetchers.account import searches_left
from .fetchers.serpapi import SerpApiFetcher
from .models import Passengers, Quote, utcnow
from .storage import PRICE_COLUMNS, PriceRow, _append, append_usage, quote_to_row, read_history

log = logging.getLogger("hub-survey")

HUB_SURVEY_PATH = Path("data/hub_survey.csv")
USAGE_ID = "hub-survey"


def leg_id(origin: str, destination: str) -> str:
    return f"leg-{origin.lower()}-{destination.lower()}"


def build_leg_config(base: Config, origin: str, destination: str,
                     depart: date, return_date: date) -> Config:
    """One fixed-date round trip, which the fetcher prices in a single search."""
    raw = base.model_dump(by_alias=True, mode="json")
    raw["routes"] = [
        {
            "id": leg_id(origin, destination),
            "origin": origin,
            "destination": destination,
            "depart": depart.isoformat(),
            "return": return_date.isoformat(),
            "threshold_usd": 1,                 # unused; this never alerts
            "alert_on": [{"absolute_below": 1}],
        }
    ]
    return Config.model_validate(raw)


def per_run_cost(config: Config) -> int:
    """What one scheduled run of the live config spends on SerpAPI."""
    return sum(
        len(config.search_dates_for(route)) * (2 if route.compare_split_booking else 1)
        for route in config.routes
    )


def affordable(config: Config, needed: int, reserve_runs: int) -> tuple[bool, str]:
    """Whether this sweep can run without starving the scheduled runs.

    The plan balance is the provider's, not the local ledger's, because the
    ledger counts calendar months while SerpAPI bills on its own renewal date.
    A sweep that cannot see the balance refuses rather than guesses: this is a
    manual spend against a budget the weekly runs depend on.
    """
    left = searches_left()
    if left is None:
        return False, (
            "could not read the plan balance from SerpAPI, so the cost to the "
            "scheduled runs is unknown. Re-run when the account endpoint answers."
        )
    protected = per_run_cost(config) * reserve_runs + config.budget.reserve
    spare = left - protected
    detail = (
        f"{left} searches left, {protected} protected "
        f"({reserve_runs} scheduled run(s) plus a {config.budget.reserve} reserve), "
        f"so {spare} spare against {needed} needed"
    )
    if needed > spare:
        return False, f"not enough headroom: {detail}."
    return True, detail


def cheapest(quotes: list[Quote]) -> Quote | None:
    return min(quotes, key=lambda q: q.total_price) if quotes else None


def through_fare(history: list[PriceRow], origin: str, destination: str,
                 depart: date, passengers: Passengers) -> float | None:
    """The best whole-party through-fare already recorded for this departure."""
    prices = [
        row.total_price
        for row in history
        if row.origin == origin
        and row.destination == destination
        and row.depart_date == depart
        and row.is_group(passengers)
    ]
    return min(prices) if prices else None


def summarise(results: list[dict], currency: str) -> str:
    if not results:
        return "No fares returned for any leg."

    lines = [
        "",
        "Two tickets through a hub against the through-fare already tracked:",
        "",
        f"  {'Depart':<12} {'Hub':<4} {'Leg 1':>10} {'Leg 2':>10} "
        f"{'Two tickets':>12} {'Through':>10} {'Saving':>9}  Carriers",
    ]
    for r in sorted(results, key=lambda r: (r["depart"], r["hub"])):
        combo = r["combo"]
        through = r["through"]
        saving = None if (combo is None or through is None) else through - combo
        lines.append(
            f"  {r['depart']:%a %d %b}  {r['hub']:<4} "
            f"{_money(r['leg1'], currency):>10} {_money(r['leg2'], currency):>10} "
            f"{_money(combo, currency):>12} {_money(through, currency):>10} "
            f"{_money(saving, currency, signed=True):>9}  {r['carriers'] or '-'}"
        )

    priced = [r for r in results if r["combo"] is not None and r["through"] is not None]
    if priced:
        best = max(priced, key=lambda r: r["through"] - r["combo"])
        gap = best["through"] - best["combo"]
        verdict = (
            f"Best saving: {currency} {gap:,.0f} via {best['hub']} on "
            f"{best['depart']:%a %d %b}."
            if gap > 0
            else f"No hub combination beat the through-fare. Closest was {best['hub']} "
                 f"on {best['depart']:%a %d %b}, {currency} {-gap:,.0f} worse."
        )
        lines += ["", verdict]

    lines += [
        "",
        "Before acting on a saving: it excludes checked bags for the whole party,",
        "assumes the two legs connect on the day, and carries no protection if the",
        "first ticket runs late. Two tickets is two contracts.",
    ]
    return "\n".join(lines)


def _money(value: float | None, currency: str, signed: bool = False) -> str:
    if value is None:
        return "-"
    sign = "+" if signed and value > 0 else ""
    return f"{sign}{value:,.0f}"


def run(args: argparse.Namespace) -> int:
    base = load_config(args.config)
    origin, destination = args.origin.upper(), args.destination.upper()
    hubs = [h.strip().upper() for h in args.hubs.split(",") if h.strip()]
    departures = [date.fromisoformat(d.strip()) for d in args.departures.split(",") if d.strip()]
    if not hubs or not departures:
        log.error("need at least one hub and one departure date")
        return 2

    needed = len(hubs) * len(departures) * 2          # two legs, one search each
    log.info(
        "%s hub(s) x %s departure(s) x 2 legs = %s searches",
        len(hubs), len(departures), needed,
    )
    if needed > args.max_searches:
        log.error(
            "that is %s searches and --max-searches is %s. Cut hubs or dates.",
            needed, args.max_searches,
        )
        return 2

    ok, detail = affordable(base, needed, args.reserve_runs)
    log.info("budget: %s", detail)
    if not ok:
        log.error("refusing to run: the scheduled runs come first.")
        return 3

    history = read_history()
    results: list[dict] = []
    rows: list[dict[str, str]] = []
    spent = 0

    try:
        for departure in departures:
            back = departure + timedelta(days=args.nights)
            # Classified on the last travel date, matching Config.passengers_for:
            # an infant who turns two mid-trip needs a seat for the return leg.
            passengers = base.defaults.passengers.to_passengers(on=back)
            baseline = through_fare(history, origin, destination, departure, passengers)
            for hub in hubs:
                legs: list[float | None] = []
                carriers: list[str] = []
                for start, end in ((origin, hub), (hub, destination)):
                    config = build_leg_config(base, start, end, departure, back)
                    route = config.routes[0]
                    fetcher = SerpApiFetcher(config)
                    try:
                        quotes = fetcher.search(route, passengers)
                    except Exception as exc:      # one leg failing must not end the sweep
                        log.error("%s-%s on %s: %s", start, end, departure, exc)
                        quotes = []
                    finally:
                        spent += fetcher.searches_consumed()
                    best = cheapest(quotes)
                    legs.append(best.total_price if best else None)
                    if best and best.carrier:
                        carriers.append(best.carrier)
                    rows += [quote_to_row(q, start, end) for q in quotes]
                    log.info(
                        "%s-%s %s: %s",
                        start, end, departure,
                        f"{best.total_price:,.0f} on {best.carrier}" if best else "no fare",
                    )
                combo = None if any(leg is None for leg in legs) else sum(legs)
                results.append({
                    "depart": departure, "hub": hub,
                    "leg1": legs[0], "leg2": legs[1],
                    "combo": combo, "through": baseline,
                    "carriers": " + ".join(carriers),
                })
    finally:
        if spent:
            append_usage("serpapi", USAGE_ID, spent, utcnow())
            log.info("recorded %s searches against the plan", spent)

    path = Path(args.out)
    _append(path, PRICE_COLUMNS, rows)
    log.info("wrote %s rows to %s", len(rows), path)
    print(summarise(results, base.defaults.currency))
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--origin", default="DSM")
    parser.add_argument("--destination", default="SJU")
    parser.add_argument(
        "--hubs", default="MCO,FLL,MIA",
        help="comma-separated hub airports to try routing through",
    )
    parser.add_argument(
        "--departures", required=True,
        help="comma-separated departure dates, YYYY-MM-DD",
    )
    parser.add_argument("--nights", type=int, default=7)
    parser.add_argument(
        "--reserve-runs", type=int, default=4,
        help="scheduled runs to leave funded before spending anything here",
    )
    parser.add_argument("--max-searches", type=int, default=24, help="refuse to run past this")
    parser.add_argument("--config", default="config/routes.yaml")
    parser.add_argument("--out", default=str(HUB_SURVEY_PATH))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
