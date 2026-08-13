from __future__ import annotations
import argparse, shutil, sqlite3
from datetime import datetime
from pathlib import Path
from config.environment import get_config_db_path
from .concept_extractor import MEASUREMENTS, LOCATIONS, EVENTS, normalize, first_match

COLUMNS = {
    "measurement": "TEXT",
    "location": "TEXT",
    "signal_type": "TEXT",
    "event_type": "TEXT",
    "threshold_type": "TEXT",
}

def infer(tag_name: str, description: str, data_type: str) -> dict[str, str]:
    text = f"{tag_name} {description}"
    measurement, _ = first_match(text, MEASUREMENTS)
    location, _ = first_match(text, LOCATIONS)
    event_type, _ = first_match(text, EVENTS)
    normalized = normalize(text)
    threshold_type = "high" if " high " in f" {normalized} " else "low" if " low " in f" {normalized} " else ""
    dt = normalize(data_type)
    signal_type = "boolean" if event_type or dt in {"bool", "boolean", "bit"} else "analog"
    return {
        "measurement": measurement,
        "location": location,
        "signal_type": signal_type,
        "event_type": event_type,
        "threshold_type": threshold_type,
    }

def migrate(database: Path, backup: bool = True) -> None:
    database = database.expanduser().resolve()
    if not database.exists():
        raise FileNotFoundError(f"Database not found: {database}")
    if backup:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = database.with_name(f"{database.stem}_before_v2_{stamp}.db")
        shutil.copy2(database, backup_path)
        print(f"Backup created: {backup_path}")

    con = sqlite3.connect(database)
    con.row_factory = sqlite3.Row
    try:
        existing = {row["name"] for row in con.execute("PRAGMA table_info(tags)")}
        for name, sql_type in COLUMNS.items():
            if name not in existing:
                con.execute(f"ALTER TABLE tags ADD COLUMN {name} {sql_type}")
                print(f"Added tags.{name}")

        rows = con.execute("""
            SELECT id, tag_name, COALESCE(description,'') description,
                   COALESCE(data_type,'') data_type,
                   COALESCE(measurement,'') measurement,
                   COALESCE(location,'') location,
                   COALESCE(signal_type,'') signal_type,
                   COALESCE(event_type,'') event_type,
                   COALESCE(threshold_type,'') threshold_type
            FROM tags ORDER BY id
        """).fetchall()

        for row in rows:
            guessed = infer(row["tag_name"], row["description"], row["data_type"])
            values = {key: (row[key] or "").strip() or guessed[key] for key in COLUMNS}
            con.execute("""
                UPDATE tags SET measurement=?, location=?, signal_type=?,
                    event_type=?, threshold_type=? WHERE id=?
            """, (
                values["measurement"], values["location"], values["signal_type"],
                values["event_type"], values["threshold_type"], row["id"],
            ))
        con.commit()
        print(f"Migration complete. Processed {len(rows)} tags.")
        for row in con.execute("""
            SELECT tag_name, measurement, location, signal_type, event_type,
                   threshold_type FROM tags ORDER BY tag_name
        """):
            print(
                f"{row['tag_name']}: measurement={row['measurement'] or '-'}, "
                f"location={row['location'] or '-'}, signal={row['signal_type'] or '-'}, "
                f"event={row['event_type'] or '-'}, threshold={row['threshold_type'] or '-'}"
            )
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--database", default=str(get_config_db_path()))
    p.add_argument("--no-backup", action="store_true")
    a = p.parse_args()
    migrate(Path(a.database), backup=not a.no_backup)

if __name__ == "__main__":
    main()
