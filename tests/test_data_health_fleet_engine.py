import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from config.environment import get_config_db_path, get_machine_db_path
from engine import data_health_engine as dhe
from engine import data_health_fleet as fleet
from engine import data_health_targets as dht

"""
Phase 16.4 - fleet Data Health aggregation engine tests. Two styles,
matching this project's own convention:

- Aggregation-CORRECTNESS tests use a controlled set of hand-built
  EquipmentDataHealth results (patched directly into
  engine.data_health_fleet.calculate_equipment_data_health and
  discover_health_targets) - so the fleet-layer aggregation logic is
  tested in isolation from the separately-tested Phase 16.1 engine's
  own calculation (item 7's "the fleet layer aggregates accepted
  per-equipment results, it does not recalculate them").
- A smaller set of tests run against the REAL live simulation database
  (matching tests/test_phase15_context_builder.py's own pattern) to
  confirm the real end-to-end wiring (discover_health_targets +
  calculate_equipment_data_health + hierarchy enrichment) actually works.
"""

CONFIG_DB = get_config_db_path()
MACHINE_DB = get_machine_db_path()


def _tag(tag_name, **overrides):
    fields = dict(
        tag_name=tag_name, available=True, last_value=1.0, last_time="2026-08-20 12:00:00",
        freshness=dht.FRESHNESS_FRESH, validity=dht.VALIDITY_VALID, log_on_change=False,
        display_state=dht.TAG_DISPLAY_FRESH,
    )
    fields.update(overrides)
    return dhe.TagDataHealth(**fields)


def _result(instance_key, equipment_id, equipment_type, status, score, **overrides):
    fields = dict(
        instance_key=instance_key, equipment_id=equipment_id, equipment_type=equipment_type,
        confidence_score=score, confidence_status=status,
        component_scores={"freshness": score, "availability": score, "validity": score, "continuity": score},
        component_applicability={"freshness": True, "availability": True, "validity": True, "continuity": True},
        required_tag_count=2, available_tag_count=2, fresh_tag_count=2,
        source={"configured_driver": "simulator"},
        tags=[_tag(f"{instance_key}.TagA"), _tag(f"{instance_key}.TagB")],
        computed_at="2026-08-20 12:00:00",
    )
    fields.update(overrides)
    return dhe.EquipmentDataHealth(**fields)


