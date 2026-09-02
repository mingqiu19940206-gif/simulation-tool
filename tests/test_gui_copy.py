"""GUI copy and layout-only regressions (no HFSS / geometry semantics)."""
from __future__ import annotations

import sys
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import ttk

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from build_info import about_message, product_title, ready_status_line
from capacitor_editor import ScrollableFrame
from gui_layout import bind_wraplength
from scenario_semantics import VIA_TRANSITION_SWEEP_NOT_IMPLEMENTED


class BuildIdentityCopyTests(unittest.TestCase):
    def test_window_title_is_product_and_version_only(self) -> None:
        title = product_title()
        self.assertTrue(title.startswith("PCIe AC耦合电容区域无源优化工具 V"))
        self.assertIn("2.4.0", title)
        self.assertNotIn("native-optimetrics", title)
        self.assertNotIn("source_hash", title)
        self.assertNotIn("·", title)

    def test_status_line_tracks_display_unit(self) -> None:
        self.assertEqual(ready_status_line("mil"), "就绪 · V2.4.0 · 显示单位 mil")
        self.assertEqual(ready_status_line("mm"), "就绪 · V2.4.0 · 显示单位 mm")

    def test_about_keeps_hash_backend_and_schema(self) -> None:
        body = about_message()
        self.assertIn("源码哈希", body)
        self.assertIn("扫描后端", body)
        self.assertIn("配置 schema", body)
        self.assertIn("native-optimetrics-v3.2", body)


class WraplengthHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"Tk GUI unavailable: {exc}")

    def tearDown(self) -> None:
        if hasattr(self, "root"):
            self.root.destroy()

    def test_bind_wraplength_follows_pane_width(self) -> None:
        self.root.geometry("320x160")
        self.root.deiconify()
        frame = tk.Frame(self.root, width=280, height=80)
        frame.pack_propagate(False)
        frame.pack(fill="x")
        label = ttk.Label(
            frame,
            text="层名来自“反焊盘与叠层”。输入/输出层须为信号层，机械钻孔须为导体层。",
            wraplength=1040,
            justify="left",
        )
        label.pack(fill="x")
        self.root.update()
        self.root.update_idletasks()
        bind_wraplength(label, source=frame, padding=24)
        self.root.update()
        self.root.update_idletasks()
        wrap = int(float(label.cget("wraplength") or 0))
        width = int(frame.winfo_width())
        self.assertGreaterEqual(width, 200)
        self.assertLess(wrap, 1040)
        self.assertLessEqual(abs(wrap - (width - 24)), 8)

    def test_bind_wraplength_ignores_collapsed_label_width(self) -> None:
        self.root.geometry("400x200")
        self.root.deiconify()
        frame = tk.Frame(self.root, width=360, height=80)
        frame.pack_propagate(False)
        frame.pack(fill="x")
        label = ttk.Label(
            frame,
            text="PROVISIONAL warning that must not wrap to twelve characters",
            wraplength=80,
            justify="left",
        )
        label.pack(anchor="w")
        self.root.update()
        self.root.update_idletasks()
        bind_wraplength(label, source=frame, padding=24, min_width=360)
        self.root.update()
        self.root.update_idletasks()
        wrap = int(float(label.cget("wraplength") or 0))
        self.assertGreaterEqual(wrap, 200)
        self.assertGreaterEqual(wrap, int(frame.winfo_width()) - 32)

    def test_nested_mousewheel_reaches_inner_scroller(self) -> None:
        self.root.geometry("320x240")
        self.root.deiconify()
        outer = ScrollableFrame(self.root, fill_viewport=True)
        outer.pack(fill="both", expand=True)
        inner = ScrollableFrame(outer.content)
        inner.pack(fill="x")
        inner.canvas.configure(height=72)
        for index in range(24):
            ttk.Label(inner.content, text=f"field {index}").pack(anchor="w")
        self.root.update()
        self.root.update_idletasks()
        inner.canvas.yview_moveto(0)
        outer.canvas.yview_moveto(0)
        child = inner.content.winfo_children()[0]
        child.event_generate("<Button-5>", x=4, y=4)
        self.root.update()
        if inner.canvas.yview()[0] == 0.0:
            child.event_generate("<MouseWheel>", delta=-120, x=4, y=4)
            self.root.update()
        inner_pos = inner.canvas.yview()
        outer_pos = outer.canvas.yview()
        self.assertGreater(inner_pos[0], 0.0)
        self.assertEqual(outer_pos[0], 0.0)


class MainWindowCopyTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from main_gui import SurfaceCapAntipadApp
            self.app = SurfaceCapAntipadApp()
            self.app.withdraw()
            self.app.update_idletasks()
        except tk.TclError as exc:
            self.skipTest(f"Tk GUI unavailable: {exc}")

    def tearDown(self) -> None:
        app = getattr(self, "app", None)
        if app is not None:
            try:
                app.destroy()
            except tk.TclError:
                pass

    def test_chrome_title_status_and_tabs(self) -> None:
        title = str(self.app.title())
        self.assertEqual(title, product_title())
        self.assertNotIn("native-optimetrics", title)
        status = self.app.status.get()
        self.assertTrue(status.startswith("就绪 · V2.4.0 · 显示单位 "))
        self.assertNotIn("source_hash", status)
        self.assertNotIn("sweep_backend", status)
        self.assertIn("检查结果", self.app.tabs)
        self.assertNotIn("Validate结果", self.app.tabs)
        self.assertEqual(self.app.notebook.tab(self.app.tabs["VIA 换层"], "state"), "disabled")

    def test_ac_cap_banner_explains_disabled_via_tab(self) -> None:
        banner = str(self.app.scenario_hints["项目与走线"].cget("text"))
        self.assertIn("VIA 换层仅在仿真场景选“差分 VIA 换层”后可用。", banner)
        self.assertNotIn("trace.*", banner)

    def test_labels_drop_json_keys_and_use_chinese_pad_copy(self) -> None:
        width = str(self.app.field_labels["trace.trace_width"].cget("text"))
        self.assertEqual(width, "走线宽度")
        self.assertNotIn("trace_width", width)
        self.assertEqual(str(self.app.field_labels["trace.trace_pitch"].cget("text")), "差分中心距")
        self.assertEqual(str(self.app.field_labels["trace.trace_gap"].cget("text")), "铜皮边到边间距")
        self.assertEqual(str(self.app.field_labels["trace.pad_escape_mode"].cget("text")), "焊盘出线模式")
        self.assertEqual(str(self.app.field_labels["project.overwrite_existing"].cget("text")), "覆盖 V2 同名输出")
        self.assertEqual(str(self.app.field_labels["project.open_aedt_after_build"].cget("text")), "保持 AEDT 打开")
        self.assertEqual(
            str(self.app.field_labels["gnd_vias.clearance_x"].cget("text")),
            "X 向焊盘边偏移（有符号）",
        )
        self.assertEqual(
            str(self.app.field_labels["gnd_vias.clearance_y"].cget("text")),
            "Y 向焊盘边偏移（有符号）",
        )
        self.assertEqual(str(self.app.field_labels["gnd_vias.barrel_model"].cget("text")), "筒壁模型")

    def test_via_hints_use_stackup_tab_name_and_single_fanout_sentence(self) -> None:
        layer = str(self.app.via_layer_hint.cget("text"))
        fanout = str(self.app.via_fanout_hint.cget("text"))
        stub = str(self.app.backdrill_hint_label.cget("text"))
        self.assertIn("反焊盘与叠层", layer)
        self.assertNotIn("下方", layer)
        self.assertIn("输入/输出", layer)
        self.assertNotIn("entry/exit", layer)
        self.assertEqual(fanout, "孔间距等于差分对中心距时，扇出三项自动停用。")
        self.assertIn("与输入/输出层无关", stub)
        self.assertNotIn("entry/exit", stub)

    def test_toolbar_validate_is_chinese(self) -> None:
        labels = {button.cget("text") for button, _full, _compact in self.app.main_toolbar_buttons}
        self.assertTrue({"模型检查"} & labels or any("模型检查" in text for text in labels))
        self.assertNotIn("Validate", labels)

    def test_via_scenario_banner_translates_fail_closed(self) -> None:
        self.app.variables["scenario.type"].set("via_transition")
        self.app.update_idletasks()
        banner = str(self.app.scenario_hints["仿真与扫描"].cget("text"))
        self.assertIn("未实现的轴按失败关闭处理。", banner)
        self.assertNotIn("fail closed", banner)

    def test_sweep_step_title_is_unified(self) -> None:
        workbench = self.app.simulation_sweep_workbench
        titles = [
            workbench.steps.tab(tab_id, "text")
            for tab_id in workbench.steps.tabs()
        ]
        self.assertEqual(titles[2], "3  创建参数扫描、运行与汇总")
        self.assertEqual(titles.count(titles[2]), 1)

    def test_capacitor_left_column_forwards_mousewheel(self) -> None:
        scroller = self.app.cap_parameter_scroller
        self.assertTrue(getattr(scroller, "_forwards_mousewheel", False))
        self.assertEqual(
            str(self.app.field_labels["capacitor.create_body_in_hfss"].cget("text")),
            "在 HFSS 中创建电容本体",
        )

    @staticmethod
    def _ancestors(widget: tk.Misc) -> list[tk.Misc]:
        chain: list[tk.Misc] = []
        owner: tk.Misc | None = widget
        while owner is not None:
            chain.append(owner)
            owner = getattr(owner, "master", None)
        return chain

    def test_create_body_checkbox_is_inside_inner_scroller(self) -> None:
        self._map_720_window("电容与模型")
        scroller = self.app.cap_parameter_scroller
        checkbox = self.app.widgets["capacitor.create_body_in_hfss"]
        label = self.app.field_labels["capacitor.create_body_in_hfss"]
        hfss_box = getattr(self.app, "cap_hfss_box", None)
        ancestors = self._ancestors(checkbox)
        self.assertIn(scroller.content, ancestors)
        self.assertIn(scroller, ancestors)
        self.assertNotIn(checkbox, self.app.cap_parameter_column.pack_slaves())
        if hfss_box is not None:
            self.assertIn(hfss_box, ancestors)
            self.assertIn(hfss_box, scroller.content.pack_slaves())
        scroller.refresh_layout()
        self.app.update()
        self.app.update_idletasks()
        scroller.canvas.yview_moveto(1.0)
        self.app.update_idletasks()
        content_top = scroller.content.winfo_rooty()
        bottom = max(
            label.winfo_rooty() + label.winfo_height() - content_top,
            checkbox.winfo_rooty() + checkbox.winfo_height() - content_top,
        )
        if hfss_box is not None:
            bottom = max(bottom, hfss_box.winfo_rooty() + hfss_box.winfo_height() - content_top)
        region = [float(value) for value in str(scroller.canvas.cget("scrollregion")).split()]
        self.assertEqual(len(region), 4)
        self.assertGreaterEqual(region[3], bottom - 1)
        view_bottom = scroller.canvas.canvasy(scroller.canvas.winfo_height())
        self.assertLessEqual(bottom, view_bottom + 2)

    def _map_720_window(self, tab: str) -> None:
        self.app.geometry("1280x720+0+0")
        self.app.deiconify()
        self.app.notebook.select(self.app.tabs[tab])
        self.app.update()
        self.app.update_idletasks()

    def test_status_bar_is_visible_under_toolbar_at_720(self) -> None:
        self._map_720_window("电容与模型")
        status = self.app.status_bar
        notebook = self.app.notebook
        toolbar = self.app.main_toolbar
        self.assertTrue(status.winfo_ismapped())
        self.assertGreater(status.winfo_y(), toolbar.winfo_y())
        self.assertLess(status.winfo_y(), notebook.winfo_y())
        self.assertLess(status.winfo_y(), 120)
        self.assertIn("就绪 · V2.4.0 · 显示单位", self.app.status.get())
        self.assertLess(status.winfo_y() + status.winfo_height(), 200)

    def test_capacitor_warning_wraplength_follows_status_box(self) -> None:
        self._map_720_window("电容与模型")
        label = self.app.cap_detail_label
        box = self.app.cap_status_box
        wrap = int(float(label.cget("wraplength") or 0))
        width = int(box.winfo_width())
        self.assertGreaterEqual(width, 240)
        self.assertGreaterEqual(wrap, 200)
        self.assertLess(wrap, width)
        self.assertGreater(wrap, 120)

    def test_fanout_formula_matches_one_line_hint(self) -> None:
        expected = "孔间距等于差分对中心距时，扇出三项自动停用。"
        self.assertEqual(str(self.app.via_fanout_hint.cget("text")), expected)
        self.assertEqual(
            str(self.app.formula_labels["via_transition.fanout.style"].cget("text")),
            expected,
        )

    def test_user_visible_copy_drops_leftover_english_and_json_keys(self) -> None:
        self.assertNotIn("scenario.type", VIA_TRANSITION_SWEEP_NOT_IMPLEMENTED)
        self.assertIn("请先把仿真场景切换为差分 VIA 换层", VIA_TRANSITION_SWEEP_NOT_IMPLEMENTED)
        main_gui_text = (ROOT / "src" / "main_gui.py").read_text(encoding="utf-8")
        self.assertNotIn("确认Validate警告", main_gui_text)
        self.assertNotIn("快速Validate存在错误", main_gui_text)
        preview = (ROOT / "src" / "model_preview.py").read_text(encoding="utf-8")
        self.assertNotIn("未运行 Validate", preview)
        self.assertNotIn("Validate检查结果", preview)
        capacitor = (ROOT / "src" / "capacitor_editor.py").read_text(encoding="utf-8")
        self.assertNotIn("PAD间隙", capacitor)
        self.assertIn("焊盘间隙无铜桥", capacitor)

    def test_gnd_performance_hint_keeps_sparam_tokens(self) -> None:
        hint = self.app.gnd_via_workbench.performance_hint
        text = str(hint.cget("text"))
        self.assertIn("Sdd11/Sdd21", text)
        self.assertIn("Sdc21/Scd21", text)
        self.assertIn("；", text)
        self.assertIn("\n", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
