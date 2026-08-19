import unittest

from ai import context_builder as cb
from config.environment import get_config_db_path, get_machine_db_path

"""Read-only against the real live simulation database - same pattern as
tests/test_phase15_equipment_resolution.py."""

CONFIG_DB = get_config_db_path()
MACHINE_DB = get_machine_db_path()


class TestIntentSelectiveRetrieval(unittest.TestCase):
    """Required tests: 'intent-selective domain retrieval' and 'unrelated
    domains not queried for narrow questions where practical'."""

    def test_health_explanation_does_not_fetch_asset_performance(self):
        context = cb.build_equipment_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "HEALTH_EXPLANATION")
        self.assertIn("health", context["request"]["domains_fetched"])
        self.assertNotIn("asset_performance", context["request"]["domains_fetched"])
        self.assertNotIn("asset_performance", context)

    def test_energy_review_does_not_fetch_health(self):
        context = cb.build_equipment_ai_context(CONFIG_DB, MACHINE_DB, "P01.UTILITY.CHL01", "ENERGY_REVIEW")
        self.assertIn("energy_opportunity", context["request"]["domains_fetched"])
        self.assertNotIn("health", context["request"]["domains_fetched"])

    def test_savings_status_does_not_fetch_asset_performance(self):
        context = cb.build_equipment_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "SAVINGS_STATUS")
        self.assertNotIn("asset_performance", context["request"]["domains_fetched"])

    def test_general_engineering_query_fetches_the_full_bounded_set(self):
        context = cb.build_equipment_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "GENERAL_ENGINEERING_QUERY")
        for domain in ("health", "maintenance_intelligence", "asset_performance", "anomalies", "energy_opportunity", "savings_verification"):
            self.assertIn(domain, context["request"]["domains_fetched"])

    def test_every_domains_by_intent_entry_only_lists_real_fetcher_keys(self):
        known_fetchers = {
            "health", "maintenance_intelligence", "asset_performance", "anomalies",
            "energy_opportunity", "savings_verification", "events", "maintenance_history", "production_context",
        }
        for intent, domains in cb.DOMAINS_BY_INTENT.items():
            for domain in domains:
                self.assertIn(domain, known_fetchers, msg=f"{intent} lists unknown domain {domain}")


class TestFocusDomainsNarrowsNeverWidens(unittest.TestCase):
    def test_focus_domains_can_only_remove_not_add(self):
        context = cb.build_equipment_ai_context(
            CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "GENERAL_ENGINEERING_QUERY",
            focus_domains=("health",),
        )
        self.assertEqual(context["request"]["domains_fetched"], ["health"])

    def test_focus_domains_naming_an_unrelated_domain_is_ignored(self):
        context = cb.build_equipment_ai_context(
            CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "HEALTH_EXPLANATION",
            focus_domains=("asset_performance",),
        )
        self.assertEqual(context["request"]["domains_fetched"], [])


class TestNullAndInsufficientPreservation(unittest.TestCase):
    """Required tests: 'NULL/INSUFFICIENT remains NULL/INSUFFICIENT',
    'Potential Saving unavailable remains unavailable'."""

    def test_unknown_instance_key_yields_unavailable_domains_not_a_crash(self):
        context = cb.build_equipment_ai_context(
            CONFIG_DB, MACHINE_DB, "P99.NOWHERE.FAKE01", "HEALTH_EXPLANATION",
        )
        self.assertFalse(context["health"]["available"])
        self.assertIsNone(context["equipment"]["equipment_id"])
        self.assertIsNone(context["equipment"]["criticality"])

    def test_criticality_none_is_never_fabricated_to_a_string(self):
        context = cb.build_equipment_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "HEALTH_EXPLANATION")
        # Live data fact confirmed during commissioning: criticality is
        # NULL for all 83 equipment rows today - must render as None
        # here, "Unconfigured" only at the presentation layer.
        self.assertIsNone(context["equipment"]["criticality"])

    def test_no_open_opportunity_is_reported_as_unavailable_not_empty_list_only(self):
        context = cb.build_equipment_ai_context(
            CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "ENERGY_REVIEW",
        )
        opp = context["energy_opportunity"]
        if not opp["available"]:
            self.assertIn("reason", opp)


class TestEquipmentIdentity(unittest.TestCase):
    def test_real_equipment_resolves_identity_fields(self):
        context = cb.build_equipment_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "HEALTH_EXPLANATION")
        equipment = context["equipment"]
        self.assertEqual(equipment["instance_key"], "P01.WATER.WSP01")
        self.assertEqual(equipment["plant_code"], "p01")
        self.assertIsNotNone(equipment["equipment_id"])
        self.assertIn("Water Supply Pump", equipment["display_name"])


class TestComparisonAndFactoryContextsAreSeparate(unittest.TestCase):
    def test_comparison_context_has_two_independent_entities(self):
        context = cb.build_comparison_ai_context(
            CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01", intent="HEALTH_EXPLANATION",
        )
        self.assertEqual(context["entity_a"]["equipment"]["instance_key"], "P01.WATER.WSP01")
        self.assertEqual(context["entity_b"]["equipment"]["instance_key"], "P02.WATER.WSP01")

    def test_factory_context_keeps_four_domains_in_separate_lists(self):
        context = cb.build_factory_ai_context(CONFIG_DB, MACHINE_DB, plant_code="p01")
        for key in ("health_attention", "maintenance_attention", "performance_attention", "energy_opportunities"):
            self.assertIn(key, context)
            self.assertIsInstance(context[key], list)
        # No blended/aggregate key of any kind.
        for forbidden in ("factory_score", "overall_score", "ai_score", "risk_score", "combined_score"):
            self.assertNotIn(forbidden, context)


class TestReadOnlyDatabaseImmutability(unittest.TestCase):
    """Required test: 'database unchanged after AI request' - snapshot
    row counts of every domain table before/after a full context build
    (mirrors the same before/after row-count technique used in this
    project's own commissioning passes)."""

    TABLES = [
        "anomalies", "energy_opportunities", "savings_interventions", "savings_verification_results",
        "equipment_health_snapshots", "equipment_health_factor_snapshots", "maintenance_log",
        "asset_performance_observations", "asset_performance_maintenance_comparisons", "equipment", "tags",
    ]

    def _row_counts(self):
        import sqlite3
        connection = sqlite3.connect(CONFIG_DB)
        try:
            return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in self.TABLES}
        finally:
            connection.close()

    def test_full_context_build_never_mutates_any_domain_table(self):
        before = self._row_counts()

        cb.build_equipment_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "GENERAL_ENGINEERING_QUERY")
        cb.build_comparison_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01")
        cb.build_factory_ai_context(CONFIG_DB, MACHINE_DB, plant_code="p01")

        after = self._row_counts()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
