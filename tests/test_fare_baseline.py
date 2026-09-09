"""The baseline's arithmetic and its filters, which are what the panel repeats.

The download is one HTTP request and the CSV is 1.8GB; neither earns a test.
What earns one is everything between: a passenger-weighted percentile that a
plain median would get wrong, the exclusions that decide which tickets count as
a price a member of the public could have paid, and the carrier threshold that
decides whether "never seen by the tracker" is a finding or noise.
"""

from __future__ import annotations

import csv

import pytest

from src.fare_baseline import (
    MIN_CARRIER_SHARE,
    both_directions,
    carriers_seen,
    summarise,
    tracked_pairs,
    weighted_percentile,
)


def market(origin="DSM", dest="STT", fare="400", passengers="1", carrier="AA", bulk="0"):
    """One DB1BMarket record, with only the fields the summariser reads."""
    return {
        "Origin": origin, "Dest": dest, "MktFare": fare,
        "Passengers": passengers, "TkCarrier": carrier, "BulkFare": bulk,
    }


class TestWeightedPercentile:
    def test_weights_by_passengers_not_by_record(self):
        # One cheap record carrying a hundred people outweighs ninety-nine
        # expensive records carrying one each. An unweighted median would report
        # the expensive fare, which is not what the market charged.
        rows = [(200.0, 100.0)] + [(900.0, 1.0)] * 99
        assert weighted_percentile(rows, 0.50) == 200.0

    def test_median_of_a_flat_distribution(self):
        rows = [(float(n), 1.0) for n in range(1, 101)]
        assert weighted_percentile(rows, 0.50) == pytest.approx(50, abs=1)

    def test_extremes_are_reachable(self):
        rows = [(100.0, 1.0), (500.0, 1.0), (900.0, 1.0)]
        assert weighted_percentile(rows, 0.0) == 100.0
        assert weighted_percentile(rows, 1.0) == 900.0


class TestSummarise:
    def test_counts_both_directions_of_the_pair(self):
        # A round trip is two directional records. Reading only one of them
        # would halve the sample and bias it toward whichever direction the
        # cheap inventory happened to sit on.
        rows = [market(), market(origin="STT", dest="DSM")]
        result = summarise(rows, "DSM", "STT", seen=set())
        assert result.markets == 2

    def test_round_trip_equivalent_doubles_the_directional_fare(self):
        result = summarise([market(fare="400")], "DSM", "STT", seen=set())
        assert result.rt_median == 800

    def test_drops_bulk_and_zero_fares(self):
        # Negotiated group contracts and award tickets are not prices anyone
        # could have paid, so they must not drag the distribution down.
        rows = [
            market(fare="400"),
            market(fare="10", bulk="1"),
            market(fare="0"),
        ]
        result = summarise(rows, "DSM", "STT", seen=set())
        assert result.markets == 1
        assert result.rt_median == 800

    def test_returns_none_when_nothing_is_usable(self):
        assert summarise([market(fare="0")], "DSM", "STT", seen=set()) is None

    def test_ignores_other_city_pairs(self):
        rows = [market(), market(origin="DSM", dest="DEN")]
        assert summarise(rows, "DSM", "STT", seen=set()).markets == 1

    def test_scales_the_sample_to_the_market(self):
        result = summarise([market(passengers="7")], "DSM", "STT", seen=set())
        assert result.sampled_passengers == 7
        assert result.passengers == 70

    def test_deciles_run_p10_to_p90_and_ascend(self):
        rows = [market(fare=str(n)) for n in range(100, 1100, 100)]
        deciles = summarise(rows, "DSM", "STT", seen=set()).rt_deciles
        assert len(deciles) == 9
        assert deciles == sorted(deciles)


class TestCarriers:
    def test_flags_a_carrier_the_tracker_has_never_quoted(self):
        # The whole point of the panel's callout: real traffic that has never
        # once reached prices.csv is the one cheap fare the tracker cannot find.
        rows = [market(carrier="AA", passengers="90"), market(carrier="F9", passengers="10")]
        result = summarise(rows, "DSM", "STT", seen={"American"})
        by_name = {c.name: c for c in result.carriers}
        assert by_name["American"].seen_by_tracker is True
        assert by_name["Frontier"].seen_by_tracker is False

    def test_drops_carriers_below_the_noise_threshold(self):
        # A single sampled ticket is an interline artifact, not a carrier
        # serving the route. Flagging it as "unseen" beside a real finding
        # would make the callout worthless.
        rows = [market(carrier="AA", passengers="1000"), market(carrier="99", passengers="1")]
        result = summarise(rows, "DSM", "STT", seen=set())
        assert [c.code for c in result.carriers] == ["AA"]

    def test_threshold_keeps_a_carrier_just_above_it(self):
        rows = [
            market(carrier="AA", passengers="98"),
            market(carrier="F9", passengers="2"),
        ]
        result = summarise(rows, "DSM", "STT", seen=set())
        assert 2 / 100 > MIN_CARRIER_SHARE
        assert [c.code for c in result.carriers] == ["AA", "F9"]

    def test_an_unknown_code_survives_under_its_own_name(self):
        rows = [market(carrier="ZZ", passengers="50"), market(carrier="AA", passengers="50")]
        result = summarise(rows, "DSM", "STT", seen=set())
        assert "ZZ" in {c.name for c in result.carriers}


class TestRouteReading:
    def test_reads_pairs_without_the_private_party_split(self, tmp_path, monkeypatch):
        # config/routes.yaml keeps the household composition in an env var
        # precisely because the file is public. A baseline of what a market
        # charges must not need that secret to build.
        monkeypatch.delenv("PARTY", raising=False)
        path = tmp_path / "routes.yaml"
        path.write_text(
            "defaults:\n"
            "  passengers:\n"
            "    from_env: PARTY\n"
            "routes:\n"
            "  - id: a\n    origin: DSM\n    destination: STT\n"
            "  - id: b\n    origin: DSM\n    destination: SJU\n"
            "  - id: c\n    origin: DSM\n    destination: STT\n",
            encoding="utf-8",
        )
        assert tracked_pairs(path) == [("DSM", "STT"), ("DSM", "SJU")]

    def test_both_directions_expands_each_pair(self):
        assert both_directions([("DSM", "STT")]) == {("DSM", "STT"), ("STT", "DSM")}

    def test_carriers_seen_is_empty_when_there_is_no_history(self, monkeypatch, tmp_path):
        monkeypatch.setattr("src.fare_baseline.PRICES_PATH", tmp_path / "absent.csv")
        assert carriers_seen() == set()

    def test_carriers_seen_reads_the_price_history(self, monkeypatch, tmp_path):
        path = tmp_path / "prices.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["carrier"])
            writer.writeheader()
            writer.writerows([{"carrier": "American"}, {"carrier": "Delta"}, {"carrier": ""}])
        monkeypatch.setattr("src.fare_baseline.PRICES_PATH", path)
        assert carriers_seen() == {"American", "Delta"}
