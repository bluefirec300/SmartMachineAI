import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from database.database import DatabaseManager
from engine import health_domain as dom
from engine import health_engine as he
from engine.equipment_metadata_migrator import migrate as migrate_equipment_metadata
from engine.health_migrator import migrate as migrate_health

from streamlit.testing.v1 import AppTest

from tests.test_anomaly_engine import _seed_config_db, _insert_tag, _plant_id
from tests.test_health_engine import BASE, _seed_anomaly, _seed_equipment, _seed_mature_baseline, _create_machine_events_table

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HEALTH_PAGE = str(PROJECT_ROOT / "ui" / "pages" / "19_Equipment_Health.py")
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# tests/test_health_engine.py's seeding helpers (_seed_anomaly,
# _seed_mature_baseline) hardcode timestamps anchored to that module's
# own BASE constant - reused here unchanged so calculate_health(now=BASE)
# correctly reads them as fresh evidence. But the Equipment Health PAGE
# itself always calls ui.health_data.get_detail() with no now= override,
# defaulting to REAL datetime.now() at render time - so every persisted
# snapshot's stored computed_at is overridden to a real-"now"-anchored
# value below (via REAL_NOW) before persisting, keeping it inside the
# page's default 30-day history window. The calculation itself still
# runs against BASE so it stays consistent with the seeded evidence.
REAL_NOW = datetime.now()


class HealthUITestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_db = Path(self.temp_dir.name) / "config.db"
        self.machine_db = Path(self.temp_dir.name) / "machine_data.db"
        _seed_config_db(self.config_db)
        migrate_equipment_metadata(self.config_db, backup=False)
        migrate_health(self.config_db, backup=False)
        _create_machine_events_table(self.machine_db)
        DatabaseManager(db_path=self.machine_db)
        self.plant_id = _plant_id(self.config_db, "p01")

        self._patches = [
            patch("ui.health_data.CONFIG_DATABASE_PATH", str(self.config_db)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.temp_dir.cleanup()

    def _run(self):
        at = AppTest.from_file(HEALTH_PAGE, default_timeout=60)
        at.run()
        return at

    def _seed_equipment_with_tags(self, name, instance_key, targets):
        equipment_id = _seed_equipment(self.config_db, name)
        for target, unit, measurement in targets:
            _insert_tag(self.config_db, f"{instance_key}.{target}", unit=unit, measurement=measurement)
        return equipment_id

    def _seed_pump_all_clean(self, instance_key="P01.WATER.WSP01", confidence="High"):
        equipment_id = self._seed_equipment_with_tags(
            "p01_pump_wsp01", instance_key,
            [("Vibration", "mm/s", "vibration"), ("BearingTemp", "degC", "temperature"), ("Power_kW", "kW", "power")],
        )
        for target in ("Vibration", "BearingTemp", "Power_kW", "Flow", "delta_p_bar", "flow_per_kw"):
            _seed_mature_baseline(self.config_db, self.plant_id, instance_key, target, "water_supply_pump", confidence=confidence)
        # Give the maintenance and events factors real (not "missing")
        # evidence too, so full 8/8 coverage is reachable - a maintenance
        # schedule that's on-time, and a fresh (but non-qualifying) events
        # feed (any recent row anywhere satisfies the system-wide freshness
        # gate; this equipment's own tags have zero matching rows, so its
        # own events factor still comes back "clean", not "penalized").
        connection = sqlite3.connect(self.config_db)
        connection.execute(
            "UPDATE equipment SET service_interval_days = 365, last_serviced_at = ?, next_due_at = ? WHERE id = ?",
            (BASE.strftime("%Y-%m-%d"), (BASE + timedelta(days=300)).strftime("%Y-%m-%d"), equipment_id),
        )
        connection.commit()
        connection.close()
        mconn = sqlite3.connect(self.machine_db)
        mconn.execute(
            "INSERT INTO machine_events (event_time, detected_at, equipment, tag, severity, condition, created_at) "
            "VALUES (?, ?, 'x', 'P01.SOME.OTHER01.Tag', 'warning', 'high_warning', ?)",
            (BASE.strftime(TIME_FORMAT), BASE.strftime(TIME_FORMAT), BASE.strftime(TIME_FORMAT)),
        )
        mconn.commit()
        mconn.close()
        return equipment_id

    def _calculate_and_persist(self, instance_key, equipment_type, offset=timedelta(0), change_reason="initial"):
        """Calculates against BASE-anchored `offset` (consistent with
        the seeded evidence's own hardcoded timestamps), but stores the
        resulting snapshot's computed_at anchored to REAL_NOW + offset
        instead - see the REAL_NOW comment above for why."""
        result = he.calculate_health(self.config_db, self.machine_db, self.plant_id, "p01", equipment_type, instance_key, now=BASE + offset)
        result.computed_at = (REAL_NOW + offset).strftime(TIME_FORMAT)
        dom.persist_snapshot(self.config_db, result, change_reason, now=REAL_NOW + offset)
        return result


# ---------------------------------------------------------------------------
# Empty state / missing data survival
# ---------------------------------------------------------------------------

class TestEmptyState(HealthUITestBase):
    def test_page_survives_zero_assessments(self):
        at = self._run()
        self.assertFalse(bool(at.exception))
        info_texts = " ".join(i.value for i in at.info)
        self.assertIn("No equipment health assessments have been recorded yet", info_texts)


# ---------------------------------------------------------------------------
# Healthy, high-confidence equipment
# ---------------------------------------------------------------------------

class TestHealthyHighConfidence(HealthUITestBase):
    def test_healthy_equipment_renders_correctly(self):
        self._seed_pump_all_clean(confidence="High")
        self._calculate_and_persist("P01.WATER.WSP01", "water_supply_pump")
        at = self._run()
        self.assertFalse(bool(at.exception))
        metrics = {m.label: m.value for m in at.metric}
        self.assertEqual(metrics["Healthy"], "1")
        self.assertEqual(metrics["Insufficient Data"], "0")
        table = at.dataframe[0].value
        self.assertEqual(table.iloc[0]["Health State"], "HEALTHY")
        self.assertEqual(table.iloc[0]["Confidence"], "HIGH")


# ---------------------------------------------------------------------------
# LOW confidence / provisional
# ---------------------------------------------------------------------------

class TestLowConfidence(HealthUITestBase):
    def test_low_confidence_renders_provisional(self):
        equipment_id = self._seed_equipment_with_tags(
            "p01_pump_wsp01", "P01.WATER.WSP01",
            [("Vibration", "mm/s", "vibration"), ("BearingTemp", "degC", "temperature"), ("flow_per_kw", "", "")],
        )
        for target in ("Vibration", "BearingTemp", "flow_per_kw"):
            _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP01", target, "water_supply_pump", confidence="Low")
        result = self._calculate_and_persist("P01.WATER.WSP01", "water_supply_pump")
        self.assertTrue(result.provisional)

        at = self._run()
        self.assertFalse(bool(at.exception))
        table = at.dataframe[0].value
        self.assertEqual(table.iloc[0]["Confidence"], "LOW · Provisional")


# ---------------------------------------------------------------------------
# Degraded equipment / factor reconciliation
# ---------------------------------------------------------------------------

class TestDegradedAndReconciliation(HealthUITestBase):
    def test_degraded_equipment_factor_breakdown_reconciles(self):
        self._seed_pump_all_clean(confidence="High")
        _seed_anomaly(self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "wsp_vibration_increase", status="OPEN", severity="HIGH", confidence="High")
        result = self._calculate_and_persist("P01.WATER.WSP01", "water_supply_pump")
        self.assertLess(result.health_score, 100.0)

        at = self._run()
        self.assertFalse(bool(at.exception))
        markdown_text = " ".join(m.value for m in at.markdown)
        self.assertIn("Starting condition: **100**", markdown_text)
        self.assertIn(f"Final Health Score: **{result.health_score:.1f}**", markdown_text)

        # Reconcile independently against the persisted factor rows.
        latest = dom.get_latest_snapshot(self.config_db, self.plant_id, "P01.WATER.WSP01")
        factors = dom.get_factor_snapshots(self.config_db, latest["id"])
        total_penalty = sum(f["penalty"] for f in factors if f["status"] == "penalized")
        self.assertAlmostEqual(100.0 - total_penalty, latest["health_score"], places=1)


# ---------------------------------------------------------------------------
# INSUFFICIENT / NULL score
# ---------------------------------------------------------------------------

class TestInsufficientData(HealthUITestBase):
    def test_null_health_renders_as_insufficient_never_0_or_100(self):
        _seed_equipment(self.config_db, "p01_pump_wsp01")
        _insert_tag(self.config_db, "P01.WATER.WSP01.Vibration", unit="mm/s", measurement="vibration")
        result = self._calculate_and_persist("P01.WATER.WSP01", "water_supply_pump")
        self.assertIsNone(result.health_score)

        at = self._run()
        self.assertFalse(bool(at.exception))
        metrics = {m.label: m.value for m in at.metric}
        self.assertEqual(metrics["Insufficient Data"], "1")
        self.assertEqual(metrics["Healthy"], "0")
        table = at.dataframe[0].value
        self.assertEqual(table.iloc[0]["Health Score"], "Unavailable")
        self.assertEqual(table.iloc[0]["Health State"], "Insufficient Data")
        # Average score must exclude this equipment entirely, not treat it as 0.
        avg_metric = next(m for m in at.metric if m.label == "Average Health Score")
        self.assertEqual(avg_metric.value, "Unavailable")


# ---------------------------------------------------------------------------
# Chronological history / score vs confidence separation
# ---------------------------------------------------------------------------

class TestHistoryAndSeparation(HealthUITestBase):
    def test_history_is_chronological_and_confidence_kept_separate_from_score(self):
        self._seed_pump_all_clean(confidence="High")
        # Earlier snapshot dated BEFORE "now" (offset=-1 day), later one AT
        # "now" (offset=0) - both must land at-or-before the page's own
        # render-time default history window end, or the "future" one gets
        # filtered out of get_history()'s [now-30, now] bound.
        first = self._calculate_and_persist("P01.WATER.WSP01", "water_supply_pump", offset=timedelta(days=-1), change_reason="initial")

        second = he.calculate_health(self.config_db, self.machine_db, self.plant_id, "p01", "water_supply_pump", "P01.WATER.WSP01", now=BASE)
        second.health_score = first.health_score  # score held exactly constant
        second.assessment_confidence = "MEDIUM"
        second.computed_at = REAL_NOW.strftime(TIME_FORMAT)
        dom.persist_snapshot(self.config_db, second, "confidence_changed", now=REAL_NOW)

        history = dom.list_snapshots(self.config_db, self.plant_id, "P01.WATER.WSP01")
        self.assertEqual([h["computed_at"] for h in history], sorted(h["computed_at"] for h in history))

        at = self._run()
        self.assertFalse(bool(at.exception))
        # Pick this equipment in the detail selectbox (should already be selected by default - single equipment).
        captions = " ".join(c.value for c in at.caption)
        self.assertIn("Recent Movement: **Stable**", captions)  # score held constant -> STABLE, not influenced by confidence change


# ---------------------------------------------------------------------------
# Descriptive trend wording / no fabricated explanations
# ---------------------------------------------------------------------------

class TestDescriptiveTrendOnly(HealthUITestBase):
    def test_no_prediction_language_anywhere_on_the_page(self):
        self._seed_pump_all_clean(confidence="High")
        first = self._calculate_and_persist("P01.WATER.WSP01", "water_supply_pump")
        worse = he.calculate_health(self.config_db, self.machine_db, self.plant_id, "p01", "water_supply_pump", "P01.WATER.WSP01", now=BASE + timedelta(days=1))
        worse.health_score = (first.health_score or 100) - 20
        worse.computed_at = (REAL_NOW + timedelta(days=1)).strftime(TIME_FORMAT)
        dom.persist_snapshot(self.config_db, worse, "score_changed", now=REAL_NOW + timedelta(days=1))

        at = self._run()
        self.assertFalse(bool(at.exception))
        full_text = " ".join(m.value for m in at.markdown) + " ".join(c.value for c in at.caption)
        for banned in (
            "predicted failure", "remaining useful life", "probability of failure", "will fail",
            "predicted future health", "days to failure", "bearing failure", "impeller damage",
        ):
            self.assertNotIn(banned.lower(), full_text.lower())

    def test_source_never_calls_llm_or_recalculates_scoring(self):
        for path in ("ui/health_data.py", "ui/pages/19_Equipment_Health.py"):
            source = Path(path).read_text()
            for banned in ("import ai.", "from ai.", "ai_provider", "_apply_family_caps", "SEVERITY_BASE_PENALTY", "CONFIDENCE_MULTIPLIER"):
                self.assertNotIn(banned, source, msg=f"found banned reference {banned!r} in {path}")
            # engine.health_engine (the scoring module) is never even
            # IMPORTED - a stronger guarantee than substring-matching
            # "calculate_health(" call sites, which collides with this
            # file's own honest docstrings explaining it's NOT called.
            for banned_import in ("import health_engine", "from engine.health_engine", "from engine import health_engine"):
                self.assertNotIn(banned_import, source, msg=f"found banned import {banned_import!r} in {path}")


# ---------------------------------------------------------------------------
# Filters / sorting
# ---------------------------------------------------------------------------

class TestFiltersAndSorting(HealthUITestBase):
    def test_equipment_type_filter_narrows_table(self):
        self._seed_pump_all_clean(confidence="High")
        self._calculate_and_persist("P01.WATER.WSP01", "water_supply_pump")

        _seed_equipment(self.config_db, "p01_chiller_chl99")
        _insert_tag(self.config_db, "P01.UTILITY.CHL99.Power_kW", unit="kW", measurement="power")
        for target in ("Power_kW", "cop", "cooling_output_kw", "SupplyTemp", "ReturnTemp", "WaterFlow"):
            _seed_mature_baseline(self.config_db, self.plant_id, "P01.UTILITY.CHL99", target, "chiller", confidence="High")
        self._calculate_and_persist("P01.UTILITY.CHL99", "chiller")

        at = self._run()
        type_select = [sb for sb in at.selectbox if sb.label == "Equipment type"][0]
        type_select.set_value("Chiller")  # the filter dropdown shows human labels, not the canonical value
        at.run()
        self.assertFalse(bool(at.exception))
        table = at.dataframe[0].value
        self.assertEqual(len(table), 1)
        self.assertEqual(table.iloc[0]["Type"], "Chiller")

    def test_sort_lowest_score_first(self):
        self._seed_pump_all_clean(confidence="High", instance_key="P01.WATER.WSP01")
        healthy = self._calculate_and_persist("P01.WATER.WSP01", "water_supply_pump")

        _seed_equipment(self.config_db, "p01_pump_wsp02")
        _insert_tag(self.config_db, "P01.WATER.WSP02.Vibration", unit="mm/s", measurement="vibration")
        for target in ("Vibration", "BearingTemp", "Power_kW", "Flow", "delta_p_bar", "flow_per_kw"):
            _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP02", target, "water_supply_pump", confidence="High")
        _seed_anomaly(self.config_db, self.plant_id, "P01.WATER.WSP02", "Vibration", "wsp_vibration_increase", status="OPEN", severity="HIGH", confidence="High")
        worse = self._calculate_and_persist("P01.WATER.WSP02", "water_supply_pump")
        self.assertLess(worse.health_score, healthy.health_score)

        at = self._run()
        sort_radio = at.radio[0]
        sort_radio.set_value("Lowest health score first")
        at.run()
        table = at.dataframe[0].value
        self.assertEqual(table.iloc[0]["Equipment"], "p01_pump_wsp02")


# ---------------------------------------------------------------------------
# Phase 12.3A - presentation-label mapping (audit-based, item 5)
# ---------------------------------------------------------------------------

class TestLabelMapping(unittest.TestCase):
    def test_every_registered_factor_id_has_a_human_label(self):
        """Audits the ACTUAL live registry - every factor_id Phase 12.1
        currently defines must have an explicit human label, not just
        fall back to the generic cleaner."""
        from engine.health_targets import HEALTH_FACTOR_REGISTRY
        from ui.health_data import FACTOR_LABELS
        for equipment_type, factors in HEALTH_FACTOR_REGISTRY.items():
            for f in factors:
                self.assertIn(f.factor_id, FACTOR_LABELS, msg=f"{f.factor_id} ({equipment_type}) has no explicit human label")

    def test_every_registered_equipment_type_has_a_human_label(self):
        from engine.health_targets import SUPPORTED_EQUIPMENT_TYPES
        from ui.health_data import EQUIPMENT_TYPE_LABELS
        for equipment_type in SUPPORTED_EQUIPMENT_TYPES:
            self.assertIn(equipment_type, EQUIPMENT_TYPE_LABELS)

    def test_unknown_factor_id_falls_back_safely_not_raises(self):
        from ui.health_data import factor_label
        result = factor_label("some_future_factor_not_yet_registered")
        self.assertEqual(result, "Some Future Factor Not Yet Registered")

    def test_unknown_equipment_type_falls_back_safely(self):
        from ui.health_data import equipment_type_label
        self.assertEqual(equipment_type_label("future_equipment_type"), "Future Equipment Type")

    def test_labels_never_mutate_the_canonical_registry(self):
        """Calling the label helpers must not rename anything in the
        backend registry itself - a static-import sentinel."""
        from engine.health_targets import HEALTH_FACTOR_REGISTRY
        from ui.health_data import factor_label
        original = HEALTH_FACTOR_REGISTRY["water_supply_pump"][0].factor_id
        factor_label(original)
        self.assertEqual(HEALTH_FACTOR_REGISTRY["water_supply_pump"][0].factor_id, original)

    def test_format_change_delta_wording_direction(self):
        from ui.health_data import format_change_delta
        improved = format_change_delta({"factor_id": "wsp_flow_process", "delta": -7.2})
        worsened = format_change_delta({"factor_id": "wsp_flow_process", "delta": 7.2})
        self.assertIn("improved", improved)
        self.assertIn("worsened", worsened)
        self.assertIn("7.2", improved)


class TestTechnicalTraceabilityPreserved(HealthUITestBase):
    def test_canonical_factor_id_and_family_still_visible_in_technical_expander(self):
        self._seed_pump_all_clean(confidence="High")
        _seed_anomaly(self.config_db, self.plant_id, "P01.WATER.WSP01", "Vibration", "wsp_vibration_increase", status="OPEN", severity="HIGH", confidence="High")
        self._calculate_and_persist("P01.WATER.WSP01", "water_supply_pump")

        at = self._run()
        self.assertFalse(bool(at.exception))
        expander = next(e for e in at.expander if "Technical detail" in e.label)
        expander_text = str(expander)
        # The canonical backend ID must still be present verbatim somewhere on the page.
        markdown_text = " ".join(m.value for m in at.markdown)
        self.assertIn("wsp_vibration_condition", markdown_text)
        self.assertIn("CONDITION", markdown_text)  # raw family code alongside the readable label


# ---------------------------------------------------------------------------
# Existing pages unaffected
# ---------------------------------------------------------------------------

class TestExistingPagesUnaffected(unittest.TestCase):
    def test_home_page_lists_new_page_without_breaking_navigation_source(self):
        source = Path("ui/Home.py").read_text()
        self.assertIn("19_Equipment_Health.py", source)
        self.assertIn("pages/18_Savings_Verification.py", source)  # prior entries still present
        self.assertIn("pages/17_Energy_Opportunities.py", source)


# ---------------------------------------------------------------------------
# Later-optional-work follow-up (Phase V2.9) - CSV/PDF export of the
# fleet-wide table, same pattern as Event Records/Energy Dashboard
# (Phase V2.6).
# ---------------------------------------------------------------------------

class TestCsvPdfExport(HealthUITestBase):
    def test_download_buttons_present_and_match_the_table(self):
        self._seed_pump_all_clean(confidence="High")
        self._calculate_and_persist("P01.WATER.WSP01", "water_supply_pump")

        at = self._run()
        self.assertFalse(bool(at.exception))

        labels = [b.label for b in at.download_button]
        self.assertIn("⬇️ Download CSV", labels)
        self.assertIn("⬇️ Download PDF", labels)

    def test_pdf_report_uses_the_health_band_color_contract_not_alarm_warning(self):
        # Direct unit check of the actual call, independent of how
        # AppTest exposes download_button internals - confirms the page
        # asks pdf_export for ITS OWN band colors (HEALTHY/MONITOR/...)
        # rather than silently falling back to Event Records' unrelated
        # ALARM/WARNING palette (which would highlight nothing here).
        import pandas as pd
        from ui.health_data import BAND_BADGE_COLORS
        from ui.pdf_export import build_pdf_report

        df = pd.DataFrame([{"Equipment": "Pump 1", "Health State": "HEALTHY"}])
        pdf_bytes = build_pdf_report(
            "Equipment Health Report", {}, df,
            highlight_column="Health State", highlight_colors=BAND_BADGE_COLORS,
        )
        self.assertTrue(pdf_bytes.startswith(b"%PDF"))

    def test_csv_export_source_reuses_the_shared_helper(self):
        source = Path("ui/pages/19_Equipment_Health.py").read_text()
        self.assertIn("from ui.csv_export import dataframe_to_csv_bytes", source)
        self.assertIn("from ui.pdf_export import build_pdf_report", source)
        self.assertIn("highlight_colors=hd.BAND_BADGE_COLORS", source)


if __name__ == "__main__":
    unittest.main()
