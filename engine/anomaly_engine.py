from __future__ import annotations

import json
import sqlite3
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from database.database import DatabaseManager
from engine import baseline_engine as base
from engine.anomaly_targets import ANOMALY_TYPE_DEVIATION, ANOMALY_TYPE_DRIFT, AnomalyRule
from engine.baseline_targets import BaselineTarget
from engine.energy_kpi_engine import TIME_FORMAT, effective_tariff

"""
Phase 9 - Anomaly Detection Engine. Deterministic only (Rule 4 - no
LLM). Consumes Phase 8's PERSISTED baseline summaries and Phase 6's
tariff engine; never recomputes a full baseline, never duplicates
RuleEngine/EventEngine's engineering-alarm logic.

Four permanently distinct concepts (never collapsed):
  - Engineering Alarm/Limit    -> ai/rule_engine.py + thresholds (untouched)
  - Statistical Anomaly        -> THIS module, "deviation" anomaly_type
  - Drift                      -> THIS module, "drift" anomaly_type (Phase 8's
                                   reference_vs_recent, reported not interpreted)
  - Energy Opportunity         -> Phase 10, not built here

Persistence counts REPRESENTATIVE BUCKETS, not worker ticks (item 4 of
the Phase 9 approval) - a target's own aggregation_minutes (reused
directly from its Phase 8 BaselineTarget) defines bucket width; the
worker may run every ~60s but only a genuinely NEW completed bucket
advances open/resolve persistence. Restart-safety is achieved by
RECONSTRUCTING the last few completed buckets from bounded historian
data every cycle (cheap - bounded to
max(persistence_periods_open, persistence_periods_resolve) buckets),
never by a dedicated in-memory or extra-table candidate-state
mechanism (item 5).
"""

SEVERITY_LEVELS = ("INFORMATION", "ATTENTION", "WARNING", "HIGH")

# item 20 - do not write to SQLite on every tick if nothing material
# changed. An already-open anomaly's last_seen is only touched when a
# genuinely new bucket was processed (which is itself already a rare,
# bounded event - at most once per aggregation_minutes).


# ---------------------------------------------------------------------------
# Bucket helpers - reuse Phase 8's own bucket-boundary function so
# "what bucket is this" is defined identically in both engines.
# ---------------------------------------------------------------------------

def last_completed_bucket_start(now: datetime, aggregation_minutes: int) -> datetime:
    """The most recent FULLY COMPLETE representative bucket as of `now`
    - e.g. at 14:06 with 5-minute buckets, the bucket containing `now`
    (14:05-14:10) is still in progress, so the last complete one is
    14:00-14:05. Re-evaluating at 14:06, 14:07, 14:08... all return the
    same bucket start until 14:10 - exactly item 4's required behavior."""
    current_bucket = base._bucket_start(now, aggregation_minutes)
    return current_bucket - timedelta(minutes=aggregation_minutes)


def _actual_bucket_value(
    target: BaselineTarget, historian: DatabaseManager, config_database_path: str | Path,
    bucket_start: datetime, aggregation_minutes: int,
) -> tuple[float | None, int]:
    """
    The CURRENT reading for one completed bucket - deliberately WITHOUT
    fault-exclusion (base._fetch_target_series(..., exclude_faults=False)):
    we need to see what's actually happening right now, including a
    genuine fault reading, to compare it against the learned-normal
    baseline. Fault-exclusion belongs only in what Phase 8 learns as
    "normal", never in what's currently being observed.
    """
    bucket_end = bucket_start + timedelta(minutes=aggregation_minutes)
    series, raw_count = base._fetch_target_series(target, historian, config_database_path, bucket_start, bucket_end, exclude_faults=False)
    return series.get(bucket_start), raw_count


def _recent_completed_buckets(now: datetime, aggregation_minutes: int, count: int) -> list[datetime]:
    """The last `count` completed bucket starts, oldest first - used
    both for normal evaluation and for restart-safe reconstruction."""
    last = last_completed_bucket_start(now, aggregation_minutes)
    return [last - timedelta(minutes=aggregation_minutes * i) for i in range(count - 1, -1, -1)]


# ---------------------------------------------------------------------------
# Operating-state gating (item 15) - per-rule tag suffix, not one
# generic assumption for every equipment family.
# ---------------------------------------------------------------------------

def _operating_state_ok(rule: AnomalyRule, historian: DatabaseManager, instance_key: str) -> tuple[bool, str | None]:
    if not rule.applicable_operating_states or rule.operating_state_tag_suffix is None:
        return True, None

    tag = f"{instance_key}.{rule.operating_state_tag_suffix}"
    row = historian.get_latest(tag)
    if row is None:
        return False, f"{tag} unavailable"

    is_running = bool(row["value"])
    state = "running" if is_running else "stopped"
    return state in rule.applicable_operating_states, None if is_running else f"{instance_key} not in an applicable operating state ({state})"


