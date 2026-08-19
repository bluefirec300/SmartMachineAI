import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from database.database import DatabaseManager
from database.initialize_config_db import initialize_config_database
from engine import data_health_engine as dh
from engine import data_health_targets as t

"""
Phase 16.1 - Data Health engine test suite. Self-contained fixture (a
fresh temp config.db + machine_data.db per test), following the same
pattern already established by tests/test_baseline_engine.py.

CONTINUITY_LOOKBACK_HOURS is patched down to a small value for every
test in this file (via DataHealthTestBase) so gap/continuity fixtures
stay small and fast, without changing the real default used in
production - the patch targets engine.data_health_engine's own bound
name (it was imported by value), not the targets module, exactly the
way this kind of test-only override needs to work.
"""

BASE = datetime(2026, 8, 20, 12, 0, 0)
INSTANCE_KEY = "P01.WATER.WSP01"
EQUIPMENT_NAME = "p01_water_supply_pump_wsp01"
# water_supply_pump's real raw_targets, per engine.baseline_targets.EQUIPMENT_TYPE_PROFILES.
REQUIRED_SUFFIXES = ("Power_kW", "Flow", "Frequency", "Vibration", "BearingTemp")
REQUIRED_TAGS = tuple(f"{INSTANCE_KEY}.{suffix}" for suffix in REQUIRED_SUFFIXES)


def _seed_config_db(database_path: Path, equipment_name: str = EQUIPMENT_NAME, display_name: str = "Water Supply Pump WSP01 (P01)") -> None:
    initialize_config_database(str(database_path))
    connection = sqlite3.connect(database_path)
    # tags.logging_interval_seconds/log_on_change are owned live by
    # engine/tag_dataset_importer.py (confirmed by audit - not part of
    # the base 001_create_config.sql schema) - added directly here for
    # this self-contained fixture rather than running the full importer.
    connection.execute("ALTER TABLE tags ADD COLUMN logging_interval_seconds INTEGER")
    connection.execute("ALTER TABLE tags ADD COLUMN log_on_change INTEGER")
    connection.execute("INSERT INTO equipment (name, display_name) VALUES (?, ?)", (equipment_name, display_name))
    connection.commit()
    connection.close()


def _insert_tag(
    database_path: Path, tag_name: str, equipment_name: str = EQUIPMENT_NAME,
    data_type: str = "REAL", logging_interval_seconds: int | None = 10, log_on_change: int = 0,
) -> None:
    connection = sqlite3.connect(database_path)
    equipment_id = connection.execute("SELECT id FROM equipment WHERE name = ?", (equipment_name,)).fetchone()[0]
    connection.execute(
        "INSERT INTO tags (tag_name, equipment_id, driver, data_type, unit, enabled, logging_interval_seconds, log_on_change) "
        "VALUES (?, ?, 'simulator', ?, '', 1, ?, ?)",
        (tag_name, equipment_id, data_type, logging_interval_seconds, log_on_change),
    )
    connection.commit()
    connection.close()


def _seed_series(historian: DatabaseManager, tag: str, points: list[tuple[datetime, float]]) -> None:
    for timestamp, value in points:
        historian.save_tag(tag, "sim", value, timestamp)


def _regular_series(start: datetime, end: datetime, step_seconds: int, value: float = 50.0) -> list[tuple[datetime, float]]:
    points = []
    current = start
    while current <= end:
        points.append((current, value))
        current += timedelta(seconds=step_seconds)
    return points


