import json
import random
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATABASE_PATH = PROJECT_ROOT / "database" / "machine_data.db"
COMMAND_FILE_PATH = PROJECT_ROOT / "config" / "simulator_command.txt"
CONFIG_FILE_PATH = PROJECT_ROOT / "config" / "simulator_config.json"


DEFAULT_CONFIG: dict[str, Any] = {
    "mode": "realistic",
    "update_interval": 2.0,
    "demo_fault_duration": 120,
    "training_fault_duration": 900,
    "realistic_fault_min": 7200,
    "realistic_fault_max": 21600,
    "minor_event_min": 1800,
    "minor_event_max": 3600,
}


def load_config() -> dict[str, Any]:
    """Load simulator settings and fall back safely when a setting is invalid."""
    config = DEFAULT_CONFIG.copy()

    if not CONFIG_FILE_PATH.exists():
        print(f"Configuration file not found: {CONFIG_FILE_PATH}")
        print("Using default simulator configuration.")
        return config

    try:
        loaded = json.loads(CONFIG_FILE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"Unable to load simulator configuration: {error}")
        print("Using default simulator configuration.")
        return config

    if not isinstance(loaded, dict):
        print("Simulator configuration must contain a JSON object.")
        print("Using default simulator configuration.")
        return config

    for key in DEFAULT_CONFIG:
        if key in loaded:
            config[key] = loaded[key]

    simulator_section = loaded.get("simulator", {})
    faults_section = loaded.get("faults", {})
    disturbances_section = loaded.get("disturbances", {})

    if isinstance(simulator_section, dict):
        if "update_interval" in simulator_section:
            config["update_interval"] = simulator_section["update_interval"]

    if isinstance(faults_section, dict):
        if "demo_duration" in faults_section:
            config["demo_fault_duration"] = faults_section["demo_duration"]
        if "training_duration" in faults_section:
            config["training_fault_duration"] = faults_section["training_duration"]
        if "realistic_min_interval" in faults_section:
            config["realistic_fault_min"] = faults_section["realistic_min_interval"]
        if "realistic_max_interval" in faults_section:
            config["realistic_fault_max"] = faults_section["realistic_max_interval"]

    if isinstance(disturbances_section, dict):
        if "min_interval" in disturbances_section:
            config["minor_event_min"] = disturbances_section["min_interval"]
        if "max_interval" in disturbances_section:
            config["minor_event_max"] = disturbances_section["max_interval"]

    mode = str(config["mode"]).strip().lower()
    if mode not in {"demo", "training", "realistic"}:
        print(f"Unknown simulator mode '{mode}'. Using realistic mode.")
        mode = "realistic"
    config["mode"] = mode

    numeric_keys = (
        "update_interval",
        "demo_fault_duration",
        "training_fault_duration",
        "realistic_fault_min",
        "realistic_fault_max",
        "minor_event_min",
        "minor_event_max",
    )

    for key in numeric_keys:
        try:
            config[key] = float(config[key])
        except (TypeError, ValueError):
            print(
                f"Invalid value for '{key}'. "
                f"Using default value {DEFAULT_CONFIG[key]}."
            )
            config[key] = float(DEFAULT_CONFIG[key])

        if config[key] <= 0:
            print(
                f"Value for '{key}' must be greater than zero. "
                f"Using default value {DEFAULT_CONFIG[key]}."
            )
            config[key] = float(DEFAULT_CONFIG[key])

    if config["realistic_fault_min"] > config["realistic_fault_max"]:
        config["realistic_fault_min"], config["realistic_fault_max"] = (
            config["realistic_fault_max"],
            config["realistic_fault_min"],
        )

    if config["minor_event_min"] > config["minor_event_max"]:
        config["minor_event_min"], config["minor_event_max"] = (
            config["minor_event_max"],
            config["minor_event_min"],
        )

    return config


