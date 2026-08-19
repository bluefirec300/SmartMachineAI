import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from database.database import DatabaseManager
from engine import savings_verification_domain as dom
from engine import savings_verification_engine as calc
from engine import savings_verification_evidence as ev
from engine.baseline_targets import BaselineTarget
from engine.energy_tariff import TARIFF_PROVENANCE_CONFIGURED, TARIFF_PROVENANCE_SIMULATION, create_tariff
from engine.opportunity_migrator import migrate as migrate_opportunity
from engine.savings_verification_migrator import migrate as migrate_savings_verification
from engine.savings_verification_targets import ACTION_CATEGORY_SETPOINT_CHANGE

from tests.test_anomaly_engine import _seed_config_db, _insert_tag, _insert_tariff, _plant_id


IMPLEMENTED_AT = datetime(2026, 8, 25, 0, 0, 0)  # well after MODERN_DATA_BOUNDARY


def _seed_opportunity(config_db: Path, plant_id: int, instance_key: str = "P01.UTILITY.CHL01") -> int:
    now_text = IMPLEMENTED_AT.strftime("%Y-%m-%d %H:%M:%S")
    row = dict(
        plant_id=plant_id, equipment_id=None, instance_key=instance_key, rule_key="chiller_power_opportunity",
        category="CHILLER", title="t", status="NEW", priority="LOW", priority_score=4.0,
        priority_breakdown_json="{}", confidence="High", source_anomaly_ids="[1]",
        source_anomaly_rule_key="chiller_power_deviation", first_identified=now_text, last_updated=now_text,
        dismissed_at=None, dismissal_reason=None, dismissal_comment=None, occurrence_count=1,
        observed_period_start=now_text, observed_period_end=now_text, observed_excess_energy_kwh=6.5,
        observed_excess_cost=3.38, observed_cost_currency="MYR", saving_basis="unavailable",
        saving_unavailable_reason="no defensible method", estimated_potential_saving_period=None,
        estimated_monthly_saving=None, estimated_annual_saving=None, annualization_method="unavailable_conservative",
        saving_currency=None, tariff_provenance="SIMULATION", implementation_difficulty="UNKNOWN",
        equipment_criticality=None, recommendation="r", rule_provenance="SIMULATION_TUNING",
        evidence_json="{}", assumptions_json="[]", limitations_json="[]", created_at=now_text, updated_at=now_text,
    )
    connection = sqlite3.connect(config_db)
    cols = list(row.keys())
    cursor = connection.execute(f"INSERT INTO energy_opportunities ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [row[c] for c in cols])
    connection.commit()
    new_id = cursor.lastrowid
    connection.close()
    return new_id


def _seed_daily(historian: DatabaseManager, tag: str, start_day_offset: int, end_day_offset: int, value: float, hour: int = 14) -> None:
    for day in range(start_day_offset, end_day_offset + 1):
        t = IMPLEMENTED_AT + timedelta(days=day, hours=hour)
        historian.save_tag(tag, "sim", value, t)


SIMPLE_TARGET = BaselineTarget(
    plant_code="p01", instance_key="P01.UTILITY.CHL01", equipment_type="chiller", target_key="Power_kW",
    tag_name="P01.UTILITY.CHL01.Power_kW", is_derived=False, context_dimensions=(), aggregation_minutes=5,
    reference_window_days_max=60, recent_window_days=7,
)


class EngineTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_db = Path(self.temp_dir.name) / "config.db"
        self.machine_db = Path(self.temp_dir.name) / "machine_data.db"
        _seed_config_db(self.config_db)
        migrate_opportunity(self.config_db, backup=False)
        migrate_savings_verification(self.config_db, backup=False)
        self.historian = DatabaseManager(db_path=self.machine_db)
        self.plant_id = _plant_id(self.config_db, "p01")
        _insert_tag(self.config_db, "P01.UTILITY.CHL01.Power_kW", unit="kW", measurement="power")
        self.opportunity_id = _seed_opportunity(self.config_db, self.plant_id)
        self.intervention_id = dom.create_intervention(
            self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01",
            ACTION_CATEGORY_SETPOINT_CHANGE, "Lowered setpoint", "test_engineer",
        )
        dom.mark_implemented(self.config_db, self.intervention_id, implemented_at=IMPLEMENTED_AT)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _calc(self, now):
        with patch("engine.savings_verification_evidence.resolve_target_for_intervention", return_value=SIMPLE_TARGET):
            return calc.calculate_verified_savings(self.config_db, self.machine_db, self.historian, self.intervention_id, now=now)

    def _seed_symmetric(self, before_value: float, after_value: float, before_days=15, after_start=3, after_days=15, tariff=True):
        if tariff:
            _insert_tariff(self.config_db, self.plant_id, energy_rate=0.5, currency="MYR", effective_date="2026-08-01")
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", -before_days, -1, before_value)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", after_start, after_start + after_days - 1, after_value)
        return IMPLEMENTED_AT + timedelta(days=after_start + after_days + 3)


