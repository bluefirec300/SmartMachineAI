import unittest

from ai.grounding_guard import check_grounding
from engine.maintenance_intelligence_targets import PRIORITY_SORT_RANK

"""
Phase 17.2e - comparison grounding hardening.

Fixes a false-rejection bug discovered live during Phase 17.2D
Scenario 4: every comparison-specific entity/value extraction pattern
in ai/grounding_guard.py used `\\b(?:entity\\s+)?([AB])\\b` - the
"Entity " prefix was OPTIONAL, so a bare, case-insensitive "a" (the
ordinary English article) could satisfy `\\b([AB])\\b` on its own and
be misread as a reference to "Entity A", producing a false grounding
violation against a factually-correct answer.

This file's first test class reproduces the exact live failure
(`TestReproduceArticleFalseRejection`) - written and run against the
UNFIXED code first, per this phase's explicit requirement. The
remaining classes are the broader adversarial suite covering every
comparison-specific pattern family, plus a fast confirmation that
numeric grounding, hallucination detection, and NOT_COMPARABLE/
unavailable handling are unweakened.
"""


def _health_facts(score_a=66.2, score_b=73.2, comparable=True):
    lower = "A" if score_a < score_b else ("B" if score_b < score_a else None)
    return {
        "comparable": comparable,
        "entity_a": {"available": True, "score": score_a, "band": "ATTENTION", "assessment_confidence": "LOW", "provisional": True},
        "entity_b": {"available": True, "score": score_b, "band": "MONITOR", "assessment_confidence": "LOW", "provisional": True},
        "score_difference": round(score_b - score_a, 1), "lower_score_entity": lower,
    }


def _maintenance_facts(priority_a="REVIEW", priority_b="ROUTINE", comparable=True):
    rank_a, rank_b = PRIORITY_SORT_RANK[priority_a], PRIORITY_SORT_RANK[priority_b]
    higher = "A" if rank_a > rank_b else ("B" if rank_b > rank_a else None)
    return {
        "comparable": comparable,
        "entity_a": {"available": True, "priority": priority_a, "recommendation_confidence": "LOW"},
        "entity_b": {"available": True, "priority": priority_b, "recommendation_confidence": "LOW"},
        "higher_priority_entity": higher,
    }


def _data_health_facts(score_a=91.0, score_b=88.0, comparable=True):
    poorer = "A" if score_a < score_b else ("B" if score_b < score_a else None)
    status = lambda s: "GOOD" if s >= 85 else ("POOR" if s < 60 else "DEGRADED")
    return {
        "comparable": comparable,
        "entity_a": {"status": status(score_a), "score": score_a}, "entity_b": {"status": status(score_b), "score": score_b},
        "score_difference": round(score_b - score_a, 1), "poorer_data_health_entity": poorer,
    }


def _energy_facts(cost_a=464.13, cost_b=511.11, comparable=True):
    higher = "A" if cost_a > cost_b else ("B" if cost_b > cost_a else None)
    return {
        "comparable": comparable,
        "entity_a": {"has_opportunity": True, "observed_excess_cost": cost_a},
        "entity_b": {"has_opportunity": True, "observed_excess_cost": cost_b},
        "higher_observed_excess_cost_entity": higher,
    }


def _full_facts(**overrides):
    facts = {
        "health": _health_facts(), "maintenance_intelligence": _maintenance_facts(),
        "data_health": _data_health_facts(),
        "asset_performance": {"comparable": False, "shared_dimensions": [], "entity_a_attention_score": None, "entity_b_attention_score": None},
        "energy_opportunity": _energy_facts(),
        "savings_verification": {"comparable": False, "entity_a": {"result": None}, "entity_b": {"result": None}},
    }
    facts.update(overrides)
    return facts


def _comparison_context(comparison_facts):
    return {"comparison_facts": comparison_facts}


# ---------------------------------------------------------------------------
# Reproduction of the exact live Phase 17.2D Scenario 4 false rejection
# ---------------------------------------------------------------------------

