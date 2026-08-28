from __future__ import annotations

import copy
import json
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from capacitor_editor import ENUM_LABELS
from config_manager import (
    ConfigManager,
    find_nearest_reference_plane,
    load_config,
    normalize_config,
    save_config,
    to_external_config,
    validate_config,
)
from geometry_calculator import GeometryCalculator
from hfss_script_generator import HfssScriptGenerator
from main_gui import SurfaceCapAntipadApp
from simulation_sweep_semantics import (
    aedt_sweep_data,
    apply_sweep_value,
    get_sweep_parameter_spec,
    hfss_variable_name,
    inspect_sweep_plan,
    preflight_sweep_geometry,
    scan_values,
)


class SignedViaOffsetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.default = ConfigManager(ROOT).default()

    def enabled(self) -> dict:
        config = copy.deepcopy(self.default)
        config["gnd_vias"]["enabled"] = True
        return config

    @staticmethod
    def coordinates(geometry) -> set[tuple[float, float]]:
        return {(round(via.x, 9), round(via.y, 9)) for via in geometry.gnd_vias}

    def test_negative_clearance_x_is_valid_without_collision_and_coordinates_are_signed(self) -> None:
        config = self.enabled()
        config["gnd_vias"].update(clearance_x=-0.05, clearance_y=0.35)
        normalized, validation = validate_config(config, ROOT, check_output_directory=False)
        geometry = GeometryCalculator(normalized).calculate()
        self.assertTrue(validation.is_valid, validation.errors)
        self.assertFalse(any("侵入信号铜皮" in message for message in geometry.errors))
        self.assertEqual(
            self.coordinates(geometry),
            {(-0.75, -1.1), (-0.75, 1.1), (0.75, -1.1), (0.75, 1.1)},
        )

    def test_negative_clearance_y_is_valid_without_collision(self) -> None:
        config = self.enabled()
        config["gnd_vias"].update(clearance_x=0.35, clearance_y=-0.05)
        normalized, validation = validate_config(config, ROOT, check_output_directory=False)
        geometry = GeometryCalculator(normalized).calculate()
        self.assertTrue(validation.is_valid, validation.errors)
        self.assertFalse(any("侵入信号铜皮" in message for message in geometry.errors))
        self.assertEqual(
            self.coordinates(geometry),
            {(-1.15, -0.7), (-1.15, 0.7), (1.15, -0.7), (1.15, 0.7)},
        )

    def test_negative_offsets_still_report_real_signal_copper_collision(self) -> None:
        config = self.enabled()
        config["gnd_vias"].update(clearance_x=-0.1, clearance_y=-0.1)
        normalized, validation = validate_config(config, ROOT, check_output_directory=False)
        geometry = GeometryCalculator(normalized).calculate()
        self.assertFalse(validation.is_valid)
        self.assertTrue(any("侵入信号铜皮" in message for message in validation.errors))
        self.assertTrue(any("侵入信号铜皮" in message for message in geometry.errors))

    def test_positive_and_zero_offsets_regress_without_false_antipad_collision(self) -> None:
        expected = {
            0.0: {(-0.8, -0.75), (-0.8, 0.75), (0.8, -0.75), (0.8, 0.75)},
            0.35: {(-1.15, -1.1), (-1.15, 1.1), (1.15, -1.1), (1.15, 1.1)},
        }
        for value, coordinates in expected.items():
            with self.subTest(value=value):
                config = self.enabled()
                config["gnd_vias"].update(clearance_x=value, clearance_y=value)
                normalized, validation = validate_config(
                    config, ROOT, check_output_directory=False
                )
                self.assertTrue(validation.is_valid, validation.errors)
                self.assertEqual(
                    self.coordinates(GeometryCalculator(normalized).calculate()), coordinates
                )

    def test_negative_offset_scan_validates_and_emits_native_hfss_variable(self) -> None:
        config = self.enabled()
        config["gnd_vias"]["clearance_x"] = -0.05
        sweep = config["sweeps"]["gnd_vias.clearance_x"]
        sweep.update(enabled=True, start=-0.1, stop=0.1, step=0.1)
        normalized, validation = validate_config(config, ROOT, check_output_directory=False)
        self.assertTrue(validation.is_valid, validation.errors)
        spec = get_sweep_parameter_spec("gnd_vias.clearance_x")
        self.assertIsNone(spec.minimum)
        self.assertEqual(scan_values("gnd_vias.clearance_x", sweep), [-0.1, 0.0, 0.1])
        self.assertEqual(
            aedt_sweep_data("gnd_vias.clearance_x", sweep),
            "LIN -0.1mm 0.1mm 0.1mm",
        )
        plan = inspect_sweep_plan(normalized)
        self.assertFalse([message for level, message in plan.issues if level == "error"])
        self.assertEqual(plan.native_axes[0][0], "gnd_vias.clearance_x")
        self.assertEqual(
            hfss_variable_name("gnd_vias.clearance_x", normalized),
            "gnd_via_clearance_x",
        )
        preflight = preflight_sweep_geometry(normalized)
        self.assertFalse(preflight.errors, preflight.errors)
        geometry = GeometryCalculator(normalized).calculate()
        script = HfssScriptGenerator.build_script_text(normalized, geometry)
        self.assertIn('"gnd_vias.clearance_x": "gnd_via_clearance_x"', script)
        self.assertIn('"gnd_via_clearance_x": length(vias.get("clearance_x", 0.0))', script)

    def test_other_via_dimensions_remain_strictly_positive(self) -> None:
        for key in ("pitch_x", "pitch_y", "drill_diameter", "pad_diameter", "antipad_diameter"):
            with self.subTest(key=key):
                config = self.enabled()
                config["gnd_vias"][key] = -0.01
                _, validation = validate_config(config, ROOT, check_output_directory=False)
                self.assertFalse(validation.is_valid)
                self.assertTrue(any(f"gnd_vias.{key}" in message for message in validation.errors))


class AntipadMountFollowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.default = ConfigManager(ROOT).default()

    @staticmethod
    def enabled_layers(config: dict) -> list[str]:
        return [str(layer["name"]) for layer in config["stackup"] if layer.get("enable_antipad")]

    def switch(self, config: dict, side: str) -> dict:
        updated = copy.deepcopy(config)
        updated["capacitor"]["mount_side"] = side
        return normalize_config(updated)[0]

    def test_top_and_bottom_choose_l2_and_l13(self) -> None:
        top = self.switch(self.default, "top")
        bottom = self.switch(top, "bottom")
        self.assertEqual(find_nearest_reference_plane(top["stackup"], "top")["name"], "L2")
        self.assertEqual(top["antipad"]["auto_follow_layer"], "L2")
        self.assertEqual(self.enabled_layers(top), ["L2"])
        self.assertEqual(find_nearest_reference_plane(bottom["stackup"], "bottom")["name"], "L13")
        self.assertEqual(bottom["antipad"]["auto_follow_layer"], "L13")
        self.assertEqual(self.enabled_layers(bottom), ["L13"])
        geometry = GeometryCalculator(bottom).calculate()
        self.assertEqual(geometry.reference_layer_name, "L13")
        self.assertEqual(set(geometry.antipads_by_layer), {"L13"})

    def test_top_bottom_top_switch_is_stable(self) -> None:
        first_top = self.switch(self.default, "top")
        bottom = self.switch(first_top, "bottom")
        final_top = self.switch(bottom, "top")
        self.assertEqual(self.enabled_layers(bottom), ["L13"])
        self.assertEqual(self.enabled_layers(final_top), ["L2"])
        self.assertEqual(
            GeometryCalculator(first_top).calculate().to_dict(),
            GeometryCalculator(final_top).calculate().to_dict(),
        )

    def test_disabling_follow_preserves_manual_layers_across_side_switch(self) -> None:
        config = copy.deepcopy(self.default)
        next(layer for layer in config["stackup"] if layer["name"] == "L4")["enable_antipad"] = True
        config["antipad"]["follow_mount_side"] = False
        config = normalize_config(config)[0]
        before = self.enabled_layers(config)
        bottom = self.switch(config, "bottom")
        self.assertEqual(before, ["L2", "L4"])
        self.assertEqual(self.enabled_layers(bottom), before)
        self.assertEqual(bottom["antipad"]["auto_follow_layer"], "")

    def test_extra_manual_reference_layer_is_not_cleared_by_follow(self) -> None:
        config = copy.deepcopy(self.default)
        next(layer for layer in config["stackup"] if layer["name"] == "L4")["enable_antipad"] = True
        bottom = self.switch(config, "bottom")
        final_top = self.switch(bottom, "top")
        self.assertEqual(self.enabled_layers(bottom), ["L4", "L13"])
        self.assertEqual(self.enabled_layers(final_top), ["L2", "L4"])

    def test_bottom_nearest_reference_sweep_drives_l13_not_l2(self) -> None:
        config = self.switch(self.default, "bottom")
        config["sweeps"]["antipad.first_reference_expansion"].update(
            enabled=True, start=0.1, stop=0.3, step=0.1
        )
        self.assertEqual(
            hfss_variable_name("antipad.first_reference_expansion", config),
            "antipad_expansion_L13",
        )
        candidate = copy.deepcopy(config)
        apply_sweep_value(candidate, "antipad.first_reference_expansion", 0.27)
        overrides = {
            layer["name"]: layer.get("antipad_expansion_override")
            for layer in candidate["stackup"]
        }
        self.assertEqual(overrides["L13"], 0.27)
        self.assertIsNone(overrides["L2"])
        geometry = GeometryCalculator(config).calculate()
        script = HfssScriptGenerator.build_script_text(config, geometry)
        self.assertEqual(geometry.reference_layer_name, "L13")
        self.assertIn('first_reference_name = str(GEOMETRY.get("reference_layer_name", ""))', script)
        self.assertIn('target_name = str(GEOMETRY.get("reference_layer_name", ""))', script)

    def test_old_v2_multilayer_config_migrates_and_round_trips_without_loss(self) -> None:
        old = to_external_config(self.default)
        old["capacitor"]["mount_side"] = "bottom"
        old["antipad"].pop("follow_mount_side")
        old["antipad"].pop("auto_follow_layer")
        next(layer for layer in old["stackup"] if layer["name"] == "L4")["enable_antipad"] = True
        with tempfile.TemporaryDirectory() as directory:
            old_path = Path(directory) / "old_v2.json"
            saved_path = Path(directory) / "saved_v2.json"
            old_path.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
            loaded, corrections = load_config(old_path)
            self.assertTrue(any("最近参考层 L13" in message for message in corrections))
            self.assertEqual(self.enabled_layers(loaded), ["L2", "L4", "L13"])
            self.assertTrue(loaded["antipad"]["follow_mount_side"])
            self.assertEqual(loaded["antipad"]["auto_follow_layer"], "L13")
            save_config(loaded, saved_path)
            reloaded, _ = load_config(saved_path)
            self.assertEqual(to_external_config(loaded), to_external_config(reloaded))

    def test_old_bottom_single_stale_default_migrates_from_l2_to_l13(self) -> None:
        old = to_external_config(self.default)
        old["capacitor"]["mount_side"] = "bottom"
        old["antipad"].pop("follow_mount_side")
        old["antipad"].pop("auto_follow_layer")
        migrated, corrections = normalize_config(old)
        self.assertEqual(self.enabled_layers(migrated), ["L13"])
        self.assertTrue(any("L2" in message and "L13" in message for message in corrections))

    def test_missing_inward_reference_plane_is_an_explicit_error(self) -> None:
        config = copy.deepcopy(self.default)
        config["capacitor"]["mount_side"] = "bottom"
        for layer in config["stackup"]:
            layer["is_reference_plane"] = False
            layer["enable_antipad"] = False
        _, validation = validate_config(config, ROOT, check_output_directory=False)
        self.assertFalse(validation.is_valid)
        self.assertTrue(any("找不到有效参考 plane" in message for message in validation.errors))

    def test_gui_switch_keeps_table_2d_3d_and_selected_layer_in_sync(self) -> None:
        try:
            app = SurfaceCapAntipadApp()
            app.withdraw()
            app.update_idletasks()
        except tk.TclError as exc:
            self.skipTest(f"Tk GUI unavailable: {exc}")

        def show(side: str) -> dict:
            app.variables["capacitor.mount_side"].set(
                ENUM_LABELS["capacitor.mount_side"][side]
            )
            app.refresh_visuals()
            app.model_preview_workbench.update_pending()
            return copy.deepcopy(app._preview_snapshot["geometry_snapshot"])

        try:
            first_top = show("top")
            bottom = show("bottom")
            self.assertEqual(app.config["antipad"]["auto_follow_layer"], "L13")
            self.assertEqual(self.enabled_layers(app.config), ["L13"])
            self.assertEqual(app.antipad_workbench.preview.layer_var.get(), "L13")
            selected = app.antipad_workbench.tree.selection()
            self.assertTrue(selected)
            self.assertEqual(app.config["stackup"][int(selected[0])]["name"], "L13")
            row = app.antipad_workbench.tree.item(selected[0], "values")
            self.assertEqual(row[7], "是")
            self.assertEqual(set(bottom["antipads_by_layer"]), {"L13"})
            self.assertEqual(bottom["reference_layer_name"], "L13")
            self.assertEqual(
                app.model_preview_workbench.current_snapshot["geometry_hash"],
                app._preview_snapshot["geometry_hash"],
            )

            final_top = show("top")
            self.assertEqual(app.config["antipad"]["auto_follow_layer"], "L2")
            self.assertEqual(app.antipad_workbench.preview.layer_var.get(), "L2")
            self.assertEqual(first_top, final_top)
        finally:
            try:
                for after_id in app.tk.call("after", "info"):
                    app.after_cancel(after_id)
            except tk.TclError:
                pass
            app.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
