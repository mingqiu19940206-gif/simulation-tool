"""Phase 6 VIA 扫描计划：安全数值轴、拓扑护栏、残桩跨界与预算（规格 §11）。"""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from config_manager import ConfigManager
from via_transition_sweeps import (
    VIA_SAFE_SWEEP_PATHS,
    VIA_TOPOLOGY_SWEEP_PATHS,
    _residual_stub_crossings,
    inspect_via_sweep_plan,
    iter_via_variations,
    via_scan_values,
)


class ViaTransitionSweepsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = ConfigManager(ROOT)
        self.config = copy.deepcopy(self.manager.default())
        self.config["scenario"]["type"] = "via_transition"
        self.config["via_transition"]["padstack"]["antipad"]["size_mode"] = "manual"

    def test_01_axes_registered_per_spec(self) -> None:
        # 基础轴 + 非圆形尺寸/角度/制造净距轴。
        self.assertGreaterEqual(len(VIA_SAFE_SWEEP_PATHS), 14)
        self.assertGreaterEqual(len(VIA_TOPOLOGY_SWEEP_PATHS), 14)
        self.assertIn("via_transition.pair_pitch", VIA_SAFE_SWEEP_PATHS)
        self.assertIn("via_transition.entry_trace.gap", VIA_SAFE_SWEEP_PATHS)
        self.assertIn("via_transition.exit_trace.gap", VIA_SAFE_SWEEP_PATHS)
        self.assertIn("via_transition.return_vias.center_to_signal", VIA_SAFE_SWEEP_PATHS)
        self.assertIn("via_transition.backdrill.residual_stub", VIA_SAFE_SWEEP_PATHS)
        self.assertIn("via_transition.exit_layer", VIA_TOPOLOGY_SWEEP_PATHS)
        self.assertIn("via_transition.return_vias.count", VIA_TOPOLOGY_SWEEP_PATHS)
        self.assertIn("via_transition.padstack.antipad.topology", VIA_TOPOLOGY_SWEEP_PATHS)
        self.assertIn("via_transition.padstack.antipad.shape", VIA_TOPOLOGY_SWEEP_PATHS)
        self.assertIn("via_transition.padstack.antipad.length", VIA_SAFE_SWEEP_PATHS)
        self.assertIn("via_transition.padstack.antipad.rotation_deg", VIA_SAFE_SWEEP_PATHS)
        self.assertIn("via_transition.padstack.antipad.clearance", VIA_TOPOLOGY_SWEEP_PATHS)

    def test_02_three_by_three_gives_nine_variations(self) -> None:
        self.config["sweeps"]["via_transition.pair_pitch"] = {"enabled": True, "start": 0.6, "stop": 1.0, "step": 0.2}
        self.config["sweeps"]["via_transition.padstack.antipad_diameter"] = {"enabled": True, "start": 0.6, "stop": 0.8, "step": 0.1}
        plan = inspect_via_sweep_plan(self.config)
        self.assertEqual(plan.case_count, 9)
        self.assertFalse(plan.issues)
        variations = iter_via_variations(plan)
        self.assertEqual(len(variations), 9)
        for variation in variations:
            self.assertIn("via_transition.pair_pitch", variation)
            self.assertIn("via_transition.padstack.antipad_diameter", variation)

    def test_03_topology_axis_blocked_with_reason(self) -> None:
        self.config["sweeps"]["via_transition.exit_layer"] = {"enabled": True, "values": ["L10", "L12"]}
        plan = inspect_via_sweep_plan(self.config)
        self.assertEqual(plan.topology_axes[0][0], "via_transition.exit_layer")
        self.assertTrue(any(level == "error" and "拓扑" in message for level, message in plan.issues))

    def test_04_budget_exceeded_blocked(self) -> None:
        # 10 值 × 10 值 = 100 > 50 上限。
        self.config["sweeps"]["via_transition.pair_pitch"] = {"enabled": True, "start": 0.2, "stop": 2.0, "step": 0.2}
        self.config["sweeps"]["via_transition.padstack.antipad_diameter"] = {"enabled": True, "start": 0.2, "stop": 2.0, "step": 0.2}
        plan = inspect_via_sweep_plan(self.config, max_cases=50)
        self.assertTrue(any(level == "error" and "上限" in message for level, message in plan.issues))

    def test_05_residual_stub_crossing_copper_blocked(self) -> None:
        self.config["sweeps"]["via_transition.backdrill.residual_stub"] = {"enabled": True, "start": 0.1, "stop": 3.0, "step": 1.0}
        plan = inspect_via_sweep_plan(self.config)
        self.assertTrue(any(level == "error" and "铜层" in message for level, message in plan.issues))

    def test_06_residual_stub_within_safe_interval_ok(self) -> None:
        # 残桩区间不跨越铜层边界（取一个窄区间）。
        self.config["sweeps"]["via_transition.backdrill.residual_stub"] = {"enabled": True, "start": 0.1, "stop": 0.15, "step": 0.05}
        plan = inspect_via_sweep_plan(self.config)
        self.assertFalse(any(level == "error" for level, _ in plan.issues))

    def test_07_unknown_axis_rejected(self) -> None:
        self.config["sweeps"]["via_transition.bogus_axis"] = {"enabled": True, "start": 0.1, "stop": 0.2, "step": 0.1}
        plan = inspect_via_sweep_plan(self.config)
        self.assertTrue(any(level == "error" and "未在" in message for level, message in plan.issues))

    def test_08_scan_values_respect_bounds(self) -> None:
        with self.assertRaises(ValueError):
            via_scan_values("via_transition.pair_pitch", {"start": 0.0, "stop": 1.0, "step": 0.5})
        values = via_scan_values("via_transition.pair_pitch", {"start": 0.6, "stop": 0.8, "step": 0.1})
        self.assertEqual(values, [0.6, 0.7, 0.8])

    def test_09_reversed_order_residual_sweep_uses_upper_functional_layer(self) -> None:
        self.config["via_transition"].update(
            {"entry_layer": "L14", "exit_layer": "L12",
             "drill_start_layer": "L1", "drill_end_layer": "L14"})
        self.config["via_transition"]["backdrill"].update(
            {"enabled": True, "from_side": "auto"})
        crossings = {
            layer: residual
            for residual, layer in _residual_stub_crossings(self.config)
        }
        # L11/L12 界面残桩必须钉在 DIEL_L11_L12 厚度 0.107442 mm，不能只比较两组配置。
        self.assertIn("L11", crossings)
        self.assertAlmostEqual(crossings["L11"], 0.107442, places=9)
        self.assertNotIn("L12", crossings)

        swapped = copy.deepcopy(self.config)
        swapped["via_transition"].update(
            {"entry_layer": "L12", "exit_layer": "L14"})
        swapped_crossings = {
            layer: residual
            for residual, layer in _residual_stub_crossings(swapped)
        }
        self.assertEqual(swapped_crossings, crossings)
        self.assertAlmostEqual(swapped_crossings["L11"], 0.107442, places=9)

        self.config["sweeps"]["via_transition.backdrill.residual_stub"] = {
            "enabled": True, "start": 0.1, "stop": 1.4, "step": 1.3,
        }
        plan = inspect_via_sweep_plan(self.config)
        self.assertTrue(any(level == "error" and "铜层" in message
                            for level, message in plan.issues), plan.issues)
        self.assertTrue(any(level == "error" and "L11" in message
                            for level, message in plan.issues), plan.issues)

        below = copy.deepcopy(self.config)
        below["sweeps"]["via_transition.backdrill.residual_stub"] = {
            "enabled": True, "start": 0.1, "stop": 0.107, "step": 0.007,
        }
        below_plan = inspect_via_sweep_plan(below)
        self.assertFalse(any(level == "error" and "铜层" in message
                             for level, message in below_plan.issues), below_plan.issues)

        straddle = copy.deepcopy(self.config)
        straddle["sweeps"]["via_transition.backdrill.residual_stub"] = {
            "enabled": True, "start": 0.1, "stop": 0.108, "step": 0.008,
        }
        straddle_plan = inspect_via_sweep_plan(straddle)
        self.assertTrue(any(level == "error" and "L11" in message
                            for level, message in straddle_plan.issues), straddle_plan.issues)


if __name__ == "__main__":
    unittest.main(verbosity=2)
