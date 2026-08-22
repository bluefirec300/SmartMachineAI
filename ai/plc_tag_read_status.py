import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DATABASE_PATH = (
    PROJECT_ROOT
    / "database"
    / "machine_data.db"
)


class PlcTagReadStatus:
    """
    Phase V2.8 - per-tag PLC read health, distinct from Data Health.

    Data Health (engine/data_health_engine.py) answers "is this tag's
    DATA fresh" - it can't distinguish "the whole PLC connection is
    down" from "this one tag's address is misconfigured while every
    other tag reads fine", because both look identical from the
    historian's point of view (no new plc_data row). This table
    answers a narrower, different question app/plc_logger.py's read
    loop is the only place that actually knows: did THIS SPECIFIC
    TAG's read attempt itself succeed or fail this cycle, independent
    of whether a successful read was even due to be logged.

    Deliberately does NOT store the per-tag error message - that would
    require changing what each protocol driver's read_all() returns
    (currently: silently omits a failed tag from its result list,
    printing the reason rather than returning it), and "don't redesign
    drivers that already work" (Phase V1.4's explicit instruction,
    reaffirmed for this phase) rules that out. Tracking WHICH tags are
    failing and HOW OFTEN is already substantial, real commissioning
    value on its own - an admin who sees a specific tag with 40
    consecutive failures knows exactly where to look (Test Connection/
    Browse on that address), even without the exact exception text.

    Only ever contains rows for tags that have failed at least once
    since this table was created - a tag that has never failed never
    gets a row at all, keeping write volume proportional to actual
    problems in a healthy system, not to total tag count (up to 625
    tags at a 2-second scan interval would make a write-every-cycle-
    regardless-of-outcome design needlessly expensive).

    Lives in the historian database (machine_data.db), same as
    machine_events/notification_log - this is per-environment
    operational state, not configuration. Since Simulation and Actual
    are already separate machine_data.db files, a given process can
    only ever read/write the tag-read status for the one environment
    it's actually running against - there is no code path here that
    could mix the two.
    """

    def __init__(
        self,
        database_path: Path | str = DEFAULT_DATABASE_PATH,
    ):
        self.database_path = Path(
            database_path
        ).resolve()

        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path
        )

        connection.row_factory = sqlite3.Row

        return connection

    def _initialize_database(self) -> None:
        self.database_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS plc_tag_read_status
                (
                    tag_name TEXT PRIMARY KEY,
                    last_success_at TEXT,
                    last_failure_at TEXT NOT NULL,
                    consecutive_failures INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def _timestamp(when: datetime) -> str:
        return when.strftime("%Y-%m-%d %H:%M:%S")

    def record_cycle(
        self,
        failed_tag_names: set[str],
        recovered_tag_names: set[str],
        when: datetime,
    ) -> None:
        """
        Called once per app/plc_logger.py read cycle. `failed_tag_names`
        is every tag whose read attempt failed THIS cycle (its name was
        requested but absent from driver.read_all()'s result) -
        upserted with an incremented failure streak. `recovered_tag_names`
        is every tag that succeeded this cycle AND has an existing row
        here (i.e. it failed at least once before) - marked with a
        fresh last_success_at and consecutive_failures reset to 0, but
        the row is kept (not deleted) so the history of "this tag has
        failed before" stays visible even after it recovers.

        A tag that succeeds and has never failed needs no call here at
        all - the caller is expected to only pass tags that actually
        need a status change, keeping this a small, cheap write even
        at hundreds of tags per cycle.
        """
        if not failed_tag_names and not recovered_tag_names:
            return

        timestamp = self._timestamp(when)

        with self._connect() as connection:
            for tag_name in failed_tag_names:
                connection.execute(
                    """
                    INSERT INTO plc_tag_read_status
                    (tag_name, last_success_at, last_failure_at, consecutive_failures, updated_at)
                    VALUES (?, NULL, ?, 1, ?)
                    ON CONFLICT (tag_name) DO UPDATE SET
                        last_failure_at = excluded.last_failure_at,
                        consecutive_failures = consecutive_failures + 1,
                        updated_at = excluded.updated_at
                    """,
                    (tag_name, timestamp, timestamp),
                )

            for tag_name in recovered_tag_names:
                connection.execute(
                    """
                    UPDATE plc_tag_read_status
                    SET last_success_at = ?, consecutive_failures = 0, updated_at = ?
                    WHERE tag_name = ?
                    """,
                    (timestamp, timestamp, tag_name),
                )

    def get_all(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM plc_tag_read_status ORDER BY consecutive_failures DESC, tag_name"
            ).fetchall()

        return [dict(row) for row in rows]

    def get(self, tag_name: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM plc_tag_read_status WHERE tag_name = ?", (tag_name,)
            ).fetchone()

        return dict(row) if row else None
