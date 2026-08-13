import sqlite3
from pathlib import Path

from config.environment import get_config_db_path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATABASE_PATH = get_config_db_path()
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


def initialize_config_database(database_path: Path | str = DATABASE_PATH) -> Path:
    database_path = Path(database_path)
    database_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    connection = sqlite3.connect(database_path)

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

    return database_path


def main() -> None:
    database_path = initialize_config_database()

    print("Configuration database initialized:")
    print(database_path)


if __name__ == "__main__":
    main()