class TestReproduceArticleFalseRejection(unittest.TestCase):
    """
    This is the exact phrasing pattern from the real qwen2.5:7b answer
    that was live-rejected in Phase 17.2D Scenario 4: a factually
    correct Data Health restatement, phrased with the ordinary English
    article "a" ("WSP01 (P01) has a Data Health score of 91.0"), with
    Entity A's real Health Score (66.2) sitting elsewhere in the facts
    for the bug to (wrongly) "contradict" against.
    """

    def test_article_a_before_data_health_score_is_not_misread_as_entity_a_health_score(self):
        facts = _full_facts(health=_health_facts(score_a=66.2, score_b=73.2), data_health=_data_health_facts(score_a=91.0, score_b=88.0))
        context = _comparison_context(facts)
        answer = (
            "Data Health for Entity A (Water Supply Pump WSP01): Deterministic status: GOOD, Confidence score: 91.0. "
            "To summarize: WSP01 (P01) has a Data Health score of 91.0, which is better than CHL01 (P01)."
        )
        result = check_grounding(answer, context, "COMPARISON")
        self.assertTrue(result.checked)
        self.assertTrue(result.grounded, f"false rejection reproduced - violations: {result.violations}")
        self.assertEqual(result.violations, [])

    def test_bare_article_a_immediately_before_lower_health_score_phrase_is_not_misread(self):
        # "...shows a lower health score for..." - no "Entity" anywhere
        # near the bare "a". Entity B (not A) is the real lower-score
        # side here, so if the bare "a" is mis-bound to letter "A" this
        # produces a detectable ordering-mismatch violation - a
        # sensitive reproduction, not one that coincidentally passes
        # because the wrong letter happens to match the right answer.
        facts = _full_facts(health=_health_facts(score_a=73.2, score_b=66.2))
        context = _comparison_context(facts)
        answer = "The comparison shows a lower health score for the second pump, though neither entity is named by letter here."
        result = check_grounding(answer, context, "COMPARISON")
        self.assertTrue(result.grounded, f"false rejection reproduced - violations: {result.violations}")


# ---------------------------------------------------------------------------
# Adversarial suite - ordinary English article "a"/"A" near every
# comparison pattern family (not just health_score)
# ---------------------------------------------------------------------------

class TestAdversarialArticleAcrossAllPatternFamilies(unittest.TestCase):
    def test_article_a_near_maintenance_priority_language_not_misread(self):
        # Entity B (not A) is really the higher-priority side.
        facts = _full_facts(maintenance_intelligence=_maintenance_facts(priority_a="ROUTINE", priority_b="URGENT_REVIEW"))
        context = _comparison_context(facts)
        answer = "There is a higher maintenance priority concern worth reviewing here, though no specific pump is named by letter."
        result = check_grounding(answer, context, "COMPARISON")
        self.assertTrue(result.grounded, f"violations: {result.violations}")

    def test_article_a_near_poorer_data_health_language_not_misread(self):
        facts = _full_facts(data_health=_data_health_facts(score_a=91.0, score_b=60.0))
        context = _comparison_context(facts)
        answer = "You could say there is a poorer data health situation on one side, but I won't name which pump by letter."
        result = check_grounding(answer, context, "COMPARISON")
        self.assertTrue(result.grounded, f"violations: {result.violations}")

    def test_article_a_near_higher_observed_excess_cost_language_not_misread(self):
        facts = _full_facts(energy_opportunity=_energy_facts(cost_a=100.0, cost_b=900.0))
        context = _comparison_context(facts)
        answer = "There is a higher observed excess cost somewhere in this comparison, though I will not attribute it by letter."
        result = check_grounding(answer, context, "COMPARISON")
        self.assertTrue(result.grounded, f"violations: {result.violations}")

    def test_article_a_near_larger_historical_decline_language_not_misread(self):
        facts = _full_facts(health_history={"comparable": True, "entity_a": {"direction": "STABLE", "absolute_change": 0.1}, "entity_b": {"direction": "DECLINING", "absolute_change": -9.0}, "larger_decline_entity": "B"})
        context = _comparison_context(facts)
        answer = "There is a larger decline visible in the historical trend, but I am deliberately not naming a side by letter."
        result = check_grounding(answer, context, "COMPARISON")
        self.assertTrue(result.grounded, f"violations: {result.violations}")

    def test_sentence_initial_capitalized_article_A_not_misread(self):
        # "A" capitalized purely because it starts the sentence - not a
        # deliberate "Entity A" label. Entity B is really the poorer side.
        facts = _full_facts(data_health=_data_health_facts(score_a=91.0, score_b=60.0))
        context = _comparison_context(facts)
        answer = "A poorer data health reading was noted on one of the two pumps during this review."
        result = check_grounding(answer, context, "COMPARISON")
        self.assertTrue(result.grounded, f"violations: {result.violations}")


# ---------------------------------------------------------------------------
# Adversarial suite - legitimate "Entity A"/"Entity B" references
# ---------------------------------------------------------------------------

