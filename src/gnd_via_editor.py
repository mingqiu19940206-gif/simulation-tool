"""GND VIA 的独立 Tkinter/ttk 工作台。"""
from __future__ import annotations

import math
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from config_manager import convert_length, format_length, format_number
from gui_layout import (
    CanvasLabelPlacer, WrappingButtonBar, attach_mousewheel, canvas_viewport,
    debounce_after, ellipsize, font_linespace, mousewheel_steps, resolve_font,
    responsive_grid_columns,
)
from gnd_via_semantics import (
    ALL_LAYOUT_FIELDS,
    BARREL_LABELS,
    MODE_DESCRIPTIONS,
    MODE_LABELS,
    SIDE_LABELS,
    ViaDerived,
    ViaIssue,
    active_layout_fields,
    applicable_reference_layers,
    derive_via_values,
    validate_via_workbench,
)


COLORS = {
    "P": "#dc2626", "N": "#2563eb", "pad": "#d6a11d", "hole": "#f8fafc",
    "antipad": "#f59e0b", "ground": "#d1fae5", "window": "#ffffff",
    "error": "#ef4444", "dimension": "#0f766e", "center": "#7c3aed",
    "connected": "#22c55e", "clearance": "#f59e0b", "outside": "#cbd5e1",
    "traversed": "#94a3b8",
}


class ScrollColumn(ttk.Frame):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.canvas = tk.Canvas(self, highlightthickness=0, background="#f7f8fa")
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.content = ttk.Frame(self.canvas, padding=(8, 10))
        self.window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.canvas.configure(yscrollcommand=bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.content.bind("<Configure>", self._configured)
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(self.window, width=event.width))
        attach_mousewheel(self, self.canvas, self.content)

    def _configured(self, _event: tk.Event) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _wheel(self, event: tk.Event) -> str:
        steps = mousewheel_steps(event)
        if steps:
            self.canvas.yview_scroll(steps, "units")
        return "break"


