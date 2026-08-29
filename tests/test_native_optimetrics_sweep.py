from __future__ import annotations

import copy
import json
import math
import sys
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config_manager import ConfigManager, normalize_config, to_external_config
from geometry_calculator import GeometryCalculator
from simulation_sweep_semantics import (
    aedt_sweep_data,
    inspect_sweep_plan,
    iter_variations,
    preflight_sweep_geometry,
)
from sweep_manager import generate_sweep_cases


class NativeOptimetricsSweepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = ConfigManager(ROOT).default()
        self.config["project"]["overwrite_existing"] = True

    def test_cap_pitch_is_one_native_axis_and_three_variations(self) -> None:
        self.config["sweeps"]["capacitor.cap_pitch"].update(
            enabled=True, start=0.5, stop=0.7, step=0.1
        )
        plan = inspect_sweep_plan(self.config)
        self.assertEqual(plan.strategy, "hybrid")
        self.assertEqual(plan.native_axes[0][0], "capacitor.cap_pitch")
        self.assertEqual(plan.case_count, 3)
        self.assertEqual(plan.design_count, 1)
        self.assertEqual(plan.optimetrics_setup_count, 1)
        self.assertFalse(plan.topology_axes)

    def test_native_generation_writes_one_project_and_one_script(self) -> None:
        self.config["sweeps"]["capacitor.cap_pitch"].update(
            enabled=True, start=0.5, stop=0.7, step=0.1
        )
        with tempfile.TemporaryDirectory() as directory:
            result = generate_sweep_cases(self.config, Path(directory))
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["project_count"], 1)
            self.assertEqual(manifest["design_count"], 1)
            self.assertEqual(manifest["optimetrics_setup_count"], 1)
            self.assertEqual(manifest["design_variation_count"], 3)
            self.assertEqual(len(result.cases), 1)
            self.assertEqual(len(result.variations), 3)
            self.assertNotIn("cases", manifest)
            self.assertNotIn("_CASE_", json.dumps(manifest))
            script = result.project.script_path.read_text(encoding="utf-8")
            self.assertIn('InsertSetup("OptiParametric"', script)
            self.assertIn("cap_pitch/2", script)
            self.assertIn("bridge_length", script)
            self.assertIn("gnd_via_finished_hole_diameter", script)

    def test_native_optimetrics_save_fields_follows_simulation_config(self) -> None:
        self.config["simulation"]["save_fields"] = True
        self.config["sweeps"]["capacitor.cap_pitch"].update(
            enabled=True, start=0.5, stop=0.7, step=0.1
        )
        with tempfile.TemporaryDirectory() as directory:
            result = generate_sweep_cases(self.config, Path(directory))
            script = result.project.script_path.read_text(encoding="utf-8")
        self.assertIn(
            'save_fields = bool(CONFIG.get("simulation", {}).get("save_fields", False))',
            script,
        )
        self.assertIn(
            '["NAME:ProdOptiSetupDataV2", "SaveFields:=", save_fields,',
            script,
        )
        self.assertNotIn(
            '["NAME:ProdOptiSetupDataV2", "SaveFields:=", False,',
            script,
        )

    def test_global_antipad_sweep_parameterizes_merged_all_geometry(self) -> None:
        self.config["antipad"]["mode"] = "merged_all"
        self.config["sweeps"]["antipad.global_expansion"].update(
            enabled=True, start=0.1, stop=0.4, step=0.05
        )
        with tempfile.TemporaryDirectory() as directory:
            result = generate_sweep_cases(self.config, Path(directory))
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(len(result.cases), 1)
            self.assertEqual(len(result.variations), 7)
            self.assertEqual(manifest["backend"], "native_optimetrics")
            self.assertEqual(manifest["project_count"], 1)
            self.assertEqual(manifest["design_count"], 1)
            self.assertEqual(manifest["optimetrics_setup_count"], 1)
            self.assertEqual(manifest["design_variation_count"], 7)
            script = result.project.script_path.read_text(encoding="utf-8")
            self.assertIn('if mode == "merged_all"', script)
            self.assertIn("antipad_expansion_variable(layer_name)", script)
            self.assertIn('"antipad_expansion_global"', script)
            self.assertIn("input_straight_length - plane_margin_input)", script)
            self.assertIn("output_straight_length + plane_margin_output)", script)
            self.assertNotIn("input_straight_length - geometry_overlap - plane_margin_input", script)
            self.assertNotIn("output_straight_length + geometry_overlap + plane_margin_output", script)
            self.assertNotIn("input_straight_length - trace_width/2 - plane_margin_input", script)
            self.assertNotIn("output_straight_length + trace_width/2 + plane_margin_output", script)
            self.assertIn("verify_native_optimetrics_geometry_binding(app, state)", script)
            self.assertNotIn("compatibility_cases", script)

    def test_mil_lumped_rlc_antipad_sweep_uses_nonzero_numeric_current_line(self) -> None:
        config, _ = normalize_config(to_external_config(self.config, "mil"))
        config["capacitor_model"]["mode"] = "lumped_rlc"
        config["antipad"]["mode"] = "merged_all"
        config["sweeps"]["antipad.global_expansion"].update(
            enabled=True, start=0.1, stop=0.4, step=0.05
        )
        with tempfile.TemporaryDirectory() as directory:
            result = generate_sweep_cases(config, Path(directory))
            script = result.project.script_path.read_text(encoding="utf-8")
        self.assertIn(
            "[model_length_value(value) for value in line_start]",
            script,
        )
        self.assertIn(
            '"Start": [length(value) for value in line_start]',
            script,
        )
        self.assertNotIn(
            '"Start": [center_expression[0], _plus(center_expression[1], "bridge_width/2")',
            script,
        )
        self.assertIn("directed_line_length <= 1.0e-12", script)
        self.assertIn('shape["metadata"]["integration_line_start"]', script)

    def test_first_reference_sweep_makes_selected_layer_variable_independent(self) -> None:
        self.config["sweeps"]["antipad.first_reference_expansion"].update(
            enabled=True, start=0.1, stop=0.4, step=0.05
        )
        with tempfile.TemporaryDirectory() as directory:
            result = generate_sweep_cases(self.config, Path(directory))
            script = result.project.script_path.read_text(encoding="utf-8")
        self.assertIn("first_reference_sweep_enabled = bool(", script)
        self.assertIn(
            'if first_reference_sweep_enabled and str(layer["name"]) == first_reference_name:',
            script,
        )
        self.assertIn('variables[prefix] = length(nominal)', script)
        self.assertIn('"sweep_variable_independent": all(', script)

    def test_topology_axis_is_not_native_optimetrics(self) -> None:
        self.config["gnd_vias"]["pattern"] = "side_pair"
        self.config["gnd_vias"]["count"] = 4
        self.config["sweeps"]["gnd_vias.count"] = {
            "enabled": True, "values": [2, 4]
        }
        plan = inspect_sweep_plan(self.config)
        self.assertEqual(plan.design_count, 2)
        self.assertFalse(plan.native_axes)
        self.assertEqual(plan.topology_axes[0][0], "gnd_vias.count")
        self.assertTrue(any("拓扑" in message for _, message in plan.issues))

    def test_topology_generation_materializes_recalculated_discrete_cases(self) -> None:
        self.config["gnd_vias"]["enabled"] = True
        self.config["gnd_vias"]["pattern"] = "side_pair"
        self.config["sweeps"]["gnd_vias.count"] = {"enabled": True, "values": [2, 4]}
        with tempfile.TemporaryDirectory() as directory:
            result = generate_sweep_cases(self.config, Path(directory))
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertTrue(manifest["execution_ready"])
            self.assertEqual(manifest["backend"], "discrete_design_cases")
            self.assertEqual(manifest["project_count"], 2)
            self.assertEqual(manifest["design_count"], 2)
            self.assertEqual(len(result.cases), 2)
            self.assertEqual([item["status"] for item in manifest["cases"]],
                             ["generated", "generated"])
            self.assertEqual([item["variation"]["gnd_vias.count"]
                              for item in manifest["cases"]], [2, 4])

    def test_scan_modes_render_native_aedt_data(self) -> None:
        spec = {"enabled": True, "start": 0.5, "stop": 0.7, "step": 0.1, "scan_mode": "LinearStep"}
        self.assertEqual(aedt_sweep_data("capacitor.cap_pitch", spec), "LIN 0.5mm 0.7mm 0.1mm")
        count = {"enabled": True, "start": 0.5, "stop": 0.7, "count": 3, "scan_mode": "LinearCount"}
        self.assertEqual(aedt_sweep_data("capacitor.cap_pitch", count), "LINC 0.5mm 0.7mm 3")
        explicit = {"enabled": True, "values": [0.5, 0.6, 0.7], "scan_mode": "Explicit Values"}
        self.assertEqual(aedt_sweep_data("capacitor.cap_pitch", explicit), "0.5mm, 0.6mm, 0.7mm")

    def test_range_preflight_accepts_equal_pitch_without_degenerate_geometry(self) -> None:
        self.config["sweeps"]["capacitor.cap_pitch"].update(
            enabled=True, start=0.1, stop=0.3, step=0.1
        )
        self.config["capacitor"]["pad_width"] = 0.06
        self.config["capacitor"]["bridge_width"] = 0.04
        self.config["trace"]["trace_width"] = 0.04
        # Equality is now a legal collinear route with a real midpoint; it
        # must not be rejected or represented by an epsilon-length segment.
        report = preflight_sweep_geometry(self.config)
        self.assertFalse(report.errors, report.errors)
        equal = copy.deepcopy(self.config)
        equal["capacitor"]["cap_pitch"] = equal["trace"]["trace_pitch"]
        geometry = GeometryCalculator(equal).calculate()
        for sides in geometry.trace_centerline_paths.values():
            for path in sides.values():
                self.assertTrue(all(math.dist((a.x, a.y), (b.x, b.y)) > 1e-9
                                    for a, b in zip(path, path[1:])))

    def test_variation_combination_modes_have_distinct_counts(self) -> None:
        self.config["sweeps"]["capacitor.cap_pitch"].update(
            enabled=True, values=[0.5, 0.6, 0.7]
        )
        self.config["sweeps"]["antipad.global_expansion"].update(
            enabled=True, values=[0.1, 0.2]
        )
        plan = inspect_sweep_plan(self.config)
        self.assertEqual(len(iter_variations(plan, self.config)), 6)
        self.config["sweep_execution"]["variation_mode"] = "synchronous"
        self.config["sweeps"]["antipad.global_expansion"]["values"] = [0.1, 0.15, 0.2]
        plan = inspect_sweep_plan(self.config)
        self.assertEqual(len(iter_variations(plan, self.config)), 3)
        self.config["sweep_execution"]["variation_mode"] = "one_at_a_time"
        self.config["sweeps"]["antipad.global_expansion"]["values"] = [0.1, 0.2]
        plan = inspect_sweep_plan(self.config)
        self.assertEqual(len(iter_variations(plan, self.config)), 4)
        self.config["sweep_execution"]["variation_mode"] = "custom"
        self.config["sweep_execution"]["custom_variations"] = [
            {"capacitor.cap_pitch": 0.5, "antipad.global_expansion": 0.1},
            {"capacitor.cap_pitch": 0.7, "antipad.global_expansion": 0.2},
        ]
        plan = inspect_sweep_plan(self.config)
        self.assertEqual(len(iter_variations(plan, self.config)), 2)

    def test_old_backend_strategy_migrates_without_changing_explicit_legacy_mode(self) -> None:
        old = self.config.copy()
        old["sweep_execution"] = {"strategy": "full_factorial", "max_cases": 20, "parallel_jobs": 1}
        normalized, _ = normalize_config(old)
        self.assertEqual(normalized["sweep_execution"]["strategy"], "hybrid")
        normalized["sweep_execution"]["strategy"] = "legacy_discrete"
        self.assertEqual(normalized["sweep_execution"]["strategy"], "legacy_discrete")


if __name__ == "__main__":
    unittest.main(verbosity=2)
