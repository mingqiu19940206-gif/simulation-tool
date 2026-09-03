"""Reusable widgets and pure helpers for the capacitor/model editor."""
from __future__ import annotations

import math
import re
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import ttk
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from config_manager import format_length, format_number
from gui_layout import (
    CanvasLabelPlacer, attach_mousewheel, canvas_viewport, debounce_after, ellipsize,
    font_linespace, mousewheel_steps, resolve_font,
)

ENUM_LABELS: Dict[str, Dict[str, str]] = {
    "capacitor.mount_side": {"top": "顶层安装（Top）", "bottom": "底层安装（Bottom）"},
    "capacitor.package": {
        "0201": "0201（公制 0603）", "0402": "0402（公制 1005）", "custom": "自定义",
    },
    "capacitor.pad_pitch_mode": {
        "auto_from_gap": "根据焊盘长度和间隙自动计算", "advanced_pitch": "高级中心距模式",
    },
    "capacitor.bridge_length_mode": {"auto": "自动计算", "manual": "手动输入"},
    "capacitor_model.mode": {
        "ideal_bridge": "理想短路（不模拟电容）", "lumped_rlc": "集总串联 RLC",
        "sparameter": "Touchstone S 参数模型", "open": "开路（仅焊盘）",
    },
}


@dataclass(frozen=True)
class EditorIssue:
    level: str
    message: str
    path: str = ""


@dataclass(frozen=True)
class TouchstoneInfo:
    path: Path
    ports: int
    frequency_unit: str
    frequency_min: Optional[float]
    frequency_max: Optional[float]
    reference_ohm: Optional[float]

    @property
    def frequency_summary(self) -> str:
        if self.frequency_min is None or self.frequency_max is None:
            return "频率范围：未能解析"
        return (f"频率范围：{format_number(self.frequency_min, 6)}–"
                f"{format_number(self.frequency_max, 6)} {self.frequency_unit}")


@dataclass(frozen=True)
class DimensionSpec:
    """A dimension derived from the authoritative geometry result."""

    path: str
    label: str
    category: str
    value: float
    first: Tuple[float, float]
    second: Tuple[float, float]
    offset: float
    structures: Tuple[str, ...]
    is_angle: bool = False


def _point_tuple(point: Any) -> Tuple[float, float]:
    return float(point.x), float(point.y)


def _midpoint(first: Any, second: Any) -> Tuple[float, float]:
    return ((float(first.x) + float(second.x)) / 2.0,
            (float(first.y) + float(second.y)) / 2.0)


def _distance(first: Tuple[float, float], second: Tuple[float, float]) -> float:
    return math.hypot(second[0] - first[0], second[1] - first[1])


def build_dimension_specs(geometry: Any) -> List[DimensionSpec]:
    """Build every local dimension from calculated centers and polygons.

    No capacitor placement formula is repeated here.  Even rotated edge lengths,
    the inner pad gap and the effective X center come from the geometry returned
    by :class:`GeometryCalculator`.
    """

    body = geometry.body_outlines["C_P"]
    pad_in = geometry.pad_shapes["P_IN"]
    pad_out = geometry.pad_shapes["P_OUT"]
    bridge = geometry.bridge_shapes.get("P")
    cap_p = geometry.capacitor_centers["C_P"]
    cap_n = geometry.capacitor_centers["C_N"]
    pad_in_center = geometry.pad_centers["P_IN"]
    pad_out_center = geometry.pad_centers["P_OUT"]

    body_length = (_point_tuple(body[0]), _point_tuple(body[1]))
    body_width = (_point_tuple(body[1]), _point_tuple(body[2]))
    pad_length = (_point_tuple(pad_in.points[0]), _point_tuple(pad_in.points[1]))
    pad_width = (_point_tuple(pad_in.points[1]), _point_tuple(pad_in.points[2]))
    gap = (_midpoint(pad_in.points[1], pad_in.points[2]),
           _midpoint(pad_out.points[0], pad_out.points[3]))
    pitch = (_point_tuple(pad_in_center), _point_tuple(pad_out_center))
    caps = (_point_tuple(cap_n), _point_tuple(cap_p))
    center_x = ((0.0, 0.0), ((float(cap_p.x) + float(cap_n.x)) / 2.0, 0.0))
    rotation = math.degrees(math.atan2(
        pad_out_center.y - pad_in_center.y,
        pad_out_center.x - pad_in_center.x,
    ))
    result = [
        DimensionSpec("capacitor.body_length", "本体长度", "body", _distance(*body_length),
                      *body_length, -14, ("body",)),
        DimensionSpec("capacitor.body_width", "本体宽度", "body", _distance(*body_width),
                      *body_width, -14, ("body",)),
        DimensionSpec("capacitor.pad_length", "焊盘长度", "pad", _distance(*pad_length),
                      *pad_length, 13, ("pad",)),
        DimensionSpec("capacitor.pad_width", "焊盘宽度", "pad", _distance(*pad_width),
                      *pad_width, 13, ("pad",)),
        DimensionSpec("capacitor.pad_gap", "焊盘间隙", "pad", _distance(*gap),
                      *gap, -20, ("pad", "bridge")),
        DimensionSpec("capacitor.pad_pitch", "焊盘中心距", "pad", _distance(*pitch),
                      *pitch, 28, ("pad",)),
        DimensionSpec("capacitor.cap_pitch", "P/N 中心距", "layout", _distance(*caps),
                      *caps, -38, ("pad", "body", "bridge")),
        DimensionSpec("capacitor.cap_center_x", "电容中心 X（有效）", "layout", center_x[1][0],
                      *center_x, 34, ("pad", "body", "bridge")),
        DimensionSpec("capacitor.pad_rotation", "焊盘旋转", "pad", rotation,
                      _point_tuple(cap_p), _point_tuple(pad_out_center), -26, ("pad", "body", "bridge"), True),
    ]
    if bridge is not None:
        bridge_length = (_point_tuple(bridge.points[0]), _point_tuple(bridge.points[1]))
        bridge_width = (_point_tuple(bridge.points[1]), _point_tuple(bridge.points[2]))
        result[-1:-1] = [
            DimensionSpec("capacitor.bridge_width", "桥接宽度", "bridge", _distance(*bridge_width),
                          *bridge_width, -12, ("bridge",)),
            DimensionSpec("capacitor.bridge_length", "桥接长度", "bridge", _distance(*bridge_length),
                          *bridge_length, 12, ("bridge",)),
        ]
    return result


def calculate_srf_ghz(capacitance_pf: float, esl_nh: float) -> Optional[float]:
    """Approximate series self-resonant frequency in GHz."""
    if capacitance_pf <= 0 or esl_nh <= 0:
        return None
    hz = 1.0 / (2.0 * math.pi * math.sqrt(capacitance_pf * 1e-12 * esl_nh * 1e-9))
    return hz / 1e9


