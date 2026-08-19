import random
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from database.database import DatabaseManager
from engine import performance_migrator as pm
from engine.maintenance_migrator import migrate as migrate_maintenance
from engine.performance_orchestration import run_cycle
from streamlit.testing.v1 import AppTest

from tests.test_baseline_engine import _insert_tag, _seed_config_db, _seed_series

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PAGE = str(PROJECT_ROOT / "ui" / "pages" / "20_Asset_Performance.py")

NOW = datetime(2026, 9, 20, 0, 0, 0)


class AssetPerformanceUITestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_db = Path(self.temp_dir.name) / "config.db"
        self.machine_db = Path(self.temp_dir.name) / "machine_data.db"
        _seed_config_db(self.config_db)
        pm.migrate(self.config_db, backup=False)
        migrate_maintenance(self.config_db, backup=False)
        self.historian = DatabaseManager(db_path=self.machine_db)
        _insert_tag(self.config_db, "P01.UTILITY.CHL01.Power_kW", unit="kW", measurement="power")

        rng = random.Random(41)
        points = []
        for day in range(14):
            points.append((NOW - timedelta(days=20 - day, hours=-10), 60.0 + rng.uniform(-1, 1)))
        for day in range(5):
            points.append((NOW - timedelta(days=5 - day, hours=-10), 90.0 + rng.uniform(-1, 1)))  # clear degradation
        _seed_series(self.historian, "P01.UTILITY.CHL01.Power_kW", points)

        run_cycle(self.config_db, self.machine_db, now=NOW)

        self._patches = [
            patch("ui.asset_performance_data.CONFIG_DATABASE_PATH", str(self.config_db)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.temp_dir.cleanup()

    def _run(self):
        at = AppTest.from_file(PAGE, default_timeout=60)
        at.run()
        return at


class TestAssetPerformancePageRenders(AssetPerformanceUITestBase):
    def test_page_loads_without_exception(self):
        at = self._run()
        self.assertFalse(bool(at.exception))

    def test_attention_ranking_table_present(self):
        at = self._run()
        self.assertGreaterEqual(len(at.dataframe), 1)

    def test_degrading_equipment_selectable_in_detail_tab(self):
        at = self._run()
        detail_select = [sb for sb in at.selectbox if sb.key == "ap_detail_select"]
        self.assertTrue(detail_select)
        sb = detail_select[0]
        self.assertTrue(sb.options)
        sb.set_value(sb.options[0])
        at.run()
        self.assertFalse(bool(at.exception))


if __name__ == "__main__":
    unittest.main()
