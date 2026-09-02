"""Independent VIA entry/exit GAP: geometry, legacy pair_pitch, one-sided fanout."""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from config_manager import (
    ConfigManager,
    apply_pair_pitch_to_trace_gaps,
    get_default_config,
    normalize_config,
    via_trace_center_pitch,
)
from geometry_calculator import GeometryCalculator
from model_validator import ModelValidator
from simulation_sweep_semantics import apply_sweep_value
from via_transition_semantics import validate_via_transition


def _via_cfg():
    cfg = copy.deepcopy(ConfigManager(ROOT).default())
    cfg["scenario"]["type"] = "via_transition"
    cfg["via_transition"]["exit_layer"] = "L14"
    cfg["via_transition"]["backdrill"]["enabled"] = False
    cfg["via_transition"]["return_vias"]["connect_layers"] = ["L2", "L13"]
    return cfg


def _port(geometry, name):
    return next(port for port in geometry.ports if port.name == name)


class ViaTransitionTraceGapTests(unittest.TestCase):
    def test_01_changing_entry_gap_does_not_change_exit_pitch(self) -> None:
        cfg = _via_cfg()
        before = GeometryCalculator(cfg).calculate()
        self.assertFalse(before.errors, before.errors)
        exit_ports_before = {
            name: (
                _port(before, name).y_min,
                _port(before, name).y_max,
                _port(before, name).integration_line_start,
            )
            for name in ("PORT_OUT_P", "PORT_OUT_N")
        }
        exit_traces_before = copy.deepcopy(before.exit_traces)
        exit_center_before = via_trace_center_pitch(
            cfg["via_transition"]["exit_trace"], cfg["via_transition"]["pair_pitch"])

        cfg["via_transition"]["entry_trace"]["gap"] = 0.5
        after = GeometryCalculator(cfg).calculate()
        self.assertFalse(after.errors, after.errors)

        vt = cfg["via_transition"]
        self.assertAlmostEqual(
            via_trace_center_pitch(vt["entry_trace"], vt["pair_pitch"]), 0.6)
        self.assertAlmostEqual(
            via_trace_center_pitch(vt["exit_trace"], vt["pair_pitch"]), exit_center_before)
        self.assertAlmostEqual(vt["exit_trace"]["gap"], 0.7)

        for name in ("PORT_OUT_P", "PORT_OUT_N"):
            port = _port(after, name)
            self.assertAlmostEqual(port.y_min, exit_ports_before[name][0])
            self.assertAlmostEqual(port.y_max, exit_ports_before[name][1])
            self.assertEqual(port.integration_line_start, exit_ports_before[name][2])
        self.assertEqual(after.exit_traces, exit_traces_before)

        self.assertAlmostEqual(_port(after, "PORT_IN_P").integration_line_start[1], 0.3)
        self.assertAlmostEqual(_port(before, "PORT_IN_P").integration_line_start[1], 0.4)
        self.assertNotEqual(after.entry_traces, before.entry_traces)

    def test_02_legacy_pair_pitch_only_matches_same_both_end_pitch(self) -> None:
        raw = get_default_config()
        raw["scenario"]["type"] = "via_transition"
        raw["via_transition"]["exit_layer"] = "L14"
        raw["via_transition"]["backdrill"]["enabled"] = False
        raw["via_transition"]["return_vias"]["connect_layers"] = ["L2", "L13"]
        raw["via_transition"]["pair_pitch"] = 1.0
        raw["via_transition"]["entry_trace"]["width"] = 0.1
        raw["via_transition"]["exit_trace"]["width"] = 0.2
        raw["via_transition"]["entry_trace"].pop("gap", None)
        raw["via_transition"]["exit_trace"].pop("gap", None)
        raw["via_transition"].pop("signal_via_pitch", None)

        loaded, corrections = normalize_config(raw)
        vt = loaded["via_transition"]
        self.assertTrue(any("gap" in message for message in corrections))
        self.assertAlmostEqual(vt["entry_trace"]["gap"], 0.9)
        self.assertAlmostEqual(vt["exit_trace"]["gap"], 0.8)
        self.assertAlmostEqual(via_trace_center_pitch(vt["entry_trace"], vt["pair_pitch"]), 1.0)
        self.assertAlmostEqual(via_trace_center_pitch(vt["exit_trace"], vt["pair_pitch"]), 1.0)
        self.assertAlmostEqual(vt["signal_via_pitch"], 1.0)

        geometry = GeometryCalculator(loaded).calculate()
        self.assertFalse(geometry.errors, geometry.errors)
        self.assertAlmostEqual(_port(geometry, "PORT_IN_P").integration_line_start[1], 0.5)
        self.assertAlmostEqual(_port(geometry, "PORT_OUT_P").integration_line_start[1], 0.5)
        self.assertAlmostEqual(_port(geometry, "PORT_IN_N").integration_line_start[1], -0.5)
        self.assertAlmostEqual(_port(geometry, "PORT_OUT_N").integration_line_start[1], -0.5)

        example = json.loads(
            (ROOT / "examples" / "via_transition_through.json").read_text(encoding="utf-8"))
        self.assertNotIn("gap", example["via_transition"]["entry_trace"])
        self.assertNotIn("gap", example["via_transition"]["exit_trace"])
        example_loaded, _ = normalize_config(example)
        example_geom = GeometryCalculator(example_loaded).calculate()
        self.assertFalse(example_geom.errors, example_geom.errors)
        pair = float(example_loaded["via_transition"]["pair_pitch"])
        self.assertAlmostEqual(
            via_trace_center_pitch(example_loaded["via_transition"]["entry_trace"], pair), pair)
        self.assertAlmostEqual(
            via_trace_center_pitch(example_loaded["via_transition"]["exit_trace"], pair), pair)
        self.assertAlmostEqual(_port(example_geom, "PORT_IN_P").integration_line_start[1], pair / 2.0)
        self.assertAlmostEqual(_port(example_geom, "PORT_OUT_P").integration_line_start[1], pair / 2.0)

    def test_03_one_side_fanout_other_straight_validates(self) -> None:
        cfg = _via_cfg()
        vt = cfg["via_transition"]
        vt["signal_via_pitch"] = 0.8
        vt["entry_trace"]["gap"] = 0.7  # width 0.1 → center 0.8, straight
        vt["exit_trace"]["gap"] = 0.5   # width 0.1 → center 0.6, fanout
        geometry = GeometryCalculator(cfg).calculate()
        self.assertFalse(geometry.errors, geometry.errors)

        self.assertAlmostEqual(via_trace_center_pitch(vt["entry_trace"], vt["pair_pitch"]), 0.8)
        self.assertAlmostEqual(via_trace_center_pitch(vt["exit_trace"], vt["pair_pitch"]), 0.6)
        for trace in geometry.entry_traces:
            self.assertEqual(len(trace["points"]), 4)
        for trace in geometry.exit_traces:
            self.assertGreater(len(trace["points"]), 4)

        issues = validate_via_transition(cfg, geometry)
        self.assertFalse([item for item in issues if item[1] == "error"], issues)

        report, _normalized, _geometry = ModelValidator(ConfigManager(ROOT)).run_local(cfg)
        self.assertEqual(report.counts["errors"], 0, report.raw_log)

    def test_04_legacy_pair_pitch_sweep_moves_both_end_gaps(self) -> None:
        cfg = _via_cfg()
        cfg["via_transition"]["entry_trace"]["width"] = 0.1
        cfg["via_transition"]["exit_trace"]["width"] = 0.2
        cfg["via_transition"]["entry_trace"]["gap"] = 0.4
        cfg["via_transition"]["exit_trace"]["gap"] = 0.3
        apply_sweep_value(cfg, "via_transition.pair_pitch", 1.0)
        vt = cfg["via_transition"]
        self.assertAlmostEqual(vt["pair_pitch"], 1.0)
        self.assertAlmostEqual(vt["entry_trace"]["gap"], 0.9)
        self.assertAlmostEqual(vt["exit_trace"]["gap"], 0.8)
        self.assertAlmostEqual(via_trace_center_pitch(vt["entry_trace"], vt["pair_pitch"]), 1.0)
        self.assertAlmostEqual(via_trace_center_pitch(vt["exit_trace"], vt["pair_pitch"]), 1.0)

        apply_sweep_value(cfg, "via_transition.entry_trace.gap", 0.5)
        self.assertAlmostEqual(vt["entry_trace"]["gap"], 0.5)
        self.assertAlmostEqual(vt["exit_trace"]["gap"], 0.8)

    def test_05_apply_pair_pitch_helper_sets_both_gaps(self) -> None:
        vt = {
            "pair_pitch": 0.8,
            "entry_trace": {"width": 0.1, "gap": 0.2},
            "exit_trace": {"width": 0.15, "gap": 0.3},
        }
        apply_pair_pitch_to_trace_gaps(vt, 1.2)
        self.assertAlmostEqual(vt["pair_pitch"], 1.2)
        self.assertAlmostEqual(vt["entry_trace"]["gap"], 1.1)
        self.assertAlmostEqual(vt["exit_trace"]["gap"], 1.05)


if __name__ == "__main__":
    unittest.main(verbosity=2)
