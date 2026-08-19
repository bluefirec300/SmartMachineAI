import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from engine import savings_verification_domain as dom
from engine.opportunity_migrator import migrate as migrate_opportunity
from engine.savings_verification_migrator import migrate as migrate_savings_verification
from engine.savings_verification_targets import (
    ACTION_CATEGORY_VALUES,
    ACTION_CATEGORY_SETPOINT_CHANGE,
    CONFIDENCE_VALUES,
    DEFAULT_STABILIZATION_DAYS,
    INTERVENTION_STATUS_COMPLETED,
    INTERVENTION_STATUS_IMPLEMENTED,
    INTERVENTION_STATUS_PLANNED,
    INTERVENTION_STATUS_VALUES,
    VERIFICATION_RESULT_INCONCLUSIVE,
    VERIFICATION_RESULT_VALUES,
    VERIFICATION_RESULT_VERIFIED,
    get_action_category,
)

from tests.test_anomaly_engine import _seed_config_db, _insert_tag, _plant_id, BASE


def _seed_opportunity(config_db: Path, plant_id: int, instance_key: str = "P01.UTILITY.CHL01") -> int:
    """A minimal, real energy_opportunities row - Phase 11.1 tests need
    a genuine FK target, not a fabricated one; this mirrors a real
    opportunity's actual shape (same fields Phase 10's engine writes),
    just constructed directly rather than via the full anomaly->
    opportunity pipeline, since this test file's job is the Phase 11.1
    schema, not re-deriving Phase 9/10 behaviour."""
    now_text = BASE.strftime("%Y-%m-%d %H:%M:%S")
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


class SavingsVerificationSchemaTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_db = Path(self.temp_dir.name) / "config.db"
        _seed_config_db(self.config_db)  # runs the full existing migrator chain (incl. anomaly)
        migrate_opportunity(self.config_db, backup=False)
        migrate_savings_verification(self.config_db, backup=False)
        self.plant_id = _plant_id(self.config_db, "p01")
        _insert_tag(self.config_db, "P01.UTILITY.CHL01.Power_kW", unit="kW", measurement="power")
        self.opportunity_id = _seed_opportunity(self.config_db, self.plant_id)

    def tearDown(self):
        self.temp_dir.cleanup()


# ---------------------------------------------------------------------------
# Migration behaviour
# ---------------------------------------------------------------------------

class TestMigration(unittest.TestCase):
    def test_migration_on_fresh_db_creates_both_tables(self):
        temp_dir = tempfile.TemporaryDirectory()
        try:
            config_db = Path(temp_dir.name) / "config.db"
            _seed_config_db(config_db)
            migrate_opportunity(config_db, backup=False)
            migrate_savings_verification(config_db, backup=False)

            connection = sqlite3.connect(config_db)
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            connection.close()
            self.assertIn("savings_interventions", tables)
            self.assertIn("savings_verification_results", tables)
        finally:
            temp_dir.cleanup()

    def test_migration_is_idempotent_on_repeated_execution(self):
        temp_dir = tempfile.TemporaryDirectory()
        try:
            config_db = Path(temp_dir.name) / "config.db"
            _seed_config_db(config_db)
            migrate_opportunity(config_db, backup=False)
            migrate_savings_verification(config_db, backup=False)
            migrate_savings_verification(config_db, backup=False)  # second run must not error or duplicate schema
            migrate_savings_verification(config_db, backup=False)  # third run, same

            connection = sqlite3.connect(config_db)
            count = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='savings_interventions'"
            ).fetchone()[0]
            connection.close()
            self.assertEqual(count, 1)
        finally:
            temp_dir.cleanup()

    def test_migration_on_existing_db_preserves_existing_opportunity_data(self):
        """Item J - existing Phase 10.6 opportunity data must remain unchanged."""
        temp_dir = tempfile.TemporaryDirectory()
        try:
            config_db = Path(temp_dir.name) / "config.db"
            _seed_config_db(config_db)
            migrate_opportunity(config_db, backup=False)
            plant_id = _plant_id(config_db, "p01")
            opp_id = _seed_opportunity(config_db, plant_id)

            before = sqlite3.connect(config_db).execute("SELECT * FROM energy_opportunities WHERE id = ?", (opp_id,)).fetchone()

            migrate_savings_verification(config_db, backup=False)

            after = sqlite3.connect(config_db).execute("SELECT * FROM energy_opportunities WHERE id = ?", (opp_id,)).fetchone()
            self.assertEqual(before, after)
        finally:
            temp_dir.cleanup()

    def test_both_new_tables_are_empty_immediately_after_migration(self):
        """Phase 11.1 must generate zero real intervention/verification
        rows automatically."""
        temp_dir = tempfile.TemporaryDirectory()
        try:
            config_db = Path(temp_dir.name) / "config.db"
            _seed_config_db(config_db)
            migrate_opportunity(config_db, backup=False)
            migrate_savings_verification(config_db, backup=False)

            connection = sqlite3.connect(config_db)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM savings_interventions").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM savings_verification_results").fetchone()[0], 0)
            connection.close()
        finally:
            temp_dir.cleanup()

    def test_backup_file_created_by_default(self):
        temp_dir = tempfile.TemporaryDirectory()
        try:
            config_db = Path(temp_dir.name) / "config.db"
            _seed_config_db(config_db)
            migrate_opportunity(config_db, backup=False)
            migrate_savings_verification(config_db, backup=True)
            backups = list(Path(temp_dir.name).glob("config_before_savings_verification_*.db"))
            self.assertEqual(len(backups), 1)
        finally:
            temp_dir.cleanup()