CONFIG = load_config()
SIMULATION_MODE = str(CONFIG["mode"])
UPDATE_INTERVAL = float(CONFIG["update_interval"])
MINOR_INTERVAL = (
    float(CONFIG["minor_event_min"]),
    float(CONFIG["minor_event_max"]),
)

if SIMULATION_MODE == "demo":
    MAJOR_INTERVAL = (300.0, 600.0)
elif SIMULATION_MODE == "training":
    MAJOR_INTERVAL = (900.0, 1800.0)
else:
    MAJOR_INTERVAL = (
        float(CONFIG["realistic_fault_min"]),
        float(CONFIG["realistic_fault_max"]),
    )

SCENARIOS = {
    "normal",
    "compressor_pressure_drop",
    "dirty_filter",
    "air_leak",
    "chiller_efficiency_loss",
    "cold_room_door_left_open",
    "water_pressure_drop",
    "tank_low_level",
}

SCENARIO_DURATION = {
    "compressor_pressure_drop": 12 * 60,
    "dirty_filter": 15 * 60,
    "air_leak": 12 * 60,
    "chiller_efficiency_loss": 18 * 60,
    "cold_room_door_left_open": 15 * 60,
    "water_pressure_drop": 10 * 60,
    "tank_low_level": 18 * 60,
}


