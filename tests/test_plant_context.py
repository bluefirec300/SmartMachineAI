import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from simulator import plant_context


class TestTagNamingHelpers(unittest.TestCase):
    def test_canonical_key_strips_plant_and_instance_numbers(self):
        self.assertEqual(
            plant_context.canonical_key("P01.UTILITY.AC01.OutletTemp"),
            "UTILITY.AC.OutletTemp",
        )

    def test_instance_key_keeps_instance_number(self):
        self.assertEqual(
            plant_context.instance_key("P01.UTILITY.AC01.OutletTemp"),
            "P01.UTILITY.AC01",
        )

    def test_plant_of(self):
        self.assertEqual(plant_context.plant_of("P01.UTILITY.AC01"), "p01")
        self.assertEqual(plant_context.plant_of("P02.PROD.MILL01"), "p02")


class TestResponseSpeed(unittest.TestCase):
    def test_room_temperature_settles_slower_than_electrical(self):
        room = plant_context.response_speed_multiplier(
            "P01.COLDROOM.CR01.RoomTemp", "temperature"
        )
        electrical = plant_context.response_speed_multiplier(
            "P01.UTILITY.AC01.Power_kW", "power"
        )
        self.assertLess(room, electrical)

    def test_unknown_measurement_falls_back_to_default(self):
        self.assertEqual(
            plant_context.response_speed_multiplier("P01.SOME.THING01.Weird", "mystery"),
            plant_context.DEFAULT_RESPONSE_SPEED_MULTIPLIER,
        )


class TestAmbientEnvironment(unittest.TestCase):
    def test_temperature_stays_within_configured_swing(self):
        for hour in range(24):
            now = datetime(2026, 8, 15, hour, 0)
            temp = plant_context.ambient_target_temperature(now)
            self.assertGreaterEqual(
                temp,
                plant_context.AMBIENT_BASE_TEMP_C - plant_context.AMBIENT_DIURNAL_TEMP_AMPLITUDE_C - 0.01,
            )
            self.assertLessEqual(
                temp,
                plant_context.AMBIENT_BASE_TEMP_C + plant_context.AMBIENT_DIURNAL_TEMP_AMPLITUDE_C + 0.01,
            )

    def test_peak_hour_is_hotter_than_pre_dawn(self):
        peak = plant_context.ambient_target_temperature(datetime(2026, 8, 15, 15, 0))
        trough = plant_context.ambient_target_temperature(datetime(2026, 8, 15, 3, 0))
        self.assertGreater(peak, trough)

    def test_humidity_runs_opposite_temperature(self):
        peak_humidity = plant_context.ambient_target_humidity(datetime(2026, 8, 15, 15, 0))
        trough_humidity = plant_context.ambient_target_humidity(datetime(2026, 8, 15, 3, 0))
        self.assertLess(peak_humidity, trough_humidity)

    def test_hvac_demand_push_increases_with_hotter_and_more_humid_air(self):
        mild = plant_context.hvac_demand_push_fraction(26.0, 50.0)
        hot_humid = plant_context.hvac_demand_push_fraction(35.0, 90.0)
        self.assertGreater(hot_humid, mild)
        self.assertGreaterEqual(mild, 0.0)


class TestEfficiencyAndDeterioration(unittest.TestCase):
    def test_efficiency_factor_is_deterministic(self):
        first = plant_context.efficiency_factor("P01.UTILITY.AC01")
        second = plant_context.efficiency_factor("P01.UTILITY.AC01")
        self.assertEqual(first, second)

    def test_efficiency_factor_within_configured_range(self):
        low, high = plant_context.EFFICIENCY_JITTER_RANGE
        for i in range(50):
            factor = plant_context.efficiency_factor(f"P01.UTILITY.AC{i:02d}")
            self.assertGreaterEqual(factor, low)
            self.assertLessEqual(factor, high)

    def test_efficiency_factor_independent_of_plant(self):
        """A specific instance's jitter must not be plant-correlated -
        this is what lets an individual P02 unit land better than its
        P01 counterpart even while P02 trends worse overall (item 2)."""
        p01_values = [plant_context.efficiency_factor(f"P01.UTILITY.AC{i:02d}") for i in range(20)]
        p02_values = [plant_context.efficiency_factor(f"P02.UTILITY.AC{i:02d}") for i in range(20)]
        # Not every P02 value should be worse (higher) than its P01
        # counterpart - if it were, that would mean plant identity is
        # leaking into the jitter, which item 2 explicitly forbids.
        better_in_p02 = sum(1 for a, b in zip(p01_values, p02_values) if b < a)
        self.assertGreater(better_in_p02, 0)

    def test_deterioration_eligible_is_deterministic(self):
        self.assertEqual(
            plant_context.deterioration_eligible("P01.UTILITY.AC01"),
            plant_context.deterioration_eligible("P01.UTILITY.AC01"),
        )

    def test_deterioration_eligible_fraction_is_a_minority(self):
        sample = [plant_context.deterioration_eligible(f"P01.SAMPLE.EQ{i:03d}") for i in range(2000)]
        fraction = sum(sample) / len(sample)
        # Target is 0.18 - allow generous tolerance since this is a
        # single large random sample, not a statistical proof.
        self.assertLess(fraction, 0.30)
        self.assertGreater(fraction, 0.08)

    def test_is_wear_sensitive_tag(self):
        self.assertTrue(plant_context.is_wear_sensitive_tag("P01.PROD.MILL01.Vibration"))
        self.assertTrue(plant_context.is_wear_sensitive_tag("P01.PROD.MILL01.BearingTemp"))
        self.assertTrue(plant_context.is_wear_sensitive_tag("P01.PROD.MILL01.MotorCurrent"))
        self.assertFalse(plant_context.is_wear_sensitive_tag("P01.PROD.MILL01.ProcessTemp"))


