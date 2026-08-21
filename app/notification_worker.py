from __future__ import annotations

import fcntl
import os
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.event_store import EventStore
from ai.notification_log import NotificationLog
from config.config_manager import ConfigManager
from config.environment import get_machine_db_path
from engine.alarm_notification_engine import (
    format_notification_email,
    select_notifiable_events,
    send_email,
    should_notify,
)

"""
Phase V2.2 - Alarm Notifications worker. A separate systemd service
from event_monitor.service on purpose: event_monitor.py owns writing
machine_events and must never be affected by an email/SMTP failure
(item 8's explicit requirement) - running notifications in a wholly
separate process makes that structural, not just defensive try/except.

Polls for new machine_events rows via EventStore.get_events_since_id()
(a plain id cursor, same "read what's already there" approach as
every other worker's polling loop - see CLAUDE.md's Background
services section) rather than adding any new hook into
app/event_monitor.py's insert path.
"""

TICK_INTERVAL_SECONDS_FALLBACK = 30.0  # overridden by config.notification_poll_interval_seconds

LOCK_FILE_PATH = PROJECT_ROOT / "logs" / "notification_worker.lock"
CURSOR_MARKER_PATH = PROJECT_ROOT / "logs" / "notification_last_event_id.txt"


def _acquire_single_instance_lock():
    LOCK_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(LOCK_FILE_PATH, "w")

    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(
            f"Another notification_worker instance already holds {LOCK_FILE_PATH} - "
            "refusing to start a second one. Exiting."
        )
        sys.exit(1)

    return lock_file


def _read_cursor(marker_path: Path = CURSOR_MARKER_PATH) -> int:
    if not marker_path.exists():
        return 0
    try:
        return int(marker_path.read_text(encoding="utf-8").strip() or "0")
    except ValueError:
        return 0


def _write_cursor(value: int, marker_path: Path = CURSOR_MARKER_PATH) -> None:
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(str(int(value)), encoding="utf-8")


def run_cycle(
    event_store: EventStore,
    notification_log: NotificationLog,
    config: ConfigManager,
    marker_path: Path = CURSOR_MARKER_PATH,
    now: datetime | None = None,
) -> dict[str, int | str]:
    now = now or datetime.now()

    if not config.notifications_enabled:
        return {"skipped": "disabled"}

    since_id = _read_cursor(marker_path)
    events = event_store.get_events_since_id(since_id, limit=500)

    if not events:
        return {"checked": 0}

    notifiable = select_notifiable_events(events, config.notification_min_severity)

    smtp_username = os.getenv("SMTP_USERNAME")
    smtp_password = os.getenv("SMTP_PASSWORD")
    recipients = config.notification_recipients

    sent = 0
    skipped_cooldown = 0
    failed = 0
    failed_ids: list[int] = []

    for event in notifiable:
        equipment = event.get("equipment", "")
        tag = event.get("tag", "")
        condition = event.get("condition", "")
        severity = event.get("severity", "")
        event_id = event.get("id")

        last_notified_at = notification_log.get_last_notified_at(equipment, tag, condition)

        if not should_notify(now, last_notified_at, config.notification_cooldown_minutes):
            skipped_cooldown += 1
            continue

        subject, body = format_notification_email(event)

        try:
            ok, error = send_email(
                smtp_host=config.smtp_host,
                smtp_port=config.smtp_port,
                smtp_use_tls=config.smtp_use_tls,
                smtp_username=smtp_username,
                smtp_password=smtp_password,
                from_address=config.smtp_from_address,
                recipients=recipients,
                subject=subject,
                body=body,
            )
        except Exception as exc:  # noqa: BLE001 - a bug in send_email itself must still never crash this loop
            # Deliberately NOT "as error" - Python implicitly deletes an
            # `except X as name:` binding at the end of the block, so
            # reassigning the same name here would raise UnboundLocalError
            # on the very next line that reads it (caught by this phase's
            # own tests before this ever shipped).
            ok, error = False, str(exc)

        if ok:
            notification_log.record_notified(equipment, tag, condition, severity, event_id, now)
            sent += 1
        else:
            failed += 1
            if event_id is not None:
                failed_ids.append(int(event_id))
            print(f"notification_worker: send failed for event id={event_id} ({equipment}/{tag}/{condition}): {error}")

    all_ids = [int(e["id"]) for e in events if e.get("id") is not None]

    # Hold the cursor just before the earliest failed send so it's
    # retried next cycle - reprocessing already-succeeded events in
    # between is safe, since notification_log's cooldown check makes
    # re-notification idempotent.
    new_cursor = (min(failed_ids) - 1) if failed_ids else (max(all_ids) if all_ids else since_id)
    _write_cursor(new_cursor, marker_path)

    return {
        "checked": len(events),
        "notifiable": len(notifiable),
        "sent": sent,
        "skipped_cooldown": skipped_cooldown,
        "failed": failed,
    }


def run_forever(machine_database_path, tick_interval: float | None = None) -> None:
    config = ConfigManager()
    event_store = EventStore(database_path=machine_database_path)
    notification_log = NotificationLog(database_path=machine_database_path)
    interval = tick_interval or config.notification_poll_interval_seconds or TICK_INTERVAL_SECONDS_FALLBACK

    print(
        f"Notification worker started. Machine DB: {machine_database_path}. "
        f"Enabled: {config.notifications_enabled}. Tick interval: {interval:g}s."
    )

    while True:
        try:
            summary = run_cycle(event_store, notification_log, config)
            if summary.get("sent") or summary.get("failed"):
                print(f"cycle: {summary}")
        except Exception as error:
            print(f"notification_worker cycle failed: {error}")

        time.sleep(interval)


def main() -> None:
    lock_file = _acquire_single_instance_lock()

    try:
        run_forever(get_machine_db_path())
    except KeyboardInterrupt:
        print("\nNotification worker stopped.")
    finally:
        lock_file.close()


if __name__ == "__main__":
    main()
