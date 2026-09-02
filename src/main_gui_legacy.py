"""PCIe AC耦合电容区域无源优化参数化建模工具 V2 Tkinter GUI。"""
from __future__ import annotations

import copy
import json
import math
import os
import runpy
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from config_manager import (
    AEDT_VERSION_LABELS, ConfigError, ConfigManager, DEFAULT_DISPLAY_UNIT,
    MM_PER_MIL, PACKAGE_PRESETS,
    SUPPORTED_AEDT_VERSIONS, apply_package_preset,
    convert_length, derive_capacitor_values, format_length, format_number,
    format_validation_report, via_trace_center_pitch,
)
from build_info import about_message
from capacitor_editor import (
    ENUM_LABELS, ElectricalModelDiagram, GeometryDiagram, ScrollableFrame,
    calculate_srf_ghz, inspect_touchstone, validate_capacitor_editor,
)
from geometry_calculator import GeometryCalculator
from gui_layout import CanvasLabelPlacer, canvas_viewport, ellipsize, font_linespace, text_size
from hfss_script_generator import HfssScriptGenerator, ScriptGenerationError
from scenario_semantics import (
    VIA_TRANSITION,
    VIA_TRANSITION_SWEEP_NOT_IMPLEMENTED,
    scenario_type,
)
from sweep_manager import generate_sweep_cases

IS_FROZEN = bool(getattr(sys, "frozen", False))
APP_VERSION = "2.4.0"
APP_TITLE = f"PCIe AC耦合电容区域无源优化工具 V{APP_VERSION}"

# VIA 换层页：几何尺寸参数 → 编号（方案 B 编号表，见示意图编号联动提示词 §四）。
# 编号只用于 label 文本与图上标注，不改变 path / variables / widgets 映射。
VIA_PARAM_NUMBERS: Dict[str, str] = {
    "via_transition.entry_trace.center_to_center": "①",
    "via_transition.exit_trace.center_to_center": "①",
    "via_transition.signal_via_pitch": "㉑",
    "via_transition.fanout.style": "㉒",
    "via_transition.fanout.direction": "㉓",
    "via_transition.fanout.straight_length": "㉔",
    "via_transition.route_axis": "②",
    "via_transition.entry_layer": "③",
    "via_transition.exit_layer": "④",
    "via_transition.drill_start_layer": "⑤",
    "via_transition.drill_end_layer": "⑥",
    "via_transition.entry_trace.length": "⑦",
    "via_transition.entry_trace.width": "⑧",
    "via_transition.entry_trace.gap": "输入GAP",
    "via_transition.exit_trace.length": "⑨",
    "via_transition.exit_trace.width": "⑩",
    "via_transition.exit_trace.gap": "输出GAP",
    "via_transition.padstack.drill_diameter": "⑪",
    "via_transition.padstack.finished_hole_diameter": "⑫",
    "via_transition.padstack.plating_thickness": "⑬",
    "via_transition.padstack.connected_pad_diameter": "⑭",
    "via_transition.padstack.nonfunctional_pad_diameter": "⑮",
    "via_transition.padstack.antipad.diameter": "⑯",
    "via_transition.backdrill.drill_diameter": "⑰",
    "via_transition.backdrill.residual_stub": "⑱",
    "via_transition.return_vias.pad_diameter": "⑲",
    "via_transition.return_vias.antipad_diameter": "⑳",
}


def runtime_base_directory(*, frozen: bool, executable: Path, module_file: Path) -> Path:
    """Return the persistent base used for relative user output paths."""

    if frozen:
        return Path(executable).resolve().parent
    return Path(module_file).resolve().parent


# PyInstaller extracts bundled modules and data under ``_MEI*``.  Resolving
# ``./generated_v2`` relative to ``__file__`` therefore placed real projects
# in a temporary directory that disappears when the EXE exits.  User files
# are now based on the EXE directory; source runs retain their former base.
SOURCE_DIRECTORY = runtime_base_directory(
    frozen=IS_FROZEN,
    executable=Path(sys.executable),
    module_file=Path(__file__),
)


def _get(data: Mapping[str, Any], path: str) -> Any:
    node: Any = data
    for part in path.split("."):
        node = node[part]
    return node


