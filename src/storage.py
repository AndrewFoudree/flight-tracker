"""CSV read/append and normalisation.

data/prices.csv is append-only. History is never rewritten; a correction is a
new row with a later timestamp. Git history is the audit log.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from .models import Passengers, Quote

PRICES_PATH = Path("data/prices.csv")
USAGE_PATH = Path("data/usage.csv")
ROUTES_META_PATH = Path("data/routes.json")
RUNS_PATH = Path("data/runs.csv")

PRICE_COLUMNS: Sequence[str] = (
    "observed_at",
    "route_id",
    "source",
    "origin",
    "destination",
    "depart_date",
    "return_date",
    "adults",
    "children",
    "infants",
    "total_price",
    "currency",
    "carrier",
    "stops",
    "booking_url",
)

# API-search accounting lives beside the price history rather than inside it,
# so prices.csv keeps exactly the columns the schema promises.
USAGE_COLUMNS: Sequence[str] = ("observed_at", "source", "route_id", "searches")

# One row per route per run, recording whether a price was actually obtained.
# Kept out of prices.csv on purpose: a null price there would break parsing and
# could leak into the rolling minimum. Here it is an explicit "no data" marker
# the dashboard can draw as a gap.
RUN_COLUMNS: Sequence[str] = ("observed_at", "route_id", "status", "quotes", "note")


@dataclass(frozen=True)
class PriceRow:
    """One parsed row of prices.csv."""

    observed_at: datetime
    route_id: str
    source: str
    origin: str
    destination: str
    depart_date: date
    return_date: date | None
    adults: int
    children: int
    infants: int
    total_price: float
    currency: str
    carrier: str | None
    stops: int | None
    booking_url: str | None

    def is_group(self, passengers: Passengers) -> bool:
        return (
            self.adults == passengers.adults
            and self.children == passengers.children
            and self.infants == passengers.infants
        )

    def is_single_adult(self) -> bool:
        return (self.adults, self.children, self.infants) == (1, 0, 0)


def parse_dt(value: str) -> datetime:
    """Parse an ISO timestamp and force it to UTC. Naive values are assumed UTC."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fmt_dt(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _opt(value: str | None) -> str | None:
    return value if value not in (None, "") else None


def quote_to_row(quote: Quote, origin: str, destination: str) -> dict[str, str]:
    return {
        "observed_at": fmt_dt(quote.observed_at),
        "route_id": quote.route_id,
        "source": quote.source,
        "origin": origin,
        "destination": destination,
        "depart_date": quote.depart_date.isoformat(),
        "return_date": quote.return_date.isoformat() if quote.return_date else "",
        "adults": str(quote.adults),
        "children": str(quote.children),
        "infants": str(quote.infants),
        "total_price": f"{quote.total_price:.2f}",
        "currency": quote.currency,
        "carrier": quote.carrier or "",
        "stops": "" if quote.stops is None else str(quote.stops),
        "booking_url": quote.booking_url or "",
    }


def _append(path: Path, columns: Sequence[str], rows: Iterable[dict[str, str]]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), lineterminator="\n")
        if is_new:
            writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def append_quotes(
    quotes: Iterable[Quote],
    origins: dict[str, str],
    destinations: dict[str, str],
    path: Path = PRICES_PATH,
) -> int:
    """Append quotes to prices.csv. origins/destinations are keyed by route_id."""
    rows = [quote_to_row(q, origins[q.route_id], destinations[q.route_id]) for q in quotes]
    return _append(path, PRICE_COLUMNS, rows)


def read_history(path: Path = PRICES_PATH) -> list[PriceRow]:
    """Parse prices.csv. A malformed row is a hard error, not a silent skip."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    rows: list[PriceRow] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for lineno, raw in enumerate(csv.DictReader(handle), start=2):
            try:
                rows.append(
                    PriceRow(
                        observed_at=parse_dt(raw["observed_at"]),
                        route_id=raw["route_id"],
                        source=raw["source"],
                        origin=raw["origin"],
                        destination=raw["destination"],
                        depart_date=date.fromisoformat(raw["depart_date"]),
                        return_date=(
                            date.fromisoformat(raw["return_date"]) if _opt(raw["return_date"]) else None
                        ),
                        adults=int(raw["adults"]),
                        children=int(raw["children"]),
                        infants=int(raw["infants"]),
                        total_price=float(raw["total_price"]),
                        currency=raw["currency"],
                        carrier=_opt(raw["carrier"]),
                        stops=int(raw["stops"]) if _opt(raw["stops"]) else None,
                        booking_url=_opt(raw["booking_url"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"{path}:{lineno} is malformed: {exc}") from exc
    return rows


def append_usage(
    source: str, route_id: str, searches: int, observed_at: datetime, path: Path = USAGE_PATH
) -> int:
    if searches <= 0:
        return 0
    return _append(
        path,
        USAGE_COLUMNS,
        [
            {
                "observed_at": fmt_dt(observed_at),
                "source": source,
                "route_id": route_id,
                "searches": str(searches),
            }
        ],
    )


def searches_used(source: str, month: datetime, path: Path = USAGE_PATH) -> int:
    """Total searches recorded for a source in the UTC calendar month of `month`."""
    if not path.exists() or path.stat().st_size == 0:
        return 0
    total = 0
    with path.open(newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            if raw["source"] != source:
                continue
            stamp = parse_dt(raw["observed_at"])
            if (stamp.year, stamp.month) == (month.year, month.month):
                total += int(raw["searches"])
    return total


def write_route_metadata(config, path: Path = ROUTES_META_PATH) -> None:
    """Publish what the dashboard needs to read prices.csv without a build step.

    Metadata, not history: it describes the current config and is rewritten each
    run, unlike prices.csv which is only ever appended to.
    """
    payload = []
    for route in config.routes:
        passengers = config.passengers_for(route)
        payload.append(
            {
                "id": route.id,
                "origin": route.origin,
                "destination": route.destination,
                "threshold_usd": route.threshold_usd,
                "currency": config.currency_for(route),
                "adults": passengers.adults,
                "children": passengers.children,
                "infants": passengers.infants,
                "party_size": passengers.party_size,
                "depart": route.depart.isoformat() if route.depart else None,
                "return": route.return_.isoformat() if route.return_ else None,
                "window": (
                    {
                        "earliest": route.depart_window.earliest.isoformat(),
                        "latest": route.depart_window.latest.isoformat(),
                        "nights": route.nights,
                    }
                    if route.depart_window
                    else None
                ),
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + chr(10), encoding="utf-8")


def append_run(
    route_id: str, status: str, quotes: int, note: str, observed_at: datetime,
    path: Path = RUNS_PATH,
) -> int:
    """Record the outcome of one route in one run. `status` is ok or a reason."""
    return _append(
        path,
        RUN_COLUMNS,
        [
            {
                "observed_at": fmt_dt(observed_at),
                "route_id": route_id,
                "status": status,
                "quotes": str(quotes),
                "note": note,
            }
        ],
    )
