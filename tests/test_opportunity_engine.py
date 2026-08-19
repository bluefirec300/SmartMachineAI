import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from database.database import DatabaseManager
from engine import opportunity_engine as opp
from engine.anomaly_migrator import migrate as migrate_anomaly
from engine.anomaly_targets import get_rule as get_anomaly_rule
from engine.energy_tariff import TARIFF_PROVENANCE_CONFIGURED, TARIFF_PROVENANCE_SIMULATION, create_tariff
from engine.opportunity_migrator import migrate as migrate_opportunity
from engine.opportunity_targets import (
    OPPORTUNITY_RULES,
    SAVING_BASIS_UNAVAILABLE,
    get_rule as get_opportunity_rule,
)

from tests.test_anomaly_engine import _seed_config_db, _insert_tag, _insert_tariff, _plant_id, BASE


def _seed_anomaly(
    config_db, plant_id, rule_key, instance_key, excess_kwh, excess_cost, status="OPEN",
    first_detected=None, last_seen=None, baseline_level="B", baseline_confidence="High", severity="ATTENTION",
    currency="MYR",
) -> int:
    fd = first_detected or BASE.strftime("%Y-%m-%d %H:%M:%S")
    ls = last_seen or fd
    row = dict(
        plant_id=plant_id, equipment_id=None, instance_key=instance_key, target_key="Power_kW", rule_key=rule_key,
        anomaly_type="deviation", category="UTILITY", title="t", severity=severity, confidence=baseline_confidence,
        provisional=0, status=status, first_detected=fd, last_seen=ls, resolved_at=ls if status == "RESOLVED" else None,
        occurrence_count=1, open_persistence_periods=3, resolve_persistence_periods=0, last_bucket_start=ls,
        actual_value=90.0, expected_value=50.0, expected_low=40.0, expected_high=60.0, deviation_absolute=40.0,
        deviation_percent=80.0, normalized_deviation=13.3, baseline_type="recent", baseline_level=baseline_level,
        baseline_confidence=baseline_confidence, engineering_limit_status="not_exceeded", engineering_limit_event_id=None,
        estimated_excess_energy_kwh=excess_kwh, estimated_excess_cost=excess_cost, estimated_excess_cost_currency=currency,
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


class OpportunityEngineTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_db = Path(self.temp_dir.name) / "config.db"
        self.machine_db = Path(self.temp_dir.name) / "machine_data.db"
        _seed_config_db(self.config_db)  # already runs migrate_anomaly internally
        migrate_opportunity(self.config_db, backup=False)
        self.historian = DatabaseManager(db_path=self.machine_db)
        self.plant_id = _plant_id(self.config_db, "p01")
        _insert_tag(self.config_db, "P01.UTILITY.CHL01.Power_kW", unit="kW", measurement="power")
        _insert_tariff(self.config_db, self.plant_id, energy_rate=0.5, currency="MYR", effective_date="2026-08-01")
        self.rule = get_opportunity_rule("chiller_power_opportunity")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _evaluate(self, now=None):
        return opp.evaluate_opportunity_rule(self.rule, self.config_db, self.machine_db, self.historian, self.plant_id, "p01", now=now or BASE)

    def _get_opportunity(self, instance_key="P01.UTILITY.CHL01"):
        connection = sqlite3.connect(self.config_db)
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM energy_opportunities WHERE plant_id = ? AND instance_key = ? AND rule_key = ? AND status = 'NEW'",
            (self.plant_id, instance_key, self.rule.rule_key),
        ).fetchone()
        connection.close()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Registry structural checks
# ---------------------------------------------------------------------------

