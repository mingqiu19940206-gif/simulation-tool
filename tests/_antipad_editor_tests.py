from __future__ import annotations

import copy
import sys
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from antipad_editor import (
    LAYER_TYPE_LABELS, MODE_LABELS, analyze_antipad, effective_expansion,
    expected_window_count, layer_from_form,
)
from config_manager import ConfigManager, convert_length, validate_config
from geometry_calculator import GeometryCalculator
from main_gui import SurfaceCapAntipadApp

REFERENCE_LAYER = "L2"


class AntipadEditorLogicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = ConfigManager(ROOT)
        self.default = self.manager.default()

    def test_mode_and_layer_labels_keep_backend_enums(self) -> None:
        self.assertEqual(MODE_LABELS["independent_pad"], "每个焊盘独立开窗")
        self.assertEqual(MODE_LABELS["merged_per_capacitor"], "每颗电容合并开窗")
        self.assertEqual(MODE_LABELS["merged_all"], "P/N 全部合并开窗")
        self.assertEqual(LAYER_TYPE_LABELS["plane"], "平面层")
        self.assertEqual(set(MODE_LABELS), {"independent_pad", "merged_per_capacitor", "merged_all"})

    def test_three_modes_use_real_geometry_and_expected_counts(self) -> None:
        for mode, count in (("independent_pad", 4), ("merged_per_capacitor", 2), ("merged_all", 1)):
            config = copy.deepcopy(self.default)
            config["antipad"]["mode"] = mode
            config["capacitor"]["pad_rotation"] = 17.0
            geometry = GeometryCalculator(config).calculate()
            tools = geometry.antipads_by_layer[REFERENCE_LAYER]
            self.assertEqual(len(tools), count)
            self.assertEqual(expected_window_count(mode), count)
            if mode == "independent_pad":
                self.assertEqual({abs(tool.rotation_deg) for tool in tools}, {17.0})
            else:
                for tool in tools:
                    self.assertTrue(all(
                        abs(first.x - second.x) < 1e-12 or abs(first.y - second.y) < 1e-12
                        for first, second in zip(tool.points, tool.points[1:] + tool.points[:1])
                    ))

    def test_global_override_and_disabled_layer(self) -> None:
        gnd1 = next(layer for layer in self.default["stackup"] if layer["name"] == REFERENCE_LAYER)
        self.assertAlmostEqual(effective_expansion(gnd1, 0.2), 0.2)
        gnd1["antipad_expansion_override"] = 0.35
        self.assertAlmostEqual(effective_expansion(gnd1, 0.2), 0.35)
        geometry = GeometryCalculator(self.default).calculate()
        self.assertAlmostEqual(geometry.antipads_by_layer[REFERENCE_LAYER][0].metadata["expansion"], 0.35)
        # Manual layer disable is authoritative only when the new recommended
        # mount-side follower is explicitly off.
        self.default["antipad"]["follow_mount_side"] = False
        gnd1["enable_antipad"] = False
        self.assertIsNone(effective_expansion(gnd1, 0.2))
        self.assertNotIn(REFERENCE_LAYER, GeometryCalculator(self.default).calculate().antipads_by_layer)

    def test_structured_layer_form_converts_units_and_enforces_interlocks(self) -> None:
        values = {
            "name": "GND_TEST", "type": "plane", "thickness": "1.37795",
            "material": "copper", "dk": "", "df": "", "conductivity": "58000000",
            "is_reference_plane": True, "enable_antipad": True, "use_global": False,
            "override": "7.874015748",
        }
        layer = layer_from_form(values, "mil")
        self.assertAlmostEqual(layer["thickness"], 0.035, places=10)
        self.assertAlmostEqual(layer["antipad_expansion_override"], 0.2, places=9)
        self.assertTrue(layer["is_reference_plane"])
        self.assertTrue(layer["enable_antipad"])
        values["type"] = "dielectric"
        values.update({"dk": "3.8", "df": "0.015"})
        layer = layer_from_form(values, "mil")
        self.assertFalse(layer["is_reference_plane"])
        self.assertFalse(layer["enable_antipad"])
        self.assertIsNone(layer["conductivity"])

    def test_layer_validation_is_immediate_and_clear(self) -> None:
        values = {"name": "bad name", "type": "plane", "thickness": "0", "material": "copper",
                  "conductivity": "58000000", "is_reference_plane": True,
                  "enable_antipad": True, "use_global": False, "override": "-1"}
        with self.assertRaisesRegex(ValueError, "层名称"):
            layer_from_form(values, "mm")
        values["name"] = "GND3"
        with self.assertRaisesRegex(ValueError, "层厚"):
            layer_from_form(values, "mm")
        values["thickness"] = "0.035"
        with self.assertRaisesRegex(ValueError, "覆盖值"):
            layer_from_form(values, "mm")

    def test_multilayer_overrides_and_summary_use_geometry(self) -> None:
        config = copy.deepcopy(self.default)
        next(layer for layer in config["stackup"] if layer["name"] == "L2")["antipad_expansion_override"] = 0.15
        next(layer for layer in config["stackup"] if layer["name"] == "L4").update(
            enable_antipad=True, antipad_expansion_override=0.35
        )
        geometry = GeometryCalculator(config).calculate()
        self.assertAlmostEqual(geometry.antipads_by_layer["L2"][0].metadata["expansion"], 0.15)
        self.assertAlmostEqual(geometry.antipads_by_layer["L4"][0].metadata["expansion"], 0.35)
        analysis = analyze_antipad(config, geometry, "L4", "mm")
        self.assertEqual(analysis.values["外扩来源"], "本层覆盖")
        self.assertEqual(analysis.values["开窗数量"], "4")
        self.assertIn("0.35", analysis.values["有效外扩"])

    def test_plane_overrun_and_merged_risk_are_reported(self) -> None:
        config = copy.deepcopy(self.default)
        config["antipad"]["mode"] = "merged_all"
        config["antipad"]["global_expansion"] = 10.0
        geometry = GeometryCalculator(config).calculate()
        analysis = analyze_antipad(config, geometry, REFERENCE_LAYER, "mm")
        self.assertTrue(any("超出参考平面" in message for message in analysis.errors))
        self.assertTrue(any("移除最多铜皮" in message for message in analysis.warnings))

    def test_config_roundtrip_still_preserves_layer_override(self) -> None:
        config = copy.deepcopy(self.default)
        next(layer for layer in config["stackup"] if layer["name"] == REFERENCE_LAYER)["antipad_expansion_override"] = 0.2718281828
        normalized, result = validate_config(config, ROOT, check_output_directory=False)
        self.assertTrue(result.is_valid, result.errors)
        self.assertEqual(normalized["schema_version"], 3)
        self.assertAlmostEqual(next(layer for layer in normalized["stackup"] if layer["name"] == REFERENCE_LAYER)["antipad_expansion_override"], 0.2718281828)


class AntipadEditorGuiTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.app = SurfaceCapAntipadApp()
            self.app.geometry("1120x720")
            self.app.withdraw()
            self.app.update_idletasks()
        except tk.TclError as exc:
            self.skipTest(f"Tk GUI unavailable: {exc}")

    def tearDown(self) -> None:
        if hasattr(self, "app"):
            try:
                for after_id in self.app.tk.call("after", "info"):
                    self.app.after_cancel(after_id)
            except tk.TclError:
                pass
            self.app.destroy()

    def test_workbench_smoke_modes_units_and_layer_selection(self) -> None:
        workbench = self.app.antipad_workbench
        self.assertEqual(len(workbench.tree["columns"]), 16)
        self.assertIn("hfss_status", workbench.tree["columns"])
        for mode in MODE_LABELS:
            self.app.variables["antipad.mode"].set(mode)
            self.app.refresh_visuals()
            self.assertEqual(self.app._collect()["antipad"]["mode"], mode)
        original_thickness = next(layer for layer in self.app.config["stackup"] if layer["name"] == REFERENCE_LAYER)["thickness"]
        self.app.display_unit = "mil"
        self.app.unit_var.set("mil")
        self.app._load_to_widgets(self.app._collect())
        self.assertAlmostEqual(next(layer for layer in self.app.config["stackup"] if layer["name"] == REFERENCE_LAYER)["thickness"], original_thickness)
        workbench.select_layer_by_name(REFERENCE_LAYER)
        self.assertEqual(workbench.preview.layer_var.get(), REFERENCE_LAYER)
        self.assertGreater(len(workbench.preview.canvas.find_all()), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
