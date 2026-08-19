import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from database.database import DatabaseManager
from engine import health_engine as he
from engine import health_evidence as hev
from engine.equipment_metadata_migrator import migrate as migrate_equipment_metadata
from engine.health_targets import (
    COVERAGE_INSUFFICIENT,
    FAMILY_MAX_PENALTY,
    FAMILY_CONDITION,
    HEALTH_BANDS,
    HEALTH_MODEL_VERSION,
    health_band_for_score,
)

from tests.test_anomaly_engine import _seed_config_db, _insert_tag, _plant_id, BASE

NOW = BASE  # 2026-08-25-ish anchor, well after MODERN_DATA_BOUNDARY - matches project-wide test convention


def _seed_equipment(config_db, name, service_interval_days=None, last_serviced_at=None, next_due_at=None, criticality=None) -> int:
    connection = sqlite3.connect(config_db)
    connection.execute(
        "INSERT INTO equipment (name, display_name, service_interval_days, last_serviced_at, next_due_at, criticality) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, name, service_interval_days, last_serviced_at, next_due_at, criticality),
    )
    connection.commit()
    row_id = connection.execute("SELECT id FROM equipment WHERE name = ?", (name,)).fetchone()[0]
    connection.close()
    return row_id


def _seed_anomaly(
    config_db, plant_id, instance_key, target_key, rule_key, status="OPEN", severity="HIGH",
    confidence="High", first_detected=None, last_seen=None, resolved_at=None, category="CONDITION",
) -> int:
    fd = first_detected or NOW.strftime("%Y-%m-%d %H:%M:%S")
    ls = last_seen or fd
    row = dict(
        plant_id=plant_id, equipment_id=None, instance_key=instance_key, target_key=target_key, rule_key=rule_key,
        anomaly_type="deviation", category=category, title="t", severity=severity, confidence=confidence,
        provisional=0, status=status, first_detected=fd, last_seen=ls, resolved_at=resolved_at,
        occurrence_count=1, open_persistence_periods=3, resolve_persistence_periods=0, last_bucket_start=ls,
        actual_value=90.0, expected_value=50.0, expected_low=40.0, expected_high=60.0, deviation_absolute=40.0,
        deviation_percent=80.0, normalized_deviation=13.3, baseline_type="recent", baseline_level="B",
        baseline_confidence=confidence, engineering_limit_status="not_exceeded", engineering_limit_event_id=None,
        estimated_excess_energy_kwh=None, estimated_excess_cost=None, estimated_excess_cost_currency=None,
        threshold_provenance="SIMULATION_TUNING", source_tags="[]", evidence_json="{}", assumptions_json="[]",
        data_limitations_json="[]", created_at=fd, updated_at=ls,
    )
    connection = sqlite3.connect(config_db)
    cols = list(row.keys())
    cursor = connection.execute(f"INSERT INTO anomalies ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [row[c] for c in cols])
    connection.commit()
    new_id = cursor.lastrowid
    connection.close()
    return new_id


def _seed_mature_baseline(config_db, plant_id, instance_key, target_key, equipment_type, confidence="Low") -> None:
    connection = sqlite3.connect(config_db)
    connection.execute(
        "INSERT INTO baseline_context_summary "
        "(plant_id, instance_key, target_key, equipment_type, baseline_type, context_bucket_key, "
        " baseline_level, baseline_status, confidence, representative_sample_count, computed_at) "
        "VALUES (?, ?, ?, ?, 'recent', 'default', 'B', 'mature', ?, 40, ?)",
        (plant_id, instance_key, target_key, equipment_type, confidence, NOW.strftime("%Y-%m-%d %H:%M:%S")),
    )
    connection.commit()
    connection.close()


def _create_machine_events_table(machine_db: Path) -> None:
    connection = sqlite3.connect(machine_db)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS machine_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_time TEXT NOT NULL,
            detected_at TEXT,
            equipment TEXT,
            tag TEXT NOT NULL,
            severity TEXT NOT NULL,
            condition TEXT NOT NULL,
            value REAL,
            unit TEXT,
            trend TEXT,
            message TEXT,
            address TEXT,
            samples INTEGER,
            minimum REAL,
            maximum REAL,
            average REAL,
            change REAL,
            created_at TEXT
        )
        """
    )
    connection.commit()
    connection.close()


def _seed_event(machine_db: Path, tag: str, condition: str, severity: str, event_time: datetime) -> None:
    connection = sqlite3.connect(machine_db)
    connection.execute(
        "INSERT INTO machine_events (event_time, detected_at, equipment, tag, severity, condition, created_at) "
        "VALUES (?, ?, 'x', ?, ?, ?, ?)",
        (event_time.strftime("%Y-%m-%d %H:%M:%S"), event_time.strftime("%Y-%m-%d %H:%M:%S"), tag, severity, condition,
         event_time.strftime("%Y-%m-%d %H:%M:%S")),
    )
    connection.commit()
    connection.close()


class HealthEngineTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_db = Path(self.temp_dir.name) / "config.db"
        self.machine_db = Path(self.temp_dir.name) / "machine_data.db"
        _seed_config_db(self.config_db)  # runs migrate_baseline + migrate_anomaly internally
        migrate_equipment_metadata(self.config_db, backup=False)  # adds service_interval_days/last_serviced_at
        _create_machine_events_table(self.machine_db)
        DatabaseManager(db_path=self.machine_db)  # ensures plc_data exists too (unused by health engine, harmless)
        self.plant_id = _plant_id(self.config_db, "p01")
        self.equipment_id = _seed_equipment(self.config_db, "p01_pump_wsp01")
        _insert_tag(self.config_db, "P01.WATER.WSP01.Vibration", unit="mm/s", measurement="vibration")
        _insert_tag(self.config_db, "P01.WATER.WSP01.BearingTemp", unit="degC", measurement="temperature")
        _insert_tag(self.config_db, "P01.WATER.WSP01.Power_kW", unit="kW", measurement="power")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _calculate(self, instance_key="P01.WATER.WSP01", equipment_type="water_supply_pump", now=None):
        return he.calculate_health(self.config_db, self.machine_db, self.plant_id, "p01", equipment_type, instance_key, now=now or NOW)


# ---------------------------------------------------------------------------
# Registry sanity (pure, no fixtures needed)
# ---------------------------------------------------------------------------

class TestRegistrySanity(unittest.TestCase):
    def test_health_bands_cover_full_0_100_range_with_no_gaps(self):
        for score in [0, 0.1, 49, 49.99, 50, 50.01, 69.99, 70, 70.01, 84.99, 85, 85.01, 99.99, 100]:
            band = health_band_for_score(score)
            self.assertIn(band, [b[2] for b in HEALTH_BANDS])

    def test_boundary_scores_exact(self):
        self.assertEqual(health_band_for_score(85.0), "HEALTHY")
        self.assertEqual(health_band_for_score(84.99), "MONITOR")
        self.assertEqual(health_band_for_score(70.0), "MONITOR")
        self.assertEqual(health_band_for_score(69.99), "ATTENTION")
        self.assertEqual(health_band_for_score(50.0), "ATTENTION")
        self.assertEqual(health_band_for_score(49.99), "INVESTIGATE")
        self.assertEqual(health_band_for_score(0.0), "INVESTIGATE")
        self.assertEqual(health_band_for_score(100.0), "HEALTHY")

    def test_known_invalid_pump_hydraulic_efficiency_excluded(self):
        for path in ("engine/health_targets.py", "engine/health_evidence.py", "engine/health_engine.py"):
            source = Path(path).read_text().lower()
            self.assertNotIn("hydraulic_efficiency", source)
            self.assertNotIn("hydraulic efficiency", source)

    def test_no_llm_or_plc_imports(self):
        for path in ("engine/health_targets.py", "engine/health_evidence.py", "engine/health_engine.py"):
            source = Path(path).read_text()
            for banned in ("import ai.", "from ai.", "ai_provider", "plc.driver_factory", "opcua", "modbus",
                           "write_tag", "set_setpoint", "reset_alarm", "systemctl"):
                self.assertNotIn(banned, source, msg=f"found banned reference {banned!r} in {path}")

    def test_no_writes_to_anomaly_baseline_opportunity_savings_tables(self):
        for path in ("engine/health_targets.py", "engine/health_evidence.py", "engine/health_engine.py"):
            source = Path(path).read_text()
            for banned in (
                "INSERT INTO anomalies", "UPDATE anomalies", "INSERT INTO baseline_context_summary",
                "UPDATE baseline_context_summary", "INSERT INTO energy_opportunities", "UPDATE energy_opportunities",
                "INSERT INTO savings_interventions", "UPDATE savings_interventions",
                "INSERT INTO savings_verification_results", "INSERT INTO machine_events", "UPDATE machine_events",
            ):
                self.assertNotIn(banned, source, msg=f"found banned write {banned!r} in {path}")

    def test_family_caps_sum_leaves_a_floor_above_zero(self):
        self.assertLess(sum(FAMILY_MAX_PENALTY.values()), 100.0)


# ---------------------------------------------------------------------------
# CASE A - healthy, substantial usable evidence, no degradation (item 34.A)
# ---------------------------------------------------------------------------

class TestCaseAHealthy(HealthEngineTestBase):
    def test_healthy_strong_evidence_gives_high_score_and_high_confidence(self):
        for target in ("Vibration", "BearingTemp"):
            _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP01", target, "water_supply_pump", confidence="High")
        _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP01", "Power_kW", "water_supply_pump", confidence="High")
        _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP01", "Flow", "water_supply_pump", confidence="High")
        _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP01", "delta_p_bar", "water_supply_pump", confidence="High")
        _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP01", "flow_per_kw", "water_supply_pump", confidence="High")
        result = self._calculate()
        self.assertEqual(result.health_score, 100.0)
        self.assertEqual(result.health_band, "HEALTHY")
        self.assertIn(result.assessment_confidence, ("HIGH", "MEDIUM"))
        self.assertFalse(result.provisional if result.assessment_confidence == "HIGH" else False)


class TestNoEvidenceDoesNotMean100(HealthEngineTestBase):
    def test_zero_evidence_at_all_is_not_a_clean_100(self):
        """item 33 - the single most important guard: no evidence must
        NEVER silently become a perfect score."""
        result = self._calculate()
        self.assertIsNone(result.health_score)
        self.assertEqual(result.coverage_status, COVERAGE_INSUFFICIENT)
        self.assertEqual(result.assessment_confidence, COVERAGE_INSUFFICIENT)


# ---------------------------------------------------------------------------
# CASE B - attention: persistent condition drift + recurrence (item 34.B)
# ---------------------------------------------------------------------------

class TestCaseBAttention(HealthEngineTestBase):
    def test_persistent_vibration_with_recurrence_reduces_score_with_explanation(self):
        for target in ("Vibration", "BearingTemp", "Power_kW", "Flow", "delta_p_bar", "flow_per_kw"):
            _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP01", target, "water_supply_pump", confidence="Medium")
        anomaly_id = _seed_anomaly(
            self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "wsp_vibration_increase",
            status="OPEN", severity="HIGH", confidence="Medium",
        )
        # 5 prior RESOLVED occurrences - genuine recurrence, not a mutable counter.
        for i in range(5):
            _seed_anomaly(
                self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "wsp_vibration_increase",
                status="RESOLVED", severity="HIGH", confidence="Medium",
                resolved_at=(NOW - timedelta(days=60 + i)).strftime("%Y-%m-%d %H:%M:%S"),
            )
        result = self._calculate()
        self.assertLess(result.health_score, 100.0)
        vibration_factor = next(f for f in result.factor_results if f.factor_id == "wsp_vibration_condition")
        self.assertEqual(vibration_factor.status, "penalized")
        self.assertGreater(vibration_factor.contribution, 0)
        # No root-cause / named-fault claim anywhere in the reason text (item 27).
        for banned in ("bearing failure", "impeller damage", "misalignment", "worn"):
            self.assertNotIn(banned, vibration_factor.reason.lower())


# ---------------------------------------------------------------------------
# CASE C - energy issue but NOT a health issue (item 34.C, item 8)
# ---------------------------------------------------------------------------

class TestCaseCEnergyNotHealth(HealthEngineTestBase):
    def test_energy_category_anomaly_on_unregistered_rule_never_affects_health(self):
        """A plant-wide / after-hours energy anomaly uses rule_keys that
        simply don't appear in the health registry at all - so it can
        have no effect by construction, not by a special-case filter."""
        for target in ("Vibration", "BearingTemp", "Power_kW", "Flow", "delta_p_bar", "flow_per_kw"):
            _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP01", target, "water_supply_pump", confidence="High")
        clean_result = self._calculate()

        # An unrelated plant-wide energy anomaly for a DIFFERENT rule_key/target - never consumed by any registered factor.
        _seed_anomaly(
            self.config_db, self.plant_id, "P01.WATER.WSP01", "non_production_demand_kw",
            "plant_elevated_non_production_demand", status="OPEN", severity="HIGH", confidence="High", category="ENERGY",
        )
        after_result = self._calculate()
        self.assertEqual(clean_result.health_score, after_result.health_score)

    def test_source_registry_never_maps_a_pure_energy_rule_to_condition_family(self):
        from engine.health_targets import HEALTH_FACTOR_REGISTRY, FAMILY_CONDITION
        # Power/load-only rule_keys must never be registered under CONDITION - they're ELECTRICAL_LOAD at most.
        energy_only_rule_keys = {"plant_high_demand", "plant_elevated_non_production_demand"}
        for factors in HEALTH_FACTOR_REGISTRY.values():
            for f in factors:
                if f.source_rule_key in energy_only_rule_keys:
                    self.fail(f"plant-wide energy rule {f.source_rule_key!r} must not be registered as an equipment health factor at all")
                if f.family == FAMILY_CONDITION:
                    self.assertNotIn("power", f.source_rule_key or "", "power/load rule wrongly classified as CONDITION")


# ---------------------------------------------------------------------------
# CASE D - insufficient data (item 34.D)
# ---------------------------------------------------------------------------

class TestCaseDInsufficientData(HealthEngineTestBase):
    def test_most_factors_unavailable_gives_unavailable_score_never_100(self):
        result = self._calculate()
        self.assertIsNone(result.health_score)
        self.assertIsNone(result.health_band)
        self.assertEqual(result.coverage_status, COVERAGE_INSUFFICIENT)
        self.assertGreater(len(result.missing_factors), 0)


# ---------------------------------------------------------------------------
# CASE E - maintenance overdue only (item 34.E, item 15)
# ---------------------------------------------------------------------------

class TestCaseEMaintenanceOverdueOnly(HealthEngineTestBase):
    def test_overdue_maintenance_alone_gives_moderate_attention_not_failure_evidence(self):
        for target in ("Vibration", "BearingTemp", "Power_kW", "Flow", "delta_p_bar", "flow_per_kw"):
            _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP01", target, "water_supply_pump", confidence="High")
        connection = sqlite3.connect(self.config_db)
        connection.execute(
            "UPDATE equipment SET service_interval_days = 90, last_serviced_at = ?, next_due_at = ? WHERE id = ?",
            ("2026-01-01", "2026-04-01", self.equipment_id),
        )
        connection.commit()
        connection.close()

        result = self._calculate()
        maint_factor = next(f for f in result.factor_results if f.factor_id == "water_supply_pump_maintenance_overdue")
        self.assertEqual(maint_factor.status, "penalized")
        self.assertGreater(maint_factor.contribution, 0)
        self.assertLessEqual(maint_factor.contribution, FAMILY_MAX_PENALTY["MAINTENANCE"])
        self.assertIn("risk/attention factor", maint_factor.reason)
        self.assertNotIn("physical degradation", maint_factor.reason.replace("not evidence of physical degradation", ""))
        self.assertGreaterEqual(result.health_score, 70.0)  # moderate, not severe


# ---------------------------------------------------------------------------
# Double-counting / family caps (items 9, 33)
# ---------------------------------------------------------------------------

class TestDoubleCountingPrevention(HealthEngineTestBase):
    def test_condition_family_capped_even_with_three_maxed_out_factors(self):
        for target in ("Vibration", "BearingTemp", "flow_per_kw"):
            _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP01", target, "water_supply_pump", confidence="High")
        _seed_anomaly(self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "wsp_vibration_increase", status="OPEN", severity="HIGH", confidence="High")
        _seed_anomaly(self.config_db, self.plant_id, "P01.WATER.WSP01", "BearingTemp", "wsp_bearing_temp_increase", status="OPEN", severity="HIGH", confidence="High")
        _seed_anomaly(self.config_db, self.plant_id, "P01.WATER.WSP01", "flow_per_kw", "wsp_flow_per_kw_drift", status="OPEN", severity="HIGH", confidence="High")

        result = self._calculate()
        condition_total = sum(f.contribution for f in result.factor_results if f.family == FAMILY_CONDITION)
        self.assertLessEqual(condition_total, FAMILY_MAX_PENALTY[FAMILY_CONDITION])
        # All three factors individually maxed (20*1.0*1.0*1.0=20, each capped at their own max_penalty)
        # would raw-sum to 15+15+12=42 - the family cap (30) must have actually engaged.
        raw_individual_caps_sum = 15.0 + 15.0 + 12.0
        self.assertLess(condition_total, raw_individual_caps_sum)


# ---------------------------------------------------------------------------
# Recency (item 10, 33)
# ---------------------------------------------------------------------------

class TestRecency(HealthEngineTestBase):
    def _score_with_status(self, status, resolved_at=None, severity="HIGH"):
        _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "water_supply_pump", confidence="High")
        _seed_anomaly(
            self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "wsp_vibration_increase",
            status=status, severity=severity, confidence="High", resolved_at=resolved_at,
        )
        result = self._calculate()
        return next(f for f in result.factor_results if f.factor_id == "wsp_vibration_condition")

    def test_open_anomaly_gets_full_penalty(self):
        factor = self._score_with_status("OPEN")
        self.assertEqual(factor.status, "penalized")
        self.assertAlmostEqual(factor.contribution, 15.0, places=1)  # capped at max_penalty

    def test_recently_resolved_gets_reduced_but_nonzero_penalty(self):
        # WARNING (base 12), not HIGH (base 20) - a HIGH/High-confidence/OPEN
        # combination already saturates this factor's own 15.0 cap even
        # after a modest recency decay, which would mask the recency
        # effect this test exists to demonstrate.
        factor = self._score_with_status("RESOLVED", resolved_at=(NOW - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S"), severity="WARNING")
        self.assertEqual(factor.status, "penalized")
        self.assertGreater(factor.contribution, 0)
        self.assertLess(factor.contribution, 12.0)

    def test_long_resolved_gets_little_or_no_penalty(self):
        factor = self._score_with_status("RESOLVED", resolved_at=(NOW - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S"))
        self.assertIn(factor.status, ("clean",))
        self.assertEqual(factor.contribution, 0.0)


# ---------------------------------------------------------------------------
# Recurrence (item 11, 33)
# ---------------------------------------------------------------------------

class TestRecurrence(HealthEngineTestBase):
    def _score_with_prior_occurrences(self, n):
        _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "water_supply_pump", confidence="High")
        _seed_anomaly(self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "wsp_vibration_increase", status="OPEN", severity="WARNING", confidence="High")
        for i in range(n):
            _seed_anomaly(
                self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "wsp_vibration_increase",
                status="RESOLVED", severity="WARNING", confidence="High",
                resolved_at=(NOW - timedelta(days=200 + i)).strftime("%Y-%m-%d %H:%M:%S"),  # long-decayed, isolates recurrence effect
            )
        result = self._calculate()
        return next(f for f in result.factor_results if f.factor_id == "wsp_vibration_condition")

    def test_more_recurrence_increases_contribution_up_to_a_cap(self):
        low_recurrence = self._score_with_prior_occurrences(0)
        self.setUp()  # fresh isolated DB for the second scenario
        high_recurrence = self._score_with_prior_occurrences(8)
        self.assertGreaterEqual(high_recurrence.contribution, low_recurrence.contribution)

    def test_recurrence_never_grows_penalty_unbounded(self):
        factor = self._score_with_prior_occurrences(50)
        self.assertLessEqual(factor.contribution, factor.max_penalty)


# ---------------------------------------------------------------------------
# Confidence gating (item 13, 33)
# ---------------------------------------------------------------------------

class TestConfidenceGating(HealthEngineTestBase):
    def _score_with_confidence(self, confidence):
        _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "water_supply_pump", confidence=confidence)
        _seed_anomaly(self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "wsp_vibration_increase", status="OPEN", severity="HIGH", confidence=confidence)
        result = self._calculate()
        return next(f for f in result.factor_results if f.factor_id == "wsp_vibration_condition")

    def test_low_confidence_contributes_less_than_high_confidence(self):
        low = self._score_with_confidence("Low")
        self.setUp()
        high = self._score_with_confidence("High")
        self.assertLess(low.contribution, high.contribution)

    def test_insufficient_confidence_is_excluded_not_used_as_evidence(self):
        factor = self._score_with_confidence("Insufficient")
        self.assertEqual(factor.status, "missing")
        self.assertEqual(factor.contribution, 0.0)


# ---------------------------------------------------------------------------
# Baseline maturity distinguishes clean vs missing (item 19 - critical)
# ---------------------------------------------------------------------------

class TestBaselineMaturityDistinguishesCleanFromMissing(HealthEngineTestBase):
    def test_no_anomaly_row_with_mature_baseline_is_clean(self):
        _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "water_supply_pump")
        result = self._calculate()
        factor = next(f for f in result.factor_results if f.factor_id == "wsp_vibration_condition")
        self.assertEqual(factor.status, "clean")

    def test_no_anomaly_row_without_mature_baseline_is_missing(self):
        result = self._calculate()
        factor = next(f for f in result.factor_results if f.factor_id == "wsp_vibration_condition")
        self.assertEqual(factor.status, "missing")


# ---------------------------------------------------------------------------
# Maintenance missing vs overdue (item 15, 33)
# ---------------------------------------------------------------------------

class TestMaintenanceEvidence(HealthEngineTestBase):
    def test_no_schedule_data_is_missing_not_fabricated_overdue(self):
        result = self._calculate()
        factor = next(f for f in result.factor_results if f.factor_id == "water_supply_pump_maintenance_overdue")
        self.assertEqual(factor.status, "missing")

    def test_on_schedule_is_clean(self):
        connection = sqlite3.connect(self.config_db)
        connection.execute(
            "UPDATE equipment SET service_interval_days = 365, last_serviced_at = ?, next_due_at = ? WHERE id = ?",
            ("2026-01-01", (NOW + timedelta(days=100)).strftime("%Y-%m-%d"), self.equipment_id),
        )
        connection.commit()
        connection.close()
        result = self._calculate()
        factor = next(f for f in result.factor_results if f.factor_id == "water_supply_pump_maintenance_overdue")
        self.assertEqual(factor.status, "clean")


# ---------------------------------------------------------------------------
# Criticality never affects score (item 16, 33)
# ---------------------------------------------------------------------------

class TestCriticalityNeverAffectsScore(HealthEngineTestBase):
    def test_configured_vs_missing_criticality_gives_identical_score(self):
        for target in ("Vibration", "BearingTemp", "Power_kW", "Flow", "delta_p_bar", "flow_per_kw"):
            _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP01", target, "water_supply_pump", confidence="High")
        without_criticality = self._calculate()
        self.assertIsNone(without_criticality.criticality)

        connection = sqlite3.connect(self.config_db)
        connection.execute("UPDATE equipment SET criticality = 'HIGH' WHERE id = ?", (self.equipment_id,))
        connection.commit()
        connection.close()
        with_criticality = self._calculate()
        self.assertEqual(with_criticality.criticality, "HIGH")
        self.assertEqual(without_criticality.health_score, with_criticality.health_score)



# ---------------------------------------------------------------------------
# Plant / equipment isolation (item 33)
# ---------------------------------------------------------------------------

class TestIsolation(HealthEngineTestBase):
    def test_p01_p02_remain_isolated(self):
        p2_id = _plant_id(self.config_db, "p02")
        _seed_equipment(self.config_db, "p02_pump_wsp01")
        _insert_tag(self.config_db, "P02.WATER.WSP01.Vibration", unit="mm/s", measurement="vibration")
        _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "water_supply_pump", confidence="High")
        _seed_mature_baseline(self.config_db, p2_id, "P02.WATER.WSP01", "Vibration", "water_supply_pump", confidence="High")
        _seed_anomaly(self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "wsp_vibration_increase", status="OPEN", severity="HIGH", confidence="High")

        p1_result = self._calculate("P01.WATER.WSP01")
        p2_connection_check = he.calculate_health(self.config_db, self.machine_db, p2_id, "p02", "water_supply_pump", "P02.WATER.WSP01", now=NOW)

        p1_factor = next(f for f in p1_result.factor_results if f.factor_id == "wsp_vibration_condition")
        p2_factor = next(f for f in p2_connection_check.factor_results if f.factor_id == "wsp_vibration_condition")
        self.assertEqual(p1_factor.status, "penalized")
        self.assertEqual(p2_factor.status, "clean")  # P02's own anomaly-free, mature-baseline instance unaffected

    def test_one_equipment_instance_does_not_affect_another(self):
        _seed_equipment(self.config_db, "p01_pump_wsp02")
        _insert_tag(self.config_db, "P01.WATER.WSP02.Vibration", unit="mm/s", measurement="vibration")
        _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "water_supply_pump", confidence="High")
        _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP02", "Vibration", "water_supply_pump", confidence="High")
        _seed_anomaly(self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "wsp_vibration_increase", status="OPEN", severity="HIGH", confidence="High")

        wsp1 = self._calculate("P01.WATER.WSP01")
        wsp2 = self._calculate("P01.WATER.WSP02")
        wsp1_factor = next(f for f in wsp1.factor_results if f.factor_id == "wsp_vibration_condition")
        wsp2_factor = next(f for f in wsp2.factor_results if f.factor_id == "wsp_vibration_condition")
        self.assertEqual(wsp1_factor.status, "penalized")
        self.assertEqual(wsp2_factor.status, "clean")


# ---------------------------------------------------------------------------
# Determinism (item 33)
# ---------------------------------------------------------------------------

class TestDeterminism(HealthEngineTestBase):
    def test_repeated_calls_with_unchanged_data_are_identical(self):
        for target in ("Vibration", "BearingTemp"):
            _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP01", target, "water_supply_pump", confidence="High")
        _seed_anomaly(self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "wsp_vibration_increase", status="OPEN", severity="HIGH", confidence="High")
        first = self._calculate()
        second = self._calculate()
        self.assertEqual(first.health_score, second.health_score)
        self.assertEqual([(f.factor_id, f.contribution, f.status) for f in first.factor_results],
                          [(f.factor_id, f.contribution, f.status) for f in second.factor_results])


# ---------------------------------------------------------------------------
# Score boundaries / explanation sums (items 12, 23, 33)
# ---------------------------------------------------------------------------

class TestScoreBoundsAndExplanation(HealthEngineTestBase):
    def test_score_never_below_zero_even_with_extreme_evidence(self):
        for target, rule, family_target in (
            ("Vibration", "wsp_vibration_increase", None), ("BearingTemp", "wsp_bearing_temp_increase", None),
            ("flow_per_kw", "wsp_flow_per_kw_drift", None), ("Power_kW", "wsp_power_deviation", None),
            ("Flow", "wsp_flow_deviation", None), ("delta_p_bar", "wsp_delta_p_deviation", None),
        ):
            _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP01", target, "water_supply_pump", confidence="High")
            _seed_anomaly(self.config_db, self.plant_id, "P01.WATER.WSP01", target, rule, status="OPEN", severity="HIGH", confidence="High")
            for i in range(20):
                _seed_anomaly(
                    self.config_db, self.plant_id, "P01.WATER.WSP01", target, rule, status="RESOLVED", severity="HIGH", confidence="High",
                    resolved_at=(NOW - timedelta(days=1 + i)).strftime("%Y-%m-%d %H:%M:%S"),
                )
        for i in range(100):
            _seed_event(self.machine_db, "P01.WATER.WSP01.Vibration", f"cond{i}", "alarm", NOW - timedelta(hours=i))
        connection = sqlite3.connect(self.config_db)
        connection.execute(
            "UPDATE equipment SET service_interval_days = 30, last_serviced_at = ?, next_due_at = ? WHERE id = ?",
            ("2020-01-01", "2020-02-01", self.equipment_id),
        )
        connection.commit()
        connection.close()

        result = self._calculate()
        self.assertGreaterEqual(result.health_score, 0.0)
        self.assertLessEqual(result.health_score, 100.0)

    def test_score_equals_100_minus_family_capped_penalty_sum(self):
        for target in ("Vibration", "BearingTemp", "Power_kW", "Flow", "delta_p_bar", "flow_per_kw"):
            _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP01", target, "water_supply_pump", confidence="High")
        _seed_anomaly(self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "wsp_vibration_increase", status="OPEN", severity="WARNING", confidence="High")
        result = self._calculate()
        family_totals = {}
        for f in result.factor_results:
            family_totals.setdefault(f.family, 0.0)
            family_totals[f.family] += f.contribution
        capped_total = sum(min(v, FAMILY_MAX_PENALTY[k]) for k, v in family_totals.items())
        self.assertAlmostEqual(result.health_score, round(max(0.0, 100.0 - capped_total), 1), places=1)


# ---------------------------------------------------------------------------
# Coverage counting (item 18, 33)
# ---------------------------------------------------------------------------

class TestCoverageCounting(HealthEngineTestBase):
    def test_applicable_and_usable_counts_correct(self):
        for target in ("Vibration", "BearingTemp"):
            _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP01", target, "water_supply_pump", confidence="High")
        result = self._calculate()
        self.assertEqual(result.applicable_factor_count, 8)  # water_supply_pump has 8 registered factors
        self.assertEqual(result.usable_factor_count, 2)  # only Vibration + BearingTemp have mature baselines
        self.assertEqual(len(result.missing_factors), 6)


# ---------------------------------------------------------------------------
# Events factor - distinct-condition counting regression guard
# ---------------------------------------------------------------------------

class TestEventsFactorCountsDistinctConditions(HealthEngineTestBase):
    def test_many_repeated_rows_of_the_same_condition_do_not_saturate_alone(self):
        for i in range(50):
            _seed_event(self.machine_db, "P01.WATER.WSP01.Vibration", "high_warning", "warning", NOW - timedelta(hours=i))
        evidence = hev.fetch_events_evidence(self.machine_db, "P01.WATER.WSP01", NOW)
        self.assertEqual(evidence["qualifying_event_count"], 1)  # one DISTINCT (tag, condition) pair, not 50

    def test_distinct_conditions_each_count_once(self):
        _seed_event(self.machine_db, "P01.WATER.WSP01.Vibration", "high_warning", "warning", NOW)
        _seed_event(self.machine_db, "P01.WATER.WSP01.BearingTemp", "high_alarm", "alarm", NOW)
        _seed_event(self.machine_db, "P01.WATER.WSP01.Power_kW", "high_warning", "warning", NOW)
        evidence = hev.fetch_events_evidence(self.machine_db, "P01.WATER.WSP01", NOW)
        self.assertEqual(evidence["qualifying_event_count"], 3)

    def test_unrelated_equipment_events_never_counted(self):
        _seed_event(self.machine_db, "P01.WATER.WSP02.Vibration", "high_warning", "warning", NOW)
        evidence = hev.fetch_events_evidence(self.machine_db, "P01.WATER.WSP01", NOW)
        self.assertEqual(evidence["qualifying_event_count"], 0)


# ---------------------------------------------------------------------------
# Freshness gates (items 17, 36)
# ---------------------------------------------------------------------------

class TestFreshnessGates(HealthEngineTestBase):
    def test_stale_anomaly_feed_makes_anomaly_factors_missing_not_clean(self):
        # A mature baseline whose OWN computed_at is old - this is the
        # real system-wide "has the baseline/anomaly pipeline run
        # recently" signal (see engine.health_evidence.anomaly_feed_is_fresh's
        # docstring: anomalies.last_seen alone can't distinguish a dead
        # worker from a genuinely healthy population with zero anomalies).
        connection = sqlite3.connect(self.config_db)
        connection.execute(
            "INSERT INTO baseline_context_summary "
            "(plant_id, instance_key, target_key, equipment_type, baseline_type, context_bucket_key, "
            " baseline_level, baseline_status, confidence, representative_sample_count, computed_at) "
            "VALUES (?, 'P01.WATER.WSP01', 'Vibration', 'water_supply_pump', 'recent', 'default', 'B', 'mature', 'High', 40, ?)",
            (self.plant_id, (NOW - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")),
        )
        connection.commit()
        connection.close()
        _seed_anomaly(
            self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "wsp_vibration_increase",
            status="OPEN", severity="HIGH", confidence="High",
        )
        result = self._calculate()  # baseline pipeline's own computed_at is 10 days old - stale
        factor = next(f for f in result.factor_results if f.factor_id == "wsp_vibration_condition")
        self.assertEqual(factor.status, "missing")
        self.assertIn("stale", " ".join(result.limitations).lower())

    def test_stale_events_feed_makes_events_factor_missing(self):
        _seed_event(self.machine_db, "P01.WATER.WSP01.Vibration", "high_warning", "warning", NOW - timedelta(days=10))
        result = self._calculate()
        factor = next(f for f in result.factor_results if f.factor_id == "water_supply_pump_repeated_events")
        self.assertEqual(factor.status, "missing")


# ---------------------------------------------------------------------------
# Existing semantics unchanged - no mutation of any Phase 8-11 table
# ---------------------------------------------------------------------------

class TestNoMutationOfExistingTables(HealthEngineTestBase):
    def test_calculate_health_never_writes_anything(self):
        _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "water_supply_pump", confidence="High")
        _seed_anomaly(self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "wsp_vibration_increase", status="OPEN", severity="HIGH", confidence="High")

        def _snapshot():
            connection = sqlite3.connect(self.config_db)
            snap = {
                "anomalies": connection.execute("SELECT * FROM anomalies ORDER BY id").fetchall(),
                "baseline_context_summary": connection.execute("SELECT * FROM baseline_context_summary ORDER BY id").fetchall(),
                "equipment": connection.execute("SELECT * FROM equipment ORDER BY id").fetchall(),
            }
            connection.close()
            return snap

        before = _snapshot()
        self._calculate()
        self._calculate()
        after = _snapshot()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