class FactorySimulator:
    def __init__(self) -> None:
        self.cycle = 0
        self.production_load = 0.50

        self.compressor_pressure = 7.0
        self.compressor_temperature = 65.0
        self.compressor_current = 18.0
        self.compressor_running = 1

        self.chiller_supply_temp = 7.0
        self.chiller_return_temp = 12.0
        self.chiller_running = 1

        self.cold_room_temp = 5.0
        self.cold_room_humidity = 55.0
        self.cold_room_door = 0

        self.tank_level = 75.0
        self.transfer_pump_running = 1
        self.transfer_pump_current = 6.0

        self.factory_energy_kw = 95.0
        self.water_pressure = 3.5
        self.air_pressure = 6.8

        self.minor_disturbance = "none"
        self.minor_elapsed = 0.0
        self.minor_duration = 0.0
        self.until_minor = random.uniform(*MINOR_INTERVAL)

        self.active_scenario = "normal"
        self.scenario_elapsed = 0.0
        self.scenario_duration = 0.0
        self.until_major = random.uniform(*MAJOR_INTERVAL)

    @staticmethod
    def noise(amount: float) -> float:
        return random.uniform(-amount, amount)

    @staticmethod
    def approach(value: float, target: float, rate: float) -> float:
        return value + (target - value) * rate

    def update(self) -> None:
        self.cycle += 1
        self._read_command()
        self._update_timers()
        self._normal_process()
        self._apply_minor_disturbance()
        self._apply_major_scenario()
        self._limit_values()

    def _read_command(self) -> None:
        if not COMMAND_FILE_PATH.exists():
            return
        try:
            command = COMMAND_FILE_PATH.read_text(encoding="utf-8").strip().lower()
        except OSError:
            return
        if not command:
            return
        try:
            COMMAND_FILE_PATH.write_text("", encoding="utf-8")
        except OSError:
            pass

        if command not in SCENARIOS:
            print(f"Unknown command: {command}")
            print("Available: " + ", ".join(sorted(SCENARIOS)))
            return
        if command == "normal":
            self.stop_scenario("manual reset")
        else:
            self.start_scenario(command, "manual")

    def _update_timers(self) -> None:
        if self.active_scenario == "normal":
            self.until_major -= UPDATE_INTERVAL
            if self.until_major <= 0:
                self.start_scenario(random.choice(list(SCENARIO_DURATION)), "automatic")
        else:
            self.scenario_elapsed += UPDATE_INTERVAL
            if self.scenario_elapsed >= self.scenario_duration:
                self.stop_scenario("completed")

        if self.minor_disturbance == "none":
            self.until_minor -= UPDATE_INTERVAL
            if self.until_minor <= 0 and self.active_scenario == "normal":
                self.start_minor()
        else:
            self.minor_elapsed += UPDATE_INTERVAL
            if self.minor_elapsed >= self.minor_duration:
                self.stop_minor()

    def start_minor(self) -> None:
        durations = {
            "production_load_change": (120, 300),
            "brief_water_pressure_dip": (30, 90),
            "cold_room_door_open": (60, 180),
            "compressor_load_change": (60, 180),
            "sensor_noise": (20, 60),
        }
        self.minor_disturbance = random.choice(list(durations))
        self.minor_duration = random.uniform(*durations[self.minor_disturbance])
        self.minor_elapsed = 0.0
        print(f"\nMinor disturbance started: {self.minor_disturbance}")

    def stop_minor(self) -> None:
        if self.minor_disturbance != "none":
            print(f"\nMinor disturbance completed: {self.minor_disturbance}")
        self.minor_disturbance = "none"
        self.minor_elapsed = 0.0
        self.minor_duration = 0.0
        self.until_minor = random.uniform(*MINOR_INTERVAL)

    def start_scenario(self, scenario: str, source: str) -> None:
        self.active_scenario = scenario
        self.scenario_elapsed = 0.0

        if SIMULATION_MODE == "demo":
            self.scenario_duration = float(CONFIG["demo_fault_duration"])
        elif SIMULATION_MODE == "training":
            self.scenario_duration = float(CONFIG["training_fault_duration"])
        else:
            self.scenario_duration = float(SCENARIO_DURATION[scenario])

        if self.minor_disturbance != "none":
            self.stop_minor()

        print(
            f"\nMajor scenario started: {scenario} ({source}) | "
            f"duration: {remaining_time(self.scenario_duration)}"
        )

    def stop_scenario(self, reason: str) -> None:
        previous = self.active_scenario
        self.active_scenario = "normal"
        self.scenario_elapsed = 0.0
        self.scenario_duration = 0.0
        self.until_major = random.uniform(*MAJOR_INTERVAL)
        self.compressor_running = 1
        self.chiller_running = 1
        self.cold_room_door = 0
        if previous != "normal":
            print(f"\nMajor scenario stopped: {previous} ({reason})")

    def _normal_process(self) -> None:
        load_target = 0.50 + self.noise(0.04)
        self.production_load = self.approach(self.production_load, load_target, 0.02)

        if self.compressor_running:
            self.compressor_pressure = self.approach(
                self.compressor_pressure, 6.90 + self.production_load * 0.20, 0.035
            ) + self.noise(0.035)
            self.compressor_current = self.approach(
                self.compressor_current, 15.5 + self.production_load * 5.0, 0.035
            ) + self.noise(0.12)
            self.compressor_temperature = self.approach(
                self.compressor_temperature, 60.0 + self.production_load * 10.0, 0.025
            ) + self.noise(0.12)
        else:
            self.compressor_pressure -= random.uniform(0.03, 0.08)
            self.compressor_current = self.approach(self.compressor_current, 0.0, 0.35)
            self.compressor_temperature = self.approach(
                self.compressor_temperature, 40.0, 0.015
            )

        if self.chiller_running:
            self.chiller_supply_temp = self.approach(
                self.chiller_supply_temp, 6.8 + self.production_load * 0.4, 0.035
            ) + self.noise(0.045)
            self.chiller_return_temp = self.approach(
                self.chiller_return_temp, 11.5 + self.production_load, 0.035
            ) + self.noise(0.06)

        if self.cold_room_door:
            self.cold_room_temp += random.uniform(0.018, 0.05)
            self.cold_room_humidity += random.uniform(0.03, 0.12)
        else:
            self.cold_room_temp = self.approach(self.cold_room_temp, 5.0, 0.015) + self.noise(0.018)
            self.cold_room_humidity = self.approach(
                self.cold_room_humidity, 55.0, 0.025
            ) + self.noise(0.08)

        usage = 0.004 + self.production_load * 0.008
        if self.transfer_pump_running:
            self.tank_level -= usage
            self.transfer_pump_current = self.approach(
                self.transfer_pump_current, 5.3 + self.production_load * 1.4, 0.06
            ) + self.noise(0.05)
        else:
            self.transfer_pump_current = self.approach(
                self.transfer_pump_current, 0.0, 0.30
            )
            self.tank_level += random.uniform(0.03, 0.08)

        if self.tank_level < 25.0 and self.active_scenario != "tank_low_level":
            self.transfer_pump_running = 0
        elif self.tank_level > 85.0 and self.active_scenario != "tank_low_level":
            self.transfer_pump_running = 1

        energy_target = 65.0 + self.production_load * 45.0
        energy_target += 12.0 if self.compressor_running else 0.0
        energy_target += 10.0 if self.chiller_running else 0.0
        energy_target += 3.0 if self.transfer_pump_running else 0.0
        self.factory_energy_kw = self.approach(
            self.factory_energy_kw, energy_target, 0.025
        ) + self.noise(0.5)

        self.water_pressure = self.approach(self.water_pressure, 3.5, 0.04) + self.noise(0.025)
        pressure_loss = 0.15 + self.production_load * 0.15
        self.air_pressure = self.approach(
            self.air_pressure, self.compressor_pressure - pressure_loss, 0.20
        ) + self.noise(0.025)

    def _apply_minor_disturbance(self) -> None:
        if self.minor_disturbance == "none":
            return
        progress = min(1.0, self.minor_elapsed / max(1.0, self.minor_duration))
        shape = 1.0 - abs(progress * 2.0 - 1.0)

        if self.minor_disturbance == "production_load_change":
            self.production_load += 0.25 * shape
        elif self.minor_disturbance == "brief_water_pressure_dip":
            self.water_pressure -= 0.60 * shape
        elif self.minor_disturbance == "cold_room_door_open":
            self.cold_room_door = 1
        elif self.minor_disturbance == "compressor_load_change":
            self.compressor_current += 3.0 * shape
            self.compressor_temperature += 2.0 * shape
            self.factory_energy_kw += 6.0 * shape
        elif self.minor_disturbance == "sensor_noise":
            self.compressor_pressure += self.noise(0.12 * shape)
            self.water_pressure += self.noise(0.10 * shape)

    def _apply_major_scenario(self) -> None:
        scenario = self.active_scenario
        if scenario == "normal":
            return
        progress = min(1.0, self.scenario_elapsed / max(1.0, self.scenario_duration))

        if scenario == "compressor_pressure_drop":
            self.compressor_pressure = self.approach(
                self.compressor_pressure, 7.0 - 3.2 * progress, 0.08
            )
            self.compressor_current += 0.025 + 0.08 * progress
            self.compressor_temperature += 0.015 + 0.06 * progress
            if progress > 0.88:
                self.compressor_running = 0

        elif scenario == "dirty_filter":
            self.compressor_pressure = self.approach(
                self.compressor_pressure, 7.0 - 2.9 * progress, 0.06
            )
            self.compressor_current = self.approach(
                self.compressor_current, 18.0 + 10.0 * progress, 0.05
            )
            self.compressor_temperature = self.approach(
                self.compressor_temperature, 65.0 + 34.0 * progress, 0.04
            )
            if progress > 0.92:
                self.compressor_running = 0

        elif scenario == "air_leak":
            self.air_pressure = self.compressor_pressure - (0.2 + 2.5 * progress)
            self.compressor_current += 0.018 + 0.05 * progress
            self.compressor_temperature += 0.012 + 0.03 * progress
            self.factory_energy_kw += 8.0 * progress

        elif scenario == "chiller_efficiency_loss":
            self.chiller_supply_temp = self.approach(
                self.chiller_supply_temp, 7.0 + 6.0 * progress, 0.05
            )
            self.chiller_return_temp = self.approach(
                self.chiller_return_temp, 12.0 + 6.0 * progress, 0.05
            )
            self.cold_room_temp += 0.010 + 0.045 * progress
            self.cold_room_humidity += 0.01 + 0.05 * progress
            self.factory_energy_kw += 10.0 * progress

        elif scenario == "cold_room_door_left_open":
            self.cold_room_door = 1
            self.cold_room_temp += 0.018 + 0.055 * progress
            self.cold_room_humidity += 0.035 + 0.10 * progress
            self.factory_energy_kw += 5.0 * progress

        elif scenario == "water_pressure_drop":
            self.water_pressure = self.approach(
                self.water_pressure, 3.5 - 2.8 * progress, 0.08
            )

        elif scenario == "tank_low_level":
            self.transfer_pump_running = 1
            self.tank_level -= 0.035 + 0.14 * progress
            self.transfer_pump_current += 0.01 + 0.035 * progress

    def _limit_values(self) -> None:
        self.production_load = max(0.10, min(1.00, self.production_load))
        self.compressor_pressure = max(0.0, min(10.0, self.compressor_pressure))
        self.compressor_temperature = max(20.0, min(120.0, self.compressor_temperature))
        self.compressor_current = max(0.0, min(50.0, self.compressor_current))
        self.chiller_supply_temp = max(2.0, min(18.0, self.chiller_supply_temp))
        self.chiller_return_temp = max(4.0, min(25.0, self.chiller_return_temp))
        self.cold_room_temp = max(-5.0, min(20.0, self.cold_room_temp))
        self.cold_room_humidity = max(0.0, min(100.0, self.cold_room_humidity))
        self.tank_level = max(0.0, min(100.0, self.tank_level))
        self.transfer_pump_current = max(0.0, min(20.0, self.transfer_pump_current))
        self.factory_energy_kw = max(0.0, min(300.0, self.factory_energy_kw))
        self.water_pressure = max(0.0, min(10.0, self.water_pressure))
        self.air_pressure = max(0.0, min(10.0, self.air_pressure))

    def tags(self) -> list[tuple[str, str, float]]:
        compressor_warning = int(
            self.compressor_pressure < 6.0
            or self.compressor_temperature > 80.0
            or self.compressor_current > 24.0
        )
        compressor_alarm = int(
            self.compressor_pressure < 5.0
            or self.compressor_temperature > 90.0
            or self.compressor_running == 0
        )
        return [
            ("CompressorPressure", "SIM.DM100", self.compressor_pressure),
            ("CompressorTemperature", "SIM.DM101", self.compressor_temperature),
            ("CompressorCurrent", "SIM.DM102", self.compressor_current),
            ("CompressorRunning", "SIM.CIO0.00", self.compressor_running),
            ("CompressorWarning", "SIM.CIO1.00", compressor_warning),
            ("CompressorAlarm", "SIM.CIO1.01", compressor_alarm),
            ("ChillerSupplyTemp", "SIM.DM110", self.chiller_supply_temp),
            ("ChillerReturnTemp", "SIM.DM111", self.chiller_return_temp),
            ("ChillerRunning", "SIM.CIO0.01", self.chiller_running),
            ("ChillerAlarm", "SIM.CIO1.02", int(self.chiller_supply_temp > 10.0)),
            ("ColdRoomTemperature", "SIM.DM120", self.cold_room_temp),
            ("ColdRoomHumidity", "SIM.DM121", self.cold_room_humidity),
            ("ColdRoomDoor", "SIM.CIO0.02", self.cold_room_door),
            ("ColdRoomAlarm", "SIM.CIO1.03", int(self.cold_room_temp > 8.0)),
            ("TankLevel", "SIM.DM130", self.tank_level),
            ("TransferPumpRunning", "SIM.CIO0.03", self.transfer_pump_running),
            ("TransferPumpCurrent", "SIM.DM131", self.transfer_pump_current),
            ("TankLowAlarm", "SIM.CIO1.04", int(self.tank_level < 15.0)),
            ("FactoryEnergyKW", "SIM.DM140", self.factory_energy_kw),
            ("WaterPressure", "SIM.DM141", self.water_pressure),
            ("AirPressure", "SIM.DM142", self.air_pressure),
        ]


