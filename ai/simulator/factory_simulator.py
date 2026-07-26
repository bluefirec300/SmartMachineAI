import random
import sqlite3
import time
from datetime import datetime
from pathlib import Path


DATABASE_PATH = Path.home() / "SmartMachineAI" / "machine_data.db"
UPDATE_INTERVAL = 2  # Seconds between updates


class FactorySimulator:
    def __init__(self):
        self.cycle_count = 0

        # Compressor
        self.compressor_pressure = 7.0
        self.compressor_temperature = 65.0
        self.compressor_current = 18.0
        self.compressor_running = 1

        # Chiller
        self.chiller_supply_temp = 7.0
        self.chiller_return_temp = 12.0
        self.chiller_running = 1

        # Cold room
        self.cold_room_temp = 5.0
        self.cold_room_humidity = 55.0
        self.cold_room_door = 0

        # Tank and pump
        self.tank_level = 75.0
        self.transfer_pump_running = 1
        self.transfer_pump_current = 6.0

        # Factory utilities
        self.factory_energy_kw = 95.0
        self.water_pressure = 3.5
        self.air_pressure = 6.8

    def update_values(self):
        self.cycle_count += 1

        # Repeating simulation cycle
        phase = self.cycle_count % 180

        self.update_compressor(phase)
        self.update_chiller()
        self.update_cold_room()
        self.update_tank_and_pump()
        self.update_factory_utilities()

        self.limit_values()

    def update_compressor(self, phase):
        # Normal operation
        if phase < 60:
            self.compressor_running = 1

            self.compressor_pressure += random.uniform(-0.05, 0.05)
            self.compressor_temperature += random.uniform(-0.2, 0.2)
            self.compressor_current += random.uniform(-0.2, 0.2)

            # Slowly return toward normal values
            self.compressor_pressure += (
                7.0 - self.compressor_pressure
            ) * 0.03

            self.compressor_temperature += (
                65.0 - self.compressor_temperature
            ) * 0.03

            self.compressor_current += (
                18.0 - self.compressor_current
            ) * 0.03

        # Developing compressor fault
        elif phase < 110:
            self.compressor_running = 1

            self.compressor_pressure -= random.uniform(0.05, 0.12)
            self.compressor_temperature += random.uniform(0.2, 0.6)
            self.compressor_current += random.uniform(0.1, 0.4)

        # Compressor trip
        elif phase < 125:
            self.compressor_running = 0

            self.compressor_pressure -= random.uniform(0.08, 0.15)
            self.compressor_temperature -= random.uniform(0.1, 0.3)
            self.compressor_current = max(
                0,
                self.compressor_current - random.uniform(1.0, 3.0)
            )

        # Recovery
        elif phase < 160:
            self.compressor_running = 1

            self.compressor_pressure += random.uniform(0.08, 0.18)
            self.compressor_temperature -= random.uniform(0.3, 0.7)
            self.compressor_current += (
                18.0 - self.compressor_current
            ) * 0.15

        # Stable condition
        else:
            self.compressor_running = 1

            self.compressor_pressure += (
                7.0 - self.compressor_pressure
            ) * 0.12

            self.compressor_temperature += (
                65.0 - self.compressor_temperature
            ) * 0.12

            self.compressor_current += (
                18.0 - self.compressor_current
            ) * 0.12

    def update_chiller(self):
        self.chiller_supply_temp += random.uniform(-0.08, 0.08)
        self.chiller_return_temp += random.uniform(-0.10, 0.10)

        # Keep chiller temperatures around normal values
        self.chiller_supply_temp += (
            7.0 - self.chiller_supply_temp
        ) * 0.03

        self.chiller_return_temp += (
            12.0 - self.chiller_return_temp
        ) * 0.03

        # Occasional chiller temperature disturbance
        if random.random() < 0.01:
            self.chiller_supply_temp += random.uniform(0.5, 1.5)
            self.chiller_return_temp += random.uniform(0.5, 1.5)

    def update_cold_room(self):
        # Randomly open the cold-room door
        if self.cold_room_door == 0 and random.random() < 0.03:
            self.cold_room_door = 1

        elif self.cold_room_door == 1 and random.random() < 0.20:
            self.cold_room_door = 0

        if self.cold_room_door == 1:
            self.cold_room_temp += random.uniform(0.05, 0.15)
            self.cold_room_humidity += random.uniform(0.1, 0.4)

        else:
            self.cold_room_temp += (
                5.0 - self.cold_room_temp
            ) * 0.05

            self.cold_room_humidity += (
                55.0 - self.cold_room_humidity
            ) * 0.04

        self.cold_room_temp += random.uniform(-0.03, 0.03)
        self.cold_room_humidity += random.uniform(-0.15, 0.15)

    def update_tank_and_pump(self):
        if self.transfer_pump_running == 1:
            self.tank_level -= random.uniform(0.02, 0.08)
            self.transfer_pump_current += random.uniform(-0.08, 0.08)

        # Refill tank when level becomes low
        if self.tank_level < 20:
            self.transfer_pump_running = 0
            self.tank_level += random.uniform(0.2, 0.5)

        # Restart pump when tank is refilled
        if self.tank_level > 85:
            self.transfer_pump_running = 1

        if self.transfer_pump_running == 0:
            self.transfer_pump_current = 0

        else:
            self.transfer_pump_current += (
                6.0 - self.transfer_pump_current
            ) * 0.08

    def update_factory_utilities(self):
        load_change = random.uniform(-1.0, 1.0)

        if self.compressor_running:
            load_change += 0.3

        if self.chiller_running:
            load_change += 0.2

        if self.transfer_pump_running:
            load_change += 0.1

        self.factory_energy_kw += load_change
        self.factory_energy_kw += (
            95.0 - self.factory_energy_kw
        ) * 0.03

        self.water_pressure += random.uniform(-0.03, 0.03)
        self.water_pressure += (
            3.5 - self.water_pressure
        ) * 0.04

        self.air_pressure = self.compressor_pressure - 0.2
        self.air_pressure += random.uniform(-0.03, 0.03)

    def limit_values(self):
        self.compressor_pressure = max(
            0,
            min(10, self.compressor_pressure)
        )

        self.compressor_temperature = max(
            20,
            min(120, self.compressor_temperature)
        )

        self.compressor_current = max(
            0,
            min(50, self.compressor_current)
        )

        self.chiller_supply_temp = max(
            2,
            min(18, self.chiller_supply_temp)
        )

        self.chiller_return_temp = max(
            4,
            min(25, self.chiller_return_temp)
        )

        self.cold_room_temp = max(
            -5,
            min(20, self.cold_room_temp)
        )

        self.cold_room_humidity = max(
            0,
            min(100, self.cold_room_humidity)
        )

        self.tank_level = max(
            0,
            min(100, self.tank_level)
        )

        self.transfer_pump_current = max(
            0,
            min(20, self.transfer_pump_current)
        )

        self.factory_energy_kw = max(
            0,
            min(300, self.factory_energy_kw)
        )

        self.water_pressure = max(
            0,
            min(10, self.water_pressure)
        )

        self.air_pressure = max(
            0,
            min(10, self.air_pressure)
        )

    def get_tags(self):
        compressor_warning = int(
            self.compressor_pressure < 6.0
            or self.compressor_temperature > 80
            or self.compressor_current > 24
        )

        compressor_alarm = int(
            self.compressor_pressure < 5.0
            or self.compressor_temperature > 90
            or self.compressor_running == 0
        )

        cold_room_alarm = int(
            self.cold_room_temp > 8.0
        )

        tank_low_alarm = int(
            self.tank_level < 15.0
        )

        chiller_alarm = int(
            self.chiller_supply_temp > 10.0
        )

        return [
            (
                "CompressorPressure",
                "SIM.DM100",
                self.compressor_pressure
            ),
            (
                "CompressorTemperature",
                "SIM.DM101",
                self.compressor_temperature
            ),
            (
                "CompressorCurrent",
                "SIM.DM102",
                self.compressor_current
            ),
            (
                "CompressorRunning",
                "SIM.CIO0.00",
                self.compressor_running
            ),
            (
                "CompressorWarning",
                "SIM.CIO1.00",
                compressor_warning
            ),
            (
                "CompressorAlarm",
                "SIM.CIO1.01",
                compressor_alarm
            ),

            (
                "ChillerSupplyTemp",
                "SIM.DM110",
                self.chiller_supply_temp
            ),
            (
                "ChillerReturnTemp",
                "SIM.DM111",
                self.chiller_return_temp
            ),
            (
                "ChillerRunning",
                "SIM.CIO0.01",
                self.chiller_running
            ),
            (
                "ChillerAlarm",
                "SIM.CIO1.02",
                chiller_alarm
            ),

            (
                "ColdRoomTemperature",
                "SIM.DM120",
                self.cold_room_temp
            ),
            (
                "ColdRoomHumidity",
                "SIM.DM121",
                self.cold_room_humidity
            ),
            (
                "ColdRoomDoor",
                "SIM.CIO0.02",
                self.cold_room_door
            ),
            (
                "ColdRoomAlarm",
                "SIM.CIO1.03",
                cold_room_alarm
            ),

            (
                "TankLevel",
                "SIM.DM130",
                self.tank_level
            ),
            (
                "TransferPumpRunning",
                "SIM.CIO0.03",
                self.transfer_pump_running
            ),
            (
                "TransferPumpCurrent",
                "SIM.DM131",
                self.transfer_pump_current
            ),
            (
                "TankLowAlarm",
                "SIM.CIO1.04",
                tank_low_alarm
            ),

            (
                "FactoryEnergyKW",
                "SIM.DM140",
                self.factory_energy_kw
            ),
            (
                "WaterPressure",
                "SIM.DM141",
                self.water_pressure
            ),
            (
                "AirPressure",
                "SIM.DM142",
                self.air_pressure
            ),
        ]


