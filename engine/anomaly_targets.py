from __future__ import annotations

from dataclasses import dataclass

"""
Phase 9 - Anomaly Detection Engine rule registry. Curated, not
automatic (mirrors engine/baseline_targets.py's discipline exactly -
every rule maps to an EXISTING Phase 8 baseline target; Phase 9
introduces no new raw measurements, no new instrumentation).

threshold_provenance is mandatory on every rule (item 8 of the Phase 9
approval): for this dev-stage simulation, every rule below is
SIMULATION_TUNING - a reasonable starting point chosen by engineering
judgment, NOT derived from manufacturer data or a real commissioned
plant. This must never be silently presented as a real engineering
limit or manufacturer reference later - the field exists specifically
so that distinction stays visible in evidence.
"""

THRESHOLD_PROVENANCE_VALUES = (
    "USER_CONFIGURED", "ENGINEERING_RULE", "MANUFACTURER_REFERENCE", "STATISTICAL", "SIMULATION_TUNING",
)

ANOMALY_TYPE_DEVIATION = "deviation"
ANOMALY_TYPE_DRIFT = "drift"

CATEGORY_ENERGY = "ENERGY"
CATEGORY_CONDITION = "CONDITION"
CATEGORY_PROCESS = "PROCESS"
CATEGORY_UTILITY = "UTILITY"
CATEGORY_PRODUCTION = "PRODUCTION"


@dataclass(frozen=True)
class AnomalyRule:
    rule_key: str
    baseline_target_key: str            # matches a Phase 8 BaselineTarget.target_key
    equipment_type: str                 # matches Phase 8's equipment_type
    anomaly_type: str                   # ANOMALY_TYPE_DEVIATION | ANOMALY_TYPE_DRIFT
    category: str
    direction: str                      # "high" | "low" | "both"
    title_template: str                 # "{instance}" is substituted at evaluation time
    deviation_threshold_pct: float | None
    deviation_threshold_mad: float
    minimum_absolute_deviation: float
    minimum_absolute_deviation_unit: str
    persistence_periods_open: int
    persistence_periods_resolve: int
    resolve_band_margin_pct: float
    applicable_operating_states: tuple[str, ...]   # () = no gating (always applicable)
    operating_state_tag_suffix: str | None
    energy_relevant: bool                          # item 18 - gates estimated-excess-cost
    threshold_provenance: str
    bootstrap_multiplier: float = 1.75
    drift_threshold_pct: float | None = None
    drift_persistence_evaluations: int = 2


def _rule(**kwargs) -> AnomalyRule:
    kwargs.setdefault("threshold_provenance", "SIMULATION_TUNING")
    assert kwargs["threshold_provenance"] in THRESHOLD_PROVENANCE_VALUES
    return AnomalyRule(**kwargs)


