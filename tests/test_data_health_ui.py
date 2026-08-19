import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from database.database import DatabaseManager
from database.initialize_config_db import initialize_config_database
from engine import data_health_engine as dhe
from engine import data_health_targets as dht
from ui import data_health_data as dhd

"""
Phase 16.2 - Data Health UI adapter tests. Most scenarios use a
controlled EquipmentDataHealth/TagDataHealth fixture patched directly
into engine.data_health_engine.calculate_equipment_data_health, so the
UI-adapter mapping is tested in isolation from the (already separately
tested, Phase 16.1) engine's own calculation logic - exactly the
separation-of-concerns item 17 requires ("adapter performs no Data
Health calculation itself").
"""

BASE = datetime(2026, 8, 20, 12, 0, 0)
INSTANCE_KEY = "P01.WATER.WSP01"


def _tag(
    tag_name="P01.WATER.WSP01.Power_kW", available=True, freshness=dht.FRESHNESS_FRESH,
    validity=dht.VALIDITY_VALID, continuity_applicable=True, has_gap=False,
    frozen_candidate=False, log_on_change=False, display_state=dht.TAG_DISPLAY_FRESH,
    last_value=50.0, last_time="2026-08-20 11:59:55", seconds_since_update=5.0,
    logging_interval_seconds=10, invalid_reason=None, gap_seconds=None,
    future_timestamp=False, non_monotonic=False,
) -> dhe.TagDataHealth:
    return dhe.TagDataHealth(
        tag_name=tag_name, available=available, last_value=last_value, last_time=last_time,
        seconds_since_update=seconds_since_update, freshness=freshness, validity=validity,
        invalid_reason=invalid_reason, continuity_applicable=continuity_applicable, has_gap=has_gap,
        gap_seconds=gap_seconds, frozen_candidate=frozen_candidate, log_on_change=log_on_change,
        logging_interval_seconds=logging_interval_seconds, display_state=display_state,
        future_timestamp=future_timestamp, non_monotonic=non_monotonic,
    )


def _result(
    confidence_score=91.0, confidence_status=dht.STATUS_GOOD, tags=None,
    component_scores=None, component_applicability=None,
    stale_tags=(), missing_tags=(), invalid_tags=(), indeterminate_freshness_tags=(),
    frozen_candidates=(), gaps=(), timestamp_issues=(), limitations=(), reasons=(),
    source=None, required_tag_count=5, available_tag_count=5, fresh_tag_count=5,
) -> dhe.EquipmentDataHealth:
    return dhe.EquipmentDataHealth(
        instance_key=INSTANCE_KEY, equipment_id=1, equipment_type="water_supply_pump",
        confidence_score=confidence_score, confidence_status=confidence_status,
        component_scores=component_scores or {"freshness": 100.0, "availability": 100.0, "validity": 100.0, "continuity": 100.0},
        component_applicability=component_applicability or {"freshness": True, "availability": True, "validity": True, "continuity": True},
        required_tag_count=required_tag_count, available_tag_count=available_tag_count, fresh_tag_count=fresh_tag_count,
        stale_tags=list(stale_tags), missing_tags=list(missing_tags), invalid_tags=list(invalid_tags),
        indeterminate_freshness_tags=list(indeterminate_freshness_tags), frozen_candidates=list(frozen_candidates),
        gaps=list(gaps), timestamp_issues=list(timestamp_issues),
        source=source if source is not None else {"configured_driver": "simulator", "note": "Reflects the currently CONFIGURED data source only - not a live connection-health check (no such signal exists in this system yet)."},
        tags=tags if tags is not None else [_tag()],
        limitations=list(limitations), reasons=list(reasons),
        model_version=dht.DATA_HEALTH_MODEL_VERSION, computed_at="2026-08-20 12:00:00",
    )


def _get(result: dhe.EquipmentDataHealth) -> dict:
    with patch.object(dhe, "calculate_equipment_data_health", return_value=result):
        return dhd.get_equipment_data_health(INSTANCE_KEY)


# ---------------------------------------------------------------------------
# 1-4: status rendering
# ---------------------------------------------------------------------------