class TestLegitimateEntityReferencesStillWork(unittest.TestCase):
    def test_correct_entity_a_health_score_still_grounded(self):
        context = _comparison_context(_full_facts())
        result = check_grounding("Entity A Health Score is 66.2.", context, "COMPARISON")
        self.assertTrue(result.grounded)
        self.assertTrue(result.checked)

    def test_correct_entity_b_data_health_score_still_grounded(self):
        context = _comparison_context(_full_facts())
        result = check_grounding("Entity B Data Health score is 88.0.", context, "COMPARISON")
        self.assertTrue(result.grounded)

    def test_correct_entity_a_lower_health_score_ordering_still_grounded(self):
        context = _comparison_context(_full_facts())
        result = check_grounding("Entity A has the lower current Health Score.", context, "COMPARISON")
        self.assertTrue(result.grounded)

    def test_correct_entity_b_higher_maintenance_priority_still_grounded(self):
        context = _comparison_context(_full_facts(maintenance_intelligence=_maintenance_facts(priority_a="ROUTINE", priority_b="URGENT_REVIEW")))
        result = check_grounding("Entity B has the higher maintenance priority.", context, "COMPARISON")
        self.assertTrue(result.grounded)

    def test_correct_entity_b_poorer_data_health_still_grounded(self):
        context = _comparison_context(_full_facts(data_health=_data_health_facts(score_a=91.0, score_b=60.0)))
        result = check_grounding("Entity B has poorer Data Health.", context, "COMPARISON")
        self.assertTrue(result.grounded)

    def test_mixed_case_entity_label_still_grounded(self):
        context = _comparison_context(_full_facts())
        result = check_grounding("entity a health score is 66.2.", context, "COMPARISON")
        self.assertTrue(result.grounded)

    def test_full_sentence_with_article_and_legitimate_entity_label_together(self):
        # Both a bare article AND a real "Entity A" label appear in the
        # same answer - only the real label should ever be attributable.
        context = _comparison_context(_full_facts())
        answer = "There is a clear difference here: Entity A Health Score is 66.2, which is a bit lower than Entity B's."
        result = check_grounding(answer, context, "COMPARISON")
        self.assertTrue(result.grounded, f"violations: {result.violations}")


# ---------------------------------------------------------------------------
# Adversarial suite - equipment names instead of A/B (documented false
# negative, unaffected by this phase - never guessed, never checked)
# ---------------------------------------------------------------------------

class TestEquipmentNameOnlyClaimsRemainUnchecked(unittest.TestCase):
    def test_equipment_name_only_health_claim_is_not_checked_even_if_correct(self):
        context = _comparison_context(_full_facts())
        result = check_grounding("WSP01 has a Health Score of 66.2, while CHL01 has a Health Score of 73.2.", context, "COMPARISON")
        self.assertTrue(result.grounded)
        self.assertEqual(result.violations, [])

    def test_equipment_name_only_health_claim_with_wrong_number_is_also_not_checked(self):
        # Documented false-negative-over-false-positive design (unchanged
        # by this phase): a claim with no A/B letter label at all cannot
        # be safely attributed to one side, so even a WRONG number here
        # slips through uncaught. Not a regression - this was already
        # true before Phase 17.2e and is a pre-existing, accepted limit.
        context = _comparison_context(_full_facts())
        result = check_grounding("WSP01 has a Health Score of 12.0, while CHL01 has a Health Score of 73.2.", context, "COMPARISON")
        self.assertTrue(result.grounded)
        self.assertEqual(result.violations, [])


# ---------------------------------------------------------------------------
# Adversarial suite - correct/fabricated/swapped numeric values
# ---------------------------------------------------------------------------

class TestNumericGroundingUnweakened(unittest.TestCase):
    def test_correct_health_score_grounded(self):
        context = _comparison_context(_full_facts())
        result = check_grounding("Entity A Health Score is 66.2.", context, "COMPARISON")
        self.assertTrue(result.grounded)

    def test_fabricated_health_score_rejected(self):
        context = _comparison_context(_full_facts())
        result = check_grounding("Entity A Health Score is 40.0.", context, "COMPARISON")
        self.assertFalse(result.grounded)

    def test_swapped_ab_health_scores_rejected(self):
        # Entity A's real score (66.2) claimed for Entity B, and vice versa.
        context = _comparison_context(_full_facts())
        answer = "Entity A Health Score is 73.2. Entity B Health Score is 66.2."
        result = check_grounding(answer, context, "COMPARISON")
        self.assertFalse(result.grounded)

    def test_correct_data_health_score_with_percent_sign_still_extracted_and_grounded(self):
        context = _comparison_context(_full_facts(data_health=_data_health_facts(score_a=91.0, score_b=88.0)))
        result = check_grounding("Entity A Data Health score is 91.0%.", context, "COMPARISON")
        self.assertTrue(result.grounded, f"violations: {result.violations}")

    def test_fabricated_data_health_score_with_percent_sign_rejected(self):
        context = _comparison_context(_full_facts(data_health=_data_health_facts(score_a=91.0, score_b=88.0)))
        result = check_grounding("Entity A Data Health score is 12.0%.", context, "COMPARISON")
        self.assertFalse(result.grounded)

    def test_correct_lower_health_score_direction_grounded(self):
        context = _comparison_context(_full_facts(health=_health_facts(score_a=66.2, score_b=73.2)))
        result = check_grounding("Entity A has the lower current Health Score.", context, "COMPARISON")
        self.assertTrue(result.grounded)

    def test_incorrect_lower_health_score_direction_rejected(self):
        context = _comparison_context(_full_facts(health=_health_facts(score_a=66.2, score_b=73.2)))
        result = check_grounding("Entity B has the lower current Health Score.", context, "COMPARISON")
        self.assertFalse(result.grounded)


