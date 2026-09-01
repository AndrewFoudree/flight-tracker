"""Loads and validates config/routes.yaml.

A typo in an airport code fails here, before any API call is spent.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import Passengers

IATA = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]

# Rule names accepted inside a route's alert_on list.
ALERT_RULES = ("absolute_below", "lowest_in_days", "percent_drop")


class _Strict(BaseModel):
    """Reject unknown keys, so a misspelled option is an error and not a silent no-op."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def turns_two_on(born: date) -> date:
    """The day a child stops being a lap infant.

    A 29 February birthday has no anniversary in a non-leap year; 1 March is the
    conventional treatment, and it errs toward buying a seat.
    """
    try:
        return born.replace(year=born.year + 2)
    except ValueError:                      # 29 February
        return date(born.year + 2, 3, 1)


class PassengerConfig(_Strict):
    """Who is flying.

    `from_env` names an environment variable holding "adults,children,infants",
    so a public repo can describe a six-seat booking without publishing the
    composition of a household. The counts still reach the API in full: a lap
    infant has to be declared, and children price separately from adults.
    """

    adults: int = Field(default=0, ge=0, le=9)
    children: int = Field(default=0, ge=0, le=9)
    infants: int = Field(default=0, ge=0, le=9)
    from_env: str | None = None
    # Optional, but strongly recommended: lap-infant eligibility depends on the
    # flight date, not the booking date. Without it the counts above are taken
    # on trust and a child who has aged out is priced as free.
    infant_birthdates: list[date] = Field(default_factory=list)

    @model_validator(mode="after")
    def _load_from_env(self) -> "PassengerConfig":
        """Fail loudly rather than price a party nobody asked for.

        A missing variable must not fall back to the zeros above: that would
        quietly search for a different trip and record the answer as if it were
        this one.
        """
        if self.from_env is None:
            return self
        raw = os.environ.get(self.from_env, "").strip()
        if not raw:
            raise ValueError(
                f"passengers.from_env names {self.from_env}, which is unset. "
                'Set it to "adults,children,infants" (the repository variable '
                "of that name in Actions, or an env var when running locally)."
            )
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            raise ValueError(
                f"{self.from_env} is {raw!r}; expected three numbers, "
                '"adults,children,infants"'
            )
        adults, children, infants = (int(p) for p in parts)
        object.__setattr__(self, "adults", adults)
        object.__setattr__(self, "children", children)
        object.__setattr__(self, "infants", infants)
        return self

    @model_validator(mode="after")
    def _check_party(self) -> "PassengerConfig":
        if self.adults < 1:
            raise ValueError("a booking needs at least one adult")
        if self.infants > self.adults:
            raise ValueError("infants may not outnumber adults: each lap infant needs an adult lap")
        if self.adults + self.children > 9:
            raise ValueError("most carriers cap a single booking at 9 seated passengers")
        if self.infant_birthdates and len(self.infant_birthdates) != self.infants:
            raise ValueError(
                f"infant_birthdates has {len(self.infant_birthdates)} entries "
                f"but infants is {self.infants}"
            )
        return self

    def to_passengers(self, on: date | None = None) -> Passengers:
        """Passenger split as it applies on `on`.

        An infant who has turned two by then is a child who needs a seat, and is
        counted as one. Without birthdates the configured counts are used as given.
        """
        if on is None or not self.infant_birthdates:
            return Passengers(self.adults, self.children, self.infants)
        aged_out = sum(1 for born in self.infant_birthdates if turns_two_on(born) <= on)
        return Passengers(self.adults, self.children + aged_out, self.infants - aged_out)


class Budget(_Strict):
    serpapi_monthly_searches: int = Field(ge=0)
    reserve: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_reserve(self) -> "Budget":
        if self.reserve >= self.serpapi_monthly_searches:
            raise ValueError("reserve must be smaller than serpapi_monthly_searches")
        return self

    def spendable(self, source: str) -> int | None:
        """Searches allowed this month, or None when the source is unmetered."""
        if source == "serpapi":
            return self.serpapi_monthly_searches - self.reserve
        return None


class Defaults(_Strict):
    passengers: PassengerConfig
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    cabin: Literal["economy", "premium_economy", "business", "first"] = "economy"
    sources: list[str] = Field(min_length=1)
    # A large step is cheap (fewer searches); only a small one costs allowance,
    # and the budget guard handles that.
    window_step_days: int = Field(default=7, ge=1, le=365)
    # A lap infant is free on domestic US routes and typically ~10% of the adult
    # fare plus taxes internationally. Used only by the split-booking estimate.
    infant_fare_pct: float = Field(default=0.0, ge=0, le=100)


class DepartWindow(_Strict):
    earliest: date
    latest: date

    @model_validator(mode="after")
    def _check_order(self) -> "DepartWindow":
        if self.latest < self.earliest:
            raise ValueError("depart_window.latest is before depart_window.earliest")
        return self


class AlertRule(_Strict):
    """One entry of alert_on: exactly one rule key, with its parameter."""

    absolute_below: float | None = Field(default=None, gt=0)
    lowest_in_days: int | None = Field(default=None, gt=0)
    percent_drop: float | None = Field(default=None, gt=0, le=100)

    @model_validator(mode="after")
    def _exactly_one(self) -> "AlertRule":
        chosen = [name for name in ALERT_RULES if getattr(self, name) is not None]
        if len(chosen) != 1:
            raise ValueError(
                "each alert_on entry needs exactly one of "
                + ", ".join(ALERT_RULES)
                + "; got " + (", ".join(chosen) if chosen else "none")
            )
        return self

    @property
    def kind(self) -> str:
        return next(name for name in ALERT_RULES if getattr(self, name) is not None)

    @property
    def value(self) -> float:
        return getattr(self, self.kind)