class TestRegistry(unittest.TestCase):
    def test_every_rule_sources_an_energy_relevant_anomaly_rule(self):
        """No opportunity rule may source a CONDITION-only (non-energy)
        anomaly rule - Phase 9 never computes excess energy for those,
        so an opportunity could never have real observed evidence."""
        for rule in OPPORTUNITY_RULES:
            source = get_anomaly_rule(rule.source_anomaly_rule_key)
            self.assertIsNotNone(source, f"{rule.rule_key} references unknown anomaly rule {rule.source_anomaly_rule_key}")
            self.assertTrue(source.energy_relevant, f"{rule.rule_key} sources a non-energy-relevant anomaly rule")

    def test_no_condition_only_anomaly_rules_have_opportunity_rules(self):
        """item 28 - vibration/bearing-temp/COP/etc. never automatically
        become opportunities: structurally, no OpportunityRule exists
        for any non-energy-relevant Phase 9 rule key."""
        opportunity_source_keys = {r.source_anomaly_rule_key for r in OPPORTUNITY_RULES}
        for key in ("pump_vibration_increase", "pump_bearing_temp_increase", "chiller_cop_low",
                    "chiller_supply_temp_deviation", "ahu_filter_dp_drift", "production_vibration_drift"):
            self.assertNotIn(key, opportunity_source_keys)

    def test_every_rule_declares_unavailable_saving_basis_in_this_build(self):
        """Guards against silently reintroducing a generic full-excess
        shortcut - every rule in the initial registry must explicitly
        declare 'unavailable' unless individually justified (none are, today)."""
        for rule in OPPORTUNITY_RULES:
            self.assertEqual(rule.saving_basis, SAVING_BASIS_UNAVAILABLE, f"{rule.rule_key} unexpectedly declares a computed saving basis")


# ---------------------------------------------------------------------------
# Qualification (item 30: qualifying anomaly creates opportunity /
# non-energy anomaly does not / below-minimum-impact is skipped)
# ---------------------------------------------------------------------------

class TestQualification(OpportunityEngineTestBase):
    def test_qualifying_energy_anomaly_creates_opportunity(self):
        _seed_anomaly(self.config_db, self.plant_id, "chiller_power_deviation", "P01.UTILITY.CHL01", 6.5, 3.38)
        results = self._evaluate()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["action"], "opened")
        row = self._get_opportunity()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "NEW")

    def test_no_qualifying_anomaly_produces_no_opportunity(self):
        results = self._evaluate()
        self.assertEqual(results, [])
        self.assertIsNone(self._get_opportunity())

    def test_below_minimum_meaningful_impact_does_not_open(self):
        _seed_anomaly(self.config_db, self.plant_id, "chiller_power_deviation", "P01.UTILITY.CHL01", 0.01, 0.005)
        results = self._evaluate()
        self.assertEqual(results[0]["action"], "below_minimum_impact")
        self.assertIsNone(self._get_opportunity())

    def test_anomaly_without_excess_energy_never_qualifies(self):
        """A CONDITION-category anomaly (no Phase-9-computed excess) has
        no matching OpportunityRule at all (see TestRegistry), so this
        confirms the qualification query itself also requires a real
        positive excess figure - defense in depth."""
        _seed_anomaly(self.config_db, self.plant_id, "chiller_power_deviation", "P01.UTILITY.CHL01", None, None)
        results = self._evaluate()
        self.assertEqual(results, [])


# ---------------------------------------------------------------------------
# Phase 18.1a.1 - tariff_provenance correction. setUp() already
# configures a SIMULATED tariff (_insert_tariff() always inserts
# is_simulated=1 - shared across many test files' fixtures, so left
# untouched here) - these tests cover both that default path and a
# genuinely CONFIGURED (is_simulated=0) override.
# ---------------------------------------------------------------------------

