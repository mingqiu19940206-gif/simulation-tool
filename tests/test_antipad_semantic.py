from __future__ import annotations

import copy
import math
import sys
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from antipad_editor import analyze_antipad
from antipad_semantic import analyze_all_modes, connected_window_count, tools_for_mode
from config_manager import ConfigManager, convert_length
from geometry_calculator import GeometryCalculator
from main_gui import SurfaceCapAntipadApp


class AntipadSemanticLogicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.default = ConfigManager(ROOT).default()

    def statuses(self, config=None):
        config = copy.deepcopy(config or self.default)
        geometry = GeometryCalculator(config).calculate()
        return config, geometry, analyze_all_modes(config, geometry, "L2")

    def test_default_critical_value_uses_first_real_contact(self) -> None:
        _config, _geometry, statuses = self.statuses()
        independent = statuses["independent_pad"]
        per_capacitor = statuses["merged_per_capacitor"]
        self.assertEqual(independent.connected_count, 4)
        self.assertEqual(per_capacitor.connected_count, 2)
        self.assertAlmostEqual(independent.critical_expansion, 0.05, places=8)
        self.assertAlmostEqual(per_capacitor.critical_expansion, 0.05, places=8)
        self.assertLess(independent.recommended_expansion, independent.critical_expansion)
        self.assertFalse(independent.mismatch)

    def test_small_expansion_preserves_4_2_1_semantics(self) -> None:
        config = copy.deepcopy(self.default)
        config["antipad"]["global_expansion"] = 0.01
        _config, _geometry, statuses = self.statuses(config)
        self.assertEqual(
            {mode: status.connected_count for mode, status in statuses.items()},
            {"independent_pad": 4, "merged_per_capacitor": 2, "merged_all": 1},
        )
        self.assertFalse(statuses["independent_pad"].mismatch)
        self.assertGreater(statuses["independent_pad"].minimum_gap, 0.0)

    def test_critical_value_brackets_rotated_real_geometry(self) -> None:
        config = copy.deepcopy(self.default)
        config["capacitor"]["pad_rotation"] = 17.0
        geometry = GeometryCalculator(config).calculate()
        status = analyze_all_modes(config, geometry, "L2")["independent_pad"]
        self.assertIsNotNone(status.critical_expansion)
        critical = float(status.critical_expansion)
        below = tools_for_mode(config, geometry, "L2", "independent_pad", critical - 1e-5)
        above = tools_for_mode(config, geometry, "L2", "independent_pad", critical + 1e-5)
        self.assertEqual(connected_window_count(below), 4)
        self.assertLess(connected_window_count(above), 4)

    def test_layer_override_drives_selected_layer_status(self) -> None:
        config = copy.deepcopy(self.default)
        next(layer for layer in config["stackup"] if layer["name"] == "L2")["antipad_expansion_override"] = 0.01
        _config, _geometry, statuses = self.statuses(config)
        self.assertAlmostEqual(statuses["independent_pad"].expansion, 0.01)
        self.assertEqual(statuses["independent_pad"].connected_count, 4)

    def test_analysis_reports_semantics_and_scan_transition(self) -> None:
        config = copy.deepcopy(self.default)
        config["antipad"]["global_expansion"] = 0.2
        config["sweeps"]["antipad.global_expansion"].update(
            {"enabled": True, "start": 0.01, "stop": 0.1, "step": 0.01}
        )
        geometry = GeometryCalculator(config).calculate()
        analysis = analyze_antipad(config, geometry, "L2", "mm")
        self.assertEqual(analysis.values["设计工具体"], "4")
        self.assertEqual(analysis.values["实际连通开窗"], "1")
        self.assertIn("0.05", analysis.values["拓扑临界净距"])
        self.assertTrue(any("模式语义不匹配" in item for item in analysis.warnings))
        self.assertTrue(any("扫描范围跨越" in item for item in analysis.warnings))
        self.assertTrue(any("保持 4 个实际开窗" in item for item in analysis.infos))

    def test_plane_edge_and_tool_gap_are_numeric(self) -> None:
        config = copy.deepcopy(self.default)
        config["antipad"]["global_expansion"] = 0.01
        _config, _geometry, statuses = self.statuses(config)
        status = statuses["independent_pad"]
        self.assertTrue(math.isfinite(status.minimum_gap))
        self.assertTrue(math.isfinite(status.plane_edge_margin))
        self.assertGreater(status.plane_edge_margin, 0.0)


class AntipadSemanticGuiTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.app = SurfaceCapAntipadApp()
            self.app.geometry("1280x780")
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

    def test_dynamic_hint_cards_and_safe_action(self) -> None:
        config = copy.deepcopy(self.app.config)
        config["antipad"]["global_expansion"] = 0.2
        self.app._load_to_widgets(config)
        self.app.refresh_visuals()
        workbench = self.app.antipad_workbench
        self.assertIn("设计 4 个，实际 1 个", workbench.semantic_hint_var.get())
        independent_detail = workbench._mode_detail_labels["independent_pad"].cget("text")
        self.assertEqual(independent_detail, "设计 4 · 当前 1")
        self.assertEqual(
            workbench.mode_selector.cards["independent_pad"].cget("highlightbackground"),
            "#2563eb",
        )
        self.assertIn("⚠", workbench.mode_selector.status_labels["independent_pad"].cget("text"))
        self.assertEqual(str(workbench.safe_button.cget("state")), "normal")
        workbench._apply_recommended_expansion()
        self.app.refresh_visuals()
        value = float(self.app.variables["antipad.global_expansion"].get())
        self.assertLess(convert_length(value, self.app.display_unit, "mm"), 0.05)
        self.assertIn("设计 4 个，实际 4 个", workbench.semantic_hint_var.get())


if __name__ == "__main__":
    unittest.main(verbosity=2)