class Route(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    origin: IATA
    destination: IATA
    depart: date | None = None
    return_: date | None = Field(default=None, alias="return")
    depart_window: DepartWindow | None = None
    nights: int | None = Field(default=None, ge=0, le=365)
    threshold_usd: float = Field(gt=0)
    alert_on: list[AlertRule] = Field(min_length=1)
    compare_split_booking: bool = False
    # Per-route overrides of the defaults block.
    passengers: PassengerConfig | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    cabin: str | None = None
    sources: list[str] | None = Field(default=None, min_length=1)
    window_step_days: int | None = Field(default=None, ge=1, le=365)
    infant_fare_pct: float | None = Field(default=None, ge=0, le=100)

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="after")
    def _check_dates(self) -> "Route":
        if self.origin == self.destination:
            raise ValueError("origin and destination are the same airport")
        fixed = self.depart is not None
        flexible = self.depart_window is not None
        if fixed == flexible:
            raise ValueError(
                "a route needs either depart (fixed dates) or depart_window (flexible), not both"
            )
        if fixed:
            if self.nights is not None:
                raise ValueError("nights belongs with depart_window; use return with depart")
            if self.return_ is not None and self.return_ < self.depart:
                raise ValueError("return is before depart")
        else:
            if self.nights is None:
                raise ValueError("depart_window routes need nights")
            if self.return_ is not None:
                raise ValueError("return belongs with depart; use nights with depart_window")
        return self

    def search_dates(self, default_step_days: int) -> list[tuple[date, date | None]]:
        """The (depart, return) pairs one run should price for this route."""
        if self.depart is not None:
            return [(self.depart, self.return_)]
        window = self.depart_window
        assert window is not None and self.nights is not None
        step = self.window_step_days or default_step_days
        pairs: list[tuple[date, date | None]] = []
        cursor = window.earliest
        while cursor <= window.latest:
            pairs.append((cursor, cursor + timedelta(days=self.nights)))
            cursor += timedelta(days=step)
        # Always price the far edge of the window; the step rarely lands on it.
        if pairs and pairs[-1][0] != window.latest:
            pairs.append((window.latest, window.latest + timedelta(days=self.nights)))
        return pairs


class Config(_Strict):
    defaults: Defaults
    budget: Budget
    routes: list[Route] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_routes(self) -> "Config":
        ids = [r.id for r in self.routes]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate route ids: {duplicates}")
        return self

    # Resolution helpers: per-route value, falling back to defaults.

    def travel_end_date(self, route: Route) -> date:
        """The last day anyone is in the air on this route."""
        if route.depart is not None:
            return route.return_ or route.depart
        window = route.depart_window
        assert window is not None
        return window.latest + timedelta(days=route.nights or 0)

    def passengers_for(self, route: Route) -> Passengers:
        """Passenger split priced for this route.

        Classified on the *last* travel date rather than the departure. A child
        who turns two mid-trip needs a seat for the return, so the trip has to be
        priced with that seat or the quote is not the price you will pay.
        """
        config = route.passengers or self.defaults.passengers
        return config.to_passengers(on=self.travel_end_date(route))

    def infant_notes(self, route: Route) -> list[str]:
        """Human-readable warnings about lap-infant eligibility on this route."""
        config = route.passengers or self.defaults.passengers
        if not config.infant_birthdates:
            return []

        notes: list[str] = []
        first_departure = (
            route.depart if route.depart is not None else route.depart_window.earliest
        )
        last_day = self.travel_end_date(route)
        for born in config.infant_birthdates:
            birthday = turns_two_on(born)
            if birthday <= first_departure:
                notes.append(
                    f"{route.id}: a child born {born} turns two on {birthday}, before "
                    f"departure on {first_departure}. Priced with a seat, not as a lap infant."
                )
            elif birthday <= last_day:
                notes.append(
                    f"{route.id}: a child born {born} turns two on {birthday}, mid-trip "
                    f"(travel ends {last_day}). A lap infant outbound still needs a seat "
                    "on the return, so the whole trip is priced with a seat."
                )
        return notes

    def currency_for(self, route: Route) -> str:
        return route.currency or self.defaults.currency

    def cabin_for(self, route: Route) -> str:
        return route.cabin or self.defaults.cabin

    def infant_fare_pct_for(self, route: Route) -> float:
        return (
            route.infant_fare_pct
            if route.infant_fare_pct is not None
            else self.defaults.infant_fare_pct
        )

    def sources_for(self, route: Route) -> list[str]:
        return list(route.sources or self.defaults.sources)

    def search_dates_for(self, route: Route) -> list[tuple[date, date | None]]:
        return route.search_dates(self.defaults.window_step_days)

    def route_by_id(self, route_id: str) -> Route:
        for route in self.routes:
            if route.id == route_id:
                return route
        raise KeyError(route_id)


def load_config(path: str | Path = "config/routes.yaml") -> Config:
    raw: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} did not parse to a mapping")
    return Config.model_validate(raw)