# ---------------------------------------------------------------------------
# Core positive/negative/zero saving scenarios (item 14)
# ---------------------------------------------------------------------------

class TestSavingScenarios(EngineTestBase):
    def test_positive_saving_is_verified(self):
        now = self._seed_symmetric(60.0, 50.0)
        result = self._calc(now)
        self.assertEqual(result["verification_result"], "VERIFIED")
        self.assertGreater(result["absolute_energy_saving_kwh"], 0)
        self.assertAlmostEqual(result["absolute_energy_saving_kwh"], 15 * 10.0 * (5 / 60.0), places=3)
        self.assertAlmostEqual(result["energy_saving_percentage"], (60.0 - 50.0) / 60.0 * 100, places=2)

    def test_negative_saving_is_rejected_not_clamped_to_zero(self):
        """Item 5/8 - an intervention that made things WORSE must show a
        real negative number, never clamped to zero or hidden."""
        now = self._seed_symmetric(50.0, 60.0)  # AFTER uses MORE power
        result = self._calc(now)
        self.assertEqual(result["verification_result"], "REJECTED")
        self.assertLess(result["absolute_energy_saving_kwh"], 0)
        self.assertAlmostEqual(result["absolute_energy_saving_kwh"], 15 * -10.0 * (5 / 60.0), places=3)

    def test_zero_saving_is_rejected_not_verified(self):
        """Item 8 - exactly zero must be REJECTED, not VERIFIED (the
        gate is strictly > 0)."""
        now = self._seed_symmetric(50.0, 50.0)
        result = self._calc(now)
        self.assertEqual(result["verification_result"], "REJECTED")
        self.assertEqual(result["absolute_energy_saving_kwh"], 0.0)

    def test_percentage_saving_formula_is_exact(self):
        now = self._seed_symmetric(80.0, 60.0)
        result = self._calc(now)
        self.assertAlmostEqual(result["energy_saving_percentage"], 25.0, places=2)  # (80-60)/80*100

    def test_zero_before_denominator_gives_no_percentage_but_keeps_absolute_saving(self):
        now = self._seed_symmetric(0.2, 0.0)  # both under MIN_POWER_DENOMINATOR_KW
        result = self._calc(now)
        self.assertIsNone(result["energy_saving_percentage"])
        self.assertIsNotNone(result["absolute_energy_saving_kwh"])  # absolute figure never depends on the ratio

    def test_tiny_denominator_reported_unavailable_not_huge_percentage(self):
        now = self._seed_symmetric(0.3, 0.1)  # below MIN_POWER_DENOMINATOR_KW threshold
        result = self._calc(now)
        self.assertIsNone(result["energy_saving_percentage"])
        self.assertIn("too close to zero", " ".join(result["limitations"]))


# ---------------------------------------------------------------------------
# Evidence-quality gating (item 9)
# ---------------------------------------------------------------------------

