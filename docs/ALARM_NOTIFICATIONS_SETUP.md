# Alarm email notifications - setup

Phase V2.2, admin UI added in Phase V2.3. Off by default. Enabling
this is always an explicit, intentional action - nothing here turns
itself on.

## What this does

A new, separate background service (`notification_worker.service`)
watches `machine_events` for new alarm/warning rows (the exact same
table Event Records, Ask AI, and Data Health already read) and emails
a configured recipient list when one qualifies. It never touches
`event_monitor.service` or how alarms are detected/stored - it only
reads what's already there.

## 1. Who receives alerts, and core behavior - the admin UI (recommended)

**Alarm Notification Settings** (admin-only page, sidebar) lets an
admin manage this without editing any file:
- Enable/disable the toggle.
- Minimum severity to notify (`alarm` or `warning`).
- Cooldown minutes between repeat notifications for the same alarm.
- Add/remove recipient email addresses (validated, duplicate-checked).

Every change is written to `config.db` (`notification_settings`/
`notification_recipients` tables - the same "admin page changes it
live, with an audit trail, instead of editing a text file" pattern
already used for PLC connections) and picked up by
`notification_worker.service` on its **next cycle, within
~30 seconds - no restart needed.** Every add/remove/settings change is
recorded in that page's own audit-log expander.

This page never shows or asks for an SMTP server address, port, or
any credential - see section 2 below for those.

### Equivalent via settings.ini (legacy / scripting)

`config/settings.ini`'s `[NOTIFICATIONS]` `enabled`/`min_severity`/
`cooldown_minutes`/`recipients` keys are still present as the schema's
initial-seed defaults, but **`app/notification_worker.py` no longer
reads them** as of Phase V2.3 - use the admin UI (or
`config.notification_settings_manager.NotificationSettingsManager`
directly, e.g. from a setup script) instead.

## 2. Secrets - environment variables, never settings.ini

Set `SMTP_USERNAME` and `SMTP_PASSWORD` as environment variables for
the `notification_worker.service` process - never write them into
`config/settings.ini` (a git-tracked file). The unit file
(`deploy/systemd/notification_worker.service`) already points at an
optional `/home/test/SmartMachineAI/.env` file
(`EnvironmentFile=-...`, the leading `-` means it's fine if this file
doesn't exist):

```bash
# /home/test/SmartMachineAI/.env - NEVER commit this file
# (.gitignore already excludes .env / .env.* project-wide)
SMTP_USERNAME=your-smtp-username
SMTP_PASSWORD=your-smtp-password
```

If your SMTP server allows anonymous/unauthenticated relay (common for
an internal mail relay), leave both unset - the worker sends without
`login()` in that case.

## 3. Manual steps required (cannot be done from this session - no sudo)

```bash
sudo cp deploy/systemd/notification_worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now notification_worker.service
```

Then check it started cleanly:
```bash
systemctl status notification_worker.service
journalctl -u notification_worker.service -n 20
```

Or check the **System Health** page in the app (Phase V2.1) - it's
listed there as "Alarm Notification Worker" alongside every other
service, once installed.

## 4. Testing it without waiting for a real alarm

```bash
source venv/bin/activate
python -c "
from engine.alarm_notification_engine import send_email
ok, error = send_email(
    smtp_host='smtp.example.com', smtp_port=587, smtp_use_tls=True,
    smtp_username='...', smtp_password='...',
    from_address='smartfactoryai@example.com',
    recipients=['you@example.com'],
    subject='SmartFactoryAI test notification',
    body='If you got this, SMTP is configured correctly.',
)
print(ok, error)
"
```

`ok=True, error=None` means it sent. Any failure prints a specific
reason (auth, connection, timeout) instead of a crash.

## 5. Turning it off again

Flip the toggle off on the Alarm Notification Settings page (takes
effect within ~30 seconds, no restart), or
`sudo systemctl stop notification_worker.service` to stop the process
entirely.

## What you'll see in an email

```
Subject: [SmartFactoryAI] ALARM - Air Compressor AC03 (P01) - high_alarm

Equipment: Air Compressor AC03 (P01)
Tag: P01.UTILITY.AC03.Power_kW
Severity: ALARM
Condition: high_alarm
Value: 40.18 kW
Timestamp: 2026-08-21 13:19:46
Message: P01.UTILITY.AC03.Power_kW is critically high at 40.18 kW (high alarm limit: 35 kW).

This is an automated notification from SmartFactoryAI. Manage recipients and notification settings on the Alarm Notification Settings page (admin).
```
