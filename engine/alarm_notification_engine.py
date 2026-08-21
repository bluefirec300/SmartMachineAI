"""
Phase V2.2 - Alarm Notifications. Pure decision/formatting logic plus
the actual SMTP send, kept separate from app/notification_worker.py's
polling loop so both halves are independently testable without needing
a running worker process or a real mail server.

Severity ranking and email content are both derived entirely from
data app/event_monitor.py already computed and stored in
machine_events - this module invents no new alarm/anomaly logic and
changes no existing calculation.
"""

from __future__ import annotations

import smtplib
from datetime import datetime
from email.message import EmailMessage
from typing import Any

SEVERITY_RANK = {
    "warning": 1,
    "alarm": 2,
}

# An unrecognized severity value is treated as at least as urgent as
# "alarm" - never silently dropped just because it doesn't match the
# two known values.
_UNKNOWN_SEVERITY_RANK = SEVERITY_RANK["alarm"]


def _severity_rank(severity: str) -> int:
    return SEVERITY_RANK.get((severity or "").strip().lower(), _UNKNOWN_SEVERITY_RANK)


def select_notifiable_events(events: list[dict[str, Any]], min_severity: str) -> list[dict[str, Any]]:
    """Events whose severity meets or exceeds min_severity - the only
    filter applied; every other field of the event is passed through
    unchanged."""
    threshold = _severity_rank(min_severity)
    return [event for event in events if _severity_rank(event.get("severity", "")) >= threshold]


def should_notify(
    now: datetime,
    last_notified_at: datetime | None,
    cooldown_minutes: float,
) -> bool:
    """True if this (equipment, tag, condition) has never been
    notified before, or the cooldown window has fully elapsed since
    the last time it was."""
    if last_notified_at is None:
        return True

    elapsed_minutes = (now - last_notified_at).total_seconds() / 60.0
    return elapsed_minutes >= cooldown_minutes


def format_notification_email(event: dict[str, Any]) -> tuple[str, str]:
    """(subject, body) - always states equipment, tag, severity,
    value, condition, and timestamp explicitly and unambiguously,
    reusing the event's own already-grounded message text rather than
    re-deriving a description."""
    equipment = event.get("equipment") or "Unknown equipment"
    tag = event.get("tag") or "Unknown tag"
    severity = str(event.get("severity") or "").upper()
    condition = event.get("condition") or "unknown"
    event_time = event.get("event_time") or "Unknown time"
    message = event.get("message") or ""

    value = event.get("value")
    unit = event.get("unit") or ""
    value_text = "Unavailable" if value is None else f"{value} {unit}".strip()

    subject = f"[SmartFactoryAI] {severity} - {equipment} - {condition}"

    body_lines = [
        f"Equipment: {equipment}",
        f"Tag: {tag}",
        f"Severity: {severity}",
        f"Condition: {condition}",
        f"Value: {value_text}",
        f"Timestamp: {event_time}",
    ]
    if message:
        body_lines.append(f"Message: {message}")
    body_lines.append("")
    body_lines.append(
        "This is an automated notification from SmartFactoryAI. "
        "Manage recipients and notification settings on the Alarm Notification Settings page (admin)."
    )

    return subject, "\n".join(body_lines)


def send_email(
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_use_tls: bool,
    smtp_username: str | None,
    smtp_password: str | None,
    from_address: str,
    recipients: list[str],
    subject: str,
    body: str,
    timeout_seconds: float = 10.0,
) -> tuple[bool, str | None]:
    """
    Sends one email. Returns (success, error_message) - NEVER raises,
    so a network/SMTP failure can never propagate into the caller's
    event-processing loop. error_message is None on success.
    """
    if not smtp_host:
        return False, "smtp_host is not configured"
    if not recipients:
        return False, "no recipients configured"
    if not from_address:
        return False, "smtp_from_address is not configured"

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_address
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=timeout_seconds) as server:
            if smtp_use_tls:
                server.starttls()
            if smtp_username:
                server.login(smtp_username, smtp_password or "")
            server.send_message(message)
        return True, None
    except Exception as error:  # noqa: BLE001 - any SMTP/network failure must be caught, never propagate
        return False, f"{type(error).__name__}: {error}"
