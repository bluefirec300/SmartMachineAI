from __future__ import annotations
from config.environment import get_config_db_path

import random
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from simulator import plant_context

# Approximates config/settings.ini's scan_interval (2s) - used only to
# integrate Energy_kWh realistically from a paired Power_kW-style tag
# (see _update_real()). Not a hard timing requirement; a 1-cycle lag
# between the two is imperceptible at this poll rate.
SIMULATED_TICK_SECONDS = 2.0


PROJECT_ROOT = Path(__file__).resolve().parent.parent


DEFAULT_DATABASE_PATH = get_config_db_path()

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

MONOTONIC_UNITS = {"kWh", "h", "m³", "Nm³", "L"}

# Per-signal normal operating bands, keyed by canonical_key(tag_name) -
# see that function. Unlike UNIT_PROFILES (generic per unit, so every
# "°C" tag shared one band regardless of what it actually measured), a
# cold room and a compressor discharge temperature now get realistic,
# distinct ranges. noise/revert are carried over from the unit-level
# defaults above (same feel, just centred on a real-world band) rather
# than hand-tuned per tag. Falls back to UNIT_PROFILES for anything not
# listed here (e.g. tags added by a future migration phase), so this is
# additive/safe to extend, never a hard requirement.
TAG_PROFILES: dict[str, dict[str, float]] = {
    # Cold Room (Bitzer 4VES-6Y refrigeration unit)
    "COLDROOM.CR.RoomTemp": {"low": 2, "high": 6, "noise": 0.3, "revert": 0.05},
    "COLDROOM.CR.Humidity": {"low": 50, "high": 70, "noise": 0.4, "revert": 0.05},
    "COLDROOM.CR.CompressorPower": {"low": 3, "high": 6, "noise": 0.5, "revert": 0.05},
    # Building/factory electrical incomers (Schneider Masterpact MTZ2 / PowerLogic)
    "ELEC.INCOMER.PF": {"low": 0.90, "high": 0.98, "noise": 0.005, "revert": 0.05},
    "ELEC.INCOMER.Power_kW": {"low": 20, "high": 50, "noise": 0.5, "revert": 0.05},
    "ELEC.MAIN.Current_L1": {"low": 15, "high": 25, "noise": 0.3, "revert": 0.05},
    "ELEC.MAIN.Current_L2": {"low": 15, "high": 25, "noise": 0.3, "revert": 0.05},
    "ELEC.MAIN.Current_L3": {"low": 15, "high": 25, "noise": 0.3, "revert": 0.05},
    "ELEC.MAIN.Frequency": {"low": 49.5, "high": 50.5, "noise": 0.05, "revert": 0.1},
    "ELEC.MAIN.PF": {"low": 0.90, "high": 0.98, "noise": 0.005, "revert": 0.05},
    "ELEC.MAIN.Power_kW": {"low": 20, "high": 35, "noise": 0.5, "revert": 0.05},
    "ELEC.MAIN.THD_I": {"low": 2, "high": 5, "noise": 0.2, "revert": 0.05},
    "ELEC.MAIN.THD_V": {"low": 1, "high": 3, "noise": 0.2, "revert": 0.05},
    "ELEC.MAIN.Voltage_L1L2": {"low": 390, "high": 410, "noise": 0.5, "revert": 0.05},
    "ELEC.MAIN.Voltage_L2L3": {"low": 390, "high": 410, "noise": 0.5, "revert": 0.05},
    "ELEC.MAIN.Voltage_L3L1": {"low": 390, "high": 410, "noise": 0.5, "revert": 0.05},
    # Outdoor ambient sensor
    "ENV.Temperature": {"low": 24, "high": 34, "noise": 0.3, "revert": 0.05},
    "ENV.Humidity": {"low": 60, "high": 90, "noise": 0.4, "revert": 0.05},
    # MCC (electrical) room environment
    "MCCROOM.ENV.Temperature": {"low": 24, "high": 32, "noise": 0.3, "revert": 0.05},
    "MCCROOM.ENV.Humidity": {"low": 40, "high": 55, "noise": 0.4, "revert": 0.05},
    # Air Compressor (Atlas Copco GA30+)
    "UTILITY.AC.OutletTemp": {"low": 75, "high": 95, "noise": 0.3, "revert": 0.05},
    "UTILITY.AC.Pressure": {"low": 6.5, "high": 8.0, "noise": 0.05, "revert": 0.05},
    "UTILITY.AC.Power_kW": {"low": 22, "high": 30, "noise": 0.5, "revert": 0.05},
    # Compressed-air header
    "UTILITY.AIRHDR.DewPoint": {"low": -25, "high": -10, "noise": 0.3, "revert": 0.05},
    "UTILITY.AIRHDR.Flow": {"low": 60, "high": 100, "noise": 1.0, "revert": 0.05},
    "UTILITY.AIRHDR.Pressure": {"low": 6.0, "high": 7.0, "noise": 0.05, "revert": 0.05},
    # Chiller (Daikin EWAD240)
    "UTILITY.CHL.SupplyTemp": {"low": 5.5, "high": 7.5, "noise": 0.3, "revert": 0.05},
    "UTILITY.CHL.ReturnTemp": {"low": 11, "high": 14, "noise": 0.3, "revert": 0.05},
    "UTILITY.CHL.Power_kW": {"low": 50, "high": 90, "noise": 0.5, "revert": 0.05},
    "UTILITY.CHL.LoadPct": {"low": 40, "high": 90, "noise": 0.5, "revert": 0.05},
    "UTILITY.CHL.WaterFlow": {"low": 45, "high": 65, "noise": 1.0, "revert": 0.05},
    # Chilled Water Pump
    "UTILITY.CHWP.BearingTemp": {"low": 30, "high": 50, "noise": 0.3, "revert": 0.05},
    "UTILITY.CHWP.Current": {"low": 15, "high": 25, "noise": 0.3, "revert": 0.05},
    "UTILITY.CHWP.DischargePressure": {"low": 3.5, "high": 5.0, "noise": 0.05, "revert": 0.05},
    "UTILITY.CHWP.SuctionPressure": {"low": 3.0, "high": 4.5, "noise": 0.05, "revert": 0.05},
    "UTILITY.CHWP.Flow": {"low": 25, "high": 50, "noise": 1.0, "revert": 0.05},
    "UTILITY.CHWP.Frequency": {"low": 45, "high": 52, "noise": 0.05, "revert": 0.1},
    "UTILITY.CHWP.Power_kW": {"low": 10, "high": 20, "noise": 0.5, "revert": 0.05},
    "UTILITY.CHWP.Vibration": {"low": 1.0, "high": 3.0, "noise": 0.05, "revert": 0.05},
    # Building Water Meter (Sensus iPERL)
    "WATER.MTR.Flow": {"low": 15, "high": 40, "noise": 1.0, "revert": 0.05},
    # Water distribution system
    "WATER.SYS.HeaderPressure": {"low": 4.5, "high": 6.0, "noise": 0.05, "revert": 0.05},
    "WATER.SYS.TankLevel": {"low": 30, "high": 85, "noise": 0.5, "revert": 0.05},
    # Water Supply Pump (smaller than the chilled water pumps)
    "WATER.WSP.BearingTemp": {"low": 30, "high": 50, "noise": 0.3, "revert": 0.05},
    "WATER.WSP.Current": {"low": 8, "high": 15, "noise": 0.3, "revert": 0.05},
    "WATER.WSP.DischargePressure": {"low": 3.5, "high": 5.0, "noise": 0.05, "revert": 0.05},
    "WATER.WSP.SuctionPressure": {"low": 3.0, "high": 4.5, "noise": 0.05, "revert": 0.05},
    "WATER.WSP.Flow": {"low": 20, "high": 40, "noise": 1.0, "revert": 0.05},
    "WATER.WSP.Frequency": {"low": 45, "high": 52, "noise": 0.05, "revert": 0.1},
    "WATER.WSP.Power_kW": {"low": 8, "high": 18, "noise": 0.5, "revert": 0.05},
    "WATER.WSP.Vibration": {"low": 1.0, "high": 3.0, "noise": 0.05, "revert": 0.05},

    # --- Phase 2/3 (P01 tag-dataset expansion, 2026-08-10) ---

    # Standby Diesel Generator (Cummins C1100D5)
    "ELEC.GEN.BatteryVoltage": {"low": 24, "high": 28, "noise": 0.1, "revert": 0.05},
    "ELEC.GEN.CoolantTemp": {"low": 80, "high": 95, "noise": 0.3, "revert": 0.05},
    "ELEC.GEN.FuelLevel": {"low": 40, "high": 100, "noise": 0.3, "revert": 0.02},
    "ELEC.GEN.Power_kW": {"low": 400, "high": 750, "noise": 3.0, "revert": 0.05},

    # Dry-type Distribution Transformer (ABB)
    "ELEC.TR.CurrentImbalance": {"low": 0.5, "high": 3.0, "noise": 0.1, "revert": 0.05},
    "ELEC.TR.LoadPct": {"low": 30, "high": 70, "noise": 0.5, "revert": 0.05},
    "ELEC.TR.THD": {"low": 1.0, "high": 4.0, "noise": 0.1, "revert": 0.05},
    "ELEC.TR.Temperature": {"low": 60, "high": 90, "noise": 0.3, "revert": 0.05},

    # AHU (Carrier 39CP/39M)
    "HVAC.AHU.FanFrequency": {"low": 35, "high": 50, "noise": 0.1, "revert": 0.1},
    "HVAC.AHU.FanPower": {"low": 5, "high": 15, "noise": 0.3, "revert": 0.05},
    "HVAC.AHU.FilterDP": {"low": 50, "high": 150, "noise": 2.0, "revert": 0.05},
    "HVAC.AHU.Humidity": {"low": 40, "high": 55, "noise": 0.4, "revert": 0.05},
    "HVAC.AHU.ReturnAirTemp": {"low": 22, "high": 26, "noise": 0.2, "revert": 0.05},
    "HVAC.AHU.SupplyAirTemp": {"low": 12, "high": 18, "noise": 0.2, "revert": 0.05},
    "HVAC.AHU.ValvePosition": {"low": 20, "high": 80, "noise": 1.0, "revert": 0.05},

    # UPS (Eaton 9395)
    "IT.UPS.BatteryPct": {"low": 85, "high": 100, "noise": 0.2, "revert": 0.02},
    "IT.UPS.BatteryTemp": {"low": 20, "high": 30, "noise": 0.2, "revert": 0.05},
    "IT.UPS.EstimatedRuntime": {"low": 15, "high": 45, "noise": 0.5, "revert": 0.05},
    "IT.UPS.LoadPct": {"low": 30, "high": 70, "noise": 0.5, "revert": 0.05},

    # Fire Water System (Xylem diesel fire pump package)
    "FIRE.SYS.BatteryVoltage": {"low": 24, "high": 28, "noise": 0.1, "revert": 0.05},
    "FIRE.SYS.DieselFuelLevel": {"low": 70, "high": 100, "noise": 0.3, "revert": 0.02},
    "FIRE.SYS.HeaderPressure": {"low": 7, "high": 10, "noise": 0.05, "revert": 0.05},
    "FIRE.SYS.TankLevel": {"low": 80, "high": 100, "noise": 0.3, "revert": 0.02},

    # RO/DI System (Grundfos Hydro MPC-E skid)
    "WT.RO.ConductivityIn": {"low": 300, "high": 800, "noise": 5.0, "revert": 0.05},
    "WT.RO.ConductivityOut": {"low": 5, "high": 30, "noise": 1.0, "revert": 0.05},
    "WT.RO.FeedPressure": {"low": 10, "high": 16, "noise": 0.1, "revert": 0.05},
    "WT.RO.MembraneDP": {"low": 0.3, "high": 1.0, "noise": 0.02, "revert": 0.05},
    "WT.RO.PermeateFlow": {"low": 5, "high": 12, "noise": 0.2, "revert": 0.05},
    "WT.RO.Power_kW": {"low": 15, "high": 30, "noise": 0.5, "revert": 0.05},
    "WT.RO.RejectFlow": {"low": 3, "high": 8, "noise": 0.2, "revert": 0.05},

    # Water Treatment System (Evoqua skid)
    "WT.SYS.ChemicalLevel": {"low": 40, "high": 100, "noise": 0.3, "revert": 0.02},
    "WT.SYS.Conductivity": {"low": 200, "high": 600, "noise": 5.0, "revert": 0.05},
    "WT.SYS.DosingRate": {"low": 0.5, "high": 5.0, "noise": 0.1, "revert": 0.05},
    "WT.SYS.FilterDP": {"low": 0.2, "high": 0.5, "noise": 0.02, "revert": 0.05},
    "WT.SYS.RawWaterFlow": {"low": 10, "high": 30, "noise": 0.5, "revert": 0.05},
    "WT.SYS.TreatedWaterFlow": {"low": 10, "high": 28, "noise": 0.5, "revert": 0.05},
    "WT.SYS.Turbidity": {"low": 0.1, "high": 0.8, "noise": 0.05, "revert": 0.05},
    "WT.SYS.pH": {"low": 6.8, "high": 7.8, "noise": 0.03, "revert": 0.05},

    # Effluent / Wastewater Treatment (Evoqua skid)
    "WW.SYS.BlowerPower": {"low": 5, "high": 20, "noise": 0.3, "revert": 0.05},
    "WW.SYS.DO": {"low": 2, "high": 5, "noise": 0.1, "revert": 0.05},
    "WW.SYS.EffluentFlow": {"low": 5, "high": 20, "noise": 0.5, "revert": 0.05},
    "WW.SYS.InfluentFlow": {"low": 5, "high": 20, "noise": 0.5, "revert": 0.05},
    "WW.SYS.ORP": {"low": 50, "high": 200, "noise": 3.0, "revert": 0.05},
    "WW.SYS.SludgeLevel": {"low": 20, "high": 60, "noise": 0.5, "revert": 0.05},
    "WW.SYS.Turbidity": {"low": 5, "high": 20, "noise": 0.5, "revert": 0.05},
    "WW.SYS.pH": {"low": 6.5, "high": 8.0, "noise": 0.03, "revert": 0.05},

    # Filling Machine (IMA line)
    "FILL.FILL.ActualWeight": {"low": 0.95, "high": 1.05, "noise": 0.005, "revert": 0.05},
    "FILL.FILL.ContainerSize": {"low": 0.95, "high": 1.05, "noise": 0.0, "revert": 0.05},
    "FILL.FILL.CycleTime": {"low": 2.0, "high": 5.0, "noise": 0.1, "revert": 0.05},
    "FILL.FILL.Downtime": {"low": 0, "high": 30, "noise": 1.0, "revert": 0.05},
    "FILL.FILL.Power_kW": {"low": 3, "high": 10, "noise": 0.2, "revert": 0.05},
    "FILL.FILL.TargetWeight": {"low": 0.95, "high": 1.05, "noise": 0.0, "revert": 0.05},

    # High-Speed Disperser (Ross HSD)
    "PROD.DISP.BearingTemp": {"low": 40, "high": 65, "noise": 0.3, "revert": 0.05},
    "PROD.DISP.MotorCurrent": {"low": 20, "high": 40, "noise": 0.3, "revert": 0.05},
    "PROD.DISP.MotorPower": {"low": 10, "high": 25, "noise": 0.5, "revert": 0.05},
    "PROD.DISP.ProcessTemp": {"low": 25, "high": 50, "noise": 0.3, "revert": 0.05},
    "PROD.DISP.Speed": {"low": 800, "high": 2500, "noise": 10.0, "revert": 0.05},
    "PROD.DISP.Vibration": {"low": 1.0, "high": 3.0, "noise": 0.05, "revert": 0.05},

    # Bead Mill (NETZSCH Zeta LMZ)
    "PROD.MILL.BearingTemp": {"low": 40, "high": 65, "noise": 0.3, "revert": 0.05},
    "PROD.MILL.MotorCurrent": {"low": 15, "high": 30, "noise": 0.3, "revert": 0.05},
    "PROD.MILL.MotorPower": {"low": 7, "high": 18, "noise": 0.4, "revert": 0.05},
    "PROD.MILL.ProcessTemp": {"low": 25, "high": 50, "noise": 0.3, "revert": 0.05},
    "PROD.MILL.Speed": {"low": 500, "high": 1500, "noise": 8.0, "revert": 0.05},
    "PROD.MILL.Vibration": {"low": 1.0, "high": 3.0, "noise": 0.05, "revert": 0.05},

    # Mixer (Silverson high-shear)
    "PROD.MIX.BearingTemp": {"low": 40, "high": 65, "noise": 0.3, "revert": 0.05},
    "PROD.MIX.MotorCurrent": {"low": 15, "high": 35, "noise": 0.3, "revert": 0.05},
    "PROD.MIX.MotorPower": {"low": 8, "high": 20, "noise": 0.4, "revert": 0.05},
    "PROD.MIX.ProcessTemp": {"low": 25, "high": 50, "noise": 0.3, "revert": 0.05},
    "PROD.MIX.Speed": {"low": 1000, "high": 3000, "noise": 10.0, "revert": 0.05},
    "PROD.MIX.Vibration": {"low": 1.0, "high": 3.0, "noise": 0.05, "revert": 0.05},

    # Dust Collector (Donaldson Torit)
    "DUST.DC.FanPower": {"low": 5, "high": 20, "noise": 0.3, "revert": 0.05},
    "DUST.DC.FilterDP": {"low": 300, "high": 800, "noise": 5.0, "revert": 0.05},
    "DUST.DC.HopperLevel": {"low": 10, "high": 50, "noise": 0.5, "revert": 0.05},

    # Solvent Transfer
    "SOLV.SYS.Flow": {"low": 10, "high": 40, "noise": 0.5, "revert": 0.05},
    "SOLV.SYS.LinePressure": {"low": 2.0, "high": 5.0, "noise": 0.05, "revert": 0.05},
    "SOLV.SYS.PumpCurrent": {"low": 5, "high": 15, "noise": 0.3, "revert": 0.05},

    # Tank (generic process/storage)
    "TANK.TK.AgitatorCurrent": {"low": 5, "high": 15, "noise": 0.3, "revert": 0.05},
    "TANK.TK.AgitatorSpeed": {"low": 30, "high": 100, "noise": 1.0, "revert": 0.05},
    "TANK.TK.Level": {"low": 20, "high": 80, "noise": 0.5, "revert": 0.05},
    "TANK.TK.Temperature": {"low": 20, "high": 40, "noise": 0.3, "revert": 0.05},
    "TANK.TK.Weight": {"low": 200, "high": 800, "noise": 3.0, "revert": 0.05},

    # Area Monitoring - Laboratory/QC (tightly controlled) and
    # Warehouse (looser, more ambient-influenced) - same shape as the
    # existing MCCROOM.ENV entries, distinct bands per area type.
    "LABORATORYQC.ENV.Temperature": {"low": 20, "high": 24, "noise": 0.2, "revert": 0.05},
    "LABORATORYQC.ENV.Humidity": {"low": 40, "high": 55, "noise": 0.4, "revert": 0.05},
    "LABORATORYQC.ENV.Power_kW": {"low": 2, "high": 8, "noise": 0.2, "revert": 0.05},
    "WAREHOUSE.ENV.Temperature": {"low": 18, "high": 32, "noise": 0.3, "revert": 0.05},
    "WAREHOUSE.ENV.Humidity": {"low": 35, "high": 65, "noise": 0.4, "revert": 0.05},
    "WAREHOUSE.ENV.Power_kW": {"low": 5, "high": 15, "noise": 0.3, "revert": 0.05},
}


