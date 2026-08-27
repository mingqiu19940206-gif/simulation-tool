from __future__ import annotations

import copy
import math
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from config_manager import ConfigManager, MM_PER_MIL, format_length, normalize_config
from geometry_calculator import GeometryCalculator
from hfss_script_generator import HfssScriptGenerator, ScriptGenerationError
from model_validator import ModelValidator
from simulation_sweep_semantics import preflight_sweep_geometry
from via_antipad import build_layer_contours, contains_circle, pn_axis_angle


def via_config() -> dict:
    cfg = copy.deepcopy(ConfigManager(ROOT).default())
    cfg["scenario"]["type"] = "via_transition"
    return cfg


def selected_contour(cfg: dict, layer: str = "L2"):
    geometry = GeometryCalculator(cfg).calculate()
    return geometry, next(item for item in geometry.antipad_contours if item.layer == layer)


class MergedAntipadAutofitTests(unittest.TestCase):
    def test_vertical_pair_auto_axis_is_vertical(self) -> None:
        cfg = via_config()
        cfg["via_transition"]["padstack"]["antipad"].update(
            topology="merged_pair", shape="capsule", direction_mode="auto_pn", size_mode="auto_fit")
        _geometry, contour = selected_contour(cfg)
        self.assertAlmostEqual(abs(contour.rotation_deg), 90.0, places=9)

    def test_horizontal_pair_auto_axis_is_horizontal(self) -> None:
        cfg = via_config()
        cfg["via_transition"]["route_axis"] = "y"
        cfg["via_transition"]["padstack"]["antipad"].update(
            topology="merged_pair", shape="capsule", direction_mode="auto_pn", size_mode="auto_fit")
        geometry, contour = selected_contour(cfg)
        p, n = geometry.signal_vias
        self.assertAlmostEqual(math.sin(math.radians(contour.rotation_deg)), 0.0, places=9)
        self.assertAlmostEqual(contour.rotation_deg % 180.0, pn_axis_angle((p.x, p.y), (n.x, n.y)) % 180.0, places=9)

    def test_arbitrary_pair_axis_uses_atan2(self) -> None:
        p, n = (0.2, -0.1), (1.1, 0.7)
        contours = build_layer_contours({
            "topology": "merged_pair", "shape": "capsule", "direction_mode": "auto_pn",
            "size_mode": "auto_fit", "length": 0.1, "width": 0.1, "diameter": 0.1,
            "corner_radius": 0.0, "rotation_deg": 0.0, "clearance": 0.1,
            "end_clearance": 0.1, "side_clearance": 0.1,
        }, "L2", p, n, 0.2)
        self.assertAlmostEqual(contours[0].rotation_deg, pn_axis_angle(p, n), places=9)

    def test_screenshot_31_4961_by_23_622_mil_is_illegal(self) -> None:
        cfg = via_config()
        anti = cfg["via_transition"]["padstack"]["antipad"]
        anti.update(topology="merged_pair", shape="capsule", direction_mode="manual",
                    size_mode="manual", rotation_deg=0.0, length=0.8, width=0.6,
                    clearance=0.2, end_clearance=0.2, side_clearance=0.2)
        report, _normalized, _geometry = ModelValidator(ConfigManager(ROOT)).run_local(cfg)
        messages = [item.message for item in report.items if item.rule_id == "VIA-TRN-090"]
        self.assertTrue(messages)
        unit = cfg["units"]
        required = f"{format_length(1.4, unit, 9)} {unit}"
        current = f"{format_length(0.8, unit, 9)} {unit}"
        self.assertTrue(any(required in message and current in message
                            for message in messages), messages)
        self.assertAlmostEqual(0.8 / MM_PER_MIL, 31.49606299, places=5)
        self.assertAlmostEqual(0.6 / MM_PER_MIL, 23.62204724, places=5)

    def test_autofit_capsule_contains_both_required_circles(self) -> None:
        cfg = via_config()
        anti = cfg["via_transition"]["padstack"]["antipad"]
        anti.update(topology="merged_pair", shape="capsule", direction_mode="auto_pn",
                    size_mode="auto_fit", clearance=0.15, end_clearance=0.15, side_clearance=0.15)
        geometry, contour = selected_contour(cfg)
        radius = contour.effective_hole_diameter / 2.0 + contour.clearance
        self.assertTrue(all(contains_circle(contour, (via.x, via.y), radius)
                            for via in geometry.signal_vias))
        self.assertAlmostEqual(contour.length, 0.8 + contour.width, places=9)

    def test_backdrill_layer_uses_larger_effective_hole(self) -> None:
        cfg = via_config()
        cfg["via_transition"]["padstack"]["antipad"].update(
            topology="merged_pair", shape="capsule", direction_mode="auto_pn", size_mode="auto_fit")
        geometry = GeometryCalculator(cfg).calculate()
        l2 = next(item for item in geometry.antipad_contours if item.layer == "L2")
        l13 = next(item for item in geometry.antipad_contours if item.layer == "L13")
        self.assertAlmostEqual(l2.effective_hole_diameter, 0.2)
        self.assertAlmostEqual(l13.effective_hole_diameter, 0.3)
        self.assertGreater(l13.width, l2.width)
        self.assertGreater(l13.length, l2.length)

    def test_manual_wrong_rotation_fails_closed(self) -> None:
        cfg = via_config()
        cfg["via_transition"]["padstack"]["antipad"].update(
            topology="merged_pair", shape="capsule", direction_mode="manual", size_mode="manual",
            rotation_deg=0.0, length=1.4, width=0.4)
        report, _cfg, _geometry = ModelValidator(ConfigManager(ROOT)).run_local(cfg)
        self.assertTrue(any(item.rule_id == "VIA-TRN-090" for item in report.items))

    def test_invalid_variation_blocks_scan_preflight(self) -> None:
        cfg = via_config()
        cfg["via_transition"]["padstack"]["antipad"].update(
            topology="merged_pair", shape="capsule", direction_mode="auto_pn", size_mode="manual",
            length=1.2, width=0.4)
        cfg["sweeps"]["via_transition.padstack.antipad.length"] = {
            "enabled": True, "values": [1.2, 0.8], "scan_mode": "Explicit Values"}
        preflight = preflight_sweep_geometry(cfg)
        self.assertTrue(preflight.errors)
        self.assertTrue(any("未完整包含" in error or "长度至少需要" in error for error in preflight.errors))

    def test_invalid_geometry_cannot_generate_hfss(self) -> None:
        cfg = via_config()
        cfg["via_transition"]["padstack"]["antipad"].update(
            topology="merged_pair", shape="capsule", direction_mode="manual", size_mode="manual",
            rotation_deg=0.0, length=0.8, width=0.6)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ScriptGenerationError):
                HfssScriptGenerator(cfg, ROOT).generate(Path(directory))

    def test_old_config_migrates_to_auto_direction_and_preserves_manual_angle(self) -> None:
        raw = via_config()
        anti = raw["via_transition"]["padstack"]["antipad"]
        anti.pop("direction_mode", None)
        anti["rotation_deg"] = 37.0
        normalized, messages = normalize_config(raw)
        migrated = normalized["via_transition"]["padstack"]["antipad"]
        self.assertEqual(migrated["direction_mode"], "auto_pn")
        self.assertEqual(migrated["rotation_deg"], 37.0)
        self.assertTrue(any("direction_mode" in message and "auto_pn" in message for message in messages))

    def test_modes_per_layer_and_physical_units_round_trip(self) -> None:
        raw = via_config()
        anti = raw["via_transition"]["padstack"]["antipad"]
        anti.update(direction_mode="manual", size_mode="auto_plus_increment",
                    rotation_deg=23.0, length_increment=0.12, width_increment=0.07)
        anti["per_layer"]["L11"] = {
            "enabled": True, "inherit_global": False, "topology": "merged_pair",
            "shape": "capsule", "direction_mode": "auto_pn", "size_mode": "auto_fit",
            "length": 1.5, "width": 0.55, "rotation_deg": 91.0,
            "clearance": 0.11, "end_clearance": 0.13, "side_clearance": 0.09,
        }
        normalized, _ = normalize_config(raw)
        saved = copy.deepcopy(normalized)
        reloaded, _ = normalize_config(saved)
        self.assertEqual(reloaded["via_transition"]["padstack"]["antipad"],
                         normalized["via_transition"]["padstack"]["antipad"])
        mil = copy.deepcopy(raw)
        mil["units"] = "mil"
        mil.pop("_internal_units", None)
        for key in ("length_increment", "width_increment"):
            mil["via_transition"]["padstack"]["antipad"][key] /= MM_PER_MIL
        converted, _ = normalize_config(mil)
        self.assertAlmostEqual(converted["via_transition"]["padstack"]["antipad"]["length_increment"], 0.12)
        self.assertAlmostEqual(converted["via_transition"]["padstack"]["antipad"]["width_increment"], 0.07)


if __name__ == "__main__":
    unittest.main(verbosity=2)
