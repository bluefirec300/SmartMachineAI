from __future__ import annotations

import random
import sqlite3
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DATABASE_PATH = PROJECT_ROOT / "database" / "config.db"

# Plausible generic value bands per unit. Not calibrated against real
# equipment specs - this is illustrative simulation data, the same
# spirit as the rest of this project's dev-stage simulator.
UNIT_PROFILES: dict[str, dict[str, float]] = {
    "°C": {"low": 15, "high": 45, "noise": 0.3, "revert": 0.05},
    "°Cdp": {"low": -20, "high": 5, "noise": 0.3, "revert": 0.05},
    "bar": {"low": 2, "high": 8, "noise": 0.05, "revert": 0.05},
    "kW": {"low": 5, "high": 60, "noise": 0.5, "revert": 0.05},
    "A": {"low": 5, "high": 30, "noise": 0.3, "revert": 0.05},
    "m³/h": {"low": 10, "high": 100, "noise": 1.0, "revert": 0.05},
    "Nm³/h": {"low": 10, "high": 100, "noise": 1.0, "revert": 0.05},
    "Hz": {"low": 48, "high": 52, "noise": 0.05, "revert": 0.1},
    "%": {"low": 20, "high": 80, "noise": 0.5, "revert": 0.05},
    "%RH": {"low": 40, "high": 70, "noise": 0.4, "revert": 0.05},
    "mm/s": {"low": 0.5, "high": 3.0, "noise": 0.05, "revert": 0.05},
    "V": {"low": 380, "high": 415, "noise": 0.5, "revert": 0.05},
    "PF": {"low": 0.85, "high": 0.98, "noise": 0.005, "revert": 0.05},
}

DEFAULT_PROFILE = {"low": 10, "high": 50, "noise": 0.5, "revert": 0.05}

MONOTONIC_UNITS = {"kWh", "h", "m³"}

FAULT_DIRECTION_DOWN = {"pressure", "flow", "level"}

# Per-cycle probability a dormant equipment instance starts a fault
# episode. At a 2s poll interval this averages roughly one episode
# every 30-60 minutes per instance.
FAULT_TRIGGER_PROBABILITY = 0.0006

DEVELOPING_CYCLES = (20, 60)
FAULTED_CYCLES = (10, 30)
RECOVERING_CYCLES = (20, 40)

FAULT_CODES = ("E101", "E204", "E317", "E450")


class _InstanceFaultState:
    """
    One equipment instance's shared fault lifecycle.

    dormant -> developing -> faulted -> recovering -> dormant

    All tags belonging to the same instance read this same state, so
    a fault shows up as a correlated pattern (temperature/current/
    vibration drifting together, the alarm bit flipping, the alarm
    code populating) rather than independent random noise per tag -
    the same "logical" fault shape as the existing compressor cycle,
    generalized instead of hand-scripted per equipment type.
    """

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng
        self.phase = "dormant"
        self._remaining = 0
        self._span = 1
        self.just_entered_fault = False

    def advance(self) -> None:
        self.just_entered_fault = False

        if self.phase == "dormant":
            if self._rng.random() < FAULT_TRIGGER_PROBABILITY:
                self.phase = "developing"
                self._span = self._rng.randint(*DEVELOPING_CYCLES)
                self._remaining = self._span
            return

        self._remaining -= 1

        if self._remaining > 0:
            return

        if self.phase == "developing":
            self.phase = "faulted"
            self._span = self._rng.randint(*FAULTED_CYCLES)
            self._remaining = self._span
            self.just_entered_fault = True
        elif self.phase == "faulted":
            self.phase = "recovering"
            self._span = self._rng.randint(*RECOVERING_CYCLES)
            self._remaining = self._span
        elif self.phase == "recovering":
            self.phase = "dormant"

    def severity(self) -> float:
        """0.0 (normal) to 1.0 (fully faulted)."""
        if self.phase == "dormant":
            return 0.0

        if self.phase == "developing":
            return 1.0 - (self._remaining / self._span)

        if self.phase == "faulted":
            return 1.0

        if self.phase == "recovering":
            return self._remaining / self._span

        return 0.0