def inspect_touchstone(path_text: str, base_directory: Optional[Path] = None) -> Tuple[Optional[TouchstoneInfo], List[str]]:
    raw = str(path_text).strip()
    if not raw:
        return None, ["请选择公共 Touchstone 文件。"]
    path = Path(raw).expanduser()
    if not path.is_absolute() and base_directory:
        path = Path(base_directory) / path
    path = path.resolve()
    suffix = path.suffix.lower()
    if suffix not in {".s2p", ".s4p", ".ts"}:
        return None, ["文件扩展名必须为 .s2p、.s4p 或 .ts。"]
    if not path.is_file():
        return None, [f"Touchstone 文件不存在：{path}"]
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        return None, [f"无法读取 Touchstone 文件：{exc}"]
    ports = int(suffix[2]) if re.fullmatch(r"\.s[24]p", suffix) else 0
    match = re.search(r"(?im)^\s*\[number\s+of\s+ports\]\s*(\d+)", text)
    if match:
        ports = int(match.group(1))
    if ports not in {2, 4}:
        return None, ["无法从 Touchstone 文件确定 2/4 端口数量。"]
    frequency_unit = "GHz"
    reference: Optional[float] = None
    header = re.search(r"(?im)^\s*#\s*(hz|khz|mhz|ghz)\s+\S+\s+\S+\s+r\s+([0-9.eE+\-]+)", text)
    if header:
        frequency_unit = header.group(1).upper()
        try:
            reference = float(header.group(2))
        except ValueError:
            reference = None
    numeric_tokens: List[float] = []
    for line in text.splitlines():
        clean = line.split("!", 1)[0].strip()
        if not clean or clean.startswith(("#", "[")):
            continue
        for token in clean.split():
            try:
                numeric_tokens.append(float(token))
            except ValueError:
                pass
    record_size = 1 + 2 * ports * ports
    frequencies = [numeric_tokens[index] for index in range(0, len(numeric_tokens), record_size)
                   if index + record_size <= len(numeric_tokens)]
    info = TouchstoneInfo(path, ports, frequency_unit,
                          min(frequencies) if frequencies else None,
                          max(frequencies) if frequencies else None, reference)
    errors = [] if frequencies else ["文件可读取，但未解析到完整的网络数据记录。"]
    return info, errors


def validate_capacitor_editor(config: Mapping[str, Any], base_directory: Optional[Path] = None) -> List[EditorIssue]:
    cap, model = config["capacitor"], config["capacitor_model"]
    trace = config["trace"]
    issues: List[EditorIssue] = []
    for key in ("body_length", "body_width", "body_height", "pad_length", "pad_width"):
        if float(cap[key]) <= 0:
            issues.append(EditorIssue("error", f"{key} 必须大于 0。", f"capacitor.{key}"))
    if float(cap["pad_gap"]) < 0:
        issues.append(EditorIssue("error", "pad_gap 不能小于 0。", "capacitor.pad_gap"))
    if float(cap["pad_pitch"]) <= float(cap["pad_length"]):
        issues.append(EditorIssue("error", "pad_pitch 必须大于 pad_length。", "capacitor.pad_pitch"))
    mode = model["mode"]
    if float(cap["cap_pitch"]) <= max(float(trace["trace_width"]),
                                      float(cap["bridge_width"]) if mode == "ideal_bridge" else 0.0):
        issues.append(EditorIssue("error", "cap_pitch 过小，会造成 P/N 信号导体短路。", "capacitor.cap_pitch"))
    if mode == "ideal_bridge":
        if float(cap["bridge_width"]) <= 0:
            issues.append(EditorIssue("error", "bridge_width 必须大于 0。", "capacitor.bridge_width"))
        elif float(cap["bridge_width"]) > float(cap["pad_width"]):
            issues.append(EditorIssue("warning", "桥接宽度大于焊盘宽度，请确认模型意图。", "capacitor.bridge_width"))
        if float(cap["bridge_length"]) <= 0:
            issues.append(EditorIssue("error", "bridge_length 必须大于 0。", "capacitor.bridge_length"))
        elif float(cap["bridge_length"]) + 1e-9 < float(cap["pad_gap"]):
            issues.append(EditorIssue("error", "bridge_length 未覆盖焊盘间隙。", "capacitor.bridge_length"))
    if mode == "lumped_rlc":
        if float(model["capacitance_pf"]) <= 0:
            issues.append(EditorIssue("error", "电容 C 必须大于 0。", "capacitor_model.capacitance_pf"))
        for key, label in (("esr_ohm", "ESR"), ("esl_nh", "ESL")):
            if float(model[key]) < 0:
                issues.append(EditorIssue("error", f"{label} 不能小于 0。", f"capacitor_model.{key}"))
        if float(model["capacitance_pf"]) > 1e6:
            issues.append(EditorIssue("warning", "电容值明显偏大，请确认单位为 pF。", "capacitor_model.capacitance_pf"))
    elif mode == "sparameter":
        _, errors = inspect_touchstone(str(model.get("touchstone_file", "")), base_directory)
        issues.extend(EditorIssue("error", item, "capacitor_model.touchstone_file") for item in errors)
    return issues


