from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config_manager import ConfigManager, make_generation_snapshot, normalize_config, validate_config
from geometry_calculator import GeometryCalculator
from hfss_script_generator import HfssScriptGenerator
from main_gui import SurfaceCapAntipadApp
from model_preview import build_model_scene


EXPECTED_LAYERS = (
    ("Top_Soldermask", "soldermask", 0.984),
    ("L1", "signal", 1.85),
    ("DIEL_L1_L2", "dielectric", 2.59),
    ("L2", "plane", 0.60),
    ("DIEL_L2_L3", "dielectric", 3.54),
    ("L3", "signal", 0.60),
    ("DIEL_L3_L4", "dielectric", 4.23),
    ("L4", "plane", 0.60),
    ("DIEL_L4_L5", "dielectric", 3.54),
    ("L5", "signal", 0.60),
    ("DIEL_L5_L6", "dielectric", 4.14),
    ("L6", "plane", 1.20),
    ("DIEL_L6_L7", "dielectric", 2.56),
    ("L7", "signal", 2.52),
    ("DIEL_L7_L8", "dielectric", 6.09),
    ("L8", "signal", 2.52),
    ("DIEL_L8_L9", "dielectric", 2.56),
    ("L9", "plane", 1.20),
    ("DIEL_L9_L10", "dielectric", 4.14),
    ("L10", "signal", 0.60),
    ("DIEL_L10_L11", "dielectric", 3.54),
    ("L11", "plane", 0.60),
    ("DIEL_L11_L12", "dielectric", 4.23),
    ("L12", "signal", 0.60),
    ("DIEL_L12_L13", "dielectric", 3.54),
    ("L13", "plane", 0.60),
    ("DIEL_L13_L14", "dielectric", 2.59),
    ("L14", "signal", 1.85),
    ("Bottom_Soldermask", "soldermask", 0.984),
)


def _all_string_values(value):
    if isinstance(value, dict):
        for child in value.values():
            yield from _all_string_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _all_string_values(child)
    elif isinstance(value, str):
        yield value