def initialize_database(connection):
    cursor = connection.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS plc_data (
            time TEXT,
            tag TEXT,
            address TEXT,
            value REAL
        )
        """
    )

    connection.commit()


def write_tags(connection, tags):
    cursor = connection.cursor()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    rows = []

    for tag, address, value in tags:
        rows.append(
            (
                timestamp,
                tag,
                address,
                round(float(value), 2)
            )
        )

    cursor.executemany(
        """
        INSERT INTO plc_data (
            time,
            tag,
            address,
            value
        )
        VALUES (?, ?, ?, ?)
        """,
        rows
    )

    connection.commit()


def display_status(tags, cycle_count):
    values = {
        tag: value
        for tag, _, value in tags
    }

    print(
        f"Cycle: {cycle_count:04d} | "
        f"Compressor: {values['CompressorPressure']:.2f} bar | "
        f"Temp: {values['CompressorTemperature']:.2f} °C | "
        f"Current: {values['CompressorCurrent']:.2f} A | "
        f"Running: {int(values['CompressorRunning'])} | "
        f"Alarm: {int(values['CompressorAlarm'])} | "
        f"Cold Room: {values['ColdRoomTemperature']:.2f} °C | "
        f"Tank: {values['TankLevel']:.2f}% | "
        f"Energy: {values['FactoryEnergyKW']:.2f} kW"
    )


def main():
    print("MMG Factory Simulator")
    print(f"Database: {DATABASE_PATH}")
    print(f"Update interval: {UPDATE_INTERVAL} seconds")
    print("Press Ctrl+C to stop.\n")

    simulator = FactorySimulator()

    try:
        DATABASE_PATH.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        with sqlite3.connect(DATABASE_PATH) as connection:
            initialize_database(connection)

            while True:
                simulator.update_values()

                tags = simulator.get_tags()

                write_tags(
                    connection,
                    tags
                )

                display_status(
                    tags,
                    simulator.cycle_count
                )

                time.sleep(UPDATE_INTERVAL)

    except KeyboardInterrupt:
        print("\nFactory simulator stopped.")

    except sqlite3.Error as error:
        print(f"Database error: {error}")

    except Exception as error:
        print(f"Unexpected error: {error}")


if __name__ == "__main__":
    main()
