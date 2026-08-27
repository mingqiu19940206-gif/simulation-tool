from __future__ import annotations

import copy
import math
import unittest

from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from config_manager import get_default_config, normalize_config
from geometry_calculator import GeometryCalculator
from simulation_sweep_semantics import inspect_sweep_plan


class TraceAndCapacitorTopologyTests(unittest.TestCase):
    def geometry(self, mode: str, **updates):
        config = get_default_config()
        config["capacitor_model"]["mode"] = mode
        for path, value in updates.items():
            section, key = path.split("__", 1)
            config[section][key] = value
        normalized, _ = normalize_config(config)
        return normalized, GeometryCalculator(normalized).calculate()

    def test_continuous_miter_strips_are_simple_and_have_no_corner_voids(self) -> None:
        for width in (0.07, 0.1, 0.16):
            for trace_pitch, cap_pitch, rotation in ((0.2, 0.6, 0.0), (0.45, 1.1, 12.0), (0.5, 1.2, -10.0)):
                with self.subTest(width=width, trace_pitch=trace_pitch, cap_pitch=cap_pitch, rotation=rotation):
                    config, geometry = self.geometry(
                        "ideal_bridge", trace__trace_width=width,
                        trace__trace_pitch=trace_pitch,
                        capacitor__cap_pitch=cap_pitch,
                        capacitor__pad_rotation=rotation,
                    )
                    self.assertFalse(geometry.errors)
                    for signal in ("P", "N"):
                        for shape in geometry.signal_shapes[signal]:
                            polygon = Polygon([(point.x, point.y) for point in shape.points])
                            self.assertTrue(polygon.is_valid)
                            self.assertTrue(polygon.exterior.is_simple)
                            if shape.category in {"trace_in", "trace_out"}:
                                centerline = LineString([(point.x, point.y) for point in shape.centerline])
                                # Every centerline point has at least half the requested
                                # copper width around it, including both 0°↔45° joins.
                                self.assertTrue(polygon.buffer(1e-9).covers(
                                    centerline.buffer(width / 2.0, join_style="mitre", cap_style="flat")
                                ))

    def test_mode_specific_bridge_and_conductor_topology(self) -> None:
        expectations = {
            "ideal_bridge": ({"P", "N"}, {"SIGNAL_P", "SIGNAL_N"}, set()),
            "lumped_rlc": (set(), {"SIGNAL_P_IN", "SIGNAL_P_OUT", "SIGNAL_N_IN", "SIGNAL_N_OUT"}, {"P", "N"}),
            "sparameter": (set(), {"SIGNAL_P_IN", "SIGNAL_P_OUT", "SIGNAL_N_IN", "SIGNAL_N_OUT"}, {"P", "N"}),
            "open": (set(), {"SIGNAL_P_IN", "SIGNAL_P_OUT", "SIGNAL_N_IN", "SIGNAL_N_OUT"}, set()),
        }
        for mode, (bridges, groups, sheets) in expectations.items():
            with self.subTest(mode=mode):
                _, geometry = self.geometry(mode)
                self.assertEqual(set(geometry.bridge_shapes), bridges)
                self.assertEqual(set(geometry.conductor_groups), groups)
                self.assertEqual(set(geometry.capacitor_model_shapes), sheets)
                for signal in ("P", "N"):
                    union = unary_union([
                        Polygon([(point.x, point.y) for point in shape.points])
                        for shape in geometry.signal_shapes[signal]
                    ])
                    components = len(getattr(union, "geoms", (union,)))
                    self.assertEqual(components, 1 if mode == "ideal_bridge" else 2)

    def test_rotated_rlc_sheet_spans_gap_and_points_input_to_output(self) -> None:
        for rotation in (-20.0, 0.0, 18.0):
            with self.subTest(rotation=rotation):
                config, geometry = self.geometry("lumped_rlc", capacitor__pad_rotation=rotation)
                for signal, sheet in geometry.capacitor_model_shapes.items():
                    start = sheet.metadata["integration_line_start"]
                    end = sheet.metadata["integration_line_end"]
                    vector = (end[0] - start[0], end[1] - start[1])
                    expected = math.radians(rotation if signal == "P" else -rotation)
                    self.assertAlmostEqual(math.hypot(*vector), config["capacitor"]["pad_gap"])
                    self.assertAlmostEqual(vector[0] / math.hypot(*vector), math.cos(expected))
                    self.assertAlmostEqual(vector[1] / math.hypot(*vector), math.sin(expected))
                    self.assertEqual(sheet.metadata["input_conductor"], f"SIGNAL_{signal}_IN")
                    self.assertEqual(sheet.metadata["output_conductor"], f"SIGNAL_{signal}_OUT")

    def test_min_nominal_max_variations_keep_topology(self) -> None:
        variations = (
            {"trace__trace_width": 0.07, "trace__trace_pitch": 0.18, "capacitor__cap_pitch": 0.6},
            {"trace__trace_width": 0.1, "trace__trace_pitch": 0.2, "capacitor__cap_pitch": 0.6},
            {"trace__trace_width": 0.15, "trace__trace_pitch": 0.34, "capacitor__cap_pitch": 0.8},
        )
        for values in variations:
            with self.subTest(values=values):
                config, geometry = self.geometry("lumped_rlc", **values)
                self.assertFalse(geometry.errors)
                self.assertFalse(geometry.bridge_shapes)
                self.assertEqual(set(geometry.capacitor_model_shapes), {"P", "N"})
                for sheet in geometry.capacitor_model_shapes.values():
                    start = sheet.metadata["integration_line_start"]
                    end = sheet.metadata["integration_line_end"]
                    self.assertGreater(math.dist(start, end), 0.0)

    def test_native_nonideal_pad_motion_fails_closed(self) -> None:
        config = get_default_config()
        config["capacitor_model"]["mode"] = "lumped_rlc"
        config["sweeps"]["capacitor.cap_pitch"].update(enabled=True, start=0.5, stop=0.7, step=0.1)
        normalized, _ = normalize_config(config)
        plan = inspect_sweep_plan(normalized)
        self.assertTrue(any(level == "error" and "逐点Validate" in message
                            for level, message in plan.issues))


if __name__ == "__main__":
    unittest.main()
