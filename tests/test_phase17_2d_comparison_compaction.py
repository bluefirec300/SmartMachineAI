import unittest

from ai import context_builder as cb
from ai.grounding_guard import check_grounding
from ai.interpretation_intent import comparison_domain_hint
from ai.interpretation_prompt_builder import (
    _render_comparison_facts,
    build_deterministic_fallback,
    build_interpretation_prompt,
    render_comparison_context,
    render_comparison_context_full,
)
from engine.industrial_query_engine import IndustrialQueryEngine
from config.environment import get_config_db_path, get_machine_db_path

"""
Phase 17.2d - comparison context compaction & latency optimization tests.

Same two-style convention as tests/test_phase17_2c_comparison_interpretation.py:
- Integration tests (compact envelope, domain selectivity, prompt-size
  budget) run against the real live simulation database.
- _render_comparison_facts() unit tests use hand-built comparison_facts
  dicts (same pattern as tests/test_phase16_3_data_health_grounding.py)
  so domain-narrowing behavior can be checked without depending on
  which domains happen to be comparable in the live data right now.
"""

CONFIG_DB = get_config_db_path()
MACHINE_DB = get_machine_db_path()


def _health_facts(score_a=68.7, score_b=82.1, comparable=True):
    lower = "A" if score_a < score_b else ("B" if score_b < score_a else None)
    return {
        "comparable": comparable,
        "entity_a": {"available": True, "score": score_a, "band": "ATTENTION", "assessment_confidence": "LOW", "provisional": False},
        "entity_b": {"available": True, "score": score_b, "band": "MONITOR", "assessment_confidence": "MEDIUM", "provisional": False},
        "score_difference": round(score_b - score_a, 1), "lower_score_entity": lower,
    }


def _maintenance_facts(priority_a="REVIEW", priority_b="ROUTINE", checks_a=None, checks_b=None):
    return {
        "comparable": True,
        "entity_a": {"available": True, "priority": priority_a, "recommendation_confidence": "MEDIUM", "recommended_checks": checks_a or []},
        "entity_b": {"available": True, "priority": priority_b, "recommendation_confidence": "HIGH", "recommended_checks": checks_b or []},
        "higher_priority_entity": "A",
    }


def _data_health_facts(score_a=91.0, score_b=88.0):
    return {
        "comparable": True,
        "entity_a": {"status": "GOOD", "score": score_a}, "entity_b": {"status": "GOOD", "score": score_b},
        "score_difference": round(score_b - score_a, 1), "poorer_data_health_entity": "B" if score_b < score_a else None,
    }


def _full_facts(**overrides):
    facts = {
        "health": _health_facts(), "maintenance_intelligence": _maintenance_facts(),
        "data_health": _data_health_facts(),
        "asset_performance": {"comparable": False, "shared_dimensions": [], "entity_a_attention_score": None, "entity_b_attention_score": None},
        "energy_opportunity": {"comparable": True, "entity_a": {"has_opportunity": True, "observed_excess_cost": 100.0}, "entity_b": {"has_opportunity": True, "observed_excess_cost": 500.0}, "higher_observed_excess_cost_entity": "B"},
        "savings_verification": {"comparable": False, "entity_a": {"result": None}, "entity_b": {"result": None}},
    }
    facts.update(overrides)
    return facts


# ---------------------------------------------------------------------------
# comparison_domain_hint() - narrowing signal
# ---------------------------------------------------------------------------

class TestComparisonDomainHint(unittest.TestCase):
    def test_health_phrase_returns_health(self):
        self.assertEqual(comparison_domain_hint("compare health score of P01 WSP01 and P02 WSP01"), ("health",))

    def test_maintenance_phrase_returns_maintenance(self):
        self.assertEqual(comparison_domain_hint("compare maintenance priority of P01 WSP01 and P02 WSP01"), ("maintenance_intelligence",))

    def test_performance_phrase_returns_asset_performance(self):
        self.assertEqual(comparison_domain_hint("compare asset performance of P01 WSP01 and P02 WSP01"), ("asset_performance",))

    def test_energy_phrase_returns_energy(self):
        self.assertEqual(comparison_domain_hint("which is wasting energy, P01 WSP01 or P02 WSP01"), ("energy_opportunity",))

    def test_savings_phrase_returns_savings(self):
        self.assertEqual(comparison_domain_hint("did P01 WSP01 actually save energy compared to P02 WSP01"), ("savings_verification",))

    def test_data_health_phrase_returns_data_health(self):
        self.assertEqual(comparison_domain_hint("compare data health of P01 WSP01 and P02 WSP01"), ("data_health",))

    def test_broad_question_returns_none(self):
        self.assertIsNone(comparison_domain_hint("compare P01 WSP01 and P02 WSP01"))

    def test_unmatched_phrasing_returns_none(self):
        # Documented limitation - reuses _TRIGGER_PHRASES verbatim rather
        # than inventing new vocabulary, so "the health of" (not an exact
        # phrase in HEALTH_EXPLANATION's list) does not narrow.
        self.assertIsNone(comparison_domain_hint("compare the health of P01 WSP01 and P02 WSP01"))


