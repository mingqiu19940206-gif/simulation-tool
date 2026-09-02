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
        cfg["via_transition"]["entry_trace"].pop("gap", None)
        cfg["via_transition"]["exit_trace"].pop("gap", None)
        normalized, messages = normalize_config(cfg)
        vt = normalized["via_transition"]
        self.assertEqual(vt["signal_via_pitch"], vt["pair_pitch"])
        self.assertEqual(vt["entry_trace"]["width"] + vt["entry_trace"]["gap"],
                         vt["pair_pitch"])
        self.assertEqual(vt["exit_trace"]["width"] + vt["exit_trace"]["gap"],
                         vt["pair_pitch"])
        self.assertEqual(vt["fanout"]["style"], "deg45")
        self.assertEqual(vt["fanout"]["direction"], "auto")
        self.assertTrue(any("signal_via_pitch" in message for message in messages))

    def test_equal_pitch_is_exact_legacy_rectangle(self) -> None:
        cfg = _config()
        cfg["via_transition"]["signal_via_pitch"] = 0.8
        cfg["via_transition"]["entry_trace"].update({"width": 0.1, "gap": 0.7})
        cfg["via_transition"]["exit_trace"].update({"width": 0.1, "gap": 0.7})
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

    def test_input_and_output_gaps_drive_independent_geometry_and_ports(self) -> None:
        cfg = _config()
        vt = cfg["via_transition"]
        vt["pair_pitch"] = 1.7  # deprecated compatibility value must not drive geometry
        vt["signal_via_pitch"] = 0.8
        vt["entry_trace"].update({"width": 0.1, "gap": 0.5})  # pitch 0.6
        vt["exit_trace"].update({"width": 0.2, "gap": 0.8})   # pitch 1.0
        geometry = GeometryCalculator(cfg).calculate()
        self.assertFalse(geometry.errors, geometry.errors)
        p_via = next(via for via in geometry.signal_vias if via.signal == "P")
        port_in = next(port for port in geometry.ports if port.name == "PORT_IN_P")
        port_out = next(port for port in geometry.ports if port.name == "PORT_OUT_P")
        self.assertAlmostEqual(p_via.y, 0.4)
        self.assertAlmostEqual(port_in.integration_line_start[1], 0.3)
        self.assertAlmostEqual(port_out.integration_line_start[1], 0.5)
        entry_p = next(trace for trace in geometry.entry_traces if trace["signal"] == "P")
        exit_p = next(trace for trace in geometry.exit_traces if trace["signal"] == "P")
        entry_edge_x = min(point[0] for point in entry_p["points"])
        exit_edge_x = max(point[0] for point in exit_p["points"])
        entry_edge = [point for point in entry_p["points"]
                      if math.isclose(point[0], entry_edge_x, abs_tol=1e-9)]
        exit_edge = [point for point in exit_p["points"]
                     if math.isclose(point[0], exit_edge_x, abs_tol=1e-9)]
        self.assertAlmostEqual(sum(point[1] for point in entry_edge) / len(entry_edge), 0.3)
        self.assertAlmostEqual(sum(point[1] for point in exit_edge) / len(exit_edge), 0.5)

    def test_non_positive_gap_is_rejected_on_the_correct_side(self) -> None:
        cfg = _config()
        cfg["via_transition"]["exit_trace"]["gap"] = 0.0
        geometry = GeometryCalculator(cfg).calculate()
        issues = validate_via_transition(cfg, geometry)
        self.assertTrue(any(item[0] == "VIA-TRN-075"
                            and item[3] == "via_transition.exit_trace.gap"
                            for item in issues), issues)


if __name__ == "__main__":
    unittest.main(verbosity=2)
