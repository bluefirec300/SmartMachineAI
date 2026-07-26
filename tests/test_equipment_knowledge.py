import unittest

from ai.equipment_knowledge import EquipmentKnowledge


class TestEquipmentKnowledge(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.knowledge = EquipmentKnowledge()

    def test_compressor_aliases(self):
        questions = [
            "How is the air system?",
            "Show compressed air system status.",
        ]

        for question in questions:
            with self.subTest(question=question):
                self.assertEqual(
                    self.knowledge.find_equipment(question),
                    "compressor",
                )

    def test_chiller_aliases(self):
        questions = [
            "Is the cooling system healthy?",
            "What is the chilled water temperature?",
            "Show chilled water system status.",
        ]

        for question in questions:
            with self.subTest(question=question):
                self.assertEqual(
                    self.knowledge.find_equipment(question),
                    "chiller",
                )

    def test_cold_room_aliases(self):
        questions = [
            "How is the cold storage area?",
            "Is the refrigerated room too warm?",
        ]

        for question in questions:
            with self.subTest(question=question):
                self.assertEqual(
                    self.knowledge.find_equipment(question),
                    "cold_room",
                )


if __name__ == "__main__":
    unittest.main()

