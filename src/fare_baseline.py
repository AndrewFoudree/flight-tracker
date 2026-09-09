"""What people actually paid on these city pairs, from the US DOT.

Every other number on the dashboard comes from SerpAPI, so the page can say a
fare is the lowest *we have seen* and nothing more. Five weeks of history and one
survey cannot answer the question that decides a booking: is $2,955 a good price
for DSM-STT, or does that market normally clear lower?

This fills that gap from the Bureau of Transportation Statistics' Airline Origin
and Destination Survey (DB1B), a 10% sample of tickets actually sold. It is
public domain, needs no key, and ships as a quarterly zip -- so unlike every fare
API surveyed in the README it cannot be withdrawn, rate-limited, or provoked into
sending a cease and desist.

What it is not: bookable. DB1B lags a quarter or two, has no month field, and
prorates round trips into two directional records. It is a baseline, not a quote,
and the panel says so. It is deliberately kept out of prices.csv for the same
reason the coverage probe is -- an average of last year's tickets is evidence
about a market, not a price anyone was offered for this trip.

It also answers a question the weekly run structurally cannot. SerpAPI reports
the carriers Google chooses to show; DB1B reports the carriers passengers were
actually ticketed on. A carrier carrying real traffic on a tracked pair that has
never once appeared in prices.csv is worth knowing about, because it is the one
kind of cheap fare the tracker would never find on its own.

Manual, like the coverage probe: run it when a new quarter is released.

    python -m src.fare_baseline --year 2025 --quarter 1

The download is ~90MB and the CSV inside is ~1.8GB, so it is streamed from the
zip and never extracted. Only the summary reaches the repo.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

import requests
import yaml

from .models import utcnow

log = logging.getLogger("fare-baseline")

BASELINE_PATH = Path("data/fare_baseline.json")
PRICES_PATH = Path("data/prices.csv")
ROUTES_PATH = Path("config/routes.yaml")
PREZIP = (
    "https://transtats.bts.gov/PREZIP/"
    "Origin_and_Destination_Survey_DB1BMarket_{year}_{quarter}.zip"
)
TIMEOUT = 600

# DB1B is a 10% sample of tickets, so passenger counts are scaled to estimate the
# market. BTS is migrating this survey to OD40 (40%, monthly); when that lands the
# rate changes and this constant is the one thing that has to move with it.
SAMPLE_RATE = 10

# A carrier under this share of the market is one or two sampled tickets, which
# is an interline oddity or a coding artifact rather than a carrier serving the
# route. The panel calls out carriers the tracker has never seen, and a single
# stray ticket flagged beside a real finding would make that callout worthless.
MIN_CARRIER_SHARE = 0.01

# Only the codes that have appeared on these pairs, plus the low-cost carriers
# worth recognising if they ever do. An unknown code passes through unchanged
# rather than being dropped: a carrier we cannot name is still traffic.
CARRIER_NAMES = {
    "AA": "American", "DL": "Delta", "UA": "United", "WN": "Southwest",
    "AS": "Alaska", "B6": "JetBlue", "F9": "Frontier", "NK": "Spirit",
    "G4": "Allegiant", "SY": "Sun Country", "HA": "Hawaiian", "MX": "Breeze",
}


@dataclass(frozen=True)
class CarrierShare:
    """One ticketing carrier's share of a market."""

    code: str
    name: str
    passengers: int              # estimated, sample scaled by SAMPLE_RATE
    share: float                 # of the pair's estimated passengers
    # Whether this carrier has ever appeared in prices.csv. False is the
    # interesting value: real traffic the weekly run has never once surfaced.
    seen_by_tracker: bool


@dataclass(frozen=True)
class PairBaseline:
    """The fare distribution for one city pair."""

    origin: str
    destination: str
    markets: int                 # directional records in the sample
    sampled_passengers: int
    passengers: int              # estimated
    # Round-trip equivalents: DB1B prices one direction, so these are doubled.
    # Approximate by construction -- one ways and open jaws are in the mix too.
    rt_p10: int
    rt_median: int
    rt_p90: int
    rt_mean: int
    # Every tenth percentile, so the page can place a live fare in the
    # distribution by interpolation instead of only next to the nearest label.
    # The floor moves with every run; baking its percentile in here would be
    # stale by the following Sunday.
    rt_deciles: list[int] = field(default_factory=list)
    carriers: list[CarrierShare] = field(default_factory=list)


