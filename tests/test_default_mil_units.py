from __future__ import annotations

import copy
import json
import math
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config_manager import (
    DEFAULT_DISPLAY_UNIT,
    INTERNAL_LENGTH_UNIT,
    ConfigManager,
    convert_length,
    geometry_hash,
    make_generation_snapshot,
    normalize_config,
    to_external_config,
)
from geometry_calculator import GeometryCalculator
from hfss_script_generator import HfssScriptGenerator
from main_gui import SurfaceCapAntipadApp


class DefaultMilUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = ConfigManager(ROOT)
        self.default = self.manager.default()

    def assert_nested_physical_equal(self, first, second) -> None:
        if isinstance(first, float) or isinstance(second, float):
            self.assertTrue(math.isclose(float(first), float(second),
                                         rel_tol=0.0, abs_tol=1e-12),
                            (first, second))
        elif isinstance(first, dict) and isinstance(second, dict):
            self.assertEqual(set(first), set(second))
            for key in first:
                self.assert_nested_physical_equal(first[key], second[key])
        elif isinstance(first, (list, tuple)) and isinstance(second, (list, tuple)):
            self.assertEqual(len(first), len(second))
            for left, right in zip(first, second):
                self.assert_nested_physical_equal(left, right)
        else:
            self.assertEqual(first, second)

    def test_default_boundary_is_mil_output_with_internal_mm_values(self) -> None:
        self.assertEqual(DEFAULT_DISPLAY_UNIT, "mil")
        self.assertEqual(INTERNAL_LENGTH_UNIT, "mm")
        self.assertEqual(self.default["units"], "mil")
        self.assertEqual(self.default["_internal_units"], "mm")
        self.assertAlmostEqual(self.default["capacitor"]["body_length"], 1.0, places=12)
        self.assertAlmostEqual(self.default["antipad"]["global_expansion"], 0.0254, places=12)

    def test_default_antipad_adds_two_mil_to_pad_length_and_width(self) -> None:
        geometry = GeometryCalculator(self.default).calculate()
        pad = geometry.pad_shapes["P_IN"].bounds
        tool = next(item for item in geometry.antipads_by_layer["L2"]
                    if "P_IN" in item.metadata.get("source_pads", []))
        self.assertAlmostEqual(convert_length(tool.bounds.width - pad.width, "mm", "mil"),
                               2.0, places=10)
        self.assertAlmostEqual(convert_length(tool.bounds.height - pad.height, "mm", "mil"),
                               2.0, places=10)

    def test_default_save_is_external_mil_without_internal_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "default_mil.json"
            self.manager.save(self.default, path)
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["units"], "mil")
        self.assertAlmostEqual(payload["antipad"]["global_expansion"], 1.0, places=12)
        self.assertNotIn("_internal_units", json.dumps(payload, ensure_ascii=False))

    def test_legacy_mm_and_mil_files_load_to_identical_physics(self) -> None:
        mm, _ = self.manager.load(ROOT / "examples" / "default_mm.json")
        mil, _ = self.manager.load(ROOT / "examples" / "equivalent_mil.json")
        self.assertEqual(mm["units"], "mm")
        self.assertEqual(mil["units"], "mil")
        self.assert_nested_physical_equal(
            GeometryCalculator(mm).calculate().to_dict(),
            GeometryCalculator(mil).calculate().to_dict(),
        )

    def test_geometry_and_snapshot_metadata_are_internal_mm(self) -> None:
        surface_geometry = GeometryCalculator(self.default).calculate()
        self.assertEqual(surface_geometry.units, "mm")
        self.assertEqual(surface_geometry.to_dict()["units"], "mm")
        snapshot = make_generation_snapshot(self.default, surface_geometry)
        self.assertEqual(snapshot["geometry_snapshot"]["units"], "mm")

        via_config = copy.deepcopy(self.default)
        via_config["scenario"]["type"] = "via_transition"
        via_geometry = GeometryCalculator(via_config).calculate()
        self.assertEqual(via_geometry.units, "mm")
        self.assertEqual(via_geometry.to_dict()["units"], "mm")

    def test_repeated_external_unit_round_trips_do_not_drift(self) -> None:
        config = copy.deepcopy(self.default)
        baseline = copy.deepcopy(config)
        for _ in range(10):
            config, _ = normalize_config(to_external_config(config, "mm"))
            config, _ = normalize_config(to_external_config(config, "mil"))
        for section, key in (
            ("trace", "trace_width"),
            ("capacitor", "pad_length"),
            ("antipad", "global_expansion"),
            ("simulation", "airbox_margin"),
        ):
            self.assertTrue(math.isclose(config[section][key], baseline[section][key],
                                         rel_tol=0.0, abs_tol=1e-12))
        self.assert_nested_physical_equal(config["stackup"], baseline["stackup"])
        self.assert_nested_physical_equal(config["sweeps"], baseline["sweeps"])

    def test_default_hfss_output_is_mil_and_mm_variant_is_physically_equal(self) -> None:
        mil = copy.deepcopy(self.default)
        mm = copy.deepcopy(self.default)
        mm["units"] = "mm"
        self.assert_nested_physical_equal(
            GeometryCalculator(mil).calculate().to_dict(),
            GeometryCalculator(mm).calculate().to_dict(),
        )
        self.assertEqual(
            geometry_hash(GeometryCalculator(mil).calculate()),
            geometry_hash(GeometryCalculator(mm).calculate()),
        )
        with tempfile.TemporaryDirectory() as directory:
            mil["project"]["overwrite_existing"] = True
            result = HfssScriptGenerator(mil, ROOT).generate(Path(directory))
            persisted = json.loads(result.config_path.read_text(encoding="utf-8"))
            script = result.script_path.read_text(encoding="utf-8")
        self.assertEqual(persisted["units"], "mil")
        self.assertIn("app.modeler.model_units = UNITS", script)
        self.assertIn('return float(value) if UNITS == "mm" else float(value) / 0.0254', script)

    def test_via_antipad_defaults_keep_existing_physical_meanings(self) -> None:
        padstack = self.default["via_transition"]["padstack"]
        return_vias = self.default["via_transition"]["return_vias"]
        gnd_vias = self.default["gnd_vias"]
        self.assertAlmostEqual(padstack["antipad"]["clearance"], 0.1, places=12)
        self.assertAlmostEqual(padstack["antipad"]["diameter"], 0.6, places=12)
        self.assertAlmostEqual(return_vias["antipad_diameter"], 0.6, places=12)
        self.assertAlmostEqual(gnd_vias["antipad_diameter"], 0.6, places=12)
        self.assertAlmostEqual((padstack["antipad"]["diameter"]
                                - padstack["connected_pad_diameter"]) / 2.0,
                               0.1, places=12)
        self.assertAlmostEqual((return_vias["antipad_diameter"]
                                - return_vias["pad_diameter"]) / 2.0,
                               0.1, places=12)
        self.assertAlmostEqual((gnd_vias["antipad_diameter"]
                                - gnd_vias["pad_diameter"]) / 2.0,
                               0.1, places=12)


class DefaultMilGuiTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.app = SurfaceCapAntipadApp()
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

    def test_startup_and_restore_defaults_show_mil_and_one_mil_expansion(self) -> None:
        self.assertEqual(self.app.display_unit, "mil")
        self.assertEqual(self.app.unit_var.get(), "mil")
        self.assertEqual(self.app.variables["antipad.global_expansion"].get(), "1")
        self.app.unit_var.set("mm")
        self.app._unit_changed()
        self.app.restore_defaults()
        self.assertEqual(self.app.display_unit, "mil")
        self.assertEqual(self.app.unit_var.get(), "mil")
        self.assertEqual(self.app.variables["antipad.global_expansion"].get(), "1")

    def test_repeated_gui_unit_switches_keep_physical_values(self) -> None:
        config = self.app.manager.default()
        config["via_transition"]["return_vias"].update({
            "pattern": "custom",
            "count": 2,
            "custom_coordinates": [[0.1, 0.2], [0.3, 0.4]],
        })
        config["gnd_vias"].update({
            "pattern": "custom",
            "count": 2,
            "custom_coordinates": [[-0.125, 0.225], [0.325, -0.425]],
        })
        self.app._load_to_widgets(config)
        baseline = self.app._collect()
        for _ in range(5):
            self.app.unit_var.set("mm")
            self.app._unit_changed()
            self.app.unit_var.set("mil")
            self.app._unit_changed()
        result = self.app._collect()
        self.assertEqual(result["stackup"], baseline["stackup"])
        self.assertEqual(result["sweeps"], baseline["sweeps"])
        self.assertEqual(
            result["via_transition"]["return_vias"]["custom_coordinates"],
            baseline["via_transition"]["return_vias"]["custom_coordinates"],
        )
        self.assertEqual(
            result["gnd_vias"]["custom_coordinates"],
            baseline["gnd_vias"]["custom_coordinates"],
        )
        self.assertTrue(math.isclose(result["antipad"]["global_expansion"], 0.0254,
                                     rel_tol=0.0, abs_tol=1e-12))


if __name__ == "__main__":
    unittest.main(verbosity=2)