class ScrollableFrame(ttk.Frame):
    """A mouse-wheel friendly vertical scroll container."""
    def __init__(self, master: tk.Misc, *, fill_viewport: bool = False,
                 **kwargs: Any) -> None:
        super().__init__(master, **kwargs)
        self.fill_viewport = bool(fill_viewport)
        self._layout_job: Optional[str] = None
        self._last_window_size = (0, 0)
        self._syncing = False
        self.canvas = tk.Canvas(self, highlightthickness=0, background="#f7f8fa")
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.content = ttk.Frame(self.canvas, padding=(2, 2))
        self._window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.content.bind("<Configure>", self._schedule_layout)
        self.canvas.bind("<Configure>", self._schedule_layout)
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.bind(sequence, self._wheel)
            self.content.bind(sequence, self._wheel)
        attach_mousewheel(self, self.canvas, self.content)

    STAGING_HEIGHT = 4096
    SCROLL_EXTRA = 120

    def _schedule_layout(self, _event: Optional[tk.Event] = None) -> None:
        """Coalesce content/viewport changes into one stable geometry update."""
        if getattr(self, "_syncing", False):
            return
        # Staging/final height writes fire <Configure> on the embedded frame.
        # Ignore those or non-fill layout restages forever.
        if (
            not self.fill_viewport
            and _event is not None
            and getattr(_event, "widget", None) is self.content
        ):
            return
        if self._layout_job is None:
            self._layout_job = self.after_idle(self._sync_layout)

    @staticmethod
    def _padding_box(widget: tk.Misc) -> Tuple[int, int, int, int]:
        try:
            raw = widget.cget("padding")
        except tk.TclError:
            return (0, 0, 0, 0)
        if isinstance(raw, (list, tuple)):
            values = [int(float(value)) for value in raw]
        else:
            parts = str(raw).replace(",", " ").split()
            values = [int(float(part)) for part in parts] if parts else [0]
        if not values:
            return (0, 0, 0, 0)
        if len(values) == 1:
            return (values[0], values[0], values[0], values[0])
        if len(values) == 2:
            return (values[0], values[1], values[0], values[1])
        if len(values) == 3:
            return (values[0], values[1], values[2], values[1])
        return (values[0], values[1], values[2], values[3])

    def occupied_content_bottom(self) -> int:
        """Bottom of packed content after a full-height staging pass.

        Uses the last packed child's ``winfo_y() + winfo_height()`` plus content
        padding and a fixed extra.  When the capacitor HFSS group is a sibling,
        its reqheight and the create-body checkbox also floor the bottom.
        """

        try:
            self.update_idletasks()
        except tk.TclError:
            return 1 + self.SCROLL_EXTRA
        _pad_left, _pad_top, _pad_right, pad_bottom = self._padding_box(self.content)
        last_bottom = 0
        try:
            children = self.content.winfo_children()
        except tk.TclError:
            children = ()
        packed = []
        for child in children:
            try:
                if child.winfo_manager() != "pack":
                    continue
                packed.append(child)
            except tk.TclError:
                continue
        mapped = [child for child in packed if child.winfo_ismapped()]
        last = (mapped or packed)[-1] if (mapped or packed) else None
        if last is not None:
            last_bottom = int(last.winfo_y()) + int(last.winfo_height())
        try:
            app = self.winfo_toplevel()
        except tk.TclError:
            app = None
        hfss = getattr(app, "cap_hfss_box", None) if app is not None else None
        if hfss is not None and getattr(hfss, "master", None) is self.content:
            last_bottom = max(
                last_bottom,
                int(hfss.winfo_y()) + int(hfss.winfo_reqheight()) + 80,
            )
            widgets = getattr(app, "widgets", None) or {}
            checkbox = widgets.get("capacitor.create_body_in_hfss")
            if checkbox is not None:
                rel_y = 0
                owner: Optional[tk.Misc] = checkbox
                while owner is not None and owner is not self.content:
                    rel_y += int(owner.winfo_y())
                    owner = getattr(owner, "master", None)
                if owner is self.content:
                    last_bottom = max(
                        last_bottom,
                        rel_y + int(checkbox.winfo_height()) + 48,
                    )
        return max(1, last_bottom + pad_bottom + self.SCROLL_EXTRA)

    def _sync_layout(self) -> None:
        self._layout_job = None
        try:
            viewport_width = max(1, self.canvas.winfo_width())
            viewport_height = max(1, self.canvas.winfo_height())
            natural_height = max(1, self.content.winfo_reqheight())
        except tk.TclError:
            return
        if self.fill_viewport:
            window_height = max(viewport_height, natural_height)
            window_size = (viewport_width, window_height)
            if window_size != self._last_window_size:
                self.canvas.itemconfigure(
                    self._window,
                    width=viewport_width,
                    height=window_height,
                )
                self._last_window_size = window_size
            self.canvas.configure(
                scrollregion=(0, 0, viewport_width, window_height),
            )
            return
        self._syncing = True
        try:
            self.update_idletasks()
            # Always remount packed children at a tall staging height first.
            # A short previous occupied_bottom would otherwise keep the last
            # LabelFrame clipped, so later measures stay undercounted.
            self.canvas.itemconfigure(
                self._window, width=viewport_width, height=self.STAGING_HEIGHT,
            )
            self.update_idletasks()
            occupied_bottom = self.occupied_content_bottom()
            self.canvas.itemconfigure(
                self._window,
                width=viewport_width,
                height=occupied_bottom,
            )
            self._last_window_size = (viewport_width, occupied_bottom)
            self.canvas.configure(scrollregion=(0, 0, viewport_width, occupied_bottom))
        except tk.TclError:
            return
        finally:
            self._syncing = False

    def refresh_layout(self) -> None:
        """Request a layout pass after font or child-request changes."""
        self._schedule_layout()

    def _wheel(self, event: tk.Event) -> str:
        steps = mousewheel_steps(event)
        if steps:
            self.canvas.yview_scroll(steps, "units")
        return "break"

    def _scrollregion_height(self) -> float:
        """Height of the canvas scrollregion, matching what ``yview_moveto`` uses."""
        region = str(self.canvas.cget("scrollregion") or "").split()
        if len(region) == 4:
            return max(1.0, float(region[3]) - float(region[1]))
        try:
            bbox = self.canvas.bbox(self._window)
        except tk.TclError:
            bbox = None
        if bbox:
            return max(1.0, float(bbox[3] - bbox[1]))
        try:
            return float(max(1, self.content.winfo_reqheight()))
        except tk.TclError:
            return 1.0

    def _row_y_in_content(self, widget: tk.Widget) -> Tuple[int, int]:
        """Return ``(top, bottom)`` of the widget's grid row relative to content.

        Includes the label and formula on the same row so click-to-focus does
        not park the entry at the top while clipping the field name above it.
        """
        try:
            top = int(widget.winfo_rooty())
            bottom = top + max(1, int(widget.winfo_height()))
        except tk.TclError:
            return 0, 1
        parent = getattr(widget, "master", None)
        try:
            info = widget.grid_info() or {}
        except tk.TclError:
            info = {}
        if info and parent is not None:
            try:
                row = int(float(info.get("row", 0)))
                for sibling in parent.grid_slaves(row=row):
                    try:
                        if not sibling.winfo_ismapped():
                            continue
                        sibling_top = int(sibling.winfo_rooty())
                        sibling_bottom = sibling_top + max(1, int(sibling.winfo_height()))
                    except tk.TclError:
                        continue
                    top = min(top, sibling_top)
                    bottom = max(bottom, sibling_bottom)
            except tk.TclError:
                pass
        try:
            origin = int(self.content.winfo_rooty())
        except tk.TclError:
            origin = 0
        return top - origin, bottom - origin

    def scroll_to_widget(self, widget: tk.Widget) -> None:
        """Place a descendant (and its grid row) inside the visible canvas.

        ``yview_moveto`` takes a fraction of the *scrollregion* height, not of
        the movable range ``content - viewport``. Dividing by the movable range
        over-scrolls and can leave the focused field above the viewport.
        """
        try:
            self.update_idletasks()
        except tk.TclError:
            return
        try:
            if not widget.winfo_exists():
                return
        except tk.TclError:
            return
        # Layout may still be queued from a just-selected notebook tab.
        if self._layout_job is not None:
            try:
                self.after_cancel(self._layout_job)
            except tk.TclError:
                pass
            self._layout_job = None
        self._sync_layout()
        try:
            self.update_idletasks()
        except tk.TclError:
            return

        viewport_height = max(1, int(self.canvas.winfo_height()))
        content_height = self._scrollregion_height()
        row_top, row_bottom = self._row_y_in_content(widget)
        padding = 8
        # Prefer the row near the top; never scroll so far the row sits above
        # the canvas, and never past the last valid top position.
        desired_top = float(row_top - padding)
        max_top = max(0.0, content_height - float(viewport_height))
        desired_top = max(0.0, min(desired_top, max_top))
        # If the row is taller than the viewport, keep the top visible.
        if row_bottom - row_top > viewport_height:
            desired_top = max(0.0, min(float(row_top), max_top))
        self.canvas.yview_moveto(desired_top / content_height)

    def destroy(self) -> None:
        if self._layout_job is not None:
            try:
                self.after_cancel(self._layout_job)
            except tk.TclError:
                pass
            self._layout_job = None
        super().destroy()


