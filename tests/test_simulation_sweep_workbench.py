from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config_manager import ConfigManager, normalize_config, validate_config
from simulation_sweep_semantics import (
    SWEEP_PARAMETER_REGISTRY,
    apply_frequency_preset,
    inspect_frequency_plan,
    inspect_sweep_plan,
    preset_is_modified,
)
from sweep_manager import expand_sweep_cases, generate_sweep_cases, summarize_sweep_results
from hfss_script_generator import HfssScriptGenerator


class SimulationSweepSemanticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = ConfigManager(ROOT).default()
        self.config["simulation"]["enabled"] = False
        self.config["gnd_vias"]["enabled"] = False

    def test_old_v2_json_receives_backward_compatible_optional_defaults(self) -> None:
        old = copy.deepcopy(self.config)
        for key in ("sweep_name", "max_delta_s", "automatically_use_causal_materials", "interpolating_tolerance",
                    "interpolating_max_solutions", "save_fields", "report_outputs"):
            old["simulation"].pop(key, None)
        old.pop("sweep_execution", None)
        normalized, _ = normalize_config(old)
        self.assertEqual(normalized["simulation"]["sweep_name"], "Sweep1")
        self.assertTrue(normalized["simulation"]["automatically_use_causal_materials"])
        self.assertEqual(normalized["simulation"]["report_outputs"]["Sdd21"], True)
        self.assertEqual(normalized["sweep_execution"]["max_cases"], 200)

    def test_frequency_preset_is_explicit_and_modified_state_is_detected(self) -> None:
        applied, changes = apply_frequency_preset(self.config, "Gen6")
        self.assertEqual(applied["simulation"]["stop_frequency_ghz"], 128.0)
        self.assertIn("stop_frequency_ghz", changes)
        self.assertFalse(preset_is_modified(applied["simulation"]))
        applied["simulation"]["stop_frequency_ghz"] = 100.0
        self.assertTrue(preset_is_modified(applied["simulation"]))

    def test_discrete_and_interpolating_frequency_semantics_are_separate(self) -> None:
        sim = copy.deepcopy(self.config["simulation"])
        sim.update(enabled=True, sweep_type="Discrete", start_frequency_ghz=0.01,
                   stop_frequency_ghz=0.12, step_frequency_ghz=0.05,
                   adaptive_frequency_ghz=0.06)
        plan = inspect_frequency_plan(sim)
        self.assertEqual(plan.point_count, 3)
        self.assertAlmostEqual(plan.last_frequency_ghz or 0, 0.11)
        self.assertTrue(any(level == "warning" for level, _ in plan.issues))
        sim.update(sweep_type="Interpolating", step_frequency_ghz=0,
                   interpolating_tolerance=0.5, interpolating_max_solutions=80)
        plan = inspect_frequency_plan(sim)
        self.assertIsNone(plan.point_count)
        self.assertFalse(any("步进" in message for _, message in plan.issues))

    def test_frequency_validation_checks_adaptive_frequency(self) -> None:
        self.config["simulation"].update(enabled=True, adaptive_frequency_ghz=40.0)
        _, validation = validate_config(self.config, ROOT, check_output_directory=False)
        self.assertTrue(any("自适应频率" in message for message in validation.errors))

    def test_automatic_mixed_mode_reports_require_terminal_solution(self) -> None:
        self.config["simulation"]["enabled"] = True
        self.config["project"]["solution_type"] = "Modal"
        _, validation = validate_config(self.config, ROOT, check_output_directory=False)
        self.assertTrue(any("Terminal" in message for message in validation.errors))

    def test_generated_script_has_four_terminal_ports_and_distinct_sweep_modes(self) -> None:
        self.config["simulation"]["enabled"] = True
        self.config["project"]["overwrite_existing"] = True
        with tempfile.TemporaryDirectory() as directory:
            result = HfssScriptGenerator(self.config, ROOT).generate(Path(directory))
            script = result.script_path.read_text(encoding="utf-8")
            for name in ("PORT_IN_P", "PORT_IN_N", "PORT_OUT_P", "PORT_OUT_N"):
                self.assertIn(name, script)
            self.assertIn('common_mode="OUT_COMM", differential_mode="OUT_DIFF"', script)
            self.assertIn('if sweep_type == "Discrete"', script)
            self.assertIn('sweep.props["InterpTolerance"]', script)
            self.assertIn('export_report_to_file(str(SCRIPT_DIRECTORY), name, ".csv")', script)

    def test_registry_dependency_and_package_apply_are_used_by_case_expansion(self) -> None:
        for sweep in self.config["sweeps"].values():
            sweep["enabled"] = False
        self.config["sweeps"]["capacitor.package"] = {"enabled": True, "values": ["0201", "0402"]}
        cases = expand_sweep_cases(self.config)
        self.assertEqual([case[0]["capacitor"]["body_length"] for case in cases], [0.6, 1.0])
        self.config["sweeps"]["capacitor.package"]["enabled"] = False
        self.config["sweeps"]["gnd_vias.clearance_x"]["enabled"] = True
        plan = inspect_sweep_plan(self.config)
        self.assertTrue(any("启用 GND VIA" in message for _, message in plan.issues))
        self.assertIn("gnd_vias.antipad_diameter", SWEEP_PARAMETER_REGISTRY)

    def test_manifest_truthfully_describes_native_variations_and_summary(self) -> None:
        for sweep in self.config["sweeps"].values():
            sweep["enabled"] = False
        with tempfile.TemporaryDirectory() as directory:
            result = generate_sweep_cases(self.config, Path(directory))
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertFalse(manifest["simulation_enabled"])
            self.assertEqual(manifest["metrics"], [])
            self.assertEqual(manifest["variations"][0]["status"], "generated")
            self.assertNotIn("cases", manifest)
            summary = summarize_sweep_results(result.manifest_path)
            payload = json.loads(summary.read_text(encoding="utf-8"))
            self.assertEqual(payload["counts"]["generated"], 1)


