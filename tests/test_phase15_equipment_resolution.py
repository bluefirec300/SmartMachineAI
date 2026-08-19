import unittest

from config.environment import get_config_db_path
from engine.industrial_query_engine import IndustrialQueryEngine

"""
Read-only against the real live simulation config.db - the same
established pattern used throughout this project's own commissioning
passes (e.g. the Post-Phase-14 verification). No write path exists
anywhere in resolve_equipment()/resolve_equipment_pair(), so this is
safe to run against the live database.
"""


class EquipmentResolutionTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = IndustrialQueryEngine(database_path=get_config_db_path())


class TestAmbiguousPlantClarification(EquipmentResolutionTestBase):
    """Required test: 'ambiguous P01/P02 equipment clarification'."""

    def test_designator_present_in_both_plants_requires_clarification(self):
        result = self.engine.resolve_equipment("why is WSP01 unhealthy")
        self.assertEqual(result.status, "clarification_required")
        self.assertEqual(result.instance_key, "")
        plants = {c.plant for c in result.candidates if c.equipment_score == 1.0}
        self.assertIn("p01", plants)
        self.assertIn("p02", plants)

    def test_never_silently_picks_lexicographically_first_plant(self):
        """The hard correction: unlike the tag-level _break_plant_ties(),
        resolve_equipment() must NEVER default to P01 when no plant is
        named."""
        result = self.engine.resolve_equipment("is AC01 wasting energy")
        self.assertNotEqual(result.status, "resolved")

    def test_explicit_plant_qualifier_resolves_unambiguously(self):
        result = self.engine.resolve_equipment("why is P01 WSP01 unhealthy")
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.instance_key, "P01.WATER.WSP01")

        result_p2 = self.engine.resolve_equipment("why is P02 WSP01 unhealthy")
        self.assertEqual(result_p2.status, "resolved")
        self.assertEqual(result_p2.instance_key, "P02.WATER.WSP01")


class TestBareFollowUpDetection(EquipmentResolutionTestBase):
    """had_equipment_terms must not be fooled by the standing 'leftover
    generic word' landmine (CLAUDE.md)."""

    def test_bare_followup_has_no_equipment_terms(self):
        result = self.engine.resolve_equipment("has it improved since yesterday")
        self.assertFalse(result.had_equipment_terms)

    def test_designator_named_equipment_has_equipment_terms_even_when_ambiguous(self):
        result = self.engine.resolve_equipment("why is WSP01 unhealthy")
        self.assertTrue(result.had_equipment_terms)

    def test_designator_named_equipment_has_equipment_terms_when_resolved(self):
        result = self.engine.resolve_equipment("why is P01 AC01 unhealthy")
        self.assertTrue(result.had_equipment_terms)


class TestOrderedMultiEntityComparisonResolution(EquipmentResolutionTestBase):
    """Required test: 'ordered multi-equipment comparison resolution' -
    each side resolved INDEPENDENTLY, not the same text matched twice."""

    def test_two_plants_of_the_same_designator_resolve_to_different_entities(self):
        pair = self.engine.resolve_equipment_pair("Compare P01 WSP01 and P02 WSP01")
        self.assertEqual(pair.status, "resolved")
        self.assertEqual(pair.entity_a.instance_key, "P01.WATER.WSP01")
        self.assertEqual(pair.entity_b.instance_key, "P02.WATER.WSP01")
        self.assertNotEqual(pair.entity_a.instance_key, pair.entity_b.instance_key)

    def test_two_different_designators_resolve_independently(self):
        # Every designator code in this dataset exists on both plants
        # (confirmed - CHL01/CHL02 included), so a plant qualifier is
        # required on each side for an unambiguous resolution, exactly
        # like the WSP01 case above.
        pair = self.engine.resolve_equipment_pair("compare P01 CHL01 vs P01 CHL02")
        self.assertEqual(pair.status, "resolved")
        self.assertNotEqual(pair.entity_a.instance_key, pair.entity_b.instance_key)
        self.assertIn("CHL01", pair.entity_a.instance_key)
        self.assertIn("CHL02", pair.entity_b.instance_key)

    def test_unsplittable_comparison_question_is_reported_not_guessed(self):
        pair = self.engine.resolve_equipment_pair("compare things")
        self.assertIn(pair.status, ("could_not_split", "no_match", "clarification_required"))

    def test_one_side_ambiguous_surfaces_as_clarification_not_silent_guess(self):
        pair = self.engine.resolve_equipment_pair("compare WSP01 and CHL01")
        self.assertEqual(pair.status, "clarification_required")
        self.assertEqual(pair.entity_a.status, "clarification_required")


class TestResolveEquipmentReusesTagLevelScoring(EquipmentResolutionTestBase):
    def test_equipment_score_scale_is_0_to_1_not_the_40x_tag_scale(self):
        result = self.engine.resolve_equipment("why is P01 AC01 unhealthy")
        self.assertLessEqual(result.confidence, 0.99)
        for candidate in result.candidates:
            self.assertLessEqual(candidate.equipment_score, 1.0)


if __name__ == "__main__":
    unittest.main()
