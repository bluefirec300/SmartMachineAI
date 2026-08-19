import unittest
from unittest.mock import patch

from ai import context_builder as cb
from ai.grounding_guard import check_grounding
from ai.interpretation_intent import requires_telemetry_confidence
from ai.interpretation_prompt_builder import FIXED_PREAMBLE, build_interpretation_prompt, render_equipment_context
from app.ask import AskEngine
from config.environment import get_config_db_path, get_machine_db_path
from engine.data_health_engine import EquipmentDataHealth

"""
Phase 16.3 - Data Health -> AI Grounding integration tests. Covers the
22 required scenarios plus the 4 adversarial examples named in the
approved plan.

Two testing styles, matching this project's own established convention:
- ai/context_builder.py tests run against the REAL live simulation
  database (same pattern as tests/test_phase15_context_builder.py),
  with engine.data_health_engine.calculate_equipment_data_health()
  mocked only where a SPECIFIC status (DEGRADED/POOR/failure) needs to
  be forced deterministically.
- ai/grounding_guard.py and ai/interpretation_prompt_builder.py tests
  use small, hand-built context dicts (same pattern as
  tests/test_phase15_grounding_guard.py) - no database needed at all.
- app/ask.py tests use a real AskEngine with ai_provider replaced by a
  fake in-memory provider (same pattern as
  tests/test_phase15_ask_integration.py's _engine_without_ai()).
"""

CONFIG_DB = get_config_db_path()
MACHINE_DB = get_machine_db_path()


def _fake_data_health(status, score=70.0, **overrides):
    fields = dict(
        instance_key="P01.WATER.WSP01",
        equipment_id=1,
        equipment_type="water_supply_pump",
        confidence_score=score,
        confidence_status=status,
        component_scores={"freshness": score, "availability": score, "validity": score, "continuity": score},
        component_applicability={"freshness": True, "availability": True, "validity": True, "continuity": True},
        required_tag_count=5,
        available_tag_count=5,
        fresh_tag_count=5,
        stale_tags=[],
        missing_tags=[],
        invalid_tags=[],
        indeterminate_freshness_tags=[],
        frozen_candidates=[],
        gaps=[],
        timestamp_issues=[],
        source={"configured_driver": "simulator", "note": "configured source only"},
        tags=[],
        limitations=[],
        reasons=[f"forced {status} for testing"],
        computed_at="2026-08-17 00:00:00",
    )
    fields.update(overrides)
    return EquipmentDataHealth(**fields)


def _compact(equipment_data_health):
    """Mirrors ai/context_builder.py's _data_health_domain() shape - kept
    here so grounding-guard/prompt-builder tests can hand-build the exact
    compact dict shape without a database round trip."""
    result = equipment_data_health
    return {
        "available": True,
        "confidence_status": result.confidence_status,
        "confidence_score": result.confidence_score,
        "component_scores": dict(result.component_scores),
        "component_applicability": dict(result.component_applicability),
        "required_tag_count": result.required_tag_count,
        "available_tag_count": result.available_tag_count,
        "fresh_tag_count": result.fresh_tag_count,
        "stale_tags": list(result.stale_tags),
        "missing_tags": list(result.missing_tags),
        "invalid_tags": list(result.invalid_tags),
        "indeterminate_freshness_tags": list(result.indeterminate_freshness_tags),
        "frozen_candidates": list(result.frozen_candidates),
        "gap_count": len(result.gaps),
        "timestamp_issue_count": len(result.timestamp_issues),
        "source_driver": (result.source or {}).get("configured_driver"),
        "reasons": list(result.reasons),
        "limitations": list(result.limitations),
        "as_of": result.computed_at,
    }


def _minimal_context(intent="HEALTH_EXPLANATION", requires_confidence=True, data_health=None):
    return {
        "request": {
            "user_question": "why is P01 WSP01 unhealthy",
            "resolved_intent": intent,
            "domains_fetched": [],
            "requires_telemetry_confidence": requires_confidence,
        },
        "equipment": {
            "instance_key": "P01.WATER.WSP01",
            "display_name": "Water Supply Pump 01 (P01.WATER.WSP01)",
            "plant_code": "p01",
            "area_name": "Water",
            "system_name": None,
            "criticality": None,
            "equipment_id": 1,
        },
        "data_health": data_health,
        "data_limitations": [],
        "provenance": {"context_built_at": "2026-08-17 00:00:00", "source_modules": []},
    }


