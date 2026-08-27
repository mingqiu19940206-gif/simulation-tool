"""VIA 信号孔间距与 45°/圆弧扇出的一致性回归。"""
from __future__ import annotations

import copy
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config_manager import ConfigManager, normalize_config
from geometry_calculator import GeometryCalculator
from via_transition_geometry import _fanout_centerline
from via_transition_semantics import validate_via_transition


def _config():
    cfg = copy.deepcopy(ConfigManager(ROOT).default())
    cfg["scenario"]["type"] = "via_transition"
    cfg["via_transition"]["exit_layer"] = "L14"
    cfg["via_transition"]["backdrill"]["enabled"] = False
    return cfg


class ViaTransitionFanoutTests(unittest.TestCase):
    def test_old_config_backfills_equal_pitch_without_fanout(self) -> None:
        cfg = _config()
        cfg["via_transition"].pop("signal_via_pitch", None)
        cfg["via_transition"].pop("fanout", None)
        normalized, messages = normalize_config(cfg)
        vt = normalized["via_transition"]
        self.assertEqual(vt["signal_via_pitch"], vt["pair_pitch"])
        self.assertEqual(vt["fanout"]["style"], "deg45")
        self.assertEqual(vt["fanout"]["direction"], "auto")
        self.assertTrue(any("signal_via_pitch" in message for message in messages))

    def test_equal_pitch_is_exact_legacy_rectangle(self) -> None:
        cfg = _config()
        cfg["via_transition"]["pair_pitch"] = 0.8
        cfg["via_transition"]["signal_via_pitch"] = 0.8
        geometry = GeometryCalculator(cfg).calculate()
        self.assertFalse(geometry.errors, geometry.errors)
        for trace in geometry.entry_traces + geometry.exit_traces:
            self.assertEqual(len(trace["points"]), 4)
            xs = sorted({round(point[0], 12) for point in trace["points"]})
            ys = sorted({round(point[1], 12) for point in trace["points"]})
            self.assertEqual(len(xs), 2)
            self.assertEqual(len(ys), 2)

    def test_deg45_expand_and_contract_have_45_degree_then_straight(self) -> None:
        for signal_pitch in (1.0, 0.6):
            with self.subTest(signal_pitch=signal_pitch):
                y_via = signal_pitch / 2.0
                y_trace = 0.8 / 2.0
                centerline = _fanout_centerline(0.0, y_via, -3.0, y_trace,
                                                y_trace - y_via, "deg45")
                self.assertEqual(len(centerline), 3)
                first_dx = centerline[1].x - centerline[0].x
                first_dy = centerline[1].y - centerline[0].y
                self.assertAlmostEqual(abs(first_dx), abs(first_dy))
                self.assertAlmostEqual(centerline[1].y, centerline[2].y)
                self.assertAlmostEqual(centerline[2].x, -3.0)

    def test_arc_exact_fit_keeps_arc_instead_of_slanted_fallback(self) -> None:
        # 横向位移 = 0.1 mm，端口总长也正好 0.1 mm，允许 straight_length=0。
        centerline = _fanout_centerline(0.0, 0.5, 0.1, 0.4, -0.1, "arc")
        self.assertGreater(len(centerline), 10)
        self.assertAlmostEqual(centerline[0].x, 0.0)
        self.assertAlmostEqual(centerline[0].y, 0.5)
        self.assertAlmostEqual(centerline[-1].x, 0.1)
        self.assertAlmostEqual(centerline[-1].y, 0.4)
        self.assertTrue(all(math.dist((a.x, a.y), (b.x, b.y)) > 1e-12
                            for a, b in zip(centerline, centerline[1:])))

        cfg = _config()
        vt = cfg["via_transition"]
        vt["signal_via_pitch"] = 1.0
        vt["fanout"] = {"style": "arc", "direction": "auto", "straight_length": 0.0}
        vt["entry_trace"]["length"] = 0.1
        vt["exit_trace"]["length"] = 0.1
        geometry = GeometryCalculator(cfg).calculate()
        issues = validate_via_transition(cfg, geometry)
        self.assertFalse([item for item in issues if item[0] == "VIA-TRN-071"], issues)
        self.assertTrue(all(len(trace["points"]) > 20
                            for trace in geometry.entry_traces + geometry.exit_traces))

    def test_invalid_length_and_direction_are_reported_without_changing_physics(self) -> None:
        cfg = _config()
        vt = cfg["via_transition"]
        vt["signal_via_pitch"] = 1.0
        vt["fanout"] = {"style": "deg45", "direction": "contract", "straight_length": 0.2}
        vt["entry_trace"]["length"] = 0.25
        vt["exit_trace"]["length"] = 0.25
        geometry = GeometryCalculator(cfg).calculate()
        issues = validate_via_transition(cfg, geometry)
        rule_ids = [item[0] for item in issues]
        self.assertIn("VIA-TRN-071", rule_ids)
        self.assertIn("VIA-TRN-072", rule_ids)
        p_via = next(via for via in geometry.signal_vias if via.signal == "P")
        self.assertAlmostEqual(p_via.y, 0.5)
        self.assertAlmostEqual(next(port for port in geometry.ports
                                    if port.name == "PORT_IN_P").integration_line_start[1], 0.4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
