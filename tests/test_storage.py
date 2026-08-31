"""CSV round trips, schema stability, and budget accounting."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src import storage
from src.models import Quote
from tests.conftest import NOW


def a_quote(price: float = 2745.0, route_id: str = "dsm-mco-spring") -> Quote:
    return Quote(
        route_id=route_id,
        source="serpapi",
        observed_at=NOW,
        depart_date=datetime(2027, 3, 14).date(),
        return_date=datetime(2027, 3, 21).date(),
        adults=2,
        children=4,
        infants=1,
        total_price=price,
        price_per_adult=None,
        currency="USD",
        carrier="Delta",
        stops=1,
        booking_url="https://example.invalid/search",
        raw_response_hash="deadbeef12345678",
    )


def test_header_matches_the_documented_schema(tmp_path):
    path = tmp_path / "prices.csv"
    storage.append_quotes([a_quote()], {"dsm-mco-spring": "DSM"}, {"dsm-mco-spring": "MCO"}, path)
    header = path.read_text(encoding="utf-8").splitlines()[0]
    assert header == (
        "observed_at,route_id,source,origin,destination,depart_date,return_date,"
        "adults,children,infants,total_price,currency,carrier,stops,booking_url"
    )


def test_append_never_rewrites_history(tmp_path):
    path = tmp_path / "prices.csv"
    origins, destinations = {"dsm-mco-spring": "DSM"}, {"dsm-mco-spring": "MCO"}
    storage.append_quotes([a_quote(2745)], origins, destinations, path)
    first = path.read_text(encoding="utf-8")
    storage.append_quotes([a_quote(2700)], origins, destinations, path)
    after = path.read_text(encoding="utf-8")
    assert after.startswith(first)                    # the old rows are untouched
    assert len(after.splitlines()) == 3                # header plus two observations


def test_round_trip_preserves_values(tmp_path):
    path = tmp_path / "prices.csv"
    storage.append_quotes([a_quote()], {"dsm-mco-spring": "DSM"}, {"dsm-mco-spring": "MCO"}, path)
    (row,) = storage.read_history(path)
    assert row.total_price == 2745.0
    assert row.origin == "DSM" and row.destination == "MCO"
    assert row.observed_at == NOW
    assert row.observed_at.tzinfo is timezone.utc     # always stored in UTC
    assert row.stops == 1
    assert row.adults == 2 and row.children == 4 and row.infants == 1


def test_one_way_and_unknown_fields_round_trip_as_none(tmp_path):
    path = tmp_path / "prices.csv"
    quote = Quote(**{**a_quote().as_dict(), "return_date": None, "carrier": None, "stops": None,
                     "booking_url": None})
    storage.append_quotes([quote], {"dsm-mco-spring": "DSM"}, {"dsm-mco-spring": "MCO"}, path)
    (row,) = storage.read_history(path)
    assert row.return_date is None and row.carrier is None
    assert row.stops is None and row.booking_url is None


def test_naive_timestamps_are_read_as_utc(tmp_path):
    path = tmp_path / "prices.csv"
    path.write_text(
        ",".join(storage.PRICE_COLUMNS) + "\n"
        "2026-08-31 13:00:00,r,serpapi,DSM,MCO,2027-03-14,,2,4,1,2745.00,USD,,,\n",
        encoding="utf-8",
    )
    (row,) = storage.read_history(path)
    assert row.observed_at == NOW


def test_a_malformed_row_fails_loudly_with_its_line_number(tmp_path):
    path = tmp_path / "prices.csv"
    path.write_text(
        ",".join(storage.PRICE_COLUMNS) + "\n"
        "2026-08-31T13:00:00Z,r,serpapi,DSM,MCO,2027-03-14,,2,4,1,not-a-price,USD,,,\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r":2 is malformed"):
        storage.read_history(path)


def test_missing_file_reads_as_empty(tmp_path):
    assert storage.read_history(tmp_path / "nope.csv") == []


def test_is_single_adult_distinguishes_the_split_booking_probe(tmp_path):
    path = tmp_path / "prices.csv"
    probe = Quote(**{**a_quote(348).as_dict(), "adults": 1, "children": 0, "infants": 0})
    storage.append_quotes(
        [a_quote(), probe], {"dsm-mco-spring": "DSM"}, {"dsm-mco-spring": "MCO"}, path
    )
    party, single = storage.read_history(path)
    assert not party.is_single_adult()
    assert single.is_single_adult()


# --- budget accounting --------------------------------------------------


def test_usage_totals_only_the_current_month(tmp_path):
    path = tmp_path / "usage.csv"
    storage.append_usage("serpapi", "r", 2, NOW, path)
    storage.append_usage("serpapi", "r", 3, NOW - timedelta(days=40), path)
    storage.append_usage("travelpayouts", "r", 9, NOW, path)
    assert storage.searches_used("serpapi", NOW, path) == 2
    assert storage.searches_used("travelpayouts", NOW, path) == 9


def test_zero_searches_are_not_recorded(tmp_path):
    path = tmp_path / "usage.csv"
    assert storage.append_usage("serpapi", "r", 0, NOW, path) == 0
    assert not path.exists()


def test_usage_of_an_untouched_source_is_zero(tmp_path):
    assert storage.searches_used("serpapi", NOW, tmp_path / "nope.csv") == 0
