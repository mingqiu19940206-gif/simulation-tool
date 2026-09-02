"""VIA 原生 Design Variables / Optimetrics 的离线生成门禁。"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config_manager import ConfigManager, format_length
from hfss_script_generator import ScriptGenerationError
from scenario_semantics import ScenarioNotImplementedError
from simulation_sweep_semantics import inspect_sweep_plan, preflight_sweep_geometry
from sweep_manager import generate_sweep_cases


def _via_config():
    cfg = copy.deepcopy(ConfigManager(ROOT).default())
    cfg["scenario"]["type"] = "via_transition"
    cfg["project"]["overwrite_existing"] = True
    return cfg


class ViaNativeOptimetricsTests(unittest.TestCase):
    def test_two_bound_axes_expand_to_offline_3x3_plan(self) -> None:
        cfg = _via_config()
        cfg["sweeps"]["via_transition.padstack.connected_pad_diameter"] = {
            "enabled": True, "start": 0.36, "stop": 0.44, "step": 0.04,
        }
        cfg["sweeps"]["via_transition.padstack.antipad_diameter"] = {
            "enabled": True, "start": 0.56, "stop": 0.64, "step": 0.04,
        }
        plan = inspect_sweep_plan(cfg)
        self.assertEqual(plan.case_count, 9)
        self.assertEqual(len(plan.native_axes), 2)
        self.assertEqual(plan.optimetrics_setup_count, 1)
        preflight = preflight_sweep_geometry(cfg)
        self.assertEqual(preflight.sampled_variations, 9)
        self.assertFalse(preflight.errors, preflight.errors)

    def test_generation_emits_one_project_bound_variables_and_parametric_setup(self) -> None:
        cfg = _via_config()
        cfg["sweeps"]["via_transition.padstack.connected_pad_diameter"] = {
            "enabled": True, "start": 0.36, "stop": 0.44, "step": 0.04,
        }
        cfg["sweeps"]["via_transition.padstack.antipad_diameter"] = {
            "enabled": True, "start": 0.56, "stop": 0.64, "step": 0.04,
        }
        with tempfile.TemporaryDirectory() as directory:
            result = generate_sweep_cases(cfg, Path(directory))
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            script = result.project.script_path.read_text(encoding="utf-8")
            self.assertEqual(manifest["project_count"], 1)
            self.assertEqual(manifest["design_count"], 1)
            self.assertEqual(manifest["optimetrics_setup_count"], 1)
            self.assertEqual(manifest["design_variation_count"], 9)
            self.assertEqual(len(result.variations), 9)
            self.assertIn('"via_signal_connected_pad_diameter"', script)
            self.assertIn('"via_signal_antipad_diameter"', script)
            self.assertIn('"(via_signal_connected_pad_diameter/2)"', script)
            self.assertIn('"(via_signal_antipad_diameter/2)"', script)
            self.assertIn('InsertSetup("OptiParametric"', script)
            self.assertIn('verify_native_optimetrics_geometry_binding(app, state)', script)
            completed = subprocess.run(
                [sys.executable, str(result.project.script_path), "--validate-only"],
                capture_output=True, text=True, timeout=90,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_drill_axis_keeps_finished_hole_derived_in_preflight_and_script(self) -> None:
        cfg = _via_config()
        cfg["sweeps"]["via_transition.padstack.drill_diameter"] = {
            "enabled": True, "start": 0.18, "stop": 0.22, "step": 0.02,
        }
        preflight = preflight_sweep_geometry(cfg)
        self.assertFalse(preflight.errors, preflight.errors)
        with tempfile.TemporaryDirectory() as directory:
            result = generate_sweep_cases(cfg, Path(directory))
            script = result.project.script_path.read_text(encoding="utf-8")
        self.assertIn(
            '"via_signal_finished_hole_diameter": "(via_signal_drill_diameter - 2*via_signal_plating_thickness)"',
            script,
        )
        self.assertIn('"(via_signal_drill_diameter/2)"', script)

    def test_non_circle_dimensions_and_rotation_are_native_and_bound(self) -> None:
        cfg = _via_config()
        cfg["via_transition"]["padstack"]["antipad"].update({
            "shape": "capsule", "length": 1.0, "width": 0.6,
            "rotation_deg": 90.0,
        })
        cfg["via_transition"]["return_vias"]["enabled"] = False
        for path, start, stop, step in (
            ("via_transition.padstack.antipad.length", 0.9, 1.1, 0.1),
            ("via_transition.padstack.antipad.width", 0.55, 0.65, 0.05),
            ("via_transition.padstack.antipad.rotation_deg", 80.0, 100.0, 10.0),
        ):
            cfg["sweeps"][path] = {"enabled": True, "start": start, "stop": stop, "step": step}
        plan = inspect_sweep_plan(cfg)
        self.assertEqual({path for path, _values in plan.native_axes}, {
            "via_transition.padstack.antipad.length",
            "via_transition.padstack.antipad.width",
            "via_transition.padstack.antipad.rotation_deg",
        })
        preflight = preflight_sweep_geometry(cfg)
        self.assertFalse(preflight.errors, preflight.errors)
        with tempfile.TemporaryDirectory() as directory:
            result = generate_sweep_cases(cfg, Path(directory))
            script = result.project.script_path.read_text(encoding="utf-8")
            self.assertIn('"via_signal_antipad_length"', script)
            self.assertIn('"via_signal_antipad_width"', script)
            self.assertIn('"via_signal_antipad_rotation_input"', script)
            self.assertIn("_parametric_via_antipad_points(contour)", script)
            completed = subprocess.run(
                [sys.executable, str(result.project.script_path), "--validate-only"],
                capture_output=True, text=True, timeout=90,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_unbound_safe_axis_remains_fail_closed(self) -> None:
        cfg = _via_config()
        cfg["sweeps"]["via_transition.pair_pitch"] = {
            "enabled": True, "start": 0.7, "stop": 0.9, "step": 0.1,
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ScenarioNotImplementedError) as ctx:
                generate_sweep_cases(cfg, Path(directory))
        self.assertIn("via_transition.pair_pitch", str(ctx.exception))

    def test_clearance_uses_recalculated_discrete_design_cases(self) -> None:
        cfg = _via_config()
        path = "via_transition.padstack.antipad.clearance"
        cfg["sweeps"][path] = {"enabled": True, "values": [0.05, 0.1]}
        plan = inspect_sweep_plan(cfg)
        self.assertEqual([item[0] for item in plan.topology_axes], [path])
        with tempfile.TemporaryDirectory() as directory:
            result = generate_sweep_cases(cfg, Path(directory))
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["backend"], "discrete_design_cases")
            self.assertTrue(manifest["execution_ready"])
            self.assertEqual(len(result.cases), 2)
            values = [json.loads(item.config_path.read_text(encoding="utf-8"))
                      ["via_transition"]["padstack"]["antipad"]["clearance"]
                      for item in result.cases]
            self.assertEqual(values, [0.05, 0.1])

    def test_trace_gap_uses_recalculated_cases_and_never_native_optimetrics(self) -> None:
        cfg = _via_config()
        path = "via_transition.entry_trace.gap"
        cfg["sweeps"][path] = {"enabled": True, "values": [0.5, 0.7]}
        plan = inspect_sweep_plan(cfg)
        self.assertEqual([item[0] for item in plan.topology_axes], [path])
        self.assertFalse(plan.native_axes)
        with tempfile.TemporaryDirectory() as directory:
            result = generate_sweep_cases(cfg, Path(directory))
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["backend"], "discrete_design_cases")
            values = [json.loads(item.config_path.read_text(encoding="utf-8"))
                      ["via_transition"]["entry_trace"]["gap"]
                      for item in result.cases]
            self.assertEqual(values, [0.5, 0.7])

    def test_per_layer_size_axis_is_registered_and_materialized(self) -> None:
        cfg = _via_config()
        cfg["via_transition"]["return_vias"]["enabled"] = False
        anti = cfg["via_transition"]["padstack"]["antipad"]
        anti["per_layer"] = {
            "L2": {"enabled": True, "inherit_global": False,
                   "topology": "independent", "shape": "circle",
                   "diameter": 0.6, "clearance": 0.1},
        }
        path = "via_transition.padstack.antipad.per_layer.L2.diameter"
        cfg["sweeps"][path] = {"enabled": True, "values": [0.55, 0.65]}
        plan = inspect_sweep_plan(cfg)
        self.assertEqual([item[0] for item in plan.topology_axes], [path])
        preflight = preflight_sweep_geometry(cfg)
        self.assertEqual(preflight.sampled_variations, 2)
        self.assertFalse(preflight.errors, preflight.errors)
        with tempfile.TemporaryDirectory() as directory:
            result = generate_sweep_cases(cfg, Path(directory))
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["backend"], "discrete_design_cases")
            values = [json.loads(item.config_path.read_text(encoding="utf-8"))
                      ["via_transition"]["padstack"]["antipad"]["per_layer"]["L2"]["diameter"]
                      for item in result.cases]
            self.assertEqual(values, [0.55, 0.65])

    def test_invalid_clearance_endpoint_blocks_all_discrete_generation(self) -> None:
        cfg = _via_config()
        cfg["via_transition"]["backdrill"]["enabled"] = False
        anti = cfg["via_transition"]["padstack"]["antipad"]
        anti.update({"topology": "merged_pair", "shape": "circle",
                     "diameter": 1.1, "clearance": 0.0, "size_mode": "manual"})
        path = "via_transition.padstack.antipad.clearance"
        cfg["sweeps"][path] = {"enabled": True, "values": [0.0, 0.1]}
        preflight = preflight_sweep_geometry(cfg)
        self.assertEqual(preflight.sampled_variations, 2)
        unit = cfg["units"]
        minimum = f"{format_length(1.2, unit, 9)} {unit}"
        clearance = f"{format_length(0.1, unit, 9)} {unit}"
        self.assertTrue(any(minimum in message and clearance in message
                            for message in preflight.errors), preflight.errors)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ScriptGenerationError):
                generate_sweep_cases(cfg, Path(directory))

    def test_via_native_axis_is_rejected_in_surface_scenario(self) -> None:
        cfg = copy.deepcopy(ConfigManager(ROOT).default())
        cfg["sweeps"]["via_transition.padstack.antipad_diameter"] = {
            "enabled": True, "start": 0.56, "stop": 0.64, "step": 0.04,
        }
        plan = inspect_sweep_plan(cfg)
        self.assertTrue(any("scenario.type" in message for level, message in plan.issues
                            if level == "error"), plan.issues)


if __name__ == "__main__":
    unittest.main(verbosity=2)
