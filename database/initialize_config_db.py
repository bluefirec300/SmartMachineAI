import sqlite3
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATABASE_PATH = PROJECT_ROOT / "database" / "config.db"
SCHEMA_DIRECTORY = PROJECT_ROOT / "database" / "schema"


def get_schema_files() -> list[Path]:
    if not SCHEMA_DIRECTORY.exists():
        raise FileNotFoundError(
            f"Schema directory not found: {SCHEMA_DIRECTORY}"
        )

    schema_files = sorted(
        SCHEMA_DIRECTORY.glob("*.sql")
    )

    if not schema_files:
        raise FileNotFoundError(
            f"No SQL schema files found in: {SCHEMA_DIRECTORY}"
        )

    return schema_files


def initialize_config_database() -> Path:
    DATABASE_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    connection = sqlite3.connect(DATABASE_PATH)

    try:
        connection.execute("PRAGMA foreign_keys = ON")

        for schema_file in get_schema_files():
            print(f"Applying: {schema_file.name}")

            schema_sql = schema_file.read_text(
                encoding="utf-8"
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

    print("Configuration database initialized:")
    print(database_path)


if __name__ == "__main__":
    main()
