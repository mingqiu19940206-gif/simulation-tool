from __future__ import annotations

import copy
import dataclasses
import os
import sys
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from geometry_calculator import GeometryCalculator
from gnd_via_semantics import derive_via_values
import main_gui
from main_gui import SurfaceCapAntipadApp


SIZES = ((1120, 720), (1280, 800), (1900, 1050), (2560, 1400))
FONT_TIERS = ("normal", "large", "ultra")


class MainGuiLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.app = SurfaceCapAntipadApp()
            self.app.withdraw()
            self.app.update_idletasks()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tk GUI unavailable: {exc}") from exc

    def tearDown(self) -> None:
        app = getattr(self, "app", None)
        if app is None:
            return
        try:
            exists = bool(app.winfo_exists())
        except tk.TclError:
            exists = False
        if exists:
            app.destroy()

    def test_destroy_closes_root_and_clears_default_root(self) -> None:
        child = tk.Frame(self.app)
        child.pack()
        child.after_idle(lambda: None)
        self.app.after_idle(lambda: None)

        self.assertIs(tk._default_root, self.app)
        self.app.destroy()

        try:
            root_still_exists = bool(self.app.winfo_exists())
        except tk.TclError:
            root_still_exists = False
        self.assertFalse(root_still_exists)
        self.assertIsNone(tk._default_root)
        self.app = None

    def test_callback_errors_keep_interactive_feedback_and_skip_headless_dialogs(self) -> None:
        with mock.patch.object(main_gui, "_write_crash_log") as write_log, \
                mock.patch.object(main_gui.messagebox, "showerror") as show_error, \
                mock.patch.dict(os.environ, {"SURFACE_CAP_HEADLESS": ""}):
            try:
                raise RuntimeError("interactive callback probe")
            except RuntimeError:
                self.app.report_callback_exception(*sys.exc_info())

            show_error.assert_called_once()
            self.assertIn("interactive callback probe", show_error.call_args.args[1])
            self.assertIn("界面操作失败", self.app.status.get())
            self.assertEqual(write_log.call_count, 1)

            with mock.patch.dict(os.environ, {"SURFACE_CAP_HEADLESS": "1"}):
                try:
                    raise RuntimeError("headless callback probe")
                except RuntimeError:
                    self.app.report_callback_exception(*sys.exc_info())

            show_error.assert_called_once()
            self.assertEqual(write_log.call_count, 2)


