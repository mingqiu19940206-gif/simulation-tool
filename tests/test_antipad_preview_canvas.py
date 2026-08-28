from __future__ import annotations

import copy
import sys
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from antipad_editor import AntipadPreview
from config_manager import ConfigManager
from geometry_calculator import GeometryCalculator


class AntipadPreviewCanvasTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
            self.root.withdraw()
            self.preview = AntipadPreview(self.root, lambda _name: None)
            self.preview.pack(fill="both", expand=True)
            self.preview.canvas.configure(width=920, height=520)
            self.root.update_idletasks()
        except tk.TclError as exc:
            self.skipTest(f"Tk GUI unavailable: {exc}")
        self.default = ConfigManager(ROOT).default()

    def tearDown(self) -> None:
        if hasattr(self, "root"):
            self.root.destroy()

    def _render(self, mode: str, expansion: float | None = None):
        config = copy.deepcopy(self.default)
        config["antipad"]["mode"] = mode
        if expansion is not None:
            config["antipad"]["global_expansion"] = expansion
        geometry = GeometryCalculator(config).calculate()
        self.preview.set_data(config, geometry, "mm", "L2")
        self.root.update_idletasks()
        self.preview.redraw()
        return self.preview.canvas

    def test_default_independent_draws_union_outline_without_internal_cross(self) -> None:
        # Preserve coverage for the legacy overlapping-opening presentation;
        # the product default is now the separate 1 mil case.
        canvas = self._render("independent_pad", expansion=0.2)
        orange_boundary = [
            item for item in canvas.find_all()
            if canvas.type(item) == "line" and canvas.itemcget(item, "fill") == "#f97316"
        ]
        self.assertEqual(len(orange_boundary), 4)
        texts = [canvas.itemcget(item, "text") for item in canvas.find_all() if canvas.type(item) == "text"]
        self.assertIn("工具体 4 · 实际连通开窗 1（存在接触/重叠）", texts)
        expansion_lines = [
            item for item in canvas.find_all()
            if canvas.type(item) == "line" and canvas.itemcget(item, "fill") == "#ea580c"
        ]
        self.assertEqual(len(expansion_lines), 1)
        x1, _y1, x2, _y2 = canvas.coords(expansion_lines[0])
        self.assertGreater(x2, x1)

    def test_all_mode_headers_follow_actual_rendered_geometry(self) -> None:
        for mode, tool_count, connected_count in (
            ("independent_pad", 4, 4),
            ("merged_per_capacitor", 2, 2),
            ("merged_all", 1, 1),
        ):
            canvas = self._render(mode)
            texts = [canvas.itemcget(item, "text") for item in canvas.find_all() if canvas.type(item) == "text"]
            expected = f"工具体 {tool_count} · 实际连通开窗 {connected_count}"
            self.assertTrue(any(text.startswith(expected) for text in texts), texts)


    def test_independent_openings_are_concentric_with_pads(self) -> None:
        config = copy.deepcopy(self.default)
        config["antipad"]["mode"] = "independent_pad"
        config["antipad"]["global_expansion"] = 0.0254
        geometry = GeometryCalculator(config).calculate()
        self.preview.set_data(config, geometry, "mil", "L2")
        self.root.update_idletasks()
        self.preview.redraw()
        canvas = self.preview.canvas

        pad_boxes = [
            canvas.bbox(item) for item in canvas.find_all()
            if canvas.type(item) == "polygon"
            and canvas.itemcget(item, "fill") in {"#b91c1c", "#1d4ed8"}
        ]
        opening_lines = [
            canvas.coords(item) for item in canvas.find_all()
            if canvas.type(item) == "line"
            and canvas.itemcget(item, "fill") == "#f97316"
        ]
        self.assertEqual(len(pad_boxes), 4)
        self.assertEqual(len(opening_lines), 16)

        for pad_box in pad_boxes:
            self.assertIsNotNone(pad_box)
            left, top, right, bottom = map(float, pad_box)
            center_x, center_y = (left + right) / 2.0, (top + bottom) / 2.0
            vertical_x = sorted(
                (x1 + x2) / 2.0 for x1, y1, x2, y2 in opening_lines
                if abs(x1 - x2) < 1e-6
                and min(y1, y2) - 1e-6 <= center_y <= max(y1, y2) + 1e-6
            )
            horizontal_y = sorted(
                (y1 + y2) / 2.0 for x1, y1, x2, y2 in opening_lines
                if abs(y1 - y2) < 1e-6
                and min(x1, x2) - 1e-6 <= center_x <= max(x1, x2) + 1e-6
            )
            self.assertTrue(any(value < center_x for value in vertical_x))
            self.assertTrue(any(value > center_x for value in vertical_x))
            self.assertTrue(any(value < center_y for value in horizontal_y))
            self.assertTrue(any(value > center_y for value in horizontal_y))
            nearest_vertical = (
                max(value for value in vertical_x if value < center_x),
                min(value for value in vertical_x if value > center_x),
            )
            nearest_horizontal = (
                max(value for value in horizontal_y if value < center_y),
                min(value for value in horizontal_y if value > center_y),
            )
            self.assertAlmostEqual(
                sum(nearest_vertical) / 2.0, center_x, delta=1.1
            )
            self.assertAlmostEqual(
                sum(nearest_horizontal) / 2.0, center_y, delta=1.1
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
