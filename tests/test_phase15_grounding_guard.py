import unittest

from ai.grounding_guard import check_grounding


def _health_context(score=57.2, band="ATTENTION"):
    return {
        "health": {"available": True, "health_score": score, "health_band": band},
        "asset_performance": {"available": False},
        "maintenance_intelligence": {"available": False},
        "savings_verification": {"available": False},
    }


def _unavailable_context():
    return {
        "health": {"available": False, "reason": "none"},
        "asset_performance": {"available": False},
        "maintenance_intelligence": {"available": False},
        "savings_verification": {"available": False},
    }


class TestGroundingGuardPositiveCases(unittest.TestCase):
    def test_true_claim_is_grounded(self):
        result = check_grounding(
            "The Health State is ATTENTION with a Health Score of 57.2.",
            _health_context(), "HEALTH_EXPLANATION",
        )
        self.assertTrue(result.grounded)
        self.assertEqual(result.violations, [])

    def test_no_authoritative_claim_at_all_is_grounded(self):
        result = check_grounding(
            "This equipment may need engineering attention soon.",
            _health_context(), "HEALTH_EXPLANATION",
        )
        self.assertTrue(result.grounded)


class TestGroundingGuardCatchesHallucination(unittest.TestCase):
    """The required 'controlled mocked hallucinated value' scenario."""

    def test_wrong_health_state_is_flagged(self):
        result = check_grounding(
            "The Health State is HEALTHY, so nothing needs attention.",
            _health_context(score=57.2, band="ATTENTION"), "HEALTH_EXPLANATION",
        )
        self.assertFalse(result.grounded)
        self.assertTrue(any("HEALTHY" in v and "ATTENTION" in v for v in result.violations))

    def test_wrong_health_score_is_flagged(self):
        result = check_grounding(
            "The Health Score is 91.4, indicating excellent condition.",
            _health_context(score=57.2), "HEALTH_EXPLANATION",
        )
        self.assertFalse(result.grounded)

    def test_fabricated_score_for_insufficient_evidence_equipment_is_flagged(self):
        """Example F from the plan: INSUFFICIENT_EVIDENCE equipment - the
        AI must not invent a diagnosis. A fabricated numeric score for an
        equipment with no persisted assessment at all must be caught."""
        result = check_grounding(
            "The Health Score is 72.5, which suggests the equipment is in good condition.",
            _unavailable_context(), "HEALTH_EXPLANATION",
        )
        self.assertFalse(result.grounded)
        self.assertTrue(any("no authoritative value is available" in v for v in result.violations))

    def test_within_rounding_tolerance_is_not_flagged(self):
        result = check_grounding(
            "The Health Score is 57.2.", _health_context(score=57.15), "HEALTH_EXPLANATION",
        )
        self.assertTrue(result.grounded)


class TestGroundingGuardScope(unittest.TestCase):
    def test_comparison_is_not_checked_not_silently_grounded(self):
        result = check_grounding(
            "Equipment A's Health State is HEALTHY, Equipment B's is ATTENTION.", {}, "COMPARISON",
        )
        self.assertTrue(result.grounded)
        self.assertFalse(result.checked)

    def test_factory_summary_is_not_checked(self):
        result = check_grounding("Several pieces of equipment need review.", {}, "FACTORY_SUMMARY")
        self.assertFalse(result.checked)

    def test_maintenance_priority_claim_checked(self):
        context = {
            "health": {"available": False},
            "asset_performance": {"available": False},
            "maintenance_intelligence": {"available": True, "maintenance_priority": "REVIEW", "priority_score": 40.0},
            "savings_verification": {"available": False},
        }
        result = check_grounding("The Maintenance Priority is URGENT_REVIEW.", context, "MAINTENANCE_REVIEW")
        self.assertFalse(result.grounded)

    def test_anomaly_severity_claim_matching_an_open_anomaly_is_grounded(self):
        """Regression for a real coverage gap found during live
        commissioning (2026-08-17): the approved plan explicitly
        required anomaly severity/state to be validated 'at minimum',
        but the first implementation only covered Health/Maintenance/
        Performance/Verification."""
        context = {
            "health": {"available": False}, "asset_performance": {"available": False},
            "maintenance_intelligence": {"available": False}, "savings_verification": {"available": False},
            "anomalies": {"available": True, "open_anomalies": [{"target_key": "Power_kW", "severity": "ATTENTION"}]},
        }
        result = check_grounding("An open anomaly's severity is ATTENTION.", context, "HEALTH_EXPLANATION")
        self.assertTrue(result.grounded)

    def test_anomaly_severity_claim_not_matching_any_open_anomaly_is_flagged(self):
        context = {
            "health": {"available": False}, "asset_performance": {"available": False},
            "maintenance_intelligence": {"available": False}, "savings_verification": {"available": False},
            "anomalies": {"available": True, "open_anomalies": [{"target_key": "Power_kW", "severity": "INFORMATION"}]},
        }
        result = check_grounding("An open anomaly's severity is HIGH.", context, "HEALTH_EXPLANATION")
        self.assertFalse(result.grounded)

    def test_verification_result_claim_checked(self):
        context = {
            "health": {"available": False},
            "asset_performance": {"available": False},
            "maintenance_intelligence": {"available": False},
            "savings_verification": {
                "available": True,
                "latest_result": {"result": "REJECTED"},
            },
        }
        result = check_grounding("The verification result is VERIFIED.", context, "SAVINGS_STATUS")
        self.assertFalse(result.grounded)


if __name__ == "__main__":
    unittest.main()