class TestGoodConfidenceRenders(unittest.TestCase):
    def test_good_renders_score_and_label(self):
        adapted = _get(_result(confidence_score=91.0, confidence_status=dht.STATUS_GOOD))
        self.assertEqual(adapted["confidence_score_text"], "91 / 100")
        self.assertEqual(adapted["confidence_status_label"], "Good")


class TestDegradedConfidenceRenders(unittest.TestCase):
    def test_degraded_renders_score_and_label(self):
        adapted = _get(_result(confidence_score=70.0, confidence_status=dht.STATUS_DEGRADED))
        self.assertEqual(adapted["confidence_score_text"], "70 / 100")
        self.assertEqual(adapted["confidence_status_label"], "Degraded")


class TestPoorConfidenceRenders(unittest.TestCase):
    def test_poor_renders_score_and_label(self):
        adapted = _get(_result(confidence_score=30.0, confidence_status=dht.STATUS_POOR))
        self.assertEqual(adapted["confidence_score_text"], "30 / 100")
        self.assertEqual(adapted["confidence_status_label"], "Poor")


class TestUnavailableConfidenceRendersWithoutFakeScore(unittest.TestCase):
    def test_unavailable_shows_not_available_never_a_number(self):
        adapted = _get(_result(
            confidence_score=None, confidence_status=dht.STATUS_UNAVAILABLE, tags=[],
            component_scores={"freshness": None, "availability": None, "validity": None, "continuity": None},
            component_applicability={"freshness": False, "availability": False, "validity": False, "continuity": False},
            required_tag_count=0, available_tag_count=0, fresh_tag_count=0,
        ))
        self.assertEqual(adapted["confidence_score_text"], "Not Available")
        self.assertNotIn("0", adapted["confidence_score_text"])
        self.assertEqual(adapted["confidence_status_label"], "Unavailable")
        self.assertIsNone(adapted["confidence_score"])


# ---------------------------------------------------------------------------
# 5-6: component score / N/A mapping
# ---------------------------------------------------------------------------

class TestComponentScoresMapCorrectly(unittest.TestCase):
    def test_applicable_components_show_numeric_text(self):
        adapted = _get(_result(component_scores={"freshness": 80.0, "availability": 100.0, "validity": 60.0, "continuity": 40.0}))
        self.assertEqual(adapted["component_display"]["freshness"], "80")
        self.assertEqual(adapted["component_display"]["availability"], "100")
        self.assertEqual(adapted["component_display"]["validity"], "60")
        self.assertEqual(adapted["component_display"]["continuity"], "40")


class TestComponentNotApplicableMapsToNA(unittest.TestCase):
    def test_inapplicable_continuity_shows_na_not_a_fabricated_score(self):
        adapted = _get(_result(
            component_scores={"freshness": 90.0, "availability": 100.0, "validity": 100.0, "continuity": None},
            component_applicability={"freshness": True, "availability": True, "validity": True, "continuity": False},
        ))
        self.assertEqual(adapted["component_display"]["continuity"], "N/A")
        self.assertNotIn(adapted["component_display"]["continuity"], ("0", "100"))


# ---------------------------------------------------------------------------
# 7-9: stale / missing / invalid appear in detail+reasons
# ---------------------------------------------------------------------------

class TestStaleTagAppearsInDetail(unittest.TestCase):
    def test_stale_tag_listed(self):
        stale_tag = "P01.WATER.WSP01.BearingTemp"
        adapted = _get(_result(
            stale_tags=[stale_tag], reasons=["1 required tag is stale"],
            tags=[_tag(tag_name=stale_tag, freshness=dht.FRESHNESS_STALE, display_state=dht.TAG_DISPLAY_STALE)],
        ))
        self.assertIn(stale_tag, adapted["stale_tags"])
        self.assertTrue(any("stale" in r.lower() for r in adapted["reasons"]))
        tag_detail = next(t for t in adapted["tags"] if t["tag_name"] == stale_tag)
        self.assertEqual(tag_detail["freshness"], "Stale")