def _set(data: Dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node = data
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value


def _display(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.12g}"
    return str(value)


def calculate_initial_window_metrics(screen_width: int, screen_height: int) -> Tuple[int, int, int, int, int, int]:
    """Return a bounded, centered window and a safe adaptive minimum size."""

    screen_width = max(640, int(screen_width))
    screen_height = max(520, int(screen_height))
    available_width = max(640, screen_width - 80)
    available_height = max(520, screen_height - 80)
    preferred_width = max(960, int(screen_width * 0.82))
    preferred_height = max(640, int(screen_height * 0.82))
    width = min(1280, available_width, preferred_width)
    height = min(800, available_height, preferred_height)
    x = max(0, (screen_width - width) // 2)
    y = max(0, (screen_height - height) // 2)
    minimum_width = min(1120, width)
    minimum_height = min(720, height)
    return width, height, x, y, minimum_width, minimum_height


class SurfaceCapAntipadApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        width, height, x, y, minimum_width, minimum_height = calculate_initial_window_metrics(
            self.winfo_screenwidth(), self.winfo_screenheight()
        )
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(minimum_width, minimum_height)
        self.manager = ConfigManager(SOURCE_DIRECTORY)
        self.config = self.manager.default()
        self.display_unit = str(self.config["units"])
        self.variables: Dict[str, tk.Variable] = {}
        self.widgets: Dict[str, tk.Widget] = {}
        self.enum_labels: Dict[str, Dict[str, str]] = {}
        self.formula_labels: Dict[str, ttk.Label] = {}
        self.field_labels: Dict[str, ttk.Label] = {}
        self.length_paths: set[str] = set()
        self._derived_display_paths: set[str] = set()
        self._length_mm_cache: Dict[str, float] = {}
        self._length_display_snapshot: Dict[str, str] = {}
        self.numeric_paths: set[str] = set()
        self.integer_paths: set[str] = set()
        self.unit_labels: List[ttk.Label] = []
        self.sweep_vars: Dict[str, Dict[str, tk.Variable]] = {}
        self._refresh_job: Optional[str] = None
        self._ui_sync = False
        self._via_param_numbers: Dict[str, str] = dict(VIA_PARAM_NUMBERS)
        self._via_param_widgets: Dict[tk.Widget, str] = {}
        self._via_param_widget_paths: Dict[str, str] = {}
        self._via_highlight_path: Optional[str] = None
        self._via_last_render: Optional[Tuple[Dict[str, Any], Any]] = None
        self._via_highlight_sync_job: Optional[str] = None
        self._via_diagram_redraw_job: Optional[str] = None
        self._configure_editor_styles()
        self.status = tk.StringVar(value="就绪")
        self._build_menu_toolbar()
        self._build_tabs()
        self._load_to_widgets(self.config)
        self._refresh_job = self.after_idle(self.refresh_visuals)

    def _configure_editor_styles(self) -> None:
        style = ttk.Style(self)
        style.configure("Derived.TEntry", fieldbackground="#eef2f7", foreground="#334155")
        style.configure("Invalid.TEntry", fieldbackground="#fff0f0", foreground="#991b1b")
        style.configure("Warning.TEntry", fieldbackground="#fff7e6", foreground="#9a3412")
        style.configure("StatusGood.TLabel", foreground="#166534", font=("TkDefaultFont", 10, "bold"))
        style.configure("StatusWarn.TLabel", foreground="#9a3412", font=("TkDefaultFont", 10, "bold"))
        style.configure("StatusError.TLabel", foreground="#b42318", font=("TkDefaultFont", 10, "bold"))
    def _build_menu_toolbar(self) -> None:
        menu = tk.Menu(self)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="载入 JSON…", command=self.load_json)
        file_menu.add_command(label="保存 JSON…", command=self.save_json)
        file_menu.add_separator()
        file_menu.add_command(label="恢复 V2 默认值", command=self.restore_defaults)
        file_menu.add_command(label="退出", command=self.destroy)
        menu.add_cascade(label="文件", menu=file_menu)
        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label="关于", command=self._show_about)
        menu.add_cascade(label="帮助", menu=help_menu)
        self.configure(menu=menu)
        bar = ttk.Frame(self, padding=(8, 6))
        self.main_toolbar = bar
        bar.pack(fill="x")
        toolbar_actions = (
            ("载入 JSON", "载入", self.load_json),
            ("保存 JSON", "保存", self.save_json),
            ("模型检查", "模型检查", self.validate_model),
            ("生成HFSS脚本", "生成脚本", self.generate_script),
            ("生成并运行", "生成并运行", self.generate_and_run),
        )
        self.main_toolbar_buttons = []
        for full_text, compact_text, command in toolbar_actions:
            button = ttk.Button(bar, text=full_text, command=command)
            button.pack(side="left", padx=3)
            self.main_toolbar_buttons.append((button, full_text, compact_text))
        self.unit_caption = ttk.Label(bar, text="显示/保存单位：")
        self.unit_caption.pack(side="left", padx=(20, 4))
        self.unit_var = tk.StringVar(value=DEFAULT_DISPLAY_UNIT)
        self.unit_combo = ttk.Combobox(
            bar, textvariable=self.unit_var, values=("mm", "mil"), width=7, state="readonly"
        )
        self.unit_combo.pack(side="left")
        self.unit_combo.bind("<<ComboboxSelected>>", self._unit_changed)
        self.unit_hint_label = ttk.Label(
            bar, text="（切换只改变显示，物理尺寸不变）", foreground="#555"
        )
        self.unit_hint_label.pack(side="left", padx=8)
        self.bind("<Configure>", lambda _event: self._apply_main_toolbar_layout(), add="+")
        self.status_bar = ttk.Label(
            self, textvariable=self.status, relief="sunken", anchor="w", padding=(8, 3),
        )
        self.status_bar.pack(fill="x")

    def _apply_main_toolbar_layout(self) -> None:
        compact = self.winfo_width() < 1320
        for button, full_text, compact_text in self.main_toolbar_buttons:
            button.configure(text=compact_text if compact else full_text)
        self.unit_caption.configure(text="单位：" if compact else "显示/保存单位：")
        self.unit_combo.configure(width=5 if compact else 7)
        if compact:
            if self.unit_hint_label.winfo_manager():
                self.unit_hint_label.pack_forget()
        elif not self.unit_hint_label.winfo_manager():
            self.unit_hint_label.configure(text="（切换只改变显示，物理尺寸不变）")
            self.unit_hint_label.pack(side="left", padx=8, after=self.unit_combo)

    def _show_about(self) -> None:
        """Show release identity that used to appear in the title and status bar."""

        try:
            identity = about_message()
        except Exception:
            identity = f"PCIe AC耦合电容区域无源优化工具\n版本 {APP_VERSION}"
        messagebox.showinfo(
            "关于 SurfaceCapAntipadTool",
            identity,
            parent=self,
        )

    def _set_ready_status(self) -> None:
        self.status.set(f"就绪 · V{APP_VERSION} · 显示单位 {self.display_unit}")

    def _build_tabs(self) -> None:
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        names = ["项目与走线", "电容与模型", "反焊盘与叠层", "GND VIA", "VIA 换层", "仿真与扫描",
                 "模型预览", "检查结果"]
        self.tabs: Dict[str, ttk.Frame] = {}
        self.scenario_hints: Dict[str, ttk.Label] = {}
        # "反焊盘与叠层" hosts a tight vertical PanedWindow whose preview canvas
        # must keep its full height even at "large"/"ultra" font tiers; its
        # scenario state is instead conveyed by hiding the antipad settings pane
        # and by the bottom status bar, so no top hint bar is added here.
        hint_pages = {"项目与走线", "电容与模型", "GND VIA", "VIA 换层", "仿真与扫描"}
        for name in names:
            frame = ttk.Frame(self.notebook, padding=10)
            self.notebook.add(frame, text=name)
            self.tabs[name] = frame
            if name in hint_pages:
                hint = ttk.Label(frame, text="", foreground="#475569", justify="left",
                                 wraplength=1040)
                hint.pack(fill="x", padx=8, pady=(4, 0), anchor="w")
                self.scenario_hints[name] = hint
        self._build_project_trace()
        self._build_capacitor()
        self._build_antipad_stackup()
        self._build_vias()
        self._build_via_transition()
        self._build_simulation_sweeps()
        self._build_model_preview()
        self._build_validate_results()

    def _build_validate_results(self) -> None:
        """Legacy text fallback; the V2 entry point installs structured results."""
        self.log_text = tk.Text(self.tabs["检查结果"], wrap="word", font=("TkFixedFont", 10),
                                background="#101418", foreground="#e6edf3")
        self.log_text.pack(fill="both", expand=True)

    def _build_model_preview(self) -> None:
        """Legacy fallback; the V2 entry point overrides this with real VTK 3D."""
        self.preview_canvas = tk.Canvas(self.tabs["模型预览"], background="white")
        self.preview_canvas.pack(fill="both", expand=True)
        self.preview_canvas.bind("<Configure>", lambda _e: self._schedule_refresh())

    def _frame(self, tab: ttk.Frame, title: str, side: str = "top") -> ttk.LabelFrame:
        frame = ttk.LabelFrame(tab, text=title, padding=10)
        frame.pack(side=side, fill="both" if side == "left" else "x", expand=side == "left", padx=4, pady=4, anchor="n")
        frame.columnconfigure(1, weight=1)
        return frame

    def _add(self, parent: ttk.Frame, row: int, label: str, path: str, *, length: bool = False,
             numeric: bool = False, integer: bool = False, boolean: bool = False,
             values: Optional[Sequence[str]] = None, unit: str = "",
             display_values: Optional[Mapping[str, str]] = None, formula: str = "") -> tk.Widget:
        label_widget = ttk.Label(parent, text=label, justify="left", wraplength=168)
        label_widget.grid(row=row, column=0, sticky="nw", padx=4, pady=3)
        self.field_labels[path] = label_widget
        try:
            parent.columnconfigure(1, weight=1, minsize=176)
        except tk.TclError:
            pass
        var: tk.Variable = tk.BooleanVar() if boolean else tk.StringVar()
        self.variables[path] = var
        if boolean:
            widget: tk.Widget = ttk.Checkbutton(parent, variable=var)
            widget.grid(row=row, column=1, sticky="w", padx=4)
        elif values:
            labels = tuple(display_values.get(value, value) for value in values) if display_values else tuple(values)
            if display_values:
                self.enum_labels[path] = dict(display_values)
            widget = ttk.Combobox(parent, textvariable=var, values=labels, state="readonly", width=18)
            widget.grid(row=row, column=1, sticky="ew", padx=4)
            if path == "capacitor.package":
                widget.bind("<<ComboboxSelected>>", self._package_changed)
        else:
            widget = ttk.Entry(parent, textvariable=var)
            widget.grid(row=row, column=1, sticky="ew", padx=4)
        self.widgets[path] = widget
        if length:
            self.length_paths.add(path)
            unit_label = ttk.Label(parent, text=self.display_unit, width=5)
            unit_label.grid(row=row, column=2, sticky="w")
            self.unit_labels.append(unit_label)
        elif unit:
            ttk.Label(parent, text=unit).grid(row=row, column=2, sticky="w")
        if formula:
            formula_label = ttk.Label(
                parent, text=formula, foreground="#64748b", font=("TkDefaultFont", 8),
                wraplength=160, justify="left",
            )
            formula_label.grid(row=row, column=3, sticky="nw", padx=(4, 2))
            self.formula_labels[path] = formula_label
        if numeric or length:
            self.numeric_paths.add(path)
        if integer:
            self.integer_paths.add(path)
        var.trace_add("write", lambda *_: self._schedule_refresh())
        return widget

    def _register_hidden_variable(self, path: str) -> None:
        var = tk.StringVar()
        self.variables[path] = var
        var.trace_add("write", lambda *_: self._schedule_refresh())

    def _add_fixed_label(self, parent: ttk.Frame, row: int, label: str, path: str,
                         backend_value: str, display_text: str) -> None:
        """注册一个不可编辑的单选项字段：后端变量保留原值，界面显示静态说明。

        ``_collect``/``_load_to_widgets`` 仍通过 ``self.variables[path]`` 往返读写，
        只是不再向用户提供可改动的下拉控件（避免"单选项"给人可改的错觉）。
        """
        ttk.Label(parent, text=label, justify="left", wraplength=200).grid(
            row=row, column=0, sticky="nw", padx=4, pady=3)
        var = tk.StringVar(value=backend_value)
        self.variables[path] = var
        var.trace_add("write", lambda *_: self._schedule_refresh())
        value_label = ttk.Label(parent, text=display_text, foreground="#475569", wraplength=280, justify="left")
        value_label.grid(row=row, column=1, sticky="w", padx=4)
        self.widgets[path] = value_label

    # ---- scenario.type linkage (GUI-only; never changes backend semantics) ----

    _CAP_ONLY_PREFIXES = (
        "trace.", "capacitor.", "capacitor_model.", "antipad.",
        "geometry.", "gnd_vias.", "baseline_comparison.",
    )
    _VIA_ONLY_PREFIXES = ("via_transition.",)

    def _scenario_is_via(self) -> bool:
        """Read the live scenario switch; ``scenario.type`` is a labeled combo."""
        return self._backend_value("scenario.type") == VIA_TRANSITION

    def _scenario_changed(self, *_args: Any) -> None:
        """Apply linkage immediately when the scenario switch is changed."""
        if self._ui_sync:
            return
        self._apply_scenario_linkage()
        if self._scenario_is_via():
            self.status.set(
                "已切换到“差分 VIA 换层”场景：请在 VIA 换层页编辑输入/输出传输线、"
                "信号孔（Signal VIA）、背钻和回流地孔（Return VIA）。"
            )
        else:
            self.status.set(
                "已切换到“AC 耦合电容”场景：电容/GND/差分走线/反焊盘参数生效，"
                "VIA 换层参数停用。"
            )
        self._schedule_refresh()

    def _apply_scenario_linkage(self) -> None:
        """Drive tab/field visibility from ``scenario.type``.

        Called on every load/refresh and directly when the switch changes so the
        UI never shows an editable-but-inactive parameter set.  Disabled widgets
        keep their ``tk.Variable`` values, so ``_collect()`` still reads them.
        """
        is_via = self._scenario_is_via()
        for page, enabled in (("电容与模型", not is_via), ("GND VIA", not is_via),
                              ("VIA 换层", is_via)):
            tab_id = self.tabs.get(page)
            if tab_id is not None:
                try:
                    self.notebook.tab(tab_id, state="normal" if enabled else "disabled")
                except tk.TclError:
                    pass
        self._set_group_state(self._CAP_ONLY_PREFIXES, enabled=not is_via)
        self._set_group_state(self._VIA_ONLY_PREFIXES, enabled=is_via)
        legacy_antipad = self.widgets.get("via_transition.padstack.antipad_diameter")
        if legacy_antipad is not None:
            self._configure_widget_state(legacy_antipad, False)
        if hasattr(self, "antipad_workbench"):
            self.antipad_workbench.set_scenario_editable(not is_via)
        if hasattr(self, "simulation_sweep_workbench"):
            self.simulation_sweep_workbench.set_sweep_scenario_enabled(
                True, "",
            )
        self._update_scenario_hints(is_via)

    def _set_group_state(self, prefixes: Sequence[str], enabled: bool) -> None:
        for path, widget in self.widgets.items():
            if not path.startswith(prefixes):
                continue
            self._configure_widget_state(widget, enabled)

    @staticmethod
    def _configure_widget_state(widget: tk.Widget, enabled: bool) -> None:
        try:
            if isinstance(widget, ttk.Combobox):
                widget.configure(state="readonly" if enabled else "disabled")
            elif isinstance(widget, (ttk.Entry, ttk.Checkbutton, ttk.Radiobutton)):
                widget.configure(state="normal" if enabled else "disabled")
        except tk.TclError:
            pass

    def _update_scenario_hints(self, is_via: bool) -> None:
        def set_hint(page: str, text: str, kind: str) -> None:
            label = self.scenario_hints.get(page)
            if label is None:
                return
            if not text:
                label.configure(text="")
                return
            label.configure(
                text=text,
                foreground="#9a3412" if kind == "warn" else "#166534",
            )

        if is_via:
            set_hint("项目与走线",
                     "⚠ 当前场景：差分 VIA 换层 —— 下方差分走线参数不生效，请在“VIA 换层”页设置输入/输出传输线；工程参数仍生效。",
                     "warn")
            set_hint("电容与模型",
                     "⚠ 当前场景：差分 VIA 换层 —— 本页电容参数不参与几何/校验/HFSS 生成。",
                     "warn")
            set_hint("反焊盘与叠层",
                     "✓ 当前场景：差分 VIA 换层 —— 本页只编辑全局/逐层反焊盘、逐层执行状态、模型边界和 PCB 叠层。",
                     "ok")
            set_hint("GND VIA",
                     "本场景的地孔请在 VIA 换层 → 回流地孔中设置。",
                     "warn")
            set_hint("VIA 换层",
                     "✓ 当前场景：差分 VIA 换层 —— 本页编辑换层/传输线、信号孔/背钻和回流地孔；右侧预览实时联动。",
                     "ok")
            set_hint("仿真与扫描",
                     "✓ 当前场景：差分 VIA 换层 —— 已绑定尺寸轴进入原生 Optimetrics；"
                     "拓扑、clearance 与逐层轴生成独立重算 Design/case；未实现的轴按失败关闭处理。",
                     "ok")
        else:
            set_hint("项目与走线",
                     "✓ 当前场景：AC 耦合电容 —— 下方差分走线参数生效。"
                     "VIA 换层仅在仿真场景选“差分 VIA 换层”后可用。",
                     "ok")
            set_hint("电容与模型",
                     "✓ 当前场景：AC 耦合电容 —— 本页电容参数生效。",
                     "ok")
            set_hint("反焊盘与叠层",
                     "✓ 当前场景：AC 耦合电容 —— 反焊盘/平面参数生效。",
                     "ok")
            set_hint("GND VIA",
                     "✓ 当前场景：AC 耦合电容 —— 本页 GND VIA 参数生效。",
                     "ok")
            set_hint("VIA 换层",
                     "⚠ 当前场景：AC 耦合电容 —— 本页 VIA 换层参数不生效。"
                     "VIA 换层仅在仿真场景选“差分 VIA 换层”后可用。",
                     "warn")
            set_hint("仿真与扫描",
                     "✓ 当前场景：AC 耦合电容 —— 参数扫描可用。",
                     "ok")

    def _build_project_trace(self) -> None:
        tab = self.tabs["项目与走线"]
        left = self._frame(tab, "工程", "left")
        right = self._frame(tab, "差分走线", "left")
        self._add(left, 0, "仿真场景", "scenario.type",
                  values=("surface_capacitor", "via_transition"),
                  display_values={"surface_capacitor": "AC 耦合电容",
                                  "via_transition": "差分 VIA 换层"})
        self.variables["scenario.type"].trace_add("write", self._scenario_changed)
        project_fields = [("工程名", "project.project_name"), ("设计名", "project.design_name"),
                          ("输出目录", "project.output_directory")]
        for row, (label, path) in enumerate(project_fields, start=1):
            self._add(left, row, label, path)
        self._add(left, 4, "AEDT 目标版本", "project.aedt_version",
                  values=tuple(sorted(SUPPORTED_AEDT_VERSIONS)),
                  display_values=AEDT_VERSION_LABELS)
        self._add(left, 5, "覆盖 V2 同名输出", "project.overwrite_existing", boolean=True)
        self._add(left, 6, "保持 AEDT 打开", "project.open_aedt_after_build", boolean=True)
        ttk.Label(left, text="支持 AEDT 2022 R2 / 2023 R2；配置中保存为 2022.2 / 2023.2。",
                  foreground="#475569", wraplength=440).grid(
                      row=7, column=0, columnspan=3, sticky="w", padx=4, pady=(6, 2))
        trace_fields = [("走线宽度", "trace.trace_width"),
                        ("差分中心距", "trace.trace_pitch"),
                        ("铜皮边到边间距", "trace.trace_gap"),
                        ("顶层铜厚", "trace.copper_thickness"),
                        ("输入直线长度", "trace.input_straight_length"),
                        ("输出直线长度", "trace.output_straight_length")]
        for row, (label, path) in enumerate(trace_fields):
            self._add(right, row, label, path, length=True)
        self._add(right, 6, "焊盘出线模式", "trace.pad_escape_mode",
                  values=("direct_45", "smooth_s_bend", "lead_then_45"),
                  display_values={"direct_45": "直接 45°（默认）",
                                  "smooth_s_bend": "平滑 S 弯",
                                  "lead_then_45": "水平引线后 45°（兼容）"})
        self._add(right, 7, "过渡方向", "trace.transition_direction",
                  values=("auto", "contract", "expand"),
                  display_values={"auto": "自动", "contract": "仅向内收缩", "expand": "仅向外展开"})
        self._add(right, 8, "输入焊盘引线（仅兼容模式）", "trace.pad_lead_in_length", length=True)
        self._add(right, 9, "输出焊盘引线（仅兼容模式）", "trace.pad_lead_out_length", length=True)
        self._add(right, 10, "S 弯过渡长度", "trace.transition_length", length=True)

    def _cap_section(self, parent: ttk.Frame, title: str) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, text=title, padding=(8, 6))
        frame.pack(fill="x", expand=False, padx=3, pady=4)
        frame.columnconfigure(1, weight=1)
        return frame

    def _build_capacitor(self) -> None:
        page_scroller = ScrollableFrame(
            self.tabs["电容与模型"], fill_viewport=True,
        )
        page_scroller.pack(fill="both", expand=True)
        self.cap_page_scroller = page_scroller
        self.cap_page_content = page_scroller.content
        tab = self.cap_page_content
        status_box = ttk.Frame(tab, padding=(8, 5))
        status_box.pack(side="bottom", fill="x")
        self.cap_status_box = status_box
        self.cap_status_var = tk.StringVar(value="正在检查配置…")
        self.cap_detail_var = tk.StringVar(value="自动派生字段将在输入后立即更新。")
        self.cap_status_label = ttk.Label(status_box, textvariable=self.cap_status_var, style="StatusGood.TLabel")
        self.cap_status_label.pack(anchor="w")
        self.cap_detail_label = ttk.Label(
            status_box, textvariable=self.cap_detail_var, foreground="#475569",
            wraplength=640, justify="left",
        )
        self.cap_detail_label.pack(anchor="w", fill="x", pady=(2, 0))

        panes = ttk.PanedWindow(tab, orient="horizontal")
        self.capacitor_panes = panes
        panes.pack(fill="both", expand=True)
        left = ttk.Frame(panes, padding=(2, 0))
        right = ttk.Frame(panes, padding=(4, 0))
        self.cap_parameter_column = left
        panes.add(left, weight=3)
        panes.add(right, weight=2)

        preview_box = ttk.LabelFrame(left, text="封装与几何 · 实时俯视", padding=6)
        preview_box.pack(fill="x", padx=3, pady=(2, 5))
        dimension_toolbar = ttk.Frame(preview_box)
        self.cap_dimension_toolbar = dimension_toolbar
        dimension_toolbar.pack(fill="x", pady=(0, 4))
        dimension_filters = ttk.Frame(dimension_toolbar)
        dimension_filters.pack(fill="x")
        dimension_view = ttk.Frame(dimension_toolbar)
        dimension_view.pack(fill="x", pady=(2, 0))
        self.cap_dimensions_visible = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            dimension_filters, text="显示尺寸", variable=self.cap_dimensions_visible,
            command=self._update_capacitor_dimension_filters,
        ).pack(side="left")
        ttk.Label(dimension_filters, text="附加：", foreground="#64748b").pack(side="left", padx=(5, 0))
        self.cap_dimension_categories: Dict[str, tk.BooleanVar] = {}
        for key, label in (("body", "本体"), ("pad", "焊盘"),
                           ("layout", "布局"), ("bridge", "桥接")):
            variable = tk.BooleanVar(value=False)
            self.cap_dimension_categories[key] = variable
            ttk.Checkbutton(
                dimension_filters, text=label, variable=variable,
                command=self._update_capacitor_dimension_filters,
            ).pack(side="left", padx=(3, 0))
        ttk.Label(dimension_view, text="视图：", foreground="#64748b").pack(side="left")
        ttk.Button(dimension_view, text="+",
                   command=lambda: self.cap_geometry_canvas.zoom_in(), width=3).pack(side="left", padx=(3, 0))
        ttk.Button(dimension_view, text="−",
                   command=lambda: self.cap_geometry_canvas.zoom_out(), width=3).pack(side="left")
        ttk.Button(dimension_view, text="恢复视图",
                   command=lambda: self.cap_geometry_canvas.reset_view()).pack(side="left", padx=(3, 0))
        self.cap_geometry_canvas = GeometryDiagram(
            preview_box, height=340, on_dimension_click=self._focus_capacitor_parameter
        )
        self.cap_geometry_scroll = ttk.Scrollbar(
            preview_box, orient="vertical", command=self.cap_geometry_canvas.yview,
        )
        self.cap_geometry_canvas.configure(yscrollcommand=self.cap_geometry_scroll.set)
        self.cap_geometry_scroll.pack(side="right", fill="y")
        self.cap_geometry_canvas.pack(side="left", fill="both", expand=True)
        self._cap_page_layout_job: Optional[str] = None
        page_scroller.canvas.bind(
            "<Configure>", self._schedule_capacitor_page_layout, add="+",
        )

        scroller = ScrollableFrame(left)
        scroller.pack(fill="both", expand=True, padx=2)
        self.cap_parameter_scroller = scroller
        parameter_root = scroller.content

        preset = self._cap_section(parameter_root, "封装预设")
        self._add(preset, 0, "电容安装面（当前）", "capacitor.mount_side",
                  values=("top", "bottom"), display_values=ENUM_LABELS["capacitor.mount_side"])
        self._add(preset, 1, "封装", "capacitor.package", values=("0201", "0402", "custom"),
                  display_values=ENUM_LABELS["capacitor.package"])
        buttons = ttk.Frame(preset)
        buttons.grid(row=2, column=0, columnspan=4, sticky="ew", padx=4, pady=3)
        ttk.Button(buttons, text="应用预设", command=self._apply_selected_preset).pack(side="left")
        ttk.Button(buttons, text="恢复当前封装预设", command=self._restore_package_preset).pack(side="left", padx=6)
        self.package_state_var = tk.StringVar(value="")
        ttk.Label(buttons, textvariable=self.package_state_var, foreground="#7c3aed").pack(side="right")
        ttk.Label(preset, text="预设为通用建议值，不代表具体厂商推荐焊盘，请以器件数据手册为准。",
                  foreground="#9a3412", wraplength=640).grid(row=3, column=0, columnspan=4, sticky="w", padx=4, pady=(3, 1))

        body = self._cap_section(parameter_root, "电容本体")
        self._add(body, 0, "本体长度", "capacitor.body_length", length=True)
        self._add(body, 1, "本体宽度", "capacitor.body_width", length=True)
        self._add(body, 2, "本体高度", "capacitor.body_height", length=True)

        pads = self._cap_section(parameter_root, "焊盘")
        self._add(pads, 0, "焊盘长度", "capacitor.pad_length", length=True)
        self._add(pads, 1, "焊盘宽度", "capacitor.pad_width", length=True)
        self._add(pads, 2, "焊盘内侧间隙", "capacitor.pad_gap", length=True, formula="用户输入")
        self._add(pads, 3, "焊盘中心距", "capacitor.pad_pitch", length=True, formula="= 焊盘长度 + 内侧间隙")
        self._add(pads, 4, "焊盘旋转", "capacitor.pad_rotation", numeric=True, unit="deg")
        self._add(pads, 5, "中心距模式", "capacitor.pad_pitch_mode",
                  values=("auto_from_gap", "advanced_pitch"), display_values=ENUM_LABELS["capacitor.pad_pitch_mode"])

        layout = self._cap_section(parameter_root, "P/N 两颗电容布置")
        self._add(layout, 0, "P/N 中心距", "capacitor.cap_pitch", length=True)
        self._add(layout, 1, "电容中心 X", "capacitor.cap_center_x", length=True)
        self._add(layout, 2, "中心 X 偏移", "capacitor.cap_center_x_offset", length=True)
        self._add(layout, 3, "关于 Y 轴对称", "capacitor.symmetric_about_y", boolean=True)

        bridge = self._cap_section(parameter_root, "桥接结构")
        self._add(bridge, 0, "桥接宽度", "capacitor.bridge_width", length=True)
        self._add(bridge, 1, "桥接长度", "capacitor.bridge_length", length=True,
                  formula="= pad_gap + 2 × overlap")
        self._add(bridge, 2, "桥接长度模式", "capacitor.bridge_length_mode", values=("auto", "manual"),
                  display_values=ENUM_LABELS["capacitor.bridge_length_mode"])
        self._add(bridge, 3, "桥接材料", "capacitor.bridge_material")

        hfss = self._cap_section(parameter_root, "HFSS 本体创建选项")
        self.cap_hfss_box = hfss
        hfss.pack_configure(pady=(4, 24))
        self._add(hfss, 0, "在 HFSS 中创建电容本体", "capacitor.create_body_in_hfss", boolean=True)
        self._add(hfss, 1, "预览中显示本体轮廓", "capacitor.show_body_outline_in_preview", boolean=True)
        spacer = tk.Frame(parameter_root, height=120, background="#f7f8fa")
        spacer.pack_propagate(False)
        spacer.pack(fill="x")
        # A newline label is a guaranteed-tall last child if the Frame still
        # reports height 1 after pack_propagate(False). Keep it short enough
        # that yview(1.0) still shows the create-body checkbox.
        tail = ttk.Label(parameter_root, text="\n", background="#f7f8fa")
        tail.pack(fill="x")
        self.cap_parameter_spacer = spacer
        self.cap_parameter_tail = tail

        mode_box = ttk.LabelFrame(right, text="电气模型模式", padding=8)
        mode_box.pack(fill="x", pady=(2, 5))
        mode_box.columnconfigure(1, weight=1)
        self._add(mode_box, 0, "模型模式", "capacitor_model.mode",
                  values=("ideal_bridge", "lumped_rlc", "sparameter", "open"),
                  display_values=ENUM_LABELS["capacitor_model.mode"])

        model_box = ttk.LabelFrame(right, text="HFSS 实现示意", padding=6)
        model_box.pack(fill="x", pady=5)
        self.model_canvas = ElectricalModelDiagram(model_box)
        self.model_canvas.pack(fill="x", expand=True)

        self.model_parameters = ttk.LabelFrame(right, text="当前模型参数", padding=8)
        self.model_parameters.pack(fill="x", pady=5)
        self.model_parameters.columnconfigure(1, weight=1)
        self.model_frames: Dict[str, ttk.Frame] = {}
        ideal = ttk.Frame(self.model_parameters)
        self.model_frames["ideal_bridge"] = ideal
        ttk.Label(ideal, text="两个焊盘将由铜导体直接连接，电容值、ESR和ESL不会参与仿真。",
                  foreground="#6b3f19", wraplength=390).pack(fill="x", pady=5)
        ttk.Label(ideal, text="桥接宽度、长度和自动/手动模式请在左侧“桥接结构”中编辑。",
                  foreground="#475569", wraplength=390).pack(fill="x")

        rlc = ttk.Frame(self.model_parameters)
        rlc.columnconfigure(1, weight=1)
        self.model_frames["lumped_rlc"] = rlc
        self._add(rlc, 0, "电容 C", "capacitor_model.capacitance_pf", numeric=True, unit="pF")
        self._add(rlc, 1, "ESR", "capacitor_model.esr_ohm", numeric=True, unit="Ω")
        self._add(rlc, 2, "ESL", "capacitor_model.esl_nh", numeric=True, unit="nH")
        self._add(rlc, 3, "RLC Sheet宽度", "capacitor_model.sheet_width", length=True)
        ttk.Label(rlc, text="焊盘之间不会创建铜桥。积分线沿电容局部长度方向：输入焊盘 → 输出焊盘。",
                  foreground="#475569", wraplength=390).grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 2))

        spar = ttk.Frame(self.model_parameters)
        spar.columnconfigure(1, weight=1)
        self.model_frames["sparameter"] = spar
        self._add(spar, 0, "公共 Touchstone 文件", "capacitor_model.touchstone_file")
        ttk.Button(spar, text="浏览…", command=self._browse_touchstone).grid(row=0, column=2, padx=4)
        self.touchstone_info_var = tk.StringVar(value="尚未选择文件")
        ttk.Label(spar, textvariable=self.touchstone_info_var, foreground="#334155", wraplength=390,
                  justify="left").grid(row=1, column=0, columnspan=3, sticky="w", padx=4, pady=5)
        ttk.Label(spar, text="端口映射：端口 1 → 焊盘 A，端口 2 → 焊盘 B。首次用于目标 AEDT/PyAEDT 版本时，请核对网络组件接口、端口方向和参考阻抗。",
                  foreground="#9a3412", wraplength=390, justify="left").grid(row=2, column=0, columnspan=3, sticky="w", padx=4)
        ttk.Label(spar, text="加载失败会明确终止建模，不会回退为理想短路。",
                  foreground="#b42318", wraplength=390).grid(row=3, column=0, columnspan=3, sticky="w", padx=4, pady=(5, 0))
        self._register_hidden_variable("capacitor_model.touchstone_file_p")
        self._register_hidden_variable("capacitor_model.touchstone_file_n")

        opened = ttk.Frame(self.model_parameters)
        self.model_frames["open"] = opened
        ttk.Label(opened, text="仅保留分离焊盘：无铜桥、无RLC、无Touchstone组件。",
                  foreground="#475569", wraplength=390).pack(fill="x", pady=5)

        summary = ttk.LabelFrame(right, text="模型摘要与派生结果", padding=8)
        summary.pack(fill="both", expand=True, pady=5)
        self.model_summary_var = tk.StringVar(value="")
        ttk.Label(summary, textvariable=self.model_summary_var, wraplength=410, justify="left",
                  foreground="#334155").pack(fill="x", anchor="nw")

    def _schedule_capacitor_page_layout(self, _event: Optional[tk.Event] = None) -> None:
        """Debounce viewport/font changes without creating Configure loops."""
        job = getattr(self, "_cap_page_layout_job", None)
        if job is not None:
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
        self._cap_page_layout_job = self.after_idle(self._apply_capacitor_page_layout)

    def _apply_capacitor_page_layout(self) -> None:
        self._cap_page_layout_job = None
        if self._ui_sync or not hasattr(self, "cap_geometry_canvas"):
            return
        try:
            viewport_height = max(1, self.cap_page_scroller.canvas.winfo_height())
            linespace = max(1, int(tkfont.nametofont("TkDefaultFont").metrics("linespace")))
        except tk.TclError:
            return

        # Give the diagram enough room for its metric-driven labels, while
        # leaving the growing share of a tall page to parameters and summary.
        minimum = max(250, linespace * 11)
        maximum = max(minimum, min(440, linespace * 18))
        target = max(minimum, min(maximum, round(viewport_height * 0.34)))
        if int(float(self.cap_geometry_canvas.cget("height"))) != target:
            self.cap_geometry_canvas.configure(height=target)
        self.cap_page_scroller.refresh_layout()

    def _update_capacitor_dimension_filters(self) -> None:
        if not hasattr(self, "cap_geometry_canvas"):
            return
        categories = {
            key: bool(variable.get())
            for key, variable in self.cap_dimension_categories.items()
        }
        self.cap_geometry_canvas.set_dimension_filters(
            bool(self.cap_dimensions_visible.get()), categories
        )

    def _focus_capacitor_parameter(self, path: str) -> None:
        """Navigate from a plotted/listed dimension to its editor field."""
        widget = self.widgets.get(path)
        if widget is None:
            return
        self.notebook.select(self.tabs["电容与模型"])

        def reveal() -> None:
            self.cap_parameter_scroller.scroll_to_widget(widget)
            widget.focus_set()
            if isinstance(widget, ttk.Entry):
                try:
                    widget.selection_range(0, "end")
                    widget.icursor("end")
                except tk.TclError:
                    pass
            self.status.set(f"已定位参数：{path}")

        self.after_idle(reveal)

    def _build_antipad_stackup(self) -> None:
        tab = self.tabs["反焊盘与叠层"]
        top = self._frame(tab, "反焊盘与板框")
        self._add(top, 0, "反焊盘模式", "antipad.mode", values=("independent_pad", "merged_per_capacitor", "merged_all"))
        for row, (label, path) in enumerate((("全局单边外扩", "antipad.global_expansion"),
                                             ("输入平面边距", "plane.margin_input"),
                                             ("输出平面边距", "plane.margin_output"),
                                             ("Y向平面边距", "plane.margin_y"),
                                             ("几何重叠量", "geometry.overlap")), 1):
            self._add(top, row, label, path, length=True)
        frame = ttk.LabelFrame(tab, text="PCB叠层（双击编辑）", padding=8)
        frame.pack(fill="both", expand=True, pady=8)
        self.stack_tree = ttk.Treeview(frame, columns=("name", "type", "thickness", "material", "dk", "df", "ref", "antipad"), show="headings")
        for key, label in (("name", "名称"), ("type", "类型"), ("thickness", "厚度"), ("material", "材料"),
                           ("dk", "Dk"), ("df", "Df"), ("ref", "参考"), ("antipad", "反焊盘")):
            self.stack_tree.heading(key, text=label)
            self.stack_tree.column(key, width=110, anchor="center")
        self.stack_tree.pack(fill="both", expand=True)
        self.stack_tree.bind("<Double-1>", self._edit_layer)

    def _build_vias(self) -> None:
        tab = self.tabs["GND VIA"]
        left = self._frame(tab, "布局与制造", "left")
        right = self._frame(tab, "层连接与安装面GND", "left")
        self._add(left, 0, "启用 GND VIA", "gnd_vias.enabled", boolean=True)
        self._add(left, 1, "布局模式", "gnd_vias.pattern", values=("four_corner", "side_pair", "rectangular_fence", "custom"))
        self._add(left, 2, "数量", "gnd_vias.count", numeric=True, integer=True)
        self._add(left, 3, "侧对方向", "gnd_vias.side_orientation", values=("top_bottom", "left_right"))
        for row, (label, key) in enumerate((("X净距", "clearance_x"), ("Y净距", "clearance_y"),
                                            ("X节距", "pitch_x"), ("Y节距", "pitch_y"),
                                            ("钻孔直径", "drill_diameter"), ("成品孔直径", "finished_hole_diameter"),
                                            ("镀铜厚度", "plating_thickness"), ("焊盘直径", "pad_diameter"),
                                            ("避空直径", "antipad_diameter")), 4):
            self._add(left, row, label, "gnd_vias." + key, length=True)
        self._add(right, 0, "起始层", "gnd_vias.start_layer")
        self._add(right, 1, "终止层", "gnd_vias.end_layer")
        self._add(right, 2, "连接参考层（逗号分隔）", "gnd_vias.connect_layers")
        self._add(right, 3, "材料", "gnd_vias.material")
        self._add(right, 4, "筒壁模型", "gnd_vias.barrel_model", values=("hollow", "solid"))
        self._add(right, 5, "创建安装面GND铜皮", "gnd_vias.create_top_ground_copper", boolean=True)
        self._add(right, 6, "安装面GND信号净距", "gnd_vias.top_ground_clearance", length=True)
        self._add(right, 7, "自定义坐标 x,y; x,y", "gnd_vias.custom_coordinates")
        ttk.Label(right, text="优先使用空心镀铜筒壁；实心模式仅用于趋势近似。自动布局关于差分中心对称。",
                  foreground="#555", wraplength=430).grid(row=8, column=0, columnspan=3, sticky="w", pady=12)

    def _build_via_transition(self) -> None:
        tab = self.tabs["VIA 换层"]
        # 垂直 PanedWindow：上为参数滚动区，下为示意图区；保证两张图获得足够高度，
        # 且分界可拖动调整，避免参数区把示意图压成不可见。
        vpanes = ttk.PanedWindow(tab, orient="vertical")
        vpanes.pack(fill="both", expand=True, padx=4, pady=4)

        scroller = ScrollableFrame(vpanes, fill_viewport=True)
        self.via_page_scroller = scroller
        body = scroller.content

        def group(title: str) -> ttk.LabelFrame:
            frame = ttk.LabelFrame(body, text=title, padding=8)
            frame.pack(fill="x", padx=2, pady=3)
            frame.columnconfigure(1, weight=1)
            return frame

        f = group("场景与进出层")
        signal_layers = tuple(str(layer["name"]) for layer in self.config.get("stackup", [])
                              if layer.get("type") == "signal")
        conductive_layers = tuple(str(layer["name"]) for layer in self.config.get("stackup", [])
                                  if layer.get("type") in {"signal", "plane"})
        self._add_fixed_label(f, 0, "模式", "via_transition.mode", "diff_pair",
                              "差分对（当前仅支持）")
        self._add(f, 1, "② 走线方向", "via_transition.route_axis", values=("x", "y"),
                  display_values={"x": "X 方向", "y": "Y 方向"})
        self._register_hidden_variable("via_transition.pair_pitch")
        self.length_paths.add("via_transition.pair_pitch")
        self.numeric_paths.add("via_transition.pair_pitch")
        self._add(f, 2, "㉑ 信号孔中心距（孔间距）", "via_transition.signal_via_pitch", length=True)
        self._add(f, 3, "中心 X", "via_transition.center_x", length=True)
        self._add(f, 4, "中心 Y", "via_transition.center_y", length=True)
        self._add(f, 5, "③ 输入层", "via_transition.entry_layer", values=signal_layers)
        self._add(f, 6, "④ 输出层", "via_transition.exit_layer", values=signal_layers)
        self._add(f, 7, "⑤ 机械钻孔起始层", "via_transition.drill_start_layer", values=conductive_layers)
        self._add(f, 8, "⑥ 机械钻孔终止层", "via_transition.drill_end_layer", values=conductive_layers)
        ttk.Label(f, text="层名来自“反焊盘与叠层”。输入/输出层须为信号层，机械钻孔须为导体层。",
                  foreground="#64748b", wraplength=430, justify="left").grid(
                      row=9, column=0, columnspan=3, sticky="ew", pady=(2, 0))

        f = group("走线扇出")
        self._add(f, 0, "㉒ 扇出方式", "via_transition.fanout.style",
                  values=("deg45", "arc"), display_values={"deg45": "45° 折线", "arc": "圆弧"})
        self._add(f, 1, "㉓ 扇出方向", "via_transition.fanout.direction",
                  values=("auto", "expand", "contract"),
                  display_values={"auto": "自动", "expand": "向外", "contract": "向内"})
        self._add(f, 2, "㉔ 耦合直线段长", "via_transition.fanout.straight_length", length=True)
        ttk.Label(f, text="两侧走线中心距均等于孔间距时，扇出三项自动停用。",
                  foreground="#64748b", wraplength=430, justify="left").grid(
                      row=3, column=0, columnspan=3, sticky="ew", pady=(2, 0))

        f = group("输入/输出走线")
        self._add(f, 0, "⑦ 输入走线长度", "via_transition.entry_trace.length", length=True)
        self._add(f, 1, "⑧ 输入走线宽度", "via_transition.entry_trace.width", length=True)
        self._add(f, 2, "输入 GAP", "via_transition.entry_trace.gap", length=True)
        self._add(f, 3, "输入中心距", "via_transition.entry_trace.center_to_center",
                  length=True, formula="= 宽度 + GAP")
        self._derived_display_paths.add("via_transition.entry_trace.center_to_center")
        self._add(f, 4, "⑨ 输出走线长度", "via_transition.exit_trace.length", length=True)
        self._add(f, 5, "⑩ 输出走线宽度", "via_transition.exit_trace.width", length=True)
        self._add(f, 6, "输出 GAP", "via_transition.exit_trace.gap", length=True)
        self._add(f, 7, "输出中心距", "via_transition.exit_trace.center_to_center",
                  length=True, formula="= 宽度 + GAP")
        self._derived_display_paths.add("via_transition.exit_trace.center_to_center")

        f = group("Signal VIA Padstack")
        self._add(f, 0, "⑪ 钻孔直径", "via_transition.padstack.drill_diameter", length=True)
        self._add(f, 1, "⑫ 成品孔直径", "via_transition.padstack.finished_hole_diameter", length=True,
                  formula="= 钻孔 − 2×镀铜")
        self._add(f, 2, "⑬ 镀铜厚度", "via_transition.padstack.plating_thickness", length=True)
        self._add(f, 3, "⑭ 功能焊盘直径", "via_transition.padstack.connected_pad_diameter", length=True)
        self._add(f, 4, "非功能焊盘", "via_transition.padstack.nonfunctional_pad_mode",
                  values=("remove", "keep"), display_values={"remove": "移除", "keep": "保留"})
        self._add(f, 5, "⑮ 非功能焊盘直径", "via_transition.padstack.nonfunctional_pad_diameter", length=True)
        self._add(f, 6, "⑯ 反焊盘直径", "via_transition.padstack.antipad_diameter", length=True)
        self._add(f, 7, "筒壁模型", "via_transition.padstack.barrel_model", values=("hollow", "solid"),
                  display_values={"hollow": "空心镀铜筒壁", "solid": "实心（仅趋势近似）"})

        f = group("背钻与残桩")
        self._add(f, 0, "启用背钻", "via_transition.backdrill.enabled", boolean=True)
        self._add(f, 1, "背钻方向", "via_transition.backdrill.from_side", values=("auto", "top", "bottom"),
                  display_values={"auto": "自动", "top": "顶面", "bottom": "底面"})
        self._add_fixed_label(f, 2, "控制模式", "via_transition.backdrill.control_mode",
                              "residual_stub", "目标残桩长度控制（当前仅支持）")
        self._add(f, 3, "⑰ 背钻孔径", "via_transition.backdrill.drill_diameter", length=True)
        self._add(f, 4, "⑱ 目标残桩长度", "via_transition.backdrill.residual_stub", length=True)
        self.backdrill_hint_label = ttk.Label(
            f, text="残桩从背钻侧最近功能连接层起算；与输入/输出层无关。",
            foreground="#64748b", font=("TkDefaultFont", 8))
        self.backdrill_hint_label.grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(2, 0))

        f = group("Return VIA 回流孔")
        self._add(f, 0, "启用回流孔", "via_transition.return_vias.enabled", boolean=True)
        self._add(f, 1, "布局模式", "via_transition.return_vias.pattern",
                  values=("side_pair", "four_corner", "custom"),
                  display_values={"side_pair": "两侧成对", "four_corner": "四角", "custom": "自定义"})
        self._add(f, 2, "数量", "via_transition.return_vias.count", numeric=True, integer=True)
        self._add(f, 3, "X 向偏移", "via_transition.return_vias.offset_x", length=True)
        self._add(f, 4, "Y 向偏移", "via_transition.return_vias.offset_y", length=True)
        self._add(f, 5, "X 向节距", "via_transition.return_vias.pitch_x", length=True)
        self._add(f, 6, "Y 向节距", "via_transition.return_vias.pitch_y", length=True)
        self._add(f, 7, "起始层", "via_transition.return_vias.start_layer", values=conductive_layers)
        self._add(f, 8, "终止层", "via_transition.return_vias.end_layer", values=conductive_layers)
        self._add(f, 9, "连接参考层（由下方矩阵选择）", "via_transition.return_vias.connect_layers")
        self._add(f, 10, "自定义坐标 x,y; x,y", "via_transition.return_vias.custom_coordinates")
        self._add(f, 11, "钻孔直径", "via_transition.return_vias.drill_diameter", length=True)
        self._add(f, 12, "成品孔直径", "via_transition.return_vias.finished_hole_diameter", length=True)
        self._add(f, 13, "镀铜厚度", "via_transition.return_vias.plating_thickness", length=True)
        self._add(f, 14, "⑲ 焊盘直径", "via_transition.return_vias.pad_diameter", length=True)
        self._add(f, 15, "⑳ 避空直径", "via_transition.return_vias.antipad_diameter", length=True)
        ttk.Label(f, text="连接参考 plane（Ctrl/Shift 多选）").grid(
            row=16, column=0, sticky="nw", padx=4, pady=(5, 2))
        self.return_connect_listbox = tk.Listbox(
            f, selectmode="extended", exportselection=False, height=4)
        self.return_connect_listbox.grid(row=16, column=1, sticky="ew", padx=4, pady=(5, 2))
        self.return_connect_listbox.bind("<<ListboxSelect>>", self._return_connect_selection_changed)

        bottom = ttk.Frame(vpanes)

        # 下层：派生值与校验整行显示，高度适中（约 6 行，可滚动），不再挤占绘图宽度。
        fb = ttk.LabelFrame(bottom, text="派生值与校验", padding=6)
        fb.pack(side="bottom", fill="x")
        self.via_feedback_var = tk.StringVar(value="输入后自动更新。")
        fb_text = tk.Text(fb, height=4, wrap="word", font=("TkDefaultFont", 9),
                          background="#f7f8fa", foreground="#334155", relief="flat",
                          borderwidth=0, highlightthickness=0, padx=2, pady=2)
        fb_scroll = ttk.Scrollbar(fb, orient="vertical", command=fb_text.yview)
        fb_text.configure(yscrollcommand=fb_scroll.set)
        fb_text.pack(side="left", fill="both", expand=True)
        fb_scroll.pack(side="right", fill="y")
        fb_text.insert("1.0", "输入后自动更新。")
        fb_text.configure(state="disabled")

        def _sync_via_feedback(*_args: Any) -> None:
            try:
                fb_text.configure(state="normal")
                fb_text.delete("1.0", "end")
                fb_text.insert("1.0", self.via_feedback_var.get())
                fb_text.configure(state="disabled")
            except tk.TclError:
                pass

        self.via_feedback_var.trace_add("write", _sync_via_feedback)

        # 上层：水平 PanedWindow，俯视图｜截面图各占约一半宽度，权重 1:1。
        panes = ttk.PanedWindow(bottom, orient="horizontal")
        panes.pack(side="top", fill="both", expand=True)

        top_box = ttk.LabelFrame(panes, text="俯视图（P/N 信号孔、回流孔、走线）", padding=6)
        self.via_top_canvas = tk.Canvas(top_box, height=240, background="#f7f8fa",
                                        highlightthickness=1, highlightbackground="#cbd5e1")
        self.via_top_canvas.pack(fill="both", expand=True)
        self.via_top_canvas.tag_bind("dimension-label", "<Button-1>", self._on_via_dimension_click)
        self.via_top_canvas.tag_bind("dimension-label", "<Enter>", self._on_via_dimension_enter)
        self.via_top_canvas.tag_bind("dimension-label", "<Leave>", self._on_via_dimension_leave)
        self.via_top_canvas.bind("<Configure>", lambda _e: self._schedule_via_diagram_redraw())
        panes.add(top_box, weight=1)

        cross_box = ttk.LabelFrame(panes, text="过孔截面示意 · 尺寸对应", padding=6)
        cross_inner = ttk.Frame(cross_box)
        cross_inner.pack(fill="both", expand=True)
        self.via_cross_canvas = tk.Canvas(cross_inner, height=420, background="#fbfcfe",
                                          highlightthickness=1, highlightbackground="#cbd5e1")
        self.via_cross_scrollbar = ttk.Scrollbar(cross_inner, orient="vertical",
                                                 command=self.via_cross_canvas.yview)
        self.via_cross_canvas.configure(yscrollcommand=self.via_cross_scrollbar.set)
        self.via_cross_canvas.pack(side="left", fill="both", expand=True)
        self.via_cross_scrollbar.pack(side="right", fill="y")
        self.via_cross_canvas.tag_bind("dimension-label", "<Button-1>", self._on_via_dimension_click)
        self.via_cross_canvas.tag_bind("dimension-label", "<Enter>", self._on_via_dimension_enter)
        self.via_cross_canvas.tag_bind("dimension-label", "<Leave>", self._on_via_dimension_leave)
        self.via_cross_canvas.bind("<Configure>", lambda _e: self._schedule_via_diagram_redraw())
        self.via_cross_canvas.bind("<MouseWheel>", self._on_via_cross_wheel)
        panes.add(cross_box, weight=1)

        # 参数滚动区与示意图区各占一半，分界可拖动调整。
        vpanes.add(scroller, weight=1)
        vpanes.add(bottom, weight=1)

        # 编号参数 → widget 反向映射，并绑定 FocusIn/FocusOut 用于「参数 → 图高亮」。
        self._via_param_widgets = {}
        self._via_param_widget_paths = {}
        for path in self._via_param_numbers:
            widget = self.widgets.get(path)
            if widget is None:
                continue
            self._via_param_widgets[widget] = path
            self._via_param_widget_paths[str(widget)] = path
            widget.bind("<FocusIn>", self._on_via_param_focus_in, add="+")
            widget.bind("<FocusOut>", self._on_via_param_focus_out, add="+")

    def _build_simulation_sweeps(self) -> None:
        tab = self.tabs["仿真与扫描"]
        sim = self._frame(tab, "自动仿真")
        self._add(sim, 0, "启用自动仿真", "simulation.enabled", boolean=True)
        self._add(sim, 1, "PCIe频率预设", "simulation.pcie_generation_preset", values=("custom", "Gen3", "Gen4", "Gen5", "Gen6"))
        self._add(sim, 2, "创建差分端口", "simulation.create_differential_ports", boolean=True)
        self._add(sim, 3, "Port参考模式", "simulation.port_reference_mode", values=("auto_nearest", "manual"))
        self._add(sim, 4, "Port参考层", "simulation.port_reference_layer")
        self._add(sim, 5, "Port尺寸模式", "simulation.port_size_mode", values=("auto", "manual"))
        self._add(sim, 6, "Port最小/手动Y外扩", "simulation.port_lateral_margin", length=True)
        self._add(sim, 7, "空气盒边距", "simulation.airbox_margin", length=True)
        self._add(sim, 8, "开放边界", "simulation.open_boundary", values=("Radiation", "PML"))
        self._add(sim, 9, "扫频类型", "simulation.sweep_type", values=("Interpolating", "Discrete"))
        for row, (label, key) in enumerate((("自适应频率", "adaptive_frequency_ghz"), ("起始频率", "start_frequency_ghz"),
                                            ("终止频率", "stop_frequency_ghz"), ("频率步进", "step_frequency_ghz")), 10):
            self._add(sim, row, label, "simulation." + key, numeric=True, unit="GHz")
        self._add(sim, 14, "生成S参数报告", "simulation.generate_reports", boolean=True)
        self._add(sim, 15, "生成TDR请求", "simulation.generate_tdr", boolean=True)
        sweep_frame = ttk.LabelFrame(tab, text="参数扫描（所有长度按当前显示单位）", padding=8)
        sweep_frame.pack(fill="both", expand=True, pady=6)
        headers = ("参数", "启用", "Start", "Stop", "Step")
        for col, text in enumerate(headers):
            ttk.Label(sweep_frame, text=text).grid(row=0, column=col, padx=5, pady=3, sticky="w")
        for row, (name, sweep) in enumerate(self.config["sweeps"].items(), 1):
            if "values" in sweep:
                continue
            vars_for = {"enabled": tk.BooleanVar(), "start": tk.StringVar(), "stop": tk.StringVar(), "step": tk.StringVar()}
            self.sweep_vars[name] = vars_for
            ttk.Label(sweep_frame, text=name).grid(row=row, column=0, sticky="w", padx=5)
            ttk.Checkbutton(sweep_frame, variable=vars_for["enabled"]).grid(row=row, column=1)
            for col, key in enumerate(("start", "stop", "step"), 2):
                ttk.Entry(sweep_frame, textvariable=vars_for[key], width=12).grid(row=row, column=col, padx=3, pady=2)

    def _refresh_via_layer_selectors(self, config: Mapping[str, Any]) -> None:
        stackup = list(config.get("stackup", []))
        signal = tuple(str(layer.get("name", "")) for layer in stackup
                       if layer.get("type") == "signal")
        conductive = tuple(str(layer.get("name", "")) for layer in stackup
                            if layer.get("type") in {"signal", "plane"})
        for path in ("via_transition.entry_layer", "via_transition.exit_layer"):
            widget = self.widgets.get(path)
            if isinstance(widget, ttk.Combobox):
                widget.configure(values=signal)
        for path in ("via_transition.drill_start_layer", "via_transition.drill_end_layer",
                     "via_transition.return_vias.start_layer", "via_transition.return_vias.end_layer"):
            widget = self.widgets.get(path)
            if isinstance(widget, ttk.Combobox):
                widget.configure(values=conductive)
        listbox = getattr(self, "return_connect_listbox", None)
        if listbox is not None:
            references = [str(layer.get("name", "")) for layer in stackup
                          if layer.get("type") == "plane" and layer.get("is_reference_plane")]
            selected = set(str(item) for item in config.get("via_transition", {}).get(
                "return_vias", {}).get("connect_layers", []))
            listbox.delete(0, "end")
            for index, name in enumerate(references):
                listbox.insert("end", name)
                if name in selected:
                    listbox.selection_set(index)

    def _return_connect_selection_changed(self, _event: Any = None) -> None:
        if self._ui_sync:
            return
        listbox = getattr(self, "return_connect_listbox", None)
        if listbox is None:
            return
        selected = [str(listbox.get(index)) for index in listbox.curselection()]
        variable = self.variables.get("via_transition.return_vias.connect_layers")
        if variable is not None:
            variable.set(",".join(selected))  # read-only compatibility summary
        self._schedule_refresh()

    def _load_to_widgets(self, config: Mapping[str, Any]) -> None:
        self.config = copy.deepcopy(dict(config))
        model = self.config.get("capacitor_model", {})
        common = str(model.get("touchstone_file", "")).strip()
        p_file = str(model.get("touchstone_file_p", "")).strip()
        n_file = str(model.get("touchstone_file_n", "")).strip()
        if model.get("mode") == "sparameter":
            if not common and p_file and p_file == n_file:
                model["touchstone_file"] = p_file
                model["touchstone_file_p"] = ""
                model["touchstone_file_n"] = ""
            elif common:
                model["touchstone_file_p"] = ""
                model["touchstone_file_n"] = ""
        self.display_unit = str(self.config["units"])
        self.unit_var.set(self.display_unit)
        self._ui_sync = True
        for path, var in self.variables.items():
            if path in self._derived_display_paths:
                continue
            value = _get(self.config, path)
            if path in self.enum_labels:
                value = self.enum_labels[path].get(str(value), str(value))
            if path == "gnd_vias.connect_layers":
                value = ",".join(value)
            elif path == "gnd_vias.custom_coordinates":
                value = "; ".join(f"{format_length(p[0], self.display_unit)},{format_length(p[1], self.display_unit)}" for p in value)
            elif path == "via_transition.return_vias.connect_layers":
                value = ",".join(str(item) for item in value)
            elif path == "via_transition.return_vias.custom_coordinates":
                value = "; ".join(f"{format_length(p[0], self.display_unit)},{format_length(p[1], self.display_unit)}" for p in value)
            elif path in self.length_paths:
                mm_value = float(value)
                value = format_length(mm_value, self.display_unit)
                self._length_mm_cache[path] = mm_value
                self._length_display_snapshot[path] = str(value)
            var.set(bool(value) if isinstance(var, tk.BooleanVar) else _display(value))
        for name, vars_for in self.sweep_vars.items():
            sweep = config["sweeps"][name]
            vars_for["enabled"].set(bool(sweep["enabled"]))
            for key in ("start", "stop", "step"):
                value = float(sweep[key])
                if name not in {"gnd_vias.count", "capacitor.pad_rotation"}:
                    value = convert_length(value, "mm", self.display_unit)
                vars_for[key].set(_display(value))
        self._refresh_via_layer_selectors(self.config)
        self._ui_sync = False
        self._refresh_stackup()
        self._update_unit_labels()
        self._apply_scenario_linkage()
        self._sync_via_derived_centers()
        self._schedule_refresh()

    def _collect(self) -> Dict[str, Any]:
        data = copy.deepcopy(self.config)
        data["units"] = self.display_unit
        data["_internal_units"] = "mm"
        for path, var in self.variables.items():
            if path in self._derived_display_paths:
                continue
            raw = var.get()
            if path == "gnd_vias.connect_layers":
                value: Any = [item.strip() for item in str(raw).split(",") if item.strip()]
            elif path == "gnd_vias.custom_coordinates":
                points = []
                for item in str(raw).split(";"):
                    if not item.strip():
                        continue
                    x, y = item.split(",")
                    points.append([convert_length(float(x), self.display_unit, "mm"),
                                   convert_length(float(y), self.display_unit, "mm")])
                value = points
            elif path == "via_transition.return_vias.connect_layers":
                listbox = getattr(self, "return_connect_listbox", None)
                value = ([str(listbox.get(index)) for index in listbox.curselection()]
                         if listbox is not None else
                         [item.strip() for item in str(raw).split(",") if item.strip()])
            elif path == "via_transition.return_vias.custom_coordinates":
                points = []
                for item in str(raw).split(";"):
                    if not item.strip():
                        continue
                    x, y = item.split(",")
                    points.append([convert_length(float(x), self.display_unit, "mm"),
                                   convert_length(float(y), self.display_unit, "mm")])
                value = points
            elif isinstance(var, tk.BooleanVar):
                value = bool(raw)
            elif path in self.enum_labels:
                inverse = {label: backend for backend, label in self.enum_labels[path].items()}
                value = inverse.get(str(raw), str(raw).strip())
            elif path in self.length_paths:
                raw_text = str(raw).strip()
                if (raw_text == self._length_display_snapshot.get(path)
                        and path in self._length_mm_cache):
                    value = self._length_mm_cache[path]
                else:
                    value = convert_length(float(raw_text), self.display_unit, "mm")
            elif path in self.numeric_paths:
                value = int(float(raw)) if path in self.integer_paths else float(raw)
            else:
                value = str(raw).strip()
            _set(data, path, value)
        model = data["capacitor_model"]
        if model.get("mode") == "sparameter" and str(model.get("touchstone_file", "")).strip():
            model["touchstone_file_p"] = ""
            model["touchstone_file_n"] = ""
        for name, vars_for in self.sweep_vars.items():
            sweep = data["sweeps"][name]
            sweep["enabled"] = bool(vars_for["enabled"].get())
            for key in ("start", "stop", "step"):
                value = float(vars_for[key].get())
                sweep[key] = (
                    value
                    if name in {"gnd_vias.count", "capacitor.pad_rotation"}
                    else convert_length(value, self.display_unit, "mm")
                )
        return data

    def _unit_changed(self, _event: Any = None) -> None:
        new_unit = self.unit_var.get()
        if new_unit == self.display_unit:
            return
        try:
            data = self._collect()  # reads using previous display_unit
            data["units"] = new_unit
            self.display_unit = new_unit
            self._load_to_widgets(data)
            self._update_package_state()
            self.status.set(f"显示单位已切换为 {new_unit}，内部物理尺寸保持不变")
            if hasattr(self, "_set_ready_status"):
                self._set_ready_status()
        except Exception as exc:
            self.unit_var.set(self.display_unit)
            messagebox.showerror("单位切换失败", str(exc), parent=self)

    def _update_unit_labels(self) -> None:
        for label in self.unit_labels:
            label.configure(text=self.display_unit)

    def _backend_value(self, path: str) -> str:
        raw = str(self.variables[path].get())
        inverse = {label: backend for backend, label in self.enum_labels.get(path, {}).items()}
        return inverse.get(raw, raw)

    def _package_changed(self, _event: Any = None) -> None:
        package = self._backend_value("capacitor.package")
        if package in {"0201", "0402"}:
            self.status.set(f"已选择 {package}；尺寸尚未覆盖，请点击“应用预设”")
        else:
            self.status.set("已切换为自定义封装；现有尺寸保持不变")
        self._update_package_state()
        self._schedule_refresh()

    def _apply_selected_preset(self) -> None:
        try:
            package = self._backend_value("capacitor.package")
            if package not in PACKAGE_PRESETS:
                self.status.set("自定义封装没有可应用的预设")
                return
            data = apply_package_preset(self._collect(), package)
            fields = ", ".join(key for key in PACKAGE_PRESETS[package] if key != "package_metric")
            self._load_to_widgets(data)
            self.status.set(f"已应用 {package} 通用建议值：{fields}")
        except Exception as exc:
            messagebox.showerror("应用预设失败", str(exc), parent=self)

    def _restore_package_preset(self) -> None:
        package = self._backend_value("capacitor.package")
        if package not in PACKAGE_PRESETS:
            self.status.set("自定义封装没有可恢复的预设")
            return
        self._apply_selected_preset()

    def _browse_touchstone(self) -> None:
        path = filedialog.askopenfilename(parent=self, title="选择公共 Touchstone 文件",
                                          filetypes=(("Touchstone", "*.s2p *.s4p *.ts"), ("所有文件", "*.*")))
        if path:
            self.variables["capacitor_model.touchstone_file"].set(path)

    def _update_package_state(self) -> None:
        if not hasattr(self, "package_state_var"):
            return
        package = self._backend_value("capacitor.package")
        if package not in PACKAGE_PRESETS:
            self.package_state_var.set("自定义")
            return
        modified = False
        for key, expected in PACKAGE_PRESETS[package].items():
            if key == "package_metric":
                continue
            try:
                path = f"capacitor.{key}"
                shown_text = str(self.variables[path].get()).strip()
                if shown_text == self._length_display_snapshot.get(path) and path in self._length_mm_cache:
                    actual = self._length_mm_cache[path]
                else:
                    actual = convert_length(float(shown_text), self.display_unit, "mm")
                if abs(actual - float(expected)) > 1e-9:
                    modified = True
                    break
            except (KeyError, TypeError, ValueError):
                modified = True
                break
        metric = PACKAGE_PRESETS[package]["package_metric"]
        self.package_state_var.set(f"{package}（公制 {metric}）" + (" · 已修改" if modified else ""))

    def _set_derived_state(self, path: str, derived: bool, formula: str) -> None:
        widget = self.widgets.get(path)
        if isinstance(widget, ttk.Entry):
            widget.configure(state="readonly" if derived else "normal",
                             style="Derived.TEntry" if derived else "TEntry")
        label = self.formula_labels.get(path)
        if label:
            label.configure(text=formula)

    def _sync_derived_fields(self) -> None:
        pad_mode = self._backend_value("capacitor.pad_pitch_mode")
        bridge_mode = self._backend_value("capacitor.bridge_length_mode")
        self._set_derived_state("capacitor.pad_gap", pad_mode == "advanced_pitch",
                                "= pad_pitch − pad_length" if pad_mode == "advanced_pitch" else "用户输入")
        self._set_derived_state("capacitor.pad_pitch", pad_mode != "advanced_pitch",
                                "用户输入" if pad_mode == "advanced_pitch" else "= pad_length + pad_gap")
        self._set_derived_state("capacitor.bridge_length", bridge_mode == "auto",
                                "= pad_gap + 2 × overlap" if bridge_mode == "auto" else "用户输入")
        self._set_derived_state("trace.trace_gap", True, "= trace_pitch − trace_width")
        def length_mm(path: str) -> float:
            shown = str(self.variables[path].get()).strip()
            if (shown == self._length_display_snapshot.get(path)
                    and path in self._length_mm_cache):
                return self._length_mm_cache[path]
            value = convert_length(float(shown), self.display_unit, "mm")
            self._length_mm_cache[path] = value
            self._length_display_snapshot[path] = shown
            return value
        capacitor = {
            "pad_pitch_mode": pad_mode, "bridge_length_mode": bridge_mode,
            "pad_length": length_mm("capacitor.pad_length"),
            "pad_gap": length_mm("capacitor.pad_gap"),
            "pad_pitch": length_mm("capacitor.pad_pitch"),
            "bridge_length": length_mm("capacitor.bridge_length"),
        }
        derived = derive_capacitor_values(capacitor, length_mm("geometry.overlap"))
        targets = []
        if pad_mode == "advanced_pitch":
            targets.append("pad_gap")
        else:
            targets.append("pad_pitch")
        if bridge_mode == "auto":
            targets.append("bridge_length")
        self._ui_sync = True
        try:
            trace_gap = length_mm("trace.trace_pitch") - length_mm("trace.trace_width")
            trace_gap_display = format_length(trace_gap, self.display_unit)
            self._length_mm_cache["trace.trace_gap"] = trace_gap
            self._length_display_snapshot["trace.trace_gap"] = trace_gap_display
            self.variables["trace.trace_gap"].set(trace_gap_display)
            for key in targets:
                path = f"capacitor.{key}"
                displayed = format_length(derived[key], self.display_unit)
                self._length_mm_cache[path] = derived[key]
                self._length_display_snapshot[path] = displayed
                self.variables[path].set(displayed)
        finally:
            self._ui_sync = False
        escape_mode = self._backend_value("trace.pad_escape_mode")
        for path in ("trace.pad_lead_in_length", "trace.pad_lead_out_length"):
            widget = self.widgets.get(path)
            if widget is not None:
                widget.configure(state="normal" if escape_mode == "lead_then_45" else "disabled")
        transition_widget = self.widgets.get("trace.transition_length")
        if transition_widget is not None:
            transition_widget.configure(state="normal" if escape_mode == "smooth_s_bend" else "disabled")

    def _length_var_mm(self, path: str) -> float:
        shown = str(self.variables[path].get()).strip()
        if (shown == self._length_display_snapshot.get(path)
                and path in self._length_mm_cache):
            return self._length_mm_cache[path]
        value = convert_length(float(shown), self.display_unit, "mm")
        self._length_mm_cache[path] = value
        self._length_display_snapshot[path] = shown
        return value

    def _via_side_center_mm(self, side: str) -> float:
        width = self._length_var_mm(f"via_transition.{side}.width")
        gap_path = f"via_transition.{side}.gap"
        if gap_path in self.variables:
            return width + self._length_var_mm(gap_path)
        return self._length_var_mm("via_transition.pair_pitch")

    def _sync_via_derived_centers(self) -> None:
        for side, path in (
            ("entry_trace", "via_transition.entry_trace.center_to_center"),
            ("exit_trace", "via_transition.exit_trace.center_to_center"),
        ):
            if path not in self.variables:
                continue
            self._set_derived_state(path, True, "= 宽度 + GAP")
            try:
                center_mm = self._via_side_center_mm(side)
            except (TypeError, ValueError, tk.TclError, KeyError):
                continue
            display = format_length(center_mm, self.display_unit)
            previous = self._ui_sync
            self._ui_sync = True
            try:
                self._length_mm_cache[path] = center_mm
                self._length_display_snapshot[path] = display
                self.variables[path].set(display)
            finally:
                self._ui_sync = previous

    def _sync_via_fields(self) -> None:
        """集中处理 VIA 换层页的字段联动与派生值（改动 1–4）。

        在 ``_apply_scenario_linkage``（场景级禁用）之后调用：场景决定整页
        是否可编辑，本方法再按字段间依赖叠加单个控件的 state，并写入成品孔
        直径派生值，保证每次输入后立即生效且 ``_collect`` 往返兼容。
        """
        # 改动 1：非功能焊盘直径联动（remove 模式下该直径只存不画）。
        nfp_mode = self._backend_value("via_transition.padstack.nonfunctional_pad_mode")
        nfp_widget = self.widgets.get("via_transition.padstack.nonfunctional_pad_diameter")
        if nfp_widget is not None:
            try:
                nfp_widget.configure(state="normal" if nfp_mode == "keep" else "disabled")
            except tk.TclError:
                pass

        # 改动 2：背钻组联动（control_mode 已是静态标签，无需参与禁用）。
        backdrill_enabled = bool(self.variables["via_transition.backdrill.enabled"].get())
        for path in (
            "via_transition.backdrill.from_side",
            "via_transition.backdrill.drill_diameter",
            "via_transition.backdrill.residual_stub",
        ):
            widget = self.widgets.get(path)
            if widget is None:
                continue
            state = "readonly" if isinstance(widget, ttk.Combobox) else "normal"
            if not backdrill_enabled:
                state = "disabled"
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass

        # 改动 3：回流孔整组联动 + custom 坐标联动。
        return_enabled = bool(self.variables["via_transition.return_vias.enabled"].get())
        return_pattern = self._backend_value("via_transition.return_vias.pattern")
        return_group = (
            "via_transition.return_vias.pattern",
            "via_transition.return_vias.count",
            "via_transition.return_vias.offset_x",
            "via_transition.return_vias.offset_y",
            "via_transition.return_vias.pitch_x",
            "via_transition.return_vias.pitch_y",
            "via_transition.return_vias.start_layer",
            "via_transition.return_vias.end_layer",
            "via_transition.return_vias.connect_layers",
            "via_transition.return_vias.custom_coordinates",
            "via_transition.return_vias.drill_diameter",
            "via_transition.return_vias.finished_hole_diameter",
            "via_transition.return_vias.plating_thickness",
            "via_transition.return_vias.pad_diameter",
            "via_transition.return_vias.antipad_diameter",
        )
        for path in return_group:
            widget = self.widgets.get(path)
            if widget is None:
                continue
            layout_field = path.rsplit(".", 1)[-1]
            active_layout = {
                "side_pair": {"count", "offset_x", "offset_y", "pitch_y"},
                "four_corner": {"offset_x", "offset_y"},
                "custom": {"custom_coordinates"},
            }.get(return_pattern, set())
            if layout_field in {"count", "offset_x", "offset_y", "pitch_x", "pitch_y",
                                "custom_coordinates"}:
                enabled = return_enabled and layout_field in active_layout
            elif path in {"via_transition.return_vias.finished_hole_diameter",
                          "via_transition.return_vias.connect_layers"}:
                enabled = False
            else:
                enabled = return_enabled
            state = "readonly" if isinstance(widget, ttk.Combobox) else "normal"
            if not enabled:
                state = "disabled"
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass
        listbox = getattr(self, "return_connect_listbox", None)
        if listbox is not None:
            listbox.configure(state="normal" if return_enabled else "disabled")

        # Derived pattern counts are visible but never editable.
        if return_pattern in {"four_corner", "custom"}:
            derived_count = 4
            if return_pattern == "custom":
                raw = str(self.variables["via_transition.return_vias.custom_coordinates"].get())
                derived_count = len([item for item in raw.split(";") if item.strip()])
            count_var = self.variables["via_transition.return_vias.count"]
            if str(count_var.get()) != str(derived_count):
                self._ui_sync = True
                try:
                    count_var.set(str(derived_count))
                finally:
                    self._ui_sync = False

        # 扇出字段联动：任一端走线中心距 ≠ 孔间距时启用扇出；两端都相等则停用。
        try:
            svp = self._length_var_mm("via_transition.signal_via_pitch")
            entry_c = self._via_side_center_mm("entry_trace")
            exit_c = self._via_side_center_mm("exit_trace")
            fanout_active = (
                not math.isclose(svp, entry_c, abs_tol=1e-12)
                or not math.isclose(svp, exit_c, abs_tol=1e-12)
            )
        except (TypeError, ValueError, tk.TclError, KeyError):
            fanout_active = True
        for path in ("via_transition.fanout.style", "via_transition.fanout.direction",
                     "via_transition.fanout.straight_length"):
            widget = self.widgets.get(path)
            if widget is None:
                continue
            state = "readonly" if isinstance(widget, ttk.Combobox) else "normal"
            if not fanout_active:
                state = "disabled"
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass
        self._sync_via_derived_centers()

        # 改动 4：成品孔直径只读派生（= 钻孔 − 2×镀铜）。
        self._set_derived_state(
            "via_transition.padstack.finished_hole_diameter", True, "= 钻孔 − 2×镀铜"
        )
        try:
            drill_mm = convert_length(
                float(self.variables["via_transition.padstack.drill_diameter"].get()),
                self.display_unit, "mm")
            plating_mm = convert_length(
                float(self.variables["via_transition.padstack.plating_thickness"].get()),
                self.display_unit, "mm")
            finished_mm = drill_mm - 2.0 * plating_mm
            finished_display = format_length(finished_mm, self.display_unit)
            path = "via_transition.padstack.finished_hole_diameter"
            self._ui_sync = True
            try:
                self._length_mm_cache[path] = finished_mm
                self._length_display_snapshot[path] = finished_display
                self.variables[path].set(finished_display)
            finally:
                self._ui_sync = False
        except (TypeError, ValueError, tk.TclError):
            pass

        self._set_derived_state(
            "via_transition.return_vias.finished_hole_diameter", True,
            "= Return VIA 钻孔 − 2×镀铜")
        try:
            drill_mm = convert_length(
                float(self.variables["via_transition.return_vias.drill_diameter"].get()),
                self.display_unit, "mm")
            plating_mm = convert_length(
                float(self.variables["via_transition.return_vias.plating_thickness"].get()),
                self.display_unit, "mm")
            finished_mm = drill_mm - 2.0 * plating_mm
            finished_display = format_length(finished_mm, self.display_unit)
            path = "via_transition.return_vias.finished_hole_diameter"
            self._length_mm_cache[path] = finished_mm
            self._length_display_snapshot[path] = finished_display
            self._ui_sync = True
            try:
                self.variables[path].set(finished_display)
            finally:
                self._ui_sync = False
        except (TypeError, ValueError, tk.TclError):
            pass

    def _update_model_mode(self) -> str:
        mode = self._backend_value("capacitor_model.mode")
        for frame in self.model_frames.values():
            frame.pack_forget()
        self.model_frames.get(mode, self.model_frames["ideal_bridge"]).pack(fill="x", expand=True)
        bridge_enabled = mode == "ideal_bridge"
        for path in ("capacitor.bridge_width", "capacitor.bridge_length",
                     "capacitor.bridge_length_mode", "capacitor.bridge_material"):
            widget = self.widgets.get(path)
            if widget is None:
                continue
            if isinstance(widget, ttk.Entry) and path == "capacitor.bridge_length":
                state = ("readonly" if self._backend_value("capacitor.bridge_length_mode") == "auto"
                         else "normal") if bridge_enabled else "disabled"
            else:
                state = "normal" if bridge_enabled else "disabled"
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass
        return mode

    def _apply_capacitor_field_styles(self, issues: Sequence[Any]) -> None:
        for path, widget in self.widgets.items():
            if not path.startswith(("capacitor.", "capacitor_model.")) or not isinstance(widget, ttk.Entry):
                continue
            if str(widget.cget("state")) == "readonly":
                widget.configure(style="Derived.TEntry")
            else:
                widget.configure(style="TEntry")
        priority = {"warning": 1, "error": 2}
        selected: Dict[str, str] = {}
        for issue in issues:
            if issue.path and priority.get(issue.level, 0) >= priority.get(selected.get(issue.path, ""), 0):
                selected[issue.path] = issue.level
        for path, level in selected.items():
            widget = self.widgets.get(path)
            if isinstance(widget, ttk.Entry):
                widget.configure(style="Invalid.TEntry" if level == "error" else "Warning.TEntry")

    def _update_model_summary(self, config: Mapping[str, Any], touchstone: Any) -> None:
        cap, model = config["capacitor"], config["capacitor_model"]
        mode = model["mode"]
        if mode == "ideal_bridge":
            text = ("HFSS 实现：在每颗电容的两个焊盘之间创建导体桥接。\n"
                    f"桥接：{format_length(cap['bridge_width'], self.display_unit)} × "
                    f"{format_length(cap['bridge_length'], self.display_unit)} {self.display_unit}；"
                    f"长度模式：{ENUM_LABELS['capacitor.bridge_length_mode'].get(cap['bridge_length_mode'], cap['bridge_length_mode'])}。")
        elif mode == "lumped_rlc":
            srf = calculate_srf_ghz(float(model["capacitance_pf"]), float(model["esl_nh"]))
            text = ("HFSS 实现：每颗电容两焊盘之间施加相同的串联 RLC。\n"
                    f"C={model['capacitance_pf']:g} pF，ESR={model['esr_ohm']:g} Ω，ESL={model['esl_nh']:g} nH。\n"
                    f"近似自谐振频率 fSRF = 1/(2π√LC) ≈ {format_number(srf, 4) if srf else '不可计算'} GHz。")
        elif mode == "sparameter":
            text = "HFSS 实现：P、N 使用同一个 Touchstone 网络组件。\n"
            if touchstone:
                reference = (f"{format_number(touchstone.reference_ohm, 3)} Ω"
                             if touchstone.reference_ohm is not None else "未解析")
                text += f"{touchstone.ports} 端口；{touchstone.frequency_summary}；参考阻抗：{reference}。"
            else:
                text += "请选择并通过检查的公共 Touchstone 文件。"
        else:
            text = "HFSS 实现：仅创建分离焊盘，焊盘间隙保持开路；不创建铜桥、RLC或Touchstone组件。"
        self.model_summary_var.set(text)

    def _update_capacitor_feedback(self, config: Mapping[str, Any], validation: Any, geometry: Any) -> None:
        issues = validate_capacitor_editor(config, SOURCE_DIRECTORY)
        self._apply_capacitor_field_styles(issues)
        errors = list(validation.errors)
        warnings = list(validation.warnings)
        for issue in issues:
            target = errors if issue.level == "error" else warnings
            if issue.message not in target:
                target.append(issue.message)
        for message in geometry.errors:
            if message not in errors:
                errors.append(message)
        for message in geometry.warnings:
            if message not in warnings:
                warnings.append(message)
        if errors:
            self.cap_status_var.set(f"● {len(errors)} 个错误，无法生成 HFSS 脚本")
            self.cap_status_label.configure(style="StatusError.TLabel")
        elif warnings:
            self.cap_status_var.set(f"▲ 配置可用，但有 {len(warnings)} 个警告")
            self.cap_status_label.configure(style="StatusWarn.TLabel")
        else:
            self.cap_status_var.set("✓ 配置有效")
            self.cap_status_label.configure(style="StatusGood.TLabel")
        messages = errors[:2] or warnings[:2]
        if not messages:
            cap = config["capacitor"]
            messages = [f"自动结果：pad_pitch={format_length(cap['pad_pitch'], self.display_unit)} {self.display_unit}；"
                        f"pad_gap={format_length(cap['pad_gap'], self.display_unit)} {self.display_unit}；"
                        f"bridge_length={format_length(cap['bridge_length'], self.display_unit)} {self.display_unit}"]
        self.cap_detail_var.set("；".join(messages))
        touchstone = None
        if config["capacitor_model"]["mode"] == "sparameter":
            touchstone, touch_errors = inspect_touchstone(config["capacitor_model"]["touchstone_file"], SOURCE_DIRECTORY)
            if touchstone:
                reference = (f"；参考阻抗 {format_number(touchstone.reference_ohm, 3)} Ω"
                             if touchstone.reference_ohm is not None else "")
                self.touchstone_info_var.set(f"{touchstone.ports} 端口；{touchstone.frequency_summary}{reference}")
            else:
                self.touchstone_info_var.set("；".join(touch_errors))
        self.model_canvas.show(config, touchstone)
        self._update_model_summary(config, touchstone)
        self._update_package_state()

    def _update_via_editor(self, config: Mapping[str, Any], geometry: Any, validation: Any) -> None:
        """刷新 VIA 换层页的派生值、校验反馈与俯视图。"""
        if not hasattr(self, "via_feedback_var"):
            return
        from via_transition_semantics import validate_via_transition
        issues = validate_via_transition(config, geometry)
        errors = [(i[0], i[2]) for i in issues if i[1] == "error"]
        warnings = [(i[0], i[2]) for i in issues if i[1] == "warning"]
        for message in getattr(geometry, "errors", []):
            if not any(message == text for _, text in errors):
                errors.append(("VIA-GEO", message))
        for message in getattr(geometry, "warnings", []):
            if not any(message == text for _, text in warnings):
                warnings.append(("VIA-GEO", message))

        vt = config.get("via_transition", {})
        padstack = vt.get("padstack", {})
        drill = float(padstack.get("drill_diameter", 0.0) or 0.0)
        plating = float(padstack.get("plating_thickness", 0.0) or 0.0)
        finished = drill - 2.0 * plating
        lines = [f"成品孔径 = 钻孔 − 2×镀铜 = {format_length(finished, self.display_unit)} {self.display_unit}"]
        for via in getattr(geometry, "signal_vias", []):
            backdrill = "有" if getattr(via, "backdrilled", False) else "无"
            lines.append(f"{via.name}（{via.signal}）：输入={via.entry_layer} 输出={via.exit_layer}；"
                         f"背钻={backdrill}；残桩={format_length(getattr(via, 'actual_residual_stub', 0.0), self.display_unit)} {self.display_unit}")
        if getattr(geometry, "return_vias", []):
            lines.append(f"回流地孔（Return VIA）数量：{len(geometry.return_vias)}")
        if getattr(geometry, "ports", []):
            lines.append(f"端口：{len(geometry.ports)} 个；差分对：{len(getattr(geometry, 'differential_pairs', []))} 个")
        if errors:
            lines.append(f"错误 {len(errors)}：")
            for rule_id, text in errors[:6]:
                lines.append(f"  [{rule_id}] {text}")
        elif warnings:
            lines.append(f"警告 {len(warnings)}：")
            for rule_id, text in warnings[:4]:
                lines.append(f"  [{rule_id}] {text}")
        else:
            lines.append("✓ VIA 配置有效")
        self.via_feedback_var.set("\n".join(lines))
        self._via_last_render = (config, geometry)
        self._draw_via_top_view(config, geometry)
        self._draw_via_cross_section(config, geometry)

    def _draw_via_top_view(self, config: Mapping[str, Any], geometry: Any) -> None:
        canvas = getattr(self, "via_top_canvas", None)
        if canvas is None:
            return
        canvas.delete("all")
        viewport = canvas_viewport(canvas)
        if viewport is None:
            return
        width, height = viewport
        # 俯视图需同时容纳信号孔、回流孔与走线，优先用 plane_bounds（包含回流孔），
        # 否则回流孔会落在 model_bounds 之外被裁切。
        bounds = getattr(geometry, "plane_bounds", None) or getattr(geometry, "model_bounds", None)
        cx = getattr(bounds, "center", None)
        if bounds is None or cx is None or getattr(bounds, "width", 0.0) <= 0:
            canvas.create_text(width / 2, height / 2, text="暂无有效几何", fill="#94a3b8")
            return
        # 分别按宽/高计算缩放，避免「宽而扁」的几何因 max(width, height)
        # 被压成一条细线，导致标注相对几何过大、互相重叠。
        scale = min(
            max(1.0, width - 60) / max(float(bounds.width), 1e-9),
            max(1.0, height - 40) / max(float(bounds.height), 1e-9),
        )
        ox = width / 2 - float(cx.x) * scale
        oy = height / 2 + float(cx.y) * scale

        def xy(x: float, y: float) -> Tuple[float, float]:
            return ox + x * scale, oy - y * scale

        x1, y1 = xy(bounds.x_min, bounds.y_max)
        x2, y2 = xy(bounds.x_max, bounds.y_min)
        canvas.create_rectangle(x1, y1, x2, y2, fill="#eef2f7", outline="#64748b",
                                tags=("via-board",))

        trace_colors = {"P": "#dc2626", "N": "#2563eb"}
        for trace in list(getattr(geometry, "entry_traces", [])) + list(getattr(geometry, "exit_traces", [])):
            points = trace.get("points", []) if isinstance(trace, Mapping) else []
            if not points:
                continue
            color = trace_colors.get(str(trace.get("signal", "")), "#334155")
            canvas.create_polygon([coord for p in points for coord in xy(float(p[0]), float(p[1]))],
                                  fill=color, outline="black", tags=("via-geometry", "via-trace"))

        layers = list(config.get("stackup", []))
        selected_index = getattr(getattr(self, "antipad_workbench", None), "selected_index", 0)
        selected_layer = (str(layers[min(selected_index, len(layers) - 1)]["name"])
                          if layers else "")
        contours = [contour for contour in getattr(geometry, "antipad_contours", [])
                    if contour.layer == selected_layer]
        validation = getattr(getattr(self, "antipad_workbench", None), "validation", None)
        invalid = any(getattr(item, "rule_id", "") == "VIA-TRN-090"
                      and selected_layer in getattr(item, "message", "")
                      for item in getattr(validation, "items", []))
        for contour in contours:
            canvas.create_polygon(
                [coord for point in contour.points for coord in xy(point[0], point[1])],
                fill="", outline="#dc2626" if invalid else "#f59e0b", width=2,
                dash=(4, 2), tags=("via-geometry", "via-antipad", "canonical-antipad"))

        for via in getattr(geometry, "signal_vias", []):
            x, y = xy(float(via.x), float(via.y))
            r_pad = float(via.connected_pad_diameter) * scale / 2
            r_hole = float(via.drill_diameter) * scale / 2
            canvas.create_oval(x - r_pad, y - r_pad, x + r_pad, y + r_pad,
                               fill=trace_colors.get(getattr(via, "signal", ""), "#dc2626"), outline="black",
                               tags=("via-geometry", "via-pad"))
            canvas.create_oval(x - r_hole, y - r_hole, x + r_hole, y + r_hole, fill="white", outline="black",
                               tags=("via-geometry", "via-hole"))

        for via in getattr(geometry, "return_vias", []):
            x, y = xy(float(via.x), float(via.y))
            r_anti = float(via.antipad_diameter) * scale / 2
            r_pad = float(via.pad_diameter) * scale / 2
            r_hole = float(via.drill_diameter) * scale / 2
            canvas.create_oval(x - r_anti, y - r_anti, x + r_anti, y + r_anti, outline="#f59e0b", dash=(3, 2),
                               tags=("via-geometry", "via-antipad"))
            canvas.create_oval(x - r_pad, y - r_pad, x + r_pad, y + r_pad, fill="#d6a11d", outline="#713f12",
                               tags=("via-geometry", "via-pad"))
            canvas.create_oval(x - r_hole, y - r_hole, x + r_hole, y + r_hole, fill="white", outline="black",
                               tags=("via-geometry", "via-hole"))

        hud_text = ellipsize(
            canvas,
            f"VIA 俯视图 | 单位 {self.display_unit} | 信号孔={len(getattr(geometry, 'signal_vias', []))} "
            f"回流孔={len(getattr(geometry, 'return_vias', []))} | 点击标注定位参数",
            ("TkDefaultFont", 10, "bold"), max(1, width - 28),
        )
        canvas.create_text(14, 12, anchor="nw",
                           text=hud_text,
                           font=("TkDefaultFont", 10, "bold"), fill="#172033", tags=("hud-label",))

        self._draw_via_dimensions(canvas, config, geometry, xy, width, height)

    def _draw_via_dimensions(self, canvas: tk.Canvas, config: Mapping[str, Any],
                             geometry: Any, xy: Any, width: int, height: int) -> None:
        """叠加 VIA 俯视图尺寸标注（改动 6）。

        标注值一律取自已校验的配置（内部 mm），用 ``format_length`` 换算到当前
        显示单位；每条标注携带 ``dimension:<path>`` 标签供点击跳转。
        """
        vt = config.get("via_transition", {})
        unit = self.display_unit

        def fmt(mm_value: float) -> str:
            return format_length(mm_value, unit)

        center_x = float(vt.get("center_x", 0.0))
        center_y = float(vt.get("center_y", 0.0))
        pair_pitch = float(vt.get("pair_pitch", 0.0))
        signal_via_pitch = float(vt.get("signal_via_pitch", pair_pitch))
        entry = vt.get("entry_trace", {}) if isinstance(vt.get("entry_trace"), Mapping) else {}
        exit_tr = vt.get("exit_trace", {}) if isinstance(vt.get("exit_trace"), Mapping) else {}
        entry_w = float(entry.get("width", 0.0))
        entry_len = float(entry.get("length", 0.0))
        entry_pitch = via_trace_center_pitch(entry, pair_pitch)
        entry_gap = entry_pitch - entry_w
        exit_w = float(exit_tr.get("width", 0.0))
        exit_len = float(exit_tr.get("length", 0.0))
        exit_pitch = via_trace_center_pitch(exit_tr, pair_pitch)
        exit_gap = exit_pitch - exit_w
        fanout_cfg = vt.get("fanout") if isinstance(vt.get("fanout"), Mapping) else {}
        fanout_style = str(fanout_cfg.get("style", "deg45"))
        fanout_direction = str(fanout_cfg.get("direction", "auto"))
        entry_trace_y = entry_pitch / 2.0
        exit_trace_y = exit_pitch / 2.0

        placer = CanvasLabelPlacer(canvas, width, height, safe=10)
        # 先把已绘制的走线/信号孔/回流孔包围盒标记为占位，使标签自动避让图形。
        # 注意：板框（via-board）是背景矩形、几乎铺满画布，不参与占位，
        # 否则会把所有标签都挤没。
        for item in canvas.find_withtag("via-geometry"):
            placer.reserve(canvas.bbox(item))
        # HUD 标题同样占位，避免被推到左上角的标注覆盖。
        for item in canvas.find_withtag("hud-label"):
            placer.reserve(canvas.bbox(item))

        # (path, label, p1, p2, dashed, lane)
        dims: List[Tuple[str, str, Tuple[float, float], Tuple[float, float], bool, int]] = []
        dims.append(("via_transition.entry_trace.center_to_center",
                     f"输入中心距 {fmt(entry_pitch)} {unit}",
                     (center_x - entry_len, center_y - entry_pitch / 2.0),
                     (center_x - entry_len, center_y + entry_pitch / 2.0), False, 0))
        dims.append(("via_transition.exit_trace.center_to_center",
                     f"输出中心距 {fmt(exit_pitch)} {unit}",
                     (center_x + exit_len, center_y - exit_pitch / 2.0),
                     (center_x + exit_len, center_y + exit_pitch / 2.0), False, 0))
        # ㉑ 信号孔中心距：标注在孔端，与走线中心距相互独立。
        dims.append(("via_transition.signal_via_pitch",
                     f"㉑ 信号孔间距 {fmt(signal_via_pitch)} {unit}",
                     (center_x, center_y - signal_via_pitch / 2.0),
                     (center_x, center_y + signal_via_pitch / 2.0), False, 0))
        if entry_gap > 0.0:
            dims.append(("via_transition.entry_trace.gap",
                         f"输入 GAP {fmt(entry_gap)} {unit}",
                         (center_x - entry_len, center_y + entry_pitch / 2.0 - entry_w / 2.0),
                         (center_x - entry_len, center_y - entry_pitch / 2.0 + entry_w / 2.0),
                         True, 0))
        if exit_gap > 0.0:
            dims.append(("via_transition.exit_trace.gap",
                         f"输出 GAP {fmt(exit_gap)} {unit}",
                         (center_x + exit_len, center_y + exit_pitch / 2.0 - exit_w / 2.0),
                         (center_x + exit_len, center_y - exit_pitch / 2.0 + exit_w / 2.0),
                         True, 0))
        dims.append(("via_transition.entry_trace.width",
                     f"⑧ 输入宽 {fmt(entry_w)} {unit}",
                     (center_x - entry_len / 2.0, center_y + entry_trace_y - entry_w / 2.0),
                     (center_x - entry_len / 2.0, center_y + entry_trace_y + entry_w / 2.0),
                     False, 0))
        dims.append(("via_transition.entry_trace.length",
                     f"⑦ 输入长 {fmt(entry_len)} {unit}",
                     (center_x - entry_len, center_y + entry_trace_y + entry_w / 2.0),
                     (center_x, center_y + entry_trace_y + entry_w / 2.0),
                     False, 1))
        dims.append(("via_transition.exit_trace.width",
                     f"⑩ 输出宽 {fmt(exit_w)} {unit}",
                     (center_x + exit_len / 2.0, center_y + exit_trace_y - exit_w / 2.0),
                     (center_x + exit_len / 2.0, center_y + exit_trace_y + exit_w / 2.0),
                     False, 0))
        dims.append(("via_transition.exit_trace.length",
                     f"⑨ 输出长 {fmt(exit_len)} {unit}",
                     (center_x, center_y + exit_trace_y + exit_w / 2.0),
                     (center_x + exit_len, center_y + exit_trace_y + exit_w / 2.0),
                     False, 1))
        # 扇出标注：任一端孔间距 ≠ 走线中心距时，在该端标注 45°/弧扇出段与耦合直线段。
        style_label = "弧" if fanout_style == "arc" else "45°"
        for _side, length, pitch, x_sign in (
            ("entry", entry_len, entry_pitch, -1.0),
            ("exit", exit_len, exit_pitch, 1.0),
        ):
            if math.isclose(signal_via_pitch, pitch, abs_tol=1e-9):
                continue
            fanout_delta = (signal_via_pitch - pitch) / 2.0
            fanout_x = abs(fanout_delta)
            direction_label = "向外" if fanout_delta > 0 else "向内"
            if fanout_direction != "auto":
                direction_label = "向外" if fanout_direction == "expand" else "向内"
            fold_x = center_x + x_sign * fanout_x
            port_x = center_x + x_sign * length
            dims.append(("via_transition.fanout.style",
                         f"㉒ {style_label}扇出·{direction_label}",
                         (center_x, center_y + signal_via_pitch / 2.0),
                         (fold_x, center_y + pitch / 2.0), True, 1))
            straight_len = max(0.0, length - fanout_x)
            dims.append(("via_transition.fanout.straight_length",
                         f"㉔ 耦合直线 {fmt(straight_len)} {unit}",
                         (fold_x, center_y + pitch / 2.0),
                         (port_x, center_y + pitch / 2.0), False, 2))

        signal_vias = list(getattr(geometry, "signal_vias", []))
        if signal_vias:
            via = signal_vias[0]
            vy = float(via.y)
            dims.append(("via_transition.padstack.drill_diameter",
                         f"⑪ 钻孔 {fmt(float(via.drill_diameter))} {unit}",
                         (float(via.x) - float(via.drill_diameter) / 2.0, vy),
                         (float(via.x) + float(via.drill_diameter) / 2.0, vy), False, 0))
            dims.append(("via_transition.padstack.connected_pad_diameter",
                         f"⑭ 焊盘 {fmt(float(via.connected_pad_diameter))} {unit}",
                         (float(via.x) - float(via.connected_pad_diameter) / 2.0, vy),
                         (float(via.x) + float(via.connected_pad_diameter) / 2.0, vy), False, 1))
            contours = list(getattr(geometry, "antipad_contours", []))
            contour = next((item for item in contours if item.owner in {via.signal, "merged_pair"}), None)
            if contour is not None:
                span = float(contour.bounds[1] - contour.bounds[0])
                shape_text = (f"Ø{fmt(contour.diameter)}" if contour.shape == "circle"
                              else f"{contour.shape} {fmt(contour.length)}×{fmt(contour.width)} @{contour.rotation_deg:g}°")
                dims.append(("via_transition.padstack.antipad.diameter",
                             f"⑯ 反焊盘 {shape_text} {unit}",
                             (float(contour.bounds[0]), float(contour.center_y)),
                             (float(contour.bounds[1]), float(contour.center_y)), False, 2))

        return_vias = list(getattr(geometry, "return_vias", []))
        if return_vias:
            via = return_vias[0]
            vy = float(via.y)
            dims.append(("via_transition.return_vias.pad_diameter",
                         f"⑲ 回流焊盘 {fmt(float(via.pad_diameter))} {unit}",
                         (float(via.x) - float(via.pad_diameter) / 2.0, vy),
                         (float(via.x) + float(via.pad_diameter) / 2.0, vy), False, 0))
            dims.append(("via_transition.return_vias.antipad_diameter",
                         f"⑳ 回流避空 {fmt(float(via.antipad_diameter))} {unit}",
                         (float(via.x) - float(via.antipad_diameter) / 2.0, vy),
                         (float(via.x) + float(via.antipad_diameter) / 2.0, vy), False, 1))

        for (path, label, p1, p2, dashed, lane) in dims:
            self._draw_via_dimension(canvas, placer, xy, path, label, p1, p2,
                                     dashed=dashed, lane=lane)

    def _draw_via_dimension(self, canvas: tk.Canvas, placer: CanvasLabelPlacer,
                            xy: Any, path: str, label: str, p1: Tuple[float, float],
                            p2: Tuple[float, float], *, dashed: bool, lane: int) -> None:
        """绘制一条尺寸线 + 引线 + 可点击标签。"""
        ax, ay = xy(*p1)
        bx, by = xy(*p2)
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        if length < 1e-6:
            return
        px, py = -dy / length, dx / length
        midx, midy = (ax + bx) / 2.0, (ay + by) / 2.0
        # 让标注线朝远离画布中心的方向偏移，避免压在图元上。
        outward_x = midx - float(placer.width) / 2.0
        outward_y = midy - float(placer.height) / 2.0
        if px * outward_x + py * outward_y < 0:
            px, py = -px, -py
        # 引线/尺寸线偏移随画布宽度自适应，避免窄画布下把标签推进几何。
        track = max(10, min(24, int(placer.width / 28)))
        dist = 5.0 + lane * track
        a2 = (ax + px * dist, ay + py * dist)
        b2 = (bx + px * dist, by + py * dist)
        highlighted = self._via_highlight_path == path
        color = "#dc2626" if highlighted else ("#64748b" if dashed else "#334155")
        tags = ("dimension-label", "dimension:" + path)
        canvas.create_line(ax, ay, a2[0], a2[1], fill="#94a3b8", dash=(2, 2), tags=tags)
        canvas.create_line(bx, by, b2[0], b2[1], fill="#94a3b8", dash=(2, 2), tags=tags)
        line_kwargs: Dict[str, Any] = {"fill": color, "width": 3 if highlighted else 1}
        if dashed:
            line_kwargs["dash"] = (3, 3)
        if length >= 8:
            line_kwargs["arrow"] = "both"
        canvas.create_line(a2[0], a2[1], b2[0], b2[1], tags=tags, **line_kwargs)
        midpoint = ((a2[0] + b2[0]) / 2.0, (a2[1] + b2[1]) / 2.0)
        # 在尺寸线法线与沿线四个方向由近及远提供候选位，优先放在图元外侧空白处；
        # 放不下时由 CanvasLabelPlacer 自动隐藏次要标注（hidden 列表）。
        ux, uy = dx / length, dy / length
        directions = ((px, py), (-px, -py), (ux, uy), (-ux, -uy))
        candidates = [midpoint]
        for mult in (1.0, 1.8, 2.8, 4.0):
            for dirx, diry in directions:
                candidates.append((midpoint[0] + dirx * track * mult, midpoint[1] + diry * track * mult))
        placer.place(
            label,
            candidates,
            font=("TkDefaultFont", 8, "bold" if highlighted else "normal"),
            fill=color, background="white", tags=tags,
            leader=midpoint, leader_color=color, priority=2,
        )

    def _on_via_dimension_click(self, event: tk.Event) -> None:
        path = self._dimension_path_from_event(event)
        if path:
            self._navigate_via_parameter(path)

    def _on_via_dimension_enter(self, event: tk.Event) -> None:
        try:
            event.widget.configure(cursor="hand2")
        except tk.TclError:
            pass

    def _on_via_dimension_leave(self, event: tk.Event) -> None:
        try:
            event.widget.configure(cursor="")
        except tk.TclError:
            pass

    def _on_via_cross_wheel(self, event: tk.Event) -> str:
        try:
            event.widget.yview_scroll(int(-event.delta / 120), "units")
        except tk.TclError:
            pass
        return "break"

    def _dimension_path_from_event(self, event: tk.Event) -> Optional[str]:
        canvas = event.widget
        for tag in canvas.gettags("current"):
            tag = str(tag)
            if tag.startswith("dimension:"):
                return tag[len("dimension:"):]
        return None

    def _navigate_via_parameter(self, path: str) -> None:
        """点击标注后定位到对应输入框（优先复用 V2 的通用跳转）。"""
        navigate = getattr(self, "_navigate_to_parameter", None)
        if navigate is not None:
            navigate(path)
            return
        widget = self.widgets.get(path)
        if widget is None:
            return
        self.notebook.select(self.tabs["VIA 换层"])

        def reveal() -> None:
            self.via_page_scroller.scroll_to_widget(widget)
            widget.focus_set()
            self.status.set(f"已定位参数：{path}")

        self.after_idle(reveal)

    # ---- 过孔截面示意（方案 B 的"图"）+ 双向点击联动 ----

    def _draw_via_cross_section(self, config: Mapping[str, Any], geometry: Any) -> None:
        """绘制 Z 轴过孔截面示意图（叠层 + 信号孔桶 + 焊盘 + 反焊盘 + 背钻）。"""
        canvas = getattr(self, "via_cross_canvas", None)
        if canvas is None:
            return
        canvas.delete("all")
        viewport = canvas_viewport(canvas)
        if viewport is None:
            return
        width, height = viewport
        if geometry is None:
            canvas.create_text(width / 2, height / 2, text="暂无有效几何", fill="#94a3b8")
            return
        layers = list(getattr(geometry, "stackup", []))
        signal_vias = list(getattr(geometry, "signal_vias", []))
        if not layers or not signal_vias:
            canvas.create_text(width / 2, height / 2, text="暂无有效几何", fill="#94a3b8")
            return

        unit = self.display_unit
        hl = self._via_highlight_path
        via0 = signal_vias[0]
        return_vias = list(getattr(geometry, "return_vias", []))

        def fmtv(mm_value: float) -> str:
            return format_length(mm_value, unit)

        pad = 12
        title_font = ("TkDefaultFont", 10, "bold")
        label_font = "TkDefaultFont"
        small_font = ("TkDefaultFont", 8)
        title_line = font_linespace(canvas, title_font)
        label_line = font_linespace(canvas, label_font)
        small_line = font_linespace(canvas, small_font)

        # 叠层带（可读性增强，非严格厚度比例）。
        # 左侧预留「层标记槽」与「层名槽」，把过孔桶画在槽右侧，避免层名/标记与桶重叠。
        # 槽宽按实际测量文字宽度分配，避免高 DPI/大字号下固定像素估算被裁。
        row_minimum = max(small_line + 4, 16)
        total = sum(float(layer.thickness) for layer in layers) or 1.0
        marker_font = ("TkDefaultFont", 9, "bold")
        marker_labels = ["③ 输入", "④ 输出", "⑤ 钻孔起", "⑥ 钻孔止"]
        marker_width = max((text_size(canvas, label, marker_font)[0] for label in marker_labels), default=0)
        # Entry/exit and drill-span markers commonly name the same physical
        # layer (for example L1 is both entry and drill start).  Reserve two
        # horizontal marker lanes so those labels never render on top of one
        # another at normal or high-DPI font sizes.
        marker_lane_gap = 12
        marker_gutter = int(marker_width) * 2 + marker_lane_gap + 24
        layer_name_texts = [f"{layer.name}·{layer.layer_type}" for layer in layers]
        max_name_width = max((text_size(canvas, text, small_font)[0] for text in layer_name_texts), default=0)
        name_cap = max(60, int((width - pad * 2 - marker_gutter) * 0.42))
        layer_name_gutter = max(60, int(min(max_name_width + 8, name_cap)))
        x1 = pad + marker_gutter + layer_name_gutter
        band_available = max(80, width - x1 - pad)
        geometry_width = min(320, band_available)
        x2 = x1 + geometry_width
        title_y = pad
        note_y = title_y + title_line + 3
        y = note_y + label_line + 9
        readable_target = max(height - y - title_line - pad * 2, len(layers) * row_minimum)
        proportional_budget = max(readable_target, len(layers) * row_minimum * 1.25)
        heights = [max(float(row_minimum), proportional_budget * float(layer.thickness) / total)
                   for layer in layers]
        colors = {"signal": "#fef3c7", "plane": "#fde68a", "dielectric": "#bae6fd", "soldermask": "#bbf7d0"}
        layer_boxes: List[Tuple[Any, float, float]] = []
        for layer, layer_height in zip(layers, heights):
            top, bottom = y, y + layer_height
            canvas.create_rectangle(x1, top, x2, bottom, fill=colors.get(layer.layer_type, "#e2e8f0"),
                                    outline="#64748b", tags=("layer-row",))
            layer_text = ellipsize(canvas, f"{layer.name}·{layer.layer_type}",
                                   small_font, layer_name_gutter - 8)
            canvas.create_text(x1 - 6, (top + bottom) / 2, anchor="e",
                               text=layer_text, fill="#172033",
                               font=small_font, tags=("layer-label",))
            layer_boxes.append((layer, top, bottom))
            y = bottom

        layer_index = {str(layer.name): (layer, top, bottom) for layer, top, bottom in layer_boxes}

        def z_to_y(z_value: float) -> float:
            for layer, top, bottom in layer_boxes:
                if layer.z_bottom - 1e-9 <= z_value <= layer.z_top + 1e-9:
                    span = float(layer.z_top - layer.z_bottom)
                    if span <= 1e-12:
                        return (top + bottom) / 2.0
                    frac = (float(layer.z_top) - z_value) / span
                    return top + frac * (bottom - top)
            if z_value >= float(layers[0].z_top):
                return float(layer_boxes[0][1])
            if z_value <= float(layers[-1].z_bottom):
                return float(layer_boxes[-1][2])
            return float((layer_boxes[0][1] + layer_boxes[-1][2]) / 2.0)

        # 水平布局：信号孔与回流孔按各自最大直径放入层带内，二者自适应不溢出。
        signal_diam = max(float(via0.antipad_diameter or 0.0),
                          float(via0.connected_pad_diameter or 0.0),
                          float(via0.backdrill_diameter or 0.0),
                          float(via0.drill_diameter or 0.0), 0.2)
        return_diam = 0.0
        for rv in return_vias:
            return_diam = max(return_diam, float(rv.antipad_diameter or 0.0),
                              float(rv.pad_diameter or 0.0), float(rv.drill_diameter or 0.0))
        has_return = bool(return_vias) and return_diam > 0.0
        gap_mm = 0.12 if has_return else 0.0
        total_span_mm = signal_diam + gap_mm + return_diam if has_return else signal_diam
        px_per_mm = max(20.0, (geometry_width - 8) / total_span_mm)
        center_px = x1 + 4 + signal_diam / 2.0 * px_per_mm
        return_px = center_px + (signal_diam / 2.0 + gap_mm + return_diam / 2.0) * px_per_mm

        def num_tags(path: str) -> Tuple[str, ...]:
            if not path:
                return ()
            return ("dimension-label", "dimension:" + path, "param:" + path)

        def hlc(path: str, base: str) -> str:
            return "#dc2626" if hl == path else base

        def hlw(path: str, base: int) -> int:
            return 3 if hl == path else base

        def draw_barrel(cx: float, z_bottom: float, z_top: float, drill: float, finished: float,
                        fill: str, drill_path: str, finish_path: str, tags: Sequence[str] = ()) -> None:
            yb, yt = z_to_y(z_bottom), z_to_y(z_top)
            if yb < yt:
                yb, yt = yt, yb
            half = max(1.0, drill * px_per_mm / 2.0)
            canvas.create_rectangle(cx - half, yt, cx + half, yb, fill=fill,
                                    outline=hlc(drill_path, "#7c2d12"), width=hlw(drill_path, 1),
                                    tags=num_tags(drill_path) + tuple(tags) + ("via-barrel",))
            if finished > 0.0 and finished < drill:
                hole_half = max(0.5, finished * px_per_mm / 2.0)
                canvas.create_rectangle(cx - hole_half, yt, cx + hole_half, yb, fill="white",
                                        outline=hlc(finish_path, "#334155"), width=hlw(finish_path, 1),
                                        tags=num_tags(finish_path) + tuple(tags) + ("via-hole",))

        def draw_pad(cx: float, top: float, bottom: float, diameter: float, path: str,
                     fill: str, outline: str, tags: Sequence[str] = ()) -> None:
            half = max(1.0, diameter * px_per_mm / 2.0)
            canvas.create_rectangle(cx - half, top + 1, cx + half, bottom - 1, fill=fill,
                                    outline=hlc(path, outline), width=hlw(path, 1),
                                    tags=num_tags(path) + tuple(tags))

        def draw_antipad(cx: float, top: float, bottom: float, diameter: float, path: str) -> None:
            half = max(1.0, diameter * px_per_mm / 2.0)
            canvas.create_rectangle(cx - half, top + 1, cx + half, bottom - 1, fill="#ffffff", outline="",
                                    tags=num_tags(path) + ("antipad",))
            canvas.create_rectangle(cx - half, top + 1, cx + half, bottom - 1,
                                    outline=hlc(path, "#f59e0b"), width=hlw(path, 1), dash=(3, 2),
                                    tags=num_tags(path) + ("antipad",))

        # 信号孔桶与背钻。
        if via0.backdrilled:
            draw_barrel(center_px, float(via0.effective_barrel_z_bottom), float(via0.effective_barrel_z_top),
                        float(via0.drill_diameter), float(via0.finished_hole_diameter), "#d97706",
                        "via_transition.padstack.drill_diameter", "via_transition.padstack.finished_hole_diameter")
            bd_half = max(1.0, float(via0.backdrill_diameter or 0.0) * px_per_mm / 2.0)
            if via0.backdrill_from_side == "bottom":
                y1, y2 = z_to_y(float(via0.effective_barrel_z_bottom)), z_to_y(float(via0.z_bottom))
            else:
                y1, y2 = z_to_y(float(via0.z_top)), z_to_y(float(via0.effective_barrel_z_top))
            if y2 < y1:
                y1, y2 = y2, y1
            canvas.create_rectangle(center_px - bd_half, y1, center_px + bd_half, y2, fill="", outline="#dc2626",
                                    width=2, dash=(5, 3), tags=num_tags("via_transition.backdrill.drill_diameter") + ("backdrill",))
            canvas.create_text(center_px, y1 + 10, anchor="w", text="背钻段", fill="#dc2626",
                               font=small_font, tags=("hud-label",))
        else:
            draw_barrel(center_px, float(via0.z_bottom), float(via0.z_top),
                        float(via0.drill_diameter), float(via0.finished_hole_diameter), "#d97706",
                        "via_transition.padstack.drill_diameter", "via_transition.padstack.finished_hole_diameter")

        # 功能焊盘 / 非功能焊盘 / 反焊盘。
        for name in getattr(via0, "functional_pad_layers", []):
            entry = layer_index.get(str(name))
            if entry is not None:
                _layer, top, bottom = entry
                draw_pad(center_px, top, bottom, float(via0.connected_pad_diameter),
                         "via_transition.padstack.connected_pad_diameter", "#dc2626", "#7f1d1d",
                         ("functional-pad",))
        if getattr(via0, "nonfunctional_pad_mode", "remove") == "keep":
            for name in getattr(via0, "nonfunctional_pad_layers", []):
                entry = layer_index.get(str(name))
                if entry is not None:
                    _layer, top, bottom = entry
                    draw_pad(center_px, top, bottom, float(via0.nonfunctional_pad_diameter),
                             "via_transition.padstack.nonfunctional_pad_diameter", "#f59e0b", "#92400e",
                             ("nonfunctional-pad",))
        for name in getattr(via0, "antipad_layers", []):
            entry = layer_index.get(str(name))
            if entry is not None:
                _layer, top, bottom = entry
                contours = [contour for contour in getattr(geometry, "antipad_contours", [])
                            if contour.layer == str(name)]
                if contours:
                    contour = contours[0]
                    diameter = max(float(contour.bounds[1] - contour.bounds[0]),
                                   float(contour.bounds[3] - contour.bounds[2]))
                    draw_antipad(center_px, top, bottom, diameter,
                                 "via_transition.padstack.antipad.diameter")

        # 回流孔（灰桶 + 焊盘 + 避空）。
        if return_vias:
            rv0 = return_vias[0]
            draw_barrel(return_px, float(rv0.z_bottom), float(rv0.z_top),
                        float(rv0.drill_diameter), float(rv0.finished_hole_diameter), "#94a3b8",
                        "", "", ("return-via-barrel",))
            for name in getattr(rv0, "connect_layers", []):
                entry = layer_index.get(str(name))
                if entry is not None:
                    _layer, top, bottom = entry
                    draw_pad(return_px, top, bottom, float(rv0.pad_diameter),
                             "via_transition.return_vias.pad_diameter", "#cbd5e1", "#475569", ("return-pad",))
            for name in getattr(rv0, "clearance_layers", []):
                entry = layer_index.get(str(name))
                if entry is not None:
                    _layer, top, bottom = entry
                    draw_antipad(return_px, top, bottom, float(rv0.antipad_diameter),
                                 "via_transition.return_vias.antipad_diameter")

        # entry/exit/drill_start/drill_end 层标记（编号 ③④⑤⑥）。
        def mark_layer(name: str, label: str, color: str, path: str, lane: int) -> None:
            entry = layer_index.get(str(name))
            if entry is None:
                return
            _layer, top, bottom = entry
            mid = (top + bottom) / 2.0
            # 标记文字放在最左「标记槽」内右对齐，短箭头指向层带，永不 <0 被裁。
            marker_right = (pad + marker_gutter - 16
                            - lane * (int(marker_width) + marker_lane_gap))
            canvas.create_text(marker_right, mid, anchor="e", text=label, fill=color,
                               font=marker_font, tags=num_tags(path) + ("entry-exit-marker",))
            canvas.create_line(marker_right + 2, mid, marker_right + 12, mid, fill=color,
                               width=2, arrow="last", tags=num_tags(path) + ("entry-exit-marker",))

        mark_layer(getattr(via0, "entry_layer", ""), "③ 输入", "#166534",
                   "via_transition.entry_layer", 0)
        mark_layer(getattr(via0, "exit_layer", ""), "④ 输出", "#b45309",
                   "via_transition.exit_layer", 0)
        mark_layer(getattr(via0, "drill_start_layer", ""), "⑤ 钻孔起", "#0f766e",
                   "via_transition.drill_start_layer", 1)
        mark_layer(getattr(via0, "drill_end_layer", ""), "⑥ 钻孔止", "#7c3aed",
                   "via_transition.drill_end_layer", 1)

        # 下方编号图例（number → 参数名 → 数值），可点击跳转，整行换行排列不横向溢出。
        vt = config.get("via_transition", {})
        padstack = vt.get("padstack", {})
        bd = vt.get("backdrill", {})
        rv_cfg = vt.get("return_vias", {})
        legend: List[Tuple[str, str, str]] = []
        legend.append(("⑪", "via_transition.padstack.drill_diameter",
                       f"钻孔 {fmtv(float(padstack.get('drill_diameter', 0.0)))} {unit}"))
        legend.append(("⑫", "via_transition.padstack.finished_hole_diameter",
                       f"成品孔 {fmtv(float(padstack.get('finished_hole_diameter', 0.0)))} {unit}"))
        legend.append(("⑬", "via_transition.padstack.plating_thickness",
                       f"镀铜 {fmtv(float(padstack.get('plating_thickness', 0.0)))} {unit}"))
        legend.append(("⑭", "via_transition.padstack.connected_pad_diameter",
                       f"功能焊盘 {fmtv(float(padstack.get('connected_pad_diameter', 0.0)))} {unit}"))
        legend.append(("⑮", "via_transition.padstack.nonfunctional_pad_diameter",
                       f"非功能焊盘 {fmtv(float(padstack.get('nonfunctional_pad_diameter', 0.0)))} {unit}"))
        anti = padstack.get("antipad", {})
        anti_value = (f"Ø{fmtv(float(anti.get('diameter', 0.0)))}" if anti.get("shape") == "circle"
                      else f"{anti.get('shape')} {fmtv(float(anti.get('length', 0.0)))}×"
                           f"{fmtv(float(anti.get('width', 0.0)))} @{float(anti.get('rotation_deg', 0.0)):g}°")
        legend.append(("⑯", "via_transition.padstack.antipad.diameter",
                       f"反焊盘 {anti_value} {unit}"))
        if via0.backdrilled:
            legend.append(("⑰", "via_transition.backdrill.drill_diameter",
                           f"背钻孔 {fmtv(float(bd.get('drill_diameter', 0.0)))} {unit}"))
            legend.append(("⑱", "via_transition.backdrill.residual_stub",
                           f"残桩 {fmtv(float(bd.get('residual_stub', 0.0)))} {unit}"))
        if return_vias:
            legend.append(("⑲", "via_transition.return_vias.pad_diameter",
                           f"回流焊盘 {fmtv(float(rv_cfg.get('pad_diameter', 0.0)))} {unit}"))
            legend.append(("⑳", "via_transition.return_vias.antipad_diameter",
                           f"回流避空 {fmtv(float(rv_cfg.get('antipad_diameter', 0.0)))} {unit}"))
        legend.append(("㉑", "via_transition.signal_via_pitch",
                       f"信号孔间距 {fmtv(float(vt.get('signal_via_pitch', vt.get('pair_pitch', 0.0)) or 0.0))} {unit}"))
        fanout_legend = vt.get("fanout") if isinstance(vt.get("fanout"), Mapping) else {}
        fanout_style_label = {"deg45": "45° 折线", "arc": "圆弧"}.get(
            str(fanout_legend.get("style", "deg45")), str(fanout_legend.get("style", "deg45")))
        fanout_direction_label = {"auto": "自动", "expand": "向外", "contract": "向内"}.get(
            str(fanout_legend.get("direction", "auto")), str(fanout_legend.get("direction", "auto")))
        legend.append(("㉒", "via_transition.fanout.style", f"扇出方式 {fanout_style_label}"))
        legend.append(("㉓", "via_transition.fanout.direction", f"扇出方向 {fanout_direction_label}"))
        legend.append(("㉔", "via_transition.fanout.straight_length",
                       f"耦合直线段长 {fmtv(float(fanout_legend.get('straight_length', 1.0) or 0.0))} {unit}"))

        legend_y = y + pad
        canvas.create_text(pad, legend_y, anchor="nw", text="尺寸编号对照",
                           font=title_font, fill="#172033", tags=("hud-label",))
        legend_y += title_line + 6
        legend_row = max(small_line + 4, 17)
        if legend:
            legend_texts = [f"{number} {text}" for number, _path, text in legend]
            legend_widths = [text_size(canvas, item_text, small_font)[0] for item_text in legend_texts]
            widest = max(legend_widths)
            col_gap = 18
            columns = max(1, (width - pad * 2) // (widest + col_gap))
            for index, (number, path, text) in enumerate(legend):
                row, col = divmod(index, columns)
                lx = pad + col * (widest + col_gap)
                ly = legend_y + row * legend_row
                canvas.create_text(lx, ly, anchor="nw", text=f"{number} {text}",
                                   font=small_font, fill=hlc(path, "#334155"),
                                   tags=num_tags(path) + ("legend-item",))
            legend_y += ((len(legend) + columns - 1) // columns) * legend_row

        title_text = ellipsize(canvas, f"过孔截面示意 · 单位 {unit}", title_font, width - pad * 2)
        canvas.create_text(pad, title_y, anchor="nw",
                           text=title_text, font=title_font,
                           fill="#172033", tags=("hud-label", "cross-title"))
        note_text = ellipsize(canvas, "截面为示意图（叠层高度经可读性增强）；点击编号/元素定位参数",
                              small_font, width - pad * 2)
        canvas.create_text(pad, note_y, anchor="nw",
                           text=note_text,
                           fill="#9a3412", font=small_font, tags=("hud-label", "scale-note"))
        content_bottom = legend_y + pad
        content_width = max(width, int(x2) + pad)
        canvas.configure(scrollregion=(0, 0, content_width, content_bottom))

    # ---- 双向联动（参数 → 图高亮） ----

    def _widget_path(self, widget: Any) -> Optional[str]:
        if widget is None:
            return None
        direct = self._via_param_widgets.get(widget)
        if direct is not None:
            return direct
        # ``focus_get()`` 可能返回同一 Tk 路径的不同包装对象，按路径名兜底。
        return self._via_param_widget_paths.get(str(widget))

    def _on_via_param_focus_in(self, _event: tk.Event = None) -> None:
        self._schedule_via_highlight_sync()

    def _on_via_param_focus_out(self, _event: tk.Event = None) -> None:
        self._schedule_via_highlight_sync()

    def _schedule_via_highlight_sync(self) -> None:
        if self._via_highlight_sync_job is not None:
            try:
                self.after_cancel(self._via_highlight_sync_job)
            except tk.TclError:
                pass
        self._via_highlight_sync_job = self.after_idle(self._sync_via_highlight)

    def _sync_via_highlight(self) -> None:
        self._via_highlight_sync_job = None
        try:
            widget = self.focus_get()
        except tk.TclError:
            widget = None
        path = self._widget_path(widget) if widget is not None else None
        if path == self._via_highlight_path:
            return
        self._via_highlight_path = path
        self._redraw_via_diagrams()

    def _redraw_via_diagrams(self) -> None:
        render = getattr(self, "_via_last_render", None)
        if render is None:
            return
        config, geometry = render
        self._draw_via_top_view(config, geometry)
        self._draw_via_cross_section(config, geometry)

    def _schedule_via_diagram_redraw(self) -> None:
        """窗口缩放/字号变化后防抖重绘 via 两张示意图。"""
        if self._via_diagram_redraw_job is not None:
            try:
                self.after_cancel(self._via_diagram_redraw_job)
            except tk.TclError:
                pass
        self._via_diagram_redraw_job = self.after_idle(self._run_via_diagram_redraw)

    def _run_via_diagram_redraw(self) -> None:
        self._via_diagram_redraw_job = None
        self._redraw_via_diagrams()

    def _refresh_stackup(self) -> None:
        self._refresh_via_layer_selectors(self.config)
        for item in self.stack_tree.get_children():
            self.stack_tree.delete(item)
        for index, layer in enumerate(self.config["stackup"]):
            self.stack_tree.insert("", "end", iid=str(index), values=(layer["name"], layer["type"],
                f"{format_length(layer['thickness'], self.display_unit)} {self.display_unit}", layer["material"],
                _display(layer.get("dk")), _display(layer.get("df")), "是" if layer.get("is_reference_plane") else "否",
                "是" if layer.get("enable_antipad") else "否"))

    def _edit_layer(self, _event: Any = None) -> None:
        selected = self.stack_tree.selection()
        if not selected:
            return
        index = int(selected[0])
        layer = self.config["stackup"][index]
        text = simpledialog.askstring("编辑层", f"输入JSON对象（厚度单位={self.display_unit}）", parent=self,
                                      initialvalue=json.dumps({**layer, "thickness": convert_length(layer["thickness"], "mm", self.display_unit)}, ensure_ascii=False))
        if text:
            try:
                updated = json.loads(text)
                updated["thickness"] = convert_length(float(updated["thickness"]), self.display_unit, "mm")
                if updated.get("antipad_expansion_override") not in (None, ""):
                    updated["antipad_expansion_override"] = convert_length(float(updated["antipad_expansion_override"]), self.display_unit, "mm")
                self.config["stackup"][index] = updated
                self._refresh_stackup()
                self._schedule_refresh()
            except Exception as exc:
                messagebox.showerror("叠层编辑失败", str(exc), parent=self)

    def restore_defaults(self) -> None:
        self._load_to_widgets(self.manager.default())

    def load_json(self) -> None:
        path = filedialog.askopenfilename(parent=self, filetypes=(("JSON", "*.json"), ("所有文件", "*.*")))
        if not path:
            return
        try:
            config, corrections = self.manager.load(Path(path))
            self._load_to_widgets(config)
            self._set_log("已载入：" + path + "\n" + "\n".join(corrections))
        except ConfigError as exc:
            messagebox.showerror("载入失败", str(exc), parent=self)

    def save_json(self) -> None:
        try:
            config, _ = self.manager.validate(self._collect(), check_output_directory=False)
            path = filedialog.asksaveasfilename(parent=self, defaultextension=".json", filetypes=(("JSON", "*.json"),),
                                                initialfile=config["project"]["project_name"] + ".json")
            if path:
                self.manager.save(config, Path(path))
                self._load_to_widgets(config)
                self.status.set("配置已保存：" + Path(path).name)
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)

    def validate_parameters(self, show_dialog: bool = True) -> bool:
        try:
            config, result = self.manager.validate(self._collect())
            if result.is_valid:
                geometry = GeometryCalculator(config).calculate()
                for warning in geometry.warnings:
                    result.add_warning(warning)
                for error in geometry.errors:
                    result.add_error(error)
            self.config = config
            self._set_log(format_validation_report(result))
            self.notebook.select(self.tabs["检查结果"])
            if show_dialog:
                (messagebox.showinfo if result.is_valid else messagebox.showerror)("模型检查", "通过" if result.is_valid else "失败，请查看检查结果", parent=self)
            return result.is_valid
        except Exception as exc:
            self._set_log("模型检查异常：\n" + str(exc))
            return False

    def validate_model(self, show_dialog: bool = True) -> bool:
        """Compatibility hook overridden by the V2 structured validator."""
        return self.validate_parameters(show_dialog)

    def _generate(self):
        config, result = self.manager.validate(self._collect())
        if not result.is_valid:
            raise ScriptGenerationError(format_validation_report(result))
        generated = HfssScriptGenerator(config, SOURCE_DIRECTORY).generate()
        self.config = config
        self._set_log("V2独立HFSS脚本生成完成\n" + str(generated.script_path) + "\n" +
                      "\n".join("[警告] " + item for item in generated.warnings))
        self.notebook.select(self.tabs["检查结果"])
        return generated

    def generate_script(self) -> None:
        try:
            result = self._generate()
            messagebox.showinfo("生成完成", str(result.script_path), parent=self)
        except Exception as exc:
            messagebox.showerror("生成失败", str(exc), parent=self)

    def generate_sweeps(self) -> None:
        try:
            config, result = self.manager.validate(self._collect())
            if not result.is_valid:
                raise ScriptGenerationError(format_validation_report(result))
            root = self.manager.output_directory(config) / (config["project"]["project_name"] + "_SWEEPS")
            generated = generate_sweep_cases(config, root)
            self._set_log(f"已生成 {len(generated.cases)} 个离散重算扫描案例。\n清单：{generated.manifest_path}")
            self.notebook.select(self.tabs["检查结果"])
        except Exception as exc:
            messagebox.showerror("扫描生成失败", str(exc), parent=self)
    def generate_and_run(self) -> None:
        try:
            result = self._generate()
            command = [sys.executable, "--run-generated-script", str(result.script_path)] if IS_FROZEN else [sys.executable, str(result.script_path)]
            kwargs: Dict[str, Any] = {"cwd": str(result.project_directory)}
            if sys.platform.startswith("win"):
                kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
            subprocess.Popen(command, **kwargs)
            self.status.set("已启动独立PyAEDT进程")
        except Exception as exc:
            messagebox.showerror("启动失败", str(exc), parent=self)

    def destroy(self) -> None:
        self._ui_sync = True
        if self._refresh_job:
            try:
                self.after_cancel(self._refresh_job)
            except tk.TclError:
                pass
            self._refresh_job = None
        for job_attr in ("_via_highlight_sync_job", "_via_diagram_redraw_job"):
            job = getattr(self, job_attr, None)
            if job:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, job_attr, None)
        super().destroy()
    def _schedule_refresh(self) -> None:
        if self._ui_sync:
            return
        if hasattr(self, "validate_workbench"):
            self.validate_workbench.mark_stale()
        if self._refresh_job:
            try:
                self.after_cancel(self._refresh_job)
            except tk.TclError:
                pass
        self._refresh_job = self.after(120, self.refresh_visuals)

    def refresh_visuals(self) -> None:
        # This method can be called directly as well as by the debounce timer.
        # Cancel a still-registered callback to avoid a late invocation after
        # a test or host window has already been destroyed.
        job = self._refresh_job
        self._refresh_job = None
        if job:
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
        try:
            self._apply_scenario_linkage()
            if self._scenario_is_via():
                self._sync_via_fields()
            else:
                self._sync_derived_fields()
                self._update_model_mode()
            config, validation = self.manager.validate(self._collect(), check_output_directory=False)
            geometry = GeometryCalculator(config).calculate()
            if scenario_type(config) == VIA_TRANSITION:
                self._update_via_editor(config, geometry, validation)
            else:
                editor_issues = validate_capacitor_editor(config, SOURCE_DIRECTORY)
                self.cap_geometry_canvas.show(config, geometry, self.display_unit, editor_issues)
                self._update_capacitor_feedback(config, validation, geometry)
            if hasattr(self, "model_preview_workbench"):
                self.model_preview_workbench.show(config, geometry, validation)
            else:
                self._draw_preview(config, geometry)
        except Exception as exc:
            message = str(exc)
            if hasattr(self, "via_feedback_var"):
                self.via_feedback_var.set("刷新失败：" + message)
            self.cap_geometry_canvas.show_error(message)
            self.cap_status_var.set("● 输入尚未构成有效配置")
            self.cap_status_label.configure(style="StatusError.TLabel")
            self.cap_detail_var.set(message)
            for path in self.numeric_paths:
                if path.startswith(("capacitor.", "capacitor_model.")):
                    widget = self.widgets.get(path)
                    try:
                        float(self.variables[path].get())
                    except (TypeError, ValueError, tk.TclError):
                        if isinstance(widget, ttk.Entry):
                            widget.configure(style="Invalid.TEntry")
            if hasattr(self, "model_preview_workbench"):
                self.model_preview_workbench.show_error(message)
            else:
                self.preview_canvas.delete("all")
                viewport = canvas_viewport(self.preview_canvas)
                if viewport is not None:
                    width, height = viewport
                    self.preview_canvas.create_text(
                        width / 2, height / 2,
                        text="缺少有效的依赖参数，模型预览暂不显示\n" + message,
                        fill="#b42318", justify="center", width=max(1, width-24),
                        tags=("hud-label",),
                    )

    def _draw_preview(self, config: Mapping[str, Any], geometry: Any) -> None:
        c = self.preview_canvas
        c.delete("all")
        viewport = canvas_viewport(c)
        if viewport is None:
            return
        w, h = viewport
        b = geometry.plane_bounds
        scale = min(max(1, w - 80) / b.width, max(1, h - 100) / b.height)
        ox, oy = w / 2 - b.center.x * scale, h / 2 + b.center.y * scale
        def xy(x: float, y: float) -> Tuple[float, float]: return ox + x * scale, oy - y * scale
        x1, y1 = xy(b.x_min, b.y_max); x2, y2 = xy(b.x_max, b.y_min)
        c.create_rectangle(x1, y1, x2, y2, fill="#eef2f7", outline="#64748b")
        for tools in geometry.antipads_by_layer.values():
            for shape in tools:
                c.create_polygon([coord for p in shape.points for coord in xy(p.x, p.y)], fill="#ffffff", outline="#94a3b8", dash=(4, 3))
        colors = {"P": "#dc2626", "N": "#2563eb"}
        for signal in ("P", "N"):
            for shape in geometry.signal_shapes[signal]:
                c.create_polygon([coord for p in shape.points for coord in xy(p.x, p.y)], fill=colors[signal], outline="black")
        for via in geometry.gnd_vias:
            x, y = xy(via.x, via.y)
            r_clear = via.antipad_diameter * scale / 2
            r_pad = via.pad_diameter * scale / 2
            r_hole = via.finished_hole_diameter * scale / 2
            c.create_oval(x-r_clear, y-r_clear, x+r_clear, y+r_clear, outline="#f59e0b", dash=(3, 2))
            c.create_oval(x-r_pad, y-r_pad, x+r_pad, y+r_pad, fill="#d4a72c", outline="#713f12")
            c.create_oval(x-r_hole, y-r_hole, x+r_hole, y+r_hole, fill="white", outline="black")
        cp = geometry.capacitor_centers["C_P"]; cn = geometry.capacitor_centers["C_N"]
        x, yp = xy(cp.x, cp.y); _, yn = xy(cn.x, cn.y)
        c.create_line(x + 30, yp, x + 30, yn, arrow="both", fill="#7c3aed")
        c.create_text(min(w-8, x + 38), (yp + yn)/2, anchor="e" if x+38 >= w-8 else "w",
                      text=f"cap_pitch {format_length(config['capacitor']['cap_pitch'], self.display_unit)} {self.display_unit}",
                      tags=("dimension-label",))
        c.create_text(20, 18, anchor="nw",
                      text=f"二维俯视图 | 单位 {self.display_unit} | VIA={len(geometry.gnd_vias)}",
                      font=("TkDefaultFont", 11, "bold"), width=max(1, w-40), tags=("hud-label",))

    def _set_log(self, text: str) -> None:
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", text)


def _run_generated_script(path: Path) -> int:
    sys.argv = [str(path)]
    try:
        runpy.run_path(str(path), run_name="__main__")
        return 0
    except SystemExit as exc:
        return int(exc.code or 0)


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--run-generated-script":
        return _run_generated_script(Path(sys.argv[2]))
    smoke = "--smoke-test" in sys.argv
    app = SurfaceCapAntipadApp()
    if smoke:
        app.update_idletasks()
        app.destroy()
        return 0
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
