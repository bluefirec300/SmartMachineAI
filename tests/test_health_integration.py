import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from database.database import DatabaseManager
from engine import health_domain as dom
from engine import health_engine as he
from engine.equipment_metadata_migrator import migrate as migrate_equipment_metadata
from engine.health_migrator import migrate as migrate_health

from tests.test_anomaly_engine import _seed_config_db, _insert_tag, _plant_id
from tests.test_health_engine import BASE, _seed_equipment, _seed_mature_baseline, _create_machine_events_table

"""
Phase 12.4 - end-to-end / cross-page acceptance tests for the
Equipment Health integration points (SCADA Floor Plan, Service &
Maintenance, Equipment & Tag Configuration, Home). Uses the real
ui.health_data read layer against an isolated throwaway database -
never the live one.
"""


class HealthIntegrationTestBase(unittest.TestCase):
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
        self.equipment_id = _seed_equipment(self.config_db, "p01_pump_wsp01")
        _insert_tag(self.config_db, "P01.WATER.WSP01.Vibration", unit="mm/s", measurement="vibration")

        self._patches = [patch("ui.health_data.CONFIG_DATABASE_PATH", str(self.config_db))]
        for p in self._patches:
            p.start()

        # Imported AFTER the patch starts, so the module picks up the patched path immediately.
        from ui import health_data as hd
        self.hd = hd

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.temp_dir.cleanup()

    def _seed_all_clean(self, confidence="High"):
        for target in ("Vibration", "BearingTemp", "Power_kW", "Flow", "delta_p_bar", "flow_per_kw"):
            _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP01", target, "water_supply_pump", confidence=confidence)

    def _calculate_and_persist(self, offset=timedelta(0), change_reason="initial"):
        result = he.calculate_health(self.config_db, self.machine_db, self.plant_id, "p01", "water_supply_pump", "P01.WATER.WSP01", now=BASE + offset)
        dom.persist_snapshot(self.config_db, result, change_reason, now=BASE + offset)
        return result


# ---------------------------------------------------------------------------
# Item 20 - end-to-end traceability
# ---------------------------------------------------------------------------

class TestEndToEndTraceability(HealthIntegrationTestBase):
    def test_equipment_to_snapshot_to_factor_to_ui_summary_to_detail(self):
        self._seed_all_clean()
        result = self._calculate_and_persist()

        # UI cross-page summary (equipment_id-keyed, item 5/9).
        compact = self.hd.compact_health_context(self.equipment_id)
        self.assertTrue(compact["assessed"])
        self.assertEqual(compact["score"], f"{result.health_score:.1f}")
        self.assertEqual(compact["state"], result.health_band)

        # Equipment Health detail reconstructs the SAME score purely
        # from persisted factor rows - no UI-side scoring arithmetic.
        detail = self.hd.get_detail(self.plant_id, "P01.WATER.WSP01")
        penalized_sum = sum(f["penalty"] for f in detail["factors"] if f["status"] == "penalized")
        self.assertAlmostEqual(100.0 - penalized_sum, detail["latest"]["health_score"], places=1)
        self.assertEqual(detail["latest"]["health_score"], result.health_score)


# ---------------------------------------------------------------------------
# Item 21 - cross-page consistency
# ---------------------------------------------------------------------------

class TestCrossPageConsistency(HealthIntegrationTestBase):
    def test_all_read_paths_report_identical_values_for_the_same_equipment(self):
        self._seed_all_clean()
        _ = self._calculate_and_persist()

        # Path 1: Service & Maintenance / Equipment Config style (by equipment_id).
        compact = self.hd.compact_health_context(self.equipment_id)

        # Path 2: SCADA Floor Plan style (bulk, by equipment_id list).
        bulk = self.hd.get_latest_health_by_equipment_ids([self.equipment_id])[self.equipment_id]

        # Path 3: Equipment Health overview.
        overview_row = next(r for r in self.hd.get_overview("p01") if r["equipment_id"] == self.equipment_id)

        # Path 4: Equipment Health detail.
        detail = self.hd.get_detail(self.plant_id, "P01.WATER.WSP01")

        self.assertEqual(compact["score"], self.hd.score_text(bulk))
        self.assertEqual(self.hd.score_text(bulk), self.hd.score_text(overview_row))
        self.assertEqual(self.hd.score_text(overview_row), self.hd.score_text(detail["latest"]))

        self.assertEqual(compact["state"], self.hd.state_text(bulk))
        self.assertEqual(self.hd.state_text(bulk), self.hd.state_text(overview_row))
        self.assertEqual(self.hd.state_text(overview_row), self.hd.state_text(detail["latest"]))

        self.assertEqual(compact["confidence"], self.hd.confidence_text(bulk))
        self.assertEqual(self.hd.confidence_text(bulk), self.hd.confidence_text(overview_row))

        self.assertEqual(bulk["computed_at"], overview_row["computed_at"])
        self.assertEqual(overview_row["computed_at"], detail["latest"]["computed_at"])


