import json
import sqlite3
import unittest
from unittest.mock import patch

from ai import context_builder as cb
from config.environment import get_config_db_path, get_machine_db_path

"""
Phase 17.2a - Structured AI Context historical-domain tests. Read-only
against the real live simulation database (same convention as
tests/test_phase15_context_builder.py) - the underlying duration/trend/
transition/recurrence MATH is already tested in
tests/test_data_health_history_engine.py (Phase 16.5) and Phase 12.2's
own health_history suite; these tests verify the ai/context_builder.py
WIRING - correct source function called, correct availability contract,
correct bounds, correct default-off behavior - not the math itself.
"""

CONFIG_DB = get_config_db_path()
MACHINE_DB = get_machine_db_path()
INSTANCE_KEY = "P01.WATER.WSP01"


class TestDefaultBehaviorUnchanged(unittest.TestCase):
    """Item 11 (regression): history_domains defaults to None - every
    existing call site is byte-for-byte unaffected."""

    def test_no_history_keys_present_when_not_requested(self):
        context = cb.build_equipment_ai_context(CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION")
        self.assertNotIn("data_health_history", context)
        self.assertNotIn("health_history", context)
        self.assertNotIn("asset_performance_history", context)

    def test_history_domains_fetched_is_empty_list_by_default(self):
        context = cb.build_equipment_ai_context(CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION")
        self.assertEqual(context["request"]["history_domains_fetched"], [])

    def test_current_state_domains_unchanged_when_history_also_requested(self):
        """Requesting history must not alter any existing current-state
        field's value."""
        without = cb.build_equipment_ai_context(CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION")
        with_history = cb.build_equipment_ai_context(
            CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION",
            history_domains=("data_health_history", "health_history", "asset_performance_history"),
        )
        for key in ("equipment", "data_health", "health"):
            self.assertEqual(without[key], with_history[key])

    def test_unrecognized_history_domain_name_silently_ignored(self):
        context = cb.build_equipment_ai_context(
            CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION",
            history_domains=("not_a_real_domain",),
        )
        self.assertNotIn("not_a_real_domain", context)
        self.assertEqual(context["request"]["history_domains_fetched"], [])


class TestDataHealthHistoryDomainStructure(unittest.TestCase):
    def test_structure_and_availability_contract(self):
        context = cb.build_equipment_ai_context(
            CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION", history_domains=("data_health_history",),
        )
        domain = context["data_health_history"]
        self.assertIn("available", domain)
        self.assertIsInstance(domain["available"], bool)
        if domain["available"]:
            for key in ("recorded_since", "window_days", "status_duration", "recent_transitions", "recurring_issues"):
                self.assertIn(key, domain)

    def test_separate_sibling_key_never_merged_into_current_state(self):
        context = cb.build_equipment_ai_context(
            CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION", history_domains=("data_health_history",),
        )
        self.assertIn("data_health", context)
        self.assertIn("data_health_history", context)
        self.assertNotIn("status_duration", context["data_health"])
        self.assertNotIn("confidence_status", context["data_health_history"])

    def test_uses_engine_data_health_history_never_raw_reconstruction(self):
        with patch("ai.context_builder.dhh.status_duration") as mock_duration, \
             patch("ai.context_builder.dhh.status_transitions", return_value=[]) as mock_transitions, \
             patch("ai.context_builder.dhh.recurring_issues", return_value={"occurrences": {}, "insufficient_history": False}) as mock_recurrence, \
             patch("ai.context_builder.dhh.first_recorded_at", return_value="2026-08-01 00:00:00"):
            mock_duration.return_value = {"insufficient_history": False, "percentages": {}}
            cb.build_equipment_ai_context(
                CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION", history_domains=("data_health_history",),
            )
        mock_duration.assert_called_once()
        mock_transitions.assert_called_once()
        mock_recurrence.assert_called_once()

    def test_transitions_bounded_to_5(self):
        fake_transitions = [{"from_status": "GOOD", "to_status": "DEGRADED", "at": f"t{i}", "changes": []} for i in range(20)]
        with patch("ai.context_builder.dhh.first_recorded_at", return_value="2026-08-01 00:00:00"), \
             patch("ai.context_builder.dhh.status_duration", return_value={"insufficient_history": False, "percentages": {}}), \
             patch("ai.context_builder.dhh.status_transitions", return_value=fake_transitions), \
             patch("ai.context_builder.dhh.recurring_issues", return_value={"occurrences": {}, "insufficient_history": False}):
            context = cb.build_equipment_ai_context(
                CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION", history_domains=("data_health_history",),
            )
        self.assertLessEqual(len(context["data_health_history"]["recent_transitions"]), 5)

    def test_no_history_recorded_yet_produces_reason_not_fabricated_status(self):
        with patch("ai.context_builder.dhh.first_recorded_at", return_value=None):
            context = cb.build_equipment_ai_context(
                CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION", history_domains=("data_health_history",),
            )
        domain = context["data_health_history"]
        self.assertFalse(domain["available"])
        self.assertIsNotNone(domain["reason"])
        self.assertNotIn("status_duration", domain)

    def test_unresolvable_equipment_is_unavailable_with_reason(self):
        context = cb.build_equipment_ai_context(
            CONFIG_DB, MACHINE_DB, "P99.NOWHERE.FAKE01", "HEALTH_EXPLANATION", history_domains=("data_health_history",),
        )
        domain = context["data_health_history"]
        self.assertFalse(domain["available"])
        self.assertIsNotNone(domain["reason"])


class TestHealthHistoryDomainStructure(unittest.TestCase):
    def test_structure_and_availability_contract(self):
        context = cb.build_equipment_ai_context(
            CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION", history_domains=("health_history",),
        )
        domain = context["health_history"]
        self.assertIn("available", domain)
        if domain["available"]:
            self.assertIn("trend", domain)
            self.assertIn("recent_band_transitions", domain)

    def test_separate_sibling_key_never_merged_into_current_state(self):
        context = cb.build_equipment_ai_context(
            CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION", history_domains=("health_history",),
        )
        self.assertNotIn("trend", context["health"])

    def test_uses_engine_health_history_never_recalculated(self):
        with patch("ai.context_builder.hist.compute_trend") as mock_trend, \
             patch("ai.context_builder.hist.band_transitions", return_value=[]) as mock_transitions:
            mock_trend.return_value = {"direction": "STABLE", "latest_score": 90.0, "previous_score": 89.0, "absolute_change": 1.0, "observation_period_start": "x", "observation_period_end": "y"}
            cb.build_equipment_ai_context(
                CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION", history_domains=("health_history",),
            )
        mock_trend.assert_called_once()
        mock_transitions.assert_called_once()

    def test_band_transitions_bounded_to_5(self):
        fake_transitions = [{"from_band": "MONITOR", "to_band": "ATTENTION", "at": f"t{i}"} for i in range(20)]
        with patch("ai.context_builder.hist.compute_trend") as mock_trend, \
             patch("ai.context_builder.hist.band_transitions", return_value=fake_transitions):
            mock_trend.return_value = {"direction": "STABLE", "latest_score": 90.0, "previous_score": 89.0, "absolute_change": 1.0, "observation_period_start": "x", "observation_period_end": "y"}
            context = cb.build_equipment_ai_context(
                CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION", history_domains=("health_history",),
            )
        self.assertLessEqual(len(context["health_history"]["recent_band_transitions"]), 5)

    def test_insufficient_history_produces_reason_not_fabricated_trend(self):
        with patch("ai.context_builder.hist.compute_trend") as mock_trend:
            mock_trend.return_value = {"direction": "INSUFFICIENT_HISTORY", "latest_score": None, "previous_score": None, "absolute_change": None, "observation_period_start": None, "observation_period_end": None}
            context = cb.build_equipment_ai_context(
                CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION", history_domains=("health_history",),
            )
        domain = context["health_history"]
        self.assertFalse(domain["available"])
        self.assertIsNotNone(domain["reason"])
        self.assertNotIn("trend", domain)


class TestAssetPerformanceHistoryDomainStructure(unittest.TestCase):
    def test_structure_and_availability_contract(self):
        context = cb.build_equipment_ai_context(
            CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION", history_domains=("asset_performance_history",),
        )
        domain = context["asset_performance_history"]
        self.assertIn("available", domain)
        self.assertIn("recent_observations", domain)
        self.assertIn("maintenance_comparisons", domain)

    def test_separate_sibling_key_never_merged_into_current_state(self):
        context = cb.build_equipment_ai_context(
            CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION",
            focus_domains=None, history_domains=("asset_performance_history",),
        )
        self.assertIn("asset_performance_history", context)
        # asset_performance itself was never requested via focus_domains/DOMAINS_BY_INTENT here
        self.assertNotIn("dimensions", context.get("asset_performance", {}))

    def test_uses_existing_asset_performance_read_model_never_a_new_engine(self):
        with patch("ai.context_builder.apd.get_overview") as mock_overview, \
             patch("ai.context_builder.apd.get_detail") as mock_detail, \
             patch("ai.context_builder.apd.get_maintenance_comparisons_for_instance", return_value=[]) as mock_comparisons:
            mock_overview.return_value = [{
                "instance_key": INSTANCE_KEY, "target_key": "flow_per_kw", "performance_state": "STABLE",
            }]
            mock_detail.return_value = {"latest": {}, "history": [], "context": {}, "maintenance_comparisons": []}
            cb.build_equipment_ai_context(
                CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION", history_domains=("asset_performance_history",),
            )
        mock_overview.assert_called()
        mock_detail.assert_called()
        mock_comparisons.assert_called_once()

    def test_single_dimension_with_long_history_yields_only_its_most_recent_observation(self):
        """17.2a.1 - a single dimension no longer contributes up to 3 of
        its own observations; only its single most recent one, since the
        bound is now TOTAL across the whole domain, not per dimension."""
        fake_history = [
            {"computed_at": f"t{i}", "performance_state": "STABLE", "observed_value": float(i), "percent_change": 0.0, "evidence_quality": "STRONG"}
            for i in range(30)
        ]
        with patch("ai.context_builder.apd.get_overview") as mock_overview, \
             patch("ai.context_builder.apd.get_detail") as mock_detail, \
             patch("ai.context_builder.apd.get_maintenance_comparisons_for_instance", return_value=[]):
            mock_overview.return_value = [{
                "instance_key": INSTANCE_KEY, "target_key": "flow_per_kw", "performance_state": "STABLE",
            }]
            mock_detail.return_value = {"latest": {}, "history": fake_history, "context": {}, "maintenance_comparisons": []}
            context = cb.build_equipment_ai_context(
                CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION", history_domains=("asset_performance_history",),
            )
        observations = context["asset_performance_history"]["recent_observations"]
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["computed_at"], "t29")  # the most recent one, not the oldest

    def test_recent_observations_bounded_to_3_total_regardless_of_dimension_count(self):
        """17.2a.1's core requirement: multiple Asset Performance
        dimensions, each with their own long history, must never push
        the TOTAL past 3."""
        dimension_names = ["flow_per_kw", "bearing_temp", "vibration", "delta_p", "power_load"]
        fake_history = [
            {"computed_at": f"t{i}", "performance_state": "STABLE", "observed_value": 1.0, "percent_change": 0.0, "evidence_quality": "STRONG"}
            for i in range(10)
        ]

        def fake_get_detail(plant_id, instance_key, target_key, days=90):
            return {"latest": {}, "history": fake_history, "context": {}, "maintenance_comparisons": []}

        with patch("ai.context_builder.apd.get_overview") as mock_overview, \
             patch("ai.context_builder.apd.get_detail", side_effect=fake_get_detail), \
             patch("ai.context_builder.apd.get_maintenance_comparisons_for_instance", return_value=[]):
            mock_overview.return_value = [
                {"instance_key": INSTANCE_KEY, "target_key": name, "performance_state": "SIGNIFICANTLY_DEGRADING"}
                for name in dimension_names
            ]
            context = cb.build_equipment_ai_context(
                CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION", history_domains=("asset_performance_history",),
            )
        observations = context["asset_performance_history"]["recent_observations"]
        self.assertLessEqual(len(observations), 3)

    def test_no_persisted_history_produces_reason_not_fabricated_observations(self):
        with patch("ai.context_builder.apd.get_overview", return_value=[]):
            context = cb.build_equipment_ai_context(
                CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION", history_domains=("asset_performance_history",),
            )
        domain = context["asset_performance_history"]
        self.assertFalse(domain["available"])
        self.assertIsNotNone(domain["reason"])
        self.assertEqual(domain["recent_observations"], [])


class TestMaintenanceIntelligenceHistoryDeliberatelyAbsent(unittest.TestCase):
    """Item 7 of the approved plan - Maintenance Intelligence has no
    persisted snapshot layer; Phase 17.2a must not invent one."""

    def test_maintenance_intelligence_history_domain_does_not_exist(self):
        self.assertFalse(hasattr(cb, "_maintenance_intelligence_history_domain"))

    def test_requesting_it_is_silently_ignored(self):
        context = cb.build_equipment_ai_context(
            CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION",
            history_domains=("maintenance_intelligence_history",),
        )
        self.assertNotIn("maintenance_intelligence_history", context)


class TestReadOnlySafety(unittest.TestCase):
    TABLES = ("data_health_snapshots", "equipment_health_snapshots", "asset_performance_observations", "equipment")

    def _row_counts(self):
        connection = sqlite3.connect(CONFIG_DB)
        try:
            return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in self.TABLES}
        finally:
            connection.close()

    def test_building_full_historical_context_never_mutates_the_database(self):
        before = self._row_counts()
        cb.build_equipment_ai_context(
            CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION",
            history_domains=("data_health_history", "health_history", "asset_performance_history"),
        )
        after = self._row_counts()
        self.assertEqual(before, after)


class TestContextSizeBudget(unittest.TestCase):
    def test_single_domain_history_stays_reasonably_bounded(self):
        """Each domain individually should stay well under a few hundred
        tokens - the full combined-3-domain aggregate is measured and
        reported separately (see the Phase 17.2a completion report),
        since it is allowed to exceed a single-domain's own budget."""
        for domain in ("data_health_history", "health_history"):
            context = cb.build_equipment_ai_context(
                CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION", history_domains=(domain,),
            )
            serialized = json.dumps(context[domain], default=str)
            approx_tokens = len(serialized) / 4
            self.assertLess(approx_tokens, 250, msg=f"{domain} serialized to ~{approx_tokens:.0f} tokens")

    def test_no_raw_historian_rows_in_any_history_domain(self):
        context = cb.build_equipment_ai_context(
            CONFIG_DB, MACHINE_DB, INSTANCE_KEY, "HEALTH_EXPLANATION",
            history_domains=("data_health_history", "health_history", "asset_performance_history"),
        )
        for domain in ("data_health_history", "health_history", "asset_performance_history"):
            serialized = json.dumps(context[domain], default=str)
            self.assertNotIn("plc_data", serialized)


class TestExistingPhase15Regression(unittest.TestCase):
    """Item 12/full-suite item - a narrow, fast confirmation that the
    Phase 15 comparison/factory paths (which never pass history_domains)
    are completely unaffected."""

    def test_comparison_context_unaffected(self):
        context = cb.build_comparison_ai_context(
            CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01", intent="HEALTH_EXPLANATION",
        )
        self.assertNotIn("data_health_history", context["entity_a"])
        self.assertNotIn("health_history", context["entity_a"])

    def test_factory_context_unaffected(self):
        context = cb.build_factory_ai_context(CONFIG_DB, MACHINE_DB, plant_code="p01")
        self.assertNotIn("data_health_history", context)
        self.assertNotIn("health_history", context)


if __name__ == "__main__":
    unittest.main()