# ---------------------------------------------------------------------------
# Adversarial suite - NOT_COMPARABLE / unavailable / insufficient data
# ---------------------------------------------------------------------------

class TestNotComparableAndUnavailableUnweakened(unittest.TestCase):
    def test_claim_on_not_comparable_health_rejected(self):
        facts = _full_facts(health=_health_facts(comparable=False))
        facts["health"]["entity_b"] = {"available": False, "score": None, "band": None, "assessment_confidence": None, "provisional": None}
        context = _comparison_context(facts)
        result = check_grounding("Entity A has the lower current Health Score.", context, "COMPARISON")
        self.assertFalse(result.grounded)
        self.assertIn("NOT_COMPARABLE", result.violations[0])

    def test_claim_on_not_comparable_data_health_rejected(self):
        facts = _full_facts(data_health=_data_health_facts(comparable=False))
        facts["data_health"]["entity_b"] = {"status": None, "score": None}
        context = _comparison_context(facts)
        result = check_grounding("Entity A has poorer Data Health.", context, "COMPARISON")
        self.assertFalse(result.grounded)

    def test_no_claim_on_not_comparable_domain_stays_grounded(self):
        facts = _full_facts(health=_health_facts(comparable=False))
        facts["health"]["entity_b"] = {"available": False, "score": None, "band": None, "assessment_confidence": None, "provisional": None}
        context = _comparison_context(facts)
        result = check_grounding("This dimension cannot be compared due to insufficient evidence.", context, "COMPARISON")
        self.assertTrue(result.grounded)


# ---------------------------------------------------------------------------
# Adversarial suite - Data Health and Health domains together (the
# exact live scenario 4 shape, generalized)
# ---------------------------------------------------------------------------

class TestDataHealthAndHealthDomainsTogether(unittest.TestCase):
    def test_correct_data_health_and_health_claims_together_grounded(self):
        facts = _full_facts(health=_health_facts(score_a=66.2, score_b=77.6), data_health=_data_health_facts(score_a=91.0, score_b=88.0))
        context = _comparison_context(facts)
        answer = (
            "Entity A Health Score is 66.2, and Entity A Data Health score is 91.0. "
            "Entity B Health Score is 77.6, and Entity B Data Health score is 88.0. "
            "Entity B has poorer Data Health, though this does not affect either equipment's Health Score."
        )
        result = check_grounding(answer, context, "COMPARISON")
        self.assertTrue(result.grounded, f"violations: {result.violations}")

    def test_data_health_value_never_conflated_with_health_score_value(self):
        # Entity A's Data Health (91.0) must never be checked AGAINST
        # Entity A's Health Score (66.2) - they are different fields.
        facts = _full_facts(health=_health_facts(score_a=66.2, score_b=77.6), data_health=_data_health_facts(score_a=91.0, score_b=88.0))
        context = _comparison_context(facts)
        answer = "Entity A Data Health score is 91.0."
        result = check_grounding(answer, context, "COMPARISON")
        self.assertTrue(result.grounded, f"violations: {result.violations}")


# ---------------------------------------------------------------------------
# Adversarial suite - false-acceptance protection (hallucination
# detection / aggregate-verdict rejection) unweakened
# ---------------------------------------------------------------------------

class TestFalseAcceptanceProtectionUnweakened(unittest.TestCase):
    def test_overall_winner_language_still_rejected(self):
        context = _comparison_context(_full_facts())
        result = check_grounding("Overall, Entity A is the better equipment.", context, "COMPARISON")
        self.assertFalse(result.grounded)

    def test_equipment_risk_score_language_still_rejected(self):
        context = _comparison_context(_full_facts())
        result = check_grounding("Entity A has a higher Equipment Risk Score.", context, "COMPARISON")
        self.assertFalse(result.grounded)

    def test_article_a_inside_aggregate_verdict_sentence_does_not_mask_the_violation(self):
        # The fix must not accidentally suppress the (unrelated)
        # aggregate-verdict check just because an article "a" is nearby.
        context = _comparison_context(_full_facts())
        result = check_grounding("There is a higher Equipment Risk Score for one of the pumps.", context, "COMPARISON")
        self.assertFalse(result.grounded)

    def test_empty_context_remains_not_checked_unchanged(self):
        result = check_grounding("Equipment A's Health State is HEALTHY, Equipment B's is ATTENTION.", {}, "COMPARISON")
        self.assertTrue(result.grounded)
        self.assertFalse(result.checked)


if __name__ == "__main__":
    unittest.main()
