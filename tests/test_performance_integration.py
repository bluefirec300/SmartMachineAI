import unittest
from pathlib import Path

"""
Phase 14 sentinel / source-inspection tests (item 44) - protects the
architectural boundaries the approval explicitly required, the same
pattern established by tests/test_health_integration.py.
"""

PHASE_14_FILES = (
    "engine/performance_targets.py",
    "engine/performance_evidence.py" if Path("engine/performance_evidence.py").exists() else None,
    "engine/performance_engine.py",
    "engine/performance_domain.py",
    "engine/performance_orchestration.py",
    "engine/performance_ranking.py",
    "engine/performance_migrator.py",
    "app/asset_performance_worker.py",
    "ui/asset_performance_data.py",
    "ui/pages/20_Asset_Performance.py",
)
PHASE_14_FILES = tuple(f for f in PHASE_14_FILES if f)


def _source_without_module_docstring(path: str) -> str:
    """Strips the leading module docstring before scanning for banned
    call-site patterns - a well-established recurring false-positive
    class in this project's own test suite (the docstring legitimately
    explains what a module does NOT do)."""
    text = Path(path).read_text()
    marker = '"""'
    first = text.find(marker)
    second = text.find(marker, first + 3)
    if first == -1 or second == -1:
        return text
    return text[:first] + text[second + 3:]


class TestNoLLMOrAIProvider(unittest.TestCase):
    def test_no_phase14_file_imports_llm_modules(self):
        banned = ("ai_provider", "ai.prompt_builder", "ai.providers", "ai.root_cause_engine", "from ai import")
        for path in PHASE_14_FILES:
            source = _source_without_module_docstring(path)
            for token in banned:
                self.assertNotIn(token, source, f"{path} references banned LLM module {token!r}")


class TestNoPredictiveOrRULLanguage(unittest.TestCase):
    def test_no_predictive_or_rul_terms_in_engine_or_ui_source(self):
        # Deliberately narrow to terms that would only appear as an
        # actual predictive CLAIM, never as this codebase's own
        # established "no forecast/no extrapolation" disclaimer prose
        # (which legitimately contains "forecast"/"extrapolat" as a
        # denial - a bare substring ban on those words would self-trip,
        # the same recurring false-positive class documented in CLAUDE.md).
        banned = (
            "remaining useful life", "predicted failure", "failure probability", "predict(",
            "expected breakdown", "will fail",
        )
        for path in PHASE_14_FILES:
            source = _source_without_module_docstring(path).lower()
            for token in banned:
                self.assertNotIn(token, source, f"{path} contains banned predictive/RUL term {token!r}")

    def test_no_forecast_or_extrapolation_claims_only_denials(self):
        """Narrower check: 'forecast'/'extrapolat' may appear ONLY inside
        a sentence that denies doing it ('no forecast', 'never
        extrapolate') - never as an affirmative claim."""
        for path in PHASE_14_FILES:
            source = _source_without_module_docstring(path).lower()
            for token in ("forecast", "extrapolat"):
                idx = source.find(token)
                while idx != -1:
                    window = source[max(0, idx - 20):idx]
                    self.assertTrue(
                        any(denial in window for denial in ("no ", "never ", "not a ", "not do")),
                        f"{path}: {token!r} at position {idx} does not appear inside a denial phrase - context: ...{source[max(0, idx-40):idx+40]}...",
                    )
                    idx = source.find(token, idx + 1)

    def test_no_mtbf_or_mttr_anywhere(self):
        for path in PHASE_14_FILES:
            source = _source_without_module_docstring(path).lower()
            self.assertNotIn("mtbf", source, f"{path} references MTBF")
            self.assertNotIn("mttr", source, f"{path} references MTTR")


class TestNoConsolidatedPerformanceScore(unittest.TestCase):
    def test_no_asset_performance_score_field_or_function(self):
        """item J.5 - explicitly rejected: no consolidated 0-100 asset
        performance score competing with Health Score."""
        banned = ("asset_performance_score", "performance_score =", "def compute_performance_score")
        for path in PHASE_14_FILES:
            source = _source_without_module_docstring(path).lower()
            for token in banned:
                self.assertNotIn(token, source, f"{path} appears to implement a banned consolidated performance score")


class TestPhase12And13Unchanged(unittest.TestCase):
    """Phase 14 must only ever READ Phase 12/13 outputs, never call their
    calculation entry points, and must never modify their files."""

    def test_performance_engine_never_calls_health_or_maintenance_calculation(self):
        source = _source_without_module_docstring("engine/performance_engine.py")
        self.assertNotIn("calculate_health(", source)
        self.assertNotIn("calculate_maintenance_priority(", source)

    def test_performance_ranking_never_calls_health_or_maintenance_calculation(self):
        source = _source_without_module_docstring("engine/performance_ranking.py")
        self.assertNotIn("calculate_health(", source)
        self.assertNotIn("calculate_maintenance_priority(", source)

    def test_no_phase14_file_writes_to_health_or_maintenance_tables(self):
        banned = (
            "INSERT INTO equipment_health_snapshots", "INSERT INTO equipment_health_factor_snapshots",
            "UPDATE equipment_health_snapshots", "INSERT INTO maintenance_log", "UPDATE maintenance_log",
        )
        for path in PHASE_14_FILES:
            source = _source_without_module_docstring(path)
            for token in banned:
                self.assertNotIn(token, source, f"{path} appears to write to a Phase 12/13 table")


class TestNoAutomaticWorkOrdersOrControlWrites(unittest.TestCase):
    def test_no_plc_or_control_write_calls(self):
        banned = ("write_tag(", "set_setpoint(", "plc.write", "modbus_driver.write", "opcua_driver.write")
        for path in PHASE_14_FILES:
            source = _source_without_module_docstring(path)
            for token in banned:
                self.assertNotIn(token, source, f"{path} appears to perform a PLC/control write")

    def test_no_automatic_work_order_creation(self):
        for path in PHASE_14_FILES:
            source = _source_without_module_docstring(path).lower()
            self.assertNotIn("work_order", source, f"{path} references work orders")


class TestElectricalLoadNotSilentlyExcluded(unittest.TestCase):
    """Unlike Phase 13 (which deliberately excludes ELECTRICAL_LOAD-family
    factors from maintenance recommendations), Phase 14 is a broader
    performance-analytics layer where Power_kW dimensions ARE legitimate
    performance evidence (energy efficiency). This test documents/
    confirms that distinction is intentional, not an oversight."""

    def test_power_kw_dimensions_have_a_real_direction_not_informational(self):
        from engine.performance_targets import INFORMATIONAL_ONLY, PERFORMANCE_DIMENSION_REGISTRY
        power_dimensions = [d for d in PERFORMANCE_DIMENSION_REGISTRY.values() if d.target_key in ("Power_kW", "MotorPower", "FanPower", "CompressorPower")]
        self.assertTrue(power_dimensions)
        for dimension in power_dimensions:
            self.assertNotEqual(dimension.direction, INFORMATIONAL_ONLY)


if __name__ == "__main__":
    unittest.main()
