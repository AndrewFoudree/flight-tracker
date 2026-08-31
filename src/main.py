"""Entrypoint: fetch, store, analyse, alert.

Run by .github/workflows/check-prices.yml, or by hand:

    python -m src.main --dry-run          # re-analyse stored history, no API calls
    python -m src.main --route dsm-mco-spring
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import alerting, analysis
from .config import Config, Route, load_config
from .fetchers.account import searches_left
from .fetchers.base import Fetcher
from .fetchers.registry import build_fetchers
from .models import Alert, Passengers, Quote, utcnow
from .notify.email import EmailNotifier
from .notify.github_issue import GitHubIssueNotifier
from .storage import (
    PRICES_PATH,
    USAGE_PATH,
    PriceRow,
    append_quotes,
    append_usage,
    read_history,
    searches_used,
    write_route_metadata,
)

log = logging.getLogger("flight-tracker")


class Budget:
    """Stops the run before the monthly cap.

    Otherwise the cap gets discovered on the day a fare drops.
    """

    def __init__(self, config: Config, now, usage_path: Path = USAGE_PATH) -> None:
        self.config = config
        self.remaining: dict[str, int | None] = {}
        for source in {s for r in config.routes for s in config.sources_for(r)}:
            allowance = config.budget.spendable(source)
            if allowance is None:
                self.remaining[source] = None          # unmetered
                continue
            # Ask the provider first. The local ledger counts by UTC calendar
            # month, but the plan renews on its own anniversary date, so the
            # ledger reads empty at the start of a month while the real cycle
            # still has most of its spend booked.
            reported = searches_left() if source == "serpapi" else None
            if reported is not None:
                self.remaining[source] = reported - config.budget.reserve
                self.authoritative = True
            else:
                self.remaining[source] = allowance - searches_used(source, now, usage_path)

    def can_afford(self, source: str, cost: int) -> bool:
        left = self.remaining.get(source)
        return left is None or left >= cost

    def spend(self, source: str, used: int) -> None:
        left = self.remaining.get(source)
        if left is not None:
            self.remaining[source] = left - used

    authoritative = False

    def summary(self) -> str:
        source_note = " (from the provider)" if self.authoritative else " (from the local ledger)"
        parts = [
            f"{source}={'unmetered' if left is None else left}"
            for source, left in sorted(self.remaining.items())
        ]
        return "searches left this cycle" + source_note + ": " + ", ".join(parts)


def _prices_passengers(fetcher: Fetcher) -> bool:
    """Travelpayouts already quotes a single adult; probing it again is waste."""
    return fetcher.name != "travelpayouts"


def collect(
    config: Config, budget: Budget, routes: list[Route]
) -> tuple[list[Quote], dict[str, dict[str, int]]]:
    """Fan out across every configured source.

    One source failing must not kill the run, so each fetcher is caught
    individually and the others carry on.
    """
    quotes: list[Quote] = []
    usage: dict[str, dict[str, int]] = {}

    for route in routes:
        passengers = config.passengers_for(route)
        fetchers = build_fetchers(config, config.sources_for(route))
        for source, fetcher in fetchers.items():
            searches = [(passengers, "party")]
            # Fare buckets: a seven-seat search only returns fares with seven
            # seats in one bucket. A single-adult probe exposes the cheap seats.
            if route.compare_split_booking and _prices_passengers(fetcher):
                searches.append((Passengers.single_adult(), "single-adult probe"))

            for party, label in searches:
                cost = fetcher.estimate_searches(route)
                if not budget.can_afford(source, cost):
                    log.warning(
                        "%s/%s: skipping %s, needs %s searches and the monthly budget is spent",
                        route.id, source, label, cost,
                    )
                    continue
                spent_before = fetcher.searches_consumed()
                try:
                    found = fetcher.search(route, party)
                except Exception as exc:               # never let one source end the run
                    log.error("%s/%s: %s failed: %s", route.id, source, label, exc)
                    found = []
                spent = fetcher.searches_consumed() - spent_before
                budget.spend(source, spent)
                usage.setdefault(source, {}).setdefault(route.id, 0)
                usage[source][route.id] += spent
                log.info(
                    "%s/%s: %s returned %s quotes for %s search(es)",
                    route.id, source, label, len(found), spent,
                )
                quotes.extend(found)
    return quotes, usage


def best_group_quote(quotes: list[Quote], route_id: str, passengers: Passengers) -> Quote | None:
    """Cheapest whole-party quote for a route across all sources this run."""
    candidates = [q for q in quotes if q.route_id == route_id and q.is_group(passengers)]
    return min(candidates, key=lambda q: q.total_price, default=None)


def report_split_booking(
    quotes: list[Quote], route: Route, passengers: Passengers, infant_fare_pct: float = 0.0
) -> None:
    group = best_group_quote(quotes, route.id, passengers)
    singles = [
        q for q in quotes
        if q.route_id == route.id and (q.adults, q.children, q.infants) == (1, 0, 0)
    ]
    if not group or not singles:
        return
    cheapest_single = min(singles, key=lambda q: q.total_price)
    delta = analysis.split_booking_delta(
        group.total_price,
        cheapest_single.total_price,
        seats=passengers.seated,
        infants=passengers.infants,
        infant_fare_pct=infant_fare_pct,
    )
    log.info(
        "%s: party fare %s %.0f vs %s separate fares at %s %.0f = %s %.0f",
        route.id, group.currency, group.total_price, passengers.seated,
        group.currency, cheapest_single.total_price,
        group.currency, delta["estimated_split_total"],
    )
    if delta["delta"] > 0:
        log.info(
            "%s: booking separately may save about %s %.0f (%.0f%%), worth checking by hand",
            route.id, group.currency, delta["delta"], delta["percent_cheaper_split"],
        )


def notify_all(alert: Alert) -> list[str]:
    sent: list[str] = []
    for notifier in (GitHubIssueNotifier(), EmailNotifier()):
        if not notifier.available:
            continue
        result = notifier.send(alert)
        if result:
            sent.append(f"{notifier.name}: {result}")
    return sent


def run(args: argparse.Namespace) -> int:
    now = utcnow()
    config = load_config(args.config)
    routes = [r for r in config.routes if not args.route or r.id in args.route]
    if not routes:
        log.error("no route matched %s", args.route)
        return 2

    prices_path = Path(args.prices)
    write_route_metadata(config)          # keeps the dashboard build-step free
    history: list[PriceRow] = read_history(prices_path)
    log.info("loaded %s historical rows from %s", len(history), prices_path)

    quotes: list[Quote] = []
    # Built even for a dry run: reading the provider's remaining balance is free
    # and does not count against the plan, so checking the budget should never
    # cost a search.
    budget = Budget(config, now)
    log.info(budget.summary())

    if args.dry_run:
        log.info("dry run: no API calls, analysing stored history only")
    else:
        quotes, usage = collect(config, budget, routes)
        origins = {r.id: r.origin for r in config.routes}
        destinations = {r.id: r.destination for r in config.routes}
        written = append_quotes(quotes, origins, destinations, prices_path)
        for source, per_route in usage.items():
            for route_id, count in per_route.items():
                append_usage(source, route_id, count, now)
        log.info("wrote %s rows to %s", written, prices_path)
        # Re-read so analysis sees exactly what was persisted, not in-memory objects.
        history = read_history(prices_path)

    state = alerting.load_state()
    alerts_fired = 0

    for note in (n for r in routes for n in config.infant_notes(r)):
        log.warning(note)

    for route in routes:
        passengers = config.passengers_for(route)
        route_history = analysis.route_rows(
            history, route.id, passengers=passengers,
            itineraries=set(config.search_dates_for(route)),
        )
        quote = best_group_quote(quotes, route.id, passengers)
        if quote is None:
            log.info("%s: no whole-party quote this run", route.id)
            continue

        report_split_booking(quotes, route, passengers, config.infant_fare_pct_for(route))
        moving = analysis.trend(route_history, now)
        log.info(
            "%s: %s %.0f (7d avg %s, 30d avg %s)",
            route.id, quote.currency, quote.total_price,
            f"{moving['ma_7']:.0f}" if moving["ma_7"] else "n/a",
            f"{moving['ma_30']:.0f}" if moving["ma_30"] else "n/a",
        )

        alert, why = alerting.evaluate(route, quote, route_history, state, now)
        if alert is None:
            log.info("%s: no alert (%s)", route.id, why)
            continue
        log.info("%s: ALERT (%s): %s", route.id, why, "; ".join(alert.reasons))
        if args.no_notify:
            log.info("%s: --no-notify set, nothing sent", route.id)
        else:
            for result in notify_all(alert):
                log.info("%s: notified via %s", route.id, result)
        alerting.record(state, alert, now)
        alerts_fired += 1

    alerting.save_state(state)
    log.info("done: %s quotes this run, %s alert(s)", len(quotes), alerts_fired)

    if not args.dry_run and not quotes:
        # Silence is the dangerous failure: the dashboard flatlines and looks
        # like stable prices. Going red gets GitHub to email it, with no extra
        # credentials needed. But a spent budget is a state we chose, not a
        # malfunction, so it warns without crying wolf.
        if not budget.can_afford("serpapi", 1):
            log.warning(
                "no quotes: the SerpAPI budget is spent for this cycle. "
                "The tracker resumes automatically when the plan renews."
            )
            return 0
        log.error("no quotes collected from any source: the tracker is blind")
        return 1
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check flight prices and alert on drops.")
    parser.add_argument("--config", default="config/routes.yaml")
    parser.add_argument("--prices", default=str(PRICES_PATH))
    parser.add_argument("--route", action="append", help="limit to one route id (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="analyse stored history, call nothing")
    parser.add_argument("--no-notify", action="store_true", help="evaluate alerts but send nothing")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S"
    )
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