class TestTariffProvenance(OpportunityEngineTestBase):
    def test_simulated_tariff_produces_simulation_provenance(self):
        _seed_anomaly(self.config_db, self.plant_id, "chiller_power_deviation", "P01.UTILITY.CHL01", 6.5, 3.38)
        self._evaluate()
        row = self._get_opportunity()
        self.assertEqual(row["tariff_provenance"], TARIFF_PROVENANCE_SIMULATION)

    def test_real_configured_tariff_produces_configured_provenance(self):
        # Closes out setUp's simulated tariff and becomes the new
        # open-ended, real one for the same plant/period.
        create_tariff(
            self.config_db, plant_id=self.plant_id, effective_date="2026-08-01",
            username="admin", currency="MYR", energy_rate=0.55,
        )
        _seed_anomaly(self.config_db, self.plant_id, "chiller_power_deviation", "P01.UTILITY.CHL01", 6.5, 3.38)
        self._evaluate()
        row = self._get_opportunity()
        self.assertEqual(row["tariff_provenance"], TARIFF_PROVENANCE_CONFIGURED)

    def test_provenance_never_inferred_from_currency(self):
        # A real, CONFIGURED tariff in a non-MYR currency must still be
        # labeled CONFIGURED - never treated as "simulation" merely
        # because it isn't MYR.
        create_tariff(
            self.config_db, plant_id=self.plant_id, effective_date="2026-08-01",
            username="admin", currency="USD", energy_rate=0.20,
        )
        _seed_anomaly(self.config_db, self.plant_id, "chiller_power_deviation", "P01.UTILITY.CHL01", 6.5, 3.38, currency="USD")
        self._evaluate()
        row = self._get_opportunity()
        self.assertEqual(row["tariff_provenance"], TARIFF_PROVENANCE_CONFIGURED)

    def test_no_tariff_resolvable_leaves_provenance_none(self):
        # No tariff at all exists for this plant/date (fresh config db,
        # no _insert_tariff() call) - provenance must stay None, never
        # guessed as either label.
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        config_db = Path(temp_dir.name) / "config.db"
        _seed_config_db(config_db)
        migrate_opportunity(config_db, backup=False)
        plant_id = _plant_id(config_db, "p01")
        _insert_tag(config_db, "P01.UTILITY.CHL01.Power_kW", unit="kW", measurement="power")
        # deliberately no _insert_tariff() call here

        _seed_anomaly(config_db, plant_id, "chiller_power_deviation", "P01.UTILITY.CHL01", 6.5, 3.38)
        historian = DatabaseManager(db_path=Path(temp_dir.name) / "machine_data.db")
        opp.evaluate_opportunity_rule(self.rule, config_db, Path(temp_dir.name) / "machine_data.db", historian, plant_id, "p01", now=BASE)

        connection = sqlite3.connect(config_db)
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM energy_opportunities WHERE plant_id = ? AND instance_key = ? AND status = 'NEW'",
            (plant_id, "P01.UTILITY.CHL01"),
        ).fetchone()
        connection.close()

        self.assertIsNotNone(row)
        self.assertIsNone(row["tariff_provenance"])


# ---------------------------------------------------------------------------
# Deduplication / recurrence (item 30: same active source does not
# duplicate / recurring anomaly handled per defined lifecycle)
# ---------------------------------------------------------------------------

class TestDeduplication(OpportunityEngineTestBase):
    def test_same_open_anomaly_reevaluated_does_not_duplicate(self):
        _seed_anomaly(self.config_db, self.plant_id, "chiller_power_deviation", "P01.UTILITY.CHL01", 6.5, 3.38)
        first = self._evaluate()
        second = self._evaluate()
        self.assertEqual(first[0]["action"], "opened")
        self.assertEqual(second[0]["action"], "unchanged")
        connection = sqlite3.connect(self.config_db)
        count = connection.execute("SELECT COUNT(*) FROM energy_opportunities WHERE status = 'NEW'").fetchone()[0]
        connection.close()
        self.assertEqual(count, 1)

    def test_recurring_anomaly_appends_without_double_counting(self):
        """Worked Example D - occurrence 1 resolves, occurrence 2 opens
        later; the SAME opportunity row accumulates both totals exactly
        once each, never re-adding occurrence 1."""
        id1 = _seed_anomaly(
            self.config_db, self.plant_id, "chiller_power_deviation", "P01.UTILITY.CHL01", 6.5, 3.38,
            status="RESOLVED", first_detected="2026-08-01 10:00:00", last_seen="2026-08-01 12:00:00",
        )
        first = self._evaluate(now=BASE)
        self.assertEqual(first[0]["action"], "opened")
        opp1 = self._get_opportunity()
        self.assertAlmostEqual(opp1["observed_excess_cost"], 3.38)

        # Re-evaluate with nothing new - must stay unchanged, not re-sum occurrence 1.
        unchanged = self._evaluate(now=BASE)
        self.assertEqual(unchanged[0]["action"], "unchanged")

        id2 = _seed_anomaly(
            self.config_db, self.plant_id, "chiller_power_deviation", "P01.UTILITY.CHL01", 9.1, 4.73,
            status="OPEN", first_detected="2026-08-15 10:00:00", last_seen="2026-08-15 12:00:00",
        )
        second = self._evaluate(now=BASE + timedelta(days=1))
        self.assertEqual(second[0]["action"], "updated")
        opp2 = self._get_opportunity()
        self.assertEqual(opp2["id"], opp1["id"])  # same row, not a new one
        self.assertAlmostEqual(opp2["observed_excess_cost"], 3.38 + 4.73, places=2)
        self.assertEqual(opp2["occurrence_count"], 2)
        self.assertEqual(sorted(json.loads(opp2["source_anomaly_ids"])), sorted([id1, id2]))

        connection = sqlite3.connect(self.config_db)
        count = connection.execute("SELECT COUNT(*) FROM energy_opportunities").fetchone()[0]
        connection.close()
        self.assertEqual(count, 1)  # never a second row