class TestFleetAggregationCorrectness(unittest.TestCase):
    """Controlled fixture: 4 equipment across 2 plants/areas/types, one
    of each status, so every count is independently checkable."""

    PAIRS = [
        ("water_supply_pump", "P01.WATER.WSP01"),
        ("chiller", "P01.UTILITY.CHL01"),
        ("air_compressor", "P02.UTILITY.AC01"),
        ("ahu", "P02.HVAC.AHU01"),
    ]

    RESULTS = {
        "P01.WATER.WSP01": _result(
            "P01.WATER.WSP01", 1, "water_supply_pump", dht.STATUS_GOOD, 95.0,
        ),
        "P01.UTILITY.CHL01": _result(
            "P01.UTILITY.CHL01", 2, "chiller", dht.STATUS_DEGRADED, 70.0,
            missing_tags=["P01.UTILITY.CHL01.TagA"],
            tags=[_tag("P01.UTILITY.CHL01.TagA", available=False, freshness=None, validity=None, display_state=dht.TAG_DISPLAY_MISSING),
                  _tag("P01.UTILITY.CHL01.TagB")],
        ),
        "P02.UTILITY.AC01": _result(
            "P02.UTILITY.AC01", 3, "air_compressor", dht.STATUS_POOR, 40.0,
            stale_tags=["P02.UTILITY.AC01.TagA"], invalid_tags=["P02.UTILITY.AC01.TagB"],
            tags=[_tag("P02.UTILITY.AC01.TagA", freshness=dht.FRESHNESS_STALE, display_state=dht.TAG_DISPLAY_STALE),
                  _tag("P02.UTILITY.AC01.TagB", validity=dht.VALIDITY_INVALID, invalid_reason="value is NaN", display_state=dht.TAG_DISPLAY_INVALID)],
        ),
        "P02.HVAC.AHU01": _result(
            "P02.HVAC.AHU01", 4, "ahu", dht.STATUS_UNAVAILABLE, None,
            required_tag_count=0, available_tag_count=0, fresh_tag_count=0, tags=[],
        ),
    }

    HIERARCHY = {
        1: {"equipment_id": 1, "display_name": "Water Supply Pump 01", "plant_code": "p01", "area_name": "Water", "system_name": "Water Supply"},
        2: {"equipment_id": 2, "display_name": "Chiller 01", "plant_code": "p01", "area_name": "Utility", "system_name": "Chilled Water"},
        3: {"equipment_id": 3, "display_name": "Air Compressor 01", "plant_code": "p02", "area_name": "Utility", "system_name": "Compressed Air"},
        4: {"equipment_id": 4, "display_name": "AHU 01", "plant_code": "p02", "area_name": "HVAC", "system_name": "HVAC"},
    }

    def _run(self):
        def fake_discover(config_db, plant_code):
            return [p for p in self.PAIRS if p[1].startswith(plant_code.upper())]

        def fake_calculate(config_db, machine_db, instance_key, now=None):
            return self.RESULTS[instance_key]

        with patch("engine.data_health_fleet.discover_health_targets", side_effect=fake_discover), \
             patch("engine.data_health_fleet.calculate_equipment_data_health", side_effect=fake_calculate), \
             patch("engine.data_health_fleet._plant_codes", return_value=["p01", "p02"]), \
             patch("engine.data_health_fleet._equipment_hierarchy", return_value=self.HIERARCHY):
            return fleet.calculate_fleet_data_health("fake_config.db", "fake_machine.db")

    def test_includes_all_eligible_equipment(self):
        result = self._run()
        self.assertEqual(result.equipment_count, 4)
        self.assertEqual({r["instance_key"] for r in result.equipment}, {p[1] for p in self.PAIRS})

    def test_per_equipment_scores_are_never_recalculated(self):
        """The fleet layer must pass through the authoritative per-
        equipment score/status/component_scores UNCHANGED - never
        recompute Freshness/Availability/Validity/Continuity itself."""
        result = self._run()
        wsp01 = next(r for r in result.equipment if r["instance_key"] == "P01.WATER.WSP01")
        self.assertEqual(wsp01["confidence_score"], 95.0)
        self.assertEqual(wsp01["confidence_status"], dht.STATUS_GOOD)
        self.assertEqual(wsp01["component_scores"], {"freshness": 95.0, "availability": 95.0, "validity": 95.0, "continuity": 95.0})

    def test_good_degraded_poor_unavailable_counts_correct(self):
        result = self._run()
        self.assertEqual(result.good_count, 1)
        self.assertEqual(result.degraded_count, 1)
        self.assertEqual(result.poor_count, 1)
        self.assertEqual(result.unavailable_count, 1)

    def test_status_counts_reconcile_to_fleet_total(self):
        result = self._run()
        self.assertEqual(
            result.good_count + result.degraded_count + result.poor_count + result.unavailable_count,
            result.equipment_count,
        )

    def test_assessed_count_excludes_unavailable(self):
        result = self._run()
        self.assertEqual(result.assessed_count, 3)

    def test_grouped_plant_counts_correct(self):
        result = self._run()
        self.assertEqual(result.by_plant["p01"]["equipment"], 2)
        self.assertEqual(result.by_plant["p01"]["GOOD"], 1)
        self.assertEqual(result.by_plant["p01"]["DEGRADED"], 1)
        self.assertEqual(result.by_plant["p02"]["equipment"], 2)
        self.assertEqual(result.by_plant["p02"]["POOR"], 1)
        self.assertEqual(result.by_plant["p02"]["UNAVAILABLE"], 1)

    def test_grouped_area_counts_correct(self):
        result = self._run()
        self.assertEqual(result.by_area["Water"]["equipment"], 1)
        self.assertEqual(result.by_area["Utility"]["equipment"], 2)
        self.assertEqual(result.by_area["HVAC"]["equipment"], 1)

    def test_grouped_system_counts_correct(self):
        result = self._run()
        self.assertEqual(result.by_system["Water Supply"]["equipment"], 1)
        self.assertEqual(result.by_system["Chilled Water"]["equipment"], 1)
        self.assertEqual(result.by_system["Compressed Air"]["equipment"], 1)

    def test_grouped_equipment_type_counts_correct(self):
        result = self._run()
        self.assertEqual(set(result.by_equipment_type), {"water_supply_pump", "chiller", "air_compressor", "ahu"})
        self.assertEqual(result.by_equipment_type["chiller"]["DEGRADED"], 1)

    def test_missing_stale_invalid_gap_issue_counts_correct(self):
        result = self._run()
        self.assertEqual(result.missing_tag_count, 1)
        self.assertEqual(result.stale_tag_count, 1)
        self.assertEqual(result.invalid_tag_count, 1)
        self.assertEqual(result.gap_count, 0)

    def test_unavailable_confidence_score_remains_none_not_zero(self):
        result = self._run()
        ahu = next(r for r in result.equipment if r["instance_key"] == "P02.HVAC.AHU01")
        self.assertIsNone(ahu["confidence_score"])
        self.assertNotEqual(ahu["confidence_score"], 0)

    def test_attention_ordering_worst_first(self):
        result = self._run()
        statuses_in_order = [r["confidence_status"] for r in result.equipment]
        self.assertEqual(
            statuses_in_order,
            [dht.STATUS_UNAVAILABLE, dht.STATUS_POOR, dht.STATUS_DEGRADED, dht.STATUS_GOOD],
        )

    def test_no_hidden_aggregate_factory_score_field(self):
        result = self._run()
        for forbidden in ("factory_score", "overall_score", "risk_score", "combined_score", "data_risk_score", "plant_ai_score"):
            self.assertFalse(hasattr(result, forbidden))

    def test_issue_rows_include_all_expected_types(self):
        result = self._run()
        issue_types = {i["issue"] for i in result.issues}
        self.assertIn("MISSING", issue_types)
        self.assertIn("STALE", issue_types)
        self.assertIn("INVALID", issue_types)

    def test_stale_issue_never_generated_for_indeterminate_freshness_tag(self):
        """log_on_change tags must never appear as a STALE issue row -
        they get their own INDETERMINATE_CHANGE_ONLY issue type."""
        indeterminate_result = _result(
            "P01.WATER.WSP02", 5, "water_supply_pump", dht.STATUS_GOOD, 90.0,
            tags=[_tag("P01.WATER.WSP02.AlarmState", freshness=dht.FRESHNESS_INDETERMINATE, log_on_change=True, display_state=dht.TAG_DISPLAY_INDETERMINATE)],
        )

        def fake_discover(config_db, plant_code):
            return [("water_supply_pump", "P01.WATER.WSP02")]

        with patch("engine.data_health_fleet.discover_health_targets", side_effect=fake_discover), \
             patch("engine.data_health_fleet.calculate_equipment_data_health", return_value=indeterminate_result), \
             patch("engine.data_health_fleet._plant_codes", return_value=["p01"]), \
             patch("engine.data_health_fleet._equipment_hierarchy", return_value={5: {"equipment_id": 5, "display_name": "WSP02", "plant_code": "p01", "area_name": "Water", "system_name": "Water Supply"}}):
            result = fleet.calculate_fleet_data_health("fake_config.db", "fake_machine.db")

        issue_types = {i["issue"] for i in result.issues}
        self.assertIn("INDETERMINATE_CHANGE_ONLY", issue_types)
        self.assertNotIn("STALE", issue_types)

    def test_frozen_candidate_issue_wording_never_implies_confirmed_failure(self):
        frozen_result = _result(
            "P01.WATER.WSP03", 6, "water_supply_pump", dht.STATUS_GOOD, 90.0,
            frozen_candidates=["P01.WATER.WSP03.Pressure"],
            tags=[_tag("P01.WATER.WSP03.Pressure", frozen_candidate=True, frozen_reason="6 identical readings", display_state=dht.TAG_DISPLAY_FROZEN_CANDIDATE)],
        )

        def fake_discover(config_db, plant_code):
            return [("water_supply_pump", "P01.WATER.WSP03")]

        with patch("engine.data_health_fleet.discover_health_targets", side_effect=fake_discover), \
             patch("engine.data_health_fleet.calculate_equipment_data_health", return_value=frozen_result), \
             patch("engine.data_health_fleet._plant_codes", return_value=["p01"]), \
             patch("engine.data_health_fleet._equipment_hierarchy", return_value={6: {"equipment_id": 6, "display_name": "WSP03", "plant_code": "p01", "area_name": "Water", "system_name": "Water Supply"}}):
            result = fleet.calculate_fleet_data_health("fake_config.db", "fake_machine.db")

        frozen_issues = [i for i in result.issues if i["issue"] == "FROZEN_CANDIDATE"]
        self.assertEqual(len(frozen_issues), 1)
        for forbidden_word in ("failed", "failure", "fault", "broken"):
            self.assertNotIn(forbidden_word, frozen_issues[0]["issue"].lower())


