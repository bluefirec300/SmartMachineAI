import unittest

from engine.anomaly_targets import ANOMALY_RULES
from engine.baseline_targets import EQUIPMENT_TYPE_PROFILES
from engine.performance_targets import (
    DIRECTION_VALUES,
    HIGHER_IS_BETTER,
    INFORMATIONAL_ONLY,
    LOWER_IS_BETTER,
    PERFORMANCE_DIMENSION_REGISTRY,
    TARGET_RANGE,
    dimension_for,
)

"""
Registry sanity + mechanical re-derivation check (item J.2's own
requirement: the direction registry must be built FROM Phase 9's
AnomalyRule.direction, not guessed - this test independently re-derives
the expected direction for every (equipment_type, target_key) that has
an anomaly rule and fails if the registry ever drifts from that source).
"""

_ANOMALY_DIRECTION_TO_PERFORMANCE_DIRECTION = {
    "high": LOWER_IS_BETTER,
    "low": HIGHER_IS_BETTER,
    "both": TARGET_RANGE,
}


class TestRegistryCompleteness(unittest.TestCase):
    def test_every_baseline_target_has_a_registry_entry(self):
        missing = []
        for equipment_type, profile in EQUIPMENT_TYPE_PROFILES.items():
            for target_key in list(profile.raw_targets) + list(profile.derived_targets):
                if (equipment_type, target_key) not in {(d.equipment_type, d.target_key) for d in PERFORMANCE_DIMENSION_REGISTRY.values()}:
                    missing.append((equipment_type, target_key))
        self.assertEqual(missing, [])

    def test_every_entry_has_a_valid_direction(self):
        for dimension in PERFORMANCE_DIMENSION_REGISTRY.values():
            self.assertIn(dimension.direction, DIRECTION_VALUES)

    def test_participates_in_degradation_matches_direction(self):
        for dimension in PERFORMANCE_DIMENSION_REGISTRY.values():
            if dimension.direction == INFORMATIONAL_ONLY:
                self.assertFalse(dimension.participates_in_degradation)
            else:
                self.assertTrue(dimension.participates_in_degradation)

    def test_dimension_for_never_returns_none_and_falls_back_safely(self):
        dimension = dimension_for("some_future_equipment_type", "SomeFutureTarget")
        self.assertIsNotNone(dimension)
        self.assertEqual(dimension.direction, INFORMATIONAL_ONLY)
        self.assertFalse(dimension.participates_in_degradation)


class TestDirectionMechanicallyMatchesAnomalyRules(unittest.TestCase):
    def test_registry_direction_matches_phase9_anomaly_rule_direction(self):
        """For every (equipment_type, target_key) that has a Phase 9
        anomaly rule, the Phase 14 direction must be exactly the
        mechanical mapping of that rule's own `direction` field - never
        an independent guess. A future edit to either registry that
        silently drifts them apart fails this test."""
        checked = 0
        for rule in ANOMALY_RULES:
            key = (rule.equipment_type, rule.baseline_target_key)
            dimension = PERFORMANCE_DIMENSION_REGISTRY.get(f"{rule.equipment_type}|{rule.baseline_target_key}")
            if dimension is None:
                continue
            expected = _ANOMALY_DIRECTION_TO_PERFORMANCE_DIRECTION[rule.direction]
            self.assertEqual(
                dimension.direction, expected,
                f"{key}: registry says {dimension.direction!r}, but anomaly rule {rule.rule_key!r} direction "
                f"{rule.direction!r} implies {expected!r}",
            )
            checked += 1
        self.assertGreater(checked, 20)  # sanity - most dimensions do have an anomaly rule backing them

    def test_informational_only_entries_have_no_anomaly_rule_or_health_factor(self):
        from engine.health_targets import HEALTH_FACTOR_REGISTRY

        anomaly_keys = {(r.equipment_type, r.baseline_target_key) for r in ANOMALY_RULES}
        health_keys = set()
        for equipment_type, factors in HEALTH_FACTOR_REGISTRY.items():
            for factor in factors:
                if factor.source_type == "anomaly":
                    from engine.anomaly_targets import get_rule
                    rule = get_rule(factor.source_rule_key)
                    if rule:
                        health_keys.add((equipment_type, rule.baseline_target_key))

        for dimension in PERFORMANCE_DIMENSION_REGISTRY.values():
            if dimension.direction == INFORMATIONAL_ONLY:
                key = (dimension.equipment_type, dimension.target_key)
                self.assertNotIn(key, anomaly_keys, f"{key} has an anomaly rule but was marked INFORMATIONAL_ONLY")
                self.assertNotIn(key, health_keys, f"{key} has a health factor but was marked INFORMATIONAL_ONLY")


if __name__ == "__main__":
    unittest.main()