class DataHealthTestBase(unittest.TestCase):
    LOOKBACK_HOURS = 0.05  # 180s - small, fast fixtures; patched in, real default untouched.

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_db = Path(self.temp_dir.name) / "config.db"
        self.machine_db = Path(self.temp_dir.name) / "machine_data.db"
        _seed_config_db(self.config_db)
        self.historian = DatabaseManager(db_path=self.machine_db)

        self._lookback_patch = patch.object(dh, "CONTINUITY_LOOKBACK_HOURS", self.LOOKBACK_HOURS)
        self._lookback_patch.start()

    def tearDown(self):
        self._lookback_patch.stop()
        self.temp_dir.cleanup()

    def _seed_all_required_tags_healthy(self, now: datetime, interval_seconds: int = 10, exclude: tuple[str, ...] = ()):
        """Seeds every required tag with fresh, regular, valid data - a
        tag named in `exclude` still gets its config row (so it remains
        a required tag), but NO plc_data rows, leaving the caller free
        to seed that one tag's own controlled series without an earlier
        "healthy" row remaining as the (more recent) latest reading."""
        window_start = now - timedelta(hours=self.LOOKBACK_HOURS)
        for tag_name in REQUIRED_TAGS:
            _insert_tag(self.config_db, tag_name, logging_interval_seconds=interval_seconds, log_on_change=0)
            if tag_name not in exclude:
                _seed_series(self.historian, tag_name, _regular_series(window_start, now, interval_seconds - 2, value=50.0))

    def _calculate(self, now: datetime) -> dh.EquipmentDataHealth:
        return dh.calculate_equipment_data_health(self.config_db, self.machine_db, INSTANCE_KEY, now=now)


# ---------------------------------------------------------------------------
# 1-3: all healthy / one stale / multiple stale
# ---------------------------------------------------------------------------

class TestAllTagsHealthy(DataHealthTestBase):
    def test_all_required_tags_fresh_valid_no_gaps_is_good(self):
        self._seed_all_required_tags_healthy(BASE)
        result = self._calculate(BASE)

        self.assertEqual(result.confidence_status, t.STATUS_GOOD)
        self.assertEqual(result.required_tag_count, 5)
        self.assertEqual(result.available_tag_count, 5)
        self.assertEqual(result.fresh_tag_count, 5)
        self.assertEqual(result.missing_tags, [])
        self.assertEqual(result.stale_tags, [])
        self.assertEqual(result.invalid_tags, [])
        self.assertEqual(result.gaps, [])
        self.assertEqual(result.component_scores["freshness"], 100.0)
        self.assertEqual(result.component_scores["availability"], 100.0)
        self.assertEqual(result.component_scores["validity"], 100.0)
        self.assertEqual(result.component_scores["continuity"], 100.0)
        self.assertEqual(result.confidence_score, 100.0)


class TestOneStaleTag(DataHealthTestBase):
    def test_one_stale_tag_reduces_freshness_only(self):
        stale_tag = f"{INSTANCE_KEY}.BearingTemp"
        self._seed_all_required_tags_healthy(BASE, exclude=(stale_tag,))
        window_start = BASE - timedelta(hours=self.LOOKBACK_HOURS)
        _seed_series(self.historian, stale_tag, _regular_series(BASE - timedelta(seconds=400), BASE - timedelta(seconds=200), 8, value=42.0))

        result = self._calculate(BASE)

        self.assertIn(stale_tag, result.stale_tags)
        self.assertEqual(len(result.stale_tags), 1)
        self.assertLess(result.component_scores["freshness"], 100.0)
        self.assertEqual(result.component_scores["availability"], 100.0)  # still available, just stale
        self.assertIn(result.confidence_status, (t.STATUS_GOOD, t.STATUS_DEGRADED))


class TestMultipleStaleTags(DataHealthTestBase):
    def test_multiple_stale_tags_reduce_freshness_more(self):
        stale_tags = [f"{INSTANCE_KEY}.BearingTemp", f"{INSTANCE_KEY}.Vibration", f"{INSTANCE_KEY}.Frequency"]
        self._seed_all_required_tags_healthy(BASE, exclude=tuple(stale_tags))
        window_start = BASE - timedelta(hours=self.LOOKBACK_HOURS)
        for tag_name in stale_tags:
            _seed_series(self.historian, tag_name, _regular_series(BASE - timedelta(seconds=400), BASE - timedelta(seconds=200), 8, value=1.0))

        result = self._calculate(BASE)

        self.assertEqual(sorted(result.stale_tags), sorted(stale_tags))
        # 2/5 fresh -> freshness component should be 40.0 (2*100/5).
        self.assertEqual(result.component_scores["freshness"], 40.0)


