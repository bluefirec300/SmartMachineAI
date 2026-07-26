import unittest

from ai.semantic_router import SemanticRouter


class FakeAIProvider:

    def __init__(self, response: str):
        self.response = response
        self.last_prompt = ""

    def generate(self, prompt: str) -> str:
        self.last_prompt = prompt
        return self.response


class TestSemanticRouter(unittest.TestCase):

    def create_router(
        self,
        response: str,
    ) -> SemanticRouter:
        ai = FakeAIProvider(response)

        return SemanticRouter(
            ai=ai,
            allowed_equipment={
                "factory",
                "compressor",
                "chiller",
                "cold room",
            },
            allowed_measurements={
                "pressure",
                "temperature",
                "current",
                "humidity",
                "alarm",
                "running",
            },
        )

    def test_valid_semantic_result(self):
        router = self.create_router(
            """
            {
              "equipment": "compressor",
              "measurements": ["temperature"],
              "intent": "current"
            }
            """
        )

        result = router.interpret(
            "Is the air system getting hot?"
        )

        self.assertEqual(
            result,
            {
                "equipment": "compressor",
                "measurements": ["temperature"],
                "intent": "current",
            },
        )

    def test_json_code_block_is_accepted(self):
        router = self.create_router(
            """
            ```json
            {
              "equipment": "cold room",
              "measurements": ["humidity"],
              "intent": "trend"
            }
            ```
            """
        )

        result = router.interpret(
            "Has the refrigerated room become more humid?"
        )

        self.assertEqual(
            result["equipment"],
            "cold room",
        )
        self.assertEqual(
            result["measurements"],
            ["humidity"],
        )
        self.assertEqual(
            result["intent"],
            "trend",
        )

    def test_unknown_equipment_is_rejected(self):
        router = self.create_router(
            """
            {
              "equipment": "unknown boiler",
              "measurements": ["temperature"],
              "intent": "current"
            }
            """
        )

        result = router.interpret(
            "What is the boiler temperature?"
        )

        self.assertIsNone(result)

    def test_unknown_measurement_is_rejected(self):
        router = self.create_router(
            """
            {
              "equipment": "compressor",
              "measurements": ["vibration magic"],
              "intent": "current"
            }
            """
        )

        result = router.interpret(
            "Is the compressor shaking?"
        )

        self.assertIsNone(result)

    def test_invalid_json_is_rejected(self):
        router = self.create_router(
            "The compressor appears normal."
        )

        result = router.interpret(
            "How is the compressor?"
        )

        self.assertIsNone(result)

    def test_string_measurement_is_normalized(self):
        router = self.create_router(
            """
            {
              "equipment": "chiller",
              "measurements": "temperature",
              "intent": "current"
            }
            """
        )

        result = router.interpret(
            "How cold is the cooling system?"
        )

        self.assertEqual(
            result["measurements"],
            ["temperature"],
        )


if __name__ == "__main__":
    unittest.main()
