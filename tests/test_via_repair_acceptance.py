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

from config_manager import ConfigManager, format_length, normalize_config
from geometry_calculator import GeometryCalculator
from hfss_script_generator import HfssScriptGenerator, ScriptGenerationError
from model_validator import ModelValidator


def config() -> dict:
    result = copy.deepcopy(ConfigManager(ROOT).default())
    result["scenario"]["type"] = "via_transition"
    result["via_transition"]["exit_layer"] = "L14"
    result["via_transition"]["backdrill"]["enabled"] = False
    result["via_transition"]["return_vias"]["connect_layers"] = ["L2", "L13"]
    return result


class ViaRepairAcceptanceTests(unittest.TestCase):
    def test_merged_circle_uses_real_clearance_and_generation_fails_closed(self) -> None:
        cfg = config()
        anti = cfg["via_transition"]["padstack"]["antipad"]
        anti.update({"topology": "merged_pair", "shape": "circle",
                     "diameter": 0.6, "clearance": 0.1, "size_mode": "manual"})
        report, _normalized, _geometry = ModelValidator(ConfigManager(ROOT)).run_local(cfg)
        issue = next(item for item in report.items if item.rule_id == "VIA-TRN-090")
        unit = cfg["units"]
        current = f"{format_length(0.6, unit, 9)} {unit}"
        minimum = f"{format_length(1.2, unit, 9)} {unit}"
        self.assertIn(current, issue.message)
        self.assertIn(minimum, issue.message)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ScriptGenerationError):
                HfssScriptGenerator(cfg, ROOT).generate(Path(directory))

    def test_backdrill_layer_uses_backdrill_diameter_for_minimum(self) -> None:
        cfg = copy.deepcopy(ConfigManager(ROOT).default())
        cfg["scenario"]["type"] = "via_transition"
        anti = cfg["via_transition"]["padstack"]["antipad"]
        anti.update({"topology": "merged_pair", "shape": "circle",
                     "diameter": 1.25, "clearance": 0.1, "size_mode": "manual"})
        report, _normalized, _geometry = ModelValidator(ConfigManager(ROOT)).run_local(cfg)
        messages = [item.message for item in report.items if item.rule_id == "VIA-TRN-090"]
        minimum = f"{format_length(1.3, cfg['units'], 9)} {cfg['units']}"
        self.assertTrue(any("L13" in message and minimum in message
                            for message in messages), messages)

    def test_route_axes_are_one_ninety_degree_transform(self) -> None:
        cfg_x = config()
        cfg_x["via_transition"]["return_vias"].update(
            {"enabled": True, "pattern": "side_pair", "count": 4})
        cfg_y = copy.deepcopy(cfg_x)
        cfg_y["via_transition"]["route_axis"] = "y"
        gx = GeometryCalculator(cfg_x).calculate()
        gy = GeometryCalculator(cfg_y).calculate()

        def rotate(point: tuple[float, float]) -> tuple[float, float]:
            return -point[1], point[0]

        for vx, vy in zip(gx.signal_vias, gy.signal_vias):
            self.assertAlmostEqual(vy.x, rotate((vx.x, vx.y))[0])
            self.assertAlmostEqual(vy.y, rotate((vx.x, vx.y))[1])
        for vx, vy in zip(gx.return_vias, gy.return_vias):
            self.assertAlmostEqual(vy.x, rotate((vx.x, vx.y))[0])
            self.assertAlmostEqual(vy.y, rotate((vx.x, vx.y))[1])
        for tx, ty in zip(gx.entry_traces + gx.exit_traces,
                          gy.entry_traces + gy.exit_traces):
            expected = [rotate(tuple(point)) for point in tx["points"]]
            for actual, target in zip(ty["points"], expected):
                self.assertAlmostEqual(actual[0], target[0])
                self.assertAlmostEqual(actual[1], target[1])
        self.assertTrue(all(port.orientation == "XZ" for port in gy.ports))
        self.assertTrue(all(port.propagation_direction in {"+Y", "-Y"} for port in gy.ports))

    def test_route_y_hfss_script_uses_xz_ports_and_validates_offline(self) -> None:
        cfg = config()
        cfg["via_transition"]["route_axis"] = "y"
        with tempfile.TemporaryDirectory() as directory:
            generated = HfssScriptGenerator(cfg, ROOT).generate(Path(directory))
            script = generated.script_path.read_text(encoding="utf-8")
            self.assertIn('get("orientation", "YZ")) == "XZ"', script)
            completed = subprocess.run(
                [sys.executable, str(generated.script_path), "--validate-only"],
                capture_output=True, text=True, timeout=90,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_return_via_pattern_counts_and_finished_hole_are_canonical(self) -> None:
        raw = config()
        rv = raw["via_transition"]["return_vias"]
        rv.update({"layout_mode": "advanced", "pattern": "four_corner", "count": 99,
                   "finished_hole_diameter": 0.19})
        normalized, messages = normalize_config(raw)
        self.assertEqual(normalized["via_transition"]["return_vias"]["count"], 4)
        self.assertAlmostEqual(normalized["via_transition"]["return_vias"]["finished_hole_diameter"], 0.15)
        self.assertTrue(any("Return VIA" in message and "成品孔" in message for message in messages))
        self.assertEqual(len(GeometryCalculator(normalized).calculate().return_vias), 4)

        side = config()
        side["via_transition"]["return_vias"].update(
            {"enabled": True, "layout_mode": "advanced",
             "pattern": "side_pair", "count": 6})
        self.assertEqual(len(GeometryCalculator(side).calculate().return_vias), 6)
        side["via_transition"]["return_vias"]["count"] = 3
        report, _cfg, _geometry = ModelValidator(ConfigManager(ROOT)).run_local(side)
        self.assertTrue(any(item.rule_id == "VIA-TRN-024" for item in report.items))

        custom = config()
        custom["via_transition"]["return_vias"].update({
            "layout_mode": "advanced", "pattern": "custom", "count": 20,
            "custom_coordinates": [[-1.0, 0.0], [0.0, 1.0], [1.0, 0.0]],
        })
        normalized, _messages = normalize_config(custom)
        self.assertEqual(normalized["via_transition"]["return_vias"]["count"], 3)
        self.assertEqual(len(GeometryCalculator(normalized).calculate().return_vias), 3)

    def test_internal_signal_layer_without_plane_is_explicit_na(self) -> None:
        geometry = GeometryCalculator(config()).calculate()
        self.assertEqual(geometry.signal_vias[0].per_layer_states["L3"], "no_plane_copper")
        self.assertFalse([contour for contour in geometry.antipad_contours
                          if contour.layer == "L3"])
        self.assertTrue([contour for contour in geometry.antipad_contours
                         if contour.layer == "L2"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
