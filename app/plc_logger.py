import sqlite3
import time
from pathlib import Path

from config.config_manager import ConfigManager
from plc.driver_factory import create_driver
from plc.tag_registry import TagRegistry
from database.database import DatabaseManager


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DATABASE_PATH = PROJECT_ROOT / "database" / "config.db"


def load_logging_cadence(database_path=CONFIG_DATABASE_PATH):
    """
    tag_name -> (logging_interval_seconds or None, log_on_change bool,
    data_type)

    Tags with no cadence configured (every original demo tag) get
    (None, False, ...), which should_log() treats as "log every poll" -
    the same behavior as before this existed.
    """
    connection = sqlite3.connect(database_path)

    try:
        rows = connection.execute(
            "SELECT tag_name, logging_interval_seconds, log_on_change, data_type FROM tags"
        ).fetchall()
    finally:
        connection.close()

    return {
        row[0]: (row[1], bool(row[2]), row[3])
        for row in rows
    }


def should_log(tag_name, value, now, cadence, last_logged_time, last_logged_value):
    interval_seconds, log_on_change, data_type = cadence.get(
        tag_name, (None, False, "REAL")
    )

    # plc_data.value is a REAL column - a STRING-typed tag (e.g. an
    # "AlarmCode" like "E101") can never be written there. Historian
    # trend/threshold analysis doesn't need these anyway (the paired
    # boolean Alarm tag + machine_events already record the fault).
    if data_type == "STRING":
        return False

    if log_on_change:
        return last_logged_value.get(tag_name, object()) != value

    if interval_seconds:
        last_time = last_logged_time.get(tag_name)
        return last_time is None or (now - last_time) >= interval_seconds

    return True


def main():

    cfg = ConfigManager()

    registry = TagRegistry()

    database = DatabaseManager()

    driver = create_driver()

    driver.connect()

    tags = registry.get_enabled_for_driver(cfg.driver)

    cadence = load_logging_cadence()

    last_logged_time = {}
    last_logged_value = {}

    print(f"Machine : {cfg.machine_name}")
    print(f"Driver  : {cfg.driver}")
    print(f"Tags    : {len(tags)}")
    print()

    while True:

        try:

            values = driver.read_all(tags)
            now = time.monotonic()
            logged_count = 0

            for row in values:

                name = row["name"]
                value = row["value"]

                if not should_log(
                    name, value, now, cadence, last_logged_time, last_logged_value
                ):
                    continue

                try:
                    database.save_tag(
                        tag=name,
                        address=row["address"],
                        value=value
                    )
                except Exception as save_error:
                    # One bad tag must never block the rest of this
                    # cycle's tags from logging - this exact failure
                    # mode (a STRING-typed AlarmCode tag) previously
                    # took down logging for every tag alphabetically
                    # after it, every cycle, for hours.
                    print(f"{name:25} FAILED TO LOG: {save_error}")
                    continue

                print(
                    f"{name:25} "
                    f"{value}"
                )

                last_logged_time[name] = now
                last_logged_value[name] = value
                logged_count += 1

            print(f"---------------------------- ({logged_count} of {len(values)} tags logged this cycle)")

            time.sleep(cfg.scan_interval)

        except KeyboardInterrupt:

            break

        except Exception as e:

            print(e)

            time.sleep(5)

    driver.disconnect()


if __name__ == "__main__":
    main()
