# Alarm email notifications - setup

Phase V2.2. Off by default. Enabling this is always an explicit,
intentional set of edits - nothing here turns itself on.

## What this does

A new, separate background service (`notification_worker.service`)
watches `machine_events` for new alarm/warning rows (the exact same
table Event Records, Ask AI, and Data Health already read) and emails
a configured recipient list when one qualifies. It never touches
`event_monitor.service` or how alarms are detected/stored - it only
reads what's already there.

## 1. Non-secret settings - `config/settings.ini`

```ini
[NOTIFICATIONS]
enabled = true
min_severity = alarm
cooldown_minutes = 60
recipients = engineer1@example.com, engineer2@example.com
smtp_host = smtp.example.com
smtp_port = 587
smtp_use_tls = true
smtp_from_address = smartfactoryai@example.com
poll_interval_seconds = 30
```

- `min_severity` - `alarm` (default) or `warning`. `warning` also
  includes `alarm`.
- `cooldown_minutes` - how long to wait before the SAME (equipment,
  tag, condition) alarm is allowed to email again. Prevents a
  `event_monitor.service` restart re-evaluating an already-active
  alarm as "new" from spamming a fresh email every restart.
- `recipients` - comma-separated. Empty means nowhere to send, so the
  worker won't attempt to even if `enabled = true`.

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

Set `enabled = false` in `config/settings.ini`'s `[NOTIFICATIONS]`
section (no restart required - the worker checks this every cycle),
or `sudo systemctl stop notification_worker.service` to stop the
process entirely.

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

This is an automated notification from SmartFactoryAI. Notification settings: config/settings.ini [NOTIFICATIONS] section.
```