# ---------------------------------------------------------------------------
# FK behaviour
# ---------------------------------------------------------------------------

class TestForeignKeys(SavingsVerificationSchemaTestBase):
    def test_intervention_references_real_opportunity(self):
        intervention_id = dom.create_intervention(
            self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01",
            ACTION_CATEGORY_SETPOINT_CHANGE, "Lowered setpoint", "test_engineer",
        )
        row = dom.get_intervention(self.config_db, intervention_id)
        self.assertEqual(row["opportunity_id"], self.opportunity_id)

    def test_verification_result_references_real_intervention(self):
        intervention_id = dom.create_intervention(
            self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01",
            ACTION_CATEGORY_SETPOINT_CHANGE, "Lowered setpoint", "test_engineer",
        )
        result_id = dom.record_verification_result(
            self.config_db, intervention_id, VERIFICATION_RESULT_INCONCLUSIVE,
            evidence_json="{}", assumptions_json="[]", limitations_json="[]",
        )
        history = dom.list_verification_history(self.config_db, intervention_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["id"], result_id)
        self.assertEqual(history[0]["intervention_id"], intervention_id)


# ---------------------------------------------------------------------------
# Lifecycle / vocabulary
# ---------------------------------------------------------------------------

class TestVocabulary(unittest.TestCase):
    def test_intervention_status_values_never_contain_verification_outcomes(self):
        """The 'no contradictory duplicate state' requirement - these
        two vocabularies must never overlap."""
        self.assertEqual(set(INTERVENTION_STATUS_VALUES) & set(VERIFICATION_RESULT_VALUES), set())

    def test_intervention_status_values_are_exactly_the_expected_five(self):
        self.assertEqual(
            set(INTERVENTION_STATUS_VALUES),
            {"PLANNED", "IMPLEMENTED", "VERIFICATION_PENDING", "VERIFICATION_IN_PROGRESS", "COMPLETED"},
        )

    def test_verification_result_values_are_exactly_the_expected_three(self):
        self.assertEqual(set(VERIFICATION_RESULT_VALUES), {"VERIFIED", "REJECTED", "INCONCLUSIVE"})

    def test_confidence_values_are_a_tier_not_a_number(self):
        for value in CONFIDENCE_VALUES:
            self.assertIsInstance(value, str)
        self.assertEqual(set(CONFIDENCE_VALUES), {"STRONG", "CONTEXT_MATCHED", "LIMITED", "INSUFFICIENT"})

    def test_only_completed_is_terminal(self):
        from engine.savings_verification_targets import INTERVENTION_TERMINAL_STATUSES
        self.assertEqual(INTERVENTION_TERMINAL_STATUSES, ("COMPLETED",))


