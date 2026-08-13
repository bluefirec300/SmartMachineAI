import json
import tempfile
import time
import unittest
from pathlib import Path

from ui.scada_snapshot_writer import _write_snapshot_atomic, build_snapshot


class TestBuildSnapshot(unittest.TestCase):
    def test_build_snapshot_has_both_plants_and_generated_at(self):
        snapshot = build_snapshot()
        self.assertIn("generated_at", snapshot)
        self.assertIn("plants", snapshot)
        self.assertEqual(set(snapshot["plants"].keys()), {"p01", "p02"})
        for plant_data in snapshot["plants"].values():
            self.assertIn("board", plant_data)
            self.assertIn("equipment", plant_data)

    def test_build_snapshot_is_json_serializable(self):
        snapshot = build_snapshot()
        # Raises if anything non-serializable (e.g. a raw sqlite3.Row) slipped through.
        json.dumps(snapshot)


class TestWriteSnapshotAtomic(unittest.TestCase):
    def test_writes_valid_json_readable_at_target_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "_scada_live.json"
            _write_snapshot_atomic({"generated_at": "2026-08-13 10:00:00", "plants": {}}, target)

            self.assertTrue(target.exists())
            with open(target) as handle:
                loaded = json.load(handle)
            self.assertEqual(loaded["generated_at"], "2026-08-13 10:00:00")

    def test_no_temp_file_left_behind_after_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "_scada_live.json"
            _write_snapshot_atomic({"generated_at": "x", "plants": {}}, target)

            remaining = list(Path(temp_dir).iterdir())
            self.assertEqual(remaining, [target])

    def test_overwrites_existing_file_cleanly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "_scada_live.json"
            _write_snapshot_atomic({"generated_at": "first", "plants": {}}, target)
            _write_snapshot_atomic({"generated_at": "second", "plants": {}}, target)

            with open(target) as handle:
                loaded = json.load(handle)
            self.assertEqual(loaded["generated_at"], "second")


if __name__ == "__main__":
    unittest.main()