class TestEvidenceGating(EngineTestBase):
    def test_insufficient_evidence_is_inconclusive_no_financial_fields(self):
        result = self._calc(IMPLEMENTED_AT + timedelta(days=5))  # no historian data seeded at all
        self.assertEqual(result["verification_result"], "INCONCLUSIVE")
        self.assertIsNone(result["absolute_energy_saving_kwh"])
        self.assertIsNone(result["cost_saving"])

    def test_limited_evidence_from_real_target_context_is_inconclusive(self):
        """Using the REAL registry-resolved chiller target (with its
        real load/ambient/hour context dimensions, none of which are
        seeded here) - context matching legitimately fails to find a
        shared dimension, giving LIMITED, which must gate to
        INCONCLUSIVE even though raw sample counts are healthy."""
        _insert_tariff(self.config_db, self.plant_id, energy_rate=0.5, currency="MYR", effective_date="2026-08-01")
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", -15, -1, 60.0)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 3, 17, 50.0)
        now = IMPLEMENTED_AT + timedelta(days=20)
        result = calc.calculate_verified_savings(self.config_db, self.machine_db, self.historian, self.intervention_id, now=now)
        self.assertEqual(result["evidence_quality"], "LIMITED")
        self.assertEqual(result["verification_result"], "INCONCLUSIVE")
        self.assertIsNone(result["absolute_energy_saving_kwh"])

    def test_context_matched_evidence_can_be_verified(self):
        now = self._seed_symmetric(60.0, 50.0)
        result = self._calc(now)
        self.assertEqual(result["evidence_quality"], "CONTEXT_MATCHED")
        self.assertEqual(result["verification_result"], "VERIFIED")

    def test_stabilization_unfinished_is_inconclusive(self):
        result = self._calc(IMPLEMENTED_AT + timedelta(hours=1))
        self.assertEqual(result["verification_result"], "INCONCLUSIVE")
        self.assertIn("stabilization", result["reason"].lower())

    def test_no_after_evidence_is_inconclusive(self):
        _insert_tariff(self.config_db, self.plant_id, energy_rate=0.5, currency="MYR", effective_date="2026-08-01")
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", -15, -1, 60.0)
        result = self._calc(IMPLEMENTED_AT + timedelta(days=5))
        self.assertEqual(result["verification_result"], "INCONCLUSIVE")
        self.assertIsNone(result["absolute_energy_saving_kwh"])

    def test_no_before_evidence_is_inconclusive(self):
        _insert_tariff(self.config_db, self.plant_id, energy_rate=0.5, currency="MYR", effective_date="2026-08-01")
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 3, 17, 50.0)
        result = self._calc(IMPLEMENTED_AT + timedelta(days=20))
        self.assertEqual(result["verification_result"], "INCONCLUSIVE")
        self.assertIsNone(result["absolute_energy_saving_kwh"])


# ---------------------------------------------------------------------------
# Tariff availability independent of energy validity (item 6)
# ---------------------------------------------------------------------------

class TestTariff(EngineTestBase):
    def test_missing_tariff_leaves_energy_valid_but_cost_unavailable(self):
        now = self._seed_symmetric(60.0, 50.0, tariff=False)  # no tariff configured at all
        result = self._calc(now)
        self.assertEqual(result["verification_result"], "VERIFIED")
        self.assertIsNotNone(result["absolute_energy_saving_kwh"])
        self.assertIsNone(result["cost_saving"])
        self.assertIsNone(result["cost_currency"])

    def test_tariff_available_gives_real_cost(self):
        now = self._seed_symmetric(60.0, 50.0, tariff=True)
        result = self._calc(now)
        self.assertIsNotNone(result["cost_saving"])
        self.assertEqual(result["cost_currency"], "MYR")
        self.assertAlmostEqual(result["cost_saving"], result["absolute_energy_saving_kwh"] * 0.5, places=3)

    # -----------------------------------------------------------------
    # Phase 18.1a.1 - tariff_provenance correction. _seed_symmetric's
    # own _insert_tariff() always inserts is_simulated=1 (shared fixture
    # helper, left untouched) - covers the SIMULATION path by default;
    # a genuinely CONFIGURED (is_simulated=0) tariff is inserted
    # directly via engine.energy_tariff.create_tariff() below.
    # -----------------------------------------------------------------

    def test_missing_tariff_leaves_provenance_none(self):
        now = self._seed_symmetric(60.0, 50.0, tariff=False)
        result = self._calc(now)
        self.assertIsNone(result["tariff_provenance"])

    def test_simulated_tariff_gives_simulation_provenance(self):
        now = self._seed_symmetric(60.0, 50.0, tariff=True)
        result = self._calc(now)
        self.assertEqual(result["tariff_provenance"], TARIFF_PROVENANCE_SIMULATION)

    def test_real_configured_tariff_gives_configured_provenance(self):
        create_tariff(
            self.config_db, plant_id=self.plant_id, effective_date="2026-08-01",
            username="admin", currency="MYR", energy_rate=0.5,
        )
        now = self._seed_symmetric(60.0, 50.0, tariff=False)  # tariff already configured above
        result = self._calc(now)
        self.assertIsNotNone(result["cost_saving"])
        self.assertEqual(result["tariff_provenance"], TARIFF_PROVENANCE_CONFIGURED)

    def test_provenance_never_inferred_from_currency(self):
        # A real, CONFIGURED tariff in a non-MYR currency must still be
        # labeled CONFIGURED - never treated as simulation merely
        # because it isn't MYR.
        create_tariff(
            self.config_db, plant_id=self.plant_id, effective_date="2026-08-01",
            username="admin", currency="USD", energy_rate=0.5,
        )
        now = self._seed_symmetric(60.0, 50.0, tariff=False)
        result = self._calc(now)
        self.assertEqual(result["cost_currency"], "USD")
        self.assertEqual(result["tariff_provenance"], TARIFF_PROVENANCE_CONFIGURED)


