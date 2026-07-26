import sqlite3
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATABASE_PATH = PROJECT_ROOT / "database" / "config.db"
SCHEMA_PATH = (
    PROJECT_ROOT
    / "database"
    / "schema"
    / "001_create_config.sql"
)


def initialize_config_database() -> Path:
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(
            f"Schema file not found: {SCHEMA_PATH}"
        )

    DATABASE_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    schema_sql = SCHEMA_PATH.read_text(
        encoding="utf-8"
    )

    connection = sqlite3.connect(DATABASE_PATH)

    try:
        connection.execute(
            "PRAGMA foreign_keys = ON"
        )

        connection.executescript(schema_sql)
        connection.commit()

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()

    return DATABASE_PATH


def main() -> None:
    database_path = initialize_config_database()

    print(
        "Configuration database initialized:"
    )
    print(database_path)


if __name__ == "__main__":
    main()
