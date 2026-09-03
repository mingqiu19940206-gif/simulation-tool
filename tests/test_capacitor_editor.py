from __future__ import annotations

import copy
import math
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from capacitor_editor import (ENUM_LABELS, build_dimension_specs,
                              calculate_srf_ghz, inspect_touchstone,
                              validate_capacitor_editor)
from config_manager import ConfigManager, derive_capacitor_values, format_length
from geometry_calculator import GeometryCalculator
from main_gui import SurfaceCapAntipadApp
from main_gui_legacy import calculate_initial_window_metrics


class CapacitorEditorLogicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.default = ConfigManager(ROOT).default()

    def test_authoritative_derived_fields_cover_all_modes(self) -> None:
        cap = copy.deepcopy(self.default["capacitor"])
        cap["pad_length"] = 0.4
        cap["pad_gap"] = 0.3
        cap["bridge_length_mode"] = "auto"
        derived = derive_capacitor_values(cap, 0.001)
        self.assertAlmostEqual(derived["pad_pitch"], 0.7)
        self.assertAlmostEqual(derived["bridge_length"], 0.302)
        cap["pad_pitch_mode"] = "advanced_pitch"
        cap["pad_pitch"] = 0.9
        derived = derive_capacitor_values(cap, 0.001)
        self.assertAlmostEqual(derived["pad_gap"], 0.5)

    def test_rlc_srf(self) -> None:
        expected = 1.0 / (2.0 * math.pi * math.sqrt(100e-12 * 0.25e-9)) / 1e9
        self.assertAlmostEqual(calculate_srf_ghz(100.0, 0.25), expected, places=12)
        self.assertIsNone(calculate_srf_ghz(0.0, 0.25))

    def test_touchstone_invalid_and_basic_metadata(self) -> None:
        info, errors = inspect_touchstone("missing.s2p", ROOT)
        self.assertIsNone(info)
        self.assertTrue(any("不存在" in item for item in errors))
        with tempfile.TemporaryDirectory() as directory:
            sample = Path(directory) / "cap.s2p"
            sample.write_text("# GHZ S RI R 50\n1 0 0 1 0 1 0 0 0\n10 0 0 1 0 1 0 0 0\n", encoding="utf-8")
            info, errors = inspect_touchstone(str(sample))
            self.assertFalse(errors)
            self.assertEqual(info.ports, 2)
            self.assertEqual(info.frequency_min, 1.0)
            self.assertEqual(info.frequency_max, 10.0)
            self.assertEqual(info.reference_ohm, 50.0)

    def test_live_validation_reports_rlc_and_bridge_problems(self) -> None:
        cfg = copy.deepcopy(self.default)
        cfg["capacitor_model"]["mode"] = "lumped_rlc"
        cfg["capacitor_model"]["capacitance_pf"] = 0
        cfg["capacitor"]["bridge_width"] = cfg["capacitor"]["pad_width"] * 2
        issues = validate_capacitor_editor(cfg, ROOT)
        self.assertTrue(any(issue.path == "capacitor_model.capacitance_pf" and issue.level == "error" for issue in issues))
        self.assertFalse(any(issue.path == "capacitor.bridge_width" for issue in issues))

    def test_v2_json_round_trip_preserves_capacitor_fields(self) -> None:
        manager = ConfigManager(ROOT)
        cfg = copy.deepcopy(self.default)
        cfg["capacitor_model"]["mode"] = "lumped_rlc"
        cfg["capacitor_model"]["capacitance_pf"] = 47.5
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "round_trip.json"
            manager.save(cfg, path)
            loaded, _ = manager.load(path)
        self.assertEqual(loaded["schema_version"], 3)
        self.assertEqual(loaded["capacitor"]["pad_pitch_mode"], cfg["capacitor"]["pad_pitch_mode"])
        self.assertEqual(loaded["capacitor_model"]["mode"], "lumped_rlc")
        self.assertAlmostEqual(loaded["capacitor_model"]["capacitance_pf"], 47.5)
    def test_editor_precision_is_compact(self) -> None:
        self.assertEqual(format_length(1.0, "mm"), "1")
        self.assertEqual(format_length(1.0, "mil"), "39.37")
        self.assertLessEqual(len(format_length(0.123456789, "mm").split(".")[-1]), 2)

    def test_dimension_specs_are_derived_from_rotated_geometry(self) -> None:
        cfg = copy.deepcopy(self.default)
        cfg["capacitor"]["pad_rotation"] = 27.0
        cfg["capacitor"]["cap_center_x"] = 0.15
        cfg["capacitor"]["cap_center_x_offset"] = 0.05
        geometry = GeometryCalculator(cfg).calculate()
        specs = {item.path: item for item in build_dimension_specs(geometry)}
        for key in ("body_length", "body_width", "pad_length", "pad_width",
                    "pad_gap", "pad_pitch", "cap_pitch", "bridge_width", "bridge_length"):
            self.assertAlmostEqual(specs[f"capacitor.{key}"].value, cfg["capacitor"][key])
        self.assertAlmostEqual(specs["capacitor.pad_rotation"].value, 27.0)
        self.assertAlmostEqual(specs["capacitor.cap_center_x"].value, 0.2)

    def test_initial_window_metrics_are_bounded_and_centered(self) -> None:
        self.assertEqual(
            calculate_initial_window_metrics(2940, 1846),
            (1280, 800, 830, 523, 1120, 720),
        )
        self.assertEqual(
            calculate_initial_window_metrics(1470, 923),
            (1205, 756, 132, 83, 1120, 720),
        )


class CapacitorEditorGuiTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.app = SurfaceCapAntipadApp()
            self.app.withdraw()
            self.app.update_idletasks()
        except tk.TclError as exc:
            self.skipTest(f"Tk GUI unavailable: {exc}")

    def tearDown(self) -> None:
        if hasattr(self, "app"):
            try:
                for after_id in self.app.tk.call("after", "info"):
                    self.app.after_cancel(after_id)
            except tk.TclError:
                pass
            self.app.destroy()

    def _set_enum(self, path: str, backend: str) -> None:
        self.app.variables[path].set(ENUM_LABELS[path][backend])

    def test_legacy_equal_pn_touchstone_is_mapped_to_common(self) -> None:
        cfg = copy.deepcopy(self.app.manager.default())
        cfg["capacitor_model"]["mode"] = "sparameter"
        cfg["capacitor_model"]["touchstone_file"] = ""
        cfg["capacitor_model"]["touchstone_file_p"] = "vendor.s2p"
        cfg["capacitor_model"]["touchstone_file_n"] = "vendor.s2p"
        self.app._load_to_widgets(cfg)
        collected = self.app._collect()
        self.assertEqual(collected["capacitor_model"]["touchstone_file"], "vendor.s2p")
        self.assertEqual(collected["capacitor_model"]["touchstone_file_p"], "")
        self.assertEqual(collected["capacitor_model"]["touchstone_file_n"], "")
    def test_model_mode_switch_shows_only_relevant_frame(self) -> None:
        for mode in ("ideal_bridge", "lumped_rlc", "sparameter"):
            self._set_enum("capacitor_model.mode", mode)
            selected = self.app._update_model_mode()
            self.assertEqual(selected, mode)
            for name, frame in self.app.model_frames.items():
                self.assertEqual(bool(frame.winfo_manager()), name == mode)

    def test_lumped_rlc_refresh_formats_summary_without_missing_dependency(self) -> None:
        self._set_enum("capacitor_model.mode", "lumped_rlc")
        config, validation = self.app.manager.validate(
            self.app._collect(), check_output_directory=False
        )
        geometry = GeometryCalculator(config).calculate()
        self.app._update_capacitor_feedback(config, validation, geometry)
        self.assertIn("fSRF", self.app.model_summary_var.get())
        self.assertNotIn("format_number", self.app.cap_detail_var.get())

    def test_pad_and_bridge_derived_states(self) -> None:
        self.app.variables["capacitor.pad_length"].set("0.4")
        self.app.variables["capacitor.pad_gap"].set("0.3")
        self._set_enum("capacitor.pad_pitch_mode", "auto_from_gap")
        self._set_enum("capacitor.bridge_length_mode", "auto")
        self.app._sync_derived_fields()
        self.assertEqual(self.app.variables["capacitor.pad_pitch"].get(), "0.7")
        self.assertEqual(str(self.app.widgets["capacitor.pad_pitch"].cget("state")), "readonly")
        self.assertEqual(str(self.app.widgets["capacitor.bridge_length"].cget("state")), "readonly")
        self._set_enum("capacitor.pad_pitch_mode", "advanced_pitch")
        self.app.variables["capacitor.pad_pitch"].set("0.9")
        self.app._sync_derived_fields()
        self.assertEqual(self.app.variables["capacitor.pad_gap"].get(), "0.5")
        self.assertEqual(str(self.app.widgets["capacitor.pad_gap"].cget("state")), "readonly")
        self._set_enum("capacitor.bridge_length_mode", "manual")
        self.app._sync_derived_fields()
        self.assertEqual(str(self.app.widgets["capacitor.bridge_length"].cget("state")), "normal")

    def test_preset_application_and_modified_feedback(self) -> None:
        self.assertEqual(self.app._backend_value("capacitor.package"), "0402")
        self.app.package_preset_var.set(ENUM_LABELS["capacitor.package"]["0201"])
        before = self.app.variables["capacitor.body_length"].get()
        before_pad = self.app.variables["capacitor.pad_length"].get()
        self.app._package_changed()
        self.assertEqual(self.app._backend_value("capacitor.package"), "0402")
        self.assertEqual(self.app._collect()["capacitor"]["package"], "0402")
        self.assertEqual(self.app.variables["capacitor.body_length"].get(), before)
        self.assertEqual(self.app.variables["capacitor.pad_length"].get(), before_pad)
        self.assertEqual(
            self.app.package_state_var.get(),
            "封装名已选 0201，焊盘/本体仍是 0402，点应用才进仿真",
        )
        self.app._apply_selected_preset()
        self.assertEqual(self.app._backend_value("capacitor.package"), "0201")
        self.assertEqual(self.app._collect()["capacitor"]["package"], "0201")
        self.assertEqual(
            self.app.variables["capacitor.body_length"].get(),
            format_length(0.6, self.app.display_unit),
        )
        self.app.variables["capacitor.body_length"].set(
            format_length(0.61, self.app.display_unit)
        )
        self.app._update_package_state()
        self.assertIn("已修改", self.app.package_state_var.get())

    def test_mm_mil_switch_preserves_physical_length(self) -> None:
        original = self.app._collect()["capacitor"]["pad_length"]
        self.app.unit_var.set("mm")
        self.app._unit_changed()
        self.assertEqual(
            self.app.variables["capacitor.pad_length"].get(),
            format_length(original, "mm"),
        )
        self.assertAlmostEqual(self.app._collect()["capacitor"]["pad_length"], original, places=12)
        self.app.unit_var.set("mil")
        self.app._unit_changed()
        self.assertEqual(
            self.app.variables["capacitor.pad_length"].get(),
            format_length(original, "mil"),
        )
        self.assertAlmostEqual(self.app._collect()["capacitor"]["pad_length"], original, places=12)
        self.app._update_package_state()
        self.assertNotIn("已修改", self.app.package_state_var.get())

    def test_minimum_window_layout_remains_usable(self) -> None:
        self.app.deiconify()
        self.app.notebook.select(self.app.tabs["电容与模型"])
        self.app.geometry("1120x720")
        self.app.update()
        self.app.update_idletasks()
        self.assertGreaterEqual(self.app.winfo_width(), 1120)
        self.assertGreaterEqual(self.app.winfo_height(), 720)
        self.assertGreater(self.app.cap_geometry_canvas.winfo_width(), 400)
        self.assertGreater(self.app.model_canvas.winfo_width(), 300)

    def test_initial_window_no_longer_opens_at_full_desktop_size(self) -> None:
        self.app.deiconify()
        self.app.update()
        self.assertLessEqual(self.app.winfo_width(), 1280)
        self.assertLessEqual(self.app.winfo_height(), 800)

    def test_diagrams_draw_default_presets_and_custom(self) -> None:
        for package in ("0201", "0402", "custom"):
            cfg = copy.deepcopy(self.app.manager.default())
            if package != "custom":
                from config_manager import apply_package_preset
                cfg = apply_package_preset(cfg, package)
            else:
                cfg["capacitor"]["package"] = "custom"
            geometry = GeometryCalculator(cfg).calculate()
            self.app.cap_geometry_canvas.show(cfg, geometry, "mm")
            self.app.model_canvas.show(cfg)
            self.assertGreater(len(self.app.cap_geometry_canvas.find_all()), 10)
            self.assertGreater(len(self.app.model_canvas.find_all()), 5)

    def test_electrical_model_modes_keep_hierarchy_and_text_in_viewport(self) -> None:
        self.app.deiconify()
        self.app.geometry("1120x720")
        self.app.notebook.select(self.app.tabs["电容与模型"])
        self.app.update()
        canvas = self.app.model_canvas
        config = copy.deepcopy(self.app.manager.default())
        for mode in ("ideal_bridge", "lumped_rlc", "sparameter", "open"):
            with self.subTest(mode=mode):
                config["capacitor_model"]["mode"] = mode
                canvas.show(config)
                self.app.update_idletasks()
                self.app.update()
                width, height = canvas.winfo_width(), canvas.winfo_height()
                texts = [item for item in canvas.find_all() if canvas.type(item) == "text"]
                self.assertGreaterEqual(len(texts), 5)
                for item in texts:
                    box = canvas.bbox(item)
                    self.assertIsNotNone(box)
                    assert box is not None
                    self.assertGreaterEqual(box[0], -2, (mode, item, box))
                    self.assertGreaterEqual(box[1], -2, (mode, item, box))
                    self.assertLessEqual(box[2], width + 2, (mode, item, box, width))
                    self.assertLessEqual(box[3], height + 2, (mode, item, box, height))
                self.assertEqual(len(canvas.find_withtag("model-title")), 1)
                self.assertEqual(len(canvas.find_withtag("model-note")), 1)
                summary = canvas.find_withtag("model-summary")
                self.assertEqual(len(summary), 1)
                self.assertEqual(canvas.itemcget(summary[0], "font"), "ViaSmallFont")

    def test_dynamic_dimension_tab_is_merged_into_capacitor_page(self) -> None:
        self.assertNotIn("动态尺寸图", self.app.tabs)
        self.assertFalse(hasattr(self.app, "dimension_canvas"))
        self.assertTrue(hasattr(self.app, "cap_dimensions_visible"))
        self.assertEqual(set(self.app.cap_dimension_categories), {"body", "pad", "layout", "bridge"})

    def test_dimension_controls_and_parameter_navigation(self) -> None:
        canvas = self.app.cap_geometry_canvas
        original_zoom = canvas._zoom
        canvas.zoom_in()
        self.assertGreater(canvas._zoom, original_zoom)
        canvas.reset_view()
        self.assertEqual(canvas._zoom, 1.0)
        self.app.cap_dimension_categories["body"].set(False)
        self.app._update_capacitor_dimension_filters()
        body_overlays_before = [
            item for item, path in canvas._dimension_paths.items()
            if path == "capacitor.body_length" and "dimension-value" not in canvas.gettags(item)
        ]
        self.assertFalse(body_overlays_before)
        self.app.cap_dimension_categories["body"].set(True)
        self.app._update_capacitor_dimension_filters()
        body_overlays_after = [
            item for item, path in canvas._dimension_paths.items()
            if path == "capacitor.body_length" and "dimension-value" not in canvas.gettags(item)
        ]
        self.assertTrue(body_overlays_after)
        pad_item = canvas.find_withtag("structure:pad")[0]
        original_outline = canvas.itemcget(pad_item, "outline")
        canvas._highlight_dimension("capacitor.pad_pitch", True)
        self.assertEqual(canvas.itemcget(pad_item, "outline"), canvas.COLORS["active"])
        canvas._highlight_dimension("capacitor.pad_pitch", False)
        self.assertEqual(canvas.itemcget(pad_item, "outline"), original_outline)
        self.app._focus_capacitor_parameter("capacitor.pad_gap")
        self.app.update_idletasks()
        self.assertEqual(self.app.notebook.select(), str(self.app.tabs["电容与模型"]))
        self.assertIn("capacitor.pad_gap", self.app.status.get())

    @staticmethod
    def _ancestors(widget: tk.Misc) -> list[tk.Misc]:
        chain: list[tk.Misc] = []
        owner: tk.Misc | None = widget
        while owner is not None:
            chain.append(owner)
            owner = getattr(owner, "master", None)
        return chain

    def test_non_geometry_switches_are_off_the_main_path(self) -> None:
        self.app.notebook.select(self.app.tabs["电容与模型"])
        self.app.update_idletasks()
        self.assertEqual(self.app.widgets["capacitor.symmetric_about_y"].winfo_manager(), "")
        self.assertEqual(self.app.widgets["trace.transition_direction"].winfo_manager(), "")
        outline = self.app.widgets["capacitor.show_body_outline_in_preview"]
        create_body = self.app.widgets["capacitor.create_body_in_hfss"]
        self.assertEqual(str(outline.cget("text")), "本体")
        self.assertIs(
            outline, self.app.widgets["capacitor.show_body_outline_in_preview"])
        self.assertNotIn(self.app.cap_hfss_box, self._ancestors(outline))
        self.assertIn(self.app.cap_dimension_toolbar, self._ancestors(outline))
        self.assertIn(self.app.cap_hfss_box, self._ancestors(create_body))
        self.assertEqual(self.app.cap_hfss_outline_label.winfo_manager(), "")
        self.assertEqual(self.app.cap_hfss_outline_checkbox.winfo_manager(), "")
        self.assertIn(self.app.cap_hfss_box, self._ancestors(self.app.cap_hfss_outline_label))
        hfss_mapped_texts = []
        hfss_hidden_texts = []
        for child in self.app.cap_hfss_box.winfo_children():
            try:
                text = str(child.cget("text"))
            except tk.TclError:
                continue
            if not text:
                continue
            (hfss_mapped_texts if child.winfo_manager() else hfss_hidden_texts).append(text)
        self.assertIn("预览中显示本体轮廓", hfss_hidden_texts)
        self.assertNotIn("预览中显示本体轮廓", hfss_mapped_texts)
        self.assertIn("在 HFSS 中创建电容本体", hfss_mapped_texts)
        visible_body = []
        def walk(widget: tk.Misc) -> None:
            for child in widget.winfo_children():
                try:
                    if str(child.cget("text")) == "本体" and child.winfo_manager():
                        visible_body.append(child)
                except tk.TclError:
                    pass
                walk(child)
        walk(self.app)
        self.assertEqual(len(visible_body), 1)
        self.assertIs(visible_body[0], outline)
        collected = self.app._collect()
        self.assertIn("symmetric_about_y", collected["capacitor"])
        self.assertIn("transition_direction", collected["trace"])
        self.assertIn("show_body_outline_in_preview", collected["capacitor"])
        self.assertIn("create_body_in_hfss", collected["capacitor"])

    def test_old_json_still_loads_hidden_capacitor_fields(self) -> None:
        loaded, _ = ConfigManager(ROOT).load(ROOT / "examples" / "v1_legacy.json")
        self.app._load_to_widgets(loaded)
        collected = self.app._collect()
        self.assertTrue(collected["capacitor"]["symmetric_about_y"])
        self.assertEqual(collected["trace"]["transition_direction"], "auto")
        self.assertTrue(collected["capacitor"]["show_body_outline_in_preview"])
        self.assertEqual(collected["capacitor"]["package"], "0402")
        self.assertEqual(self.app._backend_value("capacitor.package"), "0402")
        self.assertEqual(
            self.app._preset_picker_backend(), "0402")


if __name__ == "__main__":
    unittest.main(verbosity=2)