# ---------------------------------------------------------------------------
# Maintenance exclusion (item 2/12)
# ---------------------------------------------------------------------------

class TestMaintenanceExclusion(EngineTestBase):
    def _insert_maintenance(self, performed_at: datetime):
        connection = sqlite3.connect(self.config_db)
        equipment_id = connection.execute("SELECT id FROM equipment WHERE name = 'p01_chiller_chl01'").fetchone()[0]
        connection.execute(
            "INSERT INTO maintenance_log (equipment_id, category, description, performed_at, next_due_at, created_at) VALUES (?, 'preventive', 'test', ?, NULL, ?)",
            (equipment_id, performed_at.strftime("%Y-%m-%d %H:%M:%S"), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        connection.commit()
        connection.close()

    def test_maintenance_overlap_excludes_some_after_buckets_and_reduces_count(self):
        """25 AFTER days seeded (comfortably above the mature/bootstrap
        threshold of 15) - excluding 1 to maintenance still leaves 24,
        well above the threshold, so the result stays reachable rather
        than degrading purely from losing a single day's evidence."""
        now = self._seed_symmetric(60.0, 50.0, after_days=25)
        self._insert_maintenance(IMPLEMENTED_AT + timedelta(days=5, hours=14))  # inside the AFTER window
        result = self._calc(now)
        self.assertGreater(result["exclusion_summary"]["maintenance_contaminated_after_buckets_excluded"], 0)
        self.assertLess(result["usable_after_sample_count"], 25)
        # Still enough evidence to reach a real result (not every bucket excluded).
        self.assertIn(result["verification_result"], ("VERIFIED", "REJECTED"))

    def test_maintenance_excluding_all_usable_evidence_forces_inconclusive(self):
        """20 AFTER days seeded (passes the INITIAL evidence-quality
        gate on its own), but maintenance covers every single one of
        them - after exclusion, zero buckets survive, forcing
        INCONCLUSIVE at the post-exclusion gate specifically (distinct
        from simply never having had enough evidence to begin with)."""
        _insert_tariff(self.config_db, self.plant_id, energy_rate=0.5, currency="MYR", effective_date="2026-08-01")
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", -15, -1, 60.0)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 3, 22, 50.0)  # 20 days - comfortably mature on its own
        for day in range(3, 23):
            self._insert_maintenance(IMPLEMENTED_AT + timedelta(days=day, hours=14))
        now = IMPLEMENTED_AT + timedelta(days=25)
        result = self._calc(now)
        self.assertEqual(result["verification_result"], "INCONCLUSIVE")
        self.assertEqual(result["usable_after_sample_count"], 0)
        self.assertIn("maintenance", result["reason"].lower())

    def test_maintenance_exclusion_never_silently_hidden(self):
        now = self._seed_symmetric(60.0, 50.0)
        self._insert_maintenance(IMPLEMENTED_AT + timedelta(days=5, hours=14))
        result = self._calc(now)
        self.assertIn("maintenance_contaminated_after_buckets_excluded", result["exclusion_summary"])
        self.assertGreaterEqual(result["exclusion_summary"]["maintenance_contaminated_after_buckets_excluded"], 1)


# ---------------------------------------------------------------------------
# Anomaly semantics preserved (item 13)
# ---------------------------------------------------------------------------

class TestAnomalyRetained(EngineTestBase):
    def test_anomaly_present_is_reported_but_never_excludes_data(self):
        now = self._seed_symmetric(60.0, 50.0)
        connection = sqlite3.connect(self.config_db)
        anomaly_time = (IMPLEMENTED_AT + timedelta(days=5, hours=14)).strftime("%Y-%m-%d %H:%M:%S")
        row = dict(
            plant_id=self.plant_id, equipment_id=None, instance_key="P01.UTILITY.CHL01", target_key="Power_kW",
            rule_key="chiller_power_deviation", anomaly_type="deviation", category="UTILITY", title="t",
            severity="ATTENTION", confidence="High", provisional=0, status="RESOLVED",
            first_detected=anomaly_time, last_seen=anomaly_time, resolved_at=anomaly_time, occurrence_count=1,
            open_persistence_periods=3, resolve_persistence_periods=0, last_bucket_start=anomaly_time,
            actual_value=90.0, expected_value=50.0, expected_low=40.0, expected_high=60.0, deviation_absolute=40.0,
            deviation_percent=80.0, normalized_deviation=13.3, baseline_type="recent", baseline_level="B",
            baseline_confidence="High", engineering_limit_status="not_exceeded", engineering_limit_event_id=None,
            estimated_excess_energy_kwh=1.0, estimated_excess_cost=1.0, estimated_excess_cost_currency="MYR",
            threshold_provenance="SIMULATION_TUNING", source_tags="[]", evidence_json="{}", assumptions_json="[]",
            data_limitations_json="[]", created_at=anomaly_time, updated_at=anomaly_time,
        )
        cols = list(row.keys())
        connection.execute(f"INSERT INTO anomalies ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [row[c] for c in cols])
        connection.commit()
        connection.close()

        result = self._calc(now)
        self.assertEqual(result["exclusion_summary"]["anomaly_occurrences_overlapping_after"], 1)
        self.assertEqual(result["usable_after_sample_count"], 15)  # NOT reduced by the anomaly's presence
        self.assertEqual(result["verification_result"], "VERIFIED")


# ---------------------------------------------------------------------------
# Determinism / persistence / no-mutation (item 14)
# ---------------------------------------------------------------------------

class TestDeterminismAndPersistence(EngineTestBase):
    def test_duplicate_calculation_gives_identical_result(self):
        now = self._seed_symmetric(60.0, 50.0)
        first = self._calc(now)
        second = self._calc(now)
        self.assertEqual(first, second)

    def test_calculation_never_mutates_any_table(self):
        now = self._seed_symmetric(60.0, 50.0)

        def _counts():
            connection = sqlite3.connect(self.config_db)
            counts = {
                t: connection.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("savings_interventions", "savings_verification_results", "energy_opportunities", "anomalies", "baseline_context_summary")
            }
            connection.close()
            return counts

        before = _counts()
        self._calc(now)
        after = _counts()
        self.assertEqual(before, after)

    def test_persist_appends_a_row(self):
        now = self._seed_symmetric(60.0, 50.0)
        result = self._calc(now)
        row_id = calc.persist_verification_result(self.config_db, result)
        history = dom.list_verification_history(self.config_db, self.intervention_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["id"], row_id)
        self.assertEqual(history[0]["result"], "VERIFIED")
        self.assertAlmostEqual(history[0]["verified_energy_kwh"], result["absolute_energy_saving_kwh"], places=3)

    def test_repeated_persistence_creates_append_only_history_never_overwrites(self):
        now = self._seed_symmetric(60.0, 50.0)
        result1 = self._calc(now)
        calc.persist_verification_result(self.config_db, result1)

        later = now + timedelta(days=5)
        _seed_daily(self.historian, "P01.UTILITY.CHL01.Power_kW", 21, 24, 50.0)
        result2 = self._calc(later)
        calc.persist_verification_result(self.config_db, result2)

        history = dom.list_verification_history(self.config_db, self.intervention_id)
        self.assertEqual(len(history), 2)
        self.assertNotEqual(history[0]["id"], history[1]["id"])
        # The FIRST evaluation's stored figures are untouched by the second persist call.
        self.assertAlmostEqual(history[0]["verified_energy_kwh"], result1["absolute_energy_saving_kwh"], places=3)

    def test_persistence_never_advances_intervention_status(self):
        now = self._seed_symmetric(60.0, 50.0)
        result = self._calc(now)
        calc.persist_verification_result(self.config_db, result)
        row = dom.get_intervention(self.config_db, self.intervention_id)
        self.assertEqual(row["status"], "IMPLEMENTED")  # never auto-advanced to COMPLETED

    def test_no_opportunity_estimate_field_is_ever_mutated(self):
        now = self._seed_symmetric(60.0, 50.0)
        connection = sqlite3.connect(self.config_db)
        before = dict(zip(
            [c[1] for c in connection.execute("PRAGMA table_info(energy_opportunities)")],
            connection.execute("SELECT * FROM energy_opportunities WHERE id = ?", (self.opportunity_id,)).fetchone(),
        ))
        connection.close()

        result = self._calc(now)
        calc.persist_verification_result(self.config_db, result)
        calc.attach_estimated_comparison(self.config_db, result, self.opportunity_id)

        connection = sqlite3.connect(self.config_db)
        after = dict(zip(
            [c[1] for c in connection.execute("PRAGMA table_info(energy_opportunities)")],
            connection.execute("SELECT * FROM energy_opportunities WHERE id = ?", (self.opportunity_id,)).fetchone(),
        ))
        connection.close()
        self.assertEqual(before, after)


# ---------------------------------------------------------------------------
# Realization ratio (item 7) - comparison-only, never mutates
# ---------------------------------------------------------------------------

class TestRealizationRatio(unittest.TestCase):
    def test_ratio_computed_when_both_values_real(self):
        self.assertAlmostEqual(calc.compute_realization_ratio(estimated_saving=100.0, verified_saving=80.0), 0.8)

    def test_ratio_none_when_estimate_missing(self):
        self.assertIsNone(calc.compute_realization_ratio(estimated_saving=None, verified_saving=80.0))

    def test_ratio_none_when_estimate_zero_or_negative(self):
        self.assertIsNone(calc.compute_realization_ratio(estimated_saving=0.0, verified_saving=80.0))
        self.assertIsNone(calc.compute_realization_ratio(estimated_saving=-5.0, verified_saving=80.0))

    def test_ratio_none_when_verified_missing(self):
        self.assertIsNone(calc.compute_realization_ratio(estimated_saving=100.0, verified_saving=None))


# ---------------------------------------------------------------------------
# Source-inspection guards
# ---------------------------------------------------------------------------

class TestSourceInspection(unittest.TestCase):
    def test_engine_never_writes_to_energy_opportunities(self):
        source = Path("engine/savings_verification_engine.py").read_text()
        self.assertNotIn("UPDATE energy_opportunities", source)
        self.assertNotIn("INSERT INTO energy_opportunities", source)

    def test_engine_never_writes_to_anomalies_or_baseline_context_summary(self):
        source = Path("engine/savings_verification_engine.py").read_text()
        for banned in ("UPDATE anomalies", "INSERT INTO anomalies", "UPDATE baseline_context_summary", "INSERT INTO baseline_context_summary"):
            self.assertNotIn(banned, source)

    def test_engine_never_calls_update_intervention_status(self):
        source = Path("engine/savings_verification_engine.py").read_text()
        self.assertNotIn("update_intervention_status(", source)

    def test_engine_does_not_reuse_the_clamping_excess_cost_helper(self):
        """bucket_excess_energy_cost() clamps to max(delta, 0) - wrong
        for this module, which must preserve negative deltas. Checks
        for an actual CALL, since the module's own docstring
        legitimately names the function to explain why it's avoided."""
        source = Path("engine/savings_verification_engine.py").read_text()
        self.assertNotIn("= bucket_excess_energy_cost(", source)
        self.assertNotIn("base.bucket_excess_energy_cost(", source)
        # No import of the anomaly-engine module at all (confirms the function
        # was never wired in as a callable, not just absent from a docstring mention).
        self.assertNotIn("from engine import anomaly_engine", source)
        self.assertNotIn("from engine.anomaly_engine import", source)


if __name__ == "__main__":
    unittest.main()