class SimulationSweepGuiTests(unittest.TestCase):
    def test_minimum_window_and_auto_simulation_state_linkage(self) -> None:
        from main_gui import SurfaceCapAntipadApp
        app = SurfaceCapAntipadApp()
        try:
            app.geometry("1120x720")
            app.update()
            workbench = app.simulation_sweep_workbench
            app.notebook.select(app.tabs["仿真与扫描"])
            app.variables["simulation.enabled"].set(False)
            config, validation = app.manager.validate(app._collect(), check_output_directory=False)
            workbench.show(config, None, validation)
            self.assertEqual(str(workbench.setup_name.cget("state")), "disabled")
            self.assertIn("仅生成几何模式", workbench.auto_mode_var.get())
            app.variables["simulation.enabled"].set(True)
            app.update()
            config, validation = app.manager.validate(app._collect(), check_output_directory=False)
            workbench.show(config, None, validation)
            self.assertNotEqual(str(workbench.setup_name.cget("state")), "disabled")
            self.assertEqual(str(workbench.step_frequency.cget("state")), "disabled")
            self.assertTrue(workbench.steps.winfo_viewable())
        finally:
            app.destroy()

    def test_selected_variation_backfills_canonical_via_antipad_value(self) -> None:
        from main_gui import SurfaceCapAntipadApp
        app = SurfaceCapAntipadApp()
        try:
            app.variables["scenario.type"].set("VIA 换层")
            app.update()
            workbench = app.simulation_sweep_workbench
            iid = "backfill-via-antipad"
            workbench._variation_by_iid = {
                iid: {"via_transition.padstack.antipad.diameter": 0.73}
            }
            workbench.case_tree.insert("", "end", iid=iid,
                                       values=("Variation", "0.73", "同一Design", "关闭", "无", "passed"))
            workbench.case_tree.selection_set(iid)
            workbench.apply_selected_variation()
            collected = app._collect()
            self.assertAlmostEqual(
                collected["via_transition"]["padstack"]["antipad"]["diameter"], 0.73)
            self.assertAlmostEqual(
                collected["via_transition"]["padstack"]["antipad_diameter"], 0.73)
            self.assertIn("不会伪造", workbench.execution_status_var.get())
        finally:
            app.destroy()

    def test_per_layer_via_axes_appear_as_discrete_case_parameters(self) -> None:
        from main_gui import SurfaceCapAntipadApp
        app = SurfaceCapAntipadApp()
        try:
            app.variables["scenario.type"].set("VIA 换层")
            app.update()
            config = app._collect()
            config["via_transition"]["padstack"]["antipad"]["per_layer"] = {
                "L2": {"enabled": True, "inherit_global": False,
                       "topology": "independent", "shape": "circle",
                       "diameter": 0.6, "clearance": 0.1},
            }
            workbench = app.simulation_sweep_workbench
            workbench.load_config(config)
            labels = tuple(workbench.parameter_combo.cget("values"))
            self.assertIn("L2 本层反焊盘直径", labels)
            self.assertIn("L2 本层反焊盘最小铜边净距", labels)
        finally:
            app.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
