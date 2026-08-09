from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from config.configuration_manager import ConfigurationManager
from simulator.tag_dataset_model import MONOTONIC_UNITS, canonical_key


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "database" / "config.db"

# Mirrors the normal operating bands in simulator/tag_dataset_model.py's
# TAG_PROFILES - warning/alarm sits just outside the realistic normal
# range for that specific equipment/signal, not a generic per-unit
# guess. None means that tier deliberately isn't set (e.g. a motor
# running at "low" power isn't a fault, so no low_warning/low_alarm).
THRESHOLD_PROFILES: dict[str, dict[str, float | None]] = {
    "COLDROOM.CR.RoomTemp": {"low_warning": 1, "low_alarm": -1, "high_warning": 8, "high_alarm": 12},
    "COLDROOM.CR.Humidity": {"high_warning": 80, "high_alarm": 90},
    "COLDROOM.CR.CompressorPower": {"high_warning": 7, "high_alarm": 8.5},

    "ELEC.INCOMER.PF": {"low_warning": 0.85, "low_alarm": 0.80},
    "ELEC.INCOMER.Power_kW": {"high_warning": 55, "high_alarm": 65},

    "ELEC.MAIN.Current_L1": {"high_warning": 30, "high_alarm": 35},
    "ELEC.MAIN.Current_L2": {"high_warning": 30, "high_alarm": 35},
    "ELEC.MAIN.Current_L3": {"high_warning": 30, "high_alarm": 35},
    "ELEC.MAIN.Frequency": {"low_warning": 49.0, "low_alarm": 48.5, "high_warning": 51.0, "high_alarm": 51.5},
    "ELEC.MAIN.PF": {"low_warning": 0.85, "low_alarm": 0.80},
    "ELEC.MAIN.Power_kW": {"high_warning": 40, "high_alarm": 48},
    "ELEC.MAIN.THD_I": {"high_warning": 8, "high_alarm": 12},
    "ELEC.MAIN.THD_V": {"high_warning": 5, "high_alarm": 8},
    "ELEC.MAIN.Voltage_L1L2": {"low_warning": 380, "low_alarm": 370, "high_warning": 420, "high_alarm": 430},
    "ELEC.MAIN.Voltage_L2L3": {"low_warning": 380, "low_alarm": 370, "high_warning": 420, "high_alarm": 430},
    "ELEC.MAIN.Voltage_L3L1": {"low_warning": 380, "low_alarm": 370, "high_warning": 420, "high_alarm": 430},

    "ENV.Temperature": {"low_warning": 18, "low_alarm": 15, "high_warning": 38, "high_alarm": 42},
    "ENV.Humidity": {"low_warning": 30, "low_alarm": 20},

    "MCCROOM.ENV.Temperature": {"high_warning": 35, "high_alarm": 40},
    "MCCROOM.ENV.Humidity": {"high_warning": 60, "high_alarm": 70},

    "UTILITY.AC.OutletTemp": {"high_warning": 100, "high_alarm": 110},
    "UTILITY.AC.Pressure": {"low_warning": 6.0, "low_alarm": 5.0, "high_warning": 8.5, "high_alarm": 9.5},
    "UTILITY.AC.Power_kW": {"high_warning": 32, "high_alarm": 35},

    # DewPoint deliberately has no threshold profile - removed per
    # explicit request (2026-08-09), still simulated and viewable, just
    # not alarmed on.
    "UTILITY.AIRHDR.Flow": {"low_warning": 20, "high_warning": 140},
    "UTILITY.AIRHDR.Pressure": {"low_warning": 5.5, "low_alarm": 5.0, "high_warning": 7.5, "high_alarm": 8.0},

    "UTILITY.CHL.SupplyTemp": {"low_warning": 4, "low_alarm": 2, "high_warning": 12, "high_alarm": 16},
    "UTILITY.CHL.ReturnTemp": {"low_warning": 8, "low_alarm": 6, "high_warning": 18, "high_alarm": 24},
    "UTILITY.CHL.Power_kW": {"high_warning": 100, "high_alarm": 115},
    "UTILITY.CHL.LoadPct": {"high_warning": 95, "high_alarm": 100},
    "UTILITY.CHL.WaterFlow": {"low_warning": 30, "low_alarm": 15},

    "UTILITY.CHWP.BearingTemp": {"high_warning": 70, "high_alarm": 85},
    "UTILITY.CHWP.Current": {"high_warning": 30, "high_alarm": 35},
    "UTILITY.CHWP.DischargePressure": {"low_warning": 2.5, "low_alarm": 1.5, "high_warning": 6, "high_alarm": 7},
    "UTILITY.CHWP.SuctionPressure": {"low_warning": 1.5, "low_alarm": 0.5},
    "UTILITY.CHWP.Flow": {"low_warning": 15, "low_alarm": 8},
    "UTILITY.CHWP.Frequency": {"low_warning": 40, "low_alarm": 35, "high_warning": 55, "high_alarm": 58},
    "UTILITY.CHWP.Power_kW": {"high_warning": 25, "high_alarm": 30},
    "UTILITY.CHWP.Vibration": {"high_warning": 4.5, "high_alarm": 7.1},

    "WATER.MTR.Flow": {"high_warning": 100, "high_alarm": 150},

    "WATER.SYS.HeaderPressure": {"low_warning": 4.0, "low_alarm": 3.0, "high_warning": 6.5, "high_alarm": 7.0},
    "WATER.SYS.TankLevel": {"low_warning": 20, "low_alarm": 10, "high_warning": 90, "high_alarm": 98},

    "WATER.WSP.BearingTemp": {"high_warning": 70, "high_alarm": 85},
    "WATER.WSP.Current": {"high_warning": 18, "high_alarm": 22},
    "WATER.WSP.DischargePressure": {"low_warning": 2.5, "low_alarm": 1.5, "high_warning": 6, "high_alarm": 7},
    "WATER.WSP.SuctionPressure": {"low_warning": 1.5, "low_alarm": 0.5},
    "WATER.WSP.Flow": {"low_warning": 12, "low_alarm": 6},
    "WATER.WSP.Frequency": {"low_warning": 40, "low_alarm": 35, "high_warning": 55, "high_alarm": 58},
    "WATER.WSP.Power_kW": {"high_warning": 22, "high_alarm": 27},
    "WATER.WSP.Vibration": {"high_warning": 4.5, "high_alarm": 7.1},
}