# ---------------------------------------------------------------------------
# 4: missing required tag
# ---------------------------------------------------------------------------

class TestMissingRequiredTag(DataHealthTestBase):
    def test_missing_required_tag_is_distinct_from_stale(self):
        self._seed_all_required_tags_healthy(BASE)
        missing_tag = f"{INSTANCE_KEY}.Vibration"
        # Remove every plc_data row for this tag - configured (required)
        # but has literally never reported.
        connection = sqlite3.connect(self.machine_db)
        connection.execute("DELETE FROM plc_data WHERE tag = ?", (missing_tag,))
        connection.commit()
        connection.close()

        result = self._calculate(BASE)

        self.assertIn(missing_tag, result.missing_tags)
        self.assertNotIn(missing_tag, result.stale_tags)
        self.assertEqual(result.available_tag_count, 4)
        self.assertEqual(result.component_scores["availability"], 80.0)  # 4/5, full denominator
        # Freshness/validity denominators must ALSO stay at the full
        # required_tag_count (item C - never silently shrunk).
        self.assertEqual(result.component_scores["freshness"], 80.0)
        self.assertEqual(result.component_scores["validity"], 80.0)


# ---------------------------------------------------------------------------
# 5-6: recent-but-invalid vs stale-but-valid (orthogonal dimensions,
# item B's core requirement).
# ---------------------------------------------------------------------------

class TestRecentButInvalidTag(DataHealthTestBase):
    def test_fresh_timestamp_but_invalid_value(self):
        tag_name = f"{INSTANCE_KEY}.Power_kW"
        self._seed_all_required_tags_healthy(BASE, exclude=(tag_name,))
        # A RECENT +inf sample (SQLite converts a bound NaN parameter to
        # NULL, which the NOT NULL plc_data.value column then rejects
        # outright - a real, confirmed consequence of this: a NaN
        # reading can never actually reach plc_data via the normal write
        # path, since app/plc_logger.py's per-tag save would raise and
        # be caught there first. +inf, unlike NaN, binds and stores
        # fine, and is an equally objective INVALID case - see
        # TestNaNValueIsInvalid below for the NaN check itself, done at
        # the pure-function level instead) - freshness must stay FRESH,
        # validity must independently become INVALID.
        _seed_series(self.historian, tag_name, [(BASE - timedelta(seconds=5), float("inf"))])

        result = self._calculate(BASE)

        tag_result = next(tg for tg in result.tags if tg.tag_name == tag_name)
        self.assertEqual(tag_result.freshness, t.FRESHNESS_FRESH)
        self.assertEqual(tag_result.validity, t.VALIDITY_INVALID)
        self.assertIn(tag_name, result.invalid_tags)
        self.assertNotIn(tag_name, result.stale_tags)


class TestStaleButValidTag(DataHealthTestBase):
    def test_stale_timestamp_but_last_known_value_valid(self):
        tag_name = f"{INSTANCE_KEY}.Flow"
        self._seed_all_required_tags_healthy(BASE, exclude=(tag_name,))
        window_start = BASE - timedelta(hours=self.LOOKBACK_HOURS)
        _seed_series(self.historian, tag_name, _regular_series(BASE - timedelta(seconds=400), BASE - timedelta(seconds=200), 8, value=33.3))

        result = self._calculate(BASE)

        tag_result = next(tg for tg in result.tags if tg.tag_name == tag_name)
        self.assertEqual(tag_result.freshness, t.FRESHNESS_STALE)
        self.assertEqual(tag_result.validity, t.VALIDITY_VALID)
        self.assertIn(tag_name, result.stale_tags)
        self.assertNotIn(tag_name, result.invalid_tags)


# ---------------------------------------------------------------------------
# 7-9: NaN / +inf / -inf (equipment-level, complements the RuleEngine
# unit tests in tests/test_rule_engine_nan_fix.py).
# ---------------------------------------------------------------------------