# ---------------------------------------------------------------------------
# Engineering-limit cross-reference (item 12/19) - reuses Phase 8's own
# threshold lookup; reads the LATEST value and the most recent
# machine_events row, never writes to either.
# ---------------------------------------------------------------------------

def engineering_limit_status(
    config_database_path: str | Path, machine_database_path: str | Path, tag_name: str, latest_value: float | None,
) -> tuple[str, int | None]:
    thresholds = base._load_thresholds(config_database_path, tag_name)
    if thresholds is None or not any(v is not None for v in thresholds.values()):
        return "not_configured", None

    if latest_value is None:
        return "not_configured", None

    status = "not_exceeded"
    if thresholds.get("high_alarm") is not None and latest_value >= thresholds["high_alarm"]:
        status = "alarm_exceeded"
    elif thresholds.get("low_alarm") is not None and latest_value <= thresholds["low_alarm"]:
        status = "alarm_exceeded"
    elif thresholds.get("high_warning") is not None and latest_value >= thresholds["high_warning"]:
        status = "warning_exceeded"
    elif thresholds.get("low_warning") is not None and latest_value <= thresholds["low_warning"]:
        status = "warning_exceeded"

    event_id = None
    try:
        connection = sqlite3.connect(machine_database_path)
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT id FROM machine_events WHERE tag = ? ORDER BY event_time DESC LIMIT 1", (tag_name,),
        ).fetchone()
        connection.close()
        event_id = row["id"] if row else None
    except sqlite3.OperationalError:
        pass

    return status, event_id


# ---------------------------------------------------------------------------
# Fault/maintenance window policy (item 11 - A/B/C nuance)
# ---------------------------------------------------------------------------

def _fault_or_maintenance_window(
    config_database_path: str | Path, historian: DatabaseManager, target: BaselineTarget,
    check_start: datetime, check_end: datetime,
) -> list[tuple[datetime, datetime]]:
    """Reuses Phase 8's exact interval-reconstruction (threshold-based
    fault intervals + maintenance windows) - never a second exclusion
    mechanism."""
    windows: list[tuple[datetime, datetime]] = []

    if not target.is_derived:
        rows = base._fetch_bounded(historian, target.tag_name, check_start, check_end)
        thresholds = base._load_thresholds(config_database_path, target.tag_name)
        windows.extend(base._abnormal_intervals_from_thresholds(rows, thresholds))

    windows.extend(base._maintenance_intervals(config_database_path, target.instance_key))
    return base._merge_intervals(windows)


# ---------------------------------------------------------------------------
# Severity (item 7/10) - never CRITICAL for a purely statistical
# finding; bumped one tier (floor WARNING) when an engineering limit is
# ALSO exceeded, per approval - the limit breach remains independently
# visible via its own field, never implied to be "escalated" by this.
# ---------------------------------------------------------------------------

def classify_severity(normalized_deviation: float, persistence_periods: int, rule: AnomalyRule, engineering_status: str) -> str:
    magnitude_score = abs(normalized_deviation)

    if magnitude_score >= 5.0 or persistence_periods >= rule.persistence_periods_open * 3:
        tier = "HIGH"
    elif magnitude_score >= 3.5 or persistence_periods >= rule.persistence_periods_open * 2:
        tier = "WARNING"
    elif magnitude_score >= rule.deviation_threshold_mad:
        tier = "ATTENTION"
    else:
        tier = "INFORMATION"

    if engineering_status in ("warning_exceeded", "alarm_exceeded"):
        tier_index = max(SEVERITY_LEVELS.index(tier), SEVERITY_LEVELS.index("WARNING"))
        tier = SEVERITY_LEVELS[tier_index]

    return tier


# ---------------------------------------------------------------------------
# Deviation computation (item 7/8)
# ---------------------------------------------------------------------------

def compute_deviation(actual: float, expected: float, mad: float | None) -> tuple[float, float | None, float | None]:
    deviation_absolute = actual - expected
    deviation_percent = (deviation_absolute / expected * 100) if expected != 0 else None
    normalized_deviation = (deviation_absolute / mad) if mad and mad > 0 else None
    return deviation_absolute, deviation_percent, normalized_deviation


def _direction_ok(rule: AnomalyRule, deviation_absolute: float) -> bool:
    if rule.direction == "high":
        return deviation_absolute > 0
    if rule.direction == "low":
        return deviation_absolute < 0
    return True  # "both"


def _effective_open_thresholds(rule: AnomalyRule, baseline_status: str) -> tuple[float, int]:
    """Bootstrap/Low-confidence baselines require stronger evidence
    before opening (item 16) - never suppressed entirely, just gated
    harder, and always tagged provisional by the caller."""
    if baseline_status == "bootstrap":
        return rule.deviation_threshold_mad * rule.bootstrap_multiplier, int(round(rule.persistence_periods_open * rule.bootstrap_multiplier))
    return rule.deviation_threshold_mad, rule.persistence_periods_open