class _FakeProvider:
    def __init__(self, text):
        self._text = text
        self.provider = "fake"
        self.calls = 0

    def generate(self, prompt):
        self.calls += 1
        self.last_prompt = prompt
        return self._text


def _engine_with_fake_provider(text):
    engine = AskEngine()
    engine.ai_provider = _FakeProvider(text)
    return engine


# ---------------------------------------------------------------------------
# 1/2/3/4 - behavior by Data Confidence for telemetry-dependent questions
# ---------------------------------------------------------------------------

class TestBehaviorByDataConfidence(unittest.TestCase):
    def test_good_and_telemetry_dependent_is_normal_flow_no_extra_instruction(self):
        context = _minimal_context(data_health=_compact(_fake_data_health("GOOD", 95.0)))
        prompt = build_interpretation_prompt(context, "HEALTH_EXPLANATION", "why is P01 WSP01 unhealthy")
        self.assertNotIn("clearly qualify", prompt)
        self.assertNotIn("You must NOT provide a confident", prompt)

    def test_degraded_and_telemetry_dependent_requires_qualification_instruction(self):
        context = _minimal_context(data_health=_compact(_fake_data_health("DEGRADED", 70.0)))
        prompt = build_interpretation_prompt(context, "HEALTH_EXPLANATION", "why is P01 WSP01 unhealthy")
        self.assertIn("DEGRADED", prompt)
        self.assertIn("clearly qualify", prompt)

    def test_poor_and_telemetry_dependent_forbids_confident_conclusion_instruction(self):
        context = _minimal_context(data_health=_compact(_fake_data_health("POOR", 30.0)))
        prompt = build_interpretation_prompt(context, "HEALTH_EXPLANATION", "why is P01 WSP01 unhealthy")
        self.assertIn("POOR", prompt)
        self.assertIn("must NOT provide a confident", prompt)

    def test_unavailable_and_telemetry_dependent_short_circuits_before_llm_call(self):
        engine = _engine_with_fake_provider("should never be seen")
        context = _minimal_context(data_health=_compact(_fake_data_health("UNAVAILABLE", None)))
        result = engine._render_interpretation_answer(context, "HEALTH_EXPLANATION", "why is P01 WSP01 unhealthy", [])
        self.assertEqual(engine.ai_provider.calls, 0)
        self.assertTrue(result.fallback_used)
        self.assertIn("UNAVAILABLE", result.answer)
        self.assertIsNone(result.provider)


# ---------------------------------------------------------------------------
# 5/6 - static/reference questions remain answerable regardless of Data Health
# ---------------------------------------------------------------------------

class TestStaticReferenceQuestionsBypassGating(unittest.TestCase):
    def test_poor_static_reference_question_still_calls_provider(self):
        engine = _engine_with_fake_provider("The rated motor power is 15 kW.")
        context = _minimal_context(requires_confidence=False, data_health=_compact(_fake_data_health("POOR", 30.0)))
        result = engine._render_interpretation_answer(context, "GENERAL_ENGINEERING_QUERY", "what is the rated motor power", [])
        self.assertEqual(engine.ai_provider.calls, 1)
        self.assertFalse(result.fallback_used)

    def test_unavailable_static_reference_question_still_calls_provider(self):
        engine = _engine_with_fake_provider("The rated motor power is 15 kW.")
        context = _minimal_context(requires_confidence=False, data_health=_compact(_fake_data_health("UNAVAILABLE", None)))
        result = engine._render_interpretation_answer(context, "GENERAL_ENGINEERING_QUERY", "what is the rated motor power", [])
        self.assertEqual(engine.ai_provider.calls, 1)
        self.assertFalse(result.fallback_used)

    def test_requires_telemetry_confidence_false_for_static_reference_phrase(self):
        self.assertFalse(requires_telemetry_confidence("HEALTH_EXPLANATION", "what is the rated power of this pump?"))

    def test_requires_telemetry_confidence_true_for_condition_question(self):
        self.assertTrue(requires_telemetry_confidence("HEALTH_EXPLANATION", "is this pump healthy?"))