class TestNaNValidity(unittest.TestCase):
    def test_nan_value_is_invalid(self):
        """Pure function-level test (see the comment in
        TestRecentButInvalidTag above for why a real NaN cannot be
        round-tripped through plc_data - SQLite converts a bound NaN
        parameter to NULL, which the NOT NULL column then rejects)."""
        validity, reason = dh._classify_validity(float("nan"))
        self.assertEqual(validity, t.VALIDITY_INVALID)
        self.assertIn("NaN", reason)


class TestPositiveInfinityValidity(DataHealthTestBase):
    def test_positive_infinity_is_invalid(self):
        tag_name = f"{INSTANCE_KEY}.Power_kW"
        self._seed_all_required_tags_healthy(BASE, exclude=(tag_name,))
        _seed_series(self.historian, tag_name, [(BASE - timedelta(seconds=5), float("inf"))])
        result = self._calculate(BASE)
        self.assertIn(tag_name, result.invalid_tags)


class TestNegativeInfinityValidity(DataHealthTestBase):
    def test_negative_infinity_is_invalid(self):
        tag_name = f"{INSTANCE_KEY}.Power_kW"
        self._seed_all_required_tags_healthy(BASE, exclude=(tag_name,))
        _seed_series(self.historian, tag_name, [(BASE - timedelta(seconds=5), float("-inf"))])
        result = self._calculate(BASE)
        self.assertIn(tag_name, result.invalid_tags)


# ---------------------------------------------------------------------------
# 10: alarm-worthy but legitimate reading remains VALID data.
# ---------------------------------------------------------------------------

class TestAlarmWorthyValueRemainsValid(DataHealthTestBase):
    def test_extreme_but_finite_reading_is_not_invalidated(self):
        """Item E's hard rule: no configured physical-validity-range
        concept exists in this project, so a wildly abnormal (alarm-
        worthy) but finite numeric reading must NOT be treated as
        invalid data - only objective non-finiteness is ever checked."""
        self._seed_all_required_tags_healthy(BASE)
        tag_name = f"{INSTANCE_KEY}.Power_kW"
        _seed_series(self.historian, tag_name, [(BASE - timedelta(seconds=5), 999999.0)])
        result = self._calculate(BASE)
        self.assertNotIn(tag_name, result.invalid_tags)


# ---------------------------------------------------------------------------
# 11: future timestamp
# ---------------------------------------------------------------------------

class TestFutureTimestamp(DataHealthTestBase):
    def test_future_timestamp_is_flagged(self):
        self._seed_all_required_tags_healthy(BASE)
        tag_name = f"{INSTANCE_KEY}.Power_kW"
        _seed_series(self.historian, tag_name, [(BASE + timedelta(minutes=10), 50.0)])

        result = self._calculate(BASE)

        issue = next((i for i in result.timestamp_issues if i["tag"] == tag_name), None)
        self.assertIsNotNone(issue)
        self.assertTrue(issue["future_timestamp"])

    def test_tiny_clock_skew_within_tolerance_is_not_flagged(self):
        self._seed_all_required_tags_healthy(BASE)
        tag_name = f"{INSTANCE_KEY}.Power_kW"
        _seed_series(self.historian, tag_name, [(BASE + timedelta(seconds=1), 50.0)])

        result = self._calculate(BASE)

        issue = next((i for i in result.timestamp_issues if i["tag"] == tag_name and i["future_timestamp"]), None)
        self.assertIsNone(issue)


# ---------------------------------------------------------------------------
# 12: historical gap
# ---------------------------------------------------------------------------

