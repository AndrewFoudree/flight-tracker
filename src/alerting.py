"""Threshold logic and de-duplication.

The failure mode this is built against: a fare sits below threshold for two
weeks and you receive fourteen identical emails.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import analysis
from .config import Route
from .models import Alert, Passengers, Quote
from .storage import PriceRow, fmt_dt, parse_dt

STATE_PATH = Path("data/alert_state.json")

DEFAULT_COOLDOWN_DAYS = 7
# An alert already sent is repeated early only if the fare falls this much further.
FURTHER_DROP_PCT = 5.0


@dataclass
class AlertState:
    last_alerted_at: datetime
    last_alerted_price: float
    cooldown_days: int = DEFAULT_COOLDOWN_DAYS

    def to_json(self) -> dict:
        return {
            "last_alerted_at": fmt_dt(self.last_alerted_at),
            "last_alerted_price": round(self.last_alerted_price, 2),
            "cooldown_days": self.cooldown_days,
        }

    @classmethod
    def from_json(cls, raw: dict) -> "AlertState":
        return cls(
            last_alerted_at=parse_dt(raw["last_alerted_at"]),
            last_alerted_price=float(raw["last_alerted_price"]),
            cooldown_days=int(raw.get("cooldown_days", DEFAULT_COOLDOWN_DAYS)),
        )


def load_state(path: Path = STATE_PATH) -> dict[str, AlertState]:
    if not path.exists() or path.stat().st_size == 0:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {route_id: AlertState.from_json(value) for route_id, value in raw.items()}


def save_state(state: dict[str, AlertState], path: Path = STATE_PATH) -> None:
    """Written in the same commit as the CSV, so state and history never diverge."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {route_id: value.to_json() for route_id, value in sorted(state.items())}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def triggered_reasons(
    route: Route,
    price: float,
    history: list[PriceRow],
    now: datetime,
) -> list[str]:
    """Which of the route's alert_on conditions the current price satisfies."""
    reasons: list[str] = []
    previous = analysis.previous_observation(history, now)
    previous_price = previous.total_price if previous else None

    for rule in route.alert_on:
        if rule.kind == "absolute_below":
            if price < rule.value:
                reasons.append(f"below threshold: {price:,.0f} < {rule.value:,.0f}")
        elif rule.kind == "lowest_in_days":
            days = int(rule.value)
            if analysis.is_lowest_in_days(price, history, days, now):
                reasons.append(f"lowest price in {days} days")
        elif rule.kind == "percent_drop":
            drop = analysis.percent_drop(price, previous_price)
            if drop is not None and drop >= rule.value:
                reasons.append(
                    f"down {drop:.1f}% since the previous check ({previous_price:,.0f})"
                )
    return reasons


def should_notify(state: AlertState | None, price: float, now: datetime) -> tuple[bool, str]:
    """Cooldown gate. Returns (send, why) so the run log explains its silence."""
    if state is None:
        return True, "first alert for this route"
    elapsed = now - state.last_alerted_at
    if elapsed >= timedelta(days=state.cooldown_days):
        return True, f"cooldown of {state.cooldown_days} days has elapsed"
    if state.last_alerted_price > 0:
        further = (state.last_alerted_price - price) / state.last_alerted_price * 100.0
        if further >= FURTHER_DROP_PCT:
            return True, (
                f"a further {further:.1f}% below the last alerted price "
                f"({state.last_alerted_price:,.0f})"
            )
    remaining = timedelta(days=state.cooldown_days) - elapsed
    return False, f"suppressed: {remaining.days}d {remaining.seconds // 3600}h of cooldown left"


def evaluate(
    route: Route,
    quote: Quote,
    history: list[PriceRow],
    state: dict[str, AlertState],
    now: datetime,
    passengers: Passengers | None = None,
) -> tuple[Alert | None, str]:
    """Decide whether this quote deserves a notification.

    `history` must already be filtered to whole-party rows for this route.
    """
    reasons = triggered_reasons(route, quote.total_price, history, now)
    if not reasons:
        return None, "no trigger condition met"

    send, why = should_notify(state.get(route.id), quote.total_price, now)
    if not send:
        return None, why

    previous = analysis.previous_observation(history, now)
    alert = Alert(
        route_id=route.id,
        reasons=reasons,
        quote=quote,
        previous_price=previous.total_price if previous else None,
        rolling_min=analysis.rolling_minimum(analysis.before(history, now), 30, now),
    )
    return alert, why


def record(state: dict[str, AlertState], alert: Alert, now: datetime) -> None:
    existing = state.get(alert.route_id)
    state[alert.route_id] = AlertState(
        last_alerted_at=now,
        last_alerted_price=alert.quote.total_price,
        cooldown_days=existing.cooldown_days if existing else DEFAULT_COOLDOWN_DAYS,
    )


def utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)