class TestActionCategoryRegistry(unittest.TestCase):
    def test_registry_is_small_and_generic(self):
        self.assertGreaterEqual(len(ACTION_CATEGORY_VALUES), 5)
        self.assertLessEqual(len(ACTION_CATEGORY_VALUES), 12)  # "do not create dozens"

    def test_other_is_a_valid_fallback_category(self):
        self.assertIn("OTHER", ACTION_CATEGORY_VALUES)

    def test_every_category_has_a_default_stabilization_days(self):
        for category in ACTION_CATEGORY_VALUES:
            self.assertIn(category, DEFAULT_STABILIZATION_DAYS)
            self.assertIsInstance(DEFAULT_STABILIZATION_DAYS[category], int)
            self.assertGreater(DEFAULT_STABILIZATION_DAYS[category], 0)

    def test_get_action_category_returns_none_for_unknown_code(self):
        self.assertIsNone(get_action_category("NOT_A_REAL_CATEGORY"))

    def test_get_action_category_returns_metadata_for_known_code(self):
        category = get_action_category(ACTION_CATEGORY_SETPOINT_CHANGE)
        self.assertIsNotNone(category)
        self.assertEqual(category.code, ACTION_CATEGORY_SETPOINT_CHANGE)
        self.assertEqual(category.default_stabilization_days, DEFAULT_STABILIZATION_DAYS[ACTION_CATEGORY_SETPOINT_CHANGE])


# ---------------------------------------------------------------------------
# Domain helper behaviour
# ---------------------------------------------------------------------------