class M6GDefaultStackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = ConfigManager(ROOT)
        self.config = self.manager.default()

    def test_order_counts_and_exact_mil_conversion(self) -> None:
        stackup = self.config["stackup"]
        self.assertEqual(len(stackup), 29)
        self.assertEqual([layer["name"] for layer in stackup],
                         [item[0] for item in EXPECTED_LAYERS])
        self.assertEqual(sum(layer["type"] in {"signal", "plane"} for layer in stackup), 14)
        self.assertEqual(sum(layer["type"] == "dielectric" for layer in stackup), 13)
        self.assertEqual(sum(layer["type"] == "soldermask" for layer in stackup), 2)
        for layer, (name, kind, mil) in zip(stackup, EXPECTED_LAYERS):
            self.assertEqual((layer["name"], layer["type"]), (name, kind))
            self.assertAlmostEqual(layer["thickness"], mil * 0.0254, places=12)

    def test_finished_and_core_thickness_are_not_rescaled(self) -> None:
        stackup = self.config["stackup"]
        total = sum(layer["thickness"] for layer in stackup)
        without_masks = sum(layer["thickness"] for layer in stackup
                            if layer["type"] != "soldermask")
        self.assertAlmostEqual(without_masks, 1.606042, places=12)
        self.assertAlmostEqual(total, 1.6560292, places=12)
        self.assertNotAlmostEqual(total, 1.600, places=6)

    def test_material_values_distinguish_excel_dk_and_provisional_values(self) -> None:
        by_name = {layer["name"]: layer for layer in self.config["stackup"]}
        self.assertEqual(by_name["DIEL_L1_L2"]["dk"], 2.99)
        self.assertEqual(by_name["DIEL_L13_L14"]["dk"], 2.99)
        for name in ("DIEL_L2_L3", "DIEL_L4_L5", "DIEL_L10_L11", "DIEL_L12_L13"):
            self.assertEqual(by_name[name]["dk"], 2.98)
            self.assertIn("PROVISIONAL_DF", by_name[name]["material"])
        for name in ("DIEL_L3_L4", "DIEL_L5_L6", "DIEL_L9_L10", "DIEL_L11_L12"):
            self.assertEqual(by_name[name]["dk"], 3.11)
        for name in ("DIEL_L6_L7", "DIEL_L7_L8", "DIEL_L8_L9"):
            self.assertEqual(by_name[name]["dk"], 3.8)
            self.assertIn("PROVISIONAL", by_name[name]["material"])
        self.assertTrue(all(layer["df"] == 0.015 for layer in by_name.values()
                            if layer["type"] in {"dielectric", "soldermask"}))
        self.assertTrue(all("Panasonic R-5775(G)" in layer["material"]
                            for layer in by_name.values() if layer["type"] == "dielectric"))

    def test_default_links_and_validation_are_consistent(self) -> None:
        by_name = {layer["name"]: layer for layer in self.config["stackup"]}
        self.assertEqual(self.config["trace"]["copper_thickness"], 0.04699)
        self.assertEqual(by_name["L1"]["thickness"], self.config["trace"]["copper_thickness"])
        self.assertEqual(self.config["simulation"]["port_reference_layer"], "L2")
        self.assertFalse(self.config["gnd_vias"]["enabled"])
        self.assertEqual(self.config["gnd_vias"]["start_layer"], "L1")
        self.assertEqual(self.config["gnd_vias"]["end_layer"], "L14")
        self.assertEqual(self.config["gnd_vias"]["connect_layers"], ["L2"])
        self.assertEqual([name for name, layer in by_name.items() if layer["enable_antipad"]], ["L2"])
        normalized, result = validate_config(self.config, ROOT, check_output_directory=False)
        self.assertTrue(result.is_valid, result.errors)
        self.assertTrue(any("临时仿真值，待材料数据表确认" in warning
                            for warning in result.warnings))
        self.assertTrue(any("不生成这些层的导体图形" in warning and "L7" in warning
                            for warning in result.warnings))
        geometry = GeometryCalculator(normalized).calculate()
        self.assertEqual({port.reference_layer for port in geometry.ports}, {"L2"})
        self.assertEqual(set(geometry.antipads_by_layer), {"L2"})
        self.assertEqual(geometry.gnd_vias, [])

    def test_preview_and_hfss_snapshot_share_stackup_and_l1_geometry(self) -> None:
        normalized, validation = self.manager.validate(self.config, check_output_directory=False)
        geometry = GeometryCalculator(normalized).calculate()
        scene = build_model_scene(normalized, geometry, validation)
        self.assertEqual([item.layer for item in scene.objects if item.category == "PCB叠层"],
                         [layer["name"] for layer in normalized["stackup"]])
        self.assertEqual({item.layer for item in scene.objects
                          if item.category in {"信号走线", "电容与焊盘", "电气模型"}}, {"L1"})
        snapshot = make_generation_snapshot(normalized, geometry)
        self.assertEqual(snapshot["geometry_snapshot"]["stackup"], geometry.to_dict()["stackup"])
        self.assertEqual(snapshot["config_snapshot"]["stackup"], normalized["stackup"])
        self.assertEqual(snapshot["geometry_snapshot"]["trace_z_bottom"],
                         next(layer.z_bottom for layer in geometry.stackup if layer.name == "L1"))

    def test_hfss_generation_has_soldermask_and_no_stale_default_layer_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = copy.deepcopy(self.config)
            config["project"]["overwrite_existing"] = True
            generated = HfssScriptGenerator(config, ROOT).generate(Path(directory))
            persisted = json.loads(generated.config_path.read_text(encoding="utf-8"))
            strings = set(_all_string_values(persisted))
            self.assertTrue({"Top_Soldermask", "Bottom_Soldermask", "L1", "L2"} <= strings)
            self.assertTrue({"TOP", "GND1", "GND2", "DIEL1", "DIEL2"}.isdisjoint(strings))
            script = generated.script_path.read_text(encoding="utf-8")
            self.assertIn('layer["type"] not in ("dielectric", "soldermask")', script)
            self.assertIn('layer["layer_type"] not in ("dielectric", "soldermask")', script)
            completed = subprocess.run(
                [sys.executable, str(generated.script_path), "--validate-only"],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_legacy_stackup_is_preserved_and_missing_links_are_derived(self) -> None:
        legacy = json.loads((ROOT / "examples" / "v1_legacy.json").read_text(encoding="utf-8"))
        normalized, corrections = normalize_config(legacy)
        self.assertEqual([layer["name"] for layer in normalized["stackup"]],
                         ["TOP", "DIEL1", "GND1", "DIEL2", "GND2"])
        self.assertEqual(normalized["gnd_vias"]["start_layer"], "TOP")
        self.assertEqual(normalized["gnd_vias"]["end_layer"], "GND2")
        self.assertEqual(normalized["gnd_vias"]["connect_layers"], ["GND1", "GND2"])
        self.assertEqual(normalized["simulation"]["port_reference_layer"], "GND1")
        self.assertTrue(any("重新派生" in message for message in corrections))

    def test_partial_custom_stackup_does_not_inherit_new_l1_copper_thickness(self) -> None:
        legacy = json.loads((ROOT / "examples" / "v1_legacy.json").read_text(encoding="utf-8"))
        partial = {"schema_version": 2, "units": "mm", "stackup": legacy["stackup"]}
        normalized, corrections = normalize_config(partial)
        self.assertEqual(normalized["trace"]["copper_thickness"], 0.035)
        self.assertEqual(normalized["stackup"][0]["thickness"], 0.035)
        self.assertTrue(any("未套用新版默认铜厚" in message for message in corrections))

    def test_explicit_stale_layer_links_fail_instead_of_silent_remapping(self) -> None:
        config = copy.deepcopy(self.config)
        config["gnd_vias"]["start_layer"] = "TOP"
        config["simulation"]["port_reference_layer"] = "GND1"
        _normalized, result = validate_config(config, ROOT, check_output_directory=False)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("start_layer/end_layer" in error for error in result.errors))
        self.assertTrue(any("port_reference_layer" in error for error in result.errors))


