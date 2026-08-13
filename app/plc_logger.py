import sqlite3
import time
from pathlib import Path

from config.config_manager import ConfigManager
from config.environment import get_config_db_path
from plc.driver_factory import create_driver, resolve_driver_name
from plc.tag_registry import TagRegistry
from database.database import DatabaseManager


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DATABASE_PATH = get_config_db_path()

# How often to proactively refresh the driver connection, regardless
# of whether anything has failed. Needed because a dropped connection
# doesn't reliably surface as an exception here: each driver's
# read_all() catches and prints per-tag read errors internally rather
# than raising them (deliberately - one bad tag must never block the
# rest of a cycle), and with zero tags configured for the active
# driver there's no read attempt at all, so nothing ever generates
# traffic to even notice a drop. Confirmed against a real OPC UA
# server: a connection with nothing to read idle-timed-out and was
# dropped server-side after ~45s, with no way for this process to
# detect or recover from that on its own before this was added.
RECONNECT_INTERVAL_SECONDS = 30


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


def _reconnect(driver, driver_name: str) -> None:
    """
    Best-effort reconnect - disconnect first (ignoring any error, since
    the existing connection may already be dead) then connect again.
    Safe to call on a driver that's already fine, too (just a brief
    refresh), which is what makes the periodic proactive call to this
    below reasonable rather than something that should only ever run
    after a confirmed failure.
    """
    try:
        driver.disconnect()
    except Exception:
        pass

    driver.connect()
    print(f"{driver_name} driver reconnected.")


def _refresh_driver(driver, driver_name: str, registry: TagRegistry):
    """
    Re-resolves the active PLC connection and either swaps to it (a
    different connection activated, or deactivated back to the
    settings.ini fallback) or just refreshes the existing one in
    place if nothing's changed. Returns (driver, driver_name, tags,
    switched) - tags is only meaningful (not None) when switched is
    True, since the caller already has a perfectly good tags list to
    keep using otherwise.

    Without this re-resolution, this process stayed permanently
    "stuck" to whatever connection was active at startup - confirmed
    live: deactivating a connection on the PLC Connectivity page only
    ever updates the database (see config/plc_connection_manager.py's
    deactivate()), so an already-running process here - which used to
    only ever read the active connection once, at startup - had no
    way to notice, and the periodic proactive reconnect below just
    kept faithfully re-establishing a connection to the same, by-then-
    deactivated server every RECONNECT_INTERVAL_SECONDS, indefinitely,
    until manually restarted. Visible on the OPC UA simulator's own
    side as a client that "won't disconnect" no matter what's done in
    this app's own UI.
    """
    new_driver_name = resolve_driver_name()

    if new_driver_name == driver_name:
        _reconnect(driver, driver_name)
        return driver, driver_name, None, False

    print(f"Active connection changed ({driver_name} -> {new_driver_name}) - switching driver.")

    try:
        driver.disconnect()
    except Exception:
        pass

    new_driver = create_driver()
    new_driver.connect()
    new_tags = registry.get_enabled_for_driver(new_driver_name)

    print(f"Driver  : {new_driver_name}")
    print(f"Tags    : {len(new_tags)}")

    return new_driver, new_driver_name, new_tags, True


def main():

    cfg = ConfigManager()

    registry = TagRegistry()

    database = DatabaseManager()

    driver = create_driver()

    driver.connect()

    # The actual driver create_driver() resolved to - the active PLC
    # connection's protocol if one's active, else settings.ini's
    # legacy driver - NOT cfg.driver directly, which never updates
    # when a connection is activated on the PLC Connectivity page and
    # would silently poll zero tags forever otherwise (tag_addresses
    # keys each mapping by this exact driver name).
    driver_name = resolve_driver_name()

    tags = registry.get_enabled_for_driver(driver_name)

    cadence = load_logging_cadence()

    last_logged_time = {}
    last_logged_value = {}
    last_connection_refresh = time.monotonic()

    print(f"Machine : {cfg.machine_name}")
    print(f"Driver  : {driver_name}")
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
                data_type = cadence.get(name, (None, False, "REAL"))[2]

                # STRING tags (e.g. AlarmCode) can't go in plc_data (a
                # REAL column), but the UI still wants to show their
                # current value - written to a separate one-row-per-tag
                # table instead of the historian.
                if data_type == "STRING":
                    try:
                        database.save_text_tag(
                            tag=name,
                            address=row["address"],
                            value=value,
                        )
                    except Exception as save_error:
                        print(f"{name:25} FAILED TO LOG: {save_error}")
                        continue

                    print(f"{name:25} {value}")
                    logged_count += 1
                    continue

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

            now_monotonic = time.monotonic()

            if now_monotonic - last_connection_refresh >= RECONNECT_INTERVAL_SECONDS:
                driver, driver_name, refreshed_tags, switched = _refresh_driver(
                    driver, driver_name, registry
                )
                if switched:
                    tags = refreshed_tags
                last_connection_refresh = now_monotonic

            time.sleep(cfg.scan_interval)

        except KeyboardInterrupt:

            break

        except Exception as e:

            print(e)

            try:
                driver, driver_name, refreshed_tags, switched = _refresh_driver(
                    driver, driver_name, registry
                )
                if switched:
                    tags = refreshed_tags
                last_connection_refresh = time.monotonic()
            except Exception as reconnect_error:
                print(f"Reconnect failed: {reconnect_error}")

            time.sleep(5)

    driver.disconnect()


if __name__ == "__main__":
    main()
