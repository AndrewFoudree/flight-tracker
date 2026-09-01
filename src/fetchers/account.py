"""SerpAPI account status.

The local ledger in data/usage.csv counts by UTC calendar month. SerpAPI bills on
the plan's own renewal date, which is not the first of the month, so the two
disagree for most of every cycle -- and the ledger is the optimistic one.

SerpAPI is the authority on how many searches remain. This asks it. The call is
free and does not count against the plan.
"""

from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)

ENDPOINT = "https://serpapi.com/account.json"
TIMEOUT = 20


def searches_left(api_key: str | None = None, session=None) -> int | None:
    """Searches remaining in the current billing cycle, or None if unavailable.

    None means "could not tell", and callers fall back to the local ledger
    rather than assuming an empty or a full budget.
    """
    key = api_key if api_key is not None else os.environ.get("SERPAPI_KEY", "")
    if not key:
        return None
    try:
        response = (session or requests).get(ENDPOINT, params={"api_key": key}, timeout=TIMEOUT)
        if response.status_code != 200:
            log.warning("account: HTTP %s, falling back to the local ledger", response.status_code)
            return None
        body = response.json()
    except Exception as exc:                      # network, JSON, anything
        log.warning("account: %s, falling back to the local ledger", exc)
        return None

    for field in ("total_searches_left", "plan_searches_left"):
        value = body.get(field)
        if isinstance(value, (int, float)):
            # this_month_usage is the provider's own cycle-to-date count. Logging it
            # beside the local ledger is what makes "has the plan renewed yet?"
            # answerable from a run log instead of inferred from arithmetic.
            log.info(
                "account: %s of %s searches left this cycle, %s used so far (%s)",
                int(value), body.get("searches_per_month", "?"),
                body.get("this_month_usage", "?"), body.get("plan_name", "?"),
            )
            return int(value)
    log.warning("account: no searches-left field in the response, using the local ledger")
    return None
