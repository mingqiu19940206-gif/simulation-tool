"""VIA 换层专属编辑器 GUI smoke 回归（规格 §12.2 / §14.1）。

需要 tkinter 与 VTK；在完整 Python 3.14.6 环境运行，沙箱（无 tkinter）自动跳过。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:
    import tkinter  # noqa: F401
    from tkinter import ttk
    HAS_TK = True
except ImportError:
    HAS_TK = False

from config_manager import convert_length, format_length


@unittest.skipUnless(HAS_TK, "需要 tkinter/VTK 完整 GUI 环境")
class ViaTransitionGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from main_gui import SurfaceCapAntipadApp
        cls.app = SurfaceCapAntipadApp()
        cls.app.withdraw()
        cls.app.update_idletasks()

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.app.destroy()
        except Exception:
            pass

    def _realize_desktop_layout(self) -> None:
        self.app.geometry("1600x1000+0+0")
        self.app.deiconify()
        self.app.update()
        self.app.update_idletasks()

    def _enter_via_scenario(self) -> None:
        self.app.variables["scenario.type"].set("via_transition")
        self.app.update_idletasks()

    def _restore_default_via_gaps(self) -> None:
        for path, mm_value in (
            ("via_transition.entry_trace.gap", 0.7),
            ("via_transition.exit_trace.gap", 0.7),
            ("via_transition.entry_trace.width", 0.1),
            ("via_transition.exit_trace.width", 0.1),
        ):
            display = format_length(mm_value, self.app.display_unit)
            self.app._length_mm_cache[path] = mm_value
            self.app._length_display_snapshot[path] = display
            self.app.variables[path].set(display)
        self.app._sync_via_fields()
        self.app.update_idletasks()

    @staticmethod
    def _fraction_digits(text: str) -> int:
        token = str(text).strip().split()[0]
        if "." not in token:
            return 0
        fraction = "".join(ch for ch in token.split(".", 1)[1] if ch.isdigit())
        return len(fraction)

    def _canvas_texts(self, canvas) -> list[str]:
        return [
            str(canvas.itemcget(item, "text"))
            for item in canvas.find_all()
            if canvas.type(item) == "text"
        ]

    def _click_dimension_label(self, canvas, path: str, prefix: str) -> None:
        items = [
            item for item in canvas.find_withtag(f"dimension:{path}")
            if canvas.type(item) == "text"
            and str(canvas.itemcget(item, "text")).startswith(prefix)
        ]
        self.assertTrue(items, (path, self._canvas_texts(canvas)))
        bbox = canvas.bbox(items[0])
        self.assertIsNotNone(bbox)
        x = int((bbox[0] + bbox[2]) / 2)
        y = int((bbox[1] + bbox[3]) / 2)
        canvas.event_generate("<Button-1>", x=x, y=y)
        self.app.update()
        self.app.update_idletasks()

    def test_01_via_tab_present(self) -> None:
        self.assertIn("VIA 换层", self.app.tabs)
        self.assertIn("反焊盘与叠层", self.app.tabs)

        # The VIA page is now the authoritative whole-transition editor.
        from tkinter import ttk
        via_tab = self.app.tabs["VIA 换层"]
        editable = []
        pending = list(via_tab.winfo_children())
        while pending:
            widget = pending.pop()
            pending.extend(widget.winfo_children())
            if isinstance(widget, (ttk.Entry, ttk.Combobox, ttk.Checkbutton,
                                   ttk.Radiobutton)):
                editable.append(widget)
        self.assertGreater(len(editable), 20)
        self.assertEqual(
            tuple(self.app.via_parameter_notebook.tab(page, "text")
                  for page in self.app.via_parameter_notebook.tabs()),
            ("换层与传输线", "信号孔与背钻", "回流地孔"),
        )

    def test_02_scenario_switch_round_trips_via_fields(self) -> None:
        self.app.variables["scenario.type"].set("via_transition")
        self.app.update_idletasks()
        config = self.app._collect()
        vt = config["via_transition"]
        self.assertEqual(vt["entry_layer"], "L1")
        self.assertEqual(vt["exit_layer"], "L10")
        self.assertIsInstance(vt["return_vias"]["connect_layers"], list)
        self.assertEqual(vt["return_vias"]["connect_layers"], ["L2", "L9"])
        self.assertTrue(vt["backdrill"]["enabled"])
        self.assertEqual(vt["signal_via_pitch"], vt["pair_pitch"])
        self.assertEqual(vt["fanout"]["style"], "deg45")
        self.assertEqual(vt["fanout"]["direction"], "auto")
        self.assertGreaterEqual(vt["fanout"]["straight_length"], 0.0)
        self.assertAlmostEqual(vt["entry_trace"]["gap"], 0.7)
        self.assertAlmostEqual(vt["exit_trace"]["gap"], 0.7)
        self.assertAlmostEqual(
            vt["entry_trace"]["width"] + vt["entry_trace"]["gap"], vt["pair_pitch"])
        self.assertIn("via_transition.entry_trace.gap", self.app.widgets)
        self.assertIn("via_transition.exit_trace.gap", self.app.widgets)
        self.assertNotIn("via_transition.pair_pitch", self.app.widgets)
        for path in (
            "via_transition.entry_trace.center_to_center",
            "via_transition.exit_trace.center_to_center",
        ):
            widget = self.app.widgets[path]
            self.assertEqual(str(widget.cget("state")), "readonly")
            self.assertIsInstance(widget, tkinter.Entry)
            self.assertNotIsInstance(widget, ttk.Entry)
            self.assertEqual(str(widget.cget("readonlybackground")).lower(), "#eef2f7")
            self.assertIn("宽度 + GAP", str(self.app.formula_labels[path].cget("text")))
        self.assertIn("残桩从背钻侧最近功能连接层起算",
                      self.app.backdrill_hint_label.cget("text"))
        self.assertIn("与输入/输出层无关",
                      self.app.backdrill_hint_label.cget("text"))
        self.assertNotIn("entry/exit", self.app.backdrill_hint_label.cget("text"))

    def test_03_via_editor_feedback_populates(self) -> None:
        from config_manager import ConfigManager
        from geometry_calculator import GeometryCalculator
        config = self.app._collect()
        manager = ConfigManager(ROOT)
        cfg, validation = manager.validate(config, check_output_directory=False)
        geometry = GeometryCalculator(cfg).calculate()
        self.app._update_via_editor(cfg, geometry, validation)
        feedback = self.app.via_feedback_var.get()
        self.assertIn("成品孔径", feedback)
        self.assertTrue(any(token in feedback for token in ("VIA 配置有效", "错误", "警告")))

    def test_04_antipad_page_owns_layer_antipad_and_sweeps_remain_reachable(self) -> None:
        self.app.variables["scenario.type"].set("via_transition")
        self.app.update_idletasks()
        workbench = self.app.antipad_workbench
        self.assertTrue(workbench.via_box.winfo_manager())
        self.assertIn("via_transition.padstack.antipad.topology", self.app.variables)
        self.assertIn("via_transition.padstack.antipad.shape", self.app.variables)
        self.assertIn("via_transition.padstack.antipad.diameter", self.app.variables)
        steps = self.app.simulation_sweep_workbench.steps
        self.assertEqual(str(steps.tab(self.app.simulation_sweep_workbench.design_page, "state")), "normal")
        self.assertEqual(str(steps.tab(self.app.simulation_sweep_workbench.execution_page, "state")), "normal")

    def test_05_shape_interlocks_and_round_trip(self) -> None:
        self.app.variables["scenario.type"].set("via_transition")
        self.app.variables["via_transition.padstack.antipad.shape"].set("圆角矩形")
        self.app.antipad_workbench._update_via_field_states()
        self.assertEqual(str(self.app.antipad_workbench.via_diameter.cget("state")), "disabled")
        self.assertEqual(str(self.app.antipad_workbench.via_length.cget("state")), "normal")
        self.assertEqual(str(self.app.antipad_workbench.via_width.cget("state")), "normal")
        self.assertEqual(str(self.app.antipad_workbench.via_corner.cget("state")), "normal")
        collected = self.app._collect()
        self.assertEqual(collected["via_transition"]["padstack"]["antipad"]["shape"],
                         "rounded_rectangle")
        self.app.variables["via_transition.padstack.antipad.shape"].set("圆形")

    def test_06_via_stack_table_uses_real_state_and_dimensions(self) -> None:
        from config_manager import ConfigManager
        from geometry_calculator import GeometryCalculator
        self.app.variables["scenario.type"].set("via_transition")
        config, validation = ConfigManager(ROOT).validate(
            self.app._collect(), check_output_directory=False)
        geometry = GeometryCalculator(config).calculate()
        self.app.antipad_workbench.show_via_transition(config, geometry, validation)
        values = self.app.antipad_workbench.tree.item(
            str(next(i for i, layer in enumerate(config["stackup"]) if layer["name"] == "L2")),
            "values")
        self.assertIn(values[7], {"生成反焊盘", "背钻影响/生成反焊盘", "保留非功能焊盘"})
        self.assertIn(values[8], {"P/N 独立开窗", "P/N 合并开窗"})
        self.assertNotEqual(values[11], "—")

    def test_07_layer_selectors_and_return_plane_matrix_are_stack_driven(self) -> None:
        from tkinter import ttk

        stackup = self.app.config["stackup"]
        signal_layers = tuple(str(layer["name"]) for layer in stackup
                              if layer["type"] == "signal")
        conductive_layers = tuple(str(layer["name"]) for layer in stackup
                                  if layer["type"] in {"signal", "plane"})
        reference_layers = tuple(str(layer["name"]) for layer in stackup
                                 if layer["type"] == "plane" and layer.get("is_reference_plane"))

        for path in ("via_transition.entry_layer", "via_transition.exit_layer"):
            widget = self.app.widgets[path]
            self.assertIsInstance(widget, ttk.Combobox)
            self.assertEqual(tuple(widget.cget("values")), signal_layers)
        for path in ("via_transition.drill_start_layer", "via_transition.drill_end_layer",
                     "via_transition.return_vias.start_layer",
                     "via_transition.return_vias.end_layer"):
            widget = self.app.widgets[path]
            self.assertIsInstance(widget, ttk.Combobox)
            self.assertEqual(tuple(widget.cget("values")), conductive_layers)

        self.assertIs(
            self.app.widgets["via_transition.return_vias.connect_layers"],
            self.app.return_connect_matrix)
        self.assertEqual(tuple(self.app.return_connect_vars), reference_layers)
        selected = tuple(name for name, variable in self.app.return_connect_vars.items()
                         if variable.get())
        self.assertEqual(selected, ("L2", "L9"))
        # Exercise the same refresh ordering as a real edit: scenario linkage
        # first enables the VIA group, then the derived-field linkage must
        # restore the finished-hole entry to read-only.
        self.app.refresh_visuals()
        self.assertEqual(str(self.app.widgets[
            "via_transition.return_vias.finished_hole_diameter"].cget("state")), "readonly")

    def test_08_via_matrix_supports_extended_bulk_selection(self) -> None:
        from config_manager import ConfigManager
        from geometry_calculator import GeometryCalculator

        config, validation = ConfigManager(ROOT).validate(
            self.app._collect(), check_output_directory=False)
        geometry = GeometryCalculator(config).calculate()
        workbench = self.app.antipad_workbench
        workbench.show_via_transition(config, geometry, validation)
        self.assertEqual(str(workbench.tree.cget("selectmode")), "extended")

        workbench._select_via_references()
        selected = tuple(workbench.tree.selection())
        expected = tuple(str(index) for index, layer in enumerate(config["stackup"])
                         if layer["type"] == "plane" and layer.get("is_reference_plane")
                         and workbench._via_state(index) in {"clearance", "backdrilled_removed"})
        self.assertEqual(selected, expected)
        self.assertGreaterEqual(len(selected), 2)

        # The normal selection callback must keep every selected row instead of
        # collapsing a bulk operation back to a single layer.
        workbench._tree_selected()
        self.assertEqual(tuple(workbench.tree.selection()), expected)

    def test_09_via_visual_labels_do_not_cover_each_other_or_signal_vias(self) -> None:
        self.app.geometry("1600x1000+0+0")
        self.app.variables["scenario.type"].set("via_transition")
        self.app.refresh_visuals()
        self.app.notebook.select(self.app.tabs["VIA 换层"])
        self.app.update_idletasks()
        self.app._redraw_via_diagrams()

        cross = self.app.via_cross_canvas
        marker_boxes = [cross.bbox(item) for item in cross.find_withtag("entry-exit-marker")
                        if cross.type(item) == "text"]
        marker_boxes = [box for box in marker_boxes if box is not None]
        for index, first in enumerate(marker_boxes):
            for second in marker_boxes[index + 1:]:
                overlap_x = min(first[2], second[2]) - max(first[0], second[0])
                overlap_y = min(first[3], second[3]) - max(first[1], second[1])
                self.assertFalse(overlap_x > 1 and overlap_y > 1, (first, second))

        workbench = self.app.antipad_workbench
        workbench.select_layer_by_name("L2")
        workbench.preview.redraw()
        self.app.update_idletasks()
        preview = workbench.preview.canvas
        dimension_boxes = [preview.bbox(item) for item in preview.find_withtag("via-dimension")
                           if preview.type(item) == "text"]
        via_boxes = [preview.bbox(item) for item in preview.find_all()
                     if preview.type(item) == "oval"]
        self.assertTrue(dimension_boxes)
        self.assertTrue(via_boxes)
        for dimension in dimension_boxes:
            assert dimension is not None
            for via in via_boxes:
                assert via is not None
                overlap_x = min(dimension[2], via[2]) - max(dimension[0], via[0])
                overlap_y = min(dimension[3], via[3]) - max(dimension[1], via[1])
                self.assertFalse(overlap_x > 1 and overlap_y > 1, (dimension, via))

    def test_10_auto_direction_is_read_only_and_uses_derived_vertical_axis(self) -> None:
        from config_manager import ConfigManager
        from geometry_calculator import GeometryCalculator

        cfg = self.app._collect()
        cfg["scenario"]["type"] = "via_transition"
        cfg["via_transition"]["padstack"]["antipad"].update({
            "topology": "merged_pair", "shape": "capsule",
            "direction_mode": "auto_pn", "size_mode": "auto_fit",
        })
        cfg, validation = ConfigManager(ROOT).validate(cfg, check_output_directory=False)
        geometry = GeometryCalculator(cfg).calculate()
        self.app._load_to_widgets(cfg)
        workbench = self.app.antipad_workbench
        workbench.set_scenario_editable(False)
        workbench.show_via_transition(cfg, geometry, validation)
        workbench._update_via_field_states()
        contour = next(item for item in geometry.antipad_contours if item.layer == "L2")
        self.assertAlmostEqual(abs(contour.rotation_deg), 90.0, places=9)
        self.assertEqual(str(workbench.via_rotation.cget("state")), "disabled")
        self.assertEqual(str(workbench.via_length.cget("state")), "disabled")
        self.assertIn("atan2", workbench.via_field_hint.get())

    def test_11_return_via_layer_states_and_invalid_outline_are_explicit(self) -> None:
        from config_manager import ConfigManager
        from geometry_calculator import GeometryCalculator

        cfg = self.app._collect()
        cfg["scenario"]["type"] = "via_transition"
        anti = cfg["via_transition"]["padstack"]["antipad"]
        anti.update({"topology": "merged_pair", "shape": "capsule",
                     "direction_mode": "manual", "size_mode": "manual",
                     "rotation_deg": 0.0, "length": 0.8, "width": 0.6})
        cfg, _validation = ConfigManager(ROOT).validate(cfg, check_output_directory=False)
        geometry = GeometryCalculator(cfg).calculate()
        preview = self.app.antipad_workbench.preview
        preview.set_via_data(cfg, geometry, "mm", "L2")
        self.app.update_idletasks()
        self.assertTrue(preview.canvas.find_withtag("return-pad"))
        self.assertTrue(preview.canvas.find_withtag("return-drill"))
        preview.layer_var.set("L4")
        preview.redraw()
        self.app.update_idletasks()
        self.assertTrue(preview.canvas.find_withtag("return-clearance"))
        self.assertTrue(preview.canvas.find_withtag("return-label"))
        invalid = preview.canvas.find_withtag("via-antipad")
        self.assertTrue(invalid)
        self.assertTrue(any(str(preview.canvas.itemcget(item, "outline")) == "#dc2626"
                            for item in invalid))

    def test_12_current_layer_auto_modes_disable_ignored_inputs_and_explain_effective_geometry(self) -> None:
        self._realize_desktop_layout()
        cfg = self.app._collect()
        cfg["scenario"]["type"] = "via_transition"
        cfg["via_transition"]["padstack"]["antipad"]["per_layer"]["L2"] = {
            "enabled": True,
            "inherit_global": False,
            "topology": "merged_pair",
            "shape": "capsule",
            "direction_mode": "auto_pn",
            "size_mode": "auto_fit",
            "length": 0.2,
            "width": 0.2,
            "rotation_deg": -90.0,
            "clearance": 0.1,
            "end_clearance": 0.12,
            "side_clearance": 0.11,
        }
        self.app._load_to_widgets(cfg)
        self.app.notebook.select(self.app.tabs["反焊盘与叠层"])
        self.app.update()
        self.app.refresh_visuals()
        workbench = self.app.antipad_workbench
        workbench.select_layer_by_name("L2")
        self.app.refresh_visuals()
        self.app.update()

        self.assertEqual(workbench.via_layer_topology_var.get(), "P/N 合并开窗")
        self.assertEqual(workbench.via_layer_shape_var.get(), "胶囊形")
        self.assertEqual(workbench.via_layer_direction_var.get(), "自动沿 P/N 轴")
        self.assertEqual(workbench.via_layer_size_mode_var.get(), "自动适配")
        self.assertEqual(str(workbench.via_layer_widgets["length"].cget("state")), "disabled")
        self.assertEqual(str(workbench.via_layer_widgets["width"].cget("state")), "disabled")
        self.assertEqual(str(workbench.via_layer_widgets["rotation_deg"].cget("state")), "disabled")
        self.assertEqual(str(workbench.via_layer_widgets["end_clearance"].cget("state")), "normal")
        self.assertEqual(str(workbench.via_layer_widgets["side_clearance"].cget("state")), "normal")
        self.assertIn("尺寸自动计算", workbench.layer_effective_var.get())
        self.assertIn("角度自动计算", workbench.layer_effective_var.get())
        card_text = [
            workbench.preview.canvas.itemcget(item, "text")
            for item in workbench.preview.canvas.find_withtag("via-parameter-card")
            if workbench.preview.canvas.type(item) == "text"
        ]
        self.assertTrue(any("尺寸、角度由规则计算" in text for text in card_text), card_text)

    def test_13_manual_current_layer_edit_changes_effective_preview(self) -> None:
        cfg = self.app._collect()
        cfg["scenario"]["type"] = "via_transition"
        anti = cfg["via_transition"]["padstack"]["antipad"]
        anti["per_layer"]["L2"] = {
            "enabled": True,
            "inherit_global": False,
            "topology": "merged_pair",
            "shape": "capsule",
            "direction_mode": "manual",
            "size_mode": "manual",
            "length": 1.8,
            "width": 0.8,
            "rotation_deg": 90.0,
            "clearance": 0.1,
        }
        self.app._load_to_widgets(cfg)
        self.app.refresh_visuals()
        workbench = self.app.antipad_workbench
        workbench.select_layer_by_name("L2")
        self.app.refresh_visuals()
        self.assertEqual(str(workbench.via_layer_widgets["length"].cget("state")), "normal")

        workbench.via_layer_vars["length"].set(
            repr(convert_length(2.0, "mm", self.app.display_unit))
        )
        workbench._commit_layer_value_edit()
        self.app.refresh_visuals()
        contour = next(item for item in workbench.geometry.antipad_contours if item.layer == "L2")
        self.assertAlmostEqual(contour.length, 2.0)
        self.assertIn(
            format_length(2.0, self.app.display_unit),
            workbench.layer_effective_var.get(),
        )
        self.assertIn("采用手工尺寸/角度", workbench.layer_effective_var.get())

    def test_14_current_layer_clearance_edit_resizes_reference_envelope(self) -> None:
        cfg = self.app._collect()
        cfg["scenario"]["type"] = "via_transition"
        anti = cfg["via_transition"]["padstack"]["antipad"]
        anti["per_layer"]["L2"] = {
            "enabled": True,
            "inherit_global": False,
            "topology": "independent",
            "shape": "circle",
            "size_mode": "manual",
            "diameter": 0.9,
            "clearance": 0.05,
        }
        self.app._load_to_widgets(cfg)
        self.app.geometry("1600x1000+0+0")
        self.app.deiconify()
        self.app.notebook.select(self.app.tabs["反焊盘与叠层"])
        self.app.update()
        self.app.refresh_visuals()
        workbench = self.app.antipad_workbench
        workbench.select_layer_by_name("L2")
        workbench._show_via_help("clearance", "本层覆盖")
        self.app.refresh_visuals()
        self.app.update()
        self.app.update_idletasks()

        canvas = workbench.preview.canvas
        before = [canvas.bbox(item) for item in canvas.find_withtag("via-clearance-envelope")]
        self.assertEqual(len(before), 2)
        self.assertTrue(all(box is not None for box in before))

        workbench.via_layer_vars["clearance"].set(
            format_length(0.2, self.app.display_unit)
        )
        workbench._commit_layer_value_edit()
        self.app.refresh_visuals()
        self.app.update_idletasks()
        after = [canvas.bbox(item) for item in canvas.find_withtag("via-clearance-envelope")]
        self.assertEqual(len(after), 2)
        self.assertGreater(after[0][2] - after[0][0], before[0][2] - before[0][0])
        self.assertIn("浅蓝虚线圆", workbench.via_help_var.get() + " " + " ".join(
            canvas.itemcget(item, "text") for item in canvas.find_withtag("via-parameter-card")
            if canvas.type(item) == "text"
        ))

    def test_15_via_editor_prioritizes_layer_scope_and_hides_irrelevant_fields(self) -> None:
        self.app.variables["scenario.type"].set("via_transition")
        self.app.refresh_visuals()
        workbench = self.app.antipad_workbench
        workbench.set_scenario_editable(False)
        workbench.select_layer_by_name("L2")
        self.app.refresh_visuals()
        self.app.update_idletasks()

        antipad_content = workbench.settings_content
        packed = [str(widget) for widget in antipad_content.pack_slaves()]
        self.assertLess(packed.index(str(workbench.via_box)),
                        packed.index(str(workbench.via_status_box)))
        self.assertLess(packed.index(str(workbench.via_status_box)),
                        packed.index(str(workbench.layer_box)))
        self.assertEqual(workbench.via_layer_widgets["diameter"].winfo_manager(), "grid")
        self.assertEqual(workbench.via_layer_widgets["clearance"].winfo_manager(), "grid")
        self.assertEqual(workbench.via_layer_widgets["length"].winfo_manager(), "")
        self.assertEqual(workbench.via_layer_widgets["end_clearance"].winfo_manager(), "")

    def test_16_via_and_layer_widgets_have_one_authoritative_owner(self) -> None:
        from tkinter import ttk

        antipad_root = str(self.app.tabs["反焊盘与叠层"])
        via_root = str(self.app.tabs["VIA 换层"])
        for path, widget in self.app.widgets.items():
            if not path.startswith("via_transition."):
                continue
            layer_owned = (path.startswith("via_transition.padstack.antipad.")
                           or path.startswith("via_transition.padstack.antipad_per_layer"))
            expected_root = antipad_root if layer_owned else via_root
            self.assertTrue(str(widget).startswith(expected_root), (path, widget))
            self.assertIn(path, self.app.variables)
            if isinstance(widget, (ttk.Entry, ttk.Combobox, ttk.Checkbutton)):
                self.assertIs(widget, self.app.widgets[path])
        for path in (
            "via_transition.padstack.material",
            "via_transition.return_vias.barrel_model",
            "via_transition.return_vias.material",
        ):
            self.assertIn(path, self.app.widgets)
            self.assertTrue(str(self.app.widgets[path]).startswith(via_root), path)

    def test_17_dimension_navigation_selects_authoritative_subtab(self) -> None:
        self.app.variables["scenario.type"].set("via_transition")
        cases = {
            "via_transition.entry_trace.width": ("VIA 换层", "换层与传输线"),
            "via_transition.entry_trace.gap": ("VIA 换层", "换层与传输线"),
            "via_transition.exit_trace.center_to_center": ("VIA 换层", "换层与传输线"),
            "via_transition.backdrill.residual_stub": ("VIA 换层", "信号孔与背钻"),
            "via_transition.return_vias.pad_diameter": ("VIA 换层", "回流地孔"),
            "via_transition.padstack.antipad.diameter": ("反焊盘与叠层", None),
        }
        for path, (expected_page, expected_tab) in cases.items():
            self.app._navigate_to_parameter(path)
            self.app.update_idletasks()
            self.assertEqual(
                self.app.notebook.select(), str(self.app.tabs[expected_page]), path)
            if expected_tab is not None:
                selected = self.app.via_parameter_notebook.select()
                self.assertEqual(
                    str(self.app.via_parameter_notebook.tab(selected, "text")),
                    expected_tab, path)
            self.assertIn(path, self.app.status.get())

    def test_18_config_and_geometry_round_trip_are_unchanged_by_page_move(self) -> None:
        import copy
        from config_manager import ConfigManager
        from geometry_calculator import GeometryCalculator

        before, _ = ConfigManager(ROOT).validate(
            self.app._collect(), check_output_directory=False)
        before_geometry = GeometryCalculator(before).calculate().to_dict()
        snapshot = copy.deepcopy(before)
        self.app._load_to_widgets(snapshot)
        after, _ = ConfigManager(ROOT).validate(
            self.app._collect(), check_output_directory=False)
        after_geometry = GeometryCalculator(after).calculate().to_dict()
        self.assertEqual(after["via_transition"], before["via_transition"])
        self.assertEqual(after_geometry, before_geometry)

    def test_19_scenario_entry_defaults_to_route_and_hides_capacitor_gnd_tab(self) -> None:
        self.app.variables["scenario.type"].set("surface_capacitor")
        self.app.variables["scenario.type"].set("via_transition")
        self.app.update_idletasks()
        self.assertEqual(self.app.notebook.select(), str(self.app.tabs["VIA 换层"]))
        selected = self.app.via_parameter_notebook.select()
        self.assertEqual(self.app.via_parameter_notebook.tab(selected, "text"), "换层与传输线")
        self.assertEqual(str(self.app.notebook.tab(self.app.tabs["GND VIA"], "state")), "hidden")

    def test_20_plane_margins_are_editable_in_via_and_ac_scenarios(self) -> None:
        for scenario in ("via_transition", "surface_capacitor"):
            self.app.variables["scenario.type"].set(scenario)
            self.app.update_idletasks()
            for path in ("plane.margin_input", "plane.margin_output", "plane.margin_y"):
                self.assertEqual(str(self.app.widgets[path].cget("state")), "normal", (scenario, path))
        self.assertEqual(str(self.app.notebook.tab(self.app.tabs["GND VIA"], "state")), "normal")
        self.app.variables["scenario.type"].set("via_transition")

    def test_21_closed_combobox_wheel_never_changes_critical_layer(self) -> None:
        path = "via_transition.entry_layer"
        combo = self.app.widgets[path]
        before = self.app.variables[path].get()
        combo.event_generate("<MouseWheel>", delta=-120)
        self.app.update_idletasks()
        self.assertEqual(self.app.variables[path].get(), before)

    def test_22_return_custom_coordinate_table_round_trips_array(self) -> None:
        import copy

        original = copy.deepcopy(self.app._collect())
        try:
            config = copy.deepcopy(original)
            config["via_transition"]["return_vias"]["pattern"] = "custom"
            config["via_transition"]["return_vias"]["custom_coordinates"] = [
                [0.1, 0.2], [0.3, 0.4]]
            config["via_transition"]["return_vias"]["count"] = 2
            self.app._load_to_widgets(config)
            self.assertEqual(len(self.app.return_coordinate_tree.get_children()), 2)
            self.app.return_coordinate_x.set(
                repr(convert_length(0.5, "mm", self.app.display_unit))
            )
            self.app.return_coordinate_y.set(
                repr(convert_length(0.6, "mm", self.app.display_unit))
            )
            self.app._return_coordinate_add()
            collected = self.app._collect()
            self.assertEqual(collected["via_transition"]["return_vias"]["custom_coordinates"],
                             [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]])
            self.assertEqual(collected["via_transition"]["return_vias"]["count"], 3)
        finally:
            self.app._load_to_widgets(original)

    def test_23_each_via_subpage_has_one_non_nested_vertical_scroller(self) -> None:
        from capacitor_editor import ScrollableFrame

        for key, scroller in self.app.via_parameter_scrollers.items():
            descendants = list(scroller.content.winfo_children())
            self.assertFalse(any(isinstance(widget, ScrollableFrame)
                                 for widget in descendants), key)

    def test_24_signal_and_return_material_and_barrel_reach_geometry(self) -> None:
        import copy
        from config_manager import ConfigManager
        from geometry_calculator import GeometryCalculator

        original = copy.deepcopy(self.app._collect())
        try:
            self.app.variables["via_transition.padstack.barrel_model"].set(
                "实心（仅趋势近似）")
            self.app.variables["via_transition.padstack.material"].set("理想导体 PEC")
            self.app.variables["via_transition.return_vias.barrel_model"].set(
                "实心（仅趋势近似）")
            self.app.variables["via_transition.return_vias.material"].set("理想导体 PEC")
            config, _ = ConfigManager(ROOT).validate(
                self.app._collect(), check_output_directory=False)
            geometry = GeometryCalculator(config).calculate()
            self.assertTrue(all(via.barrel_model == "solid" and via.material == "pec"
                                for via in geometry.signal_vias))
            self.assertTrue(all(via.barrel_model == "solid" and via.material == "pec"
                                for via in geometry.return_vias))
        finally:
            self.app._load_to_widgets(original)

    def test_26_independent_entry_gap_does_not_change_exit_center_widget(self) -> None:
        self.app.variables["scenario.type"].set("via_transition")
        self.app.update_idletasks()
        exit_before = str(self.app.variables["via_transition.exit_trace.center_to_center"].get())
        path = "via_transition.entry_trace.gap"
        display = format_length(0.5, self.app.display_unit)
        self.app._length_mm_cache[path] = 0.5
        self.app._length_display_snapshot[path] = display
        self.app.variables[path].set(display)
        self.app._sync_via_fields()
        self.app.update_idletasks()
        self.assertEqual(
            str(self.app.variables["via_transition.exit_trace.center_to_center"].get()),
            exit_before)
        collected = self.app._collect()
        self.assertAlmostEqual(collected["via_transition"]["entry_trace"]["gap"], 0.5)
        self.assertAlmostEqual(collected["via_transition"]["exit_trace"]["gap"], 0.7)
        self.assertNotIn("center_to_center", collected["via_transition"]["entry_trace"])
        self.assertNotIn("center_to_center", collected["via_transition"]["exit_trace"])

    def test_25_every_default_via_leaf_has_one_gui_binding(self) -> None:
        def leaves(node, prefix="via_transition"):
            result = []
            if isinstance(node, dict):
                for key, value in node.items():
                    result.extend(leaves(value, prefix + "." + key))
            elif isinstance(node, list):
                result.append(prefix)
            else:
                result.append(prefix)
            return result

        default_via = self.app.manager.default()["via_transition"]
        paths = leaves(default_via)
        self.assertEqual(len(paths), 65)
        self.assertEqual([path for path in paths if path not in self.app.variables], [])

    def test_27_transmission_line_group_packs_above_fanout(self) -> None:
        self._enter_via_scenario()
        self._restore_default_via_gaps()
        self._realize_desktop_layout()
        self.app.notebook.select(self.app.tabs["VIA 换层"])
        self.app.via_parameter_notebook.select(self.app.via_parameter_tabs["route"])
        self.app.update()
        content = self.app.via_parameter_scrollers["route"].content
        slaves = list(content.pack_slaves())
        trace_group = self.app.via_trace_group
        self.assertIn(trace_group, slaves)
        self.assertLess(slaves.index(trace_group), slaves.index(self.app.via_fanout_collapsed_hint))
        self.assertFalse(bool(self.app.via_fanout_group.winfo_manager()))
        self.assertTrue(bool(self.app.via_fanout_collapsed_hint.winfo_manager()))
        self.assertLess(trace_group.winfo_y(), self.app.via_fanout_collapsed_hint.winfo_y())
        gap_widget = self.app.widgets["via_transition.entry_trace.gap"]
        self.assertLess(gap_widget.winfo_rooty(), self.app.via_fanout_collapsed_hint.winfo_rooty())

        path = "via_transition.entry_trace.gap"
        display = format_length(0.5, self.app.display_unit)
        self.app._length_mm_cache[path] = 0.5
        self.app._length_display_snapshot[path] = display
        self.app.variables[path].set(display)
        self.app._sync_via_fields()
        self.app.update()
        slaves = list(content.pack_slaves())
        self.assertTrue(bool(self.app.via_fanout_group.winfo_manager()))
        self.assertFalse(bool(self.app.via_fanout_collapsed_hint.winfo_manager()))
        self.assertLess(slaves.index(trace_group), slaves.index(self.app.via_fanout_group))
        self._restore_default_via_gaps()

    def test_28_center_widgets_are_readonly_gray_with_width_plus_gap_copy(self) -> None:
        self._enter_via_scenario()
        self.app.update_idletasks()
        for path in (
            "via_transition.entry_trace.center_to_center",
            "via_transition.exit_trace.center_to_center",
        ):
            widget = self.app.widgets[path]
            before = str(self.app.variables[path].get())
            self.assertEqual(str(widget.cget("state")), "readonly")
            self.assertIsInstance(widget, tkinter.Entry)
            self.assertNotIsInstance(widget, ttk.Entry)
            self.assertEqual(str(widget.cget("readonlybackground")).lower(), "#eef2f7")
            self.assertIn("宽度 + GAP", str(self.app.formula_labels[path].cget("text")))
            widget.focus_set()
            widget.event_generate("<KeyPress>", keysym="9", keycode=57)
            try:
                widget.insert("end", "9")
            except tkinter.TclError:
                pass
            self.app.update_idletasks()
            self.assertEqual(str(self.app.variables[path].get()), before)

    def test_29_unit_toggle_mm_refreshes_gap_center_and_preview_labels(self) -> None:
        self._enter_via_scenario()
        self._restore_default_via_gaps()
        self._realize_desktop_layout()
        self.app.notebook.select(self.app.tabs["VIA 换层"])
        original = self.app.display_unit
        try:
            self.app.unit_var.set("mm")
            self.app._unit_changed()
            self.app.update()
            self.app.refresh_visuals()
            self.app.update()
            gap = str(self.app.variables["via_transition.entry_trace.gap"].get())
            center = str(self.app.variables["via_transition.entry_trace.center_to_center"].get())
            pitch = str(self.app.variables["via_transition.signal_via_pitch"].get())
            width = str(self.app.variables["via_transition.entry_trace.width"].get())
            self.assertAlmostEqual(float(gap), 0.7, places=2)
            self.assertAlmostEqual(float(center), 0.8, places=2)
            self.assertAlmostEqual(float(pitch), 0.8, places=2)
            self.assertAlmostEqual(float(width), 0.1, places=2)
            self.assertNotIn("31.4961", gap + center + pitch)
            self.assertNotIn("27.5591", gap + center)
            labels = self._canvas_texts(self.app.via_top_canvas)
            joined = "\n".join(labels)
            self.assertTrue(any("输入 GAP" in text and "mm" in text for text in labels), labels)
            self.assertTrue(any("输出 GAP" in text and "mm" in text for text in labels), labels)
            self.assertTrue(any("输入中心距" in text and "mm" in text for text in labels), labels)
            self.assertNotIn("31.4961", joined)
            self.assertNotIn("27.5591", joined)
            self.assertFalse(any(re.search(r"\bmil\b", text) for text in labels), labels)
            self.assertTrue(any(re.search(r"\bmm\b", text) for text in labels), labels)
        finally:
            self.app.unit_var.set(original)
            self.app._unit_changed()
            self.app.update()
            self._restore_default_via_gaps()

    def test_30_mil_length_widgets_and_labels_use_at_most_two_decimals(self) -> None:
        self._enter_via_scenario()
        self._restore_default_via_gaps()
        if self.app.display_unit != "mil":
            self.app.unit_var.set("mil")
            self.app._unit_changed()
            self.app.update()
        self.app.refresh_visuals()
        self.app.update()
        length_paths = (
            "via_transition.entry_trace.length",
            "via_transition.entry_trace.width",
            "via_transition.entry_trace.gap",
            "via_transition.entry_trace.center_to_center",
            "via_transition.exit_trace.length",
            "via_transition.exit_trace.width",
            "via_transition.exit_trace.gap",
            "via_transition.exit_trace.center_to_center",
            "via_transition.signal_via_pitch",
        )
        for path in length_paths:
            shown = str(self.app.variables[path].get())
            self.assertLessEqual(self._fraction_digits(shown), 2, path)
            self.assertNotRegex(shown, r"\d+\.\d{3,}")
            self.assertNotIn("31.4961", shown)
            self.assertNotIn("27.5591", shown)
            self.assertNotIn("118.1102", shown)
        labels = self._canvas_texts(self.app.via_top_canvas)
        joined = "\n".join(labels)
        self.assertNotIn("31.4961", joined)
        self.assertNotIn("27.5591", joined)
        self.assertNotIn("118.1102", joined)
        length_tokens = ("GAP", "中心距", "输入宽", "输出宽", "输入长", "输出长", "信号孔")
        for text in labels:
            if any(token in text for token in length_tokens):
                for match in re.findall(r"\d+\.\d+", text):
                    self.assertLessEqual(self._fraction_digits(match), 2, text)
        feedback = self.app.via_feedback_var.get()
        self.assertNotIn("31.4961", feedback)
        self.assertNotIn("118.1102", feedback)
        for match in re.findall(r"\d+\.\d+", feedback):
            self.assertLessEqual(self._fraction_digits(match), 2, match)

    def test_31_empty_error_rows_are_omitted_and_real_errors_lead(self) -> None:
        from config_manager import ConfigManager
        from geometry_calculator import GeometryCalculator

        self._enter_via_scenario()
        config, validation = ConfigManager(ROOT).validate(
            self.app._collect(), check_output_directory=False)
        geometry = GeometryCalculator(config).calculate()
        self.app._update_via_editor(config, geometry, validation)
        feedback = self.app.via_feedback_var.get()
        self.assertIsNone(re.search(r"错误\s*\d+\s*[：:]\s*$", feedback, re.M))
        for line in feedback.splitlines():
            self.assertFalse(re.fullmatch(r"错误\s*\d+\s*[：:]\s*", line))

        with patch("via_transition_semantics.validate_via_transition", return_value=[
            ("VIA-EMPTY", "error", ""),
            ("VIA-EMPTY2", "error", "   "),
            ("VIA-REAL", "error", "真实错误必须完整可见"),
        ]):
            self.app._update_via_editor(config, geometry, validation)
        feedback = self.app.via_feedback_var.get()
        self.assertIn("真实错误必须完整可见", feedback)
        self.assertTrue(feedback.splitlines()[0].startswith("错误"))
        self.assertIn("VIA-REAL", feedback.splitlines()[0])
        self.assertNotIn("错误 3:", feedback)
        self.assertIsNone(re.search(r"错误\s*\d+\s*[：:]\s*$", feedback, re.M))
        self.assertGreaterEqual(int(self.app.via_feedback_text.cget("height")), 5)

    def test_32_preview_gap_label_click_focuses_matching_entry(self) -> None:
        self._enter_via_scenario()
        self._restore_default_via_gaps()
        self._realize_desktop_layout()
        self.app.notebook.select(self.app.tabs["VIA 换层"])
        self.app.refresh_visuals()
        self.app.update()
        canvas = self.app.via_top_canvas
        cases = (
            ("via_transition.entry_trace.gap", "输入 GAP"),
            ("via_transition.exit_trace.gap", "输出 GAP"),
        )
        for path, prefix in cases:
            self._click_dimension_label(canvas, path, prefix)
            widget = self.app.widgets[path]
            self.assertIs(self.app.focus_get(), widget, path)
            selected = self.app.via_parameter_notebook.select()
            self.assertEqual(
                str(self.app.via_parameter_notebook.tab(selected, "text")),
                "换层与传输线",
                path,
            )
            self.assertIn(path, self.app.status.get())

    def test_33_via_generate_and_run_is_offline_only(self) -> None:
        self._enter_via_scenario()
        with patch("main_gui.messagebox.showinfo") as info, patch.object(
            self.app, "_generate"
        ) as generate:
            self.app.generate_and_run()
        info.assert_called_once()
        generate.assert_not_called()
        self.assertIn("离线", self.app.status.get())
        self.assertIn("未启动 HFSS", self.app.status.get())

    def test_34_cross_section_right_edge_is_not_clipped(self) -> None:
        self._enter_via_scenario()
        self._realize_desktop_layout()
        self.app.notebook.select(self.app.tabs["VIA 换层"])
        self.app.refresh_visuals()
        self.app.update()
        canvas = self.app.via_cross_canvas
        width = canvas.winfo_width()
        self.assertGreater(width, 50)
        overflow = []
        for item in canvas.find_all():
            bbox = canvas.bbox(item)
            if bbox is None:
                continue
            if bbox[2] > width + 1:
                label = canvas.itemcget(item, "text") if canvas.type(item) == "text" else canvas.type(item)
                overflow.append((label, bbox, width))
        self.assertEqual(overflow, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
