"""What a manual sweep may spend without starving the scheduled runs.

The weekly check is the thing this repo exists to run. Every other entry point --
the year survey, the hub survey, whatever gets added next -- spends from the same
SerpAPI allowance, and spending it is not free: it can leave the tracker blind
for the rest of a billing cycle.

That is not hypothetical. On 2026-08-31 a year sweep took 192 of the plan's 250
searches; the cycle never recovered, and 2026-09-06 recorded NA on all six routes
because 6 searches remained against a reserve of 20. Nothing refused that spend,
because nothing asked what it would cost the runs already scheduled.

So a manual sweep asks two questions first: how much is left, and how much of
that belongs to the weekly runs between now and the renewal.
"""

from __future__ import annotations

from .config import Config
from .fetchers.account import searches_left


def per_run_cost(config: Config) -> int:
    """What one scheduled run of the live config spends on SerpAPI."""
    return sum(
        len(config.search_dates_for(route)) * (2 if route.compare_split_booking else 1)
        for route in config.routes
    )


def affordable(config: Config, needed: int, reserve_runs: int) -> tuple[bool, str]:
    """Whether this sweep can run without starving the scheduled runs.

    The plan balance is the provider's, not the local ledger's, because the
    ledger counts calendar months while SerpAPI bills on its own renewal date.
    A sweep that cannot see the balance refuses rather than guesses: this is a
    manual spend against a budget the weekly runs depend on.
    """
    left = searches_left()
    if left is None:
        return False, (
            "could not read the plan balance from SerpAPI, so the cost to the "
            "scheduled runs is unknown. Re-run when the account endpoint answers."
        )
    protected = per_run_cost(config) * reserve_runs + config.budget.reserve
    spare = left - protected
    detail = (
        f"{left} searches left, {protected} protected "
        f"({reserve_runs} scheduled run(s) plus a {config.budget.reserve} reserve), "
        f"so {spare} spare against {needed} needed"
    )
    if needed > spare:
        return False, f"not enough headroom: {detail}."
    return True, detail
