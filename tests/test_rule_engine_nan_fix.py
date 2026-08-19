import unittest

from ai.rule_engine import RuleEngine

"""
Phase 16.1 - regression tests for the RuleEngine NaN/infinity correctness
fix. Constructs RuleEngine via __new__ (bypassing __init__, which needs a
real config database) and sets .rules directly - evaluate_summary() only
ever reads self.rules for the code path under test here, so this is a
fully self-contained unit test with no database/fixture required.
"""


def _engine(rules: dict) -> RuleEngine:
    engine = RuleEngine.__new__(RuleEngine)
    engine.rules = rules
    engine.tag_metadata = {}
    return engine


class TestNaNIsNeverWithinLimits(unittest.TestCase):
    """The exact bug: float('nan') doesn't raise in float(), and every
    comparison against NaN is False, so a NaN reading previously fell
    through every alarm/warning branch to the final within_limits/normal
    else-case."""

    def test_nan_is_invalid_value_not_within_limits(self):
        engine = _engine({"TAG": {"low_alarm": 0.0, "high_alarm": 100.0}})
        result = engine.evaluate_summary({"tag": "TAG", "current": float("nan")})
        self.assertEqual(result["condition"], "invalid_value")
        self.assertNotEqual(result["condition"], "within_limits")

    def test_nan_is_never_normal_severity(self):
        engine = _engine({"TAG": {"low_alarm": 0.0, "high_alarm": 100.0}})
        result = engine.evaluate_summary({"tag": "TAG", "current": float("nan")})
        # severity stays the dict's default "normal" (invalid_value does
        # not escalate severity, matching the existing invalid-value/
        # not_evaluated convention) - the REQUIRED distinction is the
        # condition, not severity.
        self.assertEqual(result["condition"], "invalid_value")


class TestInfinityIsInvalidValue(unittest.TestCase):
    def test_positive_infinity_is_invalid_value(self):
        engine = _engine({"TAG": {"low_alarm": 0.0, "high_alarm": 100.0}})
        result = engine.evaluate_summary({"tag": "TAG", "current": float("inf")})
        self.assertEqual(result["condition"], "invalid_value")

    def test_negative_infinity_is_invalid_value(self):
        engine = _engine({"TAG": {"low_alarm": 0.0, "high_alarm": 100.0}})
        result = engine.evaluate_summary({"tag": "TAG", "current": float("-inf")})
        self.assertEqual(result["condition"], "invalid_value")

    def test_infinity_message_names_the_actual_value(self):
        engine = _engine({"TAG": {"low_alarm": 0.0, "high_alarm": 100.0}})
        result = engine.evaluate_summary({"tag": "TAG", "current": float("inf")})
        self.assertIn("inf", result["message"])


class TestFiniteValuesUnaffected(unittest.TestCase):
    """The fix must not change behavior for any real, finite reading -
    only NaN/+-inf are newly caught."""

    def test_normal_finite_value_still_within_limits(self):
        engine = _engine({"TAG": {"low_alarm": 0.0, "high_alarm": 100.0}})
        result = engine.evaluate_summary({"tag": "TAG", "current": 50.0})
        self.assertEqual(result["condition"], "within_limits")

    def test_alarm_worthy_finite_value_still_reports_alarm_not_invalid(self):
        """A genuinely abnormal but VALID reading must still be
        evaluated normally - the fix only catches objectively
        non-finite values, never reinterprets a real extreme reading as
        invalid data (Phase 16.1 item E's hard rule, shared discipline)."""
        engine = _engine({"TAG": {"low_alarm": 0.0, "high_alarm": 100.0}})
        result = engine.evaluate_summary({"tag": "TAG", "current": 150.0})
        self.assertEqual(result["condition"], "high_alarm")
        self.assertEqual(result["severity"], "alarm")

    def test_non_numeric_string_still_invalid_value(self):
        """Pre-existing behavior (float() raising) must be unaffected."""
        engine = _engine({"TAG": {"low_alarm": 0.0, "high_alarm": 100.0}})
        result = engine.evaluate_summary({"tag": "TAG", "current": "FAULT"})
        self.assertEqual(result["condition"], "invalid_value")

    def test_none_current_still_not_evaluated(self):
        engine = _engine({"TAG": {"low_alarm": 0.0, "high_alarm": 100.0}})
        result = engine.evaluate_summary({"tag": "TAG", "current": None})
        self.assertEqual(result["condition"], "not_evaluated")


if __name__ == "__main__":
    unittest.main()