class TestHistoricalGap(DataHealthTestBase):
    def test_gap_within_lookback_window_is_detected(self):
        tag_name = f"{INSTANCE_KEY}.Flow"
        self._seed_all_required_tags_healthy(BASE, exclude=(tag_name,))
        window_start = BASE - timedelta(hours=self.LOOKBACK_HOURS)
        # Data before AND after a long silent stretch, but the tag's
        # LATEST sample is still recent enough to read FRESH - the gap
        # is an internal continuity problem, distinct from staleness.
        points = (
            _regular_series(window_start, window_start + timedelta(seconds=20), 8, value=10.0)
            + _regular_series(BASE - timedelta(seconds=10), BASE, 5, value=10.0)
        )
        _seed_series(self.historian, tag_name, points)

        result = self._calculate(BASE)

        gap = next((g for g in result.gaps if g["tag"] == tag_name), None)
        self.assertIsNotNone(gap)
        self.assertGreater(gap["gap_seconds"], 0)


# ---------------------------------------------------------------------------
# 13-16: frozen/stuck detection - advisory only, never scored (item G).
# ---------------------------------------------------------------------------

class TestLegitimateConstantLogOnChangeTagNotFrozen(DataHealthTestBase):
    def test_log_on_change_tag_held_constant_is_not_frozen(self):
        self._seed_all_required_tags_healthy(BASE)
        tag_name = f"{INSTANCE_KEY}.Frequency"
        # Reconfigure as log_on_change and hold one value for a long
        # time - legitimate behavior for a change-only tag.
        connection = sqlite3.connect(self.config_db)
        connection.execute("UPDATE tags SET log_on_change = 1, logging_interval_seconds = NULL WHERE tag_name = ?", (tag_name,))
        connection.commit()
        connection.close()
        _seed_series(self.historian, tag_name, [(BASE - timedelta(seconds=30), 60.0)])

        result = self._calculate(BASE)

        self.assertNotIn(tag_name, result.frozen_candidates)
        tag_result = next(tg for tg in result.tags if tg.tag_name == tag_name)
        self.assertEqual(tag_result.freshness, t.FRESHNESS_INDETERMINATE)
        self.assertIn(tag_name, result.indeterminate_freshness_tags)


class TestLegitimateDiscreteStatusTagNotFrozen(DataHealthTestBase):
    def test_bool_data_type_tag_is_never_frozen_candidate(self):
        self._seed_all_required_tags_healthy(BASE)
        tag_name = f"{INSTANCE_KEY}.Frequency"
        connection = sqlite3.connect(self.config_db)
        connection.execute("UPDATE tags SET data_type = 'BOOL', log_on_change = 0, logging_interval_seconds = 10 WHERE tag_name = ?", (tag_name,))
        connection.commit()
        connection.close()
        window_start = BASE - timedelta(hours=self.LOOKBACK_HOURS)
        _seed_series(self.historian, tag_name, _regular_series(window_start, BASE, 8, value=1.0))

        result = self._calculate(BASE)

        self.assertNotIn(tag_name, result.frozen_candidates)


class TestContinuousSensorFrozenWithSiblingUpdating(DataHealthTestBase):
    def test_one_frozen_sensor_with_fresh_siblings_is_flagged_candidate(self):
        tag_name = f"{INSTANCE_KEY}.BearingTemp"
        self._seed_all_required_tags_healthy(BASE, exclude=(tag_name,))
        # Overwrite with FROZEN_MIN_CONSECUTIVE_SAMPLES identical values
        # spanning well over FROZEN_MIN_WINDOW_INTERVAL_MULTIPLIER x the
        # 10s interval (100s) - siblings (seeded healthy above) remain
        # fresh and varying.
        window_start = BASE - timedelta(seconds=150)
        points = [(window_start + timedelta(seconds=25 * i), 42.0) for i in range(t.FROZEN_MIN_CONSECUTIVE_SAMPLES)]
        _seed_series(self.historian, tag_name, points)

        result = self._calculate(BASE)

        self.assertIn(tag_name, result.frozen_candidates)
        # Advisory only - must never be scored (item G).
        self.assertEqual(result.component_scores["freshness"], 100.0)