# ---------------------------------------------------------------------------
# _render_comparison_facts() - domain-selective rendering (hand-built facts)
# ---------------------------------------------------------------------------

class TestRenderComparisonFactsSelectivity(unittest.TestCase):
    def test_none_render_domains_renders_every_present_domain(self):
        lines = _render_comparison_facts(_full_facts())
        text = "\n".join(lines)
        for label in ("Health:", "Maintenance Priority:", "Data Health:", "Energy Opportunity:", "Savings Verification:"):
            self.assertIn(label, text)

    def test_narrow_health_excludes_maintenance_and_energy(self):
        lines = _render_comparison_facts(_full_facts(), render_domains=("health",))
        text = "\n".join(lines)
        self.assertIn("Health:", text)
        self.assertNotIn("Maintenance Priority:", text)
        self.assertNotIn("Energy Opportunity:", text)
        self.assertNotIn("Savings Verification:", text)

    def test_narrow_data_health_excludes_unrelated_domains(self):
        lines = _render_comparison_facts(_full_facts(), render_domains=("data_health",))
        text = "\n".join(lines)
        self.assertIn("Data Health:", text)
        self.assertFalse(any(line.startswith("Health:") for line in lines))
        self.assertNotIn("Maintenance Priority:", text)

    def test_data_health_always_rendered_even_when_narrowed_to_health(self):
        # Item 9 - Data Health qualification must survive narrowing to
        # any other single domain, not just be present for its own hint.
        lines = _render_comparison_facts(_full_facts(), render_domains=("health",))
        self.assertTrue(any(line.startswith("Data Health:") for line in lines))

    def test_asset_performance_not_comparable_rendered_plainly(self):
        lines = _render_comparison_facts(_full_facts(), render_domains=("asset_performance",))
        text = "\n".join(lines)
        self.assertIn("NOT_COMPARABLE", text)

    def test_recommended_checks_bounded_and_rendered(self):
        facts = _full_facts(maintenance_intelligence=_maintenance_facts(checks_a=["check bearing", "check seal"], checks_b=["check seal"]))
        lines = _render_comparison_facts(facts, render_domains=("maintenance_intelligence",))
        text = "\n".join(lines)
        self.assertIn("Recommended checks", text)
        self.assertIn("check bearing", text)
        self.assertIn("check seal", text)

    def test_health_history_only_rendered_when_health_wanted(self):
        facts = _full_facts()
        facts["health_history"] = {"comparable": True, "entity_a": {"direction": "DECLINING", "absolute_change": -5.0}, "entity_b": {"direction": "STABLE", "absolute_change": 0.1}, "larger_decline_entity": "A"}
        narrowed_to_health = "\n".join(_render_comparison_facts(facts, render_domains=("health",)))
        narrowed_to_maintenance = "\n".join(_render_comparison_facts(facts, render_domains=("maintenance_intelligence",)))
        self.assertIn("Historical Health direction:", narrowed_to_health)
        self.assertNotIn("Historical Health direction:", narrowed_to_maintenance)

    def test_data_health_history_always_rendered_when_present(self):
        facts = _full_facts()
        facts["data_health_history"] = {"comparable": True, "entity_a": {"good_percentage": 90.0, "no_history_percentage": 2.0}, "entity_b": {"good_percentage": 80.0, "no_history_percentage": 5.0}}
        narrowed_to_health = "\n".join(_render_comparison_facts(facts, render_domains=("health",)))
        self.assertIn("Historical Data Health:", narrowed_to_health)

    def test_compact_style_not_cryptic(self):
        # Item 16 - concise structured language, not lossy abbreviations.
        lines = _render_comparison_facts(_full_facts(), render_domains=("health",))
        text = "\n".join(lines)
        self.assertIn("score=", text)
        self.assertIn("confidence=", text)
        self.assertIn("Deterministic result:", text)


# ---------------------------------------------------------------------------
# Compact envelope / domain selectivity - integration (real DB)
# ---------------------------------------------------------------------------

