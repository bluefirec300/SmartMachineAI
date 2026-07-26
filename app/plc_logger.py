import time

from config.config_manager import ConfigManager
from plc.driver_factory import create_driver
from plc.tag_registry import TagRegistry
from database.database import DatabaseManager


def main():

    cfg = ConfigManager()

    registry = TagRegistry()

    database = DatabaseManager()

    driver = create_driver()

    driver.connect()

    tags = registry.get_enabled_for_driver(cfg.driver)

    print(f"Machine : {cfg.machine_name}")
    print(f"Driver  : {cfg.driver}")
    print(f"Tags    : {len(tags)}")
    print()

    while True:

        try:

            values = driver.read_all(tags)

            for row in values:

                print(
                    f"{row['name']:25} "
                    f"{row['value']}"
                )

                database.save_tag(
                    tag=row["name"],
                    address=row["address"],
                    value=row["value"]
                )

            print("----------------------------")

            time.sleep(cfg.scan_interval)

        except KeyboardInterrupt:

            break

        except Exception as e:

            print(e)

            time.sleep(5)

    driver.disconnect()


if __name__ == "__main__":
    main()