class TestMissingTagAppearsInDetail(unittest.TestCase):
    def test_missing_tag_listed(self):
        missing_tag = "P01.WATER.WSP01.Vibration"
        adapted = _get(_result(
            missing_tags=[missing_tag], reasons=["1 required tag has no historian data"],
            tags=[_tag(tag_name=missing_tag, available=False, freshness=None, validity=None, last_value=None, last_time=None, seconds_since_update=None, display_state=dht.TAG_DISPLAY_MISSING)],
        ))
        self.assertIn(missing_tag, adapted["missing_tags"])
        tag_detail = next(t for t in adapted["tags"] if t["tag_name"] == missing_tag)
        self.assertEqual(tag_detail["availability"], "Missing (never logged)")
        self.assertEqual(tag_detail["last_time"], "Never logged")


class TestInvalidTagAppearsInDetail(unittest.TestCase):
    def test_invalid_tag_listed(self):
        invalid_tag = "P01.WATER.WSP01.Power_kW"
        adapted = _get(_result(
            invalid_tags=[invalid_tag], reasons=["1 invalid value"],
            tags=[_tag(tag_name=invalid_tag, validity=dht.VALIDITY_INVALID, invalid_reason="value is NaN", display_state=dht.TAG_DISPLAY_INVALID)],
        ))
        self.assertIn(invalid_tag, adapted["invalid_tags"])
        tag_detail = next(t for t in adapted["tags"] if t["tag_name"] == invalid_tag)
        self.assertIn("Invalid", tag_detail["validity"])
        self.assertIn("NaN", tag_detail["validity"])


# ---------------------------------------------------------------------------
# 10-11: frozen candidate is advisory, never a confirmed-fault claim
# ---------------------------------------------------------------------------

class TestFrozenCandidateLabeledAdvisory(unittest.TestCase):
    def test_frozen_candidate_tag_labeled(self):
        frozen_tag = "P01.WATER.WSP01.BearingTemp"
        adapted = _get(_result(
            frozen_candidates=[frozen_tag],
            tags=[_tag(tag_name=frozen_tag, frozen_candidate=True, display_state=dht.TAG_DISPLAY_FROZEN_CANDIDATE)],
        ))
        self.assertIn(frozen_tag, adapted["frozen_candidates"])
        tag_detail = next(t for t in adapted["tags"] if t["tag_name"] == frozen_tag)
        self.assertIn("advisory", tag_detail["frozen_candidate"].lower())


class TestFrozenCandidateNeverCalledConfirmedFault(unittest.TestCase):
    FORBIDDEN = ("sensor failure", "sensor frozen", "faulty sensor", "confirmed fault")

    def test_no_forbidden_wording_anywhere_in_adapter_output(self):
        frozen_tag = "P01.WATER.WSP01.BearingTemp"
        adapted = _get(_result(
            frozen_candidates=[frozen_tag],
            tags=[_tag(tag_name=frozen_tag, frozen_candidate=True, display_state=dht.TAG_DISPLAY_FROZEN_CANDIDATE)],
        ))
        combined = " ".join([
            str(adapted["confidence_status_label"]),
            " ".join(adapted["reasons"]), " ".join(adapted["limitations"]),
            " ".join(t["frozen_candidate"] for t in adapted["tags"]),
        ]).lower()
        for forbidden in self.FORBIDDEN:
            self.assertNotIn(forbidden, combined)


# ---------------------------------------------------------------------------
# 12: log_on_change INDETERMINATE never displayed as STALE
# ---------------------------------------------------------------------------

class TestLogOnChangeIndeterminateNotDisplayedAsStale(unittest.TestCase):
    def test_indeterminate_freshness_label_is_not_stale(self):
        change_tag = "P01.WATER.WSP01.Frequency"
        adapted = _get(_result(
            indeterminate_freshness_tags=[change_tag],
            tags=[_tag(
                tag_name=change_tag, freshness=dht.FRESHNESS_INDETERMINATE, log_on_change=True,
                continuity_applicable=False, display_state=dht.TAG_DISPLAY_INDETERMINATE,
            )],
        ))
        tag_detail = next(t for t in adapted["tags"] if t["tag_name"] == change_tag)
        self.assertNotEqual(tag_detail["freshness"], "Stale")
        self.assertIn("Indeterminate", tag_detail["freshness"])
        self.assertNotIn(change_tag, adapted["stale_tags"])


# ---------------------------------------------------------------------------
# 13-14: source/provenance wording
# ---------------------------------------------------------------------------