def weighted_percentile(pairs: list[tuple[float, float]], p: float) -> float:
    """Passenger-weighted percentile. `pairs` is (fare, passengers)."""
    ordered = sorted(pairs)
    total = sum(w for _, w in ordered)
    running = 0.0
    for value, weight in ordered:
        running += weight
        if running >= total * p:
            return value
    return ordered[-1][0]


def tracked_pairs(path: Path = ROUTES_PATH) -> list[tuple[str, str]]:
    """City pairs the tracker follows, in config order, deduplicated.

    Read straight from the YAML rather than through load_config, which validates
    the whole file and so demands the private PARTY split. A baseline of what a
    market charges is public data about two airports; it has no business
    requiring a household's composition to build, and requiring it would mean
    this could not run without the repository secret.

    Config order is load-bearing everywhere else on this page. It stays so here.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    pairs: list[tuple[str, str]] = []
    for route in raw.get("routes", []):
        pair = (route["origin"], route["destination"])
        if pair not in pairs:
            pairs.append(pair)
    return pairs


def both_directions(pairs: list[tuple[str, str]]) -> set[tuple[str, str]]:
    """DB1B stores each direction separately; a round trip is two records."""
    return {p for origin, dest in pairs for p in ((origin, dest), (dest, origin))}


def carriers_seen() -> set[str]:
    """Carrier names that have ever appeared in prices.csv."""
    if not PRICES_PATH.exists():
        return set()
    with PRICES_PATH.open(newline="", encoding="utf-8") as handle:
        return {row["carrier"] for row in csv.DictReader(handle) if row.get("carrier")}


def previous_quarter(year: int, quarter: int) -> tuple[int, int]:
    return (year - 1, 4) if quarter == 1 else (year, quarter - 1)


def is_published(year: int, quarter: int) -> bool:
    """Whether BTS has released this quarter, asked with a HEAD request."""
    response = requests.head(
        PREZIP.format(year=year, quarter=quarter), timeout=60, allow_redirects=True
    )
    return response.status_code == 200


def latest_published(quarter: int | None = None, back: int = 8) -> tuple[int, int]:
    """The most recent release BTS has actually published.

    Releases run roughly two quarters behind, but the lag is not fixed and one
    can slip. Probing costs a HEAD request per miss and removes the only
    argument a schedule would otherwise have to guess -- which is the difference
    between a quarterly cron that works and one that 404s until someone notices.

    `quarter` pins which quarter to look for, and pinning it is almost always
    what you want. DB1BMarket has no month field, so a quarter is the finest
    grain there is: Q1 is January with February and March, Q2 is April with May
    and June. Taking whatever was published most recently would silently baseline
    a January trip against spring fares, which is a different market. The
    schedule pins Q1 because January is the window being bought.
    """
    today = date.today()
    year, current = today.year, (today.month - 1) // 3 + 1
    if quarter is None:
        for _ in range(back):
            year, current = previous_quarter(year, current)
            if is_published(year, current):
                log.info("latest published release is %s Q%s", year, current)
                return year, current
        raise SystemExit(
            f"No DB1B release found in the {back} quarters before {today}. "
            "Check whether the BTS URL scheme has changed."
        )

    # Pinned: step back a year at a time on that one quarter.
    if quarter >= current:
        year -= 1
    for _ in range(back):
        if is_published(year, quarter):
            log.info("latest published Q%s is %s Q%s", quarter, year, quarter)
            return year, quarter
        year -= 1
    raise SystemExit(
        f"No published Q{quarter} found in the {back} years before {today}. "
        "Check whether the BTS URL scheme has changed."
    )


def download(year: int, quarter: int, cache: Path) -> Path:
    """Fetch the quarterly zip, reusing it if it is already on disk."""
    url = PREZIP.format(year=year, quarter=quarter)
    target = cache / f"db1b_market_{year}q{quarter}.zip"
    if target.exists():
        log.info("using cached %s (%.0fMB)", target.name, target.stat().st_size / 1e6)
        return target
    cache.mkdir(parents=True, exist_ok=True)
    log.info("downloading %s", url)
    with requests.get(url, stream=True, timeout=TIMEOUT) as response:
        if response.status_code == 404:
            raise SystemExit(
                f"BTS has not published {year} Q{quarter} yet. "
                "Releases run about two quarters behind."
            )
        response.raise_for_status()
        with target.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 20):
                handle.write(chunk)
    log.info("saved %s (%.0fMB)", target.name, target.stat().st_size / 1e6)
    return target


def read_markets(zip_path: Path, pairs: set[tuple[str, str]]) -> list[dict]:
    """Stream the CSV out of the zip, keeping only the tracked pairs.

    The member is ~1.8GB, so it is read through the zip rather than extracted.
    """
    kept: list[dict] = []
    scanned = 0
    with zipfile.ZipFile(zip_path) as archive:
        name = next(n for n in archive.namelist() if n.lower().endswith(".csv"))
        with archive.open(name) as raw:
            stream = io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
            for scanned, row in enumerate(csv.DictReader(stream), start=1):
                if (row["Origin"], row["Dest"]) in pairs:
                    kept.append(row)
    log.info("scanned %s market records, kept %s", f"{scanned:,}", len(kept))
    return kept


def summarise(
    rows: list[dict], origin: str, destination: str, seen: set[str]
) -> PairBaseline | None:
    """Collapse one city pair's records into a distribution and a carrier mix."""
    both = {(origin, destination), (destination, origin)}
    # Bulk fares are negotiated group contracts and zero fares are award tickets
    # or data errors; neither is a price a member of the public could have paid.
    usable = [
        r for r in rows
        if (r["Origin"], r["Dest"]) in both
        and r["BulkFare"].strip() in ("0", "0.00", "")
        and float(r["MktFare"] or 0) > 0
    ]
    if not usable:
        return None

    fares = [(float(r["MktFare"]), float(r["Passengers"])) for r in usable]
    sampled = sum(w for _, w in fares)

    by_carrier: dict[str, float] = {}
    for row in usable:
        code = row["TkCarrier"]
        by_carrier[code] = by_carrier.get(code, 0.0) + float(row["Passengers"])

    carriers = []
    for code, weight in sorted(by_carrier.items(), key=lambda kv: -kv[1]):
        if weight / sampled < MIN_CARRIER_SHARE:
            continue
        name = CARRIER_NAMES.get(code, code)
        carriers.append(CarrierShare(
            code=code,
            name=name,
            passengers=round(weight * SAMPLE_RATE),
            share=round(weight / sampled, 4),
            seen_by_tracker=name in seen,
        ))

    return PairBaseline(
        origin=origin,
        destination=destination,
        markets=len(usable),
        sampled_passengers=round(sampled),
        passengers=round(sampled * SAMPLE_RATE),
        rt_p10=round(weighted_percentile(fares, 0.10) * 2),
        rt_median=round(weighted_percentile(fares, 0.50) * 2),
        rt_p90=round(weighted_percentile(fares, 0.90) * 2),
        rt_mean=round(sum(v * w for v, w in fares) / sampled * 2),
        rt_deciles=[round(weighted_percentile(fares, d / 10) * 2) for d in range(1, 10)],
        carriers=carriers,
    )