# Excluded from thresholds even though numeric: cumulative
# totals/counters (never meant to be "inside/outside a range") and
# pure control setpoints (a target, not a measurement).
EXCLUDED_SUFFIXES = ("Setpoint", "StartCount")


def _threshold_eligible_tags(database_path: Path) -> list[dict]:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row

    try:
        rows = connection.execute(
            """
            SELECT tag_name, unit
            FROM tags
            WHERE enabled = 1 AND data_type = 'REAL'
            """
        ).fetchall()
    finally:
        connection.close()

    return [
        dict(row)
        for row in rows
        if row["unit"] not in MONOTONIC_UNITS
        and not row["tag_name"].endswith(EXCLUDED_SUFFIXES)
    ]


def seed(database_path: Path, username: str = "system") -> None:
    manager = ConfigurationManager(database_path=database_path)
    tags = _threshold_eligible_tags(database_path)

    applied = 0
    skipped_no_profile: list[str] = []

    for tag in tags:
        key = canonical_key(tag["tag_name"])
        profile = THRESHOLD_PROFILES.get(key)

        if profile is None:
            skipped_no_profile.append(tag["tag_name"])
            continue

        for parameter in ("low_warning", "low_alarm", "high_warning", "high_alarm"):
            value = profile.get(parameter)

            if value is not None:
                manager.set_threshold(
                    tag_name=tag["tag_name"],
                    parameter=parameter,
                    value=value,
                    username=username,
                )

        applied += 1

    print(f"Thresholds applied to {applied} tags.")

    if skipped_no_profile:
        print(f"No profile found for {len(skipped_no_profile)} tags (left unconfigured):")
        for name in skipped_no_profile:
            print(f"  - {name}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fill in engineering thresholds for the P01 tag dataset.",
    )

    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH))
    parser.add_argument("--username", default="system")

    args = parser.parse_args()

    seed(Path(args.database), username=args.username)


if __name__ == "__main__":
    main()
