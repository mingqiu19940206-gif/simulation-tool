from __future__ import annotations

import copy
import math
import subprocess
import sys
import tempfile
import unittest

from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from config_manager import ConfigManager, normalize_config, validate_config
from geometry_calculator import GeometryCalculator
from hfss_script_generator import HfssScriptGenerator
from simulation_sweep_semantics import preflight_sweep_geometry


class PadEscapeGeometryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.default = ConfigManager(".").default()

    def geometry(self, **updates):
        config = copy.deepcopy(self.default)
        for path, value in updates.items():
            section, field = path.split("__", 1)
            config[section][field] = value
        normalized, validation = validate_config(config, check_output_directory=False)
        self.assertTrue(validation.is_valid, validation.errors)
        geometry = GeometryCalculator(normalized).calculate()
        self.assertFalse(geometry.errors, geometry.errors)
        return normalized, geometry

    def test_default_direct_45_has_no_pad_shoulder_and_zero_leads_are_legal(self):
        config, geometry = self.geometry()
        self.assertEqual(config["trace"]["pad_escape_mode"], "direct_45")
        self.assertEqual(config["trace"]["pad_lead_in_length"], 0.0)
        path = geometry.trace_centerline_paths["P"]["input"]
        connection = geometry.pad_connection_points["P_IN"]
        index = min(range(len(path)), key=lambda i: math.dist(
            (path[i].x, path[i].y), (connection.x, connection.y)))
        self.assertLess(math.dist((path[index].x, path[index].y),
                                  (connection.x, connection.y)), 1e-10)
        before = path[index - 1]
        self.assertAlmostEqual(abs(connection.x - before.x),
                               abs(connection.y - before.y), places=9)
        self.assertNotAlmostEqual(connection.y, before.y, places=9)

    def test_input_output_connect_outer_short_edge_centres_with_positive_area(self):
        _config, geometry = self.geometry()
        for signal in ("P", "N"):
            for side, category in (("IN", "trace_in"), ("OUT", "trace_out")):
                connection = geometry.pad_connection_points[f"{signal}_{side}"]
                path = geometry.trace_centerline_paths[signal][side.lower().replace("in", "input").replace("out", "output")]
                self.assertTrue(any(math.dist((point.x, point.y),
                                              (connection.x, connection.y)) < 1e-10
                                    for point in path))
                trace = next(shape for shape in geometry.signal_shapes[signal]
                             if shape.category == category)
                pad = geometry.pad_shapes[f"{signal}_{side}"]
                trace_poly = Polygon([(point.x, point.y) for point in trace.points])
                pad_poly = Polygon([(point.x, point.y) for point in pad.points])
                self.assertGreater(trace_poly.intersection(pad_poly).area, 1e-12)

    def test_contract_equal_expand_are_mirrored_equal_length_and_non_degenerate(self):
        for cap_pitch, pad_width in ((0.6, 0.5), (0.2, 0.08), (0.14, 0.06)):
            with self.subTest(cap_pitch=cap_pitch):
                _config, geometry = self.geometry(capacitor__cap_pitch=cap_pitch,
                                                  capacitor__pad_width=pad_width)
                for side in ("input", "output"):
                    positive = geometry.trace_centerline_paths["P"][side]
                    negative = geometry.trace_centerline_paths["N"][side]
                    self.assertEqual(len(positive), len(negative))
                    self.assertTrue(all(math.isclose(p.x, n.x, abs_tol=1e-9)
                                        and math.isclose(p.y, -n.y, abs_tol=1e-9)
                                        for p, n in zip(positive, negative)))
                    p_len = sum(math.dist((a.x, a.y), (b.x, b.y))
                                for a, b in zip(positive, positive[1:]))
                    n_len = sum(math.dist((a.x, a.y), (b.x, b.y))
                                for a, b in zip(negative, negative[1:]))
                    self.assertAlmostEqual(p_len, n_len, places=9)
                    self.assertTrue(all(math.dist((a.x, a.y), (b.x, b.y)) > 1e-9
                                        for a, b in zip(positive, positive[1:])))

    def test_smooth_s_bend_has_endpoint_g1_tangency_and_constant_width_polygon(self):
        config, geometry = self.geometry(trace__pad_escape_mode="smooth_s_bend",
                                         trace__transition_length=0.8)
        for signal in ("P", "N"):
            for side in ("input", "output"):
                path = geometry.trace_centerline_paths[signal][side]
                if side == "input":
                    horizontal = (path[1].x-path[0].x, path[1].y-path[0].y)
                    pad_tangent = (path[-1].x-path[-2].x, path[-1].y-path[-2].y)
                else:
                    pad_tangent = (path[1].x-path[0].x, path[1].y-path[0].y)
                    horizontal = (path[-1].x-path[-2].x, path[-1].y-path[-2].y)
                self.assertAlmostEqual(horizontal[1], 0.0, places=7)
                self.assertAlmostEqual(pad_tangent[1], 0.0, places=7)
                shape = next(s for s in geometry.signal_shapes[signal]
                             if s.category == ("trace_in" if side == "input" else "trace_out"))
                polygon = Polygon([(point.x, point.y) for point in shape.points])
                buffered = LineString([(point.x, point.y) for point in shape.centerline]).buffer(
                    config["trace"]["trace_width"] / 2.0, cap_style="flat", join_style="mitre",
                    mitre_limit=config["geometry"]["miter_limit"])
                self.assertTrue(polygon.is_valid and polygon.buffer(1e-9).covers(buffered))

    def test_rotated_pad_uses_rotated_short_edge_and_smooth_local_tangent(self):
        config, geometry = self.geometry(trace__pad_escape_mode="smooth_s_bend",
                                         trace__transition_length=0.8,
                                         capacitor__pad_rotation=10.0,
                                         capacitor__cap_pitch=1.2)
        connection = geometry.pad_connection_points["P_IN"]
        pad_center = geometry.pad_centers["P_IN"]
        outward = (connection.x-pad_center.x, connection.y-pad_center.y)
        expected = (-math.cos(math.radians(10)), -math.sin(math.radians(10)))
        scale = math.hypot(*outward)
        self.assertAlmostEqual(outward[0]/scale, expected[0], places=8)
        self.assertAlmostEqual(outward[1]/scale, expected[1], places=8)
        path = geometry.trace_centerline_paths["P"]["input"]
        terminal = (path[-1].x-path[-2].x, path[-1].y-path[-2].y)
        terminal_scale = math.hypot(*terminal)
        self.assertAlmostEqual(terminal[0]/terminal_scale, math.cos(math.radians(10)), places=6)
        self.assertAlmostEqual(terminal[1]/terminal_scale, math.sin(math.radians(10)), places=6)

    def test_transition_direction_conflicts_fail_validation(self):
        for cap_pitch, direction in ((0.6, "expand"), (0.14, "contract")):
            config = copy.deepcopy(self.default)
            config["capacitor"]["cap_pitch"] = cap_pitch
            config["trace"]["transition_direction"] = direction
            _normalized, validation = validate_config(config, check_output_directory=False)
            self.assertFalse(validation.is_valid)
            self.assertTrue(any("矛盾" in message for message in validation.errors))

    def test_preview_snapshot_and_hfss_share_paths_and_mode(self):
        config, geometry = self.geometry()
        manifest = geometry.to_dict()
        self.assertEqual(manifest["trace_centerline_paths"]["P"]["input"],
                         [point.to_list() for point in geometry.trace_centerline_paths["P"]["input"]])
        script = HfssScriptGenerator.build_script_text(config, geometry)
        self.assertIn('pad_escape_mode", "direct_45"', script)
        self.assertIn("_parametric_centerline_for_shape", script)

    def test_native_min_nominal_max_sweeps_preflight_without_topology_fallback(self):
        axes = {
            "trace.trace_width": (0.08, 0.1, 0.12),
            "trace.trace_pitch": (0.18, 0.2, 0.3),
            "capacitor.cap_pitch": (0.8, 1.0, 1.2),
            "capacitor.pad_rotation": (-10.0, 0.0, 10.0),
            "trace.transition_length": (0.65, 0.8, 1.0),
        }
        for path, values in axes.items():
            with self.subTest(path=path):
                config = copy.deepcopy(self.default)
                config["trace"]["pad_escape_mode"] = "smooth_s_bend"
                config["trace"]["transition_length"] = 0.8
                config["capacitor"]["cap_pitch"] = 1.2
                config["sweeps"][path] = {"enabled": True, "values": list(values)}
                report = preflight_sweep_geometry(config, sample_limit=10)
                self.assertFalse(report.errors, report.errors)

    def test_old_config_migrates_to_direct_45_and_compatibility_mode_keeps_lead(self):
        old = copy.deepcopy(self.default)
        old["trace"].pop("pad_escape_mode")
        old["trace"]["pad_lead_in_length"] = 0.2
        migrated, corrections = normalize_config(old)
        self.assertEqual(migrated["trace"]["pad_escape_mode"], "direct_45")
        self.assertTrue(any("direct_45" in message for message in corrections))
        old["trace"]["pad_escape_mode"] = "lead_then_45"
        config, geometry = self.geometry(trace__pad_escape_mode="lead_then_45",
                                         trace__pad_lead_in_length=0.2,
                                         trace__pad_lead_out_length=0.15)
        path = geometry.trace_centerline_paths["P"]["input"]
        connection_index = min(range(len(path)), key=lambda i: math.dist(
            (path[i].x, path[i].y),
            (geometry.pad_connection_points["P_IN"].x,
             geometry.pad_connection_points["P_IN"].y)))
        lead_start = path[connection_index-1]
        connection = geometry.pad_connection_points["P_IN"]
        self.assertAlmostEqual(math.dist((lead_start.x, lead_start.y),
                                         (connection.x, connection.y)), 0.2, places=9)

    def test_generated_scripts_validate_for_direct_smooth_and_zero_lead_compat(self):
        cases = []
        direct = copy.deepcopy(self.default)
        cases.append(direct)
        smooth = copy.deepcopy(self.default)
        smooth["project"]["project_name"] = "PAD_ESCAPE_SMOOTH_ROTATED"
        smooth["trace"]["pad_escape_mode"] = "smooth_s_bend"
        smooth["trace"]["transition_length"] = 0.8
        smooth["capacitor"]["pad_rotation"] = 10.0
        smooth["capacitor"]["cap_pitch"] = 1.2
        cases.append(smooth)
        compatibility = copy.deepcopy(self.default)
        compatibility["project"]["project_name"] = "PAD_ESCAPE_COMPAT_ZERO"
        compatibility["trace"]["pad_escape_mode"] = "lead_then_45"
        compatibility["trace"]["pad_lead_in_length"] = 0.0
        compatibility["trace"]["pad_lead_out_length"] = 0.0
        cases.append(compatibility)
        with tempfile.TemporaryDirectory() as directory:
            for config in cases:
                config["project"]["overwrite_existing"] = True
                generated = HfssScriptGenerator(config, ".").generate(directory)
                completed = subprocess.run(
                    [sys.executable, str(generated.script_path), "--validate-only"],
                    capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(completed.returncode, 0,
                                 completed.stdout + completed.stderr)

    def test_rotated_native_sweep_parameterizes_trace_pad_plane_and_gnd_vias(self):
        config = copy.deepcopy(self.default)
        config["trace"]["pad_escape_mode"] = "smooth_s_bend"
        config["trace"]["transition_length"] = 0.8
        config["capacitor"]["cap_pitch"] = 1.2
        config["sweeps"]["capacitor.pad_rotation"] = {
            "enabled": True, "values": [-10.0, 0.0, 10.0]
        }
        script = HfssScriptGenerator.build_script_text(
            config, GeometryCalculator(config).calculate()
        )
        self.assertIn('"capacitor.pad_rotation": "pad_rotation"', script)
        self.assertIn("cos(pad_rotation*pi/180)", script)
        self.assertIn("sin(pad_rotation*pi/180)", script)
        self.assertIn("gnd_via_clearance_x", script)
        self.assertIn("cap_center_x_offset", script)


if __name__ == "__main__":
    unittest.main(verbosity=2)