class TestMainIncomerAggregationBoundary(unittest.TestCase):
    ALL_TAGS = [
        "P01.UTILITY.AC01.Power_kW",
        "P01.UTILITY.CHL01.Power_kW",
        "P01.COLDROOM.CR01.CompressorPower",
        "P01.PROD.MILL01.MotorPower",
        "P01.FILL.FILL01.Power_kW",
        # Explicitly excluded - must never appear in the contributing list.
        "P01.ELEC.GEN01.Power_kW",       # backup generation source
        "P01.ELEC.TR01.LoadPct",          # sub-metering pass-through
        "P01.IT.UPS01.LoadPct",           # sub-metering pass-through
        "P01.FIRE.SYS01.HeaderPressure",  # not electrical at all
        # Self-reference guards - must never sum into themselves.
        "P01.ELEC.MAIN.Power_kW",
        "P01.ELEC.INCOMER01.Power_kW",
        # A different plant's tag must never leak into P01's boundary.
        "P02.UTILITY.AC01.Power_kW",
        # Not a power-type tag at all.
        "P01.UTILITY.AC01.OutletTemp",
    ]

    def test_includes_expected_end_loads(self):
        contributing = plant_context.main_incomer_contributing_tags(self.ALL_TAGS, "p01")
        for expected in (
            "P01.UTILITY.AC01.Power_kW",
            "P01.UTILITY.CHL01.Power_kW",
            "P01.COLDROOM.CR01.CompressorPower",
            "P01.PROD.MILL01.MotorPower",
            "P01.FILL.FILL01.Power_kW",
        ):
            self.assertIn(expected, contributing)

    def test_excludes_generator_transformer_ups_and_fire_system(self):
        contributing = plant_context.main_incomer_contributing_tags(self.ALL_TAGS, "p01")
        for excluded in (
            "P01.ELEC.GEN01.Power_kW",
            "P01.ELEC.TR01.LoadPct",
            "P01.IT.UPS01.LoadPct",
            "P01.FIRE.SYS01.HeaderPressure",
        ):
            self.assertNotIn(excluded, contributing)

    def test_never_sums_main_incomer_into_itself(self):
        contributing = plant_context.main_incomer_contributing_tags(self.ALL_TAGS, "p01")
        self.assertNotIn("P01.ELEC.MAIN.Power_kW", contributing)
        self.assertNotIn("P01.ELEC.INCOMER01.Power_kW", contributing)

    def test_never_crosses_plants(self):
        contributing = plant_context.main_incomer_contributing_tags(self.ALL_TAGS, "p01")
        self.assertNotIn("P02.UTILITY.AC01.Power_kW", contributing)

    def test_ignores_non_power_tags(self):
        contributing = plant_context.main_incomer_contributing_tags(self.ALL_TAGS, "p01")
        self.assertNotIn("P01.UTILITY.AC01.OutletTemp", contributing)

    def test_p02_base_load_higher_than_p01(self):
        """The one small structural plant modifier (item 2)."""
        self.assertGreater(
            plant_context.PLANT_BASE_LOAD_KW["p02"],
            plant_context.PLANT_BASE_LOAD_KW["p01"],
        )


class TestProductionState(unittest.TestCase):
    def test_returns_empty_dict_when_tables_do_not_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "empty.db"
            connection = sqlite3.connect(database_path)
            connection.execute("CREATE TABLE placeholder (id INTEGER)")
            connection.commit()
            connection.close()

            state = plant_context.get_production_state(database_path)
            self.assertEqual(state, {})

    def test_equipment_name_to_instance_key_mapping(self):
        self.assertEqual(
            plant_context._equipment_name_to_instance_key("p01_bead_mill_mill01"),
            "P01.PROD.MILL01",
        )
        self.assertEqual(
            plant_context._equipment_name_to_instance_key("p01_filling_machine_fill01"),
            "P01.FILL.FILL01",
        )
        self.assertEqual(
            plant_context._equipment_name_to_instance_key("p02_high_speed_disperser_disp01"),
            "P02.PROD.DISP01",
        )
        self.assertEqual(
            plant_context._equipment_name_to_instance_key("p01_mixer_mix01"),
            "P01.PROD.MIX01",
        )

    def test_is_production_linked_instance(self):
        self.assertTrue(plant_context.is_production_linked_instance("P01.PROD.MILL01"))
        self.assertTrue(plant_context.is_production_linked_instance("P01.FILL.FILL01"))
        self.assertFalse(plant_context.is_production_linked_instance("P01.UTILITY.AC01"))


if __name__ == "__main__":
    unittest.main()