class TestWholeEquipmentStopsUpdatingIsNotFrozenDiagnosis(DataHealthTestBase):
    def test_all_tags_stale_together_suppresses_frozen_candidacy(self):
        # Last sample well beyond the 10s-interval x 5 freshness
        # threshold (50s), with FROZEN_MIN_CONSECUTIVE_SAMPLES identical
        # values spaced 25s apart before that - looks exactly like the
        # frozen-candidate raw evidence, but for EVERY required tag at
        # once (the whole equipment/source going quiet together).
        last_sample_time = BASE - timedelta(seconds=60)
        window_start = last_sample_time - timedelta(seconds=25 * (t.FROZEN_MIN_CONSECUTIVE_SAMPLES - 1))
        for tag_name in REQUIRED_TAGS:
            _insert_tag(self.config_db, tag_name, logging_interval_seconds=10, log_on_change=0)
            points = [(window_start + timedelta(seconds=25 * i), 7.0) for i in range(t.FROZEN_MIN_CONSECUTIVE_SAMPLES)]
            _seed_series(self.historian, tag_name, points)

        result = self._calculate(BASE)

        self.assertEqual(result.frozen_candidates, [])
        self.assertEqual(len(result.stale_tags), 5)
        self.assertTrue(any("suppressed" in limitation for limitation in result.limitations))


# ---------------------------------------------------------------------------
# 17: recovery when fresh data resumes
# ---------------------------------------------------------------------------

class TestRecoveryWhenFreshDataResumes(DataHealthTestBase):
    def test_confidence_recovers_once_fresh_data_resumes(self):
        tag_name = f"{INSTANCE_KEY}.Vibration"
        self._seed_all_required_tags_healthy(BASE, exclude=(tag_name,))
        window_start = BASE - timedelta(hours=self.LOOKBACK_HOURS)

        # First: simulate the tag having been stale as of an earlier "now".
        _seed_series(self.historian, tag_name, _regular_series(BASE - timedelta(seconds=400), BASE - timedelta(seconds=200), 8, value=5.0))
        earlier_result = self._calculate(BASE)
        self.assertIn(tag_name, earlier_result.stale_tags)

        # Then: fresh data resumes - a LATER "now", with a new recent sample.
        later_now = BASE + timedelta(seconds=30)
        _seed_series(self.historian, tag_name, [(later_now - timedelta(seconds=5), 5.5)])
        later_result = self._calculate(later_now)

        self.assertNotIn(tag_name, later_result.stale_tags)


# ---------------------------------------------------------------------------
# 18: no required telemetry evidence -> UNAVAILABLE / score=None
# ---------------------------------------------------------------------------

class TestNoRequiredTelemetryRegistry(DataHealthTestBase):
    def test_unprofiled_equipment_type_is_unavailable(self):
        """An instance whose type has NO engine.baseline_targets profile
        at all (e.g. a generator) - never a fabricated score."""
        result = dh.calculate_equipment_data_health(self.config_db, self.machine_db, "P01.ELEC.GEN01", now=BASE)
        self.assertIsNone(result.confidence_score)
        self.assertEqual(result.confidence_status, t.STATUS_UNAVAILABLE)
        self.assertTrue(any("no applicable" in r.lower() or "not covered" in lim.lower() for r, lim in [(x, y) for x in result.reasons for y in result.limitations]))


class TestNoDataAtAllIsUnavailable(DataHealthTestBase):
    def test_zero_available_required_tags_is_unavailable_never_zero(self):
        for tag_name in REQUIRED_TAGS:
            _insert_tag(self.config_db, tag_name, logging_interval_seconds=10, log_on_change=0)
        # No plc_data rows inserted at all for any required tag.

        result = self._calculate(BASE)

        self.assertIsNone(result.confidence_score)
        self.assertEqual(result.confidence_status, t.STATUS_UNAVAILABLE)
        self.assertEqual(result.available_tag_count, 0)


class TestProfileExistsButNoTagsForInstance(DataHealthTestBase):
    def test_profile_exists_but_zero_tags_configured_for_this_instance(self):
        # No tags at all inserted for INSTANCE_KEY, but its equipment_type
        # ("water_supply_pump") DOES have a registry profile.
        result = self._calculate(BASE)
        self.assertIsNone(result.confidence_score)
        self.assertEqual(result.confidence_status, t.STATUS_UNAVAILABLE)
        self.assertEqual(result.required_tag_count, 0)