class ViaModeCards(ttk.Frame):
    """Font-aware compact selector whose rows follow its own width."""

    MODES = ("four_corner", "side_pair", "rectangular_fence", "custom")
    CARD_TITLES = {
        "four_corner": "四角布置",
        "side_pair": "两侧对称",
        "rectangular_fence": "四边围栏",
        "custom": "自定义坐标",
    }
    SHORT_RULES = {
        "four_corner": "固定 4 颗",
        "side_pair": "偶数颗",
        "rectangular_fence": "4 的倍数",
        "custom": "坐标表派生",
    }

    def __init__(self, master: tk.Misc, variable: tk.StringVar, command: Any) -> None:
        super().__init__(master)
        self.variable = variable
        self.command = command
        self.cards: Dict[str, tk.Frame] = {}
        self.card_widgets: Dict[str, Tuple[tk.Widget, ...]] = {}
        self.enabled = True
        self.current_columns = 1
        self._layout_job: Optional[str] = None
        for index, mode in enumerate(self.MODES):
            card = tk.Frame(
                self, background="white", highlightthickness=2,
                highlightbackground="#cbd5e1", cursor="hand2", padx=6, pady=5,
            )
            card.grid(row=index, column=0, sticky="nsew", padx=3, pady=2)
            content = tk.Frame(card, background="white")
            content.pack(fill="both", expand=True)
            sketch = tk.Canvas(
                content, width=44, height=32, background="white", highlightthickness=0,
            )
            sketch.pack(side="left", padx=(0, 8))
            self._draw_icon(sketch, mode)
            text = tk.Frame(content, background="white")
            text.pack(side="left", fill="both", expand=True)
            title = tk.Label(
                text, text=self.CARD_TITLES[mode], background="white", foreground="#172033",
                font="ViaCardTitleFont", anchor="w",
            )
            title.pack(fill="x")
            rule = tk.Label(
                text, text=self.SHORT_RULES[mode], background="white", foreground="#64748b",
                font="ViaCardBodyFont", anchor="w",
            )
            rule.pack(fill="x")
            widgets: Tuple[tk.Widget, ...] = (card, content, sketch, text, title, rule)
            for widget in widgets:
                widget.bind("<Button-1>", lambda _e, value=mode: self.select(value))
            self.cards[mode] = card
            self.card_widgets[mode] = widgets

        self.description_var = tk.StringVar()
        self.description_label = tk.Label(
            self, textvariable=self.description_var, justify="left", anchor="w",
            background="#f8fafc", foreground="#475569", font="ViaCardBodyFont",
            padx=8, pady=5,
        )
        self.description_label.grid(
            row=len(self.MODES), column=0, sticky="ew", padx=3, pady=(3, 1),
        )
        self.bind("<Configure>", self._configured, add="+")
        variable.trace_add("write", lambda *_: self.refresh())
        self.after_idle(self._apply_layout)
        self.refresh()

    @staticmethod
    def _draw_icon(canvas: tk.Canvas, mode: str) -> None:
        canvas.create_rectangle(15, 8, 29, 24, fill="#e2e8f0", outline="#64748b")
        points: Sequence[Tuple[int, int]]
        if mode == "four_corner":
            points = ((7, 5), (37, 5), (7, 27), (37, 27))
        elif mode == "side_pair":
            points = ((9, 5), (22, 5), (35, 5), (9, 27), (22, 27), (35, 27))
        elif mode == "rectangular_fence":
            points = ((7, 5), (22, 5), (37, 5), (7, 16), (37, 16),
                      (7, 27), (22, 27), (37, 27))
        else:
            points = ((6, 9), (36, 5), (34, 25), (11, 27), (39, 16))
        for x, y in points:
            canvas.create_oval(
                x - 3, y - 3, x + 3, y + 3, fill="#d6a11d", outline="#713f12",
            )

    def _configured(self, _event: tk.Event) -> None:
        debounce_after(self, "_layout_job", self._apply_layout)

    def _minimum_card_width(self) -> int:
        title_font = resolve_font(self, "ViaCardTitleFont")
        body_font = resolve_font(self, "ViaCardBodyFont")
        text_width = max(
            max(title_font.measure(self.CARD_TITLES[mode]) for mode in self.MODES),
            max(body_font.measure(self.SHORT_RULES[mode]) for mode in self.MODES),
        )
        return 44 + 8 + text_width + 16

    def _apply_layout(self) -> None:
        width = max(1, int(self.winfo_width()))
        columns = responsive_grid_columns(
            width, len(self.MODES), self._minimum_card_width(),
            choices=(4, 2, 1), gap=6,
        )
        if columns != self.current_columns:
            self.current_columns = columns
            for column in range(len(self.MODES)):
                self.columnconfigure(
                    column, weight=1 if column < columns else 0,
                    uniform="via-mode" if column < columns else "",
                )
            for index, mode in enumerate(self.MODES):
                self.cards[mode].grid_configure(row=index // columns, column=index % columns)
        rows = int(math.ceil(len(self.MODES) / columns))
        self.description_label.grid_configure(row=rows, column=0, columnspan=columns)
        self.description_label.configure(wraplength=max(1, width - 22))

    def select(self, mode: str) -> None:
        if self.enabled:
            self.variable.set(mode)
            self.command()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self.refresh()

    def refresh(self) -> None:
        selected = self.variable.get()
        for mode, card in self.cards.items():
            selected_card = mode == selected
            background = (
                "#eff6ff" if selected_card and self.enabled
                else "white" if self.enabled else "#f1f5f9"
            )
            card.configure(
                highlightbackground="#2563eb" if selected_card else "#cbd5e1",
                highlightcolor="#2563eb" if selected_card else "#cbd5e1",
                background=background,
                cursor="hand2" if self.enabled else "arrow",
            )
            for widget in self.card_widgets[mode][1:]:
                if isinstance(widget, (tk.Frame, tk.Label, tk.Canvas)):
                    widget.configure(background=background)
                widget.configure(cursor="hand2" if self.enabled else "arrow")
        rules = MODE_DESCRIPTIONS.get(selected)
        self.description_var.set(
            f"{MODE_LABELS[selected]}：{rules[0]}；{rules[1]}；{rules[2]}。"
            if rules and selected in MODE_LABELS else ""
        )
        self._apply_layout()


class ViaTopPreview(tk.Canvas):
    def __init__(self, master: tk.Misc, on_place: Any) -> None:
        super().__init__(master, background="white", height=360, highlightthickness=1,
                         highlightbackground="#cbd5e1")
        self.zoom = 1.0
        self.options = {"dimensions": True, "signal": True, "antipad": True, "top_ground": True}
        self._last: Optional[Tuple[Mapping[str, Any], Any, str, Sequence[ViaIssue]]] = None
        self._transform: Optional[Tuple[float, float, float]] = None
        self.on_place = on_place
        self.bind("<Configure>", lambda _e: self.winfo_toplevel().after_idle(self.redraw))
        self.bind("<Button-1>", self._clicked)

    def show(self, config: Mapping[str, Any], geometry: Any, unit: str,
             issues: Sequence[ViaIssue]) -> None:
        self._last = (config, geometry, unit, issues)
        self.redraw()

    def show_error(self, message: str) -> None:
        self._last = None
        self.delete("all")
        viewport = canvas_viewport(self)
        if viewport is None:
            return
        width, height = viewport
        self.create_text(width / 2, height / 2,
                         text="GND VIA 预览暂不可用\n" + message, fill="#b42318",
                         justify="center", width=max(1, width - 24), tags=("hud-label",))

    def fit(self) -> None:
        self.zoom = 1.0
        self.redraw()

    def zoom_by(self, factor: float) -> None:
        self.zoom = max(0.45, min(4.0, self.zoom * factor))
        self.redraw()

    def toggle(self, name: str, value: bool) -> None:
        self.options[name] = value
        self.redraw()

    def _clicked(self, event: tk.Event) -> None:
        if not self._last or not self._transform:
            return
        config = self._last[0]
        if not config["gnd_vias"].get("enabled") or config["gnd_vias"].get("pattern") != "custom":
            return
        scale, ox, oy = self._transform
        self.on_place((event.x - ox) / scale, (oy - event.y) / scale)

    def redraw(self) -> None:
        if not self._last:
            return
        config, geometry, unit, issues = self._last
        viewport = canvas_viewport(self)
        if viewport is None:
            return
        self.delete("all")
        width, height = viewport
        pad = 12
        title_line = font_linespace(self, "ViaPreviewTitleFont")
        body_line = font_linespace(self, "ViaCanvasFont")
        small_line = font_linespace(self, "ViaSmallFont")
        legend_title_line = font_linespace(self, "ViaCardTitleFont")
        legend = (("P 信号", COLORS["P"]), ("N 信号", COLORS["N"]), ("VIA 焊盘", COLORS["pad"]),
                  ("成品孔", COLORS["hole"]), ("VIA 避空", COLORS["antipad"]),
                  ("安装面 GND", COLORS["ground"]), ("信号开窗", "#10b981"), ("碰撞", COLORS["error"]))
        legend_font = resolve_font(self, "ViaCanvasFont")
        legend_label_width = max(legend_font.measure(label) for label, _color in legend)
        legend_column_width = 18 + 6 + legend_label_width + 12
        legend_row_height = max(body_line + 6, 16)
        right_legend = (
            width >= max(660, legend_column_width + 360)
            and height >= pad * 2 + legend_title_line + len(legend) * legend_row_height
        )
        if right_legend:
            legend_columns = 1
            legend_rows = len(legend)
            legend_left = width - legend_column_width - pad
            legend_top = pad
            plot_right = legend_left - pad
            plot_bottom = height - pad
        else:
            legend_columns = max(1, min(4, (width - pad * 2) // max(1, legend_column_width)))
            legend_rows = int(math.ceil(len(legend) / legend_columns))
            legend_height = legend_title_line + 4 + legend_rows * legend_row_height
            legend_left = pad
            legend_top = max(pad, height - pad - legend_height)
            plot_right = width - pad
            plot_bottom = legend_top - pad
        status = "当前未生成 GND VIA" if not config["gnd_vias"].get("enabled") else (
            f"{MODE_LABELS.get(config['gnd_vias']['pattern'], config['gnd_vias']['pattern'])} · 实际 {len(geometry.gnd_vias)} 颗"
        )
        active = active_layout_fields(
            config["gnd_vias"]["pattern"],
            config["gnd_vias"].get("side_orientation", "top_bottom"),
        )
        offset_labels = []
        cfg = config["gnd_vias"]
        if "clearance_x" in active:
            offset_labels.append(f"X偏移={format_length(cfg['clearance_x'], unit)} {unit}")
        if "clearance_y" in active:
            offset_labels.append(f"Y偏移={format_length(cfg['clearance_y'], unit)} {unit}")
        header_lines = 1 + bool(offset_labels)
        plot_top = pad + title_line + 6 + (body_line + 4 if offset_labels else 0)
        plot_left = pad
        plot_width = plot_right - plot_left
        plot_height = plot_bottom - plot_top
        placer = CanvasLabelPlacer(self, width, height, safe=pad)
        title_item = placer.place(
            status,
            ((pad + resolve_font(self, "ViaPreviewTitleFont").measure(status) / 2.0, pad + title_line / 2.0),),
            font="ViaPreviewTitleFont", fill="#172033", tags=("hud-label", "status-label"), priority=3,
        )
        if offset_labels:
            offset_text = "；".join(offset_labels)
            offset_width = resolve_font(self, "ViaCanvasFont").measure(offset_text)
            placer.place(
                offset_text,
                ((pad + offset_width / 2.0, pad + title_line + 4 + body_line / 2.0),),
                font="ViaCanvasFont", fill=COLORS["dimension"],
                tags=("hud-label", "dimension-label"), priority=3,
            )
        legend_title = self.create_text(legend_left, legend_top, anchor="nw", text="图例",
                                        font="ViaCardTitleFont", tags=("legend-title",))
        legend_title_box = self.bbox(legend_title)
        if legend_title_box:
            placer.reserve(legend_title_box)
        item_top = legend_top + legend_title_line + 4
        for index, (label, color) in enumerate(legend):
            column = index % legend_columns
            row = index // legend_columns
            x = legend_left + column * legend_column_width
            y = item_top + row * legend_row_height
            swatch_height = min(12, legend_row_height - 4)
            self.create_rectangle(x, y + (legend_row_height-swatch_height)/2,
                                  x + 16, y + (legend_row_height+swatch_height)/2,
                                  fill=color, outline="#64748b", tags=("legend-swatch",))
            text_item = self.create_text(x + 22, y + legend_row_height / 2,
                                         anchor="w", text=label, font="ViaCanvasFont",
                                         tags=("legend-label",))
            placer.reserve(self.bbox(text_item))

        if plot_width < 80 or plot_height < 70:
            self.create_text(width / 2, (plot_top + plot_bottom) / 2,
                             text="视口过小，请拖动窗格扩大预览区域",
                             fill="#9a3412", width=max(1, width - 2 * pad),
                             tags=("hud-label",))
            return
        shapes = list(geometry.all_signal_shapes())
        points = [point for shape in shapes for point in shape.points]
        vias = list(geometry.gnd_vias)
        pad_r = float(config["gnd_vias"]["pad_diameter"]) / 2.0
        for via in vias:
            points.extend((type("P", (), {"x": via.x - pad_r, "y": via.y - pad_r})(),
                           type("P", (), {"x": via.x + pad_r, "y": via.y + pad_r})()))
        if not points:
            self.create_text(width / 2, height / 2, text="没有可显示的几何")
            return
        x_min, x_max = min(p.x for p in points), max(p.x for p in points)
        y_min, y_max = min(p.y for p in points), max(p.y for p in points)
        margin = max((x_max - x_min) * .13, (y_max - y_min) * .2, .25)
        x_min -= margin; x_max += margin; y_min -= margin; y_max += margin
        scale = min(plot_width / max(x_max - x_min, 1e-9),
                    plot_height / max(y_max - y_min, 1e-9)) * self.zoom
        ox = plot_left + (plot_width - (x_max - x_min) * scale) / 2 - x_min * scale
        oy = plot_top + plot_height / 2 + (y_min + y_max) * scale / 2
        self._transform = (scale, ox, oy)
        def xy(x: float, y: float) -> Tuple[float, float]:
            return ox + x * scale, oy - y * scale
        # 当前安装面GND局部示意及其保守AABB开窗。
        if self.options["top_ground"] and config["gnd_vias"].get("create_top_ground_copper"):
            x1, y1 = xy(x_min, y_max); x2, y2 = xy(x_max, y_min)
            self.create_rectangle(x1, y1, x2, y2, fill=COLORS["ground"], outline="#059669")
            clearance = float(config["gnd_vias"]["top_ground_clearance"])
            for shape in shapes:
                b = shape.bounds
                a, c = xy(b.x_min - clearance, b.y_max + clearance)
                d, e = xy(b.x_max + clearance, b.y_min - clearance)
                self.create_rectangle(a, c, d, e, fill=COLORS["window"], outline="#10b981", dash=(3, 2))
        if self.options["antipad"]:
            for tools in geometry.antipads_by_layer.values():
                for shape in tools:
                    self.create_polygon([coord for point in shape.points for coord in xy(point.x, point.y)],
                                        fill="", outline="#8b5cf6", dash=(5, 3))
                break
        if self.options["signal"]:
            for signal in ("P", "N"):
                for shape in geometry.signal_shapes[signal]:
                    self.create_polygon([coord for point in shape.points for coord in xy(point.x, point.y)],
                                        fill=COLORS[signal], outline="#1f2937")
        global_overlap = any("VIA 焊盘之间发生重叠" in issue.message and issue.level == "error"
                             for issue in issues)
        colliding = {via.name for via in vias if global_overlap or
                     any(via.name in issue.message and issue.level == "error" for issue in issues)}
        for index, via in enumerate(vias, 1):
            x, y = xy(via.x, via.y)
            r_clear = via.antipad_diameter * scale / 2.0
            r_pad = via.pad_diameter * scale / 2.0
            r_hole = via.finished_hole_diameter * scale / 2.0
            if self.options["antipad"]:
                self.create_oval(x-r_clear, y-r_clear, x+r_clear, y+r_clear,
                                 outline=COLORS["antipad"], width=2, dash=(4, 2))
            outline = COLORS["error"] if via.name in colliding else "#713f12"
            self.create_oval(x-r_pad, y-r_pad, x+r_pad, y+r_pad, fill=COLORS["pad"], outline=outline, width=2)
            self.create_oval(x-r_hole, y-r_hole, x+r_hole, y+r_hole, fill=COLORS["hole"], outline="#334155")
            self.create_oval(x-2, y-2, x+2, y+2, fill="#111827", outline="")
            side = -1 if via.x <= (x_min + x_max) / 2.0 else 1
            placer.place(
                f"VIA{index:02d}",
                ((x + side * (r_pad + 24), y - r_pad - small_line / 2),
                 (x + side * (r_pad + 24), y + r_pad + small_line / 2),
                 (x, y - r_pad - small_line)),
                font="ViaSmallFont", fill="#172033", tags=("via-label",), priority=1,
            )
        cx = (x_min + x_max) / 2.0
        x, y1 = xy(cx, y_min); _, y2 = xy(cx, y_max)
        self.create_line(x, y1, x, y2, fill=COLORS["center"], dash=(3, 3))
        if self.options["dimensions"] and vias:
            self._dimensions(config, vias, xy, active, unit, placer)

    def _dimensions(self, config: Mapping[str, Any], vias: Sequence[Any], xy: Any,
                    active: set[str], unit: str, placer: CanvasLabelPlacer) -> None:
        cfg = config["gnd_vias"]
        if len(vias) >= 2:
            ordered_x = sorted(vias, key=lambda via: (via.y, via.x))
            ordered_y = sorted(vias, key=lambda via: (via.x, via.y))
            if "pitch_x" in active:
                first = ordered_x[0]
                candidates = [via for via in ordered_x[1:] if abs(via.x - first.x) > 1e-9]
                if candidates:
                    second = min(candidates, key=lambda via: abs(via.x - first.x))
                    self._arrow(xy(first.x, first.y), xy(second.x, first.y),
                                f"X节距 {format_length(cfg['pitch_x'], unit)} {unit}", placer)
            if "pitch_y" in active:
                first = ordered_y[0]
                candidates = [via for via in ordered_y[1:] if abs(via.y - first.y) > 1e-9]
                if candidates:
                    second = min(candidates, key=lambda via: abs(via.y - first.y))
                    self._arrow(xy(first.x, first.y), xy(first.x, second.y),
                                f"Y节距 {format_length(cfg['pitch_y'], unit)} {unit}", placer)

    def _arrow(self, first: Tuple[float, float], second: Tuple[float, float], label: str,
               placer: CanvasLabelPlacer) -> None:
        self.create_line(*first, *second, arrow="both", fill=COLORS["dimension"], width=1,
                         tags=("dimension-line",))
        middle = ((first[0] + second[0]) / 2, (first[1] + second[1]) / 2)
        line = font_linespace(self, "ViaSmallFont")
        horizontal = abs(second[0]-first[0]) >= abs(second[1]-first[1])
        candidates = (
            (middle[0], middle[1] - line),
            (middle[0], middle[1] + line),
            (middle[0] + (line * 2 if not horizontal else 0), middle[1]),
            (middle[0] - (line * 2 if not horizontal else 0), middle[1]),
        )
        placer.place(label, candidates, font="ViaSmallFont", fill=COLORS["dimension"],
                     tags=("dimension-label",), priority=2)


class ViaCrossSection(tk.Canvas):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, background="white", height=355, highlightthickness=1,
                         highlightbackground="#cbd5e1")
        self._last: Optional[Tuple[Mapping[str, Any], ViaDerived, str]] = None
        self._font_tier = "normal"
        self._requested_height = 0
        self._tooltip: Optional[tk.Toplevel] = None
        self.bind("<Configure>", lambda _e: self.winfo_toplevel().after_idle(self.redraw))

    def _show_layer_tooltip(self, event: tk.Event, text: str) -> None:
        self._hide_layer_tooltip()
        popup = tk.Toplevel(self)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        ttk.Label(
            popup, text=text, padding=(7, 4), justify="left",
            wraplength=min(720, max(260, self.winfo_screenwidth() // 2)),
        ).pack()
        popup.geometry(f"+{int(event.x_root)+12}+{int(event.y_root)+12}")
        self._tooltip = popup

    def _hide_layer_tooltip(self, _event: Any = None) -> None:
        popup, self._tooltip = self._tooltip, None
        if popup is not None:
            try:
                popup.destroy()
            except tk.TclError:
                pass

    def set_font_tier(self, tier: str) -> None:
        self._font_tier = tier
        self.redraw()

    def show(self, config: Mapping[str, Any], derived: ViaDerived, unit: str) -> None:
        self._last = (config, derived, unit)
        self.redraw()

    def redraw(self) -> None:
        if not self._last:
            return
        self._hide_layer_tooltip()
        config, derived, unit = self._last
        viewport = canvas_viewport(self)
        if viewport is None:
            return
        self.delete("all")
        width, _visible_height = viewport
        cfg = config["gnd_vias"]
        pad = 12
        title_line = font_linespace(self, "ViaCardTitleFont")
        body_line = font_linespace(self, "ViaCanvasFont")
        mono_line = font_linespace(self, "ViaMonoFont")
        small_line = font_linespace(self, "ViaSmallFont")
        title_text = ellipsize(
            self, "VIA 孔盘制造截面 · 尺寸关系示意",
            "ViaCardTitleFont", max(1, width-pad*2),
        )
        title = self.create_text(pad, pad, anchor="nw", text=title_text,
                                 font="ViaCardTitleFont", tags=("cross-title", "hud-label"))
        figure_top = pad + title_line + body_line + 12
        wide = width >= 420
        drawing_width = min(205.0, width * (0.46 if wide else 0.82))
        cx = pad + drawing_width / 2 if wide else width / 2
        pad_r = max(30.0, min(70.0, drawing_width / 2 - 10))
        top = figure_top
        bottom = top + max(58.0, body_line * 4.2)
        drill_r = max(10.0, pad_r * float(cfg["drill_diameter"]) / float(cfg["pad_diameter"]))
        hole_r = max(7.0, pad_r * float(cfg["finished_hole_diameter"]) / float(cfg["pad_diameter"]))
        pad_bar = max(7.0, min(15.0, body_line * .85))
        self.create_rectangle(cx-pad_r, top, cx+pad_r, top+pad_bar, fill=COLORS["pad"], outline="#713f12")
        self.create_rectangle(cx-pad_r, bottom-pad_bar, cx+pad_r, bottom, fill=COLORS["pad"], outline="#713f12")
        self.create_rectangle(cx-drill_r, top, cx+drill_r, bottom, fill="#b87333", outline="#7c2d12")
        self.create_rectangle(cx-hole_r, top, cx+hole_r, bottom, fill="white", outline="#334155")
        self.create_line(cx-pad_r, top-8, cx+pad_r, top-8, arrow="both", fill=COLORS["dimension"])
        self.create_text(cx, top-body_line-3,
                         text=ellipsize(self, f"焊盘 Ø{format_length(cfg['pad_diameter'], unit)} {unit}",
                                        "ViaCanvasFont", int(drawing_width)),
                         fill=COLORS["dimension"], font="ViaCanvasFont", tags=("dimension-label",))
        self.create_line(cx-drill_r, bottom+8, cx+drill_r, bottom+8, arrow="both", fill="#9a3412")
        self.create_text(cx, bottom+body_line+2,
                         text=ellipsize(self, f"钻孔 Ø{format_length(cfg['drill_diameter'], unit)} {unit}",
                                        "ViaCanvasFont", int(drawing_width)),
                         fill="#9a3412", font="ViaCanvasFont", tags=("dimension-label",))
        self.create_line(cx-hole_r, bottom+body_line+10, cx+hole_r, bottom+body_line+10,
                         arrow="both", fill="#475569")
        self.create_text(cx, bottom+body_line*2+12,
                         text=ellipsize(self, f"成品孔 Ø{format_length(cfg['finished_hole_diameter'], unit)} {unit}",
                                        "ViaCanvasFont", int(drawing_width)),
                         fill="#475569", font="ViaCanvasFont", tags=("dimension-label",))
        info_text = (
            f"环宽  {format_length(derived.annular_ring, unit)} {unit}\n"
            f"孔壁铜  {format_length(derived.wall_thickness, unit)} {unit}\n"
            f"避空 Ø{format_length(cfg['antipad_diameter'], unit)} {unit}\n"
            f"避空单边  {format_length(derived.antipad_clearance, unit)} {unit}\n"
            f"跨越厚度  {format_length(derived.span_thickness, unit)} {unit}\n"
            f"长径比  {format_number(derived.aspect_ratio, 3)}:1\n"
            f"{cfg['start_layer']} → {cfg['end_layer']} · {derived.via_type}"
        )
        info_x = pad + drawing_width + 12 if wide else pad
        info_y = figure_top - body_line if wide else bottom + body_line * 3 + 20
        info_width = max(1, width - info_x - pad) if wide else max(1, width - pad * 2)
        info_item = self.create_text(info_x, info_y, anchor="nw", text=info_text,
                                     width=info_width, font="ViaMonoFont", fill="#334155",
                                     tags=("manufacturing-info",))
        info_box = self.bbox(info_item)
        drawing_bottom = bottom + body_line * 3 + 18
        header_bottom = max(drawing_bottom, float(info_box[3]) if info_box else info_y + mono_line * 7)
        # 叠层状态截面。
        section_title_y = header_bottom + pad
        y0 = section_title_y + title_line + 10
        self.create_line(pad, section_title_y-5, width-pad, section_title_y-5, fill="#cbd5e1")
        self.create_text(pad, section_title_y, anchor="nw", text="叠层跨越与连接状态",
                         font="ViaCardTitleFont", tags=("cross-title",))
        row_h = max(small_line + 8, 18)
        for index, layer in enumerate(derived.layers):
            y = y0 + index * row_h
            color = COLORS[layer.status]
            self.create_rectangle(pad+6, y, width-pad, y+row_h-2, fill=color, outline="#64748b",
                                  tags=("layer-row", f"via-layer-{index}"))
            label = {"connected": "已连接", "clearance": "穿过/避空", "traversed": "跨越", "outside": "未跨越"}[layer.status]
            markers = []
            if layer.name == cfg["start_layer"]: markers.append("起始")
            if layer.name == cfg["end_layer"]: markers.append("终止")
            if layer.status == "clearance":
                markers.append(f"避空Ø{format_length(cfg['antipad_diameter'], unit)}{unit}")
            marker_text = " / ".join(markers)
            full_label = (f"{layer.name} · {layer.layer_type} · {label} · {format_length(layer.thickness, unit)} {unit}"
                          + (f" · {marker_text}" if marker_text else ""))
            shown_label = ellipsize(self, full_label, "ViaSmallFont", width - pad * 2 - 22)
            item = self.create_text(pad+12, y+row_h/2-1, anchor="w", text=shown_label,
                                    font="ViaSmallFont", fill="#172033",
                                    tags=("layer-label", f"via-layer-{index}"))
            self.tag_bind(item, "<Enter>", lambda event, text=full_label: (
                self.configure(cursor="hand2"), self._show_layer_tooltip(event, text)
            ))
            self.tag_bind(item, "<Leave>", lambda _e: (
                self.configure(cursor=""), self._hide_layer_tooltip()
            ))
        footer_y = y0 + len(derived.layers) * row_h + pad
        footer_item = self.create_text(
            pad, footer_y, anchor="nw",
            text=f"全部 {len(derived.layers)} 层均已绘制；使用右侧页面滚动条查看完整叠层。",
            font="ViaCanvasFont", fill="#475569", width=max(1, width-pad*2),
            tags=("cross-footer", "footer-label"),
        )
        footer_box = self.bbox(footer_item)
        requested_height = int(max(
            footer_y + body_line + pad * 2,
            (footer_box[3] if footer_box else footer_y + body_line) + pad,
        ))
        self.configure(scrollregion=(0, 0, width, requested_height))
        if requested_height != self._requested_height:
            self._requested_height = requested_height
            self.configure(height=requested_height)


class GndViaWorkbench(ttk.Frame):
    """独立、可滚动、实时刷新的 GND VIA 工作台。"""

    def __init__(self, parent: tk.Misc, app: Any) -> None:
        super().__init__(parent)
        self.pack(fill="both", expand=True)
        self.app = app
        self.geometry: Any = None
        self.current_config: Optional[Mapping[str, Any]] = None
        self.issues: List[ViaIssue] = []
        self.layout_widgets: Dict[str, tk.Widget] = {}
        self.layout_notes: Dict[str, ttk.Label] = {}
        self.layer_vars: Dict[str, tk.BooleanVar] = {}
        self._loaded_signature: Optional[Tuple[Any, ...]] = None
        self._coordinate_mm_cache: Dict[str, Tuple[float, float]] = {}
        self._coordinate_display_cache: Dict[str, Tuple[str, str]] = {}
        self._build()

    @staticmethod
    def _section(parent: tk.Misc, title: str) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, text=title, padding=(7, 6))
        frame.pack(fill="x", expand=True, padx=3, pady=4)
        frame.columnconfigure(1, weight=1)
        return frame

    def _build(self) -> None:
        panes = ttk.PanedWindow(self, orient="horizontal")
        self.panes = panes
        panes.pack(fill="both", expand=True)
        left = ScrollColumn(panes); right = ScrollColumn(panes)
        self.left_scroll = left
        self.right_scroll = right
        panes.add(left, weight=3); panes.add(right, weight=2)
        lroot, rroot = left.content, right.content

        enabled_box = self._section(lroot, "启用与方案")
        self.enabled_widget = self.app._add(enabled_box, 0, "启用 GND VIA", "gnd_vias.enabled", boolean=True)
        self.enabled_widget.grid_configure(pady=(10, 4))
        if "gnd_vias.enabled" in self.app.field_labels:
            self.app.field_labels["gnd_vias.enabled"].grid_configure(pady=(10, 4))
        self.baseline_enabled = self.app._add(
            enabled_box, 1, "同时生成无 VIA 基线和有 VIA 方案",
            "baseline_comparison.enabled", boolean=True,
        )
        self.baseline_vias = self.app._add(
            enabled_box, 2, "基线比较包含 GND VIA",
            "baseline_comparison.compare_gnd_vias", boolean=True,
        )
        ttk.Label(enabled_box, text="不预设 VIA 一定改善性能；请对比 Sdd11/Sdd21、Sdc21/Scd21 与 TDR。",
                  foreground="#475569", wraplength=690).grid(row=3, column=0, columnspan=4, sticky="w", padx=4, pady=(3, 1))

        mode_box = self._section(lroot, "布局模式")
        self.app._register_hidden_variable("gnd_vias.pattern")
        self.mode_cards = ViaModeCards(mode_box, self.app.variables["gnd_vias.pattern"], self.app._schedule_refresh)
        self.mode_cards.grid(row=0, column=0, columnspan=4, sticky="ew")

        params = self._section(lroot, "布局参数 · 位置由 GeometryCalculator 统一计算")
        fields = (
            ("count", "数量", False, True),
            ("side_orientation", "侧对方向", False, False),
            ("clearance_x", "X 向焊盘边偏移（有符号）", True, False),
            ("clearance_y", "Y 向焊盘边偏移（有符号）", True, False),
            ("pitch_x", "X方向VIA中心节距", True, False),
            ("pitch_y", "Y方向VIA中心节距", True, False),
        )
        for row, (key, label, length, integer) in enumerate(fields):
            path = "gnd_vias." + key
            if key == "side_orientation":
                widget = self.app._add(params, row, label, path, values=("top_bottom", "left_right"), display_values=SIDE_LABELS)
            else:
                widget = self.app._add(params, row, label, path, length=length, numeric=not length, integer=integer,
                                       formula="参与当前布局" if key != "count" else "布局规则决定")
            self.layout_widgets[key] = widget
            if path in self.app.formula_labels:
                self.layout_notes[key] = self.app.formula_labels[path]
                self.layout_notes[key].configure(font="ViaCanvasFont")
        ttk.Label(params, text="正值向外，负值向内；最终是否可用由实际几何碰撞检查决定。节距仍为 VIA 中心到中心。",
                  foreground="#0f766e", wraplength=700).grid(row=7, column=0, columnspan=4, sticky="w", padx=4, pady=(6, 1))

        preview_box = self._section(lroot, "电容区域局部俯视 · 点击可在自定义模式放置 VIA")
        toolbar = ttk.Frame(preview_box); toolbar.grid(row=0, column=0, columnspan=4, sticky="ew")
        self.preview_toolbar = toolbar
        toolbar_actions = ttk.Frame(toolbar); toolbar_actions.pack(fill="x")
        toolbar_options = ttk.Frame(toolbar); toolbar_options.pack(fill="x", pady=(2, 0))
        ttk.Button(toolbar_actions, text="自动适配", command=lambda: self.preview.fit()).pack(side="left")
        ttk.Button(toolbar_actions, text="放大", command=lambda: self.preview.zoom_by(1.2)).pack(side="left", padx=3)
        ttk.Button(toolbar_actions, text="缩小", command=lambda: self.preview.zoom_by(1/1.2)).pack(side="left")
        self.preview_options: Dict[str, tk.BooleanVar] = {}
        for name, label in (("dimensions", "尺寸"), ("signal", "信号铜"), ("antipad", "VIA/信号避空"), ("top_ground", "顶层GND")):
            var = tk.BooleanVar(value=True); self.preview_options[name] = var
            ttk.Checkbutton(toolbar_options, text=label, variable=var,
                            command=lambda n=name, v=var: self.preview.toggle(n, bool(v.get()))).pack(side="left", padx=(8, 0))
        self.preview = ViaTopPreview(preview_box, self._place_from_preview)
        self.preview.grid(row=1, column=0, columnspan=4, sticky="nsew", pady=(5, 0))
        preview_box.rowconfigure(1, weight=1)

        custom = self._section(lroot, "自定义坐标编辑器")
        self.app._register_hidden_variable("gnd_vias.custom_coordinates")
        self.coord_tree = ttk.Treeview(custom, columns=("id", "x", "y", "status"), show="headings", height=6)
        for key, label, width in (("id", "编号", 75), ("x", "X", 100), ("y", "Y", 100), ("status", "状态", 150)):
            self.coord_tree.heading(key, text=label); self.coord_tree.column(key, width=width, anchor="center")
        self.coord_tree.grid(row=0, column=0, columnspan=4, sticky="ew")
        self.coord_tree.bind("<Double-1>", self._edit_coordinate)
        buttons = WrappingButtonBar(custom, (
            ("添加", self._add_coordinate), ("删除", self._delete_coordinates),
            ("复制", self._duplicate_coordinates),
            ("上移", lambda: self._move_coordinate(-1)), ("下移", lambda: self._move_coordinate(1)),
            ("X镜像", lambda: self._mirror_coordinates("x")), ("Y镜像", lambda: self._mirror_coordinates("y")),
            ("四象限复制", self._quadrant_copy), ("CSV粘贴", self._paste_csv), ("清空", self._clear_coordinates),
        ))
        buttons.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(4, 0))
        self.custom_buttons = list(buttons.buttons)
        self.custom_hint = ttk.Label(custom, text="自定义布局允许非对称，但会给出回流路径警告；数量由坐标行数自动派生。",
                                     foreground="#9a3412", wraplength=700)
        self.custom_hint.grid(row=2, column=0, columnspan=4, sticky="w", padx=4, pady=(4, 0))

        cross = self._section(rroot, "孔盘与叠层动态截面")
        self.cross_section = ViaCrossSection(cross)
        self.cross_section.grid(row=0, column=0, columnspan=4, sticky="nsew")

        manufacture = self._section(rroot, "制造参数与派生尺寸")
        self.app._add(manufacture, 0, "钻孔直径", "gnd_vias.drill_diameter", length=True)
        self.app._add(manufacture, 1, "孔壁镀铜厚度", "gnd_vias.plating_thickness", length=True,
                      formula="用户输入")
        finished = self.app._add(manufacture, 2, "成品孔直径", "gnd_vias.finished_hole_diameter", length=True,
                                 formula="= 钻孔 − 2 × 镀铜厚度")
        if isinstance(finished, ttk.Entry):
            finished.configure(state="readonly", style="Derived.TEntry")
        self.app._add(manufacture, 3, "VIA焊盘直径", "gnd_vias.pad_diameter", length=True)
        self.app._add(manufacture, 4, "非连接层避空直径", "gnd_vias.antipad_diameter", length=True)
        self.derived_var = tk.StringVar(value="派生尺寸将在输入后实时更新。")
        ttk.Label(manufacture, textvariable=self.derived_var, foreground="#0f766e", justify="left",
                  wraplength=500).grid(row=5, column=0, columnspan=4, sticky="w", padx=4, pady=(6, 2))

        model = self._section(rroot, "筒壁模型与材料")
        self.app._add(model, 0, "筒壁模型", "gnd_vias.barrel_model", values=("hollow", "solid"), display_values=BARREL_LABELS)
        self.material_combo = self.app._add(
            model, 1, "导体材料", "gnd_vias.material", values=("copper", "pec"),
            display_values={"copper": "铜 copper", "pec": "理想导体 PEC"},
        )
        self.model_note_var = tk.StringVar(value="空心镀铜筒壁：推荐，用于较真实的电磁趋势比较。")
        self.model_note = ttk.Label(model, textvariable=self.model_note_var, foreground="#166534", wraplength=500)
        self.model_note.grid(row=2, column=0, columnspan=4, sticky="w", padx=4, pady=(4, 1))

        layers = self._section(rroot, "叠层跨越与连接层")
        names = tuple(str(layer["name"]) for layer in self.app.config.get("stackup", []))
        self.start_combo = self.app._add(layers, 0, "起始层", "gnd_vias.start_layer", values=names)
        self.end_combo = self.app._add(layers, 1, "终止层", "gnd_vias.end_layer", values=names)
        self.app._register_hidden_variable("gnd_vias.connect_layers")
        ttk.Label(layers, text="连接参考层（仅显示起止范围内的参考平面）").grid(row=2, column=0, columnspan=4, sticky="w", padx=4, pady=(5, 2))
        self.layer_checks = ttk.Frame(layers); self.layer_checks.grid(row=3, column=0, columnspan=4, sticky="ew")
        self.layer_hint_var = tk.StringVar(value="")
        ttk.Label(layers, textvariable=self.layer_hint_var, foreground="#475569", wraplength=500).grid(row=4, column=0, columnspan=4, sticky="w", padx=4, pady=(4, 1))

        top_ground = self._section(rroot, "当前安装面 GND 独立设置")
        self.top_ground_enable = self.app._add(top_ground, 0, "创建安装面 GND 铜皮", "gnd_vias.create_top_ground_copper", boolean=True)
        self.top_ground_clearance = self.app._add(top_ground, 1, "安装面 GND 信号净距", "gnd_vias.top_ground_clearance", length=True)
        ttk.Label(top_ground, text="开窗按每段信号铜轴对齐包围盒加净距生成，属于保守近似；可能移除更多铜皮，请检查剩余铜颈和平面连续性。",
                  foreground="#9a3412", wraplength=500).grid(row=2, column=0, columnspan=4, sticky="w", padx=4, pady=(4, 1))

        summary = self._section(rroot, "结果摘要与即时校验")
        self.status_var = tk.StringVar(value="正在检查 GND VIA…")
        self.status_label = ttk.Label(summary, textvariable=self.status_var, style="StatusGood.TLabel")
        self.status_label.grid(row=0, column=0, columnspan=4, sticky="w", padx=4)
        self.summary_text = tk.Text(summary, height=14, wrap="word", font="ViaMonoFont",
                                    background="#f8fafc", foreground="#172033", relief="flat")
        self.summary_text.grid(row=1, column=0, columnspan=4, sticky="ew", padx=4, pady=4)
        self.issue_text = tk.Text(summary, height=7, wrap="word", font="ViaCanvasFont", relief="flat")
        self.issue_text.tag_configure("error", foreground="#b42318")
        self.issue_text.tag_configure("warning", foreground="#b45309")
        self.issue_text.tag_configure("info", foreground="#166534")
        self.issue_text.grid(row=2, column=0, columnspan=4, sticky="ew", padx=4)

        for path in ("gnd_vias.drill_diameter", "gnd_vias.plating_thickness"):
            self.app.variables[path].trace_add("write", lambda *_: self._derive_finished_hole())

    def prepare_collect(self) -> None:
        """在主窗口收集字段前同步只读派生值和结构化编辑器。"""
        self._derive_finished_hole()
        previous_sync = self.app._ui_sync
        self.app._ui_sync = True
        try:
            pattern = self.app.variables["gnd_vias.pattern"].get()
            if pattern == "four_corner" and self.app.variables["gnd_vias.count"].get() != "4":
                self.app.variables["gnd_vias.count"].set("4")
            elif pattern == "custom":
                count = str(len(self.coord_tree.get_children()))
                if self.app.variables["gnd_vias.count"].get() != count:
                    self.app.variables["gnd_vias.count"].set(count)
            self._sync_coordinate_variable(schedule=False)
            self._sync_layer_variable(schedule=False)
        finally:
            self.app._ui_sync = previous_sync

    def load_config(self, config: Mapping[str, Any]) -> None:
        self._load_coordinates(config["gnd_vias"].get("custom_coordinates", []))
        self.refresh_stackup(config)
        conductor_materials = {
            str(layer.get("material", "")).strip()
            for layer in config.get("stackup", [])
            if layer.get("type") in {"signal", "plane"} and str(layer.get("material", "")).strip()
        }
        conductor_materials.add(str(config["gnd_vias"].get("material", "copper")))
        custom_materials = sorted(name for name in conductor_materials if name not in {"copper", "pec"})
        if isinstance(self.material_combo, ttk.Combobox):
            self.material_combo.configure(values=("铜 copper", "理想导体 PEC", *custom_materials))
        self._derive_finished_hole()

    def refresh_stackup(self, config: Mapping[str, Any]) -> None:
        names = tuple(str(layer["name"]) for layer in config.get("stackup", []))
        if isinstance(self.start_combo, ttk.Combobox): self.start_combo.configure(values=names)
        if isinstance(self.end_combo, ttk.Combobox): self.end_combo.configure(values=names)
        selected = {str(name) for name in config["gnd_vias"].get("connect_layers", [])}
        for child in self.layer_checks.winfo_children(): child.destroy()
        self.layer_vars.clear()
        allowed = applicable_reference_layers(config)
        reference_layers = [layer for layer in config["stackup"]
                            if layer.get("type") == "plane" and layer.get("is_reference_plane")]
        for row, layer in enumerate(reference_layers):
            name = str(layer["name"]); var = tk.BooleanVar(value=name in selected)
            check = ttk.Checkbutton(self.layer_checks, text=name, variable=var, command=self._sync_layer_variable)
            check.grid(row=row // 3, column=row % 3, sticky="w", padx=4, pady=2)
            if not allowed.get(name, False): check.configure(state="disabled")
            self.layer_vars[name] = var
        self._sync_layer_variable(schedule=False)

    def _derive_finished_hole(self) -> None:
        if self.app._ui_sync:
            return
        try:
            drill = convert_length(float(self.app.variables["gnd_vias.drill_diameter"].get()), self.app.display_unit, "mm")
            plating = convert_length(float(self.app.variables["gnd_vias.plating_thickness"].get()), self.app.display_unit, "mm")
            value = drill - 2.0 * plating
            path = "gnd_vias.finished_hole_diameter"
            shown = format_length(value, self.app.display_unit)
            self.app._length_mm_cache[path] = value
            self.app._length_display_snapshot[path] = shown
            self.app._ui_sync = True
            self.app.variables[path].set(shown)
        except (KeyError, TypeError, ValueError, tk.TclError):
            pass
        finally:
            self.app._ui_sync = False

    def _set_states(self, config: Mapping[str, Any]) -> None:
        enabled = bool(config["gnd_vias"].get("enabled"))
        pattern = str(config["gnd_vias"].get("pattern"))
        side = str(config["gnd_vias"].get("side_orientation", "top_bottom"))
        active = active_layout_fields(pattern, side)
        self.mode_cards.set_enabled(enabled)
        self.baseline_enabled.configure(state="normal" if enabled else "disabled")
        self.baseline_vias.configure(
            state="normal" if enabled and bool(config.get("baseline_comparison", {}).get("enabled")) else "disabled"
        )
        for key, widget in self.layout_widgets.items():
            applicable = key in active
            if isinstance(widget, ttk.Combobox):
                widget.configure(state="readonly" if enabled and applicable else "disabled")
            elif isinstance(widget, ttk.Entry):
                readonly = key == "count" and pattern in {"four_corner", "custom"}
                widget.configure(state="readonly" if enabled and applicable and readonly else
                                 "normal" if enabled and applicable else "disabled",
                                 style="Derived.TEntry" if readonly or not applicable else "TEntry")
            if key in self.layout_notes:
                self.layout_notes[key].configure(text="参与当前布局" if applicable else "不适用于当前布局",
                                                 foreground="#0f766e" if applicable else "#94a3b8")
        editable_paths = ("gnd_vias.drill_diameter", "gnd_vias.plating_thickness", "gnd_vias.pad_diameter",
                          "gnd_vias.antipad_diameter", "gnd_vias.barrel_model", "gnd_vias.material",
                          "gnd_vias.start_layer", "gnd_vias.end_layer")
        for path in editable_paths:
            widget = self.app.widgets.get(path)
            if isinstance(widget, ttk.Combobox): widget.configure(state="readonly" if enabled else "disabled")
            elif widget is not None: widget.configure(state="normal" if enabled else "disabled")
        finished = self.app.widgets.get("gnd_vias.finished_hole_diameter")
        if finished is not None: finished.configure(state="readonly" if enabled else "disabled")
        stack_names = [str(layer["name"]) for layer in config.get("stackup", [])]
        start_name = str(config["gnd_vias"].get("start_layer", ""))
        try:
            start_index = stack_names.index(start_name)
            end_values = tuple(stack_names[start_index:])
        except ValueError:
            end_values = tuple(stack_names)
        if isinstance(self.end_combo, ttk.Combobox):
            self.end_combo.configure(values=end_values)
        custom_enabled = enabled and pattern == "custom"
        for button in self.custom_buttons: button.configure(state="normal" if custom_enabled else "disabled")
        self.coord_tree.configure(selectmode="extended" if custom_enabled else "none")
        self.top_ground_enable.configure(state="normal" if enabled else "disabled")
        top_ground_enabled = enabled and bool(config["gnd_vias"].get("create_top_ground_copper"))
        self.top_ground_clearance.configure(state="normal" if top_ground_enabled else "disabled")
        for child in self.layer_checks.winfo_children():
            name = str(child.cget("text")); allowed = applicable_reference_layers(config).get(name, False)
            child.configure(state="normal" if enabled and allowed else "disabled")
        barrel = str(config["gnd_vias"].get("barrel_model"))
        if barrel == "solid":
            self.model_note_var.set("实心圆柱近似：仅用于快速趋势预筛选，会高估铜体积并低估孔内电磁场。")
            self.model_note.configure(foreground="#b45309")
        else:
            self.model_note_var.set("空心镀铜筒壁：推荐，表达钻孔、成品孔和孔壁镀铜。")
            self.model_note.configure(foreground="#166534")

    def show(self, config: Mapping[str, Any], geometry: Any, validation: Any) -> None:
        self.current_config = config; self.geometry = geometry
        self.issues = validate_via_workbench(config, geometry)
        existing = {item.message for item in self.issues}
        for message in validation.errors:
            if ("VIA" in message or "gnd_vias" in message) and message not in existing:
                self.issues.append(ViaIssue("error", message))
        for message in validation.warnings:
            if ("VIA" in message or "gnd_vias" in message) and message not in existing:
                self.issues.append(ViaIssue("warning", message))
        derived = derive_via_values(config, geometry)
        self._set_states(config)
        self.preview.show(config, geometry, self.app.display_unit, self.issues)
        self.cross_section.show(config, derived, self.app.display_unit)
        self._update_feedback(config, derived)
        self._update_coordinate_status(config, geometry)
        connected = [layer.name for layer in derived.layers if layer.status == "connected"]
        clearance = [layer.name for layer in derived.layers if layer.status == "clearance"]
        self.layer_hint_var.set(f"已连接：{', '.join(connected) or '无'}；穿过但避空：{', '.join(clearance) or '无'}")

    def show_error(self, message: str) -> None:
        self.preview.show_error(message)
        self.status_var.set("● GND VIA 输入尚未构成有效配置")
        self.status_label.configure(style="StatusError.TLabel")
        self.issue_text.delete("1.0", "end"); self.issue_text.insert("end", message, "error")

    def on_font_scale_changed(self, tier: str) -> None:
        """主窗口尺寸档位变化时同步放大图形标注和可视区域。"""
        if getattr(self.app, "_destroying", False):
            return
        preview_height = max(360, font_linespace(self.preview, "ViaCanvasFont") * 18 + 80)
        self.preview.configure(height=preview_height)
        self.cross_section.set_font_tier(tier)
        self.coord_tree.configure(height={"normal": 6, "large": 8, "ultra": 10}[tier])
        self.preview.redraw()
        self.cross_section.redraw()
        previous = getattr(self, "_font_scroll_job", None)
        if previous:
            try:
                self.app.after_cancel(previous)
            except tk.TclError:
                pass
        self._font_scroll_job = self.app.after_idle(self._refresh_font_scrollregions)

    def _refresh_font_scrollregions(self) -> None:
        self._font_scroll_job = None
        self.left_scroll.canvas.configure(scrollregion=self.left_scroll.canvas.bbox("all"))
        self.right_scroll.canvas.configure(scrollregion=self.right_scroll.canvas.bbox("all"))

    def cancel_pending_callbacks(self) -> None:
        job = getattr(self, "_font_scroll_job", None)
        if job:
            try:
                self.app.after_cancel(job)
            except tk.TclError:
                pass
            self._font_scroll_job = None

    def _update_feedback(self, config: Mapping[str, Any], derived: ViaDerived) -> None:
        errors = [item for item in self.issues if item.level == "error"]
        warnings = [item for item in self.issues if item.level == "warning"]
        if errors:
            self.status_var.set(f"● {len(errors)} 个错误 · {len(warnings)} 个警告")
            self.status_label.configure(style="StatusError.TLabel")
        elif warnings:
            self.status_var.set(f"▲ 配置可用，但有 {len(warnings)} 个警告")
            self.status_label.configure(style="StatusWarn.TLabel")
        else:
            self.status_var.set("✓ GND VIA 配置有效")
            self.status_label.configure(style="StatusGood.TLabel")
        unit = self.app.display_unit
        def length(value: Optional[float]) -> str:
            return "—" if value is None else f"{format_length(value, unit)} {unit}"
        bounds = "—" if derived.coordinate_bounds is None else (
            f"X[{length(derived.coordinate_bounds[0])}, {length(derived.coordinate_bounds[1])}] "
            f"Y[{length(derived.coordinate_bounds[2])}, {length(derived.coordinate_bounds[3])}]"
        )
        connected = [layer.name for layer in derived.layers if layer.status == "connected"]
        clearance = [layer.name for layer in derived.layers if layer.status == "clearance"]
        cfg = config["gnd_vias"]
        rows = (
            ("启用状态", "启用" if cfg.get("enabled") else "关闭"),
            ("布局模式", MODE_LABELS.get(cfg["pattern"], cfg["pattern"])),
            ("配置 / 实际数量", f"{derived.configured_count} / {derived.actual_count}"),
            ("坐标范围", bounds), ("最小中心距", length(derived.minimum_center_distance)),
            ("最小焊盘边缘距", length(derived.minimum_pad_edge_distance)),
            ("最小信号铜净距", length(derived.minimum_signal_clearance)),
            ("环宽", length(derived.annular_ring)), ("实际孔壁厚度", length(derived.wall_thickness)),
            ("避空单边", length(derived.antipad_clearance)), ("跨越厚度", length(derived.span_thickness)),
            ("长径比", f"{format_number(derived.aspect_ratio, 3)}:1"),
            ("起止层 / 类型", f"{cfg['start_layer']} → {cfg['end_layer']} / {derived.via_type}"),
            ("连接参考层", ", ".join(connected) or "无"), ("避空参考层", ", ".join(clearance) or "无"),
            ("安装面 GND", "启用（保守AABB开窗）" if cfg.get("create_top_ground_copper") and cfg.get("enabled") else "关闭"),
            ("错误 / 警告", f"{len(errors)} / {len(warnings)}"),
        )
        self.summary_text.configure(state="normal"); self.summary_text.delete("1.0", "end")
        self.summary_text.insert("end", "\n".join(f"{label:<12} {value}" for label, value in rows))
        self.summary_text.configure(state="disabled")
        self.derived_var.set(
            f"环宽 {length(derived.annular_ring)}；孔壁铜 {length(derived.wall_thickness)}；"
            f"避空单边 {length(derived.antipad_clearance)}；长径比 {format_number(derived.aspect_ratio, 3)}:1"
        )
        self.issue_text.delete("1.0", "end")
        if not self.issues:
            self.issue_text.insert("end", "✓ 当前未发现 GND VIA 错误或制造警告。", "info")
        else:
            for issue in self.issues:
                prefix = "●" if issue.level == "error" else "▲"
                self.issue_text.insert("end", f"{prefix} {issue.message}\n", issue.level)

    def _load_coordinates(self, points: Iterable[Sequence[float]]) -> None:
        for item in self.coord_tree.get_children(): self.coord_tree.delete(item)
        self._coordinate_mm_cache.clear()
        self._coordinate_display_cache.clear()
        for point in points:
            self._insert_coordinate(float(point[0]), float(point[1]))
        self._renumber_coordinates()

    def _coordinate_points_mm(self) -> List[Tuple[float, float]]:
        result = []
        for item in self.coord_tree.get_children():
            values = self.coord_tree.item(item, "values")
            shown = (str(values[1]), str(values[2]))
            if item in self._coordinate_mm_cache and self._coordinate_display_cache.get(item) == shown:
                point = self._coordinate_mm_cache[item]
            else:
                point = (convert_length(float(shown[0]), self.app.display_unit, "mm"),
                         convert_length(float(shown[1]), self.app.display_unit, "mm"))
                self._coordinate_mm_cache[item] = point
                self._coordinate_display_cache[item] = shown
            result.append(point)
        return result

    def _insert_coordinate(self, x_mm: float, y_mm: float, index: str = "end") -> None:
        self.coord_tree.insert("", index, values=("", format_length(x_mm, self.app.display_unit),
                                                  format_length(y_mm, self.app.display_unit), "待检查"))
        item = self.coord_tree.get_children()[-1] if index == "end" else self.coord_tree.get_children()[int(index)]
        values = self.coord_tree.item(item, "values")
        self._coordinate_mm_cache[item] = (float(x_mm), float(y_mm))
        self._coordinate_display_cache[item] = (str(values[1]), str(values[2]))

    def _renumber_coordinates(self) -> None:
        for index, item in enumerate(self.coord_tree.get_children(), 1):
            values = list(self.coord_tree.item(item, "values")); values[0] = f"VIA{index:02d}"
            self.coord_tree.item(item, values=values)

    def _sync_coordinate_variable(self, schedule: bool = True) -> None:
        text = "; ".join(f"{format_length(x, self.app.display_unit)},{format_length(y, self.app.display_unit)}"
                         for x, y in self._coordinate_points_mm())
        if self.app.variables["gnd_vias.custom_coordinates"].get() != text:
            self.app.variables["gnd_vias.custom_coordinates"].set(text)
        if self.app.variables["gnd_vias.pattern"].get() == "custom":
            count = str(len(self.coord_tree.get_children()))
            if self.app.variables["gnd_vias.count"].get() != count:
                self.app.variables["gnd_vias.count"].set(count)
        if schedule: self.app._schedule_refresh()

    def _sync_layer_variable(self, schedule: bool = True) -> None:
        names = [name for name, var in self.layer_vars.items() if var.get()]
        text = ",".join(names)
        if self.app.variables["gnd_vias.connect_layers"].get() != text:
            self.app.variables["gnd_vias.connect_layers"].set(text)
        if schedule: self.app._schedule_refresh()

    def _add_coordinate(self) -> None:
        text = simpledialog.askstring("添加 VIA", f"输入 X,Y（{self.app.display_unit}）", parent=self.app, initialvalue="0,0")
        if not text: return
        try:
            x, y = (float(value.strip()) for value in text.split(",", 1))
            self._insert_coordinate(convert_length(x, self.app.display_unit, "mm"), convert_length(y, self.app.display_unit, "mm"))
            self._renumber_coordinates(); self._sync_coordinate_variable()
        except Exception as exc:
            messagebox.showerror("坐标格式错误", str(exc), parent=self.app)

    def _delete_coordinates(self) -> None:
        for item in self.coord_tree.selection():
            self.coord_tree.delete(item)
            self._coordinate_mm_cache.pop(item, None)
            self._coordinate_display_cache.pop(item, None)
        self._renumber_coordinates(); self._sync_coordinate_variable()

    def _duplicate_coordinates(self) -> None:
        for item in self.coord_tree.selection():
            values = self.coord_tree.item(item, "values")
            shown = (str(values[1]), str(values[2]))
            point = self._coordinate_mm_cache.get(item)
            if point is None or self._coordinate_display_cache.get(item) != shown:
                point = (convert_length(float(shown[0]), self.app.display_unit, "mm"),
                         convert_length(float(shown[1]), self.app.display_unit, "mm"))
            self._insert_coordinate(point[0], point[1])
        self._renumber_coordinates(); self._sync_coordinate_variable()

    def _move_coordinate(self, delta: int) -> None:
        selected = self.coord_tree.selection()
        if not selected: return
        item = selected[0]; index = self.coord_tree.index(item); target = max(0, min(len(self.coord_tree.get_children())-1, index+delta))
        self.coord_tree.move(item, "", target); self._renumber_coordinates(); self._sync_coordinate_variable()

    def _mirror_coordinates(self, axis: str) -> None:
        selected = self.coord_tree.selection() or self.coord_tree.get_children()
        for item in selected:
            values = list(self.coord_tree.item(item, "values")); x, y = float(values[1]), float(values[2])
            values[1] = format_number(-x if axis == "x" else x, 6)
            values[2] = format_number(-y if axis == "y" else y, 6)
            self.coord_tree.item(item, values=values)
        self._sync_coordinate_variable()

    def _quadrant_copy(self) -> None:
        points = self._coordinate_points_mm(); existing = {(round(x, 12), round(y, 12)) for x, y in points}
        for x, y in list(points):
            for px, py in ((x, y), (-x, y), (x, -y), (-x, -y)):
                key = (round(px, 12), round(py, 12))
                if key not in existing: self._insert_coordinate(px, py); existing.add(key)
        self._renumber_coordinates(); self._sync_coordinate_variable()

    def _paste_csv(self) -> None:
        try:
            text = self.app.clipboard_get(); added = 0
            for line in text.replace(";", "\n").splitlines():
                parts = [item.strip() for item in line.replace("\t", ",").split(",") if item.strip()]
                if len(parts) < 2: continue
                x, y = float(parts[-2]), float(parts[-1])
                self._insert_coordinate(convert_length(x, self.app.display_unit, "mm"), convert_length(y, self.app.display_unit, "mm")); added += 1
            if not added: raise ValueError("剪贴板中没有可识别的 X,Y 行。")
            self._renumber_coordinates(); self._sync_coordinate_variable()
        except Exception as exc:
            messagebox.showerror("CSV 粘贴失败", str(exc), parent=self.app)

    def _clear_coordinates(self) -> None:
        for item in self.coord_tree.get_children(): self.coord_tree.delete(item)
        self._coordinate_mm_cache.clear()
        self._coordinate_display_cache.clear()
        self._sync_coordinate_variable()

    def _edit_coordinate(self, _event: tk.Event) -> None:
        selected = self.coord_tree.selection()
        if not selected: return
        item = selected[0]; values = list(self.coord_tree.item(item, "values"))
        text = simpledialog.askstring("编辑坐标", f"输入 X,Y（{self.app.display_unit}）", parent=self.app,
                                      initialvalue=f"{values[1]},{values[2]}")
        if not text: return
        try:
            x, y = (float(value.strip()) for value in text.split(",", 1)); values[1:3] = (format_number(x, 6), format_number(y, 6))
            self.coord_tree.item(item, values=values); self._sync_coordinate_variable()
        except Exception as exc:
            messagebox.showerror("坐标格式错误", str(exc), parent=self.app)

    def _place_from_preview(self, x_mm: float, y_mm: float) -> None:
        self._insert_coordinate(x_mm, y_mm); self._renumber_coordinates(); self._sync_coordinate_variable()

    def _update_coordinate_status(self, config: Mapping[str, Any], geometry: Any) -> None:
        duplicates = []
        points = [tuple(map(float, point)) for point in config["gnd_vias"].get("custom_coordinates", [])]
        for index, point in enumerate(points):
            if point in points[:index]: duplicates.append(index)
        for index, item in enumerate(self.coord_tree.get_children()):
            values = list(self.coord_tree.item(item, "values"))
            values[3] = "重复" if index in duplicates else "有效"
            self.coord_tree.item(item, values=values, tags=("bad",) if index in duplicates else ())
        self.coord_tree.tag_configure("bad", foreground="#b42318")