# ---------------------------------------------------------------------------
# 7/8/9/11/12 + exact adversarial strings - grounding guard conflation checks
# ---------------------------------------------------------------------------

class TestGroundingGuardDataHealthConflation(unittest.TestCase):
    def _context(self, data_health_overrides=None, requires_confidence=True):
        data_health = _compact(_fake_data_health("GOOD", 95.0))
        if data_health_overrides:
            data_health.update(data_health_overrides)
        return {
            "health": {"available": False},
            "asset_performance": {"available": False},
            "maintenance_intelligence": {"available": False},
            "savings_verification": {"available": False},
            "data_health": data_health,
            "request": {"requires_telemetry_confidence": requires_confidence},
        }

    def test_good_data_health_never_translated_to_machine_healthy(self):
        context = self._context({"confidence_status": "GOOD"})
        result = check_grounding("The pump is healthy because Data Confidence is GOOD.", context, "HEALTH_EXPLANATION")
        self.assertFalse(result.grounded)
        self.assertTrue(any("GOOD Data Health" in v for v in result.violations))

    def test_poor_data_health_never_translated_to_machine_faulty(self):
        context = self._context({"confidence_status": "POOR"})
        result = check_grounding("The pump is faulty because Data Confidence is POOR.", context, "HEALTH_EXPLANATION")
        self.assertFalse(result.grounded)
        self.assertTrue(any("POOR Data Health" in v for v in result.violations))

    def test_frozen_candidate_never_promoted_to_confirmed_sensor_failure(self):
        context = self._context({"frozen_candidates": ["P01.WATER.WSP01.Pressure"]})
        result = check_grounding("The sensor has failed because it is frozen.", context, "HEALTH_EXPLANATION")
        self.assertFalse(result.grounded)
        self.assertTrue(any("frozen" in v for v in result.violations))

    def test_configured_plc_source_does_not_become_plc_connected(self):
        context = self._context({"source_driver": "fins"})
        result = check_grounding("The PLC is online because the source is FINS UDP.", context, "HEALTH_EXPLANATION")
        self.assertFalse(result.grounded)
        self.assertTrue(any("connection" in v for v in result.violations))

    def test_configured_opcua_source_does_not_become_opcua_online(self):
        context = self._context({"source_driver": "opcua"})
        result = check_grounding("The OPC UA is online, so communication is healthy.", context, "HEALTH_EXPLANATION")
        self.assertFalse(result.grounded)

    def test_poor_confidence_and_telemetry_dependent_blocks_confident_operating_normally_claim(self):
        context = self._context({"confidence_status": "POOR"}, requires_confidence=True)
        result = check_grounding("The compressor is operating normally.", context, "HEALTH_EXPLANATION")
        self.assertFalse(result.grounded)

    def test_poor_confidence_but_not_telemetry_dependent_does_not_block_the_same_claim(self):
        """A static/reference question routed here (edge case) must not
        trip the confident-condition check - it is gated on
        requires_telemetry_confidence, not on Data Confidence alone."""
        context = self._context({"confidence_status": "POOR"}, requires_confidence=False)
        result = check_grounding("The compressor is operating normally.", context, "HEALTH_EXPLANATION")
        self.assertTrue(result.grounded)

    def test_no_data_health_context_never_raises_and_never_false_flags(self):
        context = {
            "health": {"available": False}, "asset_performance": {"available": False},
            "maintenance_intelligence": {"available": False}, "savings_verification": {"available": False},
        }
        result = check_grounding("This pump is healthy.", context, "HEALTH_EXPLANATION")
        self.assertTrue(result.grounded)

    def test_data_health_engine_failure_marked_unavailable_never_triggers_good_conflation_check(self):
        context = self._context()
        context["data_health"] = {"available": False, "confidence_status": "UNAVAILABLE", "reason": "boom"}
        result = check_grounding("The pump is healthy.", context, "HEALTH_EXPLANATION")
        self.assertTrue(result.grounded)


# ---------------------------------------------------------------------------
# 10 - log_on_change INDETERMINATE never described as stale in rendering
# ---------------------------------------------------------------------------