class M6GRestoreDefaultGuiTest(unittest.TestCase):
    def test_restore_default_stackup_uses_m6g_layers(self) -> None:
        try:
            app = SurfaceCapAntipadApp()
            app.withdraw()
            app.update_idletasks()
        except tk.TclError as exc:
            self.skipTest(f"Tk GUI unavailable: {exc}")
        try:
            legacy = json.loads((ROOT / "examples" / "v1_legacy.json").read_text(encoding="utf-8"))
            custom = app.manager.default()
            custom["stackup"] = copy.deepcopy(legacy["stackup"])
            custom["trace"]["copper_thickness"] = 0.035
            custom["gnd_vias"].update({
                "start_layer": "TOP", "end_layer": "GND2",
                "connect_layers": ["GND1", "GND2"],
            })
            custom["simulation"]["port_reference_layer"] = "GND1"
            app._load_to_widgets(custom)
            with mock.patch("antipad_editor_core.messagebox.askyesno", return_value=True):
                app.antipad_workbench.restore_stackup()
            self.assertEqual([layer["name"] for layer in app.config["stackup"]],
                             [item[0] for item in EXPECTED_LAYERS])
            self.assertEqual(app.config["trace"]["copper_thickness"], 0.04699)
            self.assertFalse(app.config["gnd_vias"]["enabled"])
            self.assertEqual(app.config["gnd_vias"]["start_layer"], "L1")
            self.assertEqual(app.config["gnd_vias"]["end_layer"], "L14")
            self.assertEqual(app.config["gnd_vias"]["connect_layers"], ["L2"])
            self.assertEqual(app.config["simulation"]["port_reference_layer"], "L2")
            collected, result = app.manager.validate(app._collect(), check_output_directory=False)
            self.assertTrue(result.is_valid, result.errors)
            self.assertEqual([layer["name"] for layer in collected["stackup"]],
                             [item[0] for item in EXPECTED_LAYERS])
        finally:
            app.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