class TestFailureIsolation(unittest.TestCase):
    def test_one_equipment_exception_does_not_fail_fleet_evaluation(self):
        def fake_discover(config_db, plant_code):
            return [("water_supply_pump", "P01.WATER.WSP01"), ("chiller", "P01.UTILITY.CHL01")]

        def fake_calculate(config_db, machine_db, instance_key, now=None):
            if instance_key == "P01.WATER.WSP01":
                raise RuntimeError("simulated failure")
            return _result("P01.UTILITY.CHL01", 2, "chiller", dht.STATUS_GOOD, 90.0)

        with patch("engine.data_health_fleet.discover_health_targets", side_effect=fake_discover), \
             patch("engine.data_health_fleet.calculate_equipment_data_health", side_effect=fake_calculate), \
             patch("engine.data_health_fleet._plant_codes", return_value=["p01"]), \
             patch("engine.data_health_fleet._equipment_hierarchy", return_value={2: {"equipment_id": 2, "display_name": "CHL01", "plant_code": "p01", "area_name": "Utility", "system_name": "Chilled Water"}}):
            result = fleet.calculate_fleet_data_health("fake_config.db", "fake_machine.db")

        self.assertEqual(result.equipment_count, 2)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.failures[0]["instance_key"], "P01.WATER.WSP01")

    def test_failed_equipment_appears_as_unavailable_never_good(self):
        def fake_discover(config_db, plant_code):
            return [("water_supply_pump", "P01.WATER.WSP01")]

        def fake_calculate(config_db, machine_db, instance_key, now=None):
            raise RuntimeError("db unreachable")

        with patch("engine.data_health_fleet.discover_health_targets", side_effect=fake_discover), \
             patch("engine.data_health_fleet.calculate_equipment_data_health", side_effect=fake_calculate), \
             patch("engine.data_health_fleet._plant_codes", return_value=["p01"]), \
             patch("engine.data_health_fleet._equipment_hierarchy", return_value={}):
            result = fleet.calculate_fleet_data_health("fake_config.db", "fake_machine.db")

        row = result.equipment[0]
        self.assertEqual(row["confidence_status"], dht.STATUS_UNAVAILABLE)
        self.assertNotEqual(row["confidence_status"], dht.STATUS_GOOD)
        self.assertEqual(row["evaluation_error"], "db unreachable")