# ---------------------------------------------------------------------------
# Persisted baseline lookup - reads ONLY baseline_context_summary
# (cheap, indexed), never calls compute_baseline() (item 21/23).
# ---------------------------------------------------------------------------

def lookup_baseline_row(
    config_database_path: str | Path, plant_id: int, target: BaselineTarget,
    current_context: dict[str, str], baseline_type: str = "recent",
) -> dict[str, Any] | None:
    bucket_key = base.context_bucket_key(current_context)
    connection = sqlite3.connect(config_database_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM baseline_context_summary WHERE plant_id = ? AND instance_key = ? AND target_key = ? "
            "AND baseline_type = ? AND context_bucket_key = ?",
            (plant_id, target.instance_key, target.target_key, baseline_type, bucket_key),
        ).fetchone()
        if row is None:
            # Fall back to the Level-C "__all__" bucket if the exact
            # current context hasn't been covered by the worker yet -
            # still a real, honestly-classified baseline, never fabricated.
            row = connection.execute(
                "SELECT * FROM baseline_context_summary WHERE plant_id = ? AND instance_key = ? AND target_key = ? "
                "AND baseline_type = ? AND context_bucket_key = '__all__'",
                (plant_id, target.instance_key, target.target_key, baseline_type),
            ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Estimated excess energy/cost (items 9, 10, 18) - interval-integrated
# per NEW bucket, tariff applied per-bucket's own date (never the
# latest-reading-times-total-duration extrapolation the approval
# explicitly rejected). Only ever computed for category=ENERGY rules.
# ---------------------------------------------------------------------------

def bucket_excess_energy_cost(
    config_database_path: str | Path, plant_id: int, actual_kw: float, expected_kw: float,
    bucket_start: datetime, aggregation_minutes: int,
) -> tuple[float, float | None, str | None]:
    """Returns (excess_kwh, excess_cost_or_None, currency_or_None) for
    ONE bucket. excess_kwh is always a real number (0.0 when actual <=
    expected - "excess" only ever means over-consumption). Cost is
    None when no tariff is configured for that bucket's date - never
    fabricated, never approximated from a different day's rate."""
    excess_kw = max(actual_kw - expected_kw, 0.0)
    if excess_kw <= 0:
        return 0.0, None, None

    excess_kwh = excess_kw * (aggregation_minutes / 60.0)
    tariff = effective_tariff(config_database_path, plant_id, bucket_start)
    rate = tariff.get("energy_rate") if tariff else None

    if rate is None:
        return excess_kwh, None, None

    return excess_kwh, round(excess_kwh * rate, 4), tariff.get("currency")


# ---------------------------------------------------------------------------
# anomalies table read/write
# ---------------------------------------------------------------------------

def find_open_anomaly(config_database_path: str | Path, plant_id: int, instance_key: str, target_key: str, rule_key: str) -> dict[str, Any] | None:
    connection = sqlite3.connect(config_database_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM anomalies WHERE plant_id = ? AND instance_key = ? AND target_key = ? AND rule_key = ? AND status = 'OPEN'",
            (plant_id, instance_key, target_key, rule_key),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def count_prior_occurrences(config_database_path: str | Path, plant_id: int, instance_key: str, target_key: str, rule_key: str) -> int:
    """Authoritative recurrence count - always derived by counting real
    RESOLVED rows (item 14), never trusted as a bare mutable counter."""
    connection = sqlite3.connect(config_database_path)
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM anomalies WHERE plant_id = ? AND instance_key = ? AND target_key = ? AND rule_key = ? AND status = 'RESOLVED'",
            (plant_id, instance_key, target_key, rule_key),
        ).fetchone()[0]
        return count
    finally:
        connection.close()


def _write_anomaly_row(config_database_path: str | Path, row_id: int | None, fields: dict[str, Any]) -> int:
    """Dict-driven insert/update - avoids positional-placeholder
    miscounting on a wide table. Returns the row id (new or existing)."""
    connection = sqlite3.connect(config_database_path)
    try:
        if row_id is None:
            columns = list(fields.keys())
            placeholders = ", ".join("?" for _ in columns)
            cursor = connection.execute(
                f"INSERT INTO anomalies ({', '.join(columns)}) VALUES ({placeholders})",
                [fields[c] for c in columns],
            )
            connection.commit()
            return cursor.lastrowid
        else:
            set_clause = ", ".join(f"{c} = ?" for c in fields)
            connection.execute(
                f"UPDATE anomalies SET {set_clause} WHERE id = ?",
                [*fields.values(), row_id],
            )
            connection.commit()
            return row_id
    finally:
        connection.close()


def _equipment_id_for_instance(config_database_path: str | Path, instance_key: str) -> int | None:
    connection = sqlite3.connect(config_database_path)
    connection.row_factory = sqlite3.Row
    try:
        parts = instance_key.split(".")
        plant, instance_code = parts[0].lower(), parts[-1].lower()
        row = connection.execute(
            "SELECT id FROM equipment WHERE name LIKE ? AND name LIKE ?", (f"{plant}_%", f"%_{instance_code}"),
        ).fetchone()
        return row["id"] if row else None
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Main deviation-rule evaluation - the core of item 4/5/6's bucket-based,
# restart-safe persistence design.
# ---------------------------------------------------------------------------

def _classify_bucket(
    rule: AnomalyRule, actual: float, expected: float, mad: float | None, threshold_mad: float,
    range_low: float | None, range_high: float | None,
) -> dict[str, Any]:
    deviation_absolute, deviation_percent, normalized_deviation = compute_deviation(actual, expected, mad)

    is_abnormal = (
        _direction_ok(rule, deviation_absolute)
        and abs(deviation_absolute) >= rule.minimum_absolute_deviation
        and normalized_deviation is not None and abs(normalized_deviation) >= threshold_mad
    )

    is_within_resolve_band = True
    if range_low is not None and range_high is not None:
        band_width = range_high - range_low
        margin = band_width * (rule.resolve_band_margin_pct / 100.0)
        is_within_resolve_band = (range_low - margin) <= actual <= (range_high + margin)

    return {
        "actual": actual, "deviation_absolute": deviation_absolute, "deviation_percent": deviation_percent,
        "normalized_deviation": normalized_deviation, "is_abnormal": is_abnormal,
        "is_within_resolve_band": is_within_resolve_band,
    }


def evaluate_deviation_rule(
    rule: AnomalyRule, target: BaselineTarget, config_database_path: str | Path, machine_database_path: str | Path,
    historian: DatabaseManager, plant_id: int, now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now()

    state_ok, state_reason = _operating_state_ok(rule, historian, target.instance_key)
    existing = find_open_anomaly(config_database_path, plant_id, target.instance_key, target.target_key, rule.rule_key)

    if not state_ok:
        return {"action": "skipped", "reason": state_reason}

    # Sized from the WORST-CASE (bootstrap-adjusted) persistence
    # requirement, since baseline_status (and therefore the actual
    # effective threshold) isn't known until the baseline is looked up
    # below - if this used the raw rule.persistence_periods_open
    # instead, a bootstrap baseline's inflated requirement could never
    # be satisfied: too few buckets would ever be fetched to reach it.
    worst_case_persistence_open = int(round(rule.persistence_periods_open * rule.bootstrap_multiplier))
    n_buckets_needed = max(worst_case_persistence_open, rule.persistence_periods_resolve, 1) + 1
    buckets = _recent_completed_buckets(now, target.aggregation_minutes, n_buckets_needed)
    lookback_start, lookback_end = buckets[0], buckets[-1] + timedelta(minutes=target.aggregation_minutes)

    maintenance_windows = base._maintenance_intervals(config_database_path, target.instance_key)
    fault_and_maintenance_windows = _fault_or_maintenance_window(config_database_path, historian, target, lookback_start, lookback_end)
    latest_bucket = buckets[-1]
    in_maintenance_now = base._excluded(latest_bucket, maintenance_windows)
    in_fault_or_maintenance_now = base._excluded(latest_bucket, fault_and_maintenance_windows)

    current_context = base._current_context(target, historian, config_database_path, now)
    baseline_row = lookup_baseline_row(config_database_path, plant_id, target, current_context)

    tag_for_limits = target.tag_name or f"{target.instance_key}.{target.target_key}"
    if target.is_derived:
        eng_status, eng_event_id = "not_configured", None
    else:
        latest_row = historian.get_latest(tag_for_limits)
        latest_value = latest_row["value"] if latest_row else None
        eng_status, eng_event_id = engineering_limit_status(config_database_path, machine_database_path, tag_for_limits, latest_value)

    # item 11 policy C: maintenance suppresses opening AND pauses an
    # already-open candidate's persistence entirely (neither open nor
    # resolve counters advance during the window).
    if in_maintenance_now and existing is None:
        return {"action": "suppressed", "reason": "maintenance window"}

    if baseline_row is None or baseline_row["baseline_status"] == "unavailable":
        return {"action": "skipped", "reason": "baseline unavailable - no statistical anomaly evaluated (engineering alarms unaffected)"}

    expected = baseline_row["median_value"]
    mad = baseline_row["mad"]
    range_low, range_high = baseline_row["range_low"], baseline_row["range_high"]
    baseline_status = baseline_row["baseline_status"]
    threshold_mad, effective_persistence_open = _effective_open_thresholds(rule, baseline_status)
    provisional = baseline_status == "bootstrap"

    # item 11 policy B: first-ever evaluation (no existing candidate),
    # an engineering alarm is ALREADY active for this exact tag, and
    # this is a plain deviation rule (not drift) - a new statistical
    # finding adds no new information the alarm doesn't already convey.
    if existing is None and eng_status in ("warning_exceeded", "alarm_exceeded"):
        return {"action": "suppressed", "reason": "engineering alarm already active - redundant statistical finding not opened"}

    if in_maintenance_now and existing is not None:
        # Policy C, already-open branch: annotate, pause, do not advance.
        evidence = json.loads(existing["evidence_json"]) if existing["evidence_json"] else {}
        if not evidence.get("maintenance_window_noted"):
            evidence["maintenance_window_noted"] = True
            evidence.setdefault("notes", []).append(f"Co-occurring with an active maintenance window as of {now.strftime(TIME_FORMAT)}")
            _write_anomaly_row(config_database_path, existing["id"], {"evidence_json": json.dumps(evidence), "updated_at": now.strftime(TIME_FORMAT)})
        return {"action": "paused", "reason": "maintenance window - persistence not advanced"}

    return _evaluate_deviation_buckets(
        rule, target, config_database_path, historian, plant_id, now, existing,
        buckets, expected, mad, threshold_mad, effective_persistence_open, range_low, range_high,
        baseline_row, provisional, eng_status, eng_event_id, in_fault_or_maintenance_now,
    )


def _evaluate_deviation_buckets(
    rule: AnomalyRule, target: BaselineTarget, config_database_path: str | Path, historian: DatabaseManager,
    plant_id: int, now: datetime, existing: dict[str, Any] | None, buckets: list[datetime],
    expected: float, mad: float | None, threshold_mad: float, effective_persistence_open: int,
    range_low: float | None, range_high: float | None, baseline_row: dict[str, Any], provisional: bool,
    eng_status: str, eng_event_id: int | None, in_fault_or_maintenance_now: bool,
) -> dict[str, Any]:
    classifications: list[tuple[datetime, dict[str, Any] | None]] = []
    for bucket_start in buckets:
        actual, _ = _actual_bucket_value(target, historian, config_database_path, bucket_start, target.aggregation_minutes)
        if actual is None:
            classifications.append((bucket_start, None))
            continue
        classifications.append((bucket_start, _classify_bucket(rule, actual, expected, mad, threshold_mad, range_low, range_high)))

    # Walk backward from the most recent bucket, counting consecutive
    # abnormal buckets (open) and consecutive within-resolve-band
    # buckets (resolve) - recomputed fresh from real historian data
    # every cycle, restart-safe by construction (item 5), never
    # trusting a stored counter alone.
    open_persistence = 0
    for _, classification in reversed(classifications):
        if classification is None:
            break
        if classification["is_abnormal"]:
            open_persistence += 1
        else:
            break

    resolve_persistence = 0
    for _, classification in reversed(classifications):
        if classification is None:
            break
        if classification["is_within_resolve_band"]:
            resolve_persistence += 1
        else:
            break

    latest_classified = next((c for _, c in reversed(classifications) if c is not None), None)
    latest_bucket_start = next((b for b, c in reversed(classifications) if c is not None), None)

    if latest_classified is None:
        return {"action": "skipped", "reason": "no recent representative data available"}

    now_text = now.strftime(TIME_FORMAT)
    context_payload = json.loads(baseline_row["context_json"]) if baseline_row.get("context_json") else {}

    if existing is None:
        if open_persistence < effective_persistence_open:
            return {"action": "candidate", "open_persistence": open_persistence, "required": effective_persistence_open}

        excess_kwh_total, excess_cost_total, currency = None, None, None
        if rule.energy_relevant:
            excess_kwh_total, excess_cost_total = 0.0, 0.0
            for bucket_start, classification in classifications[-open_persistence:]:
                if classification is None:
                    continue
                kwh, cost, cur = bucket_excess_energy_cost(
                    config_database_path, plant_id, classification["actual"], expected, bucket_start, target.aggregation_minutes,
                )
                excess_kwh_total += kwh
                if cost is not None:
                    excess_cost_total += cost
                    currency = cur
            if excess_cost_total == 0.0 and currency is None:
                excess_cost_total = None  # no tariff could be applied to any contributing bucket - stay honestly Unavailable

        severity = classify_severity(latest_classified["normalized_deviation"] or 0.0, open_persistence, rule, eng_status)
        equipment_id = _equipment_id_for_instance(config_database_path, target.instance_key)
        title = rule.title_template.format(instance=target.instance_key)

        evidence = {"context_used": context_payload.get("context_used", {})}
        if in_fault_or_maintenance_now and eng_status == "not_exceeded":
            evidence["notes"] = [f"Co-occurring with a reconstructed abnormal/fault window as of {now_text}"]

        assumptions = [
            f"deviation threshold: {threshold_mad:.2f}x MAD" + (" (bootstrap-adjusted)" if provisional else ""),
            f"persistence required to open: {effective_persistence_open} representative period(s) of {target.aggregation_minutes} min each",
            f"minimum absolute deviation: {rule.minimum_absolute_deviation} {rule.minimum_absolute_deviation_unit} "
            f"(provenance: {rule.threshold_provenance})",
        ]
        data_limitations = list(context_payload.get("missing_context", []))
        if provisional:
            data_limitations.append("baseline is bootstrap/Low confidence - finding is provisional")

        fields = {
            "plant_id": plant_id, "equipment_id": equipment_id, "instance_key": target.instance_key,
            "target_key": target.target_key, "rule_key": rule.rule_key, "anomaly_type": rule.anomaly_type,
            "category": rule.category, "title": title, "severity": severity,
            "confidence": baseline_row.get("confidence") or "Low", "provisional": 1 if provisional else 0,
            "status": "OPEN", "first_detected": now_text, "last_seen": now_text,
            "occurrence_count": count_prior_occurrences(config_database_path, plant_id, target.instance_key, target.target_key, rule.rule_key) + 1,
            "open_persistence_periods": open_persistence, "resolve_persistence_periods": 0,
            "last_bucket_start": latest_bucket_start.strftime(TIME_FORMAT),
            "actual_value": latest_classified["actual"], "expected_value": expected,
            "expected_low": range_low, "expected_high": range_high,
            "deviation_absolute": latest_classified["deviation_absolute"], "deviation_percent": latest_classified["deviation_percent"],
            "normalized_deviation": latest_classified["normalized_deviation"],
            "baseline_type": baseline_row.get("baseline_type"), "baseline_level": baseline_row.get("baseline_level"),
            "baseline_confidence": baseline_row.get("confidence"),
            "engineering_limit_status": eng_status, "engineering_limit_event_id": eng_event_id,
            "estimated_excess_energy_kwh": excess_kwh_total, "estimated_excess_cost": excess_cost_total,
            "estimated_excess_cost_currency": currency, "threshold_provenance": rule.threshold_provenance,
            "source_tags": json.dumps([target.tag_name] if target.tag_name else []),
            "evidence_json": json.dumps(evidence), "assumptions_json": json.dumps(assumptions),
            "data_limitations_json": json.dumps(data_limitations),
            "created_at": now_text, "updated_at": now_text,
        }
        row_id = _write_anomaly_row(config_database_path, None, fields)
        return {"action": "opened", "id": row_id, "severity": severity}

    # --- an OPEN anomaly already exists for this exact condition ---
    if resolve_persistence >= rule.persistence_periods_resolve:
        _write_anomaly_row(config_database_path, existing["id"], {
            "status": "RESOLVED", "resolved_at": now_text, "last_seen": now_text,
            "resolve_persistence_periods": resolve_persistence, "updated_at": now_text,
        })
        return {"action": "resolved", "id": existing["id"]}

    stored_last_bucket = datetime.strptime(existing["last_bucket_start"], TIME_FORMAT) if existing["last_bucket_start"] else None
    is_new_bucket = stored_last_bucket is None or latest_bucket_start > stored_last_bucket

    if not is_new_bucket:
        # item 20 - nothing material changed since the last write;
        # do not touch the row again this cycle.
        return {"action": "unchanged", "id": existing["id"]}

    excess_kwh_total = existing["estimated_excess_energy_kwh"] or 0.0
    excess_cost_total = existing["estimated_excess_cost"]
    currency = existing["estimated_excess_cost_currency"]
    if rule.energy_relevant and latest_classified["is_abnormal"]:
        kwh, cost, cur = bucket_excess_energy_cost(
            config_database_path, plant_id, latest_classified["actual"], expected, latest_bucket_start, target.aggregation_minutes,
        )
        excess_kwh_total += kwh
        if cost is not None:
            excess_cost_total = (excess_cost_total or 0.0) + cost
            currency = cur

    severity = classify_severity(latest_classified["normalized_deviation"] or 0.0, open_persistence, rule, eng_status)

    evidence = json.loads(existing["evidence_json"]) if existing["evidence_json"] else {}
    if in_fault_or_maintenance_now and eng_status == "not_exceeded" and not evidence.get("fault_window_noted"):
        evidence["fault_window_noted"] = True
        evidence.setdefault("notes", []).append(f"Co-occurring with a reconstructed abnormal/fault window as of {now_text}")
    if eng_status in ("warning_exceeded", "alarm_exceeded") and not evidence.get("engineering_limit_noted"):
        evidence["engineering_limit_noted"] = True
        evidence.setdefault("notes", []).append(f"Engineering limit also exceeded as of {now_text} ({eng_status})")

    _write_anomaly_row(config_database_path, existing["id"], {
        "last_seen": now_text, "open_persistence_periods": open_persistence, "resolve_persistence_periods": resolve_persistence,
        "last_bucket_start": latest_bucket_start.strftime(TIME_FORMAT),
        "actual_value": latest_classified["actual"], "deviation_absolute": latest_classified["deviation_absolute"],
        "deviation_percent": latest_classified["deviation_percent"], "normalized_deviation": latest_classified["normalized_deviation"],
        "severity": severity, "engineering_limit_status": eng_status, "engineering_limit_event_id": eng_event_id,
        "estimated_excess_energy_kwh": excess_kwh_total, "estimated_excess_cost": excess_cost_total,
        "estimated_excess_cost_currency": currency, "evidence_json": json.dumps(evidence), "updated_at": now_text,
    })
    return {"action": "updated", "id": existing["id"]}


# ---------------------------------------------------------------------------
# Drift-rule evaluation (items 12, 13) - reads Phase 8's already-computed
# reference-vs-recent baselines directly; never recomputes anything,
# never interprets cause (item 22 - "recent X baseline has increased
# relative to reference", nothing more).
#
# Known scope limitation, disclosed rather than hidden: Phase 8 only
# PERSISTS the latest reference/recent snapshot per bucket (never a
# history of past snapshots), so unlike deviation rules (which
# reconstruct persistence from real historian buckets and are fully
# restart-safe), a NEW drift candidate's "more than one evaluation"
# requirement is tracked with a small process-local cache rather than
# reconstructed from data that doesn't exist to reconstruct from. Once
# a drift anomaly is OPEN, its ongoing state IS fully DB-persisted and
# restart-safe like every other anomaly row.
# ---------------------------------------------------------------------------

_drift_candidate_cache: dict[tuple, str] = {}  # (plant_id, instance_key, target_key, rule_key) -> last-seen recent computed_at


def evaluate_drift_rule(
    rule: AnomalyRule, target: BaselineTarget, config_database_path: str | Path,
    plant_id: int, now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now()
    now_text = now.strftime(TIME_FORMAT)

    reference_row = lookup_baseline_row(config_database_path, plant_id, target, {}, baseline_type="reference")
    recent_row = lookup_baseline_row(config_database_path, plant_id, target, {}, baseline_type="recent")
    existing = find_open_anomaly(config_database_path, plant_id, target.instance_key, target.target_key, rule.rule_key)

    if reference_row is None or recent_row is None:
        return {"action": "skipped", "reason": "reference or recent baseline not yet persisted"}

    if reference_row["baseline_status"] != "mature" or recent_row["baseline_status"] != "mature":
        return {"action": "skipped", "reason": "reference and recent baselines must both be mature for drift evaluation"}

    if reference_row["history_start"] == recent_row["history_start"]:
        return {"action": "skipped", "reason": "reference and recent windows have not yet separated - insufficient history"}

    cache_key = (plant_id, target.instance_key, target.target_key, rule.rule_key)
    recent_computed_at = recent_row["computed_at"]

    # item 12 - only evaluate when the persisted baseline could actually
    # have changed since we last looked, and skip entirely otherwise -
    # never repeatedly process an identical snapshot.
    if existing is not None:
        stored_computed_at = existing["last_bucket_start"]  # repurposed for drift - see module note
        if stored_computed_at == recent_computed_at:
            return {"action": "unchanged", "id": existing["id"]}
    else:
        if _drift_candidate_cache.get(cache_key) == recent_computed_at:
            return {"action": "unchanged_candidate"}

    reference_value, recent_value = reference_row["median_value"], recent_row["median_value"]
    if reference_value is None or recent_value is None or reference_value == 0:
        return {"action": "skipped", "reason": "reference/recent expected value unavailable"}

    drift_percent = (recent_value - reference_value) / reference_value * 100
    direction_ok = (rule.direction != "low" and drift_percent > 0) or (rule.direction == "low" and drift_percent < 0) or rule.direction == "both"
    qualifies = direction_ok and abs(drift_percent) >= (rule.drift_threshold_pct or 0)

    if existing is None:
        if not qualifies:
            _drift_candidate_cache[cache_key] = recent_computed_at
            return {"action": "no_drift"}

        first_seen_at = _drift_candidate_cache.get(cache_key)
        if first_seen_at is None or first_seen_at == recent_computed_at:
            # First qualifying snapshot seen (or the only snapshot we
            # have a memory of) - wait for at least one more distinct
            # snapshot before opening (item 13's "more than one
            # evaluation" requirement).
            _drift_candidate_cache[cache_key] = recent_computed_at
            return {"action": "drift_candidate"}

        equipment_id = _equipment_id_for_instance(config_database_path, target.instance_key)
        title = rule.title_template.format(instance=target.instance_key)
        evidence = {
            "reference_expected_value": reference_value, "recent_expected_value": recent_value,
            "reference_history_start": reference_row["history_start"], "reference_history_end": reference_row["history_end"],
            "recent_history_start": recent_row["history_start"], "recent_history_end": recent_row["history_end"],
        }
        assumptions = [
            f"drift threshold: {rule.drift_threshold_pct}%",
            "reference and recent baselines both required 'mature' status before this comparison was made",
            "no cause is inferred - this reports that recent behavior has moved away from the historical reference, nothing more",
        ]
        fields = {
            "plant_id": plant_id, "equipment_id": equipment_id, "instance_key": target.instance_key,
            "target_key": target.target_key, "rule_key": rule.rule_key, "anomaly_type": rule.anomaly_type,
            "category": rule.category, "title": title, "severity": "ATTENTION",
            "confidence": recent_row["confidence"] or "Low", "provisional": 0,
            "status": "OPEN", "first_detected": now_text, "last_seen": now_text,
            "occurrence_count": count_prior_occurrences(config_database_path, plant_id, target.instance_key, target.target_key, rule.rule_key) + 1,
            "open_persistence_periods": 2, "resolve_persistence_periods": 0,
            "last_bucket_start": recent_computed_at,
            "actual_value": recent_value, "expected_value": reference_value,
            "expected_low": reference_row["range_low"], "expected_high": reference_row["range_high"],
            "deviation_absolute": recent_value - reference_value, "deviation_percent": drift_percent, "normalized_deviation": None,
            "baseline_type": "reference_vs_recent", "baseline_level": recent_row["baseline_level"], "baseline_confidence": recent_row["confidence"],
            "engineering_limit_status": "not_configured", "engineering_limit_event_id": None,
            "estimated_excess_energy_kwh": None, "estimated_excess_cost": None, "estimated_excess_cost_currency": None,
            "threshold_provenance": rule.threshold_provenance,
            "source_tags": json.dumps([target.tag_name] if target.tag_name else []),
            "evidence_json": json.dumps(evidence), "assumptions_json": json.dumps(assumptions),
            "data_limitations_json": json.dumps(["drift pre-open persistence uses a process-local cache, not full "
                                                  "restart-safe reconstruction - see engine/anomaly_engine.py's module docstring"]),
            "created_at": now_text, "updated_at": now_text,
        }
        row_id = _write_anomaly_row(config_database_path, None, fields)
        _drift_candidate_cache.pop(cache_key, None)
        return {"action": "opened", "id": row_id}

    # existing OPEN drift anomaly
    if not qualifies:
        _write_anomaly_row(config_database_path, existing["id"], {
            "status": "RESOLVED", "resolved_at": now_text, "last_seen": now_text, "updated_at": now_text,
        })
        return {"action": "resolved", "id": existing["id"]}

    _write_anomaly_row(config_database_path, existing["id"], {
        "last_seen": now_text, "last_bucket_start": recent_computed_at,
        "actual_value": recent_value, "deviation_absolute": recent_value - reference_value,
        "deviation_percent": drift_percent, "updated_at": now_text,
    })
    return {"action": "updated", "id": existing["id"]}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def evaluate_rule(
    rule: AnomalyRule, target: BaselineTarget, config_database_path: str | Path, machine_database_path: str | Path,
    historian: DatabaseManager, plant_id: int, now: datetime | None = None,
) -> dict[str, Any]:
    if rule.anomaly_type == ANOMALY_TYPE_DRIFT:
        return evaluate_drift_rule(rule, target, config_database_path, plant_id, now)
    return evaluate_deviation_rule(rule, target, config_database_path, machine_database_path, historian, plant_id, now)


# ---------------------------------------------------------------------------
# Debug CLI (Phase 9 deliverable is the engine + a minimal read-only
# page - this is a manual sanity-check tool)
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    from config.environment import get_config_db_path, get_machine_db_path
    from engine.anomaly_targets import ANOMALY_RULES
    from engine.baseline_targets import discover_targets

    parser = argparse.ArgumentParser(description="Phase 9 Anomaly Engine - debug CLI")
    parser.add_argument("--plant", default="p01")
    parser.add_argument("--config-database", default=str(get_config_db_path()))
    parser.add_argument("--machine-database", default=str(get_machine_db_path()))
    args = parser.parse_args()

    historian = DatabaseManager(db_path=args.machine_database)
    targets = {t.instance_key + "." + t.target_key: t for t in discover_targets(args.config_database, args.plant)}

    connection = sqlite3.connect(args.config_database)
    plant = connection.execute("SELECT id FROM plants WHERE code = ?", (args.plant.lower(),)).fetchone()
    connection.close()
    if plant is None:
        print(f"Unknown plant: {args.plant}")
        return
    plant_id = plant[0]

    for rule in ANOMALY_RULES:
        matching = [t for key, t in targets.items() if t.equipment_type == rule.equipment_type and t.target_key == rule.baseline_target_key]
        for target in matching:
            result = evaluate_rule(rule, target, args.config_database, args.machine_database, historian, plant_id)
            print(f"{target.instance_key}.{target.target_key} [{rule.rule_key}]: {result}")


if __name__ == "__main__":
    main()