# ---------------------------------------------------------------------------
# Saving methodology (item 30: never auto-100%, insufficient evidence
# -> Unavailable, no verified/guaranteed-saving language)
# ---------------------------------------------------------------------------

class TestSavingUnavailable(OpportunityEngineTestBase):
    def test_potential_saving_is_unavailable_with_a_stated_reason(self):
        _seed_anomaly(self.config_db, self.plant_id, "chiller_power_deviation", "P01.UTILITY.CHL01", 6.5, 3.38, baseline_level="A")
        self._evaluate()
        row = self._get_opportunity()
        self.assertIsNone(row["estimated_potential_saving_period"])
        self.assertEqual(row["saving_basis"], SAVING_BASIS_UNAVAILABLE)
        self.assertTrue(row["saving_unavailable_reason"])
        self.assertIsNone(row["estimated_monthly_saving"])
        self.assertIsNone(row["estimated_annual_saving"])

    def test_observed_excess_still_shown_as_evidence_despite_saving_unavailable(self):
        """Requirement 5's terminology check - Observed Estimated Excess
        Cost and Potential Saving must both be independently present,
        never merged or one substituting the other."""
        _seed_anomaly(self.config_db, self.plant_id, "chiller_power_deviation", "P01.UTILITY.CHL01", 6.5, 3.38)
        self._evaluate()
        row = self._get_opportunity()
        self.assertEqual(row["observed_excess_cost"], 3.38)
        self.assertIsNone(row["estimated_potential_saving_period"])

    def test_no_forbidden_saving_language_anywhere_in_stored_text(self):
        _seed_anomaly(self.config_db, self.plant_id, "chiller_power_deviation", "P01.UTILITY.CHL01", 6.5, 3.38)
        self._evaluate()
        row = self._get_opportunity()
        text = " ".join([
            row["title"], row["recommendation"], row["saving_unavailable_reason"] or "",
            " ".join(json.loads(row["assumptions_json"])), " ".join(json.loads(row["limitations_json"])),
        ]).lower()
        for banned in ("verified saving", "actual saving", "guaranteed saving", "fault confirmed", "root cause", "must change setpoint"):
            self.assertNotIn(banned, text)


# ---------------------------------------------------------------------------
# Annualization safeguards (item 2's correction - configurable minimum
# evidence, never a bare ">=2 occurrences", never 24x365 from one event)
# ---------------------------------------------------------------------------

class TestAnnualizationSafeguards(unittest.TestCase):
    def test_no_potential_saving_means_no_annualization_regardless_of_evidence(self):
        method, monthly, annual = opp.compute_annualization(
            occurrence_count=10, first_identified="2026-01-01 00:00:00", last_updated="2026-08-01 00:00:00",
            schedule_available=False, potential_saving_period=None,
        )
        self.assertEqual(method, "unavailable_conservative")
        self.assertIsNone(monthly)
        self.assertIsNone(annual)

    def test_two_occurrences_never_justify_annualization(self):
        method, monthly, annual = opp.compute_annualization(
            occurrence_count=2, first_identified="2026-07-01 00:00:00", last_updated="2026-08-15 00:00:00",
            schedule_available=False, potential_saving_period=50.0,
        )
        self.assertEqual(method, "unavailable_conservative")
        self.assertIsNone(monthly)
        self.assertIsNone(annual)

    def test_one_short_event_is_never_extrapolated_24x365(self):
        method, monthly, annual = opp.compute_annualization(
            occurrence_count=1, first_identified="2026-08-15 10:00:00", last_updated="2026-08-15 12:00:00",
            schedule_available=False, potential_saving_period=5.0,
        )
        self.assertEqual(method, "unavailable_conservative")
        self.assertIsNone(monthly)
        self.assertIsNone(annual)

    def test_sufficient_recurrence_and_span_enables_observed_rate_projection(self):
        method, monthly, annual = opp.compute_annualization(
            occurrence_count=opp.MIN_OCCURRENCES_FOR_ANNUALIZATION,
            first_identified="2026-01-01 00:00:00",
            last_updated=(datetime(2026, 1, 1) + timedelta(days=opp.MIN_OBSERVATION_SPAN_DAYS_FOR_ANNUALIZATION)).strftime("%Y-%m-%d %H:%M:%S"),
            schedule_available=False, potential_saving_period=60.0,
        )
        self.assertEqual(method, "observed_recurrence_rate")
        self.assertIsNotNone(monthly)
        self.assertIsNotNone(annual)
        self.assertGreater(annual, monthly)

    def test_sufficient_occurrences_but_insufficient_span_still_unavailable(self):
        method, monthly, annual = opp.compute_annualization(
            occurrence_count=10, first_identified="2026-08-10 00:00:00", last_updated="2026-08-15 00:00:00",
            schedule_available=False, potential_saving_period=60.0,
        )
        self.assertEqual(method, "unavailable_conservative")
        self.assertIsNone(monthly)


