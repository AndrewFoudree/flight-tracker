"""Open a GitHub issue; GitHub emails you the notification.

The preferred path. It uses the workflow's built-in GITHUB_TOKEN, so there are
no SMTP credentials, no app passwords, and nothing to rotate. The issue title
carries the route and price, which makes the email subject line self-contained.
"""

from __future__ import annotations

import logging
import os

import requests

from ..models import Alert

log = logging.getLogger(__name__)

API = "https://api.github.com"
TIMEOUT = 30


class GitHubIssueNotifier:
    name = "github_issue"

    def __init__(self, token: str | None = None, repo: str | None = None, session=None) -> None:
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        self.repo = repo if repo is not None else os.environ.get("GITHUB_REPOSITORY", "")
        self.session = session or requests.Session()

    @property
    def available(self) -> bool:
        return bool(self.token and self.repo)

    def send(self, alert: Alert) -> str | None:
        if not self.available:
            log.warning("github_issue: GITHUB_TOKEN or GITHUB_REPOSITORY missing, skipping")
            return None
        response = self.session.post(
            f"{API}/repos/{self.repo}/issues",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"title": title_for(alert), "body": body_for(alert), "labels": ["price-alert"]},
            timeout=TIMEOUT,
        )
        if response.status_code >= 300:
            log.error("github_issue: HTTP %s: %s", response.status_code, response.text[:300])
            return None
        return response.json().get("html_url")


def title_for(alert: Alert) -> str:
    quote = alert.quote
    return (
        f"{alert.route_id}: {quote.currency} {quote.total_price:,.0f} "
        f"({quote.depart_date:%b %d})"
    )


def body_for(alert: Alert) -> str:
    quote = alert.quote
    lines = [
        f"**{quote.currency} {quote.total_price:,.2f}** total for "
        f"{quote.adults + quote.children} seats.",
        "",
        "| | |",
        "|---|---|",
        f"| Route | `{alert.route_id}` |",
        f"| Depart | {quote.depart_date} |",
        f"| Return | {quote.return_date or 'one way'} |",
        f"| Carrier | {quote.carrier or 'unknown'} |",
        f"| Stops | {'nonstop' if quote.stops == 0 else quote.stops if quote.stops is not None else 'unknown'} |",
        f"| Source | {quote.source} |",
        f"| Observed | {quote.observed_at:%Y-%m-%d %H:%M} UTC |",
    ]
    if alert.previous_price is not None:
        lines.append(f"| Previous check | {quote.currency} {alert.previous_price:,.2f} |")
    if alert.rolling_min is not None:
        lines.append(f"| Cheapest in 30 days | {quote.currency} {alert.rolling_min:,.2f} |")
    lines += ["", "**Why this fired**"]
    lines += [f"- {reason}" for reason in alert.reasons]
    if quote.booking_url:
        lines += ["", f"[Open the search]({quote.booking_url})"]
    lines += [
        "",
        "---",
        "Fares move. Verify on the airline site before booking.",
    ]
    return "\n".join(lines)