# canonical_key()/instance_key() now live in simulator/plant_context.py
# (Phase 5 - that module needs them too, for the Main Incomer aggregation
# boundary). Re-exported here unchanged so existing callers (the
# Simulator Control UI page) keep working without a code change.
canonical_key = plant_context.canonical_key
instance_key = plant_context.instance_key


FAULT_DIRECTION_DOWN = {"pressure", "flow", "level"}

# Per-cycle probability a dormant equipment instance starts a fault
# episode. At a 2s poll interval this averages roughly one episode
# every 30-60 minutes per instance.
FAULT_TRIGGER_PROBABILITY = 0.0006

DEVELOPING_CYCLES = (20, 60)
FAULTED_CYCLES = (10, 30)
RECOVERING_CYCLES = (20, 40)

FAULT_CODES = ("E101", "E204", "E317", "E450")

# Only *.AlarmCode is genuinely fault-related (cycles between "None"
# and a fault code as the instance's severity changes - see
# _update_string). Every other STRING signal in this dataset is
# informational (a batch/product/material identity, not a fault), and
# gets a plausible-looking, STABLE value instead - generated once and
# then left alone, same as a real batch number wouldn't change every
# 2-second poll. Keyed by the tag's final segment (the signal name).
STRING_KIND_GENERATORS = {
    "BatchNumber": lambda rng: f"BATCH-{rng.randint(10000, 99999)}",
    "ProductCode": lambda rng: f"PROD-{rng.choice('ABCD')}{rng.randint(100, 999)}",
    "MaterialID": lambda rng: f"MAT-{rng.randint(1000, 9999)}",
    "MaterialCode": lambda rng: f"MAT-{rng.randint(1000, 9999)}",
    "SourceTank": lambda rng: f"TK{rng.randint(1, 4):02d}",
    "DestinationTank": lambda rng: f"TK{rng.randint(1, 4):02d}",
}