# ---------------------------------------------------------------------------
# Priority ("Engineering Investigation Priority") semantics (item 3's
# correction)
# ---------------------------------------------------------------------------

class TestPriority(unittest.TestCase):
    def test_unavailable_saving_never_gets_potential_saving_credit(self):
        priority, score, breakdown = opp.compute_priority(
            potential_saving_period=None, observed_excess_cost=1000.0, confidence="High", occurrence_count=5,
            severity="HIGH", implementation_difficulty="LOW", equipment_criticality="high",
        )
        self.assertEqual(breakdown["potential_saving_component"], 0)
        self.assertGreater(breakdown["observed_impact_component"], 0)

    def test_two_opportunities_same_observed_cost_differ_by_potential_saving_availability(self):
        """The core semantic requirement - an opportunity with a REAL
        computed potential saving must never be made to look merely
        'financially comparable' to one with unknown potential saving,
        purely because their observed costs match."""
        _, score_unavailable, breakdown_unavailable = opp.compute_priority(
            potential_saving_period=None, observed_excess_cost=600.0, confidence="High", occurrence_count=1,
            severity="ATTENTION", implementation_difficulty="UNKNOWN", equipment_criticality=None,
        )
        _, score_available, breakdown_available = opp.compute_priority(
            potential_saving_period=600.0, observed_excess_cost=600.0, confidence="High", occurrence_count=1,
            severity="ATTENTION", implementation_difficulty="UNKNOWN", equipment_criticality=None,
        )
        self.assertGreater(score_available, score_unavailable)
        self.assertEqual(breakdown_unavailable["potential_saving_component"], 0)
        self.assertGreater(breakdown_available["potential_saving_component"], 0)

    def test_breakdown_exposes_every_documented_component(self):
        _, _, breakdown = opp.compute_priority(
            potential_saving_period=None, observed_excess_cost=50.0, confidence="Medium", occurrence_count=2,
            severity="WARNING", implementation_difficulty="MEDIUM", equipment_criticality=None,
        )
        for key in ("potential_saving_component", "observed_impact_component", "confidence_component",
                    "recurrence_component", "severity_component", "difficulty_component", "criticality_component"):
            self.assertIn(key, breakdown)

    def test_unknown_difficulty_is_neutral_not_penalized_or_boosted(self):
        _, score_unknown, _ = opp.compute_priority(
            potential_saving_period=None, observed_excess_cost=50.0, confidence="Medium", occurrence_count=1,
            severity="INFORMATION", implementation_difficulty="UNKNOWN", equipment_criticality=None,
        )
        _, score_medium, _ = opp.compute_priority(
            potential_saving_period=None, observed_excess_cost=50.0, confidence="Medium", occurrence_count=1,
            severity="INFORMATION", implementation_difficulty="MEDIUM", equipment_criticality=None,
        )
        self.assertEqual(score_unknown, score_medium)

    def test_criticality_never_fabricated_stays_neutral_when_none(self):
        _, _, breakdown = opp.compute_priority(
            potential_saving_period=None, observed_excess_cost=50.0, confidence="Medium", occurrence_count=1,
            severity="INFORMATION", implementation_difficulty="UNKNOWN", equipment_criticality=None,
        )
        self.assertEqual(breakdown["criticality_component"], 0)


