"""Phase 2 差分贯通孔换层几何、预览、Validate 与 HFSS 脚本测试。"""
from __future__ import annotations

import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config_manager import ConfigManager, validate_config
from geometry_calculator import GeometryCalculator
from model_preview import build_model_scene
from model_validator import ModelValidator


def _via_cfg():
    manager = ConfigManager(ROOT)
    cfg = copy.deepcopy(manager.default())
    cfg["scenario"]["type"] = "via_transition"
    # Phase 2 范围：L1 entry、L14 exit、L1–L14 机械贯通（外层到外层）、无背钻。
    cfg["via_transition"]["exit_layer"] = "L14"
    cfg["via_transition"]["backdrill"]["enabled"] = False
    cfg["via_transition"]["return_vias"]["connect_layers"] = ["L2", "L13"]
    return cfg


class ViaTransitionGeometryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = ConfigManager(ROOT)
        self.cfg = _via_cfg()

    def test_01_two_signal_vias_with_pn_positions(self) -> None:
        geometry = GeometryCalculator(self.cfg).calculate()
        self.assertEqual(len(geometry.signal_vias), 2)
        p = next(via for via in geometry.signal_vias if via.signal == "P")
        n = next(via for via in geometry.signal_vias if via.signal == "N")
        self.assertAlmostEqual(p.y, 0.4)
        self.assertAlmostEqual(n.y, -0.4)
        self.assertEqual(p.entry_layer, "L1")
        self.assertEqual(p.exit_layer, "L14")
        self.assertEqual(p.functional_pad_layers, ("L1", "L14"))
        self.assertIn("L2", p.antipad_layers)
        self.assertIn("L13", p.antipad_layers)
        self.assertFalse(geometry.errors, geometry.errors)

    def test_02_signal_via_barrel_spans_drill_layers(self) -> None:
        geometry = GeometryCalculator(self.cfg).calculate()
        p = geometry.signal_vias[0]
        layers = {layer.name: layer for layer in geometry.stackup}
        self.assertAlmostEqual(p.z_top, layers["L1"].z_top)
        self.assertAlmostEqual(p.z_bottom, layers["L14"].z_bottom)

    def test_03_return_vias_symmetric_and_connect_planes(self) -> None:
        geometry = GeometryCalculator(self.cfg).calculate()
        self.assertEqual(len(geometry.return_vias), 2)
        ys = sorted(via.y for via in geometry.return_vias)
        self.assertAlmostEqual(ys[0], -1.1)
        self.assertAlmostEqual(ys[1], 1.1)
        for via in geometry.return_vias:
            self.assertEqual(via.connect_layers, ("L2", "L13"))
            self.assertIn("L1", via.clearance_layers)

    def test_04_four_ports_named_correctly(self) -> None:
        geometry = GeometryCalculator(self.cfg).calculate()
        names = {port.name for port in geometry.ports}
        self.assertEqual(names, {"PORT_IN_P", "PORT_IN_N", "PORT_OUT_P", "PORT_OUT_N"})
        for side in ("IN", "OUT"):
            p = next(port for port in geometry.ports if port.name == f"PORT_{side}_P")
            n = next(port for port in geometry.ports if port.name == f"PORT_{side}_N")
            self.assertLessEqual(n.y_max, p.y_min)

    def test_05_preview_builds_via_categories(self) -> None:
        geometry = GeometryCalculator(self.cfg).calculate()
        scene = build_model_scene(self.cfg, geometry, None)
        categories = {item.category for item in scene.objects}
        for expected in ("信号VIA", "回流孔", "输入走线", "输出走线", "功能焊盘", "VIA反焊盘", "Port"):
            self.assertIn(expected, categories)
        self.assertIn("airbox", {item.key for item in scene.objects})
        self.assertIn("open_boundary", {item.key for item in scene.objects})
        self.assertLess(geometry.airbox["x_min"], geometry.plane_bounds.x_min)
        self.assertGreater(geometry.airbox["x_max"], geometry.plane_bounds.x_max)
        self.assertLess(geometry.airbox["y_min"], geometry.plane_bounds.y_min)
        self.assertGreater(geometry.airbox["y_max"], geometry.plane_bounds.y_max)

    def test_06_model_validator_passes_default(self) -> None:
        report, _config, _geometry = ModelValidator(self.manager).run_local(self.cfg)
        self.assertEqual(report.counts["errors"], 0, report.raw_log)

    def test_07_entry_equals_exit_rejected(self) -> None:
        cfg = copy.deepcopy(self.cfg)
        cfg["via_transition"]["exit_layer"] = "L1"
        _, result = validate_config(cfg, ROOT, check_output_directory=False)
        report, _config, _geometry = ModelValidator(self.manager).run_local(cfg)
        self.assertGreater(report.counts["errors"], 0)

    def test_08_return_via_collision_detected(self) -> None:
        cfg = copy.deepcopy(self.cfg)
        cfg["via_transition"]["return_vias"]["offset_y"] = 0.4
        geometry = GeometryCalculator(cfg).calculate()
        self.assertTrue(any("侵入" in error for error in geometry.errors))

    def test_09_hfss_script_validate_only_passes(self) -> None:
        from hfss_script_generator import HfssScriptGenerator
        cfg = copy.deepcopy(self.cfg)
        cfg["project"]["overwrite_existing"] = True
        with tempfile.TemporaryDirectory() as directory:
            result = HfssScriptGenerator(cfg, ROOT).generate(Path(directory))
            script_text = result.script_path.read_text(encoding="utf-8")
            self.assertIn("build_via_transition_scenario", script_text)
            self.assertIn("_create_signal_vias", script_text)
            self.assertIn("height = length(z_top_value - z_bottom_value)", script_text)
            self.assertIn("bd_height = length(bd_z_top_value - bd_z_bottom_value)", script_text)
            self.assertIn('result["SIGNAL_" + signal]', script_text)
            self.assertIn('["SIGNAL_P", "SIGNAL_N"] if scenario_type == "via_transition"', script_text)
            self.assertIn("app.modeler.create_region", script_text)
            self.assertIn('name + "_BOARD_CUT_" + safe_identifier(layer_name)', script_text)
            self.assertIn("plane_parts + ground_parts", script_text)
            self.assertIn('"Sdd11": "St(IN_DIFF,IN_DIFF)"', script_text)
            self.assertIn("app.post.get_solution_data_per_variation", script_text)
            self.assertIn("setup_sweep_name=setup_name + \" : \" + sweep_name", script_text)
            self.assertIn('solution_type="Terminal Solution Data"', script_text)
            self.assertIn("solution.get_expression_data", script_text)
            self.assertIn("_hfss_profile_convergence", script_text)
            self.assertIn('"solve_completed": solve_completed', script_text)
            self.assertIn('"convergence_evidence": convergence_evidence', script_text)
            self.assertIn("TDR requires a DC-anchored uniform grid", script_text)
            self.assertIn("stale_tdr_path.unlink()", script_text)
            self.assertIn('CONFIG.get("simulation", {}).get("generate_tdr", False)', script_text)
            self.assertIn("final TDR settings and fail-closed warnings", script_text)
            self.assertNotIn("dB(S(IN_DIFF,IN_DIFF))", script_text)
            self.assertNotIn("app.get_solution_data(expressions", script_text)
            self.assertNotIn("height = z_top - z_bottom", script_text)
            self.assertNotIn("bd_height = bd_z_top - bd_z_bottom", script_text)
            completed = subprocess.run(
                [sys.executable, str(result.script_path), "--validate-only"],
                capture_output=True, text=True, timeout=90,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_10_snapshot_round_trips_geometry(self) -> None:
        from config_manager import make_generation_snapshot, validate_generation_snapshot
        geometry = GeometryCalculator(self.cfg).calculate()
        snapshot = make_generation_snapshot(self.cfg, geometry)
        validate_generation_snapshot(snapshot)
        self.assertEqual(snapshot["geometry_snapshot"]["scenario"], "via_transition")
        self.assertEqual(len(snapshot["geometry_snapshot"]["signal_vias"]), 2)

    def test_11_backdrill_enabled_without_stub_rejected(self) -> None:
        cfg = copy.deepcopy(self.cfg)
        cfg["via_transition"]["backdrill"]["enabled"] = True
        geometry = GeometryCalculator(cfg).calculate()
        self.assertTrue(any("不存在残桩" in error for error in geometry.errors), geometry.errors)
        report, _config, _geometry = ModelValidator(self.manager).run_local(cfg)
        self.assertTrue(any(item.rule_id == "VIA-TRN-049" for item in report.items), report.raw_log)

    def test_12_internal_exit_without_backdrill_rejected(self) -> None:
        cfg = copy.deepcopy(self.cfg)
        cfg["via_transition"]["exit_layer"] = "L10"
        geometry = GeometryCalculator(cfg).calculate()
        self.assertTrue(any("残桩" in error for error in geometry.errors), geometry.errors)
        report, _config, _geometry = ModelValidator(self.manager).run_local(cfg)
        self.assertTrue(any(item.rule_id == "VIA-TRN-050" for item in report.items), report.raw_log)


if __name__ == "__main__":
    unittest.main(verbosity=2)
