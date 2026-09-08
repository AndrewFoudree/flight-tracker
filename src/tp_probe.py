"""One-off: does Travelpayouts hold any cache for the routes this tracker follows?

Every weekly run spends eight free Travelpayouts calls and gets nothing back --
`success: true` with an empty `data` array. That is the API having no fares
rather than a parsing bug, but "no fares" has three causes that call for three
different responses, and the runs so far cannot tell them apart:

  * **Our query.** These endpoints filter the cache by market and the fetcher
    sends none. If that is the reason, the fix is one constant.
  * **The horizon.** The cache is built from real user searches, so it may
    simply not reach 2027 yet. Nothing to fix; it fills as departure nears.
  * **The routes.** Nobody searches DSM->STT on Aviasales. Then this source will
    never help these trips and should stop being called a fallback.

The probe separates them by pricing a control route that certainly has traffic
(DSM-DEN) beside the tracked ones, near-term beside the tracked window, with and
without an explicit market:

    control, near     empty  -> our query or the account, not the routes
    control, near ok; far    -> a horizon limit
    control, far  ok; tracked far empty -> these routes are simply not searched

Nothing here spends a SerpAPI search, and nothing is written to prices.csv. A
cached one-adult fare for Denver in October is evidence about an API, not a fare
for a trip being tracked, and mixing the two would put a line on a chart that
nobody was ever quoted. The findings go to data/source_probe.json, which the
dashboard renders as a coverage panel.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

from .config import load_config
from .fetchers.base import FetcherError
from .fetchers.travelpayouts import TravelpayoutsFetcher
from .models import utcnow
from .storage import append_usage

log = logging.getLogger("tp-probe")

PROBE_PATH = Path("data/source_probe.json")
USAGE_ID = "source-probe"

# Markets to try. None reproduces production exactly and must stay first, so a
# market that works is always visible as a difference against the live query.
MARKETS: tuple[str | None, ...] = (None, "us")


@dataclass(frozen=True)
class Cell:
    """One call: a pair of airports, a departure month, a market."""

    origin: str
    destination: str
    departure_at: str
    horizon: str                 # "near" or "far"
    market: str | None
    control: bool
    fares: int | None            # None when the call failed
    error: str | None

    @property
    def pair(self) -> str:
        return f"{self.origin}-{self.destination}"

    @property
    def ok(self) -> bool:
        return bool(self.fares)


def near_month(today: date, days_ahead: int = 60) -> str:
    """A departure month close enough that the cache should hold something."""
    return (today + timedelta(days=days_ahead)).strftime("%Y-%m")


def probe(
    fetcher_for, pairs: list[tuple[str, str]], months: dict[str, str],
    control: tuple[str, str], currency: str,
) -> list[Cell]:
    """Price every pair x month x market. One call each, all of them free."""
    cells: list[Cell] = []
    for market in MARKETS:
        fetcher = fetcher_for(market)
        for origin, destination in pairs:
            for horizon, month in months.items():
                fares: int | None
                error: str | None = None
                try:
                    body = fetcher.request(
                        origin=origin,
                        destination=destination,
                        departure_at=month,
                        currency=currency.lower(),
                        one_way=False,
                    )
                    data = body.get("data")
                    fares = len(data) if isinstance(data, list) else 0
                except FetcherError as exc:
                    fares, error = None, str(exc)[:200]
                except Exception as exc:              # network, JSON, anything
                    fares, error = None, f"{type(exc).__name__}: {exc}"[:200]
                cell = Cell(
                    origin=origin, destination=destination, departure_at=month,
                    horizon=horizon, market=market, control=(origin, destination) == control,
                    fares=fares, error=error,
                )
                cells.append(cell)
                log.info(
                    "%s %s market=%s: %s",
                    cell.pair, month, market or "-",
                    error if error else f"{fares} fare(s)",
                )
    return cells


def market_that_helps(cells: list[Cell]) -> str | None:
    """A market returning fares where the live query (no market) returns none.

    That is the one outcome that is a bug in this repo rather than a fact about
    the API, so it outranks every other reading.
    """
    baseline = {
        (c.pair, c.departure_at): c for c in cells if c.market is None
    }
    for cell in cells:
        if cell.market is None or not cell.ok:
            continue
        twin = baseline.get((cell.pair, cell.departure_at))
        if twin is not None and not twin.ok:
            return cell.market
    return None


def verdict(cells: list[Cell]) -> tuple[str, str]:
    """A machine-readable verdict and the sentence the dashboard shows."""
    if not cells:
        return "no_data", "The probe made no calls."

    if all(c.fares is None for c in cells):
        first = next(c.error for c in cells if c.error)
        return "unreachable", (
            f"Travelpayouts did not answer any call, so nothing can be concluded "
            f"about coverage. First error: {first}"
        )

    helped = market_that_helps(cells)
    if helped:
        return "market_filter", (
            f"Fares appear when the query sends market={helped} and vanish without it. "
            f"The empty results are our query, not the cache: set DEFAULT_MARKET "
            f'in src/fetchers/travelpayouts.py to "{helped}".'
        )

    control_near = [c for c in cells if c.control and c.horizon == "near"]
    control_far = [c for c in cells if c.control and c.horizon == "far"]
    tracked_far = [c for c in cells if not c.control and c.horizon == "far"]

    if any(c.ok for c in tracked_far):
        return "healthy", (
            "Travelpayouts holds cached fares for the tracked routes in the tracked "
            "window. They reach the dashboard through the normal weekly run, in the "
            "single-adult columns of the pull table."
        )

    if not any(c.ok for c in cells):
        return "no_cache_at_all", (
            "Every call came back empty, including a near-term control route with real "
            "traffic. That points at the query or the account rather than at these "
            "routes: check the token and whether the endpoint still serves this plan."
        )

    if any(c.ok for c in control_near) and not any(c.ok for c in control_far):
        return "horizon", (
            "The control route returns fares near-term and nothing in the tracked "
            "window, so the cache does not reach that far ahead. Nothing to fix. It "
            "may fill as the departure dates approach; leave the source enabled."
        )

    if any(c.ok for c in control_far) and not any(c.ok for c in tracked_far):
        return "route_thin", (
            "The control route returns fares in the tracked window and the tracked "
            "routes return none, so the cache reaches these dates but nobody searches "
            "these routes. Travelpayouts will not serve as a fallback here."
        )

    return "mixed", (
        "Coverage is partial and does not fit a single explanation. Read the table "
        "below before changing anything."
    )


def summarise(cells: list[Cell], name: str, detail: str) -> str:
    lines = [
        "",
        "Travelpayouts cache coverage:",
        "",
        f"  {'Route':<9} {'Departure':<9} {'Market':<7} {'Horizon':<8} Result",
    ]
    for c in cells:
        result = f"ERROR {c.error}" if c.error else f"{c.fares} fare(s)"
        flag = " (control)" if c.control else ""
        lines.append(
            f"  {c.pair:<9} {c.departure_at:<9} {(c.market or '-'):<7} "
            f"{c.horizon:<8} {result}{flag}"
        )
    lines += ["", f"Verdict: {name}", "", detail, ""]
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    currency = config.defaults.currency

    pairs: list[tuple[str, str]] = []
    for raw in args.pairs.split(","):
        raw = raw.strip().upper()
        if not raw:
            continue
        if "-" not in raw:
            log.error("pair %r is not ORIGIN-DESTINATION", raw)
            return 2
        origin, destination = raw.split("-", 1)
        pairs.append((origin, destination))
    if not pairs:
        log.error("need at least one ORIGIN-DESTINATION pair")
        return 2

    control_raw = args.control.strip().upper()
    control = tuple(control_raw.split("-", 1)) if "-" in control_raw else ("", "")
    if control not in pairs:
        log.error("--control %s is not in --pairs; the verdict needs it", args.control)
        return 2

    months = {"near": args.near or near_month(date.today()), "far": args.far}
    needed = len(pairs) * len(months) * len(MARKETS)
    log.info(
        "%s pair(s) x %s month(s) x %s market(s) = %s calls, all unmetered",
        len(pairs), len(months), len(MARKETS), needed,
    )
    if needed > args.max_calls:
        log.error("that is %s calls and --max-calls is %s.", needed, args.max_calls)
        return 2

    def fetcher_for(market: str | None) -> TravelpayoutsFetcher:
        return TravelpayoutsFetcher(config, market=market)

    cells = probe(fetcher_for, pairs, months, control, currency)
    name, detail = verdict(cells)

    now = utcnow()
    payload = {
        "source": "travelpayouts",
        "checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "control": f"{control[0]}-{control[1]}",
        "currency": currency,
        "verdict": name,
        "detail": detail,
        "cells": [asdict(c) for c in cells],
    }
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    log.info("wrote %s", path)

    # Travelpayouts is unmetered, but the ledger is how spend is read back, so a
    # manual probe belongs in it beside the scheduled runs.
    made = sum(1 for c in cells if c.fares is not None or c.error)
    append_usage("travelpayouts", USAGE_ID, made, now)

    print(summarise(cells, name, detail))
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--pairs", default="DSM-DEN,DSM-STT,DSM-SJU",
        help="comma-separated ORIGIN-DESTINATION pairs to probe",
    )
    parser.add_argument(
        "--control", default="DSM-DEN",
        help="the pair with known traffic, used as the reference for the verdict",
    )
    parser.add_argument(
        "--near", default="",
        help="near-term departure month YYYY-MM (default: about two months out)",
    )
    parser.add_argument(
        "--far", default="2027-01",
        help="the tracked departure month YYYY-MM",
    )
    parser.add_argument("--max-calls", type=int, default=24, help="refuse to run past this")
    parser.add_argument("--config", default="config/routes.yaml")
    parser.add_argument("--out", default=str(PROBE_PATH))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