class GuiLayoutRegressionTests(unittest.TestCase):
    """Pixel-space regression coverage for every font tier and target viewport."""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.app = SurfaceCapAntipadApp()
            cls.app.minsize(1, 1)
            cls.app.deiconify()
            cls.app.variables["simulation.enabled"].set(True)
            cls.app.variables["gnd_vias.enabled"].set(True)
            cls.app.refresh_visuals()
            cls.app.model_preview_workbench.update_pending()
            cls.app.update()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tk GUI unavailable: {exc}") from exc

    @classmethod
    def tearDownClass(cls) -> None:
        app = getattr(cls, "app", None)
        if app is None:
            return
        app.destroy()

    def _settle(self) -> None:
        self.app.update_idletasks()
        self.app.update()
        self.app.update_idletasks()

    @staticmethod
    def _text_items(canvas: tk.Canvas, *tags: str) -> list[int]:
        if not tags:
            source = canvas.find_all()
        else:
            source = {item for tag in tags for item in canvas.find_withtag(tag)}
        return sorted(item for item in source if canvas.type(item) == "text")

    def _assert_inside_viewport(self, canvas: tk.Canvas, items: list[int], label: str) -> None:
        width, height = canvas.winfo_width(), canvas.winfo_height()
        self.assertGreater(width, 2, label)
        self.assertGreater(height, 2, label)
        for item in items:
            box = canvas.bbox(item)
            self.assertIsNotNone(box, (label, item))
            assert box is not None
            self.assertGreaterEqual(box[0], -2, (label, item, box, width, height))
            self.assertGreaterEqual(box[1], -2, (label, item, box, width, height))
            self.assertLessEqual(box[2], width + 2, (label, item, box, width, height))
            self.assertLessEqual(box[3], height + 2, (label, item, box, width, height))

    def _assert_inside_scrollregion(self, canvas: tk.Canvas, items: list[int], label: str) -> tuple[float, ...]:
        region = tuple(float(value) for value in str(canvas.cget("scrollregion")).split())
        self.assertEqual(len(region), 4, label)
        for item in items:
            box = canvas.bbox(item)
            self.assertIsNotNone(box, (label, item))
            assert box is not None
            self.assertGreaterEqual(box[0], region[0] - 2, (label, item, box, region))
            self.assertGreaterEqual(box[1], region[1] - 2, (label, item, box, region))
            self.assertLessEqual(box[2], region[2] + 2, (label, item, box, region))
            self.assertLessEqual(box[3], region[3] + 2, (label, item, box, region))
        return region

    def _assert_no_text_overlap(self, canvas: tk.Canvas, items: list[int], label: str) -> None:
        boxes = [(item, canvas.bbox(item)) for item in items]
        boxes = [(item, box) for item, box in boxes if box is not None]
        for index, (first_item, first) in enumerate(boxes):
            assert first is not None
            for second_item, second in boxes[index + 1:]:
                assert second is not None
                intersection_width = min(first[2], second[2]) - max(first[0], second[0])
                intersection_height = min(first[3], second[3]) - max(first[1], second[1])
                self.assertFalse(
                    intersection_width > 1 and intersection_height > 1,
                    (label, first_item, second_item, first, second,
                     canvas.itemcget(first_item, "text"), canvas.itemcget(second_item, "text")),
                )

    def _assert_toolbar_children_fit(self, toolbar: tk.Widget, label: str) -> None:
        left, top = toolbar.winfo_rootx(), toolbar.winfo_rooty()
        right, bottom = left + toolbar.winfo_width(), top + toolbar.winfo_height()

        def descendants(widget: tk.Widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        for child in descendants(toolbar):
            if not child.winfo_ismapped() or not isinstance(child, (tk.Button, tk.Checkbutton, tk.Label)):
                # ttk widgets are identified below by their Tk class name.
                if child.winfo_class() not in {"TButton", "TCheckbutton", "TLabel", "TCombobox"}:
                    continue
            child_box = (
                child.winfo_rootx(), child.winfo_rooty(),
                child.winfo_rootx() + child.winfo_width(),
                child.winfo_rooty() + child.winfo_height(),
            )
            self.assertGreaterEqual(child_box[0], left - 2, (label, child, child_box))
            self.assertGreaterEqual(child_box[1], top - 2, (label, child, child_box))
            self.assertLessEqual(child_box[2], right + 2, (label, child, child_box, right))
            self.assertLessEqual(child_box[3], bottom + 2, (label, child, child_box, bottom))

    @staticmethod
    def _widget_box(widget: tk.Widget) -> tuple[int, int, int, int]:
        return (
            widget.winfo_rootx(), widget.winfo_rooty(),
            widget.winfo_rootx() + widget.winfo_width(),
            widget.winfo_rooty() + widget.winfo_height(),
        )

    def _assert_compact_selector(
        self, selector: tk.Widget, allowed_columns: set[int], label: str,
    ) -> None:
        self.assertIn(selector.current_columns, allowed_columns, label)
        selector_box = self._widget_box(selector)
        card_boxes = []
        for mode, card in selector.cards.items():
            self.assertTrue(card.winfo_ismapped(), (label, mode))
            card_box = self._widget_box(card)
            card_boxes.append((mode, card_box))
            self.assertGreaterEqual(card_box[0], selector_box[0] - 2, (label, mode, card_box))
            self.assertGreaterEqual(card_box[1], selector_box[1] - 2, (label, mode, card_box))
            self.assertLessEqual(card_box[2], selector_box[2] + 2, (label, mode, card_box))
            self.assertLessEqual(card_box[3], selector_box[3] + 2, (label, mode, card_box))
            grid = card.grid_info()
            self.assertLess(int(grid["column"]), selector.current_columns, (label, mode, grid))
            for widget in selector.card_widgets[mode]:
                if not isinstance(widget, tk.Label):
                    continue
                text = str(widget.cget("text"))
                self.assertNotIn("\n", text, (label, mode, text))
                self.assertEqual(int(float(widget.cget("wraplength"))), 0, (label, mode, text))
                self.assertGreaterEqual(widget.winfo_width() + 2, widget.winfo_reqwidth(), (label, mode, text))

        for index, (first_mode, first) in enumerate(card_boxes):
            for second_mode, second in card_boxes[index + 1:]:
                intersection_width = min(first[2], second[2]) - max(first[0], second[0])
                intersection_height = min(first[3], second[3]) - max(first[1], second[1])
                self.assertFalse(
                    intersection_width > 1 and intersection_height > 1,
                    (label, first_mode, second_mode, first, second),
                )
        description = selector.description_label
        self.assertTrue(description.winfo_ismapped(), label)
        self.assertGreaterEqual(int(float(description.cget("wraplength"))), selector.winfo_width() - 30)
        card_bottom = max(box[3] for _mode, box in card_boxes)
        self.assertGreaterEqual(description.winfo_rooty(), card_bottom, label)

    def _show_page(self, name: str) -> None:
        self.app.show_workspace_page(name)
        self._settle()

    def _assert_via_layout(self) -> None:
        workbench = self.app.gnd_via_workbench
        workbench.preview.redraw()
        workbench.cross_section.redraw()
        self._settle()
        preview_text = self._text_items(workbench.preview)
        legend = self._text_items(workbench.preview, "legend-label")
        self.assertEqual(len(legend), 8)
        self._assert_inside_viewport(workbench.preview, preview_text, "GND VIA top preview")
        self._assert_no_text_overlap(workbench.preview, legend, "GND VIA legend")

        cross = workbench.cross_section
        layers = self._text_items(cross, "layer-label")
        self.assertEqual(len(layers), 29)
        self.assertEqual(
            [len(self._text_items(cross, f"via-layer-{index}")) for index in range(29)],
            [1] * 29,
        )
        self._assert_no_text_overlap(cross, layers, "GND VIA layer labels")
        region = self._assert_inside_scrollregion(cross, self._text_items(cross), "GND VIA cross-section")
        last_box = cross.bbox(layers[-1])
        assert last_box is not None
        self.assertLessEqual(last_box[3], region[3])
        outer_region = tuple(float(value) for value in str(workbench.right_scroll.canvas.cget("scrollregion")).split())
        self.assertEqual(len(outer_region), 4)
        self.assertGreater(outer_region[3], workbench.right_scroll.canvas.winfo_height())
        self._assert_compact_selector(workbench.mode_cards, {1, 2, 4}, "GND VIA mode selector")
        selected = workbench.mode_cards.variable.get()
        self.assertEqual(workbench.mode_cards.cards[selected].cget("highlightbackground"), "#2563eb")
        for mode, card in workbench.mode_cards.cards.items():
            if mode != selected:
                self.assertEqual(card.cget("highlightbackground"), "#cbd5e1")

    def _assert_antipad_layout(self) -> None:
        workbench = self.app.antipad_workbench
        workbench.preview.redraw()
        workbench.cross_section.redraw()
        self._settle()
        self._assert_inside_viewport(
            workbench.preview.canvas,
            self._text_items(workbench.preview.canvas),
            "antipad top preview",
        )
        cross = workbench.cross_section.canvas
        layers = self._text_items(cross, "layer-label")
        self.assertEqual(len(layers), 29)
        self.assertEqual(
            [len(self._text_items(cross, f"stack-layer-{index}")) for index in range(29)],
            [1] * 29,
        )
        self._assert_no_text_overlap(cross, layers, "stackup two-line labels")
        region = self._assert_inside_scrollregion(cross, self._text_items(cross), "stackup cross-section")
        last_box = cross.bbox(layers[-1])
        assert last_box is not None
        self.assertLessEqual(last_box[3], region[3])
        cross.yview_moveto(1.0)
        self._settle()
        self.assertGreaterEqual(cross.canvasy(cross.winfo_height()), last_box[3] - 3)
        cross.yview_moveto(0.0)
        selector = workbench.mode_selector
        self._assert_compact_selector(selector, {1, 3}, "antipad mode selector")
        selected = selector.variable.get()
        self.assertEqual(selector.cards[selected].cget("highlightbackground"), "#2563eb")
        for mode, status_label in selector.status_labels.items():
            border = selector.cards[mode].cget("highlightbackground")
            self.assertIn(border, {"#2563eb", "#cbd5e1"})
            if "⚠" in str(status_label.cget("text")):
                self.assertEqual(str(status_label.cget("foreground")), "#b45309")
        semantic_width = int(float(workbench.semantic_hint_label.cget("wraplength")))
        self.assertGreaterEqual(semantic_width, workbench.semantic_hint_label.master.winfo_width() - 30)

    def _assert_capacitor_layout(self) -> None:
        canvas = self.app.cap_geometry_canvas
        canvas._redraw_last()
        self._settle()
        all_text = self._text_items(canvas)
        region = self._assert_inside_scrollregion(canvas, all_text, "capacitor geometry")
        self.assertGreaterEqual(region[3], canvas.winfo_height())
        summary = self._text_items(canvas, "dimension-summary-label")
        self.assertGreater(len(summary), 10)
        self._assert_no_text_overlap(canvas, summary, "capacitor dimension summary")
        plotted = sorted(set(
            self._text_items(canvas, "dimension-label")
            + self._text_items(canvas, "axis-label")
            + self._text_items(canvas, "object-label")
        ))
        self._assert_no_text_overlap(canvas, plotted, "capacitor plotted labels")
        semantic_text = [canvas.itemcget(item, "text") for item in plotted]
        for required_label in ("P", "N", "X", "Y"):
            self.assertEqual(semantic_text.count(required_label), 1, required_label)
        for path in ("capacitor.pad_pitch", "capacitor.cap_pitch"):
            labels = [item for item in self._text_items(canvas, f"dimension:{path}")
                      if "dimension-label" in canvas.gettags(item)]
            self.assertEqual(len(labels), 1, path)
            self.assertEqual(canvas._dimension_paths[labels[0]], path)
        canvas.yview_moveto(1.0)
        self._settle()
        last_box = max((canvas.bbox(item) for item in all_text if canvas.bbox(item)), key=lambda box: box[3])
        self.assertGreaterEqual(canvas.canvasy(canvas.winfo_height()), last_box[3] - 3)
        canvas.yview_moveto(0.0)

        model_canvas = self.app.model_canvas
        config = copy.deepcopy(self.app._collect())
        mode_titles = {
            "ideal_bridge": "理想短路",
            "lumped_rlc": "集总串联",
            "sparameter": "Touchstone S 参数模型",
            "open": "开路",
        }
        for mode, expected_title in mode_titles.items():
            # A large VIA editor makes ultra-font reflow long enough for the
            # application's normal 120 ms live-refresh timer to fire while this
            # helper is deliberately previewing an off-widget model config.
            # Cancel that unrelated pending live refresh so this canvas-only
            # layout assertion remains deterministic.
            pending_refresh = self.app._refresh_job
            if pending_refresh:
                self.app.after_cancel(pending_refresh)
                self.app._refresh_job = None
            config["capacitor_model"]["mode"] = mode
            model_canvas.show(config)
            self._settle()
            all_model_text = self._text_items(model_canvas)
            self.assertGreaterEqual(len(all_model_text), 5, mode)
            self._assert_inside_viewport(model_canvas, all_model_text, f"HFSS model {mode}")
            self._assert_no_text_overlap(model_canvas, all_model_text, f"HFSS model {mode}")
            texts = [model_canvas.itemcget(item, "text") for item in all_model_text]
            self.assertTrue(any(expected_title in text for text in texts), (mode, texts))
            self.assertIn("P、N 两颗电容使用相同模型", texts, mode)
            summary = self._text_items(model_canvas, "model-summary")
            self.assertEqual(len(summary), 1, mode)
            self.assertEqual(model_canvas.itemcget(summary[0], "font"), "ViaSmallFont")
            circuit_box = model_canvas.bbox("circuit")
            self.assertIsNotNone(circuit_box, mode)
            assert circuit_box is not None
            left_margin = circuit_box[0]
            right_margin = model_canvas.winfo_width() - circuit_box[2]
            self.assertAlmostEqual(left_margin, right_margin, delta=3, msg=mode)
            if model_canvas.winfo_width() > 700:
                self.assertLess(circuit_box[2] - circuit_box[0], model_canvas.winfo_width() * .85, mode)
        self.assertGreaterEqual(int(float(model_canvas.cget("height"))), 130)

    def _assert_model_layout(self) -> None:
        workbench = self.app.model_preview_workbench
        workbench.set_view("等轴测")
        workbench._software_redraw()
        self._settle()
        canvas = workbench.software_canvas
        all_text = self._text_items(canvas)
        self._assert_inside_viewport(canvas, all_text, "software 3D")
        screen_labels = sorted(set(
            self._text_items(canvas, "hud-label")
            + self._text_items(canvas, "screen-label")
        ))
        self._assert_no_text_overlap(canvas, screen_labels, "software 3D screen-space labels")
        ports = self._text_items(canvas, "port-label")
        self.assertEqual(len(ports), 4)
        self.assertEqual(workbench.software_view, "等轴测")
        combined = [canvas.itemcget(item, "text") for item in self._text_items(canvas, "object-label")]
        combined_count = sum("AIRBOX" in text and "开放边界" in text for text in combined)
        hidden_count = sum("AIRBOX" in text and "开放边界" in text
                           for text in workbench._hidden_screen_labels)
        self.assertEqual(combined_count + hidden_count, 1)

    def test_full_viewport_and_font_matrix_has_no_canvas_text_collisions(self) -> None:
        for width, height in SIZES:
            for tier in FONT_TIERS:
                with self.subTest(size=(width, height), tier=tier):
                    self.app.geometry(f"{width}x{height}+0+0")
                    self.app.set_responsive_font_tier(tier)
                    self._settle()
                    actual_size = (self.app.winfo_width(), self.app.winfo_height())
                    if width <= 1900:
                        self.assertEqual(actual_size, (width, height))
                    else:
                        # Windows clamps an oversized request to the real work
                        # area.  This is the accepted "2560×1400 or maximized"
                        # case and still exercises the largest real viewport.
                        self.assertGreaterEqual(actual_size[0], 1900)
                        self.assertGreaterEqual(actual_size[1], 1050)
                    self.assertEqual(self.app._responsive_font_tier, tier)

                    self._show_page("GND VIA")
                    self._assert_via_layout()
                    self._show_page("反焊盘与叠层")
                    self._assert_antipad_layout()
                    self._show_page("电容与模型")
                    self._assert_capacitor_layout()
                    self._show_page("模型预览")
                    self._assert_model_layout()

    def test_capacitor_page_fills_tall_viewport_without_trailing_blank(self) -> None:
        self.app.set_responsive_font_tier("normal")
        self._show_page("电容与模型")

        def snapshot(width: int, height: int) -> tuple[int, ...]:
            self.app.geometry(f"{width}x{height}+0+0")
            for _ in range(3):
                self._settle()
            scroller = self.app.cap_page_scroller
            scroller.refresh_layout()
            self.app._schedule_capacitor_page_layout()
            for _ in range(3):
                self._settle()

            viewport_width = scroller.canvas.winfo_width()
            viewport_height = scroller.canvas.winfo_height()
            window_box = scroller.canvas.bbox(scroller._window)
            self.assertIsNotNone(window_box)
            assert window_box is not None
            self.assertEqual(window_box[:2], (0, 0))
            self.assertAlmostEqual(window_box[2], viewport_width, delta=2)
            self.assertGreaterEqual(window_box[3], viewport_height - 2)

            region = tuple(float(value) for value in str(scroller.canvas.cget("scrollregion")).split())
            self.assertEqual(len(region), 4)
            self.assertAlmostEqual(region[2], viewport_width, delta=2)
            self.assertAlmostEqual(region[3], window_box[3], delta=2)

            content = self.app.cap_page_content
            status = self.app.cap_status_box
            trailing_gap = content.winfo_height() - (status.winfo_y() + status.winfo_height())
            self.assertLessEqual(trailing_gap, 4)
            preview_height = int(float(self.app.cap_geometry_canvas.cget("height")))
            self.assertGreaterEqual(preview_height, 250)
            self.assertLessEqual(preview_height, 440)
            return (
                viewport_width, viewport_height, window_box[3],
                content.winfo_reqheight(), content.winfo_height(),
                self.app.capacitor_panes.winfo_height(), preview_height,
                trailing_gap,
            )

        small = snapshot(1120, 720)
        large_width = min(3800, max(1900, self.app.winfo_screenwidth() - 40))
        large_height = min(2050, max(1050, self.app.winfo_screenheight() - 80))
        tall = snapshot(large_width, large_height)
        self.assertGreater(tall[1], small[1])
        self.assertGreater(tall[5], small[5])
        self.assertGreaterEqual(tall[6], small[6])
        if tall[3] <= tall[1]:
            # When the natural content fits, the window must consume exactly
            # the viewport and its panes receive the otherwise blank height.
            self.assertAlmostEqual(tall[2], tall[1], delta=2)
            self.assertAlmostEqual(tall[4], tall[1], delta=2)
            self.assertEqual(tuple(self.app.cap_page_scroller.canvas.yview()), (0.0, 1.0))

        # A shrink/grow cycle must return to the same allocation and not feed
        # a Configure -> after_idle -> Configure loop.
        snapshot(1280, 800)
        repeated_tall = snapshot(large_width, large_height)
        self.assertEqual(repeated_tall, tall)

    def test_collect_does_not_schedule_another_refresh(self) -> None:
        pending = self.app._refresh_job
        if pending:
            self.app.after_cancel(pending)
            self.app._refresh_job = None

        connect_layers = self.app.variables[
            "via_transition.return_vias.connect_layers"
        ].get()
        coordinates = self.app.variables[
            "via_transition.return_vias.custom_coordinates"
        ].get()
        self.app._collect()
        self.app._collect()

        self.assertIsNone(self.app._refresh_job)
        self.assertEqual(self.app.variables[
            "via_transition.return_vias.connect_layers"].get(), connect_layers)
        self.assertEqual(self.app.variables[
            "via_transition.return_vias.custom_coordinates"].get(), coordinates)

    def test_short_stack_units_disabled_vias_and_repeated_redraw_are_stable(self) -> None:
        self.app.geometry("1120x720+0+0")
        self.app.set_responsive_font_tier("ultra")
        self._settle()
        full_config, validation = self.app.manager.validate(
            self.app._collect(), check_output_directory=False,
        )
        full_geometry = GeometryCalculator(full_config).calculate()
        full_derived = derive_via_values(full_config, full_geometry)

        short_geometry = SimpleNamespace(stackup=tuple(full_geometry.stackup[:5]))
        short_derived = dataclasses.replace(full_derived, layers=full_derived.layers[:5])
        self.app.antipad_workbench.cross_section.set_data(short_geometry, "mm", 4)
        self.app.gnd_via_workbench.cross_section.show(full_config, short_derived, "mm")
        self._settle()
        self.assertEqual(len(self._text_items(self.app.antipad_workbench.cross_section.canvas, "layer-label")), 5)
        self.assertEqual(len(self._text_items(self.app.gnd_via_workbench.cross_section, "layer-label")), 5)

        # Restore the formal 29-layer M6G model before unit/state coverage.
        self.app.antipad_workbench.show(full_config, full_geometry, validation)
        self.app.gnd_via_workbench.show(full_config, full_geometry, validation)
        physical_before = self.app._collect()["capacitor"]["pad_length"]
        for unit in ("mil", "mm"):
            self.app.unit_var.set(unit)
            self.app._unit_changed()
            self.app.refresh_visuals()
            self._settle()
            self.assertEqual(self.app.display_unit, unit)
            self.assertAlmostEqual(self.app._collect()["capacitor"]["pad_length"], physical_before, places=10)

        for enabled in (False, True):
            self.app.variables["gnd_vias.enabled"].set(enabled)
            self.app.refresh_visuals()
            self._show_page("GND VIA")
            self._assert_via_layout()

        canvas = self.app.gnd_via_workbench.preview
        signature_before = [
            (canvas.gettags(item), canvas.itemcget(item, "text"), canvas.bbox(item))
            for item in self._text_items(canvas)
        ]
        for _ in range(3):
            canvas.redraw()
            self._settle()
        signature_after = [
            (canvas.gettags(item), canvas.itemcget(item, "text"), canvas.bbox(item))
            for item in self._text_items(canvas)
        ]
        self.assertEqual(signature_after, signature_before)

    def test_paned_resizing_scroll_reachability_selection_and_toolbars(self) -> None:
        self.app.geometry("1120x720+0+0")
        self.app.set_responsive_font_tier("ultra")
        self._show_page("GND VIA")
        gnd_panes = self.app.gnd_via_workbench.panes
        gnd_panes.sashpos(0, max(280, min(545, int(gnd_panes.winfo_width() * 0.52))))
        self._settle()
        self._assert_via_layout()
        self._assert_toolbar_children_fit(self.app.gnd_via_workbench.preview_toolbar, "GND VIA toolbar")

        self._show_page("反焊盘与叠层")
        workbench = self.app.antipad_workbench
        workbench.lower_panes.sashpos(
            0, max(360, min(690, int(workbench.lower_panes.winfo_width() * 0.62))))
        self._settle()
        self._assert_antipad_layout()
        cross = workbench.cross_section.canvas
        region = tuple(float(value) for value in str(cross.cget("scrollregion")).split())
        self.assertGreater(region[2], cross.winfo_width())
        self.assertTrue(workbench.cross_section.vertical_scroll.winfo_ismapped())
        self.assertTrue(workbench.cross_section.horizontal_scroll.winfo_ismapped())
        self._assert_toolbar_children_fit(workbench.table_toolbar, "stackup toolbar")

        workbench.cross_section.canvas.yview_moveto(0.0)
        first_top, first_bottom, _index = workbench.cross_section.hit_regions[0]
        workbench.cross_section._clicked(SimpleNamespace(y=(first_top + first_bottom) / 2))
        self._settle()
        self.assertEqual(workbench.tree.selection(), ("0",))
        self.assertEqual(len(self._text_items(cross, "selected-layer-label")), 1)

        workbench.select_layer(28)
        self._settle()
        self.assertEqual(workbench.tree.selection(), ("28",))
        self.assertEqual(workbench.preview.layer_var.get(), self.app.config["stackup"][28]["name"])
        footer = " ".join(cross.itemcget(item, "text") for item in self._text_items(cross, "selected-layer-footer"))
        self.assertIn(self.app.config["stackup"][28]["name"], footer)

        self._show_page("电容与模型")
        cap_panes = self.app.capacitor_panes
        cap_panes.sashpos(0, max(360, min(700, int(cap_panes.winfo_width() * 0.64))))
        self._settle()
        self._assert_capacitor_layout()
        self._assert_toolbar_children_fit(self.app.cap_dimension_toolbar, "capacitor dimension toolbar")

        self._show_page("模型预览")
        preview_panes = self.app.model_preview_workbench.panes
        preview_width = max(1, preview_panes.winfo_width())
        preview_panes.sashpos(0, max(155, min(225, int(preview_width * 0.17))))
        preview_panes.sashpos(1, max(400, min(preview_width - 200, int(preview_width * 0.73))))
        self._settle()
        self.app.model_preview_workbench.fit_model()
        self._assert_model_layout()
        self._assert_toolbar_children_fit(self.app.model_preview_workbench.toolbar, "model toolbar")
        self._assert_toolbar_children_fit(self.app.model_preview_workbench.quick_bar, "model quick toolbar")


if __name__ == "__main__":
    unittest.main(verbosity=2)
