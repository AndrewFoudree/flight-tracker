"""One-off: does Travelpayouts hold any cache for the routes this tracker follows?

Every weekly run spends eight free Travelpayouts calls and gets nothing back --
`success: true` with an empty `data` array. That is the API having no fares
rather than a parsing bug, but "no fares" has several causes that call for
different responses, and a scheduled run cannot tell them apart:

  * **Our query.** These endpoints filter the cache by market, and they are
    asked for a whole month of round trips with no return date. If either is
    the reason, the fix is a few lines in the fetcher.
  * **The horizon.** The cache is built from real user searches, so it may
    simply not reach 2027 yet. Nothing to fix; it fills as departure nears.
  * **The routes.** Nobody searches DSM->STT on Aviasales. Then this source will
    never help these trips and should stop being called a fallback.

The probe separates them by pricing a control route that certainly has traffic
(DSM-DEN) beside the tracked ones, near-term beside the tracked window, in every
query shape -- and comparing all of it against the query production actually
sends:

    a variant returns fares, production does not -> our bug, and it names itself
    control, near     empty in every shape       -> the account, not the routes
    control, near ok; far empty                  -> a horizon limit
    control, far  ok; tracked far empty          -> these routes are not searched

The 2026-09-08 run came back empty in all twelve cells including the near-term
control, with and without `market=us`. That ruled the market filter out and put
the query shape next in line, which is what the shape arms are for.

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


@dataclass(frozen=True)
class Shape:
    """One way of asking for a trip."""

    name: str
    one_way: bool
    dated: bool                  # exact departure date rather than a whole month
    label: str


# The first entry is what src/fetchers/travelpayouts.py actually sends, and the
# rest exist only to be compared against it. Order matters: every other arm is
# read as a difference from SHAPES[0], so it has to stay first.
SHAPES: tuple[Shape, ...] = (
    Shape("round_trip_month", one_way=False, dated=False, label="round trip, whole month"),
    Shape("one_way_month", one_way=True, dated=False, label="one way, whole month"),
    Shape("round_trip_dated", one_way=False, dated=True, label="round trip, exact dates"),
    Shape("one_way_dated", one_way=True, dated=True, label="one way, exact date"),
)
PRODUCTION_SHAPE = SHAPES[0].name
PRODUCTION_MARKET: str | None = None

# Day of the month the dated shapes ask for. Mid-month, so it is never the edge
# of a cache window in either direction.
DATED_DAY = 15


@dataclass(frozen=True)
class Cell:
    """One call: a pair of airports, a horizon, a query shape, a market."""

    origin: str
    destination: str
    month: str                   # the horizon month, e.g. "2026-11"
    departure_at: str            # what was actually sent
    return_at: str | None
    horizon: str                 # "near" or "far"
    shape: str
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

    @property
    def is_production(self) -> bool:
        """Whether this cell reproduces the query the weekly runs send."""
        return self.shape == PRODUCTION_SHAPE and self.market == PRODUCTION_MARKET


def near_month(today: date, days_ahead: int = 60) -> str:
    """A departure month close enough that the cache should hold something."""
    return (today + timedelta(days=days_ahead)).strftime("%Y-%m")


def dates_for(shape: Shape, month: str, nights: int) -> tuple[str, str | None]:
    """What to send as departure_at and return_at for this shape."""
    if not shape.dated:
        return month, None
    departure = date.fromisoformat(f"{month}-{DATED_DAY:02d}")
    if shape.one_way:
        return departure.isoformat(), None
    return departure.isoformat(), (departure + timedelta(days=nights)).isoformat()


def probe(
    fetcher_for, pairs: list[tuple[str, str]], months: dict[str, str],
    control: tuple[str, str], currency: str,
    shapes: tuple[Shape, ...] = SHAPES, markets: tuple[str | None, ...] = (PRODUCTION_MARKET,),
    nights: int = 7,
) -> list[Cell]:
    """Price every pair x horizon x shape x market. One call each, all free."""
    cells: list[Cell] = []
    for market in markets:
        fetcher = fetcher_for(market)
        for shape in shapes:
            for origin, destination in pairs:
                for horizon, month in months.items():
                    departure_at, return_at = dates_for(shape, month, nights)
                    fares: int | None
                    error: str | None = None
                    try:
                        body = fetcher.request(
                            origin=origin,
                            destination=destination,
                            departure_at=departure_at,
                            currency=currency.lower(),
                            one_way=shape.one_way,
                            return_at=return_at,
                        )
                        data = body.get("data")
                        fares = len(data) if isinstance(data, list) else 0
                    except FetcherError as exc:
                        fares, error = None, str(exc)[:200]
                    except Exception as exc:          # network, JSON, anything
                        fares, error = None, f"{type(exc).__name__}: {exc}"[:200]
                    cell = Cell(
                        origin=origin, destination=destination, month=month,
                        departure_at=departure_at, return_at=return_at, horizon=horizon,
                        shape=shape.name, market=market,
                        control=(origin, destination) == control,
                        fares=fares, error=error,
                    )
                    cells.append(cell)
                    log.info(
                        "%s %s %s market=%s: %s",
                        cell.pair, departure_at, shape.name, market or "-",
                        error if error else f"{fares} fare(s)",
                    )
    return cells


def production_cells(cells: list[Cell]) -> list[Cell]:
    """Only the cells that reproduce what the weekly runs send."""
    return [c for c in cells if c.is_production]


def variant_that_helps(cells: list[Cell]) -> Cell | None:
    """A variant returning fares where the live query returns none.

    That is the one outcome that is a bug in this repo rather than a fact about
    the API, so it outranks every other reading.
    """
    baseline = {(c.pair, c.month): c for c in production_cells(cells)}
    for cell in cells:
        if cell.is_production or not cell.ok:
            continue
        twin = baseline.get((cell.pair, cell.month))
        if twin is not None and not twin.ok:
            return cell
    return None


def _fix_for(cell: Cell) -> str:
    """The change that would make production behave like this cell."""
    parts = []
    if cell.shape != PRODUCTION_SHAPE:
        shape = next(s for s in SHAPES if s.name == cell.shape)
        parts.append(
            f"ask as {shape.label} ({cell.shape}) rather than "
            f"{next(s.label for s in SHAPES if s.name == PRODUCTION_SHAPE)}"
        )
    if cell.market != PRODUCTION_MARKET:
        parts.append(f'set DEFAULT_MARKET to "{cell.market}"')
    return " and ".join(parts) or "no change"


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

    helped = variant_that_helps(cells)
    if helped:
        kind = "market_filter" if helped.shape == PRODUCTION_SHAPE else "query_shape"
        return kind, (
            f"{helped.pair} {helped.departure_at} returns {helped.fares} fare(s) as "
            f"{helped.shape}/market={helped.market or '-'} and none as the query the "
            f"weekly runs send. The empty results are our query, not the cache: in "
            f"src/fetchers/travelpayouts.py, {_fix_for(helped)}."
        )

    # Everything below reasons about the live query only. A variant that also
    # found nothing says nothing about coverage.
    live = production_cells(cells)
    control_near = [c for c in live if c.control and c.horizon == "near"]
    control_far = [c for c in live if c.control and c.horizon == "far"]
    tracked_far = [c for c in live if not c.control and c.horizon == "far"]

    if any(c.ok for c in tracked_far):
        return "healthy", (
            "Travelpayouts holds cached fares for the tracked routes in the tracked "
            "window. They reach the dashboard through the normal weekly run, in the "
            "single-adult columns of the pull table."
        )

    if not any(c.ok for c in cells):
        return "no_cache_at_all", (
            "Every call came back empty, in every query shape, including a near-term "
            "control route with real traffic. That is not a fact about these routes: "
            "check whether the token is entitled to this endpoint and whether the "
            "endpoint still serves this plan."
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
        f"  {'Route':<9} {'Departure':<12} {'Shape':<17} {'Market':<7} Result",
    ]
    for c in cells:
        result = f"ERROR {c.error}" if c.error else f"{c.fares} fare(s)"
        flags = "".join(
            [" (control)" if c.control else "", " <- live query" if c.is_production else ""]
        )
        lines.append(
            f"  {c.pair:<9} {c.departure_at:<12} {c.shape:<17} "
            f"{(c.market or '-'):<7} {result}{flags}"
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

    # The production market always leads, because every other arm is only
    # meaningful as a difference from it. Extras are additions, never
    # replacements. "us" was tried on 2026-09-08 and changed nothing, so it is
    # no longer on by default.
    markets: tuple[str | None, ...] = (PRODUCTION_MARKET,) + tuple(
        m.strip().lower() for m in args.markets.split(",") if m.strip()
    )

    months = {"near": args.near or near_month(date.today()), "far": args.far}
    needed = len(pairs) * len(months) * len(SHAPES) * len(markets)
    log.info(
        "%s pair(s) x %s month(s) x %s shape(s) x %s market(s) = %s calls, all unmetered",
        len(pairs), len(months), len(SHAPES), len(markets), needed,
    )
    if needed > args.max_calls:
        log.error("that is %s calls and --max-calls is %s.", needed, args.max_calls)
        return 2

    def fetcher_for(market: str | None) -> TravelpayoutsFetcher:
        return TravelpayoutsFetcher(config, market=market)

    cells = probe(
        fetcher_for, pairs, months, control, currency,
        markets=markets, nights=args.nights,
    )
    name, detail = verdict(cells)

    now = utcnow()
    payload = {
        "source": "travelpayouts",
        "checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "control": f"{control[0]}-{control[1]}",
        "currency": currency,
        "production_shape": PRODUCTION_SHAPE,
        "verdict": name,
        "detail": detail,
        "cells": [asdict(c) | {"is_production": c.is_production} for c in cells],
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
    parser.add_argument(
        "--markets", default="",
        help="extra market codes to try beside the live query, comma separated",
    )
    parser.add_argument(
        "--nights", type=int, default=7,
        help="trip length for the exact-date round-trip shape",
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