# ---------------------------------------------------------------------------
# Dismiss lifecycle (item 4's approval - preserved, not deleted; new
# evidence after dismissal creates a fresh row without double-counting
# old evidence)
# ---------------------------------------------------------------------------

class TestDismissLifecycle(OpportunityEngineTestBase):
    def test_dismiss_preserves_row_and_sets_fields(self):
        _seed_anomaly(self.config_db, self.plant_id, "chiller_power_deviation", "P01.UTILITY.CHL01", 6.5, 3.38)
        self._evaluate()
        row = self._get_opportunity()
        opp.dismiss_opportunity(self.config_db, row["id"], "EXPECTED_OPERATION", "legit high load", now=BASE)
        connection = sqlite3.connect(self.config_db)
        connection.row_factory = sqlite3.Row
        dismissed = dict(connection.execute("SELECT * FROM energy_opportunities WHERE id = ?", (row["id"],)).fetchone())
        connection.close()
        self.assertEqual(dismissed["status"], "DISMISSED")
        self.assertEqual(dismissed["dismissal_reason"], "EXPECTED_OPERATION")
        self.assertEqual(dismissed["dismissal_comment"], "legit high load")
        self.assertIsNotNone(dismissed["dismissed_at"])
        self.assertEqual(dismissed["observed_excess_cost"], 3.38)  # evidence preserved, not cleared

    def test_dismiss_rejects_invalid_reason(self):
        with self.assertRaises(ValueError):
            opp.dismiss_opportunity(self.config_db, 1, "NOT_A_REAL_REASON", None)

    def test_new_evidence_after_dismissal_creates_fresh_row_without_old_evidence(self):
        id1 = _seed_anomaly(
            self.config_db, self.plant_id, "chiller_power_deviation", "P01.UTILITY.CHL01", 6.5, 3.38,
            status="RESOLVED", first_detected="2026-08-01 10:00:00", last_seen="2026-08-01 12:00:00",
        )
        self._evaluate(now=BASE)
        original = self._get_opportunity()
        opp.dismiss_opportunity(self.config_db, original["id"], "NOT_ACTIONABLE", None, now=BASE)

        # No new evidence yet - must not resurrect the dismissed row.
        still_none = self._evaluate(now=BASE + timedelta(hours=1))
        self.assertTrue(all(r["action"] != "opened" for r in still_none))
        self.assertIsNone(self._get_opportunity())

        id2 = _seed_anomaly(
            self.config_db, self.plant_id, "chiller_power_deviation", "P01.UTILITY.CHL01", 9.1, 4.73,
            status="OPEN", first_detected=(BASE + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
        )
        after = self._evaluate(now=BASE + timedelta(hours=3))
        self.assertEqual(after[0]["action"], "opened")
        fresh = self._get_opportunity()
        self.assertNotEqual(fresh["id"], original["id"])  # a NEW row
        self.assertEqual(json.loads(fresh["source_anomaly_ids"]), [id2])  # id1 NOT re-linked
        self.assertAlmostEqual(fresh["observed_excess_cost"], 4.73)  # not 3.38+4.73

        connection = sqlite3.connect(self.config_db)
        statuses = connection.execute("SELECT status FROM energy_opportunities ORDER BY id").fetchall()
        connection.close()
        self.assertEqual([s[0] for s in statuses], ["DISMISSED", "NEW"])

    def test_duplicate_new_row_rejected_at_db_level(self):
        """Regression guard mirroring Phase 9's own proven partial
        unique index test."""
        _seed_anomaly(self.config_db, self.plant_id, "chiller_power_deviation", "P01.UTILITY.CHL01", 6.5, 3.38)
        self._evaluate()
        row = self._get_opportunity()
        connection = sqlite3.connect(self.config_db)
        connection.row_factory = sqlite3.Row
        full_row = dict(connection.execute("SELECT * FROM energy_opportunities WHERE id = ?", (row["id"],)).fetchone())
        connection.close()
        full_row.pop("id")
        with self.assertRaises(sqlite3.IntegrityError):
            opp._write_opportunity_row(self.config_db, None, full_row)


# ---------------------------------------------------------------------------
# Non-production gating (compressed-air rule) / false-opportunity control
# ---------------------------------------------------------------------------

class TestNonProductionGating(OpportunityEngineTestBase):
    def setUp(self):
        super().setUp()
        _insert_tag(self.config_db, "P01.UTILITY.AC01.Power_kW", unit="kW", measurement="power")
        self.rule = get_opportunity_rule("compressor_non_production_air_opportunity")

    def test_anomaly_during_production_does_not_qualify(self):
        """item 28 - compressor power legitimately tracks production
        demand; this rule must not fire when production was running."""
        connection = sqlite3.connect(self.config_db)
        plant_id = self.plant_id
        product = connection.execute("SELECT id FROM products WHERE active = 1 LIMIT 1").fetchone()[0]
        t = BASE
        connection.execute(
            "INSERT INTO production_batches (batch_code, product_id, plant_id, status, start_time, end_time, "
            "planned_quantity, actual_quantity, good_quantity, reject_quantity, unit_of_measure, source, created_at) "
            "VALUES ('TEST-NPG', ?, ?, 'completed', ?, ?, 100, 100, 100, 0, 'kg', 'simulated', ?)",
            (product, plant_id, (t - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S"),
             (t + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S"), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        connection.commit()
        connection.close()

        _seed_anomaly(self.config_db, self.plant_id, "compressor_power_deviation", "P01.UTILITY.AC01", 5.0, 2.5,
                      first_detected=t.strftime("%Y-%m-%d %H:%M:%S"), last_seen=t.strftime("%Y-%m-%d %H:%M:%S"))
        results = opp.evaluate_opportunity_rule(self.rule, self.config_db, self.machine_db, self.historian, self.plant_id, "p01", now=t)
        self.assertEqual(results, [])

    def test_anomaly_during_non_production_qualifies(self):
        _seed_anomaly(self.config_db, self.plant_id, "compressor_power_deviation", "P01.UTILITY.AC01", 5.0, 2.5)
        results = opp.evaluate_opportunity_rule(self.rule, self.config_db, self.machine_db, self.historian, self.plant_id, "p01", now=BASE)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["action"], "opened")


# ---------------------------------------------------------------------------
# Plant independence
# ---------------------------------------------------------------------------

class TestPlantIndependence(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_db = Path(self.temp_dir.name) / "config.db"
        self.machine_db = Path(self.temp_dir.name) / "machine_data.db"
        _seed_config_db(self.config_db)
        migrate_opportunity(self.config_db, backup=False)
        self.historian = DatabaseManager(db_path=self.machine_db)
        _insert_tag(self.config_db, "P01.UTILITY.CHL01.Power_kW", unit="kW", measurement="power")
        _insert_tag(self.config_db, "P02.UTILITY.CHL01.Power_kW", unit="kW", measurement="power")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_p01_opportunity_never_appears_under_p02(self):
        rule = get_opportunity_rule("chiller_power_opportunity")
        p01_id, p02_id = _plant_id(self.config_db, "p01"), _plant_id(self.config_db, "p02")
        _seed_anomaly(self.config_db, p01_id, "chiller_power_deviation", "P01.UTILITY.CHL01", 6.5, 3.38)

        p01_results = opp.evaluate_opportunity_rule(rule, self.config_db, self.machine_db, self.historian, p01_id, "p01", now=BASE)
        p02_results = opp.evaluate_opportunity_rule(rule, self.config_db, self.machine_db, self.historian, p02_id, "p02", now=BASE)

        self.assertEqual(len(p01_results), 1)
        self.assertEqual(p02_results, [])


# ---------------------------------------------------------------------------
# No LLM, no PLC/setpoint writes (source-inspection, mirrors Phase 7's
# UI-layer discipline)
# ---------------------------------------------------------------------------

class TestNoLLMNoControlWrites(unittest.TestCase):
    def test_opportunity_engine_never_imports_ai_modules_or_plc_writers(self):
        source = Path("engine/opportunity_engine.py").read_text()
        for banned in ("import ai.", "from ai.", "ai_provider", "AIProvider", "plc.driver_factory", "opcua", "modbus"):
            self.assertNotIn(banned, source)

    def test_opportunity_worker_never_imports_ai_modules_or_plc_writers(self):
        source = Path("app/opportunity_worker.py").read_text()
        for banned in ("import ai.", "from ai.", "ai_provider", "plc.driver_factory", "opcua", "modbus"):
            self.assertNotIn(banned, source)


if __name__ == "__main__":
    unittest.main()
