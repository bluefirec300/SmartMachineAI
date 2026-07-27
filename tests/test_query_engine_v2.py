from __future__ import annotations
import sqlite3, tempfile, unittest
from pathlib import Path
from engine import IndustrialQueryEngine

class EngineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "config.db"
        con = sqlite3.connect(self.db)
        con.executescript("""
        CREATE TABLE equipment(id INTEGER PRIMARY KEY,name TEXT,display_name TEXT,description TEXT);
        CREATE TABLE equipment_aliases(id INTEGER PRIMARY KEY,equipment_id INTEGER,alias TEXT);
        CREATE TABLE tags(
          id INTEGER PRIMARY KEY,equipment_id INTEGER,tag_name TEXT UNIQUE,description TEXT,
          driver TEXT,address TEXT,data_type TEXT,unit TEXT,enabled INTEGER,
          measurement TEXT,location TEXT,signal_type TEXT,event_type TEXT,threshold_type TEXT);
        CREATE TABLE tag_addresses(id INTEGER PRIMARY KEY,tag_id INTEGER,driver TEXT,address TEXT,enabled INTEGER);
        INSERT INTO equipment VALUES(1,'air_compressor_system','Air Compressor System','Factory compressor');
        INSERT INTO equipment_aliases VALUES(1,1,'compressor');
        INSERT INTO tags VALUES
          (1,1,'CompressorPressure','Compressor discharge pressure','fins','D100','INT','bar',1,
           'pressure','discharge','analog','',''),
          (2,1,'CompressorCurrent','Compressor motor current','fins','D101','INT','A',1,
           'current','motor','analog','','');
        """)
        con.commit(); con.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_pressure(self):
        result = IndustrialQueryEngine(self.db).query("What is the compressor discharge pressure?")
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.selected_tag, "CompressorPressure")

if __name__ == "__main__":
    unittest.main()
