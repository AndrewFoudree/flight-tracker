"""Optional SMTP path.

Off unless SMTP_HOST is set. Keep it for the day alerts need to reach somewhere
other than the GitHub account that owns this repo -- otherwise prefer
github_issue, which has no credentials to rotate.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

from ..models import Alert
from .github_issue import body_for, title_for

log = logging.getLogger(__name__)


class EmailNotifier:
    name = "email"

    def __init__(self) -> None:
        self.host = os.environ.get("SMTP_HOST", "")
        self.port = int(os.environ.get("SMTP_PORT", "587"))
        self.username = os.environ.get("SMTP_USERNAME", "")
        self.password = os.environ.get("SMTP_PASSWORD", "")
        self.sender = os.environ.get("ALERT_FROM", self.username)
        self.recipients = [r.strip() for r in os.environ.get("ALERT_TO", "").split(",") if r.strip()]

    @property
    def available(self) -> bool:
        return bool(self.host and self.sender and self.recipients)

    def send(self, alert: Alert) -> str | None:
        if not self.available:
            return None
        message = EmailMessage()
        message["Subject"] = f"Flight alert - {title_for(alert)}"
        message["From"] = self.sender
        message["To"] = ", ".join(self.recipients)
        message.set_content(body_for(alert))
        try:
            with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
                smtp.starttls()
                if self.username:
                    smtp.login(self.username, self.password)
                smtp.send_message(message)
        except OSError as exc:                       # network or auth failure
            log.error("email: send failed: %s", exc)
            return None
        return f"emailed {len(self.recipients)} recipient(s)"
