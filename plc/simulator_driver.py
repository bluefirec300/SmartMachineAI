from plc.base_driver import BaseDriver
from simulator.factory_model import FactorySimulator


class SimulatorDriver(BaseDriver):

    def __init__(self):
        self.simulator = FactorySimulator()

    def connect(self):
        print("Simulator connected.")

    def disconnect(self):
        print("Simulator disconnected.")

    def read(self, tag_name):
        self.simulator.update_values()

        for name, address, value in self.simulator.get_tags():
            if name == tag_name:
                return {
                    "name": name,
                    "address": address,
                    "value": value,
                }

        raise KeyError(f"Unknown simulator tag: {tag_name}")

    def read_all(self, tags):
        self.simulator.update_values()

        values = []

        simulator_tags = {
            name: (address, value)
            for name, address, value in self.simulator.get_tags()
        }

        for tag in tags:

            name = tag["name"]

            if name in simulator_tags:

                address, value = simulator_tags[name]

                values.append(
                    {
                        "name": name,
                        "address": address,
                        "value": value,
                    }
                )

        return values

    def write(self, tag, value):
        raise NotImplementedError(
            "Simulator write not implemented."
        )