class TestCreateIntervention(SavingsVerificationSchemaTestBase):
    def test_create_intervention_starts_planned(self):
        intervention_id = dom.create_intervention(
            self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01",
            ACTION_CATEGORY_SETPOINT_CHANGE, "Lowered setpoint by 2C", "test_engineer",
        )
        row = dom.get_intervention(self.config_db, intervention_id)
        self.assertEqual(row["status"], INTERVENTION_STATUS_PLANNED)
        self.assertIsNone(row["implemented_at"])

    def test_create_intervention_rejects_invalid_category(self):
        with self.assertRaises(ValueError):
            dom.create_intervention(
                self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01",
                "NOT_A_REAL_CATEGORY", "desc", "test_engineer",
            )

    def test_create_intervention_requires_description_and_recorded_by(self):
        with self.assertRaises(ValueError):
            dom.create_intervention(self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01", ACTION_CATEGORY_SETPOINT_CHANGE, "", "test_engineer")
        with self.assertRaises(ValueError):
            dom.create_intervention(self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01", ACTION_CATEGORY_SETPOINT_CHANGE, "desc", "")

    def test_timestamps_use_local_space_separated_format_not_audit_log_iso(self):
        """Item: 'do not use audit_log timestamps for verification-period
        arithmetic - use the analytics project's established timestamp
        convention consistently for new Phase 11 tables.'"""
        intervention_id = dom.create_intervention(
            self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01",
            ACTION_CATEGORY_SETPOINT_CHANGE, "desc", "test_engineer",
        )
        row = dom.get_intervention(self.config_db, intervention_id)
        # Must parse cleanly with the analytics TIME_FORMAT and must NOT contain
        # a literal 'T' (the audit_log/UTC convention's telltale marker).
        datetime.strptime(row["recorded_at"], "%Y-%m-%d %H:%M:%S")
        datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
        self.assertNotIn("T", row["recorded_at"])
        self.assertNotIn("T", row["created_at"])


class TestDuplicateActivePrevention(SavingsVerificationSchemaTestBase):
    def test_second_active_intervention_for_same_opportunity_is_rejected(self):
        dom.create_intervention(
            self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01",
            ACTION_CATEGORY_SETPOINT_CHANGE, "first attempt", "engineer_a",
        )
        with self.assertRaises(ValueError):
            dom.create_intervention(
                self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01",
                ACTION_CATEGORY_SETPOINT_CHANGE, "second attempt while first still active", "engineer_b",
            )

    def test_find_active_intervention_returns_the_active_one(self):
        intervention_id = dom.create_intervention(
            self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01",
            ACTION_CATEGORY_SETPOINT_CHANGE, "first attempt", "engineer_a",
        )
        active = dom.find_active_intervention(self.config_db, self.opportunity_id)
        self.assertEqual(active["id"], intervention_id)

    def test_terminal_intervention_allows_a_new_one_to_be_recorded(self):
        """Item: 'terminal intervention allows later intervention.'"""
        first_id = dom.create_intervention(
            self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01",
            ACTION_CATEGORY_SETPOINT_CHANGE, "first attempt", "engineer_a",
        )
        dom.update_intervention_status(self.config_db, first_id, INTERVENTION_STATUS_COMPLETED)

        second_id = dom.create_intervention(
            self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01",
            ACTION_CATEGORY_SETPOINT_CHANGE, "second attempt after first completed", "engineer_b",
        )
        self.assertNotEqual(first_id, second_id)

        connection = sqlite3.connect(self.config_db)
        count = connection.execute("SELECT COUNT(*) FROM savings_interventions WHERE opportunity_id = ?", (self.opportunity_id,)).fetchone()[0]
        connection.close()
        self.assertEqual(count, 2)  # both rows preserved - history, not overwritten

    def test_duplicate_active_rejected_at_db_level_even_bypassing_the_python_helper(self):
        """Regression guard mirroring Phase 9/10's own proven partial-
        unique-index tests - the DB constraint itself, not just the
        Python wrapper, must enforce this."""
        now_text = BASE.strftime("%Y-%m-%d %H:%M:%S")
        connection = sqlite3.connect(self.config_db)
        cols = "opportunity_id, plant_id, instance_key, action_category, action_description, recorded_by, recorded_at, status, created_at, updated_at"
        values = (self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01", "SETPOINT_CHANGE", "d", "r", now_text, "PLANNED", now_text, now_text)
        connection.execute(f"INSERT INTO savings_interventions ({cols}) VALUES (?,?,?,?,?,?,?,?,?,?)", values)
        connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(f"INSERT INTO savings_interventions ({cols}) VALUES (?,?,?,?,?,?,?,?,?,?)", values)
        connection.close()


class TestMarkImplemented(SavingsVerificationSchemaTestBase):
    def test_mark_implemented_transitions_planned_to_implemented(self):
        intervention_id = dom.create_intervention(
            self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01",
            ACTION_CATEGORY_SETPOINT_CHANGE, "desc", "test_engineer",
        )
        dom.mark_implemented(self.config_db, intervention_id, implemented_at=BASE)
        row = dom.get_intervention(self.config_db, intervention_id)
        self.assertEqual(row["status"], INTERVENTION_STATUS_IMPLEMENTED)
        self.assertEqual(row["implemented_at"], BASE.strftime("%Y-%m-%d %H:%M:%S"))

    def test_mark_implemented_rejects_non_planned_intervention(self):
        intervention_id = dom.create_intervention(
            self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01",
            ACTION_CATEGORY_SETPOINT_CHANGE, "desc", "test_engineer",
        )
        dom.mark_implemented(self.config_db, intervention_id)
        with self.assertRaises(ValueError):
            dom.mark_implemented(self.config_db, intervention_id)  # already IMPLEMENTED


class TestVerificationResultAppendOnly(SavingsVerificationSchemaTestBase):
    def setUp(self):
        super().setUp()
        self.intervention_id = dom.create_intervention(
            self.config_db, self.opportunity_id, self.plant_id, "P01.UTILITY.CHL01",
            ACTION_CATEGORY_SETPOINT_CHANGE, "desc", "test_engineer",
        )

    def test_multiple_evaluations_are_preserved_as_separate_rows(self):
        """The worked example from your spec: 3 evaluations, all
        auditable, none overwritten."""
        dom.record_verification_result(
            self.config_db, self.intervention_id, VERIFICATION_RESULT_INCONCLUSIVE,
            evidence_json="{}", assumptions_json="[]", limitations_json="[]",
            reason="insufficient post-action samples", evaluated_at=BASE,
        )
        dom.record_verification_result(
            self.config_db, self.intervention_id, VERIFICATION_RESULT_INCONCLUSIVE,
            evidence_json="{}", assumptions_json="[]", limitations_json="[]",
            reason="sufficient samples but poor context match", evaluated_at=BASE + timedelta(days=3),
        )
        dom.record_verification_result(
            self.config_db, self.intervention_id, VERIFICATION_RESULT_VERIFIED,
            evidence_json=json.dumps({"reference_median": 165.0, "verification_median": 142.0}),
            assumptions_json="[]", limitations_json="[]", confidence="STRONG",
            verified_energy_kwh=120.5, verified_cost=62.5, verified_cost_currency="MYR",
            evaluated_at=BASE + timedelta(days=10),
        )

        history = dom.list_verification_history(self.config_db, self.intervention_id)
        self.assertEqual(len(history), 3)
        self.assertEqual([h["result"] for h in history], ["INCONCLUSIVE", "INCONCLUSIVE", "VERIFIED"])
        # Earlier evaluations' evidence must remain intact, not overwritten.
        self.assertEqual(history[0]["reason"], "insufficient post-action samples")
        self.assertEqual(history[1]["reason"], "sufficient samples but poor context match")
        self.assertIsNone(history[0]["verified_cost"])
        self.assertEqual(history[2]["verified_cost"], 62.5)

    def test_get_latest_verification_result_returns_the_most_recent(self):
        dom.record_verification_result(
            self.config_db, self.intervention_id, VERIFICATION_RESULT_INCONCLUSIVE,
            evidence_json="{}", assumptions_json="[]", limitations_json="[]", evaluated_at=BASE,
        )
        dom.record_verification_result(
            self.config_db, self.intervention_id, VERIFICATION_RESULT_VERIFIED,
            evidence_json="{}", assumptions_json="[]", limitations_json="[]",
            confidence="STRONG", verified_cost=10.0, evaluated_at=BASE + timedelta(days=5),
        )
        latest = dom.get_latest_verification_result(self.config_db, self.intervention_id)
        self.assertEqual(latest["result"], VERIFICATION_RESULT_VERIFIED)

    def test_invalid_result_value_rejected(self):
        with self.assertRaises(ValueError):
            dom.record_verification_result(
                self.config_db, self.intervention_id, "NOT_A_REAL_RESULT",
                evidence_json="{}", assumptions_json="[]", limitations_json="[]",
            )

    def test_invalid_confidence_value_rejected(self):
        with self.assertRaises(ValueError):
            dom.record_verification_result(
                self.config_db, self.intervention_id, VERIFICATION_RESULT_INCONCLUSIVE,
                evidence_json="{}", assumptions_json="[]", limitations_json="[]", confidence="NOT_A_REAL_TIER",
            )

    def test_no_financial_value_defaulted_to_zero_when_not_supplied(self):
        """Non-negotiable financial semantics - INCONCLUSIVE/REJECTED
        results must leave verified_energy_kwh/verified_cost as real
        NULL, never a fabricated 0."""
        dom.record_verification_result(
            self.config_db, self.intervention_id, VERIFICATION_RESULT_INCONCLUSIVE,
            evidence_json="{}", assumptions_json="[]", limitations_json="[]", reason="insufficient data",
        )
        result = dom.get_latest_verification_result(self.config_db, self.intervention_id)
        self.assertIsNone(result["verified_energy_kwh"])
        self.assertIsNone(result["verified_cost"])

    def test_no_helper_exists_to_update_a_prior_verification_row(self):
        """Structural guard for 'append-only in concept' - the domain
        module must expose no update/delete path for
        savings_verification_results."""
        self.assertFalse(hasattr(dom, "update_verification_result"))
        self.assertFalse(hasattr(dom, "delete_verification_result"))


class TestPhase106DataUntouched(unittest.TestCase):
    def test_live_database_opportunities_are_never_modified_by_this_module(self):
        """Item J, direct check against source: nothing in
        engine/savings_verification_domain.py writes to
        energy_opportunities or anomalies."""
        source = Path("engine/savings_verification_domain.py").read_text()
        self.assertNotIn("UPDATE energy_opportunities", source)
        self.assertNotIn("INSERT INTO energy_opportunities", source)
        self.assertNotIn("UPDATE anomalies", source)
        self.assertNotIn("INSERT INTO anomalies", source)


if __name__ == "__main__":
    unittest.main()
