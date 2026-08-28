from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path
import tkinter as tk
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from capacitor_editor import ENUM_LABELS
from config_manager import ConfigError, ConfigManager, load_config, normalize_config, save_config, validate_config
from geometry_calculator import GeometryCalculator
from hfss_script_generator import HfssScriptGenerator
from main_gui import SurfaceCapAntipadApp
from model_preview import build_model_scene
from model_validator import ModelValidator


class MountSideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = ConfigManager(ROOT)
        self.default = self.manager.default()

    def geometry(self, side: str = "top", mode: str = "ideal_bridge"):
        config = copy.deepcopy(self.default)
        config["capacitor"]["mount_side"] = side
        config["capacitor_model"]["mode"] = mode
        if mode == "sparameter":
            config["capacitor_model"]["touchstone_file"] = "missing_for_geometry_only.s2p"
        normalized, _ = normalize_config(config)
        return normalized, GeometryCalculator(normalized).calculate()

    @staticmethod
    def xy_manifest(geometry):
        data = geometry.to_dict()
        return {
            key: data[key]
            for key in ("trace_centers", "capacitor_centers", "pad_centers",
                        "pad_connection_points", "transition_points", "trace_centerline_paths",
                        "model_bounds", "plane_bounds", "body_outlines")
        } | {
            "signals": {
                signal: [shape["points"] for shape in data["signal_shapes"][signal]]
                for signal in ("P", "N")
            },
            "pads": {name: shape["points"] for name, shape in data["pad_shapes"].items()},
            "bridges": {name: shape["points"] for name, shape in data["bridge_shapes"].items()},
            "models": {name: shape["points"] for name, shape in data["capacitor_model_shapes"].items()},
        }

    def test_01_old_config_defaults_to_top_and_invalid_value_is_rejected(self) -> None:
        old = copy.deepcopy(self.default)
        old["capacitor"].pop("mount_side")
        normalized, corrections = normalize_config(old)
        self.assertEqual(normalized["capacitor"]["mount_side"], "top")
        self.assertTrue(any("mount_side" in item and "top" in item for item in corrections))
        old["capacitor"]["mount_side"] = "sideways"
        normalized, validation = validate_config(old, ROOT, check_output_directory=False)
        self.assertFalse(validation.is_valid)
        self.assertTrue(any("capacitor.mount_side" in item for item in validation.errors))

    def test_02_top_default_regression_and_top_bottom_xy_are_identical(self) -> None:
        _, default_geometry = self.geometry()
        explicit = copy.deepcopy(self.default)
        explicit["capacitor"]["mount_side"] = "top"
        explicit_geometry = GeometryCalculator(explicit).calculate()
        self.assertEqual(default_geometry.to_dict(), explicit_geometry.to_dict())
        _, bottom = self.geometry("bottom")
        self.assertEqual(self.xy_manifest(default_geometry), self.xy_manifest(bottom))

    def test_03_surface_z_body_reference_ports_masks_and_airbox(self) -> None:
        _, top = self.geometry("top")
        _, bottom = self.geometry("bottom")
        self.assertEqual((top.signal_layer_name, bottom.signal_layer_name), ("L1", "L14"))
        self.assertEqual((top.reference_layer_name, bottom.reference_layer_name), ("L2", "L13"))
        for geometry in (top, bottom):
            for signal in ("P", "N"):
                for shape in geometry.signal_shapes[signal]:
                    self.assertAlmostEqual(shape.z_bottom, geometry.signal_z_bottom, places=12)
                    self.assertAlmostEqual(shape.z_bottom + shape.thickness, geometry.signal_z_top, places=12)
            for port in geometry.ports:
                self.assertEqual(port.signal, "P" if port.name.endswith("_P") else "N")
                self.assertEqual(port.side, "IN" if "_IN_" in port.name else "OUT")
            self.assertEqual({item["layer"] for item in geometry.soldermask_openings},
                             set(geometry.surface_soldermask_layers))
            self.assertLessEqual(geometry.airbox["z_bottom"],
                                 min(body["z_bottom"] for body in geometry.capacitor_bodies) - geometry.airbox["margin"] + 1e-12)
            self.assertGreaterEqual(geometry.airbox["z_top"],
                                    max(body["z_top"] for body in geometry.capacitor_bodies) + geometry.airbox["margin"] - 1e-12)
        self.assertTrue(all(body["z_bottom"] == top.component_outward_z and
                            body["z_top"] > top.component_outward_z for body in top.capacitor_bodies))
        self.assertTrue(all(body["z_top"] == bottom.component_outward_z and
                            body["z_bottom"] < bottom.component_outward_z for body in bottom.capacitor_bodies))
        self.assertTrue(all(port.integration_line_end[2] < port.integration_line_start[2]
                            for port in top.ports))
        self.assertTrue(all(port.integration_line_end[2] > port.integration_line_start[2]
                            for port in bottom.ports))

    def test_04_all_capacitor_models_preserve_terminal_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            touchstone = root / "capacitor.s2p"
            touchstone.write_text(
                "# GHZ S RI R 50\n0.01 0 0 1 0 1 0 0 0\n32 0 0 1 0 1 0 0 0\n",
                encoding="utf-8",
            )
            for mode in ("ideal_bridge", "lumped_rlc", "sparameter"):
                for side in ("top", "bottom"):
                    with self.subTest(mode=mode, side=side):
                        config = copy.deepcopy(self.default)
                        config["capacitor"]["mount_side"] = side
                        config["capacitor_model"]["mode"] = mode
                        config["capacitor_model"]["touchstone_file"] = str(touchstone)
                        config["project"]["project_name"] = f"Mount_{mode}_{side}"
                        normalized, validation = validate_config(
                            config, ROOT, check_output_directory=False
                        )
                        self.assertTrue(validation.is_valid, validation.errors)
                        geometry = GeometryCalculator(normalized).calculate()
                        if mode == "ideal_bridge":
                            self.assertEqual(set(geometry.bridge_shapes), {"P", "N"})
                            self.assertFalse(geometry.capacitor_model_shapes)
                        else:
                            self.assertEqual(set(geometry.capacitor_model_shapes), {"P", "N"})
                            for signal, sheet in geometry.capacitor_model_shapes.items():
                                self.assertEqual(sheet.metadata["input_conductor"], f"SIGNAL_{signal}_IN")
                                self.assertEqual(sheet.metadata["output_conductor"], f"SIGNAL_{signal}_OUT")
                                self.assertEqual(sheet.metadata["port_mapping"], "1=input_pad,2=output_pad")
                        scene = build_model_scene(normalized, geometry)
                        self.assertEqual(
                            len([item for item in scene.objects if item.key.startswith("body:")]), 2
                        )
                        generated = HfssScriptGenerator(normalized, ROOT).generate(root / "generated")
                        completed = subprocess.run(
                            [sys.executable, str(generated.script_path), "--validate-only"],
                            capture_output=True, text=True, timeout=30,
                        )
                        self.assertEqual(
                            completed.returncode, 0, completed.stdout + completed.stderr
                        )

    def test_05_save_load_bottom_and_repeated_switch_has_no_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bottom.json"
            config = copy.deepcopy(self.default)
            config["capacitor"]["mount_side"] = "bottom"
            save_config(config, path)
            loaded, _ = load_config(path)
            self.assertEqual(loaded["capacitor"]["mount_side"], "bottom")
            self.assertEqual(GeometryCalculator(loaded).calculate().mount_side, "bottom")
        config = copy.deepcopy(self.default)
        first = GeometryCalculator(config).calculate().to_dict()
        for side in ("bottom", "top", "bottom", "top"):
            config["capacitor"]["mount_side"] = side
            latest = GeometryCalculator(config).calculate().to_dict()
        self.assertEqual(first, latest)

    def test_06_missing_bottom_outer_signal_is_explicit_error(self) -> None:
        config = copy.deepcopy(self.default)
        config["capacitor"]["mount_side"] = "bottom"
        config["stackup"][-2]["type"] = "dielectric"
        config["stackup"][-2]["dk"] = 3.8
        config["stackup"][-2]["df"] = 0.015
        _, validation = validate_config(config, ROOT, check_output_directory=False)
        self.assertFalse(validation.is_valid)
        self.assertTrue(any("底层安装" in item and "外层" in item for item in validation.errors))

    def test_07_surface_ground_moves_to_selected_layer(self) -> None:
        for side, expected_layer in (("top", "L1"), ("bottom", "L14")):
            config = copy.deepcopy(self.default)
            config["capacitor"]["mount_side"] = side
            config["gnd_vias"]["enabled"] = True
            config["gnd_vias"]["create_top_ground_copper"] = True
            geometry = GeometryCalculator(config).calculate()
            self.assertEqual(geometry.surface_ground["layer"], expected_layer)
            self.assertEqual(geometry.surface_ground["mount_side"], side)

    def test_08_bottom_local_validate_and_generated_script_validate_only(self) -> None:
        config, geometry = self.geometry("bottom", "lumped_rlc")
        report, _, _ = ModelValidator(self.manager).run_local(config)
        self.assertEqual(report.counts["errors"], 0)
        script = HfssScriptGenerator.build_script_text(config, geometry)
        self.assertIn("create_surface_soldermask_openings(app, state)", script)
        self.assertIn('length(shape["z_bottom"])', script)
        self.assertIn('z_bottom=length(body["z_bottom"])', script)
        with tempfile.TemporaryDirectory() as directory:
            generated = HfssScriptGenerator(config, ROOT).generate(Path(directory))
            completed = subprocess.run(
                [sys.executable, str(generated.script_path), "--validate-only"],
                capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_09_gui_reload_displays_bottom_and_switch_refreshes_same_snapshot_chain(self) -> None:
        try:
            app = SurfaceCapAntipadApp()
            app.withdraw()
            app.update_idletasks()
        except tk.TclError as exc:
            self.skipTest(f"Tk GUI unavailable: {exc}")
        try:
            bottom = copy.deepcopy(self.default)
            bottom["capacitor"]["mount_side"] = "bottom"
            app._load_to_widgets(bottom)
            app.update_idletasks()
            self.assertEqual(
                app.variables["capacitor.mount_side"].get(),
                ENUM_LABELS["capacitor.mount_side"]["bottom"],
            )
            self.assertEqual(app._collect()["capacitor"]["mount_side"], "bottom")
            app.refresh_visuals()
            self.assertEqual(app._preview_snapshot["geometry_snapshot"]["mount_side"], "bottom")
            self.assertEqual(app._preview_snapshot["geometry_snapshot"]["signal_layer_name"], "L14")

            app.variables["capacitor.mount_side"].set(ENUM_LABELS["capacitor.mount_side"]["top"])
            app.refresh_visuals()
            first_top = copy.deepcopy(app._preview_snapshot["geometry_snapshot"])
            for side in ("bottom", "top", "bottom", "top"):
                app.variables["capacitor.mount_side"].set(ENUM_LABELS["capacitor.mount_side"][side])
                app.refresh_visuals()
            self.assertEqual(first_top, app._preview_snapshot["geometry_snapshot"])
        finally:
            try:
                for after_id in app.tk.call("after", "info"):
                    app.after_cancel(after_id)
            except tk.TclError:
                pass
            app.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