class TagDatasetSimulator:
    """
    Generic value generator for the imported master-tag-list dataset.

    Deliberately separate from FactorySimulator (simulator/factory_model.py)
    - that class's hand-scripted compressor fault cycle is tested and
    working, and this class must not risk disturbing it. This only
    generates values for tags imported by engine.tag_dataset_importer,
    grouped by equipment instance so faults read as one coherent event
    per instance instead of independent per-tag noise.
    """

    def __init__(
        self,
        database_path: str | Path = DEFAULT_DATABASE_PATH,
    ) -> None:
        self.database_path = Path(database_path)
        self._tags = self._load_tags()
        self._rng = random.Random()

        self._values: dict[str, Any] = {}
        self._instances: dict[str, _InstanceFaultState] = {}

        self._initialize_state()

    def _load_tags(self) -> list[dict[str, Any]]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row

        try:
            rows = connection.execute(
                """
                SELECT tag_name, data_type, unit, measurement, event_type
                FROM tags
                WHERE enabled = 1
                  AND driver = 'simulator'
                  AND tag_name LIKE '%.%.%.%'
                ORDER BY tag_name
                """
            ).fetchall()
        finally:
            connection.close()

        return [dict(row) for row in rows]

    @staticmethod
    def _instance_key(tag_name: str) -> str:
        parts = tag_name.split(".")
        return ".".join(parts[:-1]) if len(parts) >= 2 else tag_name

    @staticmethod
    def _profile(unit: str) -> dict[str, float]:
        return UNIT_PROFILES.get(unit, DEFAULT_PROFILE)

    def _initialize_state(self) -> None:
        for tag in self._tags:
            instance_key = self._instance_key(tag["tag_name"])

            if instance_key not in self._instances:
                seed = random.Random(instance_key).random()
                self._instances[instance_key] = _InstanceFaultState(
                    random.Random(int(seed * 1_000_000))
                )

            self._values[tag["tag_name"]] = self._initial_value(tag)

    def _initial_value(self, tag: dict[str, Any]) -> Any:
        data_type = tag["data_type"]
        unit = tag["unit"]
        seed_rng = random.Random(tag["tag_name"])

        if data_type == "BOOL":
            return 0

        if data_type == "STRING":
            return "None"

        if data_type == "INT":
            return 0

        # REAL
        if unit in MONOTONIC_UNITS:
            return round(seed_rng.uniform(0, 1000), 1)

        profile = self._profile(unit)
        return round(seed_rng.uniform(profile["low"], profile["high"]), 2)

    def _update_real(
        self,
        tag: dict[str, Any],
        severity: float,
    ) -> float:
        unit = tag["unit"]
        current = self._values[tag["tag_name"]]

        if unit in MONOTONIC_UNITS:
            increment = self._rng.uniform(0.001, 0.05)
            return round(current + increment, 3)

        profile = self._profile(unit)
        baseline = (profile["low"] + profile["high"]) / 2
        band = profile["high"] - profile["low"]

        noise = self._rng.uniform(-profile["noise"], profile["noise"])
        reversion = (baseline - current) * profile["revert"]

        direction = (
            -1
            if tag["measurement"] in FAULT_DIRECTION_DOWN
            else 1
        )

        fault_push = direction * severity * band * 0.35

        value = current + noise + reversion + fault_push

        return round(
            max(0.0, value)
            if profile["low"] >= 0
            else value,
            3,
        )

    def _update_bool(
        self,
        tag: dict[str, Any],
        severity: float,
    ) -> int:
        is_alarm_style = tag["event_type"] in {"alarm", "warning"}

        if is_alarm_style:
            return 1 if severity >= 0.6 else 0

        current = self._values[tag["tag_name"]]

        if tag["measurement"] == "running":
            # Mostly running; drops out while faulted/recovering.
            if severity >= 0.6:
                return 0
            return 1 if self._rng.random() > 0.002 else 0

        # Generic status bit (door/defrost/etc.) - rare toggle.
        if self._rng.random() < 0.003:
            return 0 if current else 1

        return current

    def _update_int(
        self,
        tag: dict[str, Any],
        instance: _InstanceFaultState,
    ) -> int:
        current = self._values[tag["tag_name"]]

        if instance.just_entered_fault or self._rng.random() < 0.0005:
            return current + 1

        return current

    def _update_string(
        self,
        severity: float,
    ) -> str:
        if severity >= 1.0:
            return self._rng.choice(FAULT_CODES)

        return "None"

    def update_values(self) -> None:
        for instance in self._instances.values():
            instance.advance()

        for tag in self._tags:
            tag_name = tag["tag_name"]
            instance = self._instances[self._instance_key(tag_name)]
            severity = instance.severity()
            data_type = tag["data_type"]

            if data_type == "REAL":
                self._values[tag_name] = self._update_real(tag, severity)
            elif data_type == "BOOL":
                self._values[tag_name] = self._update_bool(tag, severity)
            elif data_type == "INT":
                self._values[tag_name] = self._update_int(tag, instance)
            elif data_type == "STRING":
                self._values[tag_name] = self._update_string(severity)

    def get_tags(self) -> list[tuple[str, str, Any]]:
        return [
            (tag["tag_name"], tag["tag_name"], self._values[tag["tag_name"]])
            for tag in self._tags
        ]