# ---------------------------------------------------------------------------
# Items 4/23 - NULL / INSUFFICIENT / Not Assessed acceptance
# ---------------------------------------------------------------------------

class TestNullInsufficientAndNotAssessed(HealthIntegrationTestBase):
    def test_no_snapshot_at_all_is_not_assessed(self):
        compact = self.hd.compact_health_context(self.equipment_id)
        self.assertFalse(compact["assessed"])
        self.assertEqual(compact["state"], "Not Assessed")
        self.assertEqual(compact["score"], "Unavailable")

    def test_real_snapshot_with_null_score_is_insufficient_data_never_0_or_100(self):
        # No baselines seeded at all -> a real persisted row with health_score=None.
        result = self._calculate_and_persist()
        self.assertIsNone(result.health_score)

        compact = self.hd.compact_health_context(self.equipment_id)
        self.assertTrue(compact["assessed"])  # a real row DOES exist
        self.assertEqual(compact["state"], "Insufficient Data")
        self.assertEqual(compact["score"], "Unavailable")
        self.assertNotEqual(compact["state"], "HEALTHY")
        self.assertNotIn("0", compact["score"])
        self.assertNotIn("100", compact["score"])

    def test_bulk_lookup_never_converts_missing_to_healthy(self):
        other_equipment_id = _seed_equipment(self.config_db, "p01_pump_wsp02")
        bulk = self.hd.get_latest_health_by_equipment_ids([self.equipment_id, other_equipment_id])
        self.assertEqual(bulk, {})  # neither has ever been assessed - correctly empty, not defaulted


# ---------------------------------------------------------------------------
# Item 24 - provisional acceptance
# ---------------------------------------------------------------------------

class TestProvisionalAcceptance(HealthIntegrationTestBase):
    def test_low_confidence_provisional_displays_clearly_across_paths(self):
        for target in ("Vibration", "BearingTemp", "flow_per_kw"):
            _seed_mature_baseline(self.config_db, self.plant_id, "P01.WATER.WSP01", target, "water_supply_pump", confidence="Low")
        result = self._calculate_and_persist()
        self.assertTrue(result.provisional)

        compact = self.hd.compact_health_context(self.equipment_id)
        self.assertIn("Provisional", compact["confidence"])
        self.assertIn("LOW", compact["confidence"])

        bulk = self.hd.get_latest_health_by_equipment_ids([self.equipment_id])[self.equipment_id]
        self.assertEqual(self.hd.confidence_text(bulk), compact["confidence"])


# ---------------------------------------------------------------------------
# Item 11 - vocabulary consistency (no alarm-style terms)
# ---------------------------------------------------------------------------

class TestVocabularyConsistency(unittest.TestCase):
    def test_state_text_never_produces_alarm_style_words(self):
        from ui.health_data import state_text
        for record in (
            {"health_band": "HEALTHY"}, {"health_band": "MONITOR"}, {"health_band": "ATTENTION"},
            {"health_band": "INVESTIGATE"}, {"health_band": None}, None,
        ):
            text = state_text(record)
            for banned in ("GOOD", "BAD", "CRITICAL", "FAIL", "DANGER", "ALARM"):
                self.assertNotIn(banned, text.upper() if text != text.upper() else text)
        self.assertIn(state_text({"health_band": "HEALTHY"}), ("HEALTHY",))
        self.assertEqual(state_text(None), "Not Assessed")


# ---------------------------------------------------------------------------
# Item 10 - bulk lookup, no N+1
# ---------------------------------------------------------------------------

class TestNoNPlusOneQueries(HealthIntegrationTestBase):
    def test_bulk_lookup_issues_exactly_one_query_regardless_of_equipment_count(self):
        equipment_ids = [self.equipment_id]
        for i in range(5):
            equipment_ids.append(_seed_equipment(self.config_db, f"p01_pump_extra{i}"))
        self._seed_all_clean()
        self._calculate_and_persist()

        query_count = {"n": 0}
        real_connect = sqlite3.connect

        def _counting_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            connection.set_trace_callback(lambda sql: query_count.__setitem__("n", query_count["n"] + 1))
            return connection

        with patch("ui.health_data.sqlite3.connect", side_effect=_counting_connect):
            self.hd.get_latest_health_by_equipment_ids(equipment_ids)

        # One connection's worth of SQL statements (the SELECT itself,
        # not one execute per equipment_id) - well bounded regardless of
        # how many ids were passed in.
        self.assertLessEqual(query_count["n"], 2)


