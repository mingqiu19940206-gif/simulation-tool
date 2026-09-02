"""Schema v3 migration, round-trip and scenario.type validation tests."""
from __future__ import annotations

import copy
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config_manager import ConfigManager, normalize_config, validate_config
from geometry_calculator import GeometryCalculator


class ConfigSchemaV3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = ConfigManager(ROOT)
        self.default = self.manager.default()

    def _v2_payload(self):
        """Return a v2-shaped copy of the current default (scenario removed)."""
        payload = copy.deepcopy(self.default)
        payload["schema_version"] = 2
        payload.pop("scenario", None)
        payload.pop("via_transition", None)
        return payload

    def test_01_default_config_is_schema_v3(self) -> None:
        self.assertEqual(self.default["schema_version"], 3)
        self.assertEqual(self.default["scenario"]["type"], "surface_capacitor")
        self.assertIn("via_transition", self.default)
        self.assertEqual(self.default["via_transition"]["mode"], "diff_pair")

    def test_02_v2_migrates_to_v3_surface_capacitor(self) -> None:
        normalized, corrections = normalize_config(self._v2_payload())
        self.assertEqual(normalized["schema_version"], 3)
        self.assertEqual(normalized["scenario"]["type"], "surface_capacitor")
        self.assertTrue(any("schema v2" in item for item in corrections))

    def test_03_v1_continuous_migration_to_v3(self) -> None:
        v1 = json.loads((ROOT / "examples" / "v1_legacy.json").read_text(encoding="utf-8"))
        normalized, corrections = normalize_config(v1)
        self.assertEqual(normalized["schema_version"], 3)
        self.assertEqual(normalized["scenario"]["type"], "surface_capacitor")
        self.assertTrue(any("schema_version=1" in item for item in corrections))
        self.assertTrue(any("schema v2" in item for item in corrections))

    def test_04_v3_save_load_round_trip(self) -> None:
        cfg = copy.deepcopy(self.default)
        cfg["capacitor_model"]["mode"] = "lumped_rlc"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "round_trip_v3.json"
            self.manager.save(cfg, path)
            loaded, _ = self.manager.load(path)
        self.assertEqual(loaded["schema_version"], 3)
        self.assertEqual(loaded["scenario"]["type"], "surface_capacitor")
        self.assertEqual(loaded["via_transition"]["pair_pitch"],
                         cfg["via_transition"]["pair_pitch"])
        self.assertEqual(loaded["via_transition"]["entry_trace"]["gap"],
                         cfg["via_transition"]["entry_trace"]["gap"])
        self.assertEqual(loaded["via_transition"]["exit_trace"]["gap"],
                         cfg["via_transition"]["exit_trace"]["gap"])
        self.assertEqual(loaded["capacitor_model"]["mode"], "lumped_rlc")

    def test_05_invalid_scenario_type_rejected(self) -> None:
        cfg = copy.deepcopy(self.default)
        cfg["scenario"]["type"] = "bogus"
        _, result = validate_config(cfg, ROOT, check_output_directory=False)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("scenario.type" in error for error in result.errors))

    def test_06_missing_scenario_type_in_v3_rejected(self) -> None:
        cfg = copy.deepcopy(self.default)
        cfg["scenario"] = {}
        _, result = validate_config(cfg, ROOT, check_output_directory=False)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("scenario.type 缺失" in error for error in result.errors))

    def test_07_migration_preserves_surface_capacitor_geometry(self) -> None:
        migrated, _ = normalize_config(self._v2_payload())
        current_geometry = GeometryCalculator(self.default).calculate().to_dict()
        migrated_geometry = GeometryCalculator(migrated).calculate().to_dict()
        self.assertEqual(current_geometry, migrated_geometry)

    def test_08_via_transition_mm_mil_round_trip(self) -> None:
        from config_manager import to_external_config
        cfg = copy.deepcopy(self.default)
        cfg["via_transition"]["pair_pitch"] = 1.0
        cfg["via_transition"]["signal_via_pitch"] = 1.2
        cfg["via_transition"]["entry_trace"]["gap"] = 0.55
        cfg["via_transition"]["exit_trace"]["gap"] = 0.65
        cfg["via_transition"]["fanout"]["straight_length"] = 1.5
        cfg["via_transition"]["entry_trace"]["length"] = 5.0
        cfg["via_transition"]["padstack"]["drill_diameter"] = 0.3
        cfg["via_transition"]["padstack"]["antipad_per_layer"] = {"L2": 0.7, "L4": 0.9}
        cfg["via_transition"]["padstack"]["antipad"].update({
            "clearance": 0.123,
            "per_layer": {
                "L2": {"enabled": True, "inherit_global": False,
                       "topology": "merged_pair", "shape": "rounded_rectangle",
                       "length": 1.4, "width": 0.7, "corner_radius": 0.11,
                       "rotation_deg": 25.0, "clearance": 0.09},
            },
        })
        cfg["via_transition"]["backdrill"]["residual_stub"] = 0.4
        cfg["via_transition"]["return_vias"]["relative_offset_along_route"] = -0.25
        cfg["via_transition"]["return_vias"]["relative_offset_outward"] = 0.45
        cfg["via_transition"]["return_vias"]["offset_y"] = 0.8
        cfg["via_transition"]["return_vias"]["custom_coordinates"] = [[0.1, 0.2], [0.3, 0.4]]
        cfg["sweeps"]["via_transition.entry_trace.gap"] = {
            "enabled": True, "values": [0.45, 0.55],
        }
        layer_sweep_path = "via_transition.padstack.antipad.per_layer.L2.length"
        cfg["sweeps"][layer_sweep_path] = {
            "enabled": True, "values": [1.2, 1.4],
        }
        normalized, _ = normalize_config(cfg)
        mil = to_external_config(normalized, "mil")
        internal_mil, _ = normalize_config(mil)
        mm = to_external_config(internal_mil, "mm")
        round_trip, _ = normalize_config(mm)
        vt = round_trip["via_transition"]
        self.assertTrue(math.isclose(vt["pair_pitch"], 1.0, abs_tol=1e-9))
        self.assertTrue(math.isclose(vt["signal_via_pitch"], 1.2, abs_tol=1e-9))
        self.assertTrue(math.isclose(vt["entry_trace"]["gap"], 0.55, abs_tol=1e-9))
        self.assertTrue(math.isclose(vt["exit_trace"]["gap"], 0.65, abs_tol=1e-9))
        self.assertTrue(math.isclose(vt["fanout"]["straight_length"], 1.5, abs_tol=1e-9))
        self.assertTrue(math.isclose(vt["entry_trace"]["length"], 5.0, abs_tol=1e-9))
        self.assertTrue(math.isclose(vt["padstack"]["drill_diameter"], 0.3, abs_tol=1e-9))
        # Canonical non-circle L2 wins over its stale legacy circle alias;
        # the untouched L4 alias is still imported losslessly.
        self.assertNotIn("L2", vt["padstack"]["antipad_per_layer"])
        self.assertTrue(math.isclose(vt["padstack"]["antipad_per_layer"]["L4"], 0.9, abs_tol=1e-9))
        anti = vt["padstack"]["antipad"]
        self.assertTrue(math.isclose(anti["clearance"], 0.123, abs_tol=1e-9))
        self.assertEqual(anti["per_layer"]["L2"]["shape"], "rounded_rectangle")
        self.assertTrue(math.isclose(anti["per_layer"]["L2"]["length"], 1.4, abs_tol=1e-9))
        self.assertTrue(math.isclose(anti["per_layer"]["L2"]["width"], 0.7, abs_tol=1e-9))
        self.assertTrue(math.isclose(anti["per_layer"]["L2"]["corner_radius"], 0.11, abs_tol=1e-9))
        self.assertTrue(math.isclose(anti["per_layer"]["L2"]["clearance"], 0.09, abs_tol=1e-9))
        self.assertTrue(math.isclose(anti["per_layer"]["L2"]["rotation_deg"], 25.0, abs_tol=1e-9))
        self.assertTrue(all(math.isclose(value, expected, abs_tol=1e-9)
                            for value, expected in zip(
                                round_trip["sweeps"][layer_sweep_path]["values"],
                                [1.2, 1.4])))
        self.assertTrue(math.isclose(vt["backdrill"]["residual_stub"], 0.4, abs_tol=1e-9))
        self.assertTrue(math.isclose(
            vt["return_vias"]["relative_offset_along_route"], -0.25, abs_tol=1e-9))
        self.assertTrue(math.isclose(
            vt["return_vias"]["relative_offset_outward"], 0.45, abs_tol=1e-9))
        self.assertTrue(math.isclose(vt["return_vias"]["offset_y"], 1.05, abs_tol=1e-9))
        self.assertTrue(math.isclose(vt["return_vias"]["custom_coordinates"][0][0], 0.1, abs_tol=1e-9))
        self.assertTrue(all(math.isclose(value, expected, abs_tol=1e-9)
                            for value, expected in zip(
                                round_trip["sweeps"]["via_transition.entry_trace.gap"]["values"],
                                [0.45, 0.55])))

    def test_09_legacy_return_coordinate_string_loads_and_saves_as_array(self) -> None:
        cfg = copy.deepcopy(self.default)
        cfg["via_transition"]["return_vias"]["pattern"] = "custom"
        cfg["via_transition"]["return_vias"]["custom_coordinates"] = (
            "0.1,0.2; 0.3,0.4")
        normalized, corrections = normalize_config(cfg)
        self.assertEqual(
            normalized["via_transition"]["return_vias"]["custom_coordinates"],
            [[0.1, 0.2], [0.3, 0.4]],
        )
        self.assertEqual(normalized["via_transition"]["return_vias"]["count"], 2)
        self.assertTrue(any("坐标字符串" in item for item in corrections))

    def test_10_legacy_pair_pitch_migrates_to_two_geometry_preserving_gaps(self) -> None:
        cfg = copy.deepcopy(self.default)
        vt = cfg["via_transition"]
        vt["pair_pitch"] = 0.9
        vt["entry_trace"]["width"] = 0.12
        vt["exit_trace"]["width"] = 0.18
        vt["fanout"]["direction"] = "expand"
        vt["entry_trace"].pop("gap")
        vt["exit_trace"].pop("gap")
        normalized, corrections = normalize_config(cfg)
        self.assertAlmostEqual(normalized["via_transition"]["entry_trace"]["gap"], 0.78)
        self.assertAlmostEqual(normalized["via_transition"]["exit_trace"]["gap"], 0.72)
        self.assertEqual(normalized["via_transition"]["fanout"]["direction"], "auto")
        self.assertTrue(any("entry_trace.gap" in item for item in corrections))
        self.assertTrue(any("exit_trace.gap" in item for item in corrections))
        self.assertTrue(any("fanout.direction" in item for item in corrections))

    def test_11_inconsistent_legacy_signal_hole_is_normalized(self) -> None:
        cfg = copy.deepcopy(self.default)
        padstack = cfg["via_transition"]["padstack"]
        padstack.update({
            "drill_diameter": 0.254,
            "finished_hole_diameter": 0.1524,
            "plating_thickness": 0.0254,
        })
        normalized, corrections = normalize_config(cfg)
        result = normalized["via_transition"]["padstack"]
        self.assertAlmostEqual(result["finished_hole_diameter"], 0.2032, places=12)
        self.assertAlmostEqual(
            result["plating_thickness"],
            (result["drill_diameter"] - result["finished_hole_diameter"]) / 2.0,
            places=12,
        )
        self.assertTrue(any("Signal VIA 旧配置" in item for item in corrections),
                        corrections)


if __name__ == "__main__":
    unittest.main(verbosity=2)