def _generic_string_value(tag_name: str) -> str:
    signal = tag_name.rsplit(".", 1)[-1]
    generator = STRING_KIND_GENERATORS.get(signal)

    if generator is None:
        return "N/A"

    return generator(random.Random(tag_name))


def _schedule_blended_baseline(profile: dict[str, float], baseline: float, schedule_factor: float, use_wide_anchor: bool) -> float:
    """Post-Phase-14 follow-up. Convex blend between an anchor and the
    tag's normal full-load baseline, weighted by schedule_factor
    (1.0 = anchor unused, returns baseline unchanged). Two anchor
    choices:
      - use_wide_anchor=False: profile["low"] itself - correct for
        equipment whose configured band is a substantial fraction of
        its own low bound (chiller/chilled water pump/water supply
        pump/AHU/cold room - unchanged from the original pass).
      - use_wide_anchor=True: profile["low"] - band, floored at 0 - the
        SAME floor engine._update_real()'s own trend clamp already
        uses elsewhere, needed for a narrow-band profile (air
        compressor Power_kW 22-30, air header Flow 60-100) where
        profile["low"] alone is too close to the baseline to express a
        real, "substantially lower" reduction.
    Always bounded between the anchor and the full baseline - never
    exceeds either, never produces a negative anchor when
    profile["low"] >= 0."""
    if use_wide_anchor:
        band = profile["high"] - profile["low"]
        anchor = max(0.0, profile["low"] - band) if band else profile["low"]
    else:
        anchor = profile["low"]
    return anchor + schedule_factor * (baseline - anchor)


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

    def __init__(
        self,
        rng: random.Random,
        efficiency_factor: float = 1.0,
        deterioration_eligible: bool = False,
    ) -> None:
        self._rng = rng
        self.phase = "dormant"
        self._remaining = 0
        self._span = 1
        self.just_entered_fault = False

        # Item 2 - stable, independently-seeded per-instance efficiency
        # jitter (set once at construction, see plant_context.efficiency_factor()).
        self.efficiency_factor = efficiency_factor

        # Items 3 & 9 - only a fixed seeded subset of instances ever
        # deteriorate; everything else stays "stable" forever. Eligible
        # instances start directly in "deteriorating" so predictive-
        # maintenance-style trends are actually visible in the live
        # simulator, not just theoretically possible.
        self.deterioration_eligible = deterioration_eligible
        self.deterioration_state = "deteriorating" if deterioration_eligible else "stable"
        self.deterioration_level = 0.0

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

    def force_start(self) -> bool:
        """
        Manually kick off the developing phase, same as the random
        trigger in advance() would - used by the temporary Simulator
        Control page to test AI diagnosis on demand instead of waiting
        for a random fault. No-op (returns False) if a fault is
        already in progress, so it can't be double-triggered mid-cycle.
        """
        if self.phase != "dormant":
            return False

        self.phase = "developing"
        self._span = self._rng.randint(*DEVELOPING_CYCLES)
        self._remaining = self._span
        return True

    def force_recover(self) -> bool:
        """Manually cut a fault short, skipping straight to recovering."""
        if self.phase == "dormant":
            return False

        self.phase = "recovering"
        self._span = self._rng.randint(*RECOVERING_CYCLES)
        self._remaining = self._span
        return True

    def advance_deterioration(self, tick_seconds: float) -> None:
        """
        Advances the wear accumulator for eligible instances currently
        in "deteriorating" state. tick_seconds is scaled by
        plant_context.DETERIORATION_TIME_ACCELERATION_FACTOR - a
        SIMULATION-ONLY multiplier that only affects how fast this
        internal 0.0-1.0 level advances, never real historian
        timestamps or any other tag's timing.
        """
        if not self.deterioration_eligible or self.deterioration_state != "deteriorating":
            return

        simulated_seconds = tick_seconds * plant_context.DETERIORATION_TIME_ACCELERATION_FACTOR
        self.deterioration_level = min(
            1.0,
            self.deterioration_level + simulated_seconds / plant_context.DETERIORATION_FULL_LIFE_SECONDS,
        )

    def force_recover_deterioration(self) -> bool:
        """
        Manually resets wear to zero and marks the instance "recovered" -
        mirrors force_start()/force_recover()'s existing manual-override
        pattern. Represents a maintenance intervention resetting the
        equipment's condition. No UI wires this yet (Phase 23's
        scenario-control page is the eventual caller) - just the hook.
        """
        if self.deterioration_level <= 0.0 and self.deterioration_state != "deteriorating":
            return False

        self.deterioration_state = "recovered"
        self.deterioration_level = 0.0
        return True


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

        # instance_key -> that instance's "running" BOOL tag name (e.g.
        # "...RunStatus"/"...CompressorStatus"), used by _update_int()
        # so a start counter increments on the instance's own actual
        # 0->1 transition instead of being decoupled from it - see the
        # comment on _update_int() for why this needed a real fix.
        self._running_tag_by_instance: dict[str, str] = {
            self._instance_key(tag["tag_name"]): tag["tag_name"]
            for tag in self._tags
            if tag["data_type"] == "BOOL" and tag["measurement"] == "running"
        }

        # Phase 5 - production_batches read once per update_values()
        # cycle (item 1: direct DB read, no snapshot layer), keyed by
        # the production-linked instance's tag-naming instance_key.
        self._production_state: dict[str, dict[str, Any]] = {}

        # Post-Phase-14 cleanup - shift_definitions read once per
        # update_values() cycle, same convention as _production_state
        # above. False until the first cycle runs (never guesses "in shift").
        self._within_shift: bool = False

        # Phase 5 - Main Incomer aggregation boundary (item 6),
        # precomputed once since self._tags never changes after
        # _load_tags(). plant ("p01"/"p02") -> its Main Incomer's own
        # Power_kW tag name, and the explicit list of other tags whose
        # current values sum into it.
        all_tag_names = [tag["tag_name"] for tag in self._tags]
        self._main_incomer_tag_by_plant: dict[str, str] = {
            plant_context.plant_of(plant_context.instance_key(tag_name)): tag_name
            for tag_name in all_tag_names
            if tag_name.endswith("ELEC.MAIN.Power_kW")
        }
        self._main_incomer_power_tags: set[str] = set(self._main_incomer_tag_by_plant.values())
        self._main_incomer_contributing_by_plant: dict[str, list[str]] = {
            plant: plant_context.main_incomer_contributing_tags(all_tag_names, plant)
            for plant in self._main_incomer_tag_by_plant
        }

        self._initialize_state()
        self._ensure_command_table()

    def _ensure_command_table(self) -> None:
        """
        Backs the temporary Simulator Control UI page - lets a separate
        process (Streamlit) hand this process (plc_logger) a "trigger
        this instance's fault now" command without any direct IPC.
        Self-provisioning rather than a full migrator script since this
        is explicitly a temporary/dev feature, easy to strip out later.
        """
        connection = sqlite3.connect(self.database_path)

        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS simulator_fault_commands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instance_key TEXT NOT NULL,
                    action TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    processed_at TEXT,
                    result TEXT
                )
                """
            )
            connection.commit()
        finally:
            connection.close()

    def _apply_pending_commands(self) -> None:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row

        try:
            pending = connection.execute(
                "SELECT id, instance_key, action FROM simulator_fault_commands "
                "WHERE processed_at IS NULL"
            ).fetchall()

            if not pending:
                return

            for row in pending:
                instance = self._instances.get(row["instance_key"])

                if instance is None:
                    result = "unknown instance"
                elif row["action"] == "trigger_fault":
                    result = "started" if instance.force_start() else f"skipped (already {instance.phase})"
                elif row["action"] == "force_recover":
                    result = "recovering" if instance.force_recover() else "skipped (already dormant)"
                else:
                    result = f"unknown action: {row['action']}"

                connection.execute(
                    "UPDATE simulator_fault_commands SET processed_at = ?, result = ? WHERE id = ?",
                    (datetime.utcnow().isoformat(timespec="seconds"), result, row["id"]),
                )

            connection.commit()
        finally:
            connection.close()

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
                  AND tag_name LIKE '%.%.%'
                ORDER BY tag_name
                """
            ).fetchall()
        finally:
            connection.close()

        return [dict(row) for row in rows]

    @staticmethod
    def _instance_key(tag_name: str) -> str:
        return instance_key(tag_name)

    @staticmethod
    def _profile(tag_name: str, unit: str) -> dict[str, float]:
        tag_profile = TAG_PROFILES.get(canonical_key(tag_name))

        if tag_profile is not None:
            return tag_profile

        return UNIT_PROFILES.get(unit, DEFAULT_PROFILE)

    def _initialize_state(self) -> None:
        for tag in self._tags:
            instance_key = self._instance_key(tag["tag_name"])

            if instance_key not in self._instances:
                seed = random.Random(instance_key).random()
                self._instances[instance_key] = _InstanceFaultState(
                    random.Random(int(seed * 1_000_000)),
                    efficiency_factor=plant_context.efficiency_factor(instance_key),
                    deterioration_eligible=plant_context.deterioration_eligible(instance_key),
                )

            self._values[tag["tag_name"]] = self._initial_value(tag)

    def _initial_value(self, tag: dict[str, Any]) -> Any:
        data_type = tag["data_type"]
        unit = tag["unit"]
        seed_rng = random.Random(tag["tag_name"])

        if data_type == "BOOL":
            return 0

        if data_type == "STRING":
            if tag["tag_name"].endswith(".AlarmCode"):
                return "None"
            return _generic_string_value(tag["tag_name"])

        if data_type == "INT":
            return 0

        # REAL
        if unit in MONOTONIC_UNITS:
            return round(seed_rng.uniform(0, 1000), 1)

        profile = self._profile(tag["tag_name"], unit)
        return round(seed_rng.uniform(profile["low"], profile["high"]), 2)

    def _update_real(
        self,
        tag: dict[str, Any],
        instance: _InstanceFaultState,
        previous_values: dict[str, Any],
    ) -> float:
        tag_name = tag["tag_name"]
        unit = tag["unit"]
        measurement = tag["measurement"]
        current = self._values[tag_name]
        severity = instance.severity()

        if unit in MONOTONIC_UNITS:
            if unit == "kWh":
                # Item 6 (related improvement) - integrate the now-
                # realistic paired Power_kW-style tag instead of a flat
                # random bump, so "today's kWh" actually means
                # something. Reads previous_values (last cycle's
                # already-committed figure) rather than this cycle's,
                # so ordering never matters regardless of which REAL
                # tag happens to be processed first in this pass.
                inst_key = plant_context.instance_key(tag_name)
                power_value = None
                for suffix in ("Power_kW", "CompressorPower", "FanPower", "BlowerPower", "MotorPower"):
                    power_value = previous_values.get(f"{inst_key}.{suffix}")
                    if power_value is not None:
                        break

                if power_value is not None:
                    increment = max(0.0, power_value) * (SIMULATED_TICK_SECONDS / 3600.0)
                    return round(current + increment, 4)

            increment = self._rng.uniform(0.001, 0.05)
            return round(current + increment, 3)

        profile = self._profile(tag_name, unit)
        canonical = canonical_key(tag_name)

        # Item 5 - outdoor ambient tracks a diurnal target instead of a
        # flat baseline; everything else keeps its configured band.
        if canonical == "ENV.Temperature":
            baseline = plant_context.ambient_target_temperature(datetime.now())
        elif canonical == "ENV.Humidity":
            baseline = plant_context.ambient_target_humidity(datetime.now())
        else:
            baseline = (profile["low"] + profile["high"]) / 2

            # Post-Phase-14 cleanup - continuous-duty utility equipment
            # (chiller/chilled water pump/water supply pump/AHU/cold
            # room/air compressor/air header) settles toward a
            # genuinely LOWER, never-zero baseline outside production
            # hours/weekends: a convex blend between a lower anchor and
            # the tag's full-load baseline, weighted by the schedule
            # factor - this shifts the REVERSION EQUILIBRIUM itself. An
            # earlier version applied this as a separate additive push
            # term instead (mirroring production_push's own shape) -
            # live testing caught it bottoming out at the trend clamp
            # floor (0 kW) regardless of the intended factor, since the
            # push magnitude could exceed the [low-band, high+band]
            # clamp entirely (see FACTORY_AI_DEVELOPMENT_STATUS.md).
            # Anchoring on profile["low"] itself works for equipment
            # whose TAG_PROFILES band is a substantial fraction of its
            # own low bound (chiller/pump/AHU/cold room); it does NOT
            # for a narrow-band profile (air compressor Power_kW 22-30,
            # air header Flow 60-100) - blending toward profile["low"]
            # alone there only ever produces a shallow ~15-20% dip, not
            # a "substantially lower" one. SCHEDULE_WIDE_ANCHOR_PREFIXES
            # opts those into the WIDER anchor (profile["low"] - band,
            # floored at 0 - the SAME floor the trend clamp below
            # already respects) instead - still bounded, never zero,
            # just genuinely lower. Caught during the follow-up
            # consistency review (air compressor Power_kW was barely
            # moving off-hours despite its RunStatus being mostly 0).
            if measurement in plant_context.PRODUCTION_LOAD_SENSITIVE_MEASUREMENTS:
                schedule_factor = plant_context.schedule_load_factor(canonical, datetime.now(), self._within_shift)
                if schedule_factor is not None and schedule_factor < 1.0:
                    use_wide_anchor = canonical.startswith(plant_context.SCHEDULE_WIDE_ANCHOR_PREFIXES)
                    baseline = _schedule_blended_baseline(profile, baseline, schedule_factor, use_wide_anchor)

            # Follow-up - air_header Flow/Pressure (the previously
            # disclosed gap): same mechanism, keyed by measurement type
            # instead of the power/current/speed set, since Flow/
            # Pressure need their OWN, DIFFERENT fractions under the
            # same equipment prefix (see
            # plant_context.air_header_schedule_factor()'s own
            # docstring). Deliberately a separate elif (not folded into
            # the block above) so PRODUCTION_LOAD_SENSITIVE_MEASUREMENTS
            # itself never needs to grow to cover "flow"/"pressure" -
            # zero risk of accidentally schedule-adjusting WATER.WSP.Flow/
            # UTILITY.CHWP.Flow, whose canonical prefixes already match
            # SCHEDULE_OFF_HOURS_LOAD_FACTOR entries meant for their OWN
            # Power_kW only.
            elif canonical.startswith(plant_context.AIR_HEADER_PREFIX) and measurement in plant_context.AIR_HEADER_SCHEDULE_SENSITIVE_MEASUREMENTS:
                schedule_factor = plant_context.air_header_schedule_factor(measurement, datetime.now(), self._within_shift)
                if schedule_factor is not None and schedule_factor < 1.0:
                    baseline = _schedule_blended_baseline(profile, baseline, schedule_factor, use_wide_anchor=True)

        band = profile["high"] - profile["low"]

        # Item 8 - different physical quantities settle at different
        # speeds; multiplies the configured revert rate rather than
        # replacing it.
        revert = profile["revert"] * plant_context.response_speed_multiplier(tag_name, measurement)
        noise = self._rng.uniform(-profile["noise"], profile["noise"])
        reversion = (baseline - current) * revert

        direction = -1 if measurement in FAULT_DIRECTION_DOWN else 1
        fault_push = direction * severity * band * 0.35

        # Item 5 - modest humidity-assisted, temperature-dominant HVAC
        # demand push on the AHU's fan power.
        environmental_push = 0.0
        if canonical == "HVAC.AHU.FanPower":
            plant = tag_name.split(".")[0]
            outdoor_temp = self._values.get(f"{plant}.ENV01.Temperature", plant_context.AMBIENT_BASE_TEMP_C)
            outdoor_humidity = self._values.get(f"{plant}.ENV01.Humidity", plant_context.AMBIENT_BASE_HUMIDITY_PCT)
            environmental_push = plant_context.hvac_demand_push_fraction(outdoor_temp, outdoor_humidity) * band * 0.3

        # Items 3 & 9 - only eligible, currently-deteriorating instances
        # get a slow upward drift on their genuine wear indicators.
        deterioration_push = 0.0
        if instance.deterioration_state == "deteriorating" and plant_context.is_wear_sensitive_tag(tag_name):
            deterioration_push = instance.deterioration_level * plant_context.DETERIORATION_MAX_DRIFT_FRACTION * band

        # Item 4 - a production-linked instance with no running batch
        # relaxes its power/current/speed toward an idle level instead
        # of sitting at full-load values while genuinely idle.
        production_push = 0.0
        if measurement in plant_context.PRODUCTION_LOAD_SENSITIVE_MEASUREMENTS:
            inst_key = plant_context.instance_key(tag_name)
            if plant_context.is_production_linked_instance(inst_key) and inst_key not in self._production_state:
                production_push = -(1.0 - plant_context.IDLE_LOAD_FACTOR) * (baseline - profile["low"])

        # A sustained fault otherwise keeps compounding fault_push cycle
        # after cycle with nothing to cap it, drifting to physically
        # implausible values (e.g. an air compressor discharge temp
        # past 200 degC) well before the ~10-30 cycle faulted phase
        # ends. Clamp the trend (not the final value) to a full
        # band-width beyond the profile's normal range - still a
        # clear, sustained excursion past any configured alarm limit,
        # just not an absurd one - then add noise afterwards, so a
        # "maxed out" plateau still jitters like a real sensor instead
        # of reading bit-for-bit identical every cycle.
        trend = current + reversion + fault_push + environmental_push + deterioration_push + production_push
        lower_bound = profile["low"] - band
        if profile["low"] >= 0:
            lower_bound = max(0.0, lower_bound)
        upper_bound = profile["high"] + band
        trend = min(max(trend, lower_bound), upper_bound)

        value = trend + noise

        # Item 2 - stable per-instance efficiency jitter on the specific
        # signals it plausibly affects (never applied to Main Incomer's
        # own Power_kW, which is computed separately - see
        # _update_main_incomer_power()).
        if measurement in {"power", "current"}:
            value *= instance.efficiency_factor

        # A clamped trend sitting right at 0 (e.g. an idle production
        # instance - see production_push above, or a fault_push-driven
        # excursion) can still be nudged slightly negative by noise.
        # Power/current draw can never be genuinely negative, so floor
        # it by tag-name suffix rather than the tags.measurement
        # classification alone - at least one tag in the live dataset
        # (WT.RO01.Power_kW) is mis-classified as measurement=
        # "pressure" rather than "power" (a pre-existing metadata
        # inference quirk, not touched here), which would otherwise
        # slip past a measurement-only guard.
        if tag_name.endswith(("Power_kW", "MotorPower", "FanPower", "CompressorPower", "BlowerPower", "Current")):
            value = max(0.0, value)

        return round(value, 3)

    def _update_bool(
        self,
        tag: dict[str, Any],
        severity: float,
    ) -> int:
        is_alarm_style = tag["event_type"] in {"alarm", "warning"}

        if is_alarm_style:
            return 1 if severity >= 0.6 else 0

        tag_name = tag["tag_name"]
        current = self._values[tag_name]

        if tag["measurement"] == "running":
            # Mostly running; drops out while faulted/recovering.
            if severity >= 0.6:
                return 0

            # Item 4 - a production-linked instance's RunStatus now
            # reflects real production_batches state (Phase 4's
            # authoritative source) instead of independent random noise.
            inst_key = plant_context.instance_key(tag_name)
            if plant_context.is_production_linked_instance(inst_key):
                return 1 if inst_key in self._production_state else 0

            # Post-Phase-14 cleanup - air compressors genuinely stop
            # most of the time outside production hours (compressed-air
            # demand is almost entirely production-driven), rather than
            # staying in the generic ~99.8%-always-on branch below.
            # Chiller/pump/AHU/cold-room RunStatus is deliberately
            # UNCHANGED (falls through to the always-on branch) - those
            # are genuinely continuous-duty systems; only their load
            # level shifts (see schedule_push in _update_real()).
            canonical = canonical_key(tag_name)
            if plant_context.is_schedule_stoppable_canonical(canonical):
                on_probability = plant_context.schedule_stoppable_on_probability(datetime.now(), self._within_shift)
                return 1 if self._rng.random() < on_probability else 0

            return 1 if self._rng.random() > 0.002 else 0

        # Generic status bit (door/defrost/etc.) - rare toggle.
        if self._rng.random() < 0.003:
            return 0 if current else 1

        return current

    def _update_int(
        self,
        tag: dict[str, Any],
        instance: _InstanceFaultState,
        previous_values: dict[str, Any],
    ) -> int:
        tag_name = tag["tag_name"]
        current = self._values[tag_name]

        # Item 4 - Filling Machine's GoodCount/RejectCount are the one
        # case where a discrete item count genuinely makes sense
        # (containers filled), so they're derived from the real running
        # batch's good_quantity/reject_quantity (litres/kg) divided by
        # its own current ContainerSize reading - not from an
        # independent increment. Holds its last value while idle
        # (no running batch) rather than resetting to 0, so Live Data
        # doesn't flicker between batches. Other production equipment
        # (Mill/Disperser/Mixer) has no such tag at all - discrete
        # counts don't make engineering sense for them, per explicit
        # scope confirmation.
        if tag_name.endswith((".GoodCount", ".RejectCount")):
            inst_key = plant_context.instance_key(tag_name)
            state = self._production_state.get(inst_key)

            if state is not None:
                container_size = self._values.get(f"{inst_key}.ContainerSize") or 1.0
                if container_size <= 0:
                    container_size = 1.0
                quantity = state["good_quantity"] if tag_name.endswith(".GoodCount") else state["reject_quantity"]
                return int(quantity / container_size)

            return current

        # Start counters ("...StartCount"/"...Starts") should track the
        # instance's own running-status BOOL, not be decoupled from it -
        # previously this incremented on fault-onset or independent
        # random noise, so a "run count" bore no actual relationship to
        # how many times the equipment had really started. Now driven
        # by a genuine 0->1 transition of the paired running tag this
        # same cycle (BOOL tags are updated in the pass before this one
        # - see update_values() - so both the before and after values
        # are available here regardless of tag-name sort order).
        if tag_name.endswith(("StartCount", "Starts")):
            running_tag = self._running_tag_by_instance.get(
                self._instance_key(tag_name)
            )

            if running_tag is not None:
                was_running = bool(previous_values.get(running_tag, 0))
                is_running = bool(self._values.get(running_tag, 0))

                return current + 1 if (not was_running and is_running) else current

        # Every other INT tag (production GoodCount/RejectCount, or a
        # start-style counter with no paired running tag) keeps the
        # original fault-linked/independent-random-noise behavior.
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

    def _update_main_incomer_power(self) -> None:
        """
        Item 6 - Main Incomer Power_kW is NOT an independently-noisy
        signal like every other REAL tag; it's a pure aggregation of
        this cycle's already-finalized contributing end-load tags (see
        plant_context.main_incomer_contributing_tags(), the explicit
        include/exclude boundary) plus one unmetered base-load term.
        Called after the REAL/BOOL pass so every contributor already
        holds its final value for this cycle.
        """
        now = datetime.now()

        for plant, main_tag in self._main_incomer_tag_by_plant.items():
            contributing = self._main_incomer_contributing_by_plant.get(plant, [])
            end_load_sum = sum(self._values.get(t, 0.0) for t in contributing)
            base_load = plant_context.plant_base_load_kw(plant, now, self._rng)
            self._values[main_tag] = round(end_load_sum + base_load, 2)

    def update_values(self) -> None:
        self._apply_pending_commands()

        # Item 1 - one direct DB read per cycle, no snapshot layer.
        # Degrades to {} (no production context) if production_batches
        # doesn't exist yet on this database - see
        # plant_context.get_production_state().
        self._production_state = plant_context.get_production_state(self.database_path)

        # Post-Phase-14 cleanup - one direct read of shift_definitions
        # per cycle (same "no snapshot layer" convention as the
        # production-state read directly above), cached for this
        # cycle's REAL/BOOL passes to consult without a DB round-trip
        # per tag. Degrades to False (never guesses "in shift") if no
        # shift is configured - see plant_context.is_within_active_shift().
        self._within_shift = plant_context.is_within_active_shift(self.database_path, datetime.now())

        for instance in self._instances.values():
            instance.advance()
            instance.advance_deterioration(SIMULATED_TICK_SECONDS)

        # Snapshotted before any of this cycle's updates, so
        # _update_int() can compare a running-status BOOL's value from
        # before this cycle against its value after (set in the first
        # pass below) to detect a genuine 0->1 transition, and so
        # _update_real()'s Energy_kWh integration always reads a stable
        # "last cycle" Power_kW regardless of tag processing order.
        previous_values = dict(self._values)

        # Three passes: REAL/BOOL tags are finalized for this cycle
        # first (except each plant's Main Incomer Power_kW, held back -
        # see below), so the second pass's INT tags (start counters)
        # can safely read a sibling running-status BOOL's before/after
        # values, and so the third pass can aggregate this cycle's
        # already-finalized contributing loads into Main Incomer
        # Power_kW before anything downstream (its own Energy_kWh, next
        # cycle) needs it.
        for tag in self._tags:
            tag_name = tag["tag_name"]
            instance = self._instances[self._instance_key(tag_name)]
            data_type = tag["data_type"]

            if data_type == "REAL":
                if tag_name in self._main_incomer_power_tags:
                    continue
                self._values[tag_name] = self._update_real(tag, instance, previous_values)
            elif data_type == "BOOL":
                self._values[tag_name] = self._update_bool(tag, instance.severity())

        self._update_main_incomer_power()

        for tag in self._tags:
            tag_name = tag["tag_name"]
            instance = self._instances[self._instance_key(tag_name)]
            severity = instance.severity()
            data_type = tag["data_type"]

            if data_type == "INT":
                self._values[tag_name] = self._update_int(
                    tag, instance, previous_values
                )
            elif data_type == "STRING" and tag_name.endswith(".AlarmCode"):
                self._values[tag_name] = self._update_string(severity)
            elif data_type == "STRING" and tag_name.endswith((".BatchNumber", ".ProductCode")):
                # Item 4 - production-linked instances reflect the real
                # running batch's code/product instead of a value fixed
                # forever at simulator startup. Non-production STRING
                # tags (MaterialID/SourceTank/...) are untouched below.
                inst_key = self._instance_key(tag_name)
                if plant_context.is_production_linked_instance(inst_key):
                    state = self._production_state.get(inst_key)
                    if tag_name.endswith(".BatchNumber"):
                        self._values[tag_name] = state["batch_code"] if state else "IDLE"
                    else:
                        self._values[tag_name] = state["product_code"] if state else "IDLE"
            # Other STRING tags (MaterialID/SourceTank/...) keep their
            # _initial_value() forever - informational, not fault- or
            # production-linked, so there's nothing to update each cycle.

    def get_tags(self) -> list[tuple[str, str, Any]]:
        return [
            (tag["tag_name"], tag["tag_name"], self._values[tag["tag_name"]])
            for tag in self._tags
        ]