class GeometryDiagram(tk.Canvas):
    COLORS = {
        "P": "#d94a4a", "N": "#3478c6", "pad": "#e7b54a",
        "body": "#9aa5b1", "bridge": "#7d57c2", "trace": "#64748b",
        "normal": "#475569", "warning": "#d97706", "error": "#dc2626",
        "active": "#0f766e",
    }
    LOCAL_TRACE_CATEGORIES = {
        "trace_in", "trace_out",
    }
    CORE_DIMENSION_PATHS = {"capacitor.pad_pitch", "capacitor.cap_pitch"}

    def __init__(self, master: tk.Misc, *,
                 on_dimension_click: Optional[Callable[[str], None]] = None,
                 **kwargs: Any) -> None:
        super().__init__(master, background="white", highlightthickness=1,
                         highlightbackground="#cbd5e1", **kwargs)
        self._last: Optional[Tuple[Mapping[str, Any], Any, str, Sequence[EditorIssue]]] = None
        self._on_dimension_click = on_dimension_click
        self._dimension_categories = {"body": False, "pad": False, "layout": False, "bridge": False}
        self._show_dimensions = True
        self._zoom = 1.0
        self._pan = (0.0, 0.0)
        self._drag_origin: Optional[Tuple[float, float]] = None
        self._item_styles: Dict[int, Dict[str, Any]] = {}
        self._dimension_paths: Dict[int, str] = {}
        self._dimension_structures: Dict[str, Tuple[str, ...]] = {}
        self._transform: Optional[Tuple[float, float, float]] = None
        self._requested_height = 0
        self.bind("<Configure>", lambda _e: self.winfo_toplevel().after_idle(self._redraw_last))
        self.bind("<MouseWheel>", self._mousewheel)
        self.bind("<ButtonPress-2>", self._pan_start)
        self.bind("<B2-Motion>", self._pan_move)
        self.bind("<ButtonRelease-2>", self._pan_end)
        self.bind("<ButtonPress-3>", self._pan_start)
        self.bind("<B3-Motion>", self._pan_move)
        self.bind("<ButtonRelease-3>", self._pan_end)

    def show(self, config: Mapping[str, Any], geometry: Any, unit: str,
             issues: Sequence[EditorIssue] = ()) -> None:
        self._last = (config, geometry, unit, tuple(issues))
        self._draw(config, geometry, unit, issues)

    def show_error(self, message: str) -> None:
        self._last = None
        self.delete("all")
        viewport = canvas_viewport(self)
        if viewport is None:
            return
        width, viewport_height = viewport
        height = viewport_height
        self.create_text(width / 2, height / 2,
                         text="缺少有效的依赖参数，几何与尺寸暂不显示\n" + message,
                         fill="#b42318", justify="center", width=max(1, width-24),
                         tags=("hud-label",))

    def set_dimension_filters(self, visible: bool, categories: Mapping[str, bool]) -> None:
        self._show_dimensions = bool(visible)
        self._dimension_categories.update({key: bool(value) for key, value in categories.items()})
        self._redraw_last()

    def zoom_in(self) -> None:
        self._set_zoom(self._zoom * 1.2)

    def zoom_out(self) -> None:
        self._set_zoom(self._zoom / 1.2)

    def reset_view(self) -> None:
        self._zoom = 1.0
        self._pan = (0.0, 0.0)
        self._redraw_last()

    def _set_zoom(self, value: float) -> None:
        self._zoom = max(0.45, min(5.0, value))
        self._redraw_last()

    def _mousewheel(self, event: tk.Event) -> str:
        self._set_zoom(self._zoom * (1.12 if event.delta > 0 else 1 / 1.12))
        return "break"

    def _pan_start(self, event: tk.Event) -> None:
        self._drag_origin = (float(event.x), float(event.y))
        self.configure(cursor="fleur")

    def _pan_move(self, event: tk.Event) -> None:
        if self._drag_origin is None:
            return
        dx, dy = float(event.x) - self._drag_origin[0], float(event.y) - self._drag_origin[1]
        self._pan = (self._pan[0] + dx, self._pan[1] + dy)
        self._drag_origin = (float(event.x), float(event.y))
        self._redraw_last()

    def _pan_end(self, _event: tk.Event) -> None:
        self._drag_origin = None
        self.configure(cursor="")

    def _redraw_last(self) -> None:
        if self._last:
            self._draw(*self._last)

    @staticmethod
    def _issue_levels(issues: Sequence[EditorIssue], geometry: Any) -> Dict[str, str]:
        priority = {"normal": 0, "warning": 1, "error": 2}
        result: Dict[str, str] = {}
        for issue in issues:
            if issue.path and priority.get(issue.level, 0) >= priority.get(result.get(issue.path, "normal"), 0):
                result[issue.path] = issue.level
        # Geometric overlap messages have no config path; map them to the
        # dimensions a user can actually edit.
        if any("焊盘" in message and ("重叠" in message or "接触" in message) for message in geometry.errors):
            result["capacitor.pad_gap"] = "error"
            result["capacitor.pad_pitch"] = "error"
        if any("正负" in message and ("相交" in message or "接触" in message) for message in geometry.errors):
            result["capacitor.cap_pitch"] = "error"
        values = {spec.path: spec.value for spec in build_dimension_specs(geometry)}
        if "capacitor.bridge_length" in values:
            bridge_margin = values["capacitor.bridge_length"] - values["capacitor.pad_gap"]
            if (bridge_margin >= -1e-9
                    and bridge_margin <= max(0.01, values["capacitor.pad_gap"] * .10)
                    and result.get("capacitor.bridge_length") != "error"):
                result["capacitor.bridge_length"] = "warning"
        return result

    def _draw(self, config: Mapping[str, Any], geometry: Any, unit: str,
              issues: Sequence[EditorIssue]) -> None:
        viewport = canvas_viewport(self)
        if viewport is None:
            return
        self.delete("all")
        self._item_styles.clear()
        self._dimension_paths.clear()
        self._dimension_structures.clear()
        width, viewport_height = viewport
        height = viewport_height
        pad = 12
        title_font = ("TkDefaultFont", 11, "bold")
        summary_title_font = ("TkDefaultFont", 10, "bold")
        summary_font = ("TkFixedFont", 8)
        dimension_font = ("TkDefaultFont", 8)
        title_line = font_linespace(self, title_font)
        summary_title_line = font_linespace(self, summary_title_font)
        summary_line = font_linespace(self, summary_font)
        help_line = font_linespace(self, ("TkDefaultFont", 8))
        specs = build_dimension_specs(geometry)
        levels = self._issue_levels(issues, geometry)
        summary_values = []
        summary_metric = resolve_font(self, summary_font)
        for spec in specs:
            value = (f"{format_number(spec.value, 3)}°" if spec.is_angle else
                     f"{format_length(spec.value, unit)} {unit}")
            summary_values.append((spec, f"{spec.label} = {value}"))
        maximum_summary_width = max((summary_metric.measure(text) for _spec, text in summary_values), default=0)
        side_summary = width >= max(720, maximum_summary_width + 400)
        summary_row_height = summary_line + 6
        if side_summary:
            panel_width = min(width - 360, max(230, maximum_summary_width + 30))
            plot_right = width - panel_width
            plot_bottom = height - help_line - pad * 2
            summary_top = pad
            summary_columns = 1
            summary_content_height = (
                pad + summary_title_line + 6
                + len(summary_values) * summary_row_height
                + help_line * 3 + pad * 2
            )
            required_height = max(380, summary_content_height)
            height = max(viewport_height, required_height)
            self._requested_height = required_height
            plot_bottom = height - help_line - pad * 2
        else:
            panel_width = 0
            plot_right = width
            summary_columns = 2 if width >= maximum_summary_width * 2 + pad * 4 else 1
            summary_rows = int(math.ceil(len(summary_values) / max(1, summary_columns)))
            summary_height = summary_title_line + 6 + summary_rows * summary_row_height + help_line * 3 + pad * 2
            required_height = max(380, title_line + 220 + summary_height + help_line + pad * 3)
            height = max(viewport_height, required_height)
            self._requested_height = required_height
            summary_top = height - summary_height
            plot_bottom = summary_top - pad
        self.configure(scrollregion=(0, 0, width, height))
        plot_top = pad + title_line + 10
        if plot_right - pad * 2 < 100 or plot_bottom - plot_top < 90:
            self.create_text(width / 2, height / 2, text="视口过小，请拖动窗格扩大几何预览",
                             fill="#9a3412", width=max(1, width-pad*2), tags=("hud-label",))
            return
        placer = CanvasLabelPlacer(self, width, height, safe=pad)
        placer.reserve((0, 0, plot_right, plot_top))
        placer.reserve((0, plot_bottom, plot_right, height))
        if side_summary:
            placer.reserve((plot_right, 0, width, height))
        else:
            placer.reserve((0, summary_top, width, height))
        cap = config["capacitor"]
        local_traces = [
            shape for signal in ("P", "N") for shape in geometry.signal_shapes[signal]
            if shape.category in self.LOCAL_TRACE_CATEGORIES
        ]
        points = [point for shape in local_traces for point in shape.points]
        points += [point for shape in geometry.pad_shapes.values() for point in shape.points]
        points += [point for shape in geometry.bridge_shapes.values() for point in shape.points]
        points += [point for outline in geometry.body_outlines.values() for point in outline]
        if not points:
            self.show_error("几何内核未返回电容局部结构。")
            return
        x_min, x_max = min(p.x for p in points), max(p.x for p in points)
        y_min, y_max = min(p.y for p in points), max(p.y for p in points)
        margin_x = max((x_max - x_min) * .10, 0.12)
        margin_y = max((y_max - y_min) * .18, 0.12)
        base_scale = min(max(1, plot_right - 66) / max(x_max - x_min + 2 * margin_x, 1e-9),
                         max(1, plot_bottom - plot_top - 20) / max(y_max - y_min + 2 * margin_y, 1e-9))
        scale = base_scale * self._zoom
        center_x, center_y = (x_min + x_max) / 2.0, (y_min + y_max) / 2.0
        ox = plot_right / 2.0 - center_x * scale + self._pan[0]
        oy = (plot_top + plot_bottom) / 2.0 + center_y * scale + self._pan[1]
        self._transform = (ox, oy, scale)

        def xy(x: float, y: float) -> Tuple[float, float]:
            return ox + x * scale, oy - y * scale

        for shape in local_traces:
            coords = [coord for point in shape.points for coord in xy(point.x, point.y)]
            color = self.COLORS.get(shape.signal or "", self.COLORS["trace"])
            item = self.create_polygon(coords, fill=color, outline=color, width=1,
                                       tags=("structure", "structure:trace"))
            self._remember_style(item)
        for shape in geometry.pad_shapes.values():
            coords = [coord for point in shape.points for coord in xy(point.x, point.y)]
            item = self.create_polygon(coords, fill=self.COLORS["pad"], outline="#7c5b17", width=2,
                                       tags=("structure", "structure:pad"))
            self._remember_style(item)
        for shape in geometry.bridge_shapes.values():
            coords = [coord for point in shape.points for coord in xy(point.x, point.y)]
            item = self.create_polygon(coords, fill=self.COLORS["bridge"], outline="#49327a", width=1,
                                       stipple="gray50", tags=("structure", "structure:bridge"))
            self._remember_style(item)
        if cap.get("show_body_outline_in_preview", True):
            for outline in geometry.body_outlines.values():
                coords = [coord for point in outline for coord in xy(point.x, point.y)]
                item = self.create_polygon(coords, fill="", outline=self.COLORS["body"], width=2,
                                           dash=(5, 3), tags=("structure", "structure:body"))
                self._remember_style(item)

        # Core pitch dimensions own the first annotation tracks.  Object and
        # axis labels are laid out afterwards so pad_pitch/cap_pitch can never
        # disappear merely because a lower-priority P/N marker was placed first.
        if self._show_dimensions:
            for lane, spec in enumerate(specs):
                if (spec.path in self.CORE_DIMENSION_PATHS
                        or self._dimension_categories.get(spec.category, False)):
                    self._draw_dimension_spec(spec, xy, levels.get(spec.path, "normal"),
                                              placer, lane)

        for signal, name in (("P", "C_P"), ("N", "C_N")):
            center = geometry.capacitor_centers[name]
            sx, sy = xy(center.x, center.y)
            direction = -1 if signal == "P" else 1
            label_font = ("TkDefaultFont", 9, "bold")
            label_track = font_linespace(self, label_font) + 8
            label_width = resolve_font(self, label_font).measure(signal) + label_track
            candidates = []
            for distance in (1, 2, 3):
                vertical = direction * label_track * distance
                candidates.extend((
                    (sx, sy + vertical),
                    (sx + label_width * distance, sy + direction * label_track),
                    (sx - label_width * distance, sy + direction * label_track),
                    (sx + label_width * distance, sy),
                    (sx - label_width * distance, sy),
                ))
            placer.place(
                signal,
                candidates,
                font=label_font, fill=self.COLORS[signal],
                tags=("object-label", f"signal-{signal}"), leader=(sx, sy),
                leader_color=self.COLORS[signal], priority=3,
            )

        origin_x, origin_y = xy(0, 0)
        self.create_line(origin_x, origin_y, origin_x + 34, origin_y, arrow="last", fill="#334155")
        self.create_line(origin_x, origin_y, origin_x, origin_y - 28, arrow="last", fill="#334155")
        axis_track = font_linespace(self, ("TkDefaultFont", 9)) + 8
        placer.place(
            "X",
            ((origin_x + 42, origin_y),
             (origin_x + 42, origin_y + axis_track),
             (origin_x + 42, origin_y - axis_track),
             (origin_x + 42 + axis_track * 2, origin_y)),
            fill="#334155", tags=("axis-label",), leader=(origin_x + 34, origin_y),
            leader_color="#334155", priority=3,
        )
        placer.place(
            "Y",
            ((origin_x, origin_y - 36),
             (origin_x - axis_track, origin_y - 36),
             (origin_x + axis_track, origin_y - 36),
             (origin_x, origin_y - 36 - axis_track * 2)),
            fill="#334155", tags=("axis-label",), leader=(origin_x, origin_y - 28),
            leader_color="#334155", priority=3,
        )
        self.create_oval(origin_x - 2, origin_y - 2, origin_x + 2, origin_y + 2, fill="#111827")

        self.create_text(14, 12, anchor="nw", text="实时局部几何 · 与 HFSS/模型预览共用计算结果",
                         font=title_font, fill="#172033", width=max(1, plot_right-28),
                         tags=("hud-label", "diagram-title"))
        if side_summary:
            self.create_line(plot_right, 8, plot_right, height - 8, fill="#e2e8f0")
            lx = plot_right + 14
            summary_title_y = pad
            column_width = panel_width
        else:
            self.create_line(pad, summary_top, width-pad, summary_top, fill="#e2e8f0")
            lx = pad
            summary_title_y = summary_top + pad
            column_width = (width-pad*2) / summary_columns
        self.create_text(lx, summary_title_y, anchor="nw", text="实际几何尺寸",
                         font=summary_title_font, fill="#172033",
                         tags=("dimension-summary-label", "summary-title"))
        values_top = summary_title_y + summary_title_line + 6
        for index, (spec, text) in enumerate(summary_values):
            level = levels.get(spec.path, "normal")
            color = self.COLORS[level]
            if side_summary:
                column, row = 0, index
            else:
                column, row = index % summary_columns, index // summary_columns
            item = self.create_text(lx + column * column_width, values_top + row * summary_row_height,
                                    anchor="nw", text=text, fill=color,
                                    font=summary_font,
                                    tags=("dimension-value", "dimension-summary-label"))
            self._register_dimension_item(item, spec, color)
        rows_used = len(summary_values) if side_summary else int(math.ceil(len(summary_values) / summary_columns))
        help_y = values_top + rows_used * summary_row_height + 3
        self.create_text(lx, help_y, anchor="nw",
                         text="pad_pitch：同一排相邻焊盘横向中心距\ncap_pitch：P、N 两排电容纵向中心距\n两者彼此独立",
                         fill="#475569", font=("TkDefaultFont", 8), justify="left",
                         width=max(1, (panel_width-28 if side_summary else width-pad*2)),
                         tags=("dimension-summary-label", "help-label"))
        footer_text = ellipsize(
            self, f"滚轮缩放 · 中键/右键拖动平移 · 当前 {self._zoom:.2f}×",
            ("TkDefaultFont", 8), max(1, plot_right - 28),
        )
        self.create_text(14, plot_bottom + help_line + 2 if not side_summary else height - 13, anchor="sw",
                         text=footer_text, fill="#64748b", font=("TkDefaultFont", 8),
                         tags=("hud-label", "footer-label"))
        content_box = self.bbox("all")
        content_height = max(height, (content_box[3] if content_box else height) + pad)
        self.configure(scrollregion=(0, 0, width, content_height))

    def _draw_dimension_spec(self, spec: DimensionSpec,
                             xy: Callable[[float, float], Tuple[float, float]],
                             level: str, placer: CanvasLabelPlacer, lane: int) -> None:
        first, second = xy(*spec.first), xy(*spec.second)
        dx, dy = second[0] - first[0], second[1] - first[1]
        length = math.hypot(dx, dy)
        track = font_linespace(self, ("TkDefaultFont", 8)) + 5
        sign = -1.0 if spec.offset < 0 else 1.0
        core_lane = {"capacitor.pad_pitch": 1, "capacitor.cap_pitch": 4}.get(spec.path)
        lane_index = core_lane if core_lane is not None else lane % 4
        distance = max(abs(spec.offset), track) + lane_index * track
        if length < 1e-6:
            normal = (0.0, -sign * distance)
        else:
            normal = (-dy / length * sign * distance, dx / length * sign * distance)
        a = (first[0] + normal[0], first[1] + normal[1])
        b = (second[0] + normal[0], second[1] + normal[1])
        color = self.COLORS.get(level, self.COLORS["normal"])
        ext1 = self.create_line(*first, *a, fill="#94a3b8", dash=(2, 2), tags=("dimension-line",))
        ext2 = self.create_line(*second, *b, fill="#94a3b8", dash=(2, 2), tags=("dimension-line",))
        line = self.create_line(*a, *b, arrow="both" if length >= 8 else "last",
                                fill=color, width=1, tags=("dimension-line",))
        midpoint = ((a[0]+b[0])/2, (a[1]+b[1])/2)
        tangent = (dx / length, dy / length) if length >= 1e-6 else (1.0, 0.0)
        label = placer.place(
            spec.label,
            ((midpoint[0] + normal[0] * .12, midpoint[1] + normal[1] * .12),
             (midpoint[0] + tangent[0] * track * 2, midpoint[1] + tangent[1] * track * 2),
             (midpoint[0] - tangent[0] * track * 2, midpoint[1] - tangent[1] * track * 2)),
            font=("TkDefaultFont", 8), fill=color,
            tags=("dimension-label", f"dimension:{spec.path}"),
            leader=midpoint, leader_color=color, priority=2,
        )
        for item in (ext1, ext2, line):
            self._register_dimension_item(item, spec, color)
        if label is not None:
            self._register_dimension_item(label, spec, color)

    def _remember_style(self, item: int) -> None:
        kind = self.type(item)
        if kind == "polygon":
            self._item_styles[item] = {"outline": self.itemcget(item, "outline"),
                                       "width": self.itemcget(item, "width")}
        elif kind == "line":
            self._item_styles[item] = {"fill": self.itemcget(item, "fill"),
                                       "width": self.itemcget(item, "width")}

    def _register_dimension_item(self, item: int, spec: DimensionSpec, color: str) -> None:
        self._dimension_paths[item] = spec.path
        self._dimension_structures[spec.path] = spec.structures
        kind = self.type(item)
        style: Dict[str, Any] = {"fill": color}
        if kind == "line":
            style["width"] = self.itemcget(item, "width")
        self._item_styles[item] = style
        self.tag_bind(item, "<Enter>", lambda _e, path=spec.path: self._highlight_dimension(path, True))
        self.tag_bind(item, "<Leave>", lambda _e, path=spec.path: self._highlight_dimension(path, False))
        self.tag_bind(item, "<Button-1>", lambda _e, path=spec.path: self._activate_dimension(path))

    def _highlight_dimension(self, path: str, active: bool) -> None:
        self.configure(cursor="hand2" if active else "")
        for item, item_path in self._dimension_paths.items():
            if item_path != path:
                continue
            base = self._item_styles.get(item, {})
            if self.type(item) == "line":
                self.itemconfigure(item, fill=self.COLORS["active"] if active else base.get("fill", ""),
                                   width=3 if active else base.get("width", 1))
            else:
                self.itemconfigure(item, fill=self.COLORS["active"] if active else base.get("fill", ""))
        for structure in self._dimension_structures.get(path, ()):
            for item in self.find_withtag(f"structure:{structure}"):
                base = self._item_styles.get(item, {})
                if active:
                    self.itemconfigure(item, outline=self.COLORS["active"], width=3)
                else:
                    self.itemconfigure(item, **base)

    def _activate_dimension(self, path: str) -> None:
        if self._on_dimension_click:
            self._on_dimension_click(path)