# ---------------------------------------------------------------------------
# 19-20: source identification, without a connection-health claim
# ---------------------------------------------------------------------------

class TestSimulatorSourceIdentification(DataHealthTestBase):
    def test_simulator_source_reported(self):
        self._seed_all_required_tags_healthy(BASE)
        with patch("plc.driver_factory.resolve_driver_name", return_value="simulator"):
            result = self._calculate(BASE)
        self.assertEqual(result.source["configured_driver"], "simulator")


class TestPLCSourceIdentificationWithoutHealthClaim(DataHealthTestBase):
    def test_plc_source_reported_without_connected_or_healthy_language(self):
        self._seed_all_required_tags_healthy(BASE)
        with patch("plc.driver_factory.resolve_driver_name", return_value="opcua"):
            result = self._calculate(BASE)
        self.assertEqual(result.source["configured_driver"], "opcua")
        combined_text = " ".join([result.source.get("note", "")]).upper()
        for forbidden in ("CONNECTED", "HEALTHY CONNECTION", "PLC ONLINE", "ONLINE"):
            self.assertNotIn(forbidden, combined_text)


# ---------------------------------------------------------------------------
# Formula / denominator / no-aggregate-score-hiding tests
# ---------------------------------------------------------------------------

class TestConfidenceFormulaArithmetic(DataHealthTestBase):
    def test_weighted_formula_matches_component_scores(self):
        self._seed_all_required_tags_healthy(BASE)
        stale_tag = f"{INSTANCE_KEY}.BearingTemp"
        window_start = BASE - timedelta(hours=self.LOOKBACK_HOURS)
        _seed_series(self.historian, stale_tag, _regular_series(BASE - timedelta(seconds=400), BASE - timedelta(seconds=200), 8, value=1.0))

        result = self._calculate(BASE)

        expected = (
            t.COMPONENT_WEIGHT_FRESHNESS * result.component_scores["freshness"]
            + t.COMPONENT_WEIGHT_AVAILABILITY * result.component_scores["availability"]
            + t.COMPONENT_WEIGHT_VALIDITY * result.component_scores["validity"]
            + t.COMPONENT_WEIGHT_CONTINUITY * result.component_scores["continuity"]
        )
        self.assertAlmostEqual(result.confidence_score, round(expected, 1), places=1)

    def test_status_bands_match_score(self):
        self.assertEqual(t.confidence_status_for_score(90.0), t.STATUS_GOOD)
        self.assertEqual(t.confidence_status_for_score(85.0), t.STATUS_GOOD)
        self.assertEqual(t.confidence_status_for_score(84.9), t.STATUS_DEGRADED)
        self.assertEqual(t.confidence_status_for_score(60.0), t.STATUS_DEGRADED)
        self.assertEqual(t.confidence_status_for_score(59.9), t.STATUS_POOR)
        self.assertEqual(t.confidence_status_for_score(1.0), t.STATUS_POOR)
        self.assertEqual(t.confidence_status_for_score(None), t.STATUS_UNAVAILABLE)


class TestContinuityInapplicableReweights(DataHealthTestBase):
    def test_all_log_on_change_required_tags_makes_continuity_inapplicable(self):
        for tag_name in REQUIRED_TAGS:
            _insert_tag(self.config_db, tag_name, logging_interval_seconds=None, log_on_change=1)
            _seed_series(self.historian, tag_name, [(BASE - timedelta(seconds=5), 10.0)])

        result = self._calculate(BASE)

        self.assertFalse(result.component_applicability["continuity"])
        self.assertIsNone(result.component_scores["continuity"])
        # Score still computable (reweighted across the other 3
        # components), never None just because one component doesn't apply.
        self.assertIsNotNone(result.confidence_score)


if __name__ == "__main__":
    unittest.main()