def build(year: int, quarter: int, cache: Path, routes: Path = ROUTES_PATH) -> dict:
    ordered = tracked_pairs(routes)
    rows = read_markets(download(year, quarter, cache), both_directions(ordered))
    seen = carriers_seen()

    baselines = []
    for origin, destination in ordered:
        summary = summarise(rows, origin, destination, seen)
        if summary is None:
            log.warning("%s-%s: no usable records in %s Q%s", origin, destination, year, quarter)
            continue
        log.info(
            "%s-%s: %s markets, ~%s passengers, RT median $%s",
            origin, destination, summary.markets, summary.passengers, summary.rt_median,
        )
        baselines.append(summary)

    return {
        "source": "bts_db1b_market",
        "release": f"{year} Q{quarter}",
        "sample_rate_pct": 100 // SAMPLE_RATE,
        "generated_at": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pairs": [asdict(b) for b in baselines],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the DOT fare baseline.")
    # Both optional: omit them and the latest published quarter is found by
    # probing, which is what the quarterly schedule does.
    parser.add_argument("--year", type=int)
    parser.add_argument("--quarter", type=int, choices=(1, 2, 3, 4))
    parser.add_argument(
        "--cache", type=Path, default=Path(".cache"),
        help="where to keep the downloaded zip (default: .cache, gitignored)",
    )
    parser.add_argument("--out", type=Path, default=BASELINE_PATH)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.year and args.quarter:
        year, quarter = args.year, args.quarter
    elif args.year:
        parser.error("--year needs --quarter; a year alone names four releases")
    else:
        # --quarter alone means "the newest published one of those", which is
        # how the schedule stays on Q1 without being told the year.
        year, quarter = latest_published(args.quarter)
    payload = build(year, quarter, args.cache)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    log.info("wrote %s", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