# ---------------------------------------------------------------------------
# Items 9/17/18/19 - source-inspection guards across every integrated file
# ---------------------------------------------------------------------------

class TestIntegrationSourceGuards(unittest.TestCase):
    INTEGRATED_FILES = (
        "ui/scada_floor_plan_data.py",
        "ui/pages/12_SCADA_Floor_Plan.py",
        "ui/pages/4_Service_and_Maintenance.py",
        "ui/pages/11_Equipment_and_Tag_Configuration.py",
        "ui/Home.py",
    )

    def test_no_health_engine_import_or_recalculation(self):
        for path in self.INTEGRATED_FILES:
            source = Path(path).read_text()
            for banned in (
                "from engine.health_engine", "from engine import health_engine", "import engine.health_engine",
                "_apply_family_caps", "SEVERITY_BASE_PENALTY", "CONFIDENCE_MULTIPLIER",
            ):
                self.assertNotIn(banned, source, msg=f"found {banned!r} in {path}")

    def test_no_direct_health_table_writes(self):
        for path in self.INTEGRATED_FILES:
            source = Path(path).read_text()
            for banned in (
                "INSERT INTO equipment_health_snapshots", "UPDATE equipment_health_snapshots",
                "INSERT INTO equipment_health_factor_snapshots", "UPDATE equipment_health_factor_snapshots",
                "DELETE FROM equipment_health",
            ):
                self.assertNotIn(banned, source, msg=f"found {banned!r} in {path}")

    def test_no_historian_query_for_health_context(self):
        # Health integration must never query plc_data directly - only
        # the already-persisted health tables via ui.health_data.
        for path in ("ui/pages/4_Service_and_Maintenance.py", "ui/pages/11_Equipment_and_Tag_Configuration.py", "ui/Home.py"):
            source = Path(path).read_text()
            self.assertNotIn("FROM plc_data", source)

    def test_no_llm_or_plc_control_writes(self):
        # Note: ai.rule_engine (deterministic threshold evaluation, no
        # LLM) is legitimately imported by the pre-existing
        # scada_floor_plan_data.py - only the actual LLM-calling
        # modules are banned here, not the whole "ai." package (which
        # predates Phase 12 and includes non-LLM deterministic code).
        for path in self.INTEGRATED_FILES:
            source = Path(path).read_text()
            for banned in (
                "ai_provider", "ai.prompt_builder", "ai.providers", "ai.root_cause_engine",
                "plc.driver_factory", "write_tag", "set_setpoint", "reset_alarm", "start_equipment", "stop_equipment",
            ):
                self.assertNotIn(banned, source, msg=f"found {banned!r} in {path}")

    def test_service_and_maintenance_health_block_never_mutates_maintenance_state(self):
        """The health-context block itself must not call the write
        functions this page uses elsewhere for logging real maintenance
        work - reading health must never side-effect maintenance data."""
        source = Path("ui/pages/4_Service_and_Maintenance.py").read_text()
        health_block_start = source.index("Phase 12.4 - read-only Equipment Health context")
        health_block_end = source.index("if selected_row[\"status\"] != \"OVERDUE\":")
        health_block = source[health_block_start:health_block_end]
        for banned in ("add_maintenance_entry(", "add_service_entry(", "_run_update("):
            self.assertNotIn(banned, health_block)

    def test_scada_health_block_uses_separate_color_contract_from_alarm_severity(self):
        """item 13 - health state must never be tinted with the PLC
        alarm/warning SEVERITY_COLOR palette."""
        source = Path("ui/pages/12_SCADA_Floor_Plan.py").read_text()
        health_block_start = source.index("let healthBlock = ''")
        health_block_end = source.index("let rows = ")
        health_block = source[health_block_start:health_block_end]
        self.assertIn("HEALTH_BAND_COLOR", health_block)
        self.assertNotIn("SEVERITY_COLOR", health_block)

    def test_equipment_ids_used_not_display_name_string_parsing(self):
        """item 5 - cross-page health lookups must key off the stable
        equipment_id, never parse/match a display name string."""
        for path in ("ui/pages/4_Service_and_Maintenance.py", "ui/pages/11_Equipment_and_Tag_Configuration.py"):
            source = Path(path).read_text()
            self.assertIn("compact_health_context(selected_equipment[\"id\"])", source)


if __name__ == "__main__":
    unittest.main()
