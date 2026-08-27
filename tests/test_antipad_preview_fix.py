from __future__ import annotations

import copy
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from antipad_editor import (
    connected_window_count, expansion_dimension_points, union_boundary_segments,
)
from config_manager import ConfigManager
from geometry_calculator import GeometryCalculator


class AntipadPreviewFixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.default = ConfigManager(ROOT).default()

    def geometry_for(self, mode: str, expansion: float = 0.2):
        config = copy.deepcopy(self.default)
        config["antipad"]["mode"] = mode
        config["antipad"]["global_expansion"] = expansion
        return config, GeometryCalculator(config).calculate()

    def test_default_tools_report_actual_connected_opening(self) -> None:
        expected_tools = {"independent_pad": 4, "merged_per_capacitor": 2, "merged_all": 1}
        for mode, tool_count in expected_tools.items():
            _config, geometry = self.geometry_for(mode)
            tools = geometry.antipads_by_layer["L2"]
            self.assertEqual(len(tools), tool_count)
            self.assertEqual(connected_window_count(tools), 1)

    def test_separated_geometry_keeps_mode_specific_connected_counts(self) -> None:
        expected = {"independent_pad": 4, "merged_per_capacitor": 2, "merged_all": 1}
        for mode, connected in expected.items():
            _config, geometry = self.geometry_for(mode, expansion=0.01)
            self.assertEqual(connected_window_count(geometry.antipads_by_layer["L2"]), connected)

    def test_union_boundary_removes_internal_cross_lines(self) -> None:
        _config, geometry = self.geometry_for("independent_pad")
        tools = geometry.antipads_by_layer["L2"]
        segments = union_boundary_segments([tool.points for tool in tools])
        self.assertEqual(len(segments), 4)
        endpoints = {tuple(round(value, 6) for value in point) for segment in segments for point in segment}
        self.assertEqual(endpoints, {(-0.8, -0.75), (-0.8, 0.75), (0.8, -0.75), (0.8, 0.75)})

    def test_independent_dimension_uses_outer_rotated_pad_edge(self) -> None:
        config = copy.deepcopy(self.default)
        config["antipad"]["mode"] = "independent_pad"
        config["capacitor"]["pad_rotation"] = 17.0
        geometry = GeometryCalculator(config).calculate()
        tools = geometry.antipads_by_layer["L2"]
        dimension = expansion_dimension_points(geometry, tools, "independent_pad")
        self.assertIsNotNone(dimension)
        first, second, expansion = dimension
        self.assertAlmostEqual(math.dist(first, second), expansion, places=9)
        self.assertGreater(second[0], first[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
