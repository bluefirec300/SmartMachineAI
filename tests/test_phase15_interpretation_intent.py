import unittest

from ai.interpretation_intent import classify_interpretation_intent as classify
from engine.industrial_query_engine import split_comparison_entities


class TestRequiredRoutingRegressionCases(unittest.TestCase):
    """The exact 8 regression phrases required by the approved Phase 15
    correction, plus the pre-existing classic root-cause example that
    must stay on the OLD pipeline (Route C)."""

    def test_explicit_tag_question_is_not_a_domain_question(self):
        self.assertIsNone(classify("What is AC01 pressure?"))

    def test_health_explanation_even_though_old_engine_calls_it_root_cause(self):
        self.assertEqual(classify("Why is WSP01 unhealthy?"), "HEALTH_EXPLANATION")

    def test_maintenance_review(self):
        self.assertEqual(classify("What should maintenance check on WSP01?"), "MAINTENANCE_REVIEW")

    def test_performance_review_even_though_old_engine_calls_it_root_cause(self):
        self.assertEqual(classify("Why is CHL01 performance getting worse?"), "PERFORMANCE_REVIEW")

    def test_energy_review_even_though_old_engine_resolves_a_real_tag(self):
        # "Is CHL01 wasting energy?" resolves CONFIDENTLY to a real tag
        # (Energy_kWh) under the old engine - the domain phrase must win
        # regardless, or this would be misanswered as a literal current-
        # reading question.
        self.assertEqual(classify("Is CHL01 wasting energy?"), "ENERGY_REVIEW")

    def test_general_engineering_query(self):
        self.assertEqual(classify("What is wrong with WSP01?"), "GENERAL_ENGINEERING_QUERY")

    def test_comparison(self):
        self.assertEqual(classify("Compare P01 WSP01 with P02 WSP01."), "COMPARISON")

    def test_factory_summary(self):
        self.assertEqual(classify("What should engineering look at today?"), "FACTORY_SUMMARY")

    def test_needs_attention_phrasing_routes_to_general_engineering_query(self):
        """Regression for a real gap found during live commissioning
        (2026-08-17): 'Why does P01 WSP01 need attention?' fell through
        to the OLD tag-level pipeline entirely unclassified, because
        'attention' alone wasn't a trigger phrase."""
        self.assertEqual(classify("Why does P01 WSP01 need attention?"), "GENERAL_ENGINEERING_QUERY")
        self.assertEqual(classify("Does this equipment need attention?"), "GENERAL_ENGINEERING_QUERY")

    def test_classic_root_cause_question_stays_unclassified(self):
        """Route C - no domain phrase matched, so the caller falls back
        to the existing, unchanged root_cause pipeline."""
        self.assertIsNone(classify("why is the compressor pressure dropping"))

    def test_plain_current_value_question_stays_unclassified(self):
        self.assertIsNone(classify("what is the chiller temperature"))


class TestSplitComparisonEntities(unittest.TestCase):
    def test_and_connector(self):
        self.assertEqual(
            split_comparison_entities("Compare P01 WSP01 and P02 WSP01"),
            ("P01 WSP01", "P02 WSP01"),
        )

    def test_vs_connector_preferred_over_with(self):
        self.assertEqual(split_comparison_entities("compare CHL01 vs CHL02"), ("CHL01", "CHL02"))

    def test_not_a_comparison_question_returns_none(self):
        self.assertIsNone(split_comparison_entities("why is WSP01 unhealthy"))

    def test_leading_compare_word_is_stripped(self):
        left, right = split_comparison_entities("Comparing AC01 and AC02")
        self.assertEqual((left, right), ("AC01", "AC02"))


if __name__ == "__main__":
    unittest.main()