ANOMALY_RULES: list[AnomalyRule] = [
    # --- Plant Energy ---------------------------------------------------
    _rule(
        rule_key="plant_high_demand", baseline_target_key="Power_kW", equipment_type="plant_energy",
        anomaly_type=ANOMALY_TYPE_DEVIATION, category=CATEGORY_ENERGY, direction="high",
        title_template="{instance} High Plant Demand", deviation_threshold_pct=15.0, deviation_threshold_mad=2.5,
        minimum_absolute_deviation=10.0, minimum_absolute_deviation_unit="kW",
        persistence_periods_open=3, persistence_periods_resolve=3, resolve_band_margin_pct=5.0,
        applicable_operating_states=(), operating_state_tag_suffix=None, energy_relevant=True,
    ),
    _rule(
        rule_key="plant_elevated_non_production_demand", baseline_target_key="non_production_demand_kw",
        equipment_type="plant_energy", anomaly_type=ANOMALY_TYPE_DEVIATION, category=CATEGORY_ENERGY, direction="high",
        title_template="{instance} Elevated Non-production Demand", deviation_threshold_pct=15.0, deviation_threshold_mad=2.5,
        minimum_absolute_deviation=5.0, minimum_absolute_deviation_unit="kW",
        persistence_periods_open=4, persistence_periods_resolve=4, resolve_band_margin_pct=5.0,
        applicable_operating_states=(), operating_state_tag_suffix=None, energy_relevant=True,
    ),

    # --- Air Compressors --------------------------------------------------
    _rule(
        rule_key="compressor_power_deviation", baseline_target_key="Power_kW", equipment_type="air_compressor",
        anomaly_type=ANOMALY_TYPE_DEVIATION, category=CATEGORY_UTILITY, direction="high",
        title_template="{instance} Compressor Power Deviation", deviation_threshold_pct=15.0, deviation_threshold_mad=2.5,
        minimum_absolute_deviation=3.0, minimum_absolute_deviation_unit="kW",
        persistence_periods_open=3, persistence_periods_resolve=3, resolve_band_margin_pct=5.0,
        applicable_operating_states=("running",), operating_state_tag_suffix="RunStatus", energy_relevant=True,
    ),

    # --- Compressed-air header ---------------------------------------------
    _rule(
        rule_key="header_flow_high", baseline_target_key="Flow", equipment_type="air_header",
        anomaly_type=ANOMALY_TYPE_DEVIATION, category=CATEGORY_UTILITY, direction="high",
        title_template="{instance} Header Flow Abnormally High", deviation_threshold_pct=15.0, deviation_threshold_mad=2.5,
        minimum_absolute_deviation=5.0, minimum_absolute_deviation_unit="Nm3/h",
        persistence_periods_open=3, persistence_periods_resolve=3, resolve_band_margin_pct=5.0,
        applicable_operating_states=(), operating_state_tag_suffix=None, energy_relevant=False,
    ),
    _rule(
        rule_key="header_pressure_deviation", baseline_target_key="Pressure", equipment_type="air_header",
        anomaly_type=ANOMALY_TYPE_DEVIATION, category=CATEGORY_UTILITY, direction="both",
        title_template="{instance} Header Pressure Abnormal", deviation_threshold_pct=8.0, deviation_threshold_mad=2.5,
        minimum_absolute_deviation=0.3, minimum_absolute_deviation_unit="bar",
        persistence_periods_open=3, persistence_periods_resolve=3, resolve_band_margin_pct=5.0,
        applicable_operating_states=(), operating_state_tag_suffix=None, energy_relevant=False,
    ),
    _rule(
        rule_key="compressed_air_specific_energy_drift", baseline_target_key="compressed_air_specific_energy",
        equipment_type="air_header", anomaly_type=ANOMALY_TYPE_DRIFT, category=CATEGORY_UTILITY, direction="high",
        title_template="{instance} Compressed-air Specific Energy Deteriorating", deviation_threshold_pct=None,
        deviation_threshold_mad=0.0, minimum_absolute_deviation=0.0, minimum_absolute_deviation_unit="kWh/Nm3",
        persistence_periods_open=0, persistence_periods_resolve=0, resolve_band_margin_pct=0.0,
        applicable_operating_states=(), operating_state_tag_suffix=None, energy_relevant=True,
        drift_threshold_pct=10.0, drift_persistence_evaluations=2,
    ),

    # --- Chillers -----------------------------------------------------------
    _rule(
        rule_key="chiller_power_deviation", baseline_target_key="Power_kW", equipment_type="chiller",
        anomaly_type=ANOMALY_TYPE_DEVIATION, category=CATEGORY_UTILITY, direction="high",
        title_template="{instance} Power Above Expected", deviation_threshold_pct=15.0, deviation_threshold_mad=2.5,
        minimum_absolute_deviation=5.0, minimum_absolute_deviation_unit="kW",
        persistence_periods_open=3, persistence_periods_resolve=3, resolve_band_margin_pct=5.0,
        applicable_operating_states=("running",), operating_state_tag_suffix="RunStatus", energy_relevant=True,
    ),
    _rule(
        rule_key="chiller_cop_low", baseline_target_key="cop", equipment_type="chiller",
        anomaly_type=ANOMALY_TYPE_DEVIATION, category=CATEGORY_CONDITION, direction="low",
        title_template="{instance} COP Below Expected", deviation_threshold_pct=15.0, deviation_threshold_mad=2.5,
        minimum_absolute_deviation=0.5, minimum_absolute_deviation_unit="dimensionless",
        persistence_periods_open=4, persistence_periods_resolve=4, resolve_band_margin_pct=5.0,
        applicable_operating_states=("running",), operating_state_tag_suffix="RunStatus", energy_relevant=False,
    ),
    _rule(
        rule_key="chiller_cooling_output_deviation", baseline_target_key="cooling_output_kw", equipment_type="chiller",
        anomaly_type=ANOMALY_TYPE_DEVIATION, category=CATEGORY_PROCESS, direction="both",
        title_template="{instance} Cooling Output Abnormal", deviation_threshold_pct=15.0, deviation_threshold_mad=2.5,
        minimum_absolute_deviation=15.0, minimum_absolute_deviation_unit="kW",
        persistence_periods_open=4, persistence_periods_resolve=4, resolve_band_margin_pct=5.0,
        applicable_operating_states=("running",), operating_state_tag_suffix="RunStatus", energy_relevant=False,
    ),
    _rule(
        rule_key="chiller_supply_temp_deviation", baseline_target_key="SupplyTemp", equipment_type="chiller",
        anomaly_type=ANOMALY_TYPE_DEVIATION, category=CATEGORY_PROCESS, direction="both",
        title_template="{instance} Supply Temperature Abnormal", deviation_threshold_pct=None, deviation_threshold_mad=2.5,
        minimum_absolute_deviation=0.8, minimum_absolute_deviation_unit="degC",
        persistence_periods_open=4, persistence_periods_resolve=4, resolve_band_margin_pct=5.0,
        applicable_operating_states=("running",), operating_state_tag_suffix="RunStatus", energy_relevant=False,
    ),
    _rule(
        rule_key="chiller_return_temp_deviation", baseline_target_key="ReturnTemp", equipment_type="chiller",
        anomaly_type=ANOMALY_TYPE_DEVIATION, category=CATEGORY_PROCESS, direction="both",
        title_template="{instance} Return Temperature Abnormal", deviation_threshold_pct=None, deviation_threshold_mad=2.5,
        minimum_absolute_deviation=0.8, minimum_absolute_deviation_unit="degC",
        persistence_periods_open=4, persistence_periods_resolve=4, resolve_band_margin_pct=5.0,
        applicable_operating_states=("running",), operating_state_tag_suffix="RunStatus", energy_relevant=False,
    ),
    _rule(
        rule_key="chiller_waterflow_deviation", baseline_target_key="WaterFlow", equipment_type="chiller",
        anomaly_type=ANOMALY_TYPE_DEVIATION, category=CATEGORY_PROCESS, direction="both",
        title_template="{instance} Water Flow Deviation", deviation_threshold_pct=15.0, deviation_threshold_mad=2.5,
        minimum_absolute_deviation=4.0, minimum_absolute_deviation_unit="m3/h",
        persistence_periods_open=4, persistence_periods_resolve=4, resolve_band_margin_pct=5.0,
        applicable_operating_states=("running",), operating_state_tag_suffix="RunStatus", energy_relevant=False,
    ),

    # --- Pumps (CHWP + WSP share the same rule shape) -----------------------
    _rule(
        rule_key="pump_power_deviation", baseline_target_key="Power_kW", equipment_type="chilled_water_pump",
        anomaly_type=ANOMALY_TYPE_DEVIATION, category=CATEGORY_UTILITY, direction="high",
        title_template="{instance} Power Deviation", deviation_threshold_pct=15.0, deviation_threshold_mad=2.5,
        minimum_absolute_deviation=1.0, minimum_absolute_deviation_unit="kW",
        persistence_periods_open=3, persistence_periods_resolve=3, resolve_band_margin_pct=5.0,
        applicable_operating_states=("running",), operating_state_tag_suffix="RunStatus", energy_relevant=True,
    ),
    _rule(
        rule_key="pump_flow_deviation", baseline_target_key="Flow", equipment_type="chilled_water_pump",
        anomaly_type=ANOMALY_TYPE_DEVIATION, category=CATEGORY_PROCESS, direction="both",
        title_template="{instance} Flow Deviation", deviation_threshold_pct=15.0, deviation_threshold_mad=2.5,
        minimum_absolute_deviation=3.0, minimum_absolute_deviation_unit="m3/h",
        persistence_periods_open=3, persistence_periods_resolve=3, resolve_band_margin_pct=5.0,
        applicable_operating_states=("running",), operating_state_tag_suffix="RunStatus", energy_relevant=False,
    ),
    _rule(
        rule_key="pump_delta_p_deviation", baseline_target_key="delta_p_bar", equipment_type="chilled_water_pump",
        anomaly_type=ANOMALY_TYPE_DEVIATION, category=CATEGORY_PROCESS, direction="both",
        title_template="{instance} Differential Pressure Deviation", deviation_threshold_pct=None, deviation_threshold_mad=2.5,
        minimum_absolute_deviation=0.15, minimum_absolute_deviation_unit="bar",
        persistence_periods_open=4, persistence_periods_resolve=4, resolve_band_margin_pct=5.0,
        applicable_operating_states=("running",), operating_state_tag_suffix="RunStatus", energy_relevant=False,
    ),
    _rule(
        rule_key="pump_vibration_increase", baseline_target_key="Vibration", equipment_type="chilled_water_pump",
        anomaly_type=ANOMALY_TYPE_DEVIATION, category=CATEGORY_CONDITION, direction="high",
        title_template="{instance} Vibration Increase", deviation_threshold_pct=20.0, deviation_threshold_mad=3.0,
        minimum_absolute_deviation=0.3, minimum_absolute_deviation_unit="mm/s",
        persistence_periods_open=4, persistence_periods_resolve=4, resolve_band_margin_pct=8.0,
        applicable_operating_states=("running",), operating_state_tag_suffix="RunStatus", energy_relevant=False,
    ),
    _rule(
        rule_key="pump_bearing_temp_increase", baseline_target_key="BearingTemp", equipment_type="chilled_water_pump",
        anomaly_type=ANOMALY_TYPE_DEVIATION, category=CATEGORY_CONDITION, direction="high",
        title_template="{instance} Bearing Temperature Increase", deviation_threshold_pct=15.0, deviation_threshold_mad=2.5,
        minimum_absolute_deviation=3.0, minimum_absolute_deviation_unit="degC",
        persistence_periods_open=4, persistence_periods_resolve=4, resolve_band_margin_pct=5.0,
        applicable_operating_states=("running",), operating_state_tag_suffix="RunStatus", energy_relevant=False,
    ),
    _rule(
        rule_key="pump_flow_per_kw_drift", baseline_target_key="flow_per_kw", equipment_type="chilled_water_pump",
        anomaly_type=ANOMALY_TYPE_DRIFT, category=CATEGORY_CONDITION, direction="low",
        title_template="{instance} Flow-per-kW Deteriorating", deviation_threshold_pct=None, deviation_threshold_mad=0.0,
        minimum_absolute_deviation=0.0, minimum_absolute_deviation_unit="m3/h per kW",
        persistence_periods_open=0, persistence_periods_resolve=0, resolve_band_margin_pct=0.0,
        applicable_operating_states=(), operating_state_tag_suffix=None, energy_relevant=False,
        drift_threshold_pct=10.0, drift_persistence_evaluations=2,
    ),

    # --- Water Supply Pumps (same rule shape as Chilled Water Pumps) --------
    _rule(
        rule_key="wsp_power_deviation", baseline_target_key="Power_kW", equipment_type="water_supply_pump",
        anomaly_type=ANOMALY_TYPE_DEVIATION, category=CATEGORY_UTILITY, direction="high",
        title_template="{instance} Power Deviation", deviation_threshold_pct=15.0, deviation_threshold_mad=2.5,
        minimum_absolute_deviation=1.0, minimum_absolute_deviation_unit="kW",
        persistence_periods_open=3, persistence_periods_resolve=3, resolve_band_margin_pct=5.0,
        applicable_operating_states=("running",), operating_state_tag_suffix="RunStatus", energy_relevant=True,
    ),
    _rule(
        rule_key="wsp_flow_deviation", baseline_target_key="Flow", equipment_type="water_supply_pump",
        anomaly_type=ANOMALY_TYPE_DEVIATION, category=CATEGORY_PROCESS, direction="both",
        title_template="{instance} Flow Deviation", deviation_threshold_pct=15.0, deviation_threshold_mad=2.5,
        minimum_absolute_deviation=3.0, minimum_absolute_deviation_unit="m3/h",
        persistence_periods_open=3, persistence_periods_resolve=3, resolve_band_margin_pct=5.0,
        applicable_operating_states=("running",), operating_state_tag_suffix="RunStatus", energy_relevant=False,
    ),
    _rule(
        rule_key="wsp_delta_p_deviation", baseline_target_key="delta_p_bar", equipment_type="water_supply_pump",
        anomaly_type=ANOMALY_TYPE_DEVIATION, category=CATEGORY_PROCESS, direction="both",
        title_template="{instance} Differential Pressure Deviation", deviation_threshold_pct=None, deviation_threshold_mad=2.5,
        minimum_absolute_deviation=0.15, minimum_absolute_deviation_unit="bar",
        persistence_periods_open=4, persistence_periods_resolve=4, resolve_band_margin_pct=5.0,
        applicable_operating_states=("running",), operating_state_tag_suffix="RunStatus", energy_relevant=False,
    ),
    _rule(
        rule_key="wsp_vibration_increase", baseline_target_key="Vibration", equipment_type="water_supply_pump",
        anomaly_type=ANOMALY_TYPE_DEVIATION, category=CATEGORY_CONDITION, direction="high",
        title_template="{instance} Vibration Increase", deviation_threshold_pct=20.0, deviation_threshold_mad=3.0,
        minimum_absolute_deviation=0.3, minimum_absolute_deviation_unit="mm/s",
        persistence_periods_open=4, persistence_periods_resolve=4, resolve_band_margin_pct=8.0,
        applicable_operating_states=("running",), operating_state_tag_suffix="RunStatus", energy_relevant=False,
    ),
    _rule(
        rule_key="wsp_bearing_temp_increase", baseline_target_key="BearingTemp", equipment_type="water_supply_pump",
        anomaly_type=ANOMALY_TYPE_DEVIATION, category=CATEGORY_CONDITION, direction="high",
        title_template="{instance} Bearing Temperature Increase", deviation_threshold_pct=15.0, deviation_threshold_mad=2.5,
        minimum_absolute_deviation=3.0, minimum_absolute_deviation_unit="degC",
        persistence_periods_open=4, persistence_periods_resolve=4, resolve_band_margin_pct=5.0,
        applicable_operating_states=("running",), operating_state_tag_suffix="RunStatus", energy_relevant=False,
    ),
    _rule(
        rule_key="wsp_flow_per_kw_drift", baseline_target_key="flow_per_kw", equipment_type="water_supply_pump",
        anomaly_type=ANOMALY_TYPE_DRIFT, category=CATEGORY_CONDITION, direction="low",
        title_template="{instance} Flow-per-kW Deteriorating", deviation_threshold_pct=None, deviation_threshold_mad=0.0,
        minimum_absolute_deviation=0.0, minimum_absolute_deviation_unit="m3/h per kW",
        persistence_periods_open=0, persistence_periods_resolve=0, resolve_band_margin_pct=0.0,
        applicable_operating_states=(), operating_state_tag_suffix=None, energy_relevant=False,
        drift_threshold_pct=10.0, drift_persistence_evaluations=2,
    ),

    # --- AHU -----------------------------------------------------------------
    _rule(
        rule_key="ahu_filter_dp_drift", baseline_target_key="FilterDP", equipment_type="ahu",
        anomaly_type=ANOMALY_TYPE_DRIFT, category=CATEGORY_CONDITION, direction="high",
        title_template="{instance} Filter Differential Pressure Increasing", deviation_threshold_pct=None,
        deviation_threshold_mad=0.0, minimum_absolute_deviation=0.0, minimum_absolute_deviation_unit="Pa",
        persistence_periods_open=0, persistence_periods_resolve=0, resolve_band_margin_pct=0.0,
        applicable_operating_states=(), operating_state_tag_suffix=None, energy_relevant=False,
        drift_threshold_pct=15.0, drift_persistence_evaluations=2,
    ),
    _rule(
        rule_key="ahu_fan_power_deviation", baseline_target_key="FanPower", equipment_type="ahu",
        anomaly_type=ANOMALY_TYPE_DEVIATION, category=CATEGORY_UTILITY, direction="high",
        title_template="{instance} Fan Power Deviation", deviation_threshold_pct=15.0, deviation_threshold_mad=2.5,
        minimum_absolute_deviation=1.0, minimum_absolute_deviation_unit="kW",
        persistence_periods_open=3, persistence_periods_resolve=3, resolve_band_margin_pct=5.0,
        applicable_operating_states=("running",), operating_state_tag_suffix="RunStatus", energy_relevant=True,
    ),
    _rule(
        rule_key="ahu_supply_air_temp_deviation", baseline_target_key="SupplyAirTemp", equipment_type="ahu",
        anomaly_type=ANOMALY_TYPE_DEVIATION, category=CATEGORY_PROCESS, direction="both",
        title_template="{instance} Supply Air Temperature Deviation", deviation_threshold_pct=None, deviation_threshold_mad=2.5,
        minimum_absolute_deviation=1.0, minimum_absolute_deviation_unit="degC",
        persistence_periods_open=4, persistence_periods_resolve=4, resolve_band_margin_pct=5.0,
        applicable_operating_states=("running",), operating_state_tag_suffix="RunStatus", energy_relevant=False,
    ),

    # --- Cold Rooms ------------------------------------------------------------
    _rule(
        rule_key="coldroom_room_temp_abnormal", baseline_target_key="RoomTemp", equipment_type="cold_room",
        anomaly_type=ANOMALY_TYPE_DEVIATION, category=CATEGORY_PROCESS, direction="both",
        title_template="{instance} Room Temperature Outside Learned Range", deviation_threshold_pct=None,
        deviation_threshold_mad=2.5, minimum_absolute_deviation=0.7, minimum_absolute_deviation_unit="degC",
        persistence_periods_open=3, persistence_periods_resolve=3, resolve_band_margin_pct=5.0,
        applicable_operating_states=(), operating_state_tag_suffix=None, energy_relevant=False,
    ),
    _rule(
        rule_key="coldroom_compressor_power_deviation", baseline_target_key="CompressorPower", equipment_type="cold_room",
        anomaly_type=ANOMALY_TYPE_DEVIATION, category=CATEGORY_UTILITY, direction="high",
        title_template="{instance} Compressor Duty Above Expected", deviation_threshold_pct=15.0, deviation_threshold_mad=2.5,
        minimum_absolute_deviation=0.5, minimum_absolute_deviation_unit="kW",
        persistence_periods_open=4, persistence_periods_resolve=4, resolve_band_margin_pct=5.0,
        applicable_operating_states=("running",), operating_state_tag_suffix="CompressorStatus", energy_relevant=True,
    ),

    # --- Production equipment (Disperser/Mill/Mixer share the rule shape) ----
    _rule(
        rule_key="production_motor_power_deviation", baseline_target_key="MotorPower", equipment_type="production_process",
        anomaly_type=ANOMALY_TYPE_DEVIATION, category=CATEGORY_PRODUCTION, direction="high",
        title_template="{instance} Motor Power Above Expected", deviation_threshold_pct=15.0, deviation_threshold_mad=2.5,
        minimum_absolute_deviation=1.5, minimum_absolute_deviation_unit="kW",
        persistence_periods_open=3, persistence_periods_resolve=3, resolve_band_margin_pct=5.0,
        applicable_operating_states=("running",), operating_state_tag_suffix="RunStatus", energy_relevant=True,
    ),
    _rule(
        rule_key="production_vibration_drift", baseline_target_key="Vibration", equipment_type="production_process",
        anomaly_type=ANOMALY_TYPE_DRIFT, category=CATEGORY_CONDITION, direction="high",
        title_template="{instance} Vibration Baseline Increasing", deviation_threshold_pct=None, deviation_threshold_mad=0.0,
        minimum_absolute_deviation=0.0, minimum_absolute_deviation_unit="mm/s",
        persistence_periods_open=0, persistence_periods_resolve=0, resolve_band_margin_pct=0.0,
        applicable_operating_states=(), operating_state_tag_suffix=None, energy_relevant=False,
        drift_threshold_pct=15.0, drift_persistence_evaluations=2,
    ),
    _rule(
        rule_key="production_process_temp_deviation", baseline_target_key="ProcessTemp", equipment_type="production_process",
        anomaly_type=ANOMALY_TYPE_DEVIATION, category=CATEGORY_PROCESS, direction="both",
        title_template="{instance} Process Temperature Deviation", deviation_threshold_pct=15.0, deviation_threshold_mad=2.5,
        minimum_absolute_deviation=2.0, minimum_absolute_deviation_unit="degC",
        persistence_periods_open=3, persistence_periods_resolve=3, resolve_band_margin_pct=5.0,
        applicable_operating_states=("running",), operating_state_tag_suffix="RunStatus", energy_relevant=False,
    ),
    _rule(
        rule_key="filling_power_deviation", baseline_target_key="Power_kW", equipment_type="filling",
        anomaly_type=ANOMALY_TYPE_DEVIATION, category=CATEGORY_PRODUCTION, direction="high",
        title_template="{instance} Power Above Expected", deviation_threshold_pct=15.0, deviation_threshold_mad=2.5,
        minimum_absolute_deviation=0.8, minimum_absolute_deviation_unit="kW",
        persistence_periods_open=3, persistence_periods_resolve=3, resolve_band_margin_pct=5.0,
        applicable_operating_states=("running",), operating_state_tag_suffix="RunStatus", energy_relevant=True,
    ),
]


def rules_for_equipment_type(equipment_type: str) -> list[AnomalyRule]:
    return [r for r in ANOMALY_RULES if r.equipment_type == equipment_type]


def get_rule(rule_key: str) -> AnomalyRule | None:
    for rule in ANOMALY_RULES:
        if rule.rule_key == rule_key:
            return rule
    return None
