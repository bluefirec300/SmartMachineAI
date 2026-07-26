import json
from pathlib import Path

from config.configuration_manager import ConfigurationManager


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigurationImporter:

    def __init__(self):
        self.config = ConfigurationManager()

    def import_tag_registry(
        self,
        filename="config/tag_registry.json",
        username="importer",
    ):

        path = PROJECT_ROOT / filename

        with open(path, "r", encoding="utf-8") as f:
            registry = json.load(f)

        imported = 0
        skipped = 0

        for tag in registry:

            tag_name = tag["name"]

            if self.config.get_tag(tag_name):

                skipped += 1
                continue

            equipment = tag.get("equipment")

            if equipment:

                if self.config.get_equipment(equipment) is None:

                    self.config.add_equipment(
                        name=equipment,
                        display_name=equipment.replace("_", " ").title(),
                        username=username,
                    )

            self.config.add_tag(
                tag_name=tag_name,
                equipment_name=equipment,
                description=tag.get("description", ""),
                data_type=tag.get("data_type", "float"),
                unit=tag.get("unit", ""),
                enabled=tag.get("enabled", True),
                username=username,
            )

            addresses = tag.get("addresses", {})

            for driver, address in addresses.items():

                self.config.set_tag_address(
                    tag_name=tag_name,
                    driver=driver,
                    address=address,
                    username=username,
                )

            imported += 1

        print()

        print(f"Imported : {imported}")
        print(f"Skipped  : {skipped}")

