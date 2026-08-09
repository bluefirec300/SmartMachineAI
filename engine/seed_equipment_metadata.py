from __future__ import annotations

import argparse
import random
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "database" / "config.db"

DEVICE_NUMBER_PREFIX = "EQ"

# Realistic (but not manufacturer-verified) brand/model/maintenance-
# interval assignments, one per equipment *type* rather than per
# instance - a real factory typically standardizes on one brand per
# equipment class. This is illustrative simulation data, the same
# spirit as the rest of this project's dev-stage simulator - not
# calibrated against actual purchased equipment.
TYPE_PROFILES: dict[str, tuple[str, str, int]] = {
    # (brand, model, service_interval_days)
    "compressor": ("Atlas Copco", "GA30+", 180),
    "air_compressor": ("Atlas Copco", "GA30+", 180),
    "air": ("Atlas Copco", "GA30+", 180),
    "compressed_air_header": ("SMC", "IDG-series dryer/header", 90),
    "chiller": ("Daikin", "EWAD240", 365),
    "cold_room": ("Bitzer", "4VES-6Y refrigeration unit", 180),
    "chilled_water_pump": ("Grundfos", "CR32-2", 180),
    "water_supply_pump": ("Grundfos", "CR32-2", 180),
    "pump": ("Grundfos", "CR15-2", 180),
    "tank": ("Endress+Hauser", "Micropilot FMR10 (level)", 365),
    "energy": ("Schneider Electric", "PowerLogic PM5560", 365),
    "water": ("Grundfos", "Hydro MPC-E", 365),
    "water_supply": ("Grundfos", "Hydro MPC-E", 365),
    "building_water_meter": ("Sensus", "iPERL", 365),
    "building_incomer": ("Schneider Electric", "Masterpact MTZ2", 365),
    "main_incomer": ("Schneider Electric", "Masterpact MTZ2", 365),
    "main_incomer_main": ("Schneider Electric", "Masterpact MTZ2", 365),
    "mcc_room": ("Vaisala", "GMW90 (room monitoring)", 365),
    "weather_node": ("Vaisala", "WXT536", 365),
    "ups": ("Eaton", "9395 UPS", 180),
    "fire_water_system": ("Xylem", "Fire pump skid", 90),
    "ahu": ("Carrier", "39M AHU", 90),
    "generator": ("Cummins", "C1100D5", 90),
    "transformer": ("ABB", "Dry-type distribution transformer", 365),
    "effluent_treatment": ("Evoqua Water Technologies", "Effluent treatment skid", 90),
    "ro_di_system": ("SUEZ Water Technologies", "RO/DI skid", 90),
    "water_treatment_system": ("Evoqua Water Technologies", "Water treatment skid", 90),
    "area_monitoring": ("Honeywell", "Sensepoint XCD gas detector", 180),
    "bead_mill": ("NETZSCH", "Zeta LMZ bead mill", 180),
    "filling_machine": ("IMA", "filling line", 90),
    "high_speed_disperser": ("Ross", "HSD high-speed disperser", 180),
    "mixer": ("Silverson", "high-shear mixer", 180),
    "dust_collector": ("Donaldson", "Torit dust collector", 90),
    "solvent_transfer": ("Graco", "solvent transfer pump", 90),
}

DEFAULT_PROFILE = ("Generic", "Unspecified", 365)


def _equipment_type_key(equipment_name: str) -> str:
    """
    Strip a plant prefix ("p01_"/"p02_") and trailing instance code
    ("_ac01") from an equipment.name slug to get its type category.

    e.g. "p01_air_compressor_ac01" -> "air_compressor"
         "compressor" -> "compressor" (original demo equipment, no
         plant prefix or instance code to strip)
    """
    name = re.sub(r"^p\d+_", "", equipment_name)
    name = re.sub(r"_[a-z]+\d+$", "", name)
    return name


def seed_metadata(
    database_path: Path,
    seed: int = 42,
) -> dict[str, int]:
    rng = random.Random(seed)

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row

    try:
        rows = connection.execute(
            """
            SELECT id, name, device_number, brand, model,
                   service_interval_days, last_serviced_at
            FROM equipment
            ORDER BY id
            """
        ).fetchall()

        updated = 0
        skipped_existing = 0

        for row in rows:
            if row["brand"] and row["device_number"]:
                skipped_existing += 1
                continue

            type_key = _equipment_type_key(row["name"])
            brand, model, interval_days = TYPE_PROFILES.get(
                type_key, DEFAULT_PROFILE
            )

            device_number = f"{DEVICE_NUMBER_PREFIX}-{row['id']:04d}"

            # Stagger service dates across roughly [0.2x, 1.2x] of the
            # interval before today, so some equipment reads as
            # recently serviced and some as coming due/overdue -
            # realistic variety rather than everything on day zero.
            days_ago = rng.randint(
                int(interval_days * 0.2),
                int(interval_days * 1.2),
            )
            last_serviced_at = (
                datetime.now() - timedelta(days=days_ago)
            ).strftime("%Y-%m-%d")

            connection.execute(
                """
                UPDATE equipment SET
                    device_number = ?, brand = ?, model = ?,
                    service_interval_days = ?, last_serviced_at = ?
                WHERE id = ?
                """,
                (
                    device_number, brand, model, interval_days,
                    last_serviced_at, row["id"],
                ),
            )

            updated += 1

        connection.commit()

    finally:
        connection.close()

    return {"updated": updated, "skipped_existing": skipped_existing}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed device_number/brand/model/maintenance data for equipment.",
    )

    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH))
    parser.add_argument("--seed", type=int, default=42)

    args = parser.parse_args()

    result = seed_metadata(Path(args.database), seed=args.seed)
    print(result)


if __name__ == "__main__":
    main()