def initialize_database(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS plc_data
        (
            time TEXT,
            tag TEXT,
            address TEXT,
            value REAL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_plc_data_time ON plc_data(time)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_plc_data_tag_time ON plc_data(tag, time)"
    )
    connection.commit()


def write_tags(connection: sqlite3.Connection, tags: list[tuple[str, str, float]]) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = [
        (timestamp, tag, address, round(float(value), 2))
        for tag, address, value in tags
    ]
    connection.executemany(
        "INSERT INTO plc_data(time, tag, address, value) VALUES (?, ?, ?, ?)",
        rows,
    )
    connection.commit()


def remaining_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m {seconds:02d}s"


def display_status(simulator: FactorySimulator, tags: list[tuple[str, str, float]]) -> None:
    values = {tag: value for tag, _, value in tags}
    if simulator.active_scenario == "normal":
        scenario = "normal"
    else:
        progress = 100.0 * simulator.scenario_elapsed / max(1.0, simulator.scenario_duration)
        scenario = f"{simulator.active_scenario} {progress:.0f}%"

    print(
        f"Cycle: {simulator.cycle:06d} | "
        f"Scenario: {scenario} | "
        f"Disturbance: {simulator.minor_disturbance} | "
        f"Pressure: {values['CompressorPressure']:.2f} bar | "
        f"Temp: {values['CompressorTemperature']:.1f} C | "
        f"Current: {values['CompressorCurrent']:.1f} A | "
        f"Run: {int(values['CompressorRunning'])} | "
        f"Warning: {int(values['CompressorWarning'])} | "
        f"Alarm: {int(values['CompressorAlarm'])} | "
        f"Cold Room: {values['ColdRoomTemperature']:.2f} C | "
        f"Tank: {values['TankLevel']:.1f}%"
    )


def main() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    COMMAND_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    COMMAND_FILE_PATH.touch(exist_ok=True)

    simulator = FactorySimulator()

    print("MMG Factory Simulator")
    print(f"Database: {DATABASE_PATH}")
    print(f"Configuration: {CONFIG_FILE_PATH}")
    print(f"Mode: {SIMULATION_MODE}")
    print(f"Update interval: {UPDATE_INTERVAL:g} seconds")
    print(
        "Minor disturbances: every "
        f"{remaining_time(MINOR_INTERVAL[0])} to "
        f"{remaining_time(MINOR_INTERVAL[1])}"
    )
    print(
        "Automatic major faults: every "
        f"{remaining_time(MAJOR_INTERVAL[0])} to "
        f"{remaining_time(MAJOR_INTERVAL[1])}"
    )
    if SIMULATION_MODE == "demo":
        print(
            "Major scenario duration: "
            f"{remaining_time(float(CONFIG['demo_fault_duration']))}"
        )
    elif SIMULATION_MODE == "training":
        print(
            "Major scenario duration: "
            f"{remaining_time(float(CONFIG['training_fault_duration']))}"
        )
    else:
        print("Major scenario duration: individual realistic duration")
    print(f"Manual command file: {COMMAND_FILE_PATH}")
    print("Manual example: echo dirty_filter > config/simulator_command.txt")
    print("Reset example:  echo normal > config/simulator_command.txt")
    print(
        "Next automatic major fault in approximately "
        f"{remaining_time(simulator.until_major)}"
    )
    print("Press Ctrl+C to stop.\n")

    try:
        with sqlite3.connect(DATABASE_PATH) as connection:
            initialize_database(connection)
            while True:
                simulator.update()
                tags = simulator.tags()
                write_tags(connection, tags)
                display_status(simulator, tags)
                time.sleep(UPDATE_INTERVAL)
    except KeyboardInterrupt:
        print("\nFactory simulator stopped.")
    except sqlite3.Error as error:
        print(f"Database error: {error}")
    except Exception as error:
        print(f"Unexpected error: {error}")


if __name__ == "__main__":
    main()