class TestConfiguredPlcSourceNeverClaimsConnected(unittest.TestCase):
    FORBIDDEN = ("plc connected", "plc online", "connection healthy", "connected", "online")

    def test_opcua_source_wording_is_safe(self):
        adapted = _get(_result(source={
            "configured_driver": "opcua",
            "note": "Reflects the currently CONFIGURED data source only - not a live connection-health check (no such signal exists in this system yet).",
        }))
        self.assertEqual(adapted["source_label"], "OPC UA")
        combined = f"{adapted['source_label']} {adapted['source_note']}".lower()
        for forbidden in self.FORBIDDEN:
            self.assertNotIn(forbidden, combined)


class TestSimulatorSourceDisplaysCorrectly(unittest.TestCase):
    def test_simulator_label(self):
        adapted = _get(_result(source={"configured_driver": "simulator", "note": "x"}))
        self.assertEqual(adapted["source_label"], "Simulator")


# ---------------------------------------------------------------------------
# 15-16: safe handling of no-profile / malformed results
# ---------------------------------------------------------------------------

class TestEquipmentWithNoProfileHandledSafely(unittest.TestCase):
    def test_no_registry_result_renders_without_crashing(self):
        adapted = _get(_result(
            confidence_score=None, confidence_status=dht.STATUS_UNAVAILABLE, tags=[],
            component_scores={"freshness": None, "availability": None, "validity": None, "continuity": None},
            component_applicability={"freshness": False, "availability": False, "validity": False, "continuity": False},
            required_tag_count=0, available_tag_count=0, fresh_tag_count=0,
            limitations=["No Data Health telemetry registry is defined for equipment_type 'Generator'."],
        ))
        self.assertEqual(adapted["confidence_status"], dht.STATUS_UNAVAILABLE)
        self.assertEqual(adapted["tags"], [])
        self.assertTrue(adapted["limitations"])


class TestMalformedEngineResultCannotProduceGood(unittest.TestCase):
    def test_engine_exception_never_becomes_good(self):
        with patch.object(dhe, "calculate_equipment_data_health", side_effect=RuntimeError("database unreachable")):
            adapted = dhd.get_equipment_data_health(INSTANCE_KEY)
        self.assertEqual(adapted["confidence_status"], dht.STATUS_UNAVAILABLE)
        self.assertNotEqual(adapted["confidence_status"], dht.STATUS_GOOD)
        self.assertIsNone(adapted["confidence_score"])
        self.assertTrue(any("database unreachable" in lim for lim in adapted["limitations"]))

    def test_empty_instance_key_never_becomes_good(self):
        adapted = dhd.get_equipment_data_health("")
        self.assertEqual(adapted["confidence_status"], dht.STATUS_UNAVAILABLE)


# ---------------------------------------------------------------------------
# 17: adapter performs no Data Health calculation itself
# ---------------------------------------------------------------------------

class TestAdapterPerformsNoCalculation(unittest.TestCase):
    def test_adapter_result_is_exactly_what_the_engine_returned(self):
        """If the adapter were doing its own calculation, patching the
        engine's return value wouldn't fully determine the adapter's
        output - confirm every headline number is a direct pass-through."""
        engine_result = _result(confidence_score=42.0, confidence_status=dht.STATUS_POOR, required_tag_count=7, available_tag_count=3, fresh_tag_count=2)
        adapted = _get(engine_result)
        self.assertEqual(adapted["confidence_score"], 42.0)
        self.assertEqual(adapted["required_tag_count"], 7)
        self.assertEqual(adapted["available_tag_count"], 3)
        self.assertEqual(adapted["fresh_tag_count"], 2)

    def test_adapter_module_has_no_write_sql(self):
        """The adapter DOES call .execute() once, for a plain read-only
        SELECT (tag descriptions, presentation enrichment only) - this
        checks the SQL text itself never contains a write statement,
        rather than forbidding execute() outright."""
        adapter_path = Path(__file__).resolve().parent.parent / "ui" / "data_health_data.py"
        source_text = adapter_path.read_text().upper()
        for forbidden in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP TABLE", "ALTER TABLE", "CREATE TABLE"):
            self.assertNotIn(forbidden, source_text)

    def test_adapter_never_calls_the_engines_own_calculation_helpers_directly(self):
        """The adapter must go through the single public entry point
        (calculate_equipment_data_health) - never call the engine's
        internal per-tag classification helpers itself, which would be
        the adapter quietly re-implementing calculation logic."""
        adapter_path = Path(__file__).resolve().parent.parent / "ui" / "data_health_data.py"
        source_text = adapter_path.read_text()
        for internal_helper in ("_classify_validity", "_classify_freshness", "_detect_gap", "_detect_frozen_candidate"):
            self.assertNotIn(internal_helper, source_text)


