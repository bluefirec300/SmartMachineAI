import sqlite3
from collections import defaultdict
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "database" / "machine_data.db"


def get_machine_history(tags=None, limit=10):
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    try:
        cursor = connection.cursor()

        parameters = []
        tag_filter = ""

        if tags:
            placeholders = ",".join("?" for _ in tags)
            tag_filter = f"WHERE tag IN ({placeholders})"
            parameters.extend(tags)

        parameters.append(limit)

        query = f"""
            SELECT time, tag, address, value
            FROM (
                SELECT
                    time,
                    tag,
                    address,
                    value,
                    ROW_NUMBER() OVER (
                        PARTITION BY tag
                        ORDER BY time DESC
                    ) AS row_number
                FROM plc_data
                {tag_filter}
            )
            WHERE row_number <= ?
            ORDER BY tag, time ASC
        """

        cursor.execute(query, parameters)

        history = defaultdict(list)

        for row in cursor.fetchall():
            history[row["tag"]].append(dict(row))

        return dict(history)

    finally:
        connection.close()