class TestCompactEnvelopeIntegration(unittest.TestCase):
    def test_compact_identity_lines_present_for_both_entities(self):
        context = cb.build_comparison_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01")
        rendered = render_comparison_context(context)
        self.assertIn("Entity A: ", rendered)
        self.assertIn("Entity B: ", rendered)
        self.assertIn("P01.WATER.WSP01", rendered)
        self.assertIn("P02.WATER.WSP01", rendered)

    def test_compact_render_omits_full_per_entity_dump(self):
        context = cb.build_comparison_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01")
        rendered = render_comparison_context(context)
        # A full render_equipment_context() dump would include this
        # per-entity heading - the compact envelope must not.
        self.assertNotIn("Equipment Health (assessed", rendered)
        self.assertNotIn("=== Entity A ===", rendered)

    def test_broad_comparison_still_contains_all_supported_domain_summaries(self):
        context = cb.build_comparison_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01", question="compare P01 WSP01 and P02 WSP01")
        rendered = render_comparison_context(context)
        self.assertIsNone(context["request"]["comparison_domain_hint"])
        for label in ("Health:", "Maintenance Priority:", "Data Health:", "Energy Opportunity:"):
            self.assertIn(label, rendered)

    def test_narrow_health_comparison_excludes_unrelated_domains(self):
        question = "compare health score of P01 WSP01 and P02 WSP01"
        context = cb.build_comparison_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01", question=question)
        rendered = render_comparison_context(context)
        self.assertEqual(context["request"]["comparison_domain_hint"], ("health",))
        self.assertIn("Health:", rendered)
        self.assertNotIn("Maintenance Priority:", rendered)
        self.assertNotIn("Energy Opportunity:", rendered)
        self.assertNotIn("Savings Verification:", rendered)

    def test_narrow_data_health_comparison_excludes_unrelated_domains(self):
        question = "compare data health of P01 WSP01 and P02 WSP01"
        context = cb.build_comparison_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01", question=question)
        rendered = render_comparison_context(context)
        self.assertEqual(context["request"]["comparison_domain_hint"], ("data_health",))
        self.assertIn("Data Health:", rendered)
        self.assertNotIn("Maintenance Priority:", rendered)

    def test_comparison_facts_unaffected_by_render_narrowing(self):
        # Item 12 - narrowing only changes what gets PRINTED, never what
        # gets computed/available for grounding.
        broad = cb.build_comparison_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01", question="compare P01 WSP01 and P02 WSP01")
        narrow = cb.build_comparison_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01", question="compare health score of P01 WSP01 and P02 WSP01")
        self.assertEqual(set(broad["comparison_facts"].keys()), set(narrow["comparison_facts"].keys()))

    def test_grounding_still_works_with_narrow_rendering(self):
        question = "compare health score of P01 WSP01 and P02 WSP01"
        context = cb.build_comparison_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01", question=question)
        health = context["comparison_facts"]["health"]
        if health["comparable"] and health["entity_a"]["score"] is not None:
            claim = f"Entity A Health Score is {health['entity_a']['score']}."
            result = check_grounding(claim, context, "COMPARISON")
            self.assertTrue(result.checked)
            self.assertTrue(result.grounded)

    def test_cross_equipment_gate_preserved_in_compact_render(self):
        context = cb.build_comparison_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P01.UTILITY.CHL01")
        rendered = render_comparison_context(context)
        self.assertIn("Asset Performance: NOT_COMPARABLE", rendered)

    def test_duplicate_entity_still_rejected_before_llm(self):
        engine = IndustrialQueryEngine(database_path=CONFIG_DB)
        pair = engine.resolve_equipment_pair("compare P01 WSP01 with P01 WSP01")
        self.assertEqual(pair.status, "duplicate_entity")

    def test_deterministic_fallback_uses_full_non_compact_render(self):
        context = cb.build_comparison_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01")
        fallback = build_deterministic_fallback(context, "COMPARISON", "provider unavailable")
        self.assertIn("=== Entity A ===", fallback)
        self.assertIn("=== Entity B ===", fallback)

    def test_no_database_writes_from_prompt_rendering(self):
        import sqlite3
        context = cb.build_comparison_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01")
        connection = sqlite3.connect(CONFIG_DB)
        try:
            before = connection.execute("SELECT COUNT(*) FROM equipment").fetchone()[0]
        finally:
            connection.close()

        build_interpretation_prompt(context, "COMPARISON", "compare P01 WSP01 and P02 WSP01")
        render_comparison_context_full(context)

        connection = sqlite3.connect(CONFIG_DB)
        try:
            after = connection.execute("SELECT COUNT(*) FROM equipment").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(before, after)


# ---------------------------------------------------------------------------
# Prompt-size budget (item 17)
# ---------------------------------------------------------------------------

class TestPromptSizeBudget(unittest.TestCase):
    def test_broad_comparison_prompt_within_budget(self):
        question = "compare P01 WSP01 and P02 WSP01"
        context = cb.build_comparison_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01", question=question)
        prompt = build_interpretation_prompt(context, "COMPARISON", question)
        estimated_tokens = len(prompt) // 4
        self.assertLessEqual(estimated_tokens, 2000, f"broad comparison prompt estimated at {estimated_tokens} tokens, target <=2000")

    def test_narrow_comparison_prompt_within_budget(self):
        question = "compare health score of P01 WSP01 and P02 WSP01"
        context = cb.build_comparison_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01", question=question)
        prompt = build_interpretation_prompt(context, "COMPARISON", question)
        estimated_tokens = len(prompt) // 4
        self.assertLessEqual(estimated_tokens, 1200, f"narrow comparison prompt estimated at {estimated_tokens} tokens, target <=1200")

    def test_no_prediction_or_rul_language_in_compact_prompt(self):
        question = "compare P01 WSP01 and P02 WSP01"
        context = cb.build_comparison_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01", question=question)
        prompt = build_interpretation_prompt(context, "COMPARISON", question)
        self.assertIn("NEVER predict failure", prompt)
        self.assertIn("Remaining Useful Life", prompt)


if __name__ == "__main__":
    unittest.main()
