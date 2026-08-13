from __future__ import annotations
from config.environment import get_config_db_path

import random
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


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


def canonical_key(tag_name: str) -> str:
    """
    Strip the plant prefix (P01/P02) and instance numbers from a tag
    name, keeping the equipment-type code and signal name - e.g.
    "P01.UTILITY.AC01.OutletTemp" -> "UTILITY.AC.OutletTemp". This
    lets every instance of the same equipment type (AC01/AC02/AC03,
    or the same equipment once P02 is phased in) share one realistic
    profile instead of needing one entry per instance. The final
    token (the signal name, e.g. "Current_L1") is left untouched so
    per-phase signals like L1/L2/L3 stay distinct.
    """
    tokens = tag_name.split(".")[1:]

    if not tokens:
        return tag_name

    stripped = [
        re.sub(r"\d+$", "", token) if index < len(tokens) - 1 else token
        for index, token in enumerate(tokens)
    ]

    return ".".join(stripped)


def instance_key(tag_name: str) -> str:
    """
    The specific physical unit a tag belongs to - e.g.
    "P01.UTILITY.AC01.OutletTemp" -> "P01.UTILITY.AC01". Unlike
    canonical_key(), instance numbers are kept (AC01 stays AC01, not
    AC) since each physical unit has its own independent fault state.
    Exposed at module level (not just as TagDatasetSimulator's
    internal staticmethod) so the Simulator Control UI page can
    compute the same key to target a fault command correctly.
    """
    parts = tag_name.split(".")
    return ".".join(parts[:-1]) if len(parts) >= 2 else tag_name


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
        severity: float,
    ) -> float:
        unit = tag["unit"]
        current = self._values[tag["tag_name"]]

        if unit in MONOTONIC_UNITS:
            increment = self._rng.uniform(0.001, 0.05)
            return round(current + increment, 3)

        profile = self._profile(tag["tag_name"], unit)
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
        trend = current + reversion + fault_push
        lower_bound = profile["low"] - band
        if profile["low"] >= 0:
            lower_bound = max(0.0, lower_bound)
        upper_bound = profile["high"] + band
        trend = min(max(trend, lower_bound), upper_bound)

        return round(trend + noise, 3)

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
        previous_values: dict[str, Any],
    ) -> int:
        tag_name = tag["tag_name"]
        current = self._values[tag_name]

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

    def update_values(self) -> None:
        self._apply_pending_commands()

        for instance in self._instances.values():
            instance.advance()

        # Snapshotted before any of this cycle's updates, so
        # _update_int() can compare a running-status BOOL's value from
        # before this cycle against its value after (set in the first
        # pass below) to detect a genuine 0->1 transition.
        previous_values = dict(self._values)

        # Two passes: REAL/BOOL tags are finalized for this cycle
        # first, so the second pass's INT tags (specifically start
        # counters, see _update_int()) can safely read a sibling
        # running-status BOOL's before/after values regardless of
        # which tag name happens to sort first alphabetically.
        for tag in self._tags:
            tag_name = tag["tag_name"]
            instance = self._instances[self._instance_key(tag_name)]
            severity = instance.severity()
            data_type = tag["data_type"]

            if data_type == "REAL":
                self._values[tag_name] = self._update_real(tag, severity)
            elif data_type == "BOOL":
                self._values[tag_name] = self._update_bool(tag, severity)

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
            # Other STRING tags (BatchNumber/ProductCode/MaterialID/...)
            # keep their _initial_value() forever - informational, not
            # fault-linked, so there's nothing to update each cycle.

    def get_tags(self) -> list[tuple[str, str, Any]]:
        return [
            (tag["tag_name"], tag["tag_name"], self._values[tag["tag_name"]])
            for tag in self._tags
        ]