class TestLogOnChangeRendering(unittest.TestCase):
    def test_indeterminate_freshness_tags_rendered_with_indeterminate_wording_not_stale(self):
        data_health = _compact(_fake_data_health("GOOD", 95.0, indeterminate_freshness_tags=["P01.WATER.WSP01.AlarmState"]))
        context = _minimal_context(data_health=data_health)
        rendered = render_equipment_context(context)
        self.assertIn("Indeterminate-freshness tags", rendered)
        self.assertNotIn("Stale tags: P01.WATER.WSP01.AlarmState", rendered)

    def test_fixed_preamble_instructs_indeterminate_freshness_is_not_stale(self):
        self.assertIn("indeterminate freshness", FIXED_PREAMBLE.lower())


# ---------------------------------------------------------------------------
# 13/14 - Data Health engine failure handling
# ---------------------------------------------------------------------------

class TestDataHealthEngineFailureHandling(unittest.TestCase):
    def test_engine_exception_never_silently_becomes_good(self):
        with patch("ai.context_builder.dhe.calculate_equipment_data_health", side_effect=RuntimeError("db unreachable")):
            context = cb.build_equipment_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "HEALTH_EXPLANATION")
        self.assertFalse(context["data_health"]["available"])
        self.assertEqual(context["data_health"]["confidence_status"], "UNAVAILABLE")
        self.assertNotEqual(context["data_health"]["confidence_status"], "GOOD")

    def test_engine_exception_does_not_crash_context_build(self):
        with patch("ai.context_builder.dhe.calculate_equipment_data_health", side_effect=RuntimeError("db unreachable")):
            context = cb.build_equipment_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "GENERAL_ENGINEERING_QUERY")
        self.assertIn("health", context)  # rest of the pipeline still ran

    def test_engine_exception_carries_a_readable_reason_without_crashing(self):
        with patch(
            "ai.context_builder.dhe.calculate_equipment_data_health",
            side_effect=RuntimeError("db unreachable"),
        ):
            context = cb.build_equipment_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "HEALTH_EXPLANATION")
        # Reaching this line already proves no crash; also confirm the
        # reason is a plain, readable string (an engineer-facing internal
        # tool, not end-user chat - the raw message is acceptable here).
        self.assertIn("could not be evaluated", context["data_health"]["reason"].lower())


# ---------------------------------------------------------------------------
# 15 - deterministic status/score cannot be overridden by model output
# ---------------------------------------------------------------------------

class TestDeterministicValueNeverOverriddenByModelText(unittest.TestCase):
    def test_structured_context_keeps_original_status_regardless_of_model_claim(self):
        engine = _engine_with_fake_provider("Data Confidence is actually GOOD for this equipment.")
        context = _minimal_context(requires_confidence=False, data_health=_compact(_fake_data_health("POOR", 30.0)))
        result = engine._render_interpretation_answer(context, "GENERAL_ENGINEERING_QUERY", "what is the rated power", [])
        self.assertEqual(result.structured_context["data_health"]["confidence_status"], "POOR")


# ---------------------------------------------------------------------------
# 16/17 - component scores and reasons/limitations correctly included
# ---------------------------------------------------------------------------

class TestContextContentCompleteness(unittest.TestCase):
    def test_component_scores_included_for_real_equipment(self):
        context = cb.build_equipment_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "HEALTH_EXPLANATION")
        data_health = context["data_health"]
        for component in ("freshness", "availability", "validity", "continuity"):
            self.assertIn(component, data_health["component_scores"])
            self.assertIn(component, data_health["component_applicability"])

    def test_reasons_and_limitations_included(self):
        context = cb.build_equipment_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "HEALTH_EXPLANATION")
        data_health = context["data_health"]
        self.assertIsInstance(data_health["reasons"], list)
        self.assertIsInstance(data_health["limitations"], list)
        self.assertGreater(len(data_health["reasons"]), 0)


# ---------------------------------------------------------------------------
# 18/19 - evaluation count per request / no plant-wide evaluation
# ---------------------------------------------------------------------------

