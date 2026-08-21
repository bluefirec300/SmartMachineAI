import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

"""
Phase V1.3 - the 3 remaining synthetic equipment manuals (Eaton 9395,
Donaldson Torit, IMA filling line) are now visibly marked on the
Documentation page. Real filenames from the live database are used
directly below rather than invented ones, since the detection logic
depends on the actual "_REAL" filename convention already established
by the document-lookup phase.
"""

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOC_PAGE = str(PROJECT_ROOT / "ui" / "pages" / "8_Documentation.py")

REAL_FILENAMES = (
    "manuals/Atlas_Copco_GA30_Elektronikon_II_user_manual_REAL.pdf",
    "manuals/real/Bitzer_KB-100-2_manual_REAL.pdf",
    "manuals/real/Cummins_generator_manual_REAL.pdf",
)
SYNTHETIC_FILENAMES = (
    "manuals/Donaldson_Torit_dust_collector_Industrial_dust_collector.pdf",
    "manuals/Eaton_9395_UPS_Three-phase_online_UPS.pdf",
    "manuals/IMA_Filling_line_Automated_filling_packaging_line.pdf",
)


def _load_detector():
    # Importing the page module directly would execute Streamlit calls
    # at import time (st.title(), st.set_page_config() equivalents) -
    # exec the function definition in isolation instead, matching how
    # a plain, Streamlit-free unit test should exercise pure logic.
    source = Path(DOC_PAGE).read_text(encoding="utf-8")
    start = source.index("def _is_synthetic_reference_manual")
    end = source.index("\n\n\n", start)
    namespace = {"Path": Path}
    exec(source[start:end], namespace)
    return namespace["_is_synthetic_reference_manual"]


class TestSyntheticDetectionLogic(unittest.TestCase):
    def setUp(self):
        self.is_synthetic = _load_detector()

    def test_all_three_known_synthetic_docs_are_flagged(self):
        for file_path in SYNTHETIC_FILENAMES:
            with self.subTest(file_path=file_path):
                doc = {"equipment_id": None, "file_path": file_path}
                self.assertTrue(self.is_synthetic(doc))

    def test_real_sourced_manuals_are_never_flagged(self):
        for file_path in REAL_FILENAMES:
            with self.subTest(file_path=file_path):
                doc = {"equipment_id": None, "file_path": file_path}
                self.assertFalse(self.is_synthetic(doc))

    def test_site_uploaded_documents_are_never_flagged_regardless_of_filename(self):
        # A real engineer's own uploaded photo/file - equipment_id is
        # set, must never be mistaken for a synthetic reference manual
        # no matter what its filename happens to contain.
        doc = {"equipment_id": 7, "file_path": "manuals/uploaded/some_photo_without_real_in_it.png"}
        self.assertFalse(self.is_synthetic(doc))

    def test_missing_file_path_does_not_crash(self):
        doc = {"equipment_id": None, "file_path": None}
        self.assertTrue(self.is_synthetic(doc))  # no "REAL" present -> honestly flagged, not guessed


class TestDocumentationPageRendersSyntheticWarning(unittest.TestCase):
    def test_page_loads_without_exception(self):
        at = AppTest.from_file(DOC_PAGE, default_timeout=30)
        at.run()
        self.assertFalse(bool(at.exception))

    def test_page_caption_mentions_synthetic_placeholder_convention(self):
        at = AppTest.from_file(DOC_PAGE, default_timeout=30)
        at.run()
        captions = " ".join(c.value for c in at.caption)
        self.assertIn("Synthetic placeholder", captions)


if __name__ == "__main__":
    unittest.main()
