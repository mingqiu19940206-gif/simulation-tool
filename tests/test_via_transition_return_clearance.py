"""Return VIA center_to_signal: side_pair count=2 clearance vs pair-center offset_y."""
from __future__ import annotations

import copy
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from config_manager import (
    ConfigManager,
    get_default_config,
    normalize_config,
    side_pair_center_to_signal,
)
from geometry_calculator import GeometryCalculator
from simulation_sweep_semantics import apply_sweep_value


def _via_cfg():
    cfg = copy.deepcopy(ConfigManager(ROOT).default())
    cfg["scenario"]["type"] = "via_transition"
    cfg["via_transition"]["exit_layer"] = "L14"
    cfg["via_transition"]["backdrill"]["enabled"] = False
    cfg["via_transition"]["return_vias"]["connect_layers"] = ["L2", "L13"]
    return cfg


def _xy(vias):
    return [(float(via.x), float(via.y)) for via in vias]


def _nearest_center_distance(return_via, signal_vias) -> float:
    return min(
        math.hypot(float(return_via.x) - float(sig.x),
                   float(return_via.y) - float(sig.y))
        for sig in signal_vias
    )


class ViaTransitionReturnClearanceTests(unittest.TestCase):
    def test_01_changing_center_to_signal_moves_gnd_not_signal(self) -> None:
        cfg = _via_cfg()
        before = GeometryCalculator(cfg).calculate()
        self.assertFalse(before.errors, before.errors)
        signal_before = _xy(before.signal_vias)
        self.assertEqual(len(before.return_vias), 2)
        for via in before.return_vias:
            self.assertAlmostEqual(_nearest_center_distance(via, before.signal_vias), 0.7)

        cfg["via_transition"]["return_vias"]["center_to_signal"] = 0.5
        after = GeometryCalculator(cfg).calculate()
        self.assertFalse(after.errors, after.errors)
        self.assertEqual(_xy(after.signal_vias), signal_before)
        self.assertEqual(len(after.return_vias), 2)
        for via in after.return_vias:
            self.assertAlmostEqual(_nearest_center_distance(via, after.signal_vias), 0.5)
        self.assertNotEqual(_xy(after.return_vias), _xy(before.return_vias))

    def test_02_legacy_offset_y_loads_bit_identical_geometry(self) -> None:
        baseline_raw = copy.deepcopy(get_default_config())
        baseline_raw["scenario"]["type"] = "via_transition"
        baseline_raw["via_transition"]["exit_layer"] = "L14"
        baseline_raw["via_transition"]["backdrill"]["enabled"] = False
        baseline_raw["via_transition"]["return_vias"]["connect_layers"] = ["L2", "L13"]
        baseline, _ = normalize_config(baseline_raw)
        baseline_geo = GeometryCalculator(baseline).calculate().to_dict()

        legacy = copy.deepcopy(get_default_config())
        legacy["scenario"]["type"] = "via_transition"
        legacy["via_transition"]["exit_layer"] = "L14"
        legacy["via_transition"]["backdrill"]["enabled"] = False
        legacy["via_transition"]["return_vias"]["connect_layers"] = ["L2", "L13"]
        legacy["via_transition"]["return_vias"].pop("center_to_signal", None)
        legacy["via_transition"]["return_vias"]["offset_y"] = 1.1
        legacy["via_transition"]["signal_via_pitch"] = 0.8
        loaded, corrections = normalize_config(legacy)
        self.assertAlmostEqual(
            loaded["via_transition"]["return_vias"]["center_to_signal"], 0.7)
        self.assertAlmostEqual(loaded["via_transition"]["return_vias"]["offset_y"], 1.1)
        self.assertTrue(any("center_to_signal" in item for item in corrections))
        loaded_geo = GeometryCalculator(loaded).calculate().to_dict()
        self.assertEqual(loaded_geo["signal_vias"], baseline_geo["signal_vias"])
        self.assertEqual(loaded_geo["return_vias"], baseline_geo["return_vias"])
        self.assertEqual(loaded_geo, baseline_geo)

    def test_03_holding_center_to_signal_moves_gnd_when_pitch_changes(self) -> None:
        cfg = _via_cfg()
        cfg["via_transition"]["return_vias"]["center_to_signal"] = 0.7
        before = GeometryCalculator(cfg).calculate()
        signal_before = _xy(before.signal_vias)
        for via in before.return_vias:
            self.assertAlmostEqual(_nearest_center_distance(via, before.signal_vias), 0.7)

        cfg["via_transition"]["signal_via_pitch"] = 1.0
        after = GeometryCalculator(cfg).calculate()
        self.assertFalse(after.errors, after.errors)
        self.assertNotEqual(_xy(after.signal_vias), signal_before)
        self.assertAlmostEqual(after.signal_vias[0].y, 0.5, places=9)
        for via in after.return_vias:
            self.assertAlmostEqual(_nearest_center_distance(via, after.signal_vias), 0.7)
        self.assertNotEqual(_xy(after.return_vias), _xy(before.return_vias))
        ys = sorted(via.y for via in after.return_vias)
        self.assertAlmostEqual(ys[0], -1.2)
        self.assertAlmostEqual(ys[1], 1.2)

    def test_04_legacy_offset_y_sweep_rederives_center_to_signal(self) -> None:
        cfg = _via_cfg()
        apply_sweep_value(cfg, "via_transition.return_vias.offset_y", 1.3)
        self.assertAlmostEqual(cfg["via_transition"]["return_vias"]["offset_y"], 1.3)
        self.assertAlmostEqual(
            cfg["via_transition"]["return_vias"]["center_to_signal"],
            side_pair_center_to_signal(1.3, 0.8),
        )
        geometry = GeometryCalculator(cfg).calculate()
        ys = sorted(via.y for via in geometry.return_vias)
        self.assertAlmostEqual(ys[0], -1.3)
        self.assertAlmostEqual(ys[1], 1.3)

    def test_05_center_to_signal_sweep_keeps_offset_y_derived(self) -> None:
        cfg = _via_cfg()
        apply_sweep_value(cfg, "via_transition.return_vias.center_to_signal", 0.55)
        self.assertAlmostEqual(
            cfg["via_transition"]["return_vias"]["center_to_signal"], 0.55)
        self.assertAlmostEqual(cfg["via_transition"]["return_vias"]["offset_y"], 0.95)
        geometry = GeometryCalculator(cfg).calculate()
        for via in geometry.return_vias:
            self.assertAlmostEqual(_nearest_center_distance(via, geometry.signal_vias), 0.55)

    def test_06_four_corner_still_uses_offset_y(self) -> None:
        cfg = _via_cfg()
        cfg["via_transition"]["return_vias"]["pattern"] = "four_corner"
        cfg["via_transition"]["return_vias"]["count"] = 4
        cfg["via_transition"]["return_vias"]["offset_x"] = 0.9
        cfg["via_transition"]["return_vias"]["offset_y"] = 1.2
        cfg["via_transition"]["return_vias"]["center_to_signal"] = 0.11
        geometry = GeometryCalculator(cfg).calculate()
        points = sorted(_xy(geometry.return_vias))
        expected = sorted([
            (0.9, 1.2), (0.9, -1.2), (-0.9, 1.2), (-0.9, -1.2),
        ])
        for got, want in zip(points, expected):
            self.assertAlmostEqual(got[0], want[0])
            self.assertAlmostEqual(got[1], want[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
