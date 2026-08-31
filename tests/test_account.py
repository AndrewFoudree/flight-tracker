"""Provider-reported budget. The local ledger is the fallback, not the source."""

from __future__ import annotations

from src.fetchers import account
from tests.conftest import FakeSession


def test_reports_the_searches_left_for_the_billing_cycle():
    session = FakeSession({"plan_name": "Free", "searches_per_month": 250,
                           "plan_searches_left": 37, "total_searches_left": 37})
    assert account.searches_left("key", session) == 37


def test_total_searches_left_wins_over_plan_searches_left():
    """Extra credits live outside the plan allowance, so the total is the truth."""
    session = FakeSession({"plan_searches_left": 0, "total_searches_left": 500})
    assert account.searches_left("key", session) == 500


def test_no_key_returns_none_rather_than_guessing():
    assert account.searches_left("", FakeSession({})) is None


def test_an_http_error_falls_back_instead_of_claiming_zero():
    """Claiming zero would halt the tracker; claiming full would overspend."""
    session = FakeSession({"error": "nope"}, status_code=401)
    assert account.searches_left("key", session) is None


def test_a_response_without_the_field_falls_back():
    assert account.searches_left("key", FakeSession({"plan_name": "Free"})) is None


def test_a_network_failure_falls_back():
    class Broken:
        def get(self, *a, **k):
            raise OSError("connection reset")
    assert account.searches_left("key", Broken()) is None
