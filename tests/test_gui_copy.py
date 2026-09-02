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
from gui_layout import bind_wraplength


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
        bind_wraplength(label, source=frame, padding=24)
        self.root.update_idletasks()
        frame.event_generate("<Configure>")
        self.root.update_idletasks()
        wrap = int(float(label.cget("wraplength") or 0))
        width = int(frame.winfo_width())
        self.assertGreaterEqual(width, 200)
        self.assertLess(wrap, 1040)
        self.assertLessEqual(abs(wrap - (width - 24)), 8)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
