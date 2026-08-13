from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from ai.event_engine import EventEngine
from ai.event_store import EventStore
from ai.rule_engine import RuleEngine
from config.environment import get_config_db_path, get_machine_db_path


MACHINE_DATABASE_PATH = get_machine_db_path()

CONFIG_DATABASE_PATH = get_config_db_path()

POLL_INTERVAL_SECONDS = 2.0


class BackgroundEventMonitor:
    """
    Continuously evaluates the newest PLC/historian values.

    Processing:

        plc_data
            ↓
        latest value for each tag
            ↓
        RuleEngine
            ↓
        EventEngine
            ↓
        EventStore
            ↓
        machine_events

    Only changes in abnormal state are stored.

    Example:

        normal
        → low_warning     STORE
        → low_warning     ignore
        → low_alarm       STORE
        → low_alarm       ignore
        → normal          clear internal state

    This prevents one persistent alarm from creating hundreds
    of duplicate machine_events records.
    """

    def __init__(
        self,
        machine_database_path: str | Path = MACHINE_DATABASE_PATH,
        config_database_path: str | Path = CONFIG_DATABASE_PATH,
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self.machine_database_path = Path(
            machine_database_path
        ).resolve()

        self.config_database_path = Path(
            config_database_path
        ).resolve()

        self.poll_interval = float(
            poll_interval
        )

        if self.poll_interval <= 0:
            raise ValueError(
                "poll_interval must be greater than zero."
            )

        if not self.machine_database_path.exists():
            raise FileNotFoundError(
                "Machine historian database not found: "
                f"{self.machine_database_path}"
            )

        if not self.config_database_path.exists():
            raise FileNotFoundError(
                "Configuration database not found: "
                f"{self.config_database_path}"
            )

        self.rule_engine = RuleEngine(
            database_path=self.config_database_path,
        )

        self.event_engine = EventEngine(
            database_path=self.config_database_path,
        )

        self.event_store = EventStore(
            database_path=self.machine_database_path,
        )

        # Last abnormal state seen for each tag.
        #
        # Example:
        #
        # {
        #     "CompressorPressure":
        #         ("warning", "low_warning")
        # }
        #
        self.active_states: dict[
            str,
            tuple[str, str],
        ] = {}

        self.cycle_count = 0
        self.event_count = 0

    def _connect_machine_database(
        self,
    ) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.machine_database_path)
        )

        connection.row_factory = sqlite3.Row

        return connection

    def _load_tag_units(
        self,
    ) -> dict[str, str]:
        """
        Read engineering units from config.db.
        """
        connection = sqlite3.connect(
            str(self.config_database_path)
        )

        connection.row_factory = sqlite3.Row

        try:
            rows = connection.execute(
                """
                SELECT
                    tag_name,
                    COALESCE(unit, '') AS unit
                FROM tags
                WHERE enabled = 1
                """
            ).fetchall()

        finally:
            connection.close()

        return {
            str(row["tag_name"]): str(
                row["unit"] or ""
            )
            for row in rows
        }

    def _latest_values(
        self,
    ) -> list[dict[str, Any]]:
        """
        Get the newest plc_data record for every tag.

        If several tags share exactly the same timestamp this
        still returns one newest record for each individual tag.
        """
        query = """
            SELECT
                p.time,
                p.tag,
                p.address,
                p.value
            FROM plc_data AS p

            INNER JOIN
            (
                SELECT
                    tag,
                    MAX(rowid) AS latest_rowid
                FROM plc_data
                GROUP BY tag
            ) AS latest

                ON latest.latest_rowid = p.rowid

            ORDER BY p.tag
        """

        with self._connect_machine_database() as connection:
            rows = connection.execute(
                query
            ).fetchall()

        units = self._load_tag_units()

        summaries: list[
            dict[str, Any]
        ] = []

        for row in rows:
            tag_name = str(
                row["tag"] or ""
            ).strip()

            if not tag_name:
                continue

            value = row["value"]

            summaries.append(
                {
                    "tag": tag_name,
                    "address": str(
                        row["address"] or ""
                    ),
                    "current": value,
                    "updated": str(
                        row["time"] or ""
                    ),
                    "unit": units.get(
                        tag_name,
                        "",
                    ),

                    # EventEngine supports trend information.
                    # For real-time monitoring we are evaluating
                    # the latest point, so these fields represent
                    # that latest point only.
                    "trend": "current",
                    "samples": 1,
                    "minimum": value,
                    "maximum": value,
                    "average": value,
                    "change": 0.0,
                }
            )

        return summaries

    @staticmethod
    def _state_key(
        rule_result: dict[str, Any],
    ) -> tuple[str, str]:
        severity = str(
            rule_result.get(
                "severity",
                "",
            )
            or ""
        ).strip().lower()

        condition = str(
            rule_result.get(
                "condition",
                "",
            )
            or ""
        ).strip().lower()

        return (
            severity,
            condition,
        )

    @staticmethod
    def _is_abnormal(
        rule_result: dict[str, Any],
    ) -> bool:
        severity = str(
            rule_result.get(
                "severity",
                "",
            )
            or ""
        ).strip().lower()

        return severity in {
            "warning",
            "alarm",
            "critical",
            "high",
        }

    def _process_results(
        self,
        summaries: list[dict[str, Any]],
        rule_results: list[dict[str, Any]],
    ) -> int:
        """
        Store only new abnormal-state transitions.
        """
        summaries_by_tag = {
            str(summary["tag"]): summary
            for summary in summaries
        }

        inserted_count = 0

        for rule_result in rule_results:
            tag_name = str(
                rule_result.get(
                    "tag",
                    "",
                )
                or ""
            ).strip()

            if not tag_name:
                continue

            if not self._is_abnormal(
                rule_result
            ):
                # Tag returned to normal.
                self.active_states.pop(
                    tag_name,
                    None,
                )

                continue

            new_state = self._state_key(
                rule_result
            )

            previous_state = (
                self.active_states.get(
                    tag_name
                )
            )

            if previous_state == new_state:
                # Alarm is still active.
                # Do not keep inserting it.
                continue

            summary = summaries_by_tag.get(
                tag_name
            )

            event = self.event_engine.build_event(
                rule_result=rule_result,
                summary=summary,
            )

            if event is None:
                continue

            event_id = (
                self.event_store.insert_event(
                    event
                )
            )

            self.active_states[
                tag_name
            ] = new_state

            if event_id is None:
                continue

            inserted_count += 1
            self.event_count += 1

            print(
                "EVENT STORED | "
                f"id={event_id} | "
                f"equipment={event.get('equipment')} | "
                f"tag={event.get('tag')} | "
                f"severity={event.get('severity')} | "
                f"condition={event.get('condition')} | "
                f"value={event.get('value')}",
                flush=True,
            )

        return inserted_count

    def run_cycle(
        self,
    ) -> int:
        self.cycle_count += 1

        summaries = self._latest_values()

        if not summaries:
            print(
                "No historian values available.",
                flush=True,
            )

            return 0

        rule_results = (
            self.rule_engine.evaluate(
                summaries
            )
        )

        return self._process_results(
            summaries=summaries,
            rule_results=rule_results,
        )

    def run_forever(
        self,
    ) -> None:
        print(
            "MMG Background Event Monitor"
        )

        print(
            "Machine database: "
            f"{self.machine_database_path}"
        )

        print(
            "Configuration database: "
            f"{self.config_database_path}"
        )

        print(
            "Rule source: "
            f"{self.rule_engine.get_source()}"
        )

        print(
            "Poll interval: "
            f"{self.poll_interval:g} seconds"
        )

        print(
            "Event transition filtering: enabled"
        )

        print(
            "Press Ctrl+C to stop."
        )

        print(
            flush=True
        )

        while True:
            try:
                inserted = self.run_cycle()

                if (
                    self.cycle_count == 1
                    or self.cycle_count % 30 == 0
                    or inserted > 0
                ):
                    print(
                        "Monitor cycle "
                        f"{self.cycle_count} | "
                        f"events stored this cycle: "
                        f"{inserted} | "
                        f"total stored this session: "
                        f"{self.event_count}",
                        flush=True,
                    )

                time.sleep(
                    self.poll_interval
                )

            except sqlite3.Error as error:
                print(
                    "Database error: "
                    f"{error}",
                    flush=True,
                )

                time.sleep(
                    self.poll_interval
                )

            except Exception as error:
                print(
                    "Event monitor error: "
                    f"{error}",
                    flush=True,
                )

                time.sleep(
                    self.poll_interval
                )


def main() -> None:
    try:
        monitor = BackgroundEventMonitor()

        monitor.run_forever()

    except KeyboardInterrupt:
        print(
            "\nBackground event monitor stopped."
        )

    except Exception as error:
        print(
            "Unable to start background event monitor: "
            f"{error}"
        )


if __name__ == "__main__":
    main()
