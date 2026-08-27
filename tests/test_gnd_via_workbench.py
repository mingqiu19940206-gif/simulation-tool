from __future__ import annotations

import copy
import subprocess
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config_manager import ConfigManager, normalize_config, validate_config
from geometry_calculator import GeometryCalculator
from gnd_via_semantics import active_layout_fields, derive_via_values, layer_statuses
from hfss_script_generator import HfssScriptGenerator


class GndViaWorkbenchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.default = ConfigManager(ROOT).default()

    def enabled(self) -> dict:
        config = copy.deepcopy(self.default)
        config["gnd_vias"]["enabled"] = True
        return config

    def test_mode_specific_fields_match_geometry_contract(self) -> None:
        self.assertEqual(active_layout_fields("four_corner"), {"count", "clearance_x", "clearance_y"})
        self.assertEqual(active_layout_fields("side_pair", "top_bottom"),
                         {"count", "side_orientation", "clearance_y", "pitch_x"})
        self.assertEqual(active_layout_fields("side_pair", "left_right"),
                         {"count", "side_orientation", "clearance_x", "pitch_y"})
        self.assertEqual(active_layout_fields("rectangular_fence"),
                         {"count", "clearance_x", "clearance_y", "pitch_x", "pitch_y"})
        self.assertEqual(active_layout_fields("custom"), {"count"})

    def test_finished_hole_is_derived_and_old_json_reports_migration(self) -> None:
        config = copy.deepcopy(self.default)
        config["gnd_vias"]["drill_diameter"] = 0.25
        config["gnd_vias"]["plating_thickness"] = 0.03
        config["gnd_vias"]["finished_hole_diameter"] = 0.12
        normalized, corrections = normalize_config(config)
        self.assertAlmostEqual(normalized["gnd_vias"]["finished_hole_diameter"], 0.19)
        self.assertTrue(any("成品孔直径" in message and "更新" in message for message in corrections))

    def test_four_corner_and_custom_counts_are_authoritative(self) -> None:
        four = self.enabled()
        four["gnd_vias"]["pattern"] = "four_corner"
        four["gnd_vias"]["count"] = 99
        normalized, _ = normalize_config(four)
        self.assertEqual(normalized["gnd_vias"]["count"], 4)
        custom = self.enabled()
        custom["gnd_vias"]["pattern"] = "custom"
        custom["gnd_vias"]["count"] = 20
        custom["gnd_vias"]["custom_coordinates"] = [[-1.0, 1.0], [1.0, 1.0], [0.0, -1.0]]
        normalized, _ = normalize_config(custom)
        self.assertEqual(normalized["gnd_vias"]["count"], 3)

    def test_duplicate_custom_coordinates_are_rejected(self) -> None:
        config = self.enabled()
        config["gnd_vias"]["pattern"] = "custom"
        config["gnd_vias"]["custom_coordinates"] = [[-1.0, 1.0], [-1.0, 1.0]]
        _, result = validate_config(config, ROOT, check_output_directory=False)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("坐标不能重复" in message for message in result.errors))

    def test_connected_clearance_and_outside_layers_are_classified(self) -> None:
        config = self.enabled()
        config["gnd_vias"]["connect_layers"] = ["L2"]
        statuses = {item.name: item.status for item in layer_statuses(config)}
        self.assertEqual(statuses["L2"], "connected")
        self.assertEqual(statuses["L4"], "clearance")
        self.assertEqual(statuses["DIEL_L1_L2"], "traversed")

    def test_connection_layer_must_be_inside_span(self) -> None:
        config = self.enabled()
        config["gnd_vias"]["start_layer"] = "L2"
        config["gnd_vias"]["end_layer"] = "L4"
        config["gnd_vias"]["connect_layers"] = ["L2", "L1"]
        _, result = validate_config(config, ROOT, check_output_directory=False)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("L1" in message for message in result.errors))

    def test_geometry_serializes_nonconnected_plane_clearance(self) -> None:
        config = self.enabled()
        config["gnd_vias"]["connect_layers"] = ["L2"]
        geometry = GeometryCalculator(config).calculate()
        self.assertEqual(len(geometry.gnd_vias), 4)
        self.assertEqual(geometry.gnd_vias[0].clearance_layers,
                         ("L4", "L6", "L9", "L11", "L13"))
        self.assertIn("L4", geometry.gnd_vias[0].traversed_layers)
        serialized = geometry.to_dict()["gnd_vias"][0]
        self.assertEqual(serialized["clearance_layers"],
                         ["L4", "L6", "L9", "L11", "L13"])

    def test_fence_and_left_right_side_pair_do_not_land_on_long_signal_traces(self) -> None:
        cases = (("rectangular_fence", 8, "top_bottom"),
                 ("side_pair", 4, "left_right"))
        for pattern, count, orientation in cases:
            config = self.enabled()
            config["gnd_vias"].update({"pattern": pattern, "count": count,
                                       "side_orientation": orientation})
            geometry = GeometryCalculator(config).calculate()
            intrusions = [message for message in geometry.errors if "侵入信号铜皮" in message]
            self.assertEqual(intrusions, [], (pattern, intrusions))

    def test_manufacturing_derivatives_use_one_authoritative_relationship(self) -> None:
        config = self.enabled()
        geometry = GeometryCalculator(config).calculate()
        derived = derive_via_values(config, geometry)
        self.assertAlmostEqual(derived.annular_ring, 0.1)
        self.assertAlmostEqual(derived.wall_thickness, 0.025)
        self.assertAlmostEqual(derived.antipad_clearance, 0.1)
        self.assertAlmostEqual(derived.aspect_ratio,
                               derived.span_thickness / config["gnd_vias"]["drill_diameter"])

    def test_hfss_script_contains_real_via_antipad_subtraction_and_validates(self) -> None:
        config = self.enabled()
        config["gnd_vias"]["connect_layers"] = ["L2"]
        normalized, validation = validate_config(config, ROOT, check_output_directory=False)
        self.assertTrue(validation.is_valid, validation.errors)
        geometry = GeometryCalculator(normalized).calculate()
        script = HfssScriptGenerator.build_script_text(normalized, geometry)
        self.assertIn('for layer_name in via.get("clearance_layers", [])', script)
        self.assertIn('"gnd_via_antipad_diameter/2"', script)
        self.assertIn('state["via_antipad_tools"]', script)
        with tempfile.TemporaryDirectory() as directory:
            normalized["project"]["overwrite_existing"] = True
            generated = HfssScriptGenerator(normalized, ROOT).generate(Path(directory))
            completed = subprocess.run([sys.executable, str(generated.script_path), "--validate-only"],
                                       capture_output=True, text=True, timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_hollow_solid_and_top_ground_modes_remain_serialized(self) -> None:
        for barrel in ("hollow", "solid"):
            config = self.enabled()
            config["gnd_vias"]["barrel_model"] = barrel
            config["gnd_vias"]["create_top_ground_copper"] = True
            normalized, validation = validate_config(config, ROOT, check_output_directory=False)
            self.assertTrue(validation.is_valid, validation.errors)
            geometry = GeometryCalculator(normalized).calculate()
            self.assertTrue(all(via.barrel_model == barrel for via in geometry.gnd_vias))
            script = HfssScriptGenerator.build_script_text(normalized, geometry)
            self.assertIn('if via.get("barrel_model") == "hollow"', script)
            self.assertIn('preview = GEOMETRY.get("top_ground")', script)
            self.assertIn('for keepout in preview["keepouts"]', script)

    def test_disabled_vias_keep_actual_count_zero(self) -> None:
        config = copy.deepcopy(self.default)
        config["gnd_vias"]["enabled"] = False
        geometry = GeometryCalculator(config).calculate()
        derived = derive_via_values(config, geometry)
        self.assertEqual(derived.actual_count, 0)
        self.assertEqual(geometry.gnd_vias, [])

    def test_disabled_empty_custom_layout_does_not_block_v1_geometry(self) -> None:
        config = copy.deepcopy(self.default)
        config["gnd_vias"].update({"enabled": False, "pattern": "custom",
                                   "count": 12, "custom_coordinates": [],
                                   "connect_layers": []})
        normalized, result = validate_config(config, ROOT, check_output_directory=False)
        self.assertTrue(result.is_valid, result.errors)
        self.assertEqual(normalized["gnd_vias"]["count"], 0)
        self.assertEqual(GeometryCalculator(normalized).calculate().gnd_vias, [])

    def test_gui_mode_linkage_and_disabled_state(self) -> None:
        from tkinter import font as tkfont
        from main_gui import SurfaceCapAntipadApp
        app = SurfaceCapAntipadApp()
        try:
            app.geometry("1900x1050")
            app.update()
            app._apply_responsive_fonts()
            self.assertEqual(app._responsive_font_tier, "large")
            # The responsive tier now applies a deliberately modest font increment;
            # canvas geometry is derived from the resulting real linespace instead
            # of relying on the old fixed 470 px preview height.
            self.assertGreaterEqual(tkfont.nametofont("TkDefaultFont").cget("size"), 11)
            preview_linespace = tkfont.nametofont("ViaCanvasFont").metrics("linespace")
            self.assertEqual(
                int(float(app.gnd_via_workbench.preview.cget("height"))),
                max(360, preview_linespace * 18 + 80),
            )
            self.assertGreater(
                int(float(app.gnd_via_workbench.cross_section.cget("height"))),
                29 * preview_linespace,
            )
            selector = app.gnd_via_workbench.mode_cards
            self.assertIn(selector.current_columns, {1, 2, 4})
            self.assertEqual(
                selector.SHORT_RULES,
                {
                    "four_corner": "固定 4 颗",
                    "side_pair": "偶数颗",
                    "rectangular_fence": "4 的倍数",
                    "custom": "坐标表派生",
                },
            )
            for widgets in selector.card_widgets.values():
                for widget in widgets:
                    if isinstance(widget, tk.Label):
                        self.assertNotIn("\n", str(widget.cget("text")))
                        self.assertEqual(int(float(widget.cget("wraplength"))), 0)
            self.assertEqual(
                selector.cards[selector.variable.get()].cget("highlightbackground"),
                "#2563eb",
            )
            app.variables["gnd_vias.enabled"].set(True)
            cases = (("four_corner", 4), ("side_pair", 6), ("rectangular_fence", 8))
            for pattern, count in cases:
                app.variables["gnd_vias.pattern"].set(pattern)
                app.variables["gnd_vias.count"].set(str(count))
                app.gnd_via_workbench.prepare_collect()
                config, validation = app.manager.validate(app._collect(), check_output_directory=False)
                geometry = GeometryCalculator(config).calculate()
                app.gnd_via_workbench.show(config, geometry, validation)
                self.assertEqual(len(geometry.gnd_vias), count)
                active = active_layout_fields(pattern, config["gnd_vias"]["side_orientation"])
                for name, widget in app.gnd_via_workbench.layout_widgets.items():
                    state = str(widget.cget("state"))
                    self.assertNotEqual(state == "disabled", name in active, (pattern, name, state))
            self.assertEqual(str(app.gnd_via_workbench.top_ground_clearance.cget("state")), "disabled")
            app.variables["gnd_vias.create_top_ground_copper"].set(True)
            config, validation = app.manager.validate(app._collect(), check_output_directory=False)
            geometry = GeometryCalculator(config).calculate()
            app.gnd_via_workbench.show(config, geometry, validation)
            self.assertEqual(str(app.gnd_via_workbench.top_ground_clearance.cget("state")), "normal")
            app.variables["gnd_vias.start_layer"].set("L2")
            config, validation = app.manager.validate(app._collect(), check_output_directory=False)
            geometry = GeometryCalculator(config).calculate()
            app.gnd_via_workbench.show(config, geometry, validation)
            expected_tail = tuple(layer["name"] for layer in config["stackup"]
                                  if config["stackup"].index(layer) >= 3)
            self.assertEqual(tuple(app.gnd_via_workbench.end_combo.cget("values")), expected_tail)
            app.variables["gnd_vias.start_layer"].set("L1")
            seed = GeometryCalculator(config).calculate().gnd_vias[:4]
            app.gnd_via_workbench._load_coordinates([[via.x, via.y] for via in seed])
            app.variables["gnd_vias.pattern"].set("custom")
            app.gnd_via_workbench.prepare_collect()
            config, validation = app.manager.validate(app._collect(), check_output_directory=False)
            geometry = GeometryCalculator(config).calculate()
            app.gnd_via_workbench.show(config, geometry, validation)
            self.assertEqual(config["gnd_vias"]["count"], 4)
            self.assertEqual(len(geometry.gnd_vias), 4)
            self.assertEqual(str(app.gnd_via_workbench.layout_widgets["clearance_x"].cget("state")), "disabled")
            self.assertEqual(str(app.gnd_via_workbench.custom_buttons[0].cget("state")), "normal")
            app.variables["gnd_vias.enabled"].set(False)
            config, validation = app.manager.validate(app._collect(), check_output_directory=False)
            geometry = GeometryCalculator(config).calculate()
            app.gnd_via_workbench.show(config, geometry, validation)
            self.assertEqual(len(geometry.gnd_vias), 0)
            self.assertEqual(str(app.gnd_via_workbench.layout_widgets["clearance_x"].cget("state")), "disabled")
            self.assertEqual(str(app.gnd_via_workbench.top_ground_enable.cget("state")), "disabled")
        finally:
            app.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
