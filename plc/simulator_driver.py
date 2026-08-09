from plc.base_driver import BaseDriver
from simulator.factory_model import FactorySimulator
from simulator.tag_dataset_model import TagDatasetSimulator


class SimulatorDriver(BaseDriver):
    """
    Combines two independent simulators:

    - FactorySimulator: the original hand-scripted ~20-tag demo
      (compressor/chiller/cold room/tank/power), untouched.
    - TagDatasetSimulator: generic value generation for whatever tags
      have been imported/enabled from the master tag list dataset
      (engine.tag_dataset_importer). Empty/no-op if none are enabled.

    Kept as two separate simulator objects rather than merging them,
    so the original tested fault-cycle behavior can't be disturbed by
    changes made for the larger dataset.
    """

    def __init__(self):
        self.simulator = FactorySimulator()
        self.dataset_simulator = TagDatasetSimulator()

    def connect(self):
        print("Simulator connected.")

    def disconnect(self):
        print("Simulator disconnected.")

    def _all_tags(self):
        self.simulator.update_values()
        self.dataset_simulator.update_values()

        return [
            *self.simulator.get_tags(),
            *self.dataset_simulator.get_tags(),
        ]

    def read(self, tag_name):
        for name, address, value in self._all_tags():
            if name == tag_name:
                return {
                    "name": name,
                    "address": address,
                    "value": value,
                }

        raise KeyError(f"Unknown simulator tag: {tag_name}")

    def read_all(self, tags):
        simulator_tags = {
            name: (address, value)
            for name, address, value in self._all_tags()
        }

        values = []

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