# ---------------------------------------------------------------------------
# Real DB integration test (end-to-end, no mocking) - confirms the full
# adapter -> engine -> historian plumbing genuinely works, not just the
# mocked presentation-mapping tests above.
# ---------------------------------------------------------------------------

class TestRealDatabaseIntegration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_db = Path(self.temp_dir.name) / "config.db"
        self.machine_db = Path(self.temp_dir.name) / "machine_data.db"
        initialize_config_database(str(self.config_db))
        connection = sqlite3.connect(self.config_db)
        connection.execute("ALTER TABLE tags ADD COLUMN logging_interval_seconds INTEGER")
        connection.execute("ALTER TABLE tags ADD COLUMN log_on_change INTEGER")
        connection.execute("INSERT INTO equipment (name, display_name) VALUES ('p01_water_supply_pump_wsp01', 'Water Supply Pump WSP01 (P01)')")
        equipment_id = connection.execute("SELECT id FROM equipment").fetchone()[0]
        for suffix in ("Power_kW", "Flow", "Frequency", "Vibration", "BearingTemp"):
            connection.execute(
                "INSERT INTO tags (tag_name, equipment_id, driver, data_type, unit, enabled, description, logging_interval_seconds, log_on_change) "
                "VALUES (?, ?, 'simulator', 'REAL', '', 1, ?, 10, 0)",
                (f"{INSTANCE_KEY}.{suffix}", equipment_id, f"{suffix} description"),
            )
        connection.commit()
        connection.close()

        self.historian = DatabaseManager(db_path=self.machine_db)
        for suffix in ("Power_kW", "Flow", "Frequency", "Vibration", "BearingTemp"):
            self.historian.save_tag(f"{INSTANCE_KEY}.{suffix}", "sim", 50.0, BASE - timedelta(seconds=5))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_adapter_end_to_end_against_real_database(self):
        with patch.object(dhd, "CONFIG_DATABASE_PATH", self.config_db), \
             patch.object(dhd, "MACHINE_DATABASE_PATH", self.machine_db):
            adapted = dhd.get_equipment_data_health(INSTANCE_KEY, now=BASE)

        self.assertEqual(adapted["required_tag_count"], 5)
        self.assertEqual(adapted["available_tag_count"], 5)
        self.assertEqual(adapted["confidence_status"], dht.STATUS_GOOD)
        self.assertEqual(len(adapted["tags"]), 5)
        self.assertEqual(adapted["tags"][0]["description"], f"{adapted['tags'][0]['tag_name'].rsplit('.', 1)[-1]} description")


# ---------------------------------------------------------------------------
# 18: existing Equipment Health behavior unchanged - AppTest smoke test
# ---------------------------------------------------------------------------

class TestEquipmentHealthPageStillWorks(unittest.TestCase):
    def test_page_renders_without_exception_and_shows_both_sections(self):
        from streamlit.testing.v1 import AppTest

        page_path = str(Path(__file__).resolve().parent.parent / "ui" / "pages" / "19_Equipment_Health.py")
        at = AppTest.from_file(page_path, default_timeout=60)
        at.run()

        self.assertEqual(len(at.exception), 0, msg=f"page raised: {[e.value for e in at.exception]}")
        subheaders = [s.value for s in at.subheader]
        self.assertIn("Equipment detail", subheaders)
        self.assertIn("📡 Data Health", subheaders)
        metric_labels = [m.label for m in at.metric]
        self.assertIn("Data Confidence", metric_labels)
        self.assertIn("Health Score", metric_labels)  # existing Equipment Health metric, unchanged


if __name__ == "__main__":
    unittest.main()
