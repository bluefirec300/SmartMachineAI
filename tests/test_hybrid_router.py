import unittest
from unittest.mock import patch

from ai.hybrid_router import HybridRouter


class FakeSemanticRouter:
    """
    Predictable semantic router used without calling Ollama.
    """

    def __init__(self, result):
        self.result = result
        self.called = False
        self.last_question = None

    def interpret(self, question: str):
        self.called = True
        self.last_question = question
        return self.result


class TestHybridRouter(unittest.TestCase):

    @patch(
        "ai.hybrid_router.select_tags",
        return_value=["CompressorPressure"],
    )
    def test_clear_equipment_uses_rules(
        self,
        mock_select_tags,
    ):
        semantic_router = FakeSemanticRouter(
            {
                "equipment": "chiller",
                "measurements": ["temperature"],
                "intent": "current",
            }
        )

        router = HybridRouter(semantic_router)

        result = router.route(
            "What is the compressor pressure?"
        )

        self.assertEqual(
            result["equipment"],
            "compressor",
        )
        self.assertEqual(
            result["measurements"],
            ["pressure"],
        )
        self.assertEqual(
            result["intent"],
            "current",
        )
        self.assertEqual(
            result["history_limit"],
            1,
        )
        self.assertEqual(
            result["route_source"],
            "rules",
        )
        self.assertEqual(
            result["tags"],
            ["CompressorPressure"],
        )

        self.assertFalse(
            semantic_router.called
        )

        mock_select_tags.assert_called_once_with(
            question="What is the compressor pressure?",
            equipment="compressor",
            intent="current",
            measurements={"pressure"},
        )

    @patch(
        "ai.hybrid_router.select_tags",
        return_value=["CompressorTemperature"],
    )
    def test_ambiguous_question_uses_semantic_router(
        self,
        mock_select_tags,
    ):
        semantic_router = FakeSemanticRouter(
            {
                "equipment": "compressor",
                "measurements": ["temperature"],
                "intent": "trend",
            }
        )

        router = HybridRouter(semantic_router)

        question = "Is the air supply getting hotter?"

        result = router.route(question)

        self.assertTrue(
            semantic_router.called
        )
        self.assertEqual(
            semantic_router.last_question,
            question,
        )

        self.assertEqual(
            result["equipment"],
            "compressor",
        )
        self.assertEqual(
            result["measurements"],
            ["temperature"],
        )
        self.assertEqual(
            result["intent"],
            "trend",
        )
        self.assertEqual(
            result["history_limit"],
            20,
        )
        self.assertEqual(
            result["route_source"],
            "semantic",
        )
        self.assertEqual(
            result["tags"],
            ["CompressorTemperature"],
        )

        mock_select_tags.assert_called_once_with(
            question=question,
            equipment="compressor",
            intent="trend",
            measurements={"temperature"},
        )

    @patch(
        "ai.hybrid_router.select_tags",
        return_value=None,
    )
    def test_invalid_semantic_result_uses_rule_fallback(
        self,
        mock_select_tags,
    ):
        semantic_router = FakeSemanticRouter(None)
        router = HybridRouter(semantic_router)

        question = "Tell me what is happening here."

        result = router.route(question)

        self.assertTrue(
            semantic_router.called
        )
        self.assertEqual(
            result["equipment"],
            "factory",
        )
        self.assertEqual(
            result["measurements"],
            [],
        )
        self.assertEqual(
            result["intent"],
            "current",
        )
        self.assertEqual(
            result["history_limit"],
            1,
        )
        self.assertEqual(
            result["route_source"],
            "rules_fallback",
        )
        self.assertIsNone(
            result["tags"]
        )

        mock_select_tags.assert_called_once_with(
            question=question,
            equipment="factory",
            intent="current",
            measurements=set(),
        )

    @patch(
        "ai.hybrid_router.select_tags",
        return_value=["FactoryAlarm"],
    )
    def test_explicit_factory_question_uses_rules(
        self,
        mock_select_tags,
    ):
        semantic_router = FakeSemanticRouter(
            {
                "equipment": "compressor",
                "measurements": ["alarm"],
                "intent": "status",
            }
        )

        router = HybridRouter(semantic_router)

        question = "Is anything abnormal in the whole plant?"

        result = router.route(question)

        self.assertFalse(
            semantic_router.called
        )
        self.assertEqual(
            result["equipment"],
            "factory",
        )
        self.assertEqual(
            result["intent"],
            "status",
        )
        self.assertEqual(
            result["history_limit"],
            10,
        )
        self.assertEqual(
            result["route_source"],
            "rules",
        )

    @patch(
        "ai.hybrid_router.select_tags",
        return_value=["ColdRoomHumidity"],
    )
    def test_semantic_measurements_are_deduplicated(
        self,
        mock_select_tags,
    ):
        semantic_router = FakeSemanticRouter(
            {
                "equipment": "cold room",
                "measurements": [
                    "humidity",
                    "humidity",
                ],
                "intent": "current",
            }
        )

        router = HybridRouter(semantic_router)

        result = router.route(
            "Does the storage area feel damp?"
        )

        self.assertEqual(
            result["measurements"],
            ["humidity"],
        )

        mock_select_tags.assert_called_once_with(
            question="Does the storage area feel damp?",
            equipment="cold room",
            intent="current",
            measurements={"humidity"},
        )


if __name__ == "__main__":
    unittest.main()