class TestNoScadaIntegration(unittest.TestCase):
    def test_scada_snapshot_writer_does_not_import_data_health(self):
        source = Path("ui/scada_snapshot_writer.py").read_text()
        self.assertNotIn("data_health", source.lower())


class TestRealLiveDatabase(unittest.TestCase):
    """Confirms the real end-to-end wiring works, matching every prior
    phase's own 'test against real data' discipline."""

    def test_real_fleet_evaluation_includes_expected_equipment_count(self):
        result = fleet.calculate_fleet_data_health(CONFIG_DB, MACHINE_DB)
        self.assertGreater(result.equipment_count, 0)
        self.assertEqual(
            result.good_count + result.degraded_count + result.poor_count + result.unavailable_count,
            result.equipment_count,
        )

    def test_real_fleet_evaluation_never_mutates_the_database(self):
        connection = sqlite3.connect(CONFIG_DB)
        try:
            before = connection.execute("SELECT COUNT(*) FROM equipment").fetchone()[0]
        finally:
            connection.close()

        fleet.calculate_fleet_data_health(CONFIG_DB, MACHINE_DB)

        connection = sqlite3.connect(CONFIG_DB)
        try:
            after = connection.execute("SELECT COUNT(*) FROM equipment").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(before, after)

    def test_real_fleet_hierarchy_enrichment_populates_plant_code(self):
        result = fleet.calculate_fleet_data_health(CONFIG_DB, MACHINE_DB)
        for row in result.equipment:
            self.assertTrue(row["plant_code"])

    def test_real_single_plant_filter_returns_subset(self):
        both = fleet.calculate_fleet_data_health(CONFIG_DB, MACHINE_DB)
        p01_only = fleet.calculate_fleet_data_health(CONFIG_DB, MACHINE_DB, plant_codes=("p01",))
        self.assertLess(p01_only.equipment_count, both.equipment_count)
        self.assertTrue(all(r["plant_code"] == "p01" for r in p01_only.equipment))


if __name__ == "__main__":
    unittest.main()