class TestDataHealthEvaluationCount(unittest.TestCase):
    def test_evaluated_exactly_once_per_selected_equipment_context_build(self):
        with patch(
            "ai.context_builder.dhe.calculate_equipment_data_health",
            return_value=_fake_data_health("GOOD", 95.0),
        ) as mocked:
            cb.build_equipment_ai_context(CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "GENERAL_ENGINEERING_QUERY")
        self.assertEqual(mocked.call_count, 1)

    def test_no_data_health_evaluation_for_factory_summary(self):
        with patch("ai.context_builder.dhe.calculate_equipment_data_health") as mocked:
            context = cb.build_factory_ai_context(CONFIG_DB, MACHINE_DB, plant_code="p01")
        self.assertEqual(mocked.call_count, 0)
        self.assertNotIn("data_health", context)

    def test_comparison_evaluates_data_health_once_per_entity_only(self):
        with patch(
            "ai.context_builder.dhe.calculate_equipment_data_health",
            return_value=_fake_data_health("GOOD", 95.0),
        ) as mocked:
            context = cb.build_comparison_ai_context(
                CONFIG_DB, MACHINE_DB, "P01.WATER.WSP01", "P02.WATER.WSP01", intent="HEALTH_EXPLANATION",
            )
        self.assertEqual(mocked.call_count, 2)
        self.assertIn("data_health", context["entity_a"])
        self.assertIn("data_health", context["entity_b"])


def _compact(equipment_data_health):
    """Mirrors ai/context_builder.py's _data_health_domain() shape - kept
    here so grounding-guard/prompt-builder tests can hand-build the exact
    compact dict shape without a database round trip."""
    result = equipment_data_health
    return {
        "available": True,
        "confidence_status": result.confidence_status,
        "confidence_score": result.confidence_score,
        "component_scores": dict(result.component_scores),
        "component_applicability": dict(result.component_applicability),
        "required_tag_count": result.required_tag_count,
        "available_tag_count": result.available_tag_count,
        "fresh_tag_count": result.fresh_tag_count,
        "stale_tags": list(result.stale_tags),
        "missing_tags": list(result.missing_tags),
        "invalid_tags": list(result.invalid_tags),
        "indeterminate_freshness_tags": list(result.indeterminate_freshness_tags),
        "frozen_candidates": list(result.frozen_candidates),
        "gap_count": len(result.gaps),
        "timestamp_issue_count": len(result.timestamp_issues),
        "source_driver": (result.source or {}).get("configured_driver"),
        "reasons": list(result.reasons),
        "limitations": list(result.limitations),
        "as_of": result.computed_at,
    }


# ---------------------------------------------------------------------------
# Required adversarial examples - the exact 4 strings named in the
# approved Phase 16.3 plan, verbatim, each checked against the specific
# Data Health condition that should catch it.
# ---------------------------------------------------------------------------

class TestRequiredAdversarialExamplesVerbatim(unittest.TestCase):
    def _context(self, **data_health_overrides):
        data_health = _compact(_fake_data_health("GOOD", 95.0))
        data_health.update(data_health_overrides)
        return {
            "health": {"available": False}, "asset_performance": {"available": False},
            "maintenance_intelligence": {"available": False}, "savings_verification": {"available": False},
            "data_health": data_health,
            "request": {"requires_telemetry_confidence": True},
        }

    def test_the_pump_is_healthy_because_data_confidence_is_good(self):
        result = check_grounding(
            "The pump is healthy because Data Confidence is GOOD.",
            self._context(confidence_status="GOOD"), "HEALTH_EXPLANATION",
        )
        self.assertFalse(result.grounded)

    def test_the_sensor_has_failed_because_it_is_frozen(self):
        result = check_grounding(
            "The sensor has failed because it is frozen.",
            self._context(frozen_candidates=["P01.WATER.WSP01.Pressure"]), "HEALTH_EXPLANATION",
        )
        self.assertFalse(result.grounded)

    def test_the_plc_is_online_because_the_source_is_fins_udp(self):
        result = check_grounding(
            "The PLC is online because the source is FINS UDP.",
            self._context(source_driver="fins"), "HEALTH_EXPLANATION",
        )
        self.assertFalse(result.grounded)

    def test_the_compressor_is_operating_normally_when_poor_and_telemetry_dependent(self):
        result = check_grounding(
            "The compressor is operating normally.",
            self._context(confidence_status="POOR"), "HEALTH_EXPLANATION",
        )
        self.assertFalse(result.grounded)


if __name__ == "__main__":
    unittest.main()