class ElectricalModelDiagram(tk.Canvas):
    def __init__(self, master: tk.Misc, **kwargs: Any) -> None:
        kwargs.pop("height", None)
        super().__init__(master, background="#fbfcfe", highlightthickness=1,
                         highlightbackground="#cbd5e1", height=138, **kwargs)
        self._last: Optional[Tuple[Mapping[str, Any], Optional[TouchstoneInfo]]] = None
        self._redraw_job: Optional[str] = None
        self.bind(
            "<Configure>",
            lambda _event: debounce_after(self, "_redraw_job", self._redraw, delay_ms=18),
        )

    def show(self, config: Mapping[str, Any], touchstone: Optional[TouchstoneInfo] = None) -> None:
        self._last = (config, touchstone)
        self._draw(config, touchstone)

    def _redraw(self) -> None:
        if self._last:
            self._draw(*self._last)

    def refresh_layout(self) -> None:
        """Recompute height and drawing after named-font or DPI changes."""

        debounce_after(self, "_redraw_job", self._redraw, delay_ms=0)

    def _draw(self, config: Mapping[str, Any], touchstone: Optional[TouchstoneInfo]) -> None:
        viewport = canvas_viewport(self)
        if viewport is None:
            return
        self.delete("all")
        w, h = viewport
        if w < 240:
            self.create_text(
                w / 2, h / 2, text="HFSS 电气模型视口过窄",
                fill="#9a3412", font="ViaCanvasFont", width=max(1, w - 16),
                tags=("hud-label", "viewport-message"),
            )
            return

        model = config["capacitor_model"]
        mode = model["mode"]
        if mode == "ideal_bridge":
            title = "理想短路（不模拟电容）"
            detail = f"宽 {format_length(config['capacitor']['bridge_width'], config['units'])} {config['units']}  ·  长 {format_length(config['capacitor']['bridge_length'], config['units'])} {config['units']}"
        elif mode == "lumped_rlc":
            srf = calculate_srf_ghz(float(model["capacitance_pf"]), float(model["esl_nh"]))
            title = "集总串联 R—L—C"
            detail = (f"C={format_number(model['capacitance_pf'], 6)} pF  ·  ESR={format_number(model['esr_ohm'], 6)} Ω  ·  "
                      f"ESL={format_number(model['esl_nh'], 6)} nH  ·  fSRF≈{format_number(srf, 4) if srf else '不可计算'} GHz")
        elif mode == "sparameter":
            title = "Touchstone S 参数模型"
            detail = (f"{touchstone.ports} 端口 · {touchstone.frequency_summary}" if touchstone else "等待有效的公共模型文件")
        else:
            title = "开路（仅焊盘）"
            detail = "焊盘间隙无铜桥、无RLC、无Touchstone组件"

        title_font = "ViaPreviewTitleFont"
        body_font = "ViaCanvasFont"
        small_font = "ViaSmallFont"
        card_title_font = "ViaCardTitleFont"
        title_metric = resolve_font(self, title_font)
        body_metric = resolve_font(self, body_font)
        small_metric = resolve_font(self, small_font)
        card_title_metric = resolve_font(self, card_title_font)
        title_line = font_linespace(self, title_font)
        body_line = font_linespace(self, body_font)
        small_line = font_linespace(self, small_font)
        card_title_line = font_linespace(self, card_title_font)
        pad = max(8, round(small_line * .6))
        usable = max(1, w - pad * 2)
        note = "P、N 两颗电容使用相同模型"
        header_gap = max(12, small_metric.measure(" · "))
        side_by_side = (
            title_metric.measure(title) + small_metric.measure(note) + header_gap <= usable
        )
        if side_by_side:
            title_item = self.create_text(
                pad, pad, anchor="nw", text=title, font=title_font,
                fill="#172033", tags=("hud-label", "model-title"),
            )
            note_item = self.create_text(
                w - pad, pad + max(0, (title_line - small_line) / 2), anchor="ne",
                text=note, font=small_font, fill="#7c3aed",
                tags=("hud-label", "model-note"),
            )
        else:
            title_item = self.create_text(
                pad, pad, anchor="nw", text=title, font=title_font,
                fill="#172033", width=usable, tags=("hud-label", "model-title"),
            )
            title_box = self.bbox(title_item) or (pad, pad, w - pad, pad + title_line)
            note_item = self.create_text(
                pad, title_box[3] + 2, anchor="nw", text=note, font=small_font,
                fill="#7c3aed", width=usable, tags=("hud-label", "model-note"),
            )
        header_bottom = max(
            (self.bbox(item) or (0, 0, 0, pad + title_line))[3]
            for item in (title_item, note_item)
        )

        maximum_plot_width = max(
            420,
            body_metric.measure("焊盘 A  R / ESR  L / ESL  C  焊盘 B") + body_line * 5,
        )
        initial_plot_width = min(usable, maximum_plot_width)
        initial_terminal_width = max(
            24, min(round(small_line * 2.1), round(initial_plot_width * .14)),
        )
        plot_margin = max(
            pad,
            math.ceil(small_metric.measure("焊盘 B") / 2 + 4 - initial_terminal_width / 2),
        )
        plot_width = min(max(1, w - plot_margin * 2), maximum_plot_width)
        plot_left = (w - plot_width) / 2
        plot_right = plot_left + plot_width
        terminal_width = max(24, min(round(small_line * 2.1), round(plot_width * .14)))
        pad_half = max(13, round(body_line * .72))
        node_left = plot_left + terminal_width
        node_right = plot_right - terminal_width
        circuit_span = max(1.0, node_right - node_left)
        element_half = pad_half
        sparam_rows = 1
        sparam_title_rows = 1
        sparam_lead = 0.0
        sparam_caption = "2/4 端口网络 · 两端口映射"
        rlc_label_rows = (0, 0, 0)
        if mode == "sparameter":
            sparam_lead = min(max(8.0, body_line * .45), circuit_span * .16)
            block_width = max(1, circuit_span - sparam_lead * 2 - 10)
            sparam_title_rows = max(
                1, math.ceil(card_title_metric.measure("Touchstone") / block_width),
            )
            sparam_rows = max(1, math.ceil(
                small_metric.measure(sparam_caption) / block_width
            ))
            element_half = max(
                pad_half,
                round((sparam_title_rows * card_title_line + sparam_rows * small_line + 10) / 2),
            )
        elif mode == "lumped_rlc":
            adjacent_distance = circuit_span * .28
            adjacent_overlap = adjacent_distance < max(
                (body_metric.measure("R / ESR") + body_metric.measure("L / ESL")) / 2 + 6,
                (body_metric.measure("L / ESL") + body_metric.measure("C")) / 2 + 6,
            )
            outside_overlap = circuit_span * .56 < (
                body_metric.measure("R / ESR") + body_metric.measure("C")
            ) / 2 + 6
            if outside_overlap:
                rlc_label_rows = (0, 1, 2)
            elif adjacent_overlap:
                rlc_label_rows = (0, 1, 0)
        rlc_row_count = max(rlc_label_rows, default=0) + 1
        label_track = (
            body_line * rlc_row_count + 3 * max(0, rlc_row_count - 1) + 5
            if mode == "lumped_rlc"
            else body_line + 5 if mode == "open"
            else 0
        )
        cy = header_bottom + 7 + label_track + element_half

        self.create_rectangle(
            plot_left, cy - pad_half, node_left, cy + pad_half,
            fill="#e7b54a", outline="#7c5b17", tags=("circuit", "terminal"),
        )
        self.create_rectangle(
            node_right, cy - pad_half, plot_right, cy + pad_half,
            fill="#e7b54a", outline="#7c5b17", tags=("circuit", "terminal"),
        )
        pad_label_y = cy + max(pad_half, element_half) + 4 + small_line / 2
        self.create_text(
            (plot_left + node_left) / 2, pad_label_y, text="焊盘 A", font=small_font,
            fill="#475569", tags=("hud-label", "pad-label"),
        )
        self.create_text(
            (node_right + plot_right) / 2, pad_label_y, text="焊盘 B", font=small_font,
            fill="#475569", tags=("hud-label", "pad-label"),
        )

        if mode == "ideal_bridge":
            self.create_line(
                node_left, cy, node_right, cy, fill="#7d57c2",
                width=max(5, round(body_line * .42)), tags=("circuit", "ideal-bridge"),
            )
        elif mode == "lumped_rlc":
            centers = (
                node_left + circuit_span * .22,
                node_left + circuit_span * .50,
                node_left + circuit_span * .78,
            )
            component_scale = min(1.0, max(.62, circuit_span / 300.0))
            resistor_width = 46 * component_scale
            inductor_width = 50 * component_scale
            capacitor_gap = max(8, 12 * component_scale)
            resistor_left, resistor_right = centers[0] - resistor_width / 2, centers[0] + resistor_width / 2
            inductor_left, inductor_right = centers[1] - inductor_width / 2, centers[1] + inductor_width / 2
            cap_left, cap_right = centers[2] - capacitor_gap / 2, centers[2] + capacitor_gap / 2
            self.create_line(node_left, cy, resistor_left, cy, width=2, tags=("circuit",))
            zigzag: List[float] = [resistor_left, cy]
            steps = 8
            for index in range(1, steps):
                zigzag.extend((
                    resistor_left + resistor_width * index / steps,
                    cy + (-8 if index % 2 else 8) * component_scale,
                ))
            zigzag.extend((resistor_right, cy))
            self.create_line(*zigzag, fill="#b45309", width=2, tags=("circuit", "resistor"))
            self.create_line(resistor_right, cy, inductor_left, cy, width=2, tags=("circuit",))
            loop_width = inductor_width / 3
            for index in range(3):
                x1 = inductor_left + index * loop_width
                self.create_arc(
                    x1, cy - 9 * component_scale, x1 + loop_width + 2, cy + 9 * component_scale,
                    start=0, extent=180, style="arc", width=2, outline="#2563eb",
                    tags=("circuit", "inductor"),
                )
            self.create_line(inductor_right, cy, cap_left, cy, width=2, tags=("circuit",))
            plate_half = max(9, round(body_line * .62))
            self.create_line(
                cap_left, cy - plate_half, cap_left, cy + plate_half,
                width=3, fill="#059669", tags=("circuit", "capacitor"),
            )
            self.create_line(
                cap_right, cy - plate_half, cap_right, cy + plate_half,
                width=3, fill="#059669", tags=("circuit", "capacitor"),
            )
            self.create_line(cap_right, cy, node_right, cy, width=2, tags=("circuit",))
            label_y = cy - element_half - body_line / 2 - 4
            for x, text, color, row in (
                (centers[0], "R / ESR", "#b45309", rlc_label_rows[0]),
                (centers[1], "L / ESL", "#2563eb", rlc_label_rows[1]),
                (centers[2], "C", "#059669", rlc_label_rows[2]),
            ):
                self.create_text(
                    x, label_y - row * (body_line + 3), text=text,
                    font=body_font, fill=color,
                    tags=("hud-label", "component-label"),
                )
        elif mode == "sparameter":
            block_left, block_right = node_left + sparam_lead, node_right - sparam_lead
            self.create_line(node_left, cy, block_left, cy, width=2, tags=("circuit",))
            self.create_rectangle(
                block_left, cy - element_half, block_right, cy + element_half,
                fill="#dbeafe", outline="#2563eb", width=2,
                tags=("circuit", "touchstone-block"),
            )
            inner_width = max(1, block_right - block_left - 10)
            text_top = cy - (
                sparam_title_rows * card_title_line + sparam_rows * small_line + 2
            ) / 2
            touchstone_title = self.create_text(
                (block_left + block_right) / 2, text_top, anchor="n", text="Touchstone",
                font=card_title_font, fill="#1d4ed8", width=inner_width,
                tags=("hud-label", "touchstone-label"),
            )
            touchstone_box = self.bbox(touchstone_title) or (
                block_left, text_top, block_right, text_top + sparam_title_rows * card_title_line,
            )
            self.create_text(
                (block_left + block_right) / 2, touchstone_box[3] + 2, anchor="n",
                text=sparam_caption, font=small_font,
                fill="#1e40af", width=inner_width,
                tags=("hud-label", "touchstone-label"),
            )
            self.create_line(block_right, cy, node_right, cy, width=2, tags=("circuit",))
        else:
            gap_half = max(16, round(body_line * 1.3))
            midpoint = (node_left + node_right) / 2
            self.create_line(node_left, cy, midpoint - gap_half, cy, width=2, tags=("circuit",))
            self.create_line(midpoint + gap_half, cy, node_right, cy, width=2, tags=("circuit",))
            self.create_text(
                midpoint, cy - element_half - body_line / 2 - 4, text="OPEN",
                font=body_font, fill="#9a3412", tags=("hud-label", "component-label"),
            )

        content_bottom = pad_label_y + small_line / 2
        for tag in ("component-label", "touchstone-label"):
            tagged_box = self.bbox(tag)
            if tagged_box:
                content_bottom = max(content_bottom, tagged_box[3])
        summary_y = content_bottom + 7
        summary_item = self.create_text(
            w / 2, summary_y, anchor="n", text=detail, fill="#334155",
            font=small_font, width=usable, justify="center",
            tags=("hud-label", "model-summary"),
        )
        summary_box = self.bbox(summary_item) or (pad, summary_y, w - pad, summary_y + small_line)
        metric_minimum = max(
            130, title_line + body_line * 2 + small_line * 2 + pad * 2,
        )
        requested_height = max(metric_minimum, int(summary_box[3] + pad))
        current_request = int(float(self.cget("height")))
        if abs(current_request - requested_height) > 1:
            self.configure(height=requested_height)
