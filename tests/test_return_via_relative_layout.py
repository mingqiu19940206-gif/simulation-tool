"""Return VIA standard relative layout and legacy compatibility regressions."""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from config_manager import ConfigManager, normalize_config
from geometry_calculator import GeometryCalculator


def via_config() -> dict:
    config = copy.deepcopy(ConfigManager(ROOT).default())
    config["scenario"]["type"] = "via_transition"
    config["via_transition"]["exit_layer"] = "L14"
    config["via_transition"]["backdrill"]["enabled"] = False
    return config


def xy(items) -> list[tuple[float, float]]:
    return [(item.x, item.y) for item in items]


class ReturnViaRelativeLayoutTests(unittest.TestCase):
    def test_standard_offsets_are_relative_to_p_and_n_signal_vias(self) -> None:
        for outward, expected_y in ((0.3, (0.7, -0.7)),
                                    (0.0, (0.4, -0.4)),
                                    (-0.15, (0.25, -0.25))):
            with self.subTest(outward=outward):
                config = via_config()
                rv = config["via_transition"]["return_vias"]
                rv["relative_offset_along_route"] = 0.25
                rv["relative_offset_outward"] = outward
                geometry = GeometryCalculator(config).calculate()
                self.assertEqual(xy(geometry.return_vias),
                                 [(0.25, expected_y[0]), (0.25, expected_y[1])])
                self.assertEqual(len(geometry.return_vias), 2)

    def test_route_y_uses_the_one_shared_rotation_chain(self) -> None:
        config_x = via_config()
        rv = config_x["via_transition"]["return_vias"]
        rv["relative_offset_along_route"] = 0.25
        rv["relative_offset_outward"] = 0.3
        config_y = copy.deepcopy(config_x)
        config_y["via_transition"]["route_axis"] = "y"
        geometry_x = GeometryCalculator(config_x).calculate()
        geometry_y = GeometryCalculator(config_y).calculate()
        for x_item, y_item in zip(geometry_x.return_vias, geometry_y.return_vias):
            self.assertAlmostEqual(y_item.x, -x_item.y)
            self.assertAlmostEqual(y_item.y, x_item.x)
        self.assertEqual(xy(geometry_y.return_vias), [(-0.7, 0.25), (0.7, 0.25)])

    def test_legacy_side_pair_count_two_migrates_without_moving_copper(self) -> None:
        old_as_advanced = via_config()
        old_rv = old_as_advanced["via_transition"]["return_vias"]
        old_rv.update({
            "layout_mode": "advanced", "pattern": "side_pair", "count": 2,
            "offset_x": 0.25, "offset_y": 1.05, "pitch_x": 8.0, "pitch_y": 9.0,
        })
        before = xy(GeometryCalculator(old_as_advanced).calculate().return_vias)

        legacy_payload = copy.deepcopy(old_as_advanced)
        legacy_rv = legacy_payload["via_transition"]["return_vias"]
        for key in ("layout_mode", "relative_offset_along_route",
                    "relative_offset_outward"):
            legacy_rv.pop(key, None)
        migrated, messages = normalize_config(legacy_payload)
        after = xy(GeometryCalculator(migrated).calculate().return_vias)

        self.assertEqual(after, before)
        self.assertEqual(migrated["via_transition"]["return_vias"]["layout_mode"],
                         "standard")
        self.assertAlmostEqual(
            migrated["via_transition"]["return_vias"]["relative_offset_along_route"],
            0.25)
        self.assertAlmostEqual(
            migrated["via_transition"]["return_vias"]["relative_offset_outward"],
            1.05 - 0.8 / 2.0)
        self.assertTrue(any("实际孔坐标不变" in message for message in messages))

    def test_standard_pitch_fields_do_not_affect_two_hole_geometry(self) -> None:
        first = via_config()
        second = copy.deepcopy(first)
        first["via_transition"]["return_vias"].update({"pitch_x": 0.01, "pitch_y": 0.02})
        second["via_transition"]["return_vias"].update({"pitch_x": 99.0, "pitch_y": 88.0})
        self.assertEqual(
            xy(GeometryCalculator(first).calculate().return_vias),
            xy(GeometryCalculator(second).calculate().return_vias),
        )

    def test_standard_layers_follow_signal_drill_and_actual_port_references(self) -> None:
        config = via_config()
        config["via_transition"]["return_vias"].update({
            "start_layer": "L3", "end_layer": "L10", "connect_layers": ["L4", "L6"]})
        normalized, _ = normalize_config(config)
        rv = normalized["via_transition"]["return_vias"]
        geometry = GeometryCalculator(normalized).calculate()
        self.assertEqual((rv["start_layer"], rv["end_layer"]), ("L1", "L14"))
        self.assertEqual(rv["connect_layers"], ["L2", "L13"])
        self.assertEqual({port.reference_layer for port in geometry.ports}, {"L2", "L13"})
        self.assertTrue(all(via.start_layer == "L1" and via.end_layer == "L14"
                            for via in geometry.return_vias))
        self.assertTrue(all(via.connect_layers == ("L2", "L13")
                            for via in geometry.return_vias))

    def test_legacy_four_corner_and_custom_round_trip_losslessly(self) -> None:
        for pattern, count, coordinates in (
            ("four_corner", 4, []),
            ("custom", 3, [[-1.0, 0.1], [0.2, 0.3], [1.1, -0.4]]),
        ):
            with self.subTest(pattern=pattern):
                legacy = via_config()
                rv = legacy["via_transition"]["return_vias"]
                for key in ("layout_mode", "relative_offset_along_route",
                            "relative_offset_outward"):
                    rv.pop(key, None)
                rv.update({
                    "pattern": pattern, "count": count, "offset_x": 1.25,
                    "offset_y": 1.5, "pitch_x": 0.77, "pitch_y": 0.88,
                    "custom_coordinates": copy.deepcopy(coordinates),
                    "start_layer": "L3", "end_layer": "L10",
                    "connect_layers": ["L4", "L9"],
                })
                normalized, _ = normalize_config(legacy)
                kept = normalized["via_transition"]["return_vias"]
                self.assertEqual(kept["layout_mode"], "advanced")
                for key in ("pattern", "count", "offset_x", "offset_y", "pitch_x",
                            "pitch_y", "custom_coordinates", "start_layer", "end_layer",
                            "connect_layers"):
                    self.assertEqual(kept[key], rv[key], key)

                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / f"{pattern}.json"
                    ConfigManager(ROOT).save(normalized, path)
                    loaded, _ = ConfigManager(ROOT).load(path)
                loaded_rv = loaded["via_transition"]["return_vias"]
                for key in ("layout_mode", "pattern", "count", "offset_x", "offset_y",
                            "pitch_x", "pitch_y", "custom_coordinates", "start_layer",
                            "end_layer", "connect_layers"):
                    self.assertEqual(loaded_rv[key], kept[key], key)


if __name__ == "__main__":
    unittest.main(verbosity=2)
