from __future__ import annotations

import copy
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from config_manager import ConfigManager, convert_length, normalize_config, to_external_config
from geometry_calculator import GeometryCalculator
from hfss_script_generator import HfssScriptGenerator
from model_preview import build_model_scene
from model_validator import ModelValidator
from simulation_sweep_semantics import inspect_sweep_plan
from via_antipad import circle_intersects_contour, contour_area, make_contour


def via_config():
    cfg = copy.deepcopy(ConfigManager(ROOT).default())
    cfg["scenario"]["type"] = "via_transition"
    cfg["via_transition"]["exit_layer"] = "L14"
    cfg["via_transition"]["backdrill"]["enabled"] = False
    cfg["via_transition"]["return_vias"]["enabled"] = False
    cfg["via_transition"]["return_vias"]["connect_layers"] = ["L2", "L13"]
    return cfg


class ViaAntipadShapeTests(unittest.TestCase):
    def test_legacy_aliases_migrate_losslessly_and_round_trip(self):
        raw = via_config()
        padstack = raw["via_transition"]["padstack"]
        padstack.pop("antipad", None)
        padstack["antipad_diameter"] = 0.72
        padstack["antipad_per_layer"] = {"L2": 0.81}
        normalized, messages = normalize_config(raw)
        antipad = normalized["via_transition"]["padstack"]["antipad"]
        self.assertEqual((antipad["topology"], antipad["shape"]), ("independent", "circle"))
        self.assertAlmostEqual(antipad["diameter"], 0.72)
        self.assertAlmostEqual(antipad["per_layer"]["L2"]["diameter"], 0.81)
        self.assertTrue(any("无损迁移" in message for message in messages))
        external = to_external_config(normalized)
        expected = convert_length(0.72, "mm", external["units"])
        self.assertAlmostEqual(
            external["via_transition"]["padstack"]["antipad_diameter"], expected
        )

    def test_four_shapes_have_auditable_bounds_area_and_rotation(self):
        cfg = via_config()
        cases = {
            "circle": {"diameter": 0.7},
            "ellipse": {"length": 0.9, "width": 0.55, "rotation_deg": 31.0},
            "capsule": {"length": 1.0, "width": 0.55, "rotation_deg": 45.0},
            "rounded_rectangle": {"length": 0.9, "width": 0.6,
                                  "corner_radius": 0.12, "rotation_deg": 27.0},
        }
        for shape, values in cases.items():
            with self.subTest(shape=shape):
                anti = cfg["via_transition"]["padstack"]["antipad"]
                anti.update({"topology": "independent", "shape": shape, **values})
                geometry = GeometryCalculator(cfg).calculate()
                self.assertFalse(geometry.errors, geometry.errors)
                self.assertGreater(len(geometry.antipad_contours), 0)
                contour = geometry.antipad_contours[0]
                self.assertGreater(contour.area, 0.0)
                self.assertAlmostEqual(contour.area, contour_area(anti), places=9)
                self.assertGreater(contour.bounds[1] - contour.bounds[0], 0.0)
                self.assertGreater(contour.bounds[3] - contour.bounds[2], 0.0)

    def test_independent_two_and_merged_one_per_layer(self):
        cfg = via_config()
        anti = cfg["via_transition"]["padstack"]["antipad"]
        anti.update({"topology": "independent", "shape": "circle", "diameter": 0.7})
        independent = GeometryCalculator(cfg).calculate()
        by_layer = [c for c in independent.antipad_contours if c.layer == "L2"]
        self.assertEqual(len(by_layer), 2)
        anti.update({"topology": "merged_pair", "shape": "capsule",
                     "length": 1.35, "width": 0.6, "rotation_deg": 90.0})
        merged = GeometryCalculator(cfg).calculate()
        by_layer = [c for c in merged.antipad_contours if c.layer == "L2"]
        self.assertEqual(len(by_layer), 1)
        self.assertEqual(by_layer[0].owner, "merged_pair")

    def test_per_layer_disable_inherit_and_override(self):
        cfg = via_config()
        anti = cfg["via_transition"]["padstack"]["antipad"]
        anti["per_layer"] = {
            "L2": {"enabled": True, "inherit_global": False, "topology": "merged_pair",
                   "shape": "capsule", "length": 1.4, "width": 0.65, "rotation_deg": 90.0},
            "L4": {"enabled": False, "inherit_global": True},
        }
        geometry = GeometryCalculator(cfg).calculate()
        self.assertEqual(len([c for c in geometry.antipad_contours if c.layer == "L2"]), 1)
        self.assertFalse([c for c in geometry.antipad_contours if c.layer == "L4"])

    def test_preview_and_hfss_consume_same_non_circle_points(self):
        cfg = via_config()
        anti = cfg["via_transition"]["padstack"]["antipad"]
        anti.update({"topology": "merged_pair", "shape": "rounded_rectangle",
                     "length": 1.35, "width": 0.65, "corner_radius": 0.12,
                     "rotation_deg": 90.0})
        geometry = GeometryCalculator(cfg).calculate()
        scene = build_model_scene(cfg, geometry)
        openings = [item for item in scene.objects if item.category == "VIA反焊盘"]
        self.assertEqual(len(openings), len(geometry.antipad_contours))
        self.assertEqual(openings[0].points_xy, geometry.antipad_contours[0].points)
        with tempfile.TemporaryDirectory() as directory:
            generated = HfssScriptGenerator(cfg, ROOT).generate(Path(directory))
            text = generated.script_path.read_text(encoding="utf-8")
            self.assertIn("def _create_signal_antipads", text)
            self.assertIn("create_polygon_solid_compat", text)
            self.assertIn("VIA_ANTIPAD_{}_{}_{}", text)
            completed = subprocess.run([sys.executable, str(generated.script_path), "--validate-only"],
                                       capture_output=True, text=True, timeout=90)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_small_merged_opening_fails_closed(self):
        cfg = via_config()
        cfg["via_transition"]["padstack"]["antipad"].update({
            "topology": "merged_pair", "shape": "rounded_rectangle",
            "length": 0.3, "width": 0.3, "corner_radius": 0.05, "size_mode": "manual",
        })
        report, _config, _geometry = ModelValidator(ConfigManager(ROOT)).run_local(cfg)
        self.assertTrue(any(item.rule_id == "VIA-TRN-090" for item in report.items), report.raw_log)

    def test_return_clearance_intersection_uses_true_non_circle_outline(self):
        spec = {"topology": "independent", "shape": "ellipse", "diameter": 0.6,
                "length": 1.0, "width": 0.4, "corner_radius": 0.0,
                "rotation_deg": 0.0}
        contour = make_contour("L2", "P", spec, (0.0, 0.0), "global")
        self.assertTrue(circle_intersects_contour(contour, (0.55, 0.0), 0.1))
        self.assertFalse(circle_intersects_contour(contour, (0.0, 0.35), 0.1))

    def test_per_layer_fields_must_match_selected_shape(self):
        cfg = via_config()
        cfg["via_transition"]["padstack"]["antipad"]["per_layer"] = {
            "L2": {"enabled": True, "inherit_global": False, "shape": "ellipse",
                   "length": 0.9, "width": 0.6, "diameter": 0.7}
        }
        report, _config, _geometry = ModelValidator(ConfigManager(ROOT)).run_local(cfg)
        self.assertTrue(any(item.rule_id == "VIA-TRN-089" for item in report.items), report.raw_log)

    def test_shape_specific_axes_are_native_and_wrong_shape_fails_closed(self):
        cfg = via_config()
        path = "via_transition.padstack.antipad.diameter"
        cfg["sweeps"][path] = {"enabled": True, "start": 0.55, "stop": 0.75, "step": 0.1}
        plan = inspect_sweep_plan(cfg)
        self.assertIn(path, dict(plan.native_axes))
        self.assertFalse([message for level, message in plan.issues if level == "error"], plan.issues)
        cfg["via_transition"]["padstack"]["antipad"]["shape"] = "capsule"
        cfg["via_transition"]["padstack"]["antipad"]["size_mode"] = "manual"
        cfg["sweeps"][path]["enabled"] = False
        length_path = "via_transition.padstack.antipad.length"
        cfg["sweeps"][length_path] = {"enabled": True, "start": 0.8, "stop": 1.0, "step": 0.1}
        plan = inspect_sweep_plan(cfg)
        self.assertIn(length_path, dict(plan.native_axes))
        self.assertFalse([message for level, message in plan.issues if level == "error"], plan.issues)
        cfg["via_transition"]["padstack"]["antipad"]["shape"] = "circle"
        plan = inspect_sweep_plan(cfg)
        self.assertTrue(any(level == "error" and "length/width" in message
                            for level, message in plan.issues), plan.issues)


if __name__ == "__main__":
    unittest.main(verbosity=2)
