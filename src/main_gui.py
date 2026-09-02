"""PCIe AC 耦合电容工具 V2.4.0 主入口。

V2.4.0 的通用窗口与既有页面保留在 ``main_gui_legacy``。本入口提供 v3.1
VIA 换层工作区（左场景轨 + 叠层/反焊盘抽屉、中部五组参数、右侧持久预览、
底部动作栏），电容/GND/工程表面通过场景轨切换到达。所有控件仍绑定同一
配置对象与既有 JSON 字段。
"""
from __future__ import annotations

import ctypes
import copy
import faulthandler
import json
import math
import os
import subprocess
import sys
import tempfile
import tkinter as tk
import traceback
from tkinter import font as tkfont, messagebox
from pathlib import Path
from typing import Any, Mapping


def _enable_windows_dpi_awareness() -> None:
    """在创建首个 Tk 窗口前统一使用 Per-Monitor V2 DPI 感知。"""
    if not sys.platform.startswith("win"):
        return
    # Keep source Python and the PyInstaller executable in the same DPI mode.
    # A process can set this only once, so older APIs are fallbacks.
    try:
        setter = ctypes.windll.user32.SetProcessDpiAwarenessContext
        setter.argtypes = [ctypes.c_void_p]
        setter.restype = ctypes.c_bool
        if setter(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
            return
    except (AttributeError, OSError):
        pass
    try:
        result = ctypes.windll.shcore.SetProcessDpiAwareness(2)
        if result in (0, -2147024891):  # S_OK or already fixed by a manifest.
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


_enable_windows_dpi_awareness()

import main_gui_legacy as _legacy
from antipad_editor import AntipadWorkbench
from build_info import build_identity_line, get_build_info
from capacitor_editor import ScrollableFrame
from config_manager import (
    config_hash, convert_length, format_length, make_generation_snapshot,
    normalize_config,
)
from geometry_calculator import GeometryCalculator
from gnd_via_editor import GndViaWorkbench
from model_preview import ModelPreviewWorkbench
from model_validator import (
    ModelValidator, ValidationItem, ValidationReport, config_revision,
    load_report, preview_auto_fix,
)
from scenario_semantics import VIA_TRANSITION, scenario_type
from simulation_sweep_editor import SimulationSweepWorkbench
from validate_results import ValidateResultsWorkbench
from via_workspace import (
    SCENARIO_LABELS,
    VIA_GROUP_KEYS,
    VIA_GROUP_TITLES,
    CollapsibleGroup,
    NestedMoreSection,
    PreviewViewBar,
    SegmentedToggle,
    navigation_target,
    preview_click_path,
)
from main_gui_legacy import *  # noqa: F401,F403 - 保持原模块公开接口兼容


CRASH_DIRECTORY = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SurfaceCapAntipadToolV2.4.0"
CRASH_LOG_PATH = CRASH_DIRECTORY / "crash.log"
_FAULT_LOG_HANDLE: Any = None


def _write_crash_log(title: str, detail: str) -> None:
    try:
        CRASH_DIRECTORY.mkdir(parents=True, exist_ok=True)
        with CRASH_LOG_PATH.open("a", encoding="utf-8") as stream:
            stream.write(f"\n===== {title} =====\n{detail}\n")
    except OSError:
        pass


def _enable_native_fault_log() -> None:
    global _FAULT_LOG_HANDLE
    try:
        CRASH_DIRECTORY.mkdir(parents=True, exist_ok=True)
        _FAULT_LOG_HANDLE = CRASH_LOG_PATH.open("a", encoding="utf-8")
        faulthandler.enable(_FAULT_LOG_HANDLE, all_threads=True)
    except (OSError, RuntimeError):
        _FAULT_LOG_HANDLE = None


class SurfaceCapAntipadApp(_legacy.SurfaceCapAntipadApp):
    """接入可视化反焊盘与模型预览工作台的 V2.4.0 主窗口。"""

    def __init__(self) -> None:
        super().__init__()
        self._install_combobox_wheel_guard()
        build = get_build_info()
        short_hash = str(build["source_hash"])[:12]
        self.title(
            f"PCIe AC耦合电容区域无源优化工具 V{build['software_version']} · "
            f"{build['sweep_backend_version']} · {short_hash}"
        )
        identity = build_identity_line()
        self.status.set("就绪 | " + identity)
        if hasattr(self, "log_text"):
            self.log_text.insert("1.0", "软件构建标识\n" + identity + "\n\n")

    def _install_combobox_wheel_guard(self) -> None:
        """Keep a closed combobox from consuming the page-scroll wheel event."""

        self.bind_class("TCombobox", "<MouseWheel>", self._on_closed_combobox_wheel)

    def _on_closed_combobox_wheel(self, event: tk.Event) -> str:
        owner: tk.Misc | None = event.widget
        while owner is not None:
            if isinstance(owner, ScrollableFrame):
                owner.canvas.yview_scroll(int(-event.delta / 120), "units")
                return "break"
            owner = getattr(owner, "master", None)
        workbench = getattr(self, "antipad_workbench", None)
        if workbench is not None:
            widget_name = str(event.widget)
            if widget_name.startswith(str(workbench.settings_content)):
                workbench.settings_canvas.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def report_callback_exception(self, exc_type: Any, exc_value: BaseException,
                                  exc_traceback: Any) -> None:
        """Keep Tk callback failures visible and logged instead of silently exiting."""
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        _write_crash_log("Tk callback exception", detail)
        try:
            if hasattr(self, "status"):
                self.status.set(f"界面操作失败，窗口已保留；诊断日志：{CRASH_LOG_PATH}")
        except tk.TclError:
            pass
        if not self._can_show_modal_dialog():
            return
        try:
            messagebox.showerror(
                "界面操作失败",
                f"软件已阻止本次操作异常导致窗口退出。\n\n{exc_value}\n\n诊断日志：{CRASH_LOG_PATH}",
                parent=self,
            )
        except tk.TclError:
            pass

    def _can_show_modal_dialog(self) -> bool:
        """Whether someone is present to dismiss a modal dialog.

        Unattended runs -- unit tests and the packaged GUI acceptance driver --
        would otherwise block forever on an error box nobody closes.  Set
        ``SURFACE_CAP_HEADLESS=1`` to declare such a run.  A destroyed root
        cannot host a dialog at all, and asking it to raises a second error
        inside the exception handler itself.
        """
        if str(os.environ.get("SURFACE_CAP_HEADLESS", "")).strip() == "1":
            return False
        try:
            return bool(self.winfo_exists())
        except tk.TclError:
            return False

    def _configure_editor_styles(self) -> None:
        # Per-Monitor V2 stays enabled.  Layout code consumes real font metrics
        # and the current monitor DPI instead of forcing one global Tk scale.
        super()._configure_editor_styles()
        for name, family, size in (
            ("TkDefaultFont", "Microsoft YaHei UI", 9),
            ("TkTextFont", "Microsoft YaHei UI", 9),
            ("TkMenuFont", "Microsoft YaHei UI", 9),
            ("TkHeadingFont", "Microsoft YaHei UI", 9),
            ("TkFixedFont", "Consolas", 9),
        ):
            try:
                tkfont.nametofont(name).configure(family=family, size=size)
            except tk.TclError:
                pass
        custom_fonts = (
            ("ViaCardTitleFont", "Microsoft YaHei UI", 10, "bold"),
            ("ViaCardBodyFont", "Microsoft YaHei UI", 9, "normal"),
            ("ViaPreviewTitleFont", "Microsoft YaHei UI", 11, "bold"),
            ("ViaCanvasFont", "Microsoft YaHei UI", 9, "normal"),
            ("ViaSmallFont", "Microsoft YaHei UI", 8, "normal"),
            ("ViaMonoFont", "Consolas", 9, "normal"),
            ("ViaStatusFont", "Microsoft YaHei UI", 10, "bold"),
        )
        self._responsive_fonts = {}
        for name, family, size, weight in custom_fonts:
            try:
                font_object = tkfont.nametofont(name)
                font_object.configure(family=family, size=size, weight=weight)
            except tk.TclError:
                font_object = tkfont.Font(root=self, name=name, family=family, size=size, weight=weight)
            self._responsive_fonts[name] = font_object
        style = _legacy.ttk.Style(self)
        style.configure(".", font="TkDefaultFont")
        style.configure("StatusGood.TLabel", font="ViaStatusFont")
        style.configure("StatusWarn.TLabel", font="ViaStatusFont")
        style.configure("StatusError.TLabel", font="ViaStatusFont")
        self._responsive_font_tier = ""
        self._responsive_font_state: tuple[str, float] = ("", 0.0)
        self._responsive_font_override: str | None = None
        self.bind("<Configure>", self._window_size_changed, add="+")
        self._responsive_font_job = self.after_idle(self._apply_responsive_fonts)
        style = _legacy.ttk.Style(self)
        style.configure("ViaRail.TButton", padding=(8, 8))
        style.configure("ViaRailSelected.TButton", padding=(8, 8))
        style.configure("ViaPrimary.TButton", padding=(10, 6))
        style.configure("ViaFooter.TFrame", background="#eef2f7")

    def _window_size_changed(self, event: tk.Event) -> None:
        if event.widget is self and not getattr(self, "_destroying", False):
            if getattr(self, "_responsive_font_job", None):
                try:
                    self.after_cancel(self._responsive_font_job)
                except tk.TclError:
                    pass
            self._responsive_font_job = self.after(100, self._apply_responsive_fonts)

    def _apply_responsive_fonts(self) -> None:
        self._responsive_font_job = None
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            # A queued ``after`` callback can still fire once this root is
            # gone.  ``winfo exists`` is itself a Tcl command, so on a
            # destroyed interpreter the guard raises rather than returning
            # false; both outcomes mean the same thing here.
            return
        width, height = self.winfo_width(), self.winfo_height()
        dpi_scale = self._current_monitor_dpi_scale()
        density_guard = max(1.0, dpi_scale / 1.25)
        content_width = width / density_guard
        content_height = height / density_guard
        override = self._responsive_font_override
        if override in {"normal", "large", "ultra"}:
            tier = override
        elif (width >= 2000 and height >= 1080
              and content_width >= 1580 and content_height >= 850):
            tier = "ultra"
        elif (width >= 1500 and height >= 820
              and content_width >= 1180 and content_height >= 650):
            tier = "large"
        else:
            tier = "normal"
        state = (tier, round(dpi_scale, 3))
        if state == self._responsive_font_state:
            return
        self._responsive_font_state = state
        self._responsive_font_tier = tier
        delta = {"normal": 0, "large": 2, "ultra": 3}[tier]
        for name, family, base in (
            ("TkDefaultFont", "Microsoft YaHei UI", 9),
            ("TkTextFont", "Microsoft YaHei UI", 9),
            ("TkMenuFont", "Microsoft YaHei UI", 9),
            ("TkHeadingFont", "Microsoft YaHei UI", 9),
            ("TkFixedFont", "Consolas", 9),
        ):
            tkfont.nametofont(name, root=self).configure(family=family, size=base + delta)
        for name, base in (
            ("ViaCardTitleFont", 10), ("ViaCardBodyFont", 9),
            ("ViaPreviewTitleFont", 11), ("ViaCanvasFont", 9),
            ("ViaSmallFont", 8), ("ViaMonoFont", 9), ("ViaStatusFont", 10),
        ):
            self._responsive_fonts[name].configure(size=base + delta)
        row_height = max(
            22,
            int(tkfont.nametofont("TkDefaultFont", root=self).metrics("linespace")) + 8)
        _legacy.ttk.Style(self).configure("Treeview", rowheight=row_height)
        if hasattr(self, "gnd_via_workbench"):
            self.gnd_via_workbench.on_font_scale_changed(tier)
        if hasattr(self, "simulation_sweep_workbench"):
            self.simulation_sweep_workbench.on_font_scale_changed(tier)
        self.after_idle(self._redraw_font_sensitive_canvases)

    def _current_monitor_dpi_scale(self) -> float:
        try:
            getter = ctypes.windll.user32.GetDpiForWindow
            getter.argtypes = [ctypes.c_void_p]
            getter.restype = ctypes.c_uint
            dpi = int(getter(self.winfo_id()))
            if dpi > 0:
                return dpi / 96.0
        except (AttributeError, OSError, tk.TclError):
            pass
        try:
            return max(.75, float(self.winfo_fpixels("1i")) / 96.0)
        except tk.TclError:
            return 1.0

    def set_responsive_font_tier(self, tier: str | None) -> None:
        """Test/review hook; ``None`` restores automatic width/height/DPI selection."""
        if tier not in {None, "normal", "large", "ultra"}:
            raise ValueError(f"unknown responsive font tier: {tier}")
        self._responsive_font_override = tier
        self._responsive_font_state = ("", 0.0)
        self._apply_responsive_fonts()

    def _redraw_font_sensitive_canvases(self) -> None:
        if getattr(self, "_destroying", False):
            return
        if hasattr(self, "cap_geometry_canvas"):
            self._schedule_capacitor_page_layout()
            self.cap_geometry_canvas._redraw_last()
        if hasattr(self, "model_canvas"):
            self.model_canvas.refresh_layout()
        if hasattr(self, "antipad_workbench"):
            self.antipad_workbench.mode_selector._apply_layout()
            if not getattr(self.antipad_workbench, "_via_mode", False):
                self.antipad_workbench.preview.redraw()
            self.antipad_workbench.cross_section.redraw()
        if hasattr(self, "gnd_via_workbench"):
            self.gnd_via_workbench.mode_cards._apply_layout()
        if hasattr(self, "model_preview_workbench"):
            self.model_preview_workbench._software_redraw()
        if hasattr(self, "_redraw_via_diagrams"):
            self._redraw_via_diagrams()

    def _build_tabs(self) -> None:
        """v3.1 chrome: left rail, capacitor notebook OR VIA groups, persistent preview, footer."""
        self.tabs: dict[str, tk.Widget] = {}
        self.scenario_hints: dict[str, _legacy.ttk.Label] = {}
        self._config_source_name = "default_config.json"
        self._preview_dirty = True
        self._stackup_drawer_open = False
        self._validate_overlay_open = False
        self._generation_locked = True
        self.via_active_group = "path"
        self.via_parameter_groups: dict[str, CollapsibleGroup] = {}
        self.via_parameter_scrollers: dict[str, ScrollableFrame] = {}
        self.via_parameter_tabs: dict[str, tk.Widget] = {}
        self.preview_dirty_var = tk.StringVar(value="实时预览 · dirty")
        self.footer_status_var = tk.StringVar(value="未校验 · dirty")
        self.footer_error_var = tk.StringVar(value="错误 0")
        self.footer_warn_var = tk.StringVar(value="警告 0")
        self.footer_note_var = tk.StringVar(value="")
        self.stackup_summary_var = tk.StringVar(value="叠层 / 反焊盘")
        self.config_name_var = tk.StringVar(value=self._config_source_name)

        self.shell = _legacy.ttk.Frame(self)
        self.shell.pack(fill="both", expand=True, padx=4, pady=(0, 2))
        self.shell.columnconfigure(1, weight=1)
        self.shell.rowconfigure(0, weight=1)

        self._build_left_rail(self.shell)

        main = _legacy.ttk.Frame(self.shell)
        main.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        main.rowconfigure(0, weight=1)
        main.columnconfigure(0, weight=1)

        self.workspace_panes = _legacy.ttk.PanedWindow(main, orient="horizontal")
        self.workspace_panes.grid(row=0, column=0, sticky="nsew")

        self.workspace_host = _legacy.ttk.Frame(self.workspace_panes)
        self.preview_host = _legacy.ttk.Frame(self.workspace_panes)
        self.workspace_panes.add(self.workspace_host, weight=3)
        self.workspace_panes.add(self.preview_host, weight=4)
        self.workspace_host.rowconfigure(0, weight=1)
        self.workspace_host.columnconfigure(0, weight=1)
        self.preview_host.rowconfigure(1, weight=1)
        self.preview_host.columnconfigure(0, weight=1)
        # Overlay on the middle+preview row so AntipadWorkbench keeps a full
        # editing viewport; the left rail only holds the drawer toggle.
        self.drawer_host = _legacy.ttk.Frame(main)

        self.notebook = _legacy.ttk.Notebook(self.workspace_host)
        for name in ("项目与走线", "电容与模型", "GND VIA"):
            frame = _legacy.ttk.Frame(self.notebook, padding=8)
            self.notebook.add(frame, text=name)
            self.tabs[name] = frame
            hint = _legacy.ttk.Label(
                frame, text="", foreground="#475569", justify="left", wraplength=720)
            hint.pack(fill="x", padx=4, pady=(2, 0), anchor="w")
            self.scenario_hints[name] = hint

        self.via_workspace = _legacy.ttk.Frame(self.workspace_host)
        self.via_workspace.rowconfigure(0, weight=1)
        self.via_workspace.columnconfigure(0, weight=1)

        self._build_persistent_preview(self.preview_host)
        self._build_footer(main)
        self._build_sweep_sheet()

        self._build_project_trace()
        self._build_capacitor()
        self._build_antipad_stackup()
        self._build_vias()
        self._build_via_transition()
        self._build_simulation_sweeps()
        self._build_model_preview()
        self._build_validate_results()
        self._show_scenario_workspace()

    def _build_left_rail(self, parent: tk.Widget) -> None:
        rail_column = _legacy.ttk.Frame(parent, padding=(2, 6))
        self.left_rail_column = rail_column
        rail_column.grid(row=0, column=0, sticky="nsew")
        self.shell.rowconfigure(0, weight=1)
        self.shell.columnconfigure(0, minsize=176)

        rail = _legacy.ttk.LabelFrame(rail_column, text="场景", padding=8)
        rail.pack(fill="x")
        self.left_rail = rail
        self.scenario_rail_buttons: dict[str, _legacy.ttk.Button] = {}
        for backend, label in SCENARIO_LABELS.items():
            button = _legacy.ttk.Button(
                rail, text=label, style="ViaRail.TButton",
                command=lambda value=backend: self._select_scenario_backend(value),
            )
            button.pack(fill="x", pady=3)
            self.scenario_rail_buttons[backend] = button

        _legacy.ttk.Label(rail, text="当前配置", foreground="#64748b").pack(anchor="w", pady=(10, 0))
        _legacy.ttk.Label(
            rail, textvariable=self.config_name_var, wraplength=160
        ).pack(anchor="w")

        drawer_box = _legacy.ttk.LabelFrame(rail_column, text="叠层 / 反焊盘", padding=8)
        drawer_box.pack(fill="x", pady=(8, 0))
        self.stackup_drawer_button = _legacy.ttk.Button(
            drawer_box, textvariable=self.stackup_summary_var,
            command=self.toggle_stackup_drawer,
        )
        self.stackup_drawer_button.pack(fill="x")
        _legacy.ttk.Label(
            drawer_box, text="反焊盘与叠层的唯一编辑入口",
            foreground="#64748b", wraplength=160,
        ).pack(anchor="w", pady=(4, 0))

    def _build_persistent_preview(self, parent: tk.Widget) -> None:
        self.preview_view_bar = PreviewViewBar(
            parent, on_select=self.set_preview_view, dirty_var=self.preview_dirty_var)
        self.preview_view_bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))

        self.preview_top_host = _legacy.ttk.Frame(parent)
        self.preview_iso_host = _legacy.ttk.Frame(parent)
        self.preview_section_host = _legacy.ttk.Frame(parent)
        self.preview_top_host.grid(row=1, column=0, sticky="nsew")
        for host in (self.preview_top_host, self.preview_iso_host, self.preview_section_host):
            host.rowconfigure(0, weight=1)
            host.columnconfigure(0, weight=1)

        top_box = _legacy.ttk.LabelFrame(
            self.preview_top_host,
            text="俯视（P/N 信号孔、回流地孔、输入/输出传输线与尺寸）", padding=4)
        top_box.grid(row=0, column=0, sticky="nsew")
        top_box.rowconfigure(0, weight=1)
        top_box.columnconfigure(0, weight=1)
        self.via_top_canvas = tk.Canvas(
            top_box, background="#f7f8fa", highlightthickness=1,
            highlightbackground="#cbd5e1", width=420, height=360)
        self.via_top_canvas.grid(row=0, column=0, sticky="nsew")
        self.via_top_canvas.tag_bind("dimension-label", "<Button-1>", self._on_via_dimension_click)
        self.via_top_canvas.tag_bind("dimension-label", "<Enter>", self._on_via_dimension_enter)
        self.via_top_canvas.tag_bind("dimension-label", "<Leave>", self._on_via_dimension_leave)
        self.via_top_canvas.tag_bind("via-antipad", "<Button-1>", self._on_via_antipad_click)
        self.via_top_canvas.tag_bind("scene_object", "<Button-1>", self._on_via_preview_object_click)
        self.via_top_canvas.bind("<Configure>", lambda _e: self._schedule_via_diagram_redraw())

        section_box = _legacy.ttk.LabelFrame(
            self.preview_section_host, text="截面（后续）", padding=4)
        section_box.grid(row=0, column=0, sticky="nsew")
        section_inner = _legacy.ttk.Frame(section_box)
        section_inner.pack(fill="both", expand=True)
        self.via_cross_canvas = tk.Canvas(
            section_inner, background="#fbfcfe", highlightthickness=1,
            highlightbackground="#cbd5e1", width=360, height=520)
        self.via_cross_scrollbar = _legacy.ttk.Scrollbar(
            section_inner, orient="vertical", command=self.via_cross_canvas.yview)
        self.via_cross_canvas.configure(yscrollcommand=self.via_cross_scrollbar.set)
        self.via_cross_canvas.pack(side="left", fill="both", expand=True)
        self.via_cross_scrollbar.pack(side="right", fill="y")
        self.via_cross_canvas.tag_bind("dimension-label", "<Button-1>", self._on_via_dimension_click)
        self.via_cross_canvas.tag_bind("dimension-label", "<Enter>", self._on_via_dimension_enter)
        self.via_cross_canvas.tag_bind("dimension-label", "<Leave>", self._on_via_dimension_leave)
        self.via_cross_canvas.bind("<Configure>", lambda _e: self._schedule_via_diagram_redraw())
        self.via_cross_canvas.bind("<MouseWheel>", self._on_via_cross_wheel)

        self.set_preview_view("俯视")

    def _build_footer(self, parent: tk.Widget) -> None:
        footer = _legacy.ttk.Frame(parent, padding=(4, 6), style="ViaFooter.TFrame")
        footer.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.footer = footer

        status_box = _legacy.ttk.Frame(footer)
        status_box.pack(side="left", fill="x", expand=True)
        _legacy.ttk.Label(
            status_box, textvariable=self.footer_status_var, font="ViaStatusFont"
        ).pack(side="left", padx=(0, 8))
        _legacy.ttk.Label(
            status_box, textvariable=self.footer_error_var, foreground="#b42318"
        ).pack(side="left", padx=4)
        _legacy.ttk.Label(
            status_box, textvariable=self.footer_warn_var, foreground="#b45309"
        ).pack(side="left", padx=4)
        _legacy.ttk.Button(
            status_box, text="预览 -> 生成快照",
            command=lambda: self.validate_model(show_dialog=False),
        ).pack(side="left", padx=8)
        _legacy.ttk.Label(
            status_box, textvariable=self.footer_note_var, foreground="#475569",
            wraplength=420,
        ).pack(side="left", padx=8)

        self.validate_overlay = _legacy.ttk.Frame(footer)
        # Packed on demand as an overlay on the status strip.

        actions = _legacy.ttk.Frame(footer)
        actions.pack(side="right")
        self.hfss_offline_button = _legacy.ttk.Button(
            actions, text="生成 HFSS · 离线审阅", command=self.generate_hfss_offline)
        self.hfss_offline_button.pack(side="left", padx=3)
        _legacy.ttk.Button(
            actions, text="创建参数扫描", command=self.open_sweep_sheet
        ).pack(side="left", padx=3)
        self.validate_model_button = _legacy.ttk.Button(
            actions, text="Validate 模型", style="ViaPrimary.TButton",
            command=self.validate_model,
        )
        self.validate_model_button.pack(side="left", padx=3)

    def _build_sweep_sheet(self) -> None:
        self.sweep_sheet = tk.Toplevel(self)
        self.sweep_sheet.title("创建参数扫描")
        self.sweep_sheet.geometry("1100x720")
        self.sweep_sheet.withdraw()
        self.sweep_sheet.protocol("WM_DELETE_WINDOW", self.close_sweep_sheet)
        self.sweep_sheet_host = _legacy.ttk.Frame(self.sweep_sheet)
        self.sweep_sheet_host.pack(fill="both", expand=True)

    def _select_scenario_backend(self, backend: str) -> None:
        labels = self.enum_labels.get("scenario.type", dict(SCENARIO_LABELS))
        display = labels.get(backend, backend)
        variable = self.variables.get("scenario.type")
        if variable is None:
            return
        if str(variable.get()) != str(display) and str(variable.get()) != str(backend):
            variable.set(display)
        else:
            self._apply_scenario_linkage()
            self._schedule_refresh()

    def _show_scenario_workspace(self) -> None:
        is_via = self._scenario_is_via()
        if is_via:
            if self.notebook.winfo_manager():
                self.notebook.pack_forget()
            if not self.via_workspace.winfo_manager():
                self.via_workspace.pack(fill="both", expand=True)
        else:
            if self.via_workspace.winfo_manager():
                self.via_workspace.pack_forget()
            if not self.notebook.winfo_manager():
                self.notebook.pack(fill="both", expand=True)
        self._update_rail_scenario_styles()
        self._update_stackup_drawer_summary()
        self._update_footer_notes()
        self._ensure_workspace_host(visible=True)
        self._ensure_preview_pane(visible=is_via)

    def _pane_has(self, child: tk.Widget) -> bool:
        panes = getattr(self, "workspace_panes", None)
        if panes is None:
            return False
        try:
            return str(child) in {str(item) for item in panes.panes()}
        except tk.TclError:
            return False

    def _ensure_workspace_host(self, *, visible: bool) -> None:
        panes = getattr(self, "workspace_panes", None)
        host = getattr(self, "workspace_host", None)
        if panes is None or host is None:
            return
        present = self._pane_has(host)
        try:
            if visible and not present:
                panes.insert(0, host, weight=3)
            elif not visible and present:
                panes.forget(host)
        except tk.TclError:
            pass

    def _ensure_preview_pane(self, *, visible: bool) -> None:
        """VIA keeps the persistent preview; capacitor/GND get a full-width notebook."""
        panes = getattr(self, "workspace_panes", None)
        host = getattr(self, "preview_host", None)
        if panes is None or host is None:
            return
        present = self._pane_has(host)
        try:
            if visible and not present:
                panes.add(host, weight=4)
            elif not visible and present:
                panes.forget(host)
        except tk.TclError:
            pass

    def _update_rail_scenario_styles(self) -> None:
        current = "via_transition" if self._scenario_is_via() else "surface_capacitor"
        for backend, button in getattr(self, "scenario_rail_buttons", {}).items():
            try:
                button.state(["pressed"] if backend == current else ["!pressed"])
            except tk.TclError:
                pass

    def open_stackup_drawer(self) -> None:
        self._stackup_drawer_open = True
        try:
            self.workspace_panes.grid_remove()
        except tk.TclError:
            pass
        if not self.drawer_host.winfo_manager():
            self.drawer_host.grid(row=0, column=0, sticky="nsew")
        try:
            self.update_idletasks()
        except tk.TclError:
            pass

    def close_stackup_drawer(self) -> None:
        self._stackup_drawer_open = False
        if self.drawer_host.winfo_manager():
            self.drawer_host.grid_remove()
        try:
            self.workspace_panes.grid(row=0, column=0, sticky="nsew")
        except tk.TclError:
            pass

    def toggle_stackup_drawer(self) -> None:
        if self._stackup_drawer_open:
            self.close_stackup_drawer()
        else:
            self.open_stackup_drawer()

    @property
    def stackup_drawer_open(self) -> bool:
        return bool(self._stackup_drawer_open)

    def select_via_group(self, key: str) -> None:
        if key not in VIA_GROUP_KEYS:
            return
        if self._stackup_drawer_open:
            self.close_stackup_drawer()
        self.via_active_group = key
        group = self.via_parameter_groups.get(key)
        if group is not None:
            group.expand()
            scroller = self.via_parameter_scrollers.get("main")
            if scroller is not None:
                try:
                    scroller.scroll_to_widget(group)
                except tk.TclError:
                    pass

    def set_preview_view(self, name: str) -> None:
        mapping = {
            "俯视": self.preview_top_host,
            "等轴": self.preview_iso_host,
            "截面": self.preview_section_host,
        }
        target = mapping.get(name, self.preview_top_host)
        for host in mapping.values():
            if host is target:
                if not host.winfo_manager():
                    host.grid(row=1, column=0, sticky="nsew")
            elif host.winfo_manager():
                host.grid_remove()
        if hasattr(self, "preview_view_bar"):
            self.preview_view_bar.active = name
            self.preview_view_bar._sync()
        workbench = getattr(self, "model_preview_workbench", None)
        if workbench is not None:
            if name == "俯视":
                workbench.set_view("顶视图", fit=False)
            elif name == "等轴":
                workbench.set_view("等轴测", fit=False)

    def open_sweep_sheet(self) -> None:
        sheet = getattr(self, "sweep_sheet", None)
        if sheet is None:
            return
        try:
            sheet.deiconify()
            sheet.lift()
            sheet.focus_set()
        except tk.TclError:
            pass

    def close_sweep_sheet(self) -> None:
        sheet = getattr(self, "sweep_sheet", None)
        if sheet is None:
            return
        try:
            sheet.withdraw()
        except tk.TclError:
            pass

    def _show_validate_overlay(self) -> None:
        self._validate_overlay_open = True
        overlay = getattr(self, "validate_overlay", None)
        if overlay is None:
            return
        if overlay.winfo_manager():
            return
        actions = getattr(self, "validate_model_button", None)
        kwargs: dict[str, Any] = {"side": "left", "fill": "both", "expand": True, "padx": 8}
        if actions is not None:
            kwargs["before"] = actions.master
        try:
            overlay.pack(**kwargs)
        except tk.TclError:
            overlay.pack(side="left", fill="both", expand=True, padx=8)

    def _hide_validate_overlay(self) -> None:
        self._validate_overlay_open = False
        overlay = getattr(self, "validate_overlay", None)
        if overlay is not None and overlay.winfo_manager():
            overlay.pack_forget()

    def show_workspace_page(self, name: str) -> None:
        """Test/review hook replacing notebook.select for retired VIA chrome pages."""
        if name in ("项目与走线", "电容与模型", "GND VIA"):
            self.close_stackup_drawer()
            self._select_scenario_backend("surface_capacitor")
            try:
                self.notebook.select(self.tabs[name])
            except tk.TclError:
                pass
            return
        if name in {"反焊盘与叠层", "叠层 / 反焊盘"}:
            self.open_stackup_drawer()
            return
        if name in {"模型预览", "等轴"}:
            self.close_stackup_drawer()
            self._ensure_workspace_host(visible=False)
            self._ensure_preview_pane(visible=True)
            self.set_preview_view("等轴")
            return
        if name in {"仿真与扫描", "创建参数扫描"}:
            self.open_sweep_sheet()
            return
        if name in {"Validate结果", "Validate 模型"}:
            self._show_validate_overlay()

    def _mark_preview_dirty(self) -> None:
        self._preview_dirty = True
        self._generation_locked = True
        self.preview_dirty_var.set("实时预览 · dirty")
        self.footer_status_var.set("未校验 · dirty")

    def _mark_preview_clean(self, *, errors: int = 0, warnings: int = 0) -> None:
        self._preview_dirty = False
        self._generation_locked = errors > 0
        self.preview_dirty_var.set("实时预览 · 已校验")
        if errors:
            self.footer_status_var.set("校验失败 · 禁止生成")
        elif warnings:
            self.footer_status_var.set("已校验 · 有警告")
        else:
            self.footer_status_var.set("已校验 · 可生成")
        self.footer_error_var.set(f"错误 {errors}")
        self.footer_warn_var.set(f"警告 {warnings}")

    def _update_stackup_drawer_summary(self) -> None:
        stackup = list(self.config.get("stackup", [])) if hasattr(self, "config") else []
        metal = [layer for layer in stackup if layer.get("type") in {"signal", "plane"}]
        try:
            entry = self._backend_value("via_transition.entry_layer")
            exit_layer = self._backend_value("via_transition.exit_layer")
        except (KeyError, tk.TclError):
            entry = str((self.config.get("via_transition") or {}).get("entry_layer", "L1"))
            exit_layer = str((self.config.get("via_transition") or {}).get("exit_layer", "L10"))
        count = len(metal) or len(stackup)
        self.stackup_summary_var.set(f"{count}层 · {entry}入 / {exit_layer}出")

    def _update_footer_notes(self) -> None:
        try:
            entry = self._backend_value("via_transition.entry_layer")
            exit_layer = self._backend_value("via_transition.exit_layer")
            drill_start = self._backend_value("via_transition.drill_start_layer")
            drill_end = self._backend_value("via_transition.drill_end_layer")
        except (KeyError, tk.TclError):
            return
        self.footer_note_var.set(
            f"电气 {entry}→{exit_layer} · 钻孔 {drill_start}–{drill_end} · 点击预览标注跳转到对应组"
        )
        self._update_stackup_drawer_summary()

    def _build_capacitor(self) -> None:
        super()._build_capacitor()
        advanced = _legacy.ttk.LabelFrame(
            getattr(self, "cap_page_content", self.tabs["电容与模型"]),
            text="布尔连接容差", padding=(8, 5)
        )
        advanced.pack(side="bottom", fill="x", padx=6, pady=(2, 4))
        self.cap_advanced_box = advanced
        advanced.columnconfigure(1, weight=1)
        self._add(
            advanced, 0, "布尔连接容差", "geometry.overlap", length=True,
            formula="仅避免HFSS布尔数值误差；不改变走线轮廓，也不填补折角缺口",
        )

    def _build_antipad_stackup(self) -> None:
        self.antipad_workbench = AntipadWorkbench(self.drawer_host, self)

    def _build_via_authoritative_sections(
            self, sections: Mapping[str, tuple[tk.Widget, tk.Widget]]) -> None:
        """Create the five v3.1 VIA groups.  Antipad/stackup stay in the drawer."""
        self._via_parameter_tab_by_path: dict[str, tk.Widget] = {}
        self._via_parameter_rows: dict[str, tuple[tk.Widget, int]] = {}
        self._via_parameter_group_by_path: dict[str, str] = {}

        def group_frame(section: str, title: str, parent: tk.Widget | None = None) -> _legacy.ttk.LabelFrame:
            host = parent if parent is not None else sections[section][1]
            frame = _legacy.ttk.LabelFrame(host, text=title, padding=8)
            frame.pack(fill="x", padx=3, pady=4)
            frame.columnconfigure(1, weight=1)
            return frame

        def remember(section: str, path: str, widget: tk.Widget | None = None) -> None:
            self._via_parameter_tab_by_path[path] = sections[section][0]
            self._via_parameter_group_by_path[path] = section
            if widget is not None:
                setattr(widget, "_via_parameter_section", section)

        def add(section: str, parent: tk.Widget, row: int, label: str, path: str,
                **kwargs: Any) -> tk.Widget:
            widget = self._add(parent, row, label, path, **kwargs)
            self._via_parameter_rows[path] = (parent, row)
            remember(section, path, widget)
            return widget

        def fixed(section: str, parent: tk.Widget, row: int, label: str, path: str,
                  backend_value: str, display_text: str) -> None:
            self._add_fixed_label(parent, row, label, path, backend_value, display_text)
            self._via_parameter_rows[path] = (parent, row)
            remember(section, path, self.widgets.get(path))

        stackup = self.config.get("stackup", [])
        signal_layers = tuple(str(layer["name"]) for layer in stackup
                              if layer.get("type") == "signal")
        conductive_layers = tuple(str(layer["name"]) for layer in stackup
                                   if layer.get("type") in {"signal", "plane"})

        frame = group_frame("path", "换层方向、中心与层跨度")
        fixed("path", frame, 0, "模式", "via_transition.mode", "diff_pair",
              "差分对（当前仅支持）")
        _legacy.ttk.Label(frame, text="走线方向").grid(row=1, column=0, sticky="w", padx=4, pady=3)
        axis_var = tk.StringVar(value="x")
        self.variables["via_transition.route_axis"] = axis_var
        axis_toggle = SegmentedToggle(
            frame, axis_var, (("x", "X"), ("y", "Y")), width=4)
        axis_toggle.grid(row=1, column=1, sticky="w", padx=4, pady=3)
        self.widgets["via_transition.route_axis"] = axis_toggle
        self._via_parameter_rows["via_transition.route_axis"] = (frame, 1)
        remember("path", "via_transition.route_axis", axis_toggle)
        axis_var.trace_add("write", lambda *_: self._schedule_refresh())

        add("path", frame, 2, "输入层 entry", "via_transition.entry_layer", values=signal_layers)
        add("path", frame, 3, "输出层 exit", "via_transition.exit_layer", values=signal_layers)
        add("path", frame, 4, "机械钻孔起始", "via_transition.drill_start_layer",
            values=conductive_layers)
        add("path", frame, 5, "机械钻孔终止", "via_transition.drill_end_layer",
            values=conductive_layers)
        add("path", frame, 6, "差分对中心距 pair_pitch", "via_transition.pair_pitch", length=True)
        add("path", frame, 7, "信号孔中心距 signal_via_pitch",
            "via_transition.signal_via_pitch", length=True)
        add("path", frame, 8, "中心 X", "via_transition.center_x", length=True)
        add("path", frame, 9, "中心 Y", "via_transition.center_y", length=True)

        frame = group_frame("signal", "信号孔（默认可见）")
        add("signal", frame, 0, "钻孔直径", "via_transition.padstack.drill_diameter", length=True)
        add("signal", frame, 1, "功能焊盘", "via_transition.padstack.connected_pad_diameter", length=True)
        add("signal", frame, 2, "非连接层反焊盘（全局）",
            "via_transition.padstack.antipad_diameter", length=True)
        override = _legacy.ttk.Button(
            frame, text="按层覆盖 →",
            command=lambda: self._navigate_to_parameter("via_transition.padstack.antipad.diameter"),
        )
        override.grid(row=2, column=3, sticky="w", padx=4)
        self.via_layer_override_link = override

        more = NestedMoreSection(sections["signal"][1])
        more.pack(fill="x", padx=3, pady=(0, 4))
        self.via_more_section = more
        add("signal", more.body, 0, "成品孔直径",
            "via_transition.padstack.finished_hole_diameter", length=True,
            formula="= 钻孔 − 2×镀铜")
        add("signal", more.body, 1, "镀铜厚度",
            "via_transition.padstack.plating_thickness", length=True)
        add("signal", more.body, 2, "非功能焊盘",
            "via_transition.padstack.nonfunctional_pad_mode",
            values=("remove", "keep"), display_values={"remove": "移除", "keep": "保留"})
        add("signal", more.body, 3, "非功能焊盘直径",
            "via_transition.padstack.nonfunctional_pad_diameter", length=True,
            formula="选择“移除”时当前模式不适用")
        add("signal", more.body, 4, "筒壁模型", "via_transition.padstack.barrel_model",
            values=("hollow", "solid"),
            display_values={"hollow": "空心镀铜桶", "solid": "实心（仅趋势近似）"})
        self.signal_via_material_combo = add(
            "signal", more.body, 5, "导体材料", "via_transition.padstack.material",
            values=("copper", "pec"),
            display_values={"copper": "铜 copper", "pec": "理想导体 PEC"},
        )

        frame = group_frame("backdrill", "背钻")
        _legacy.ttk.Label(frame, text="启用背钻").grid(row=0, column=0, sticky="w", padx=4, pady=3)
        enabled_var = tk.BooleanVar(value=True)
        self.variables["via_transition.backdrill.enabled"] = enabled_var
        enable_box = _legacy.ttk.Frame(frame)
        enable_box.grid(row=0, column=1, sticky="w", padx=4, pady=3)
        self.backdrill_on_button = _legacy.ttk.Button(
            enable_box, text="开", width=4,
            command=lambda: enabled_var.set(True))
        self.backdrill_off_button = _legacy.ttk.Button(
            enable_box, text="关", width=4,
            command=lambda: enabled_var.set(False))
        self.backdrill_on_button.pack(side="left", padx=1)
        self.backdrill_off_button.pack(side="left", padx=1)
        self.widgets["via_transition.backdrill.enabled"] = enable_box
        self._via_parameter_rows["via_transition.backdrill.enabled"] = (frame, 0)
        remember("backdrill", "via_transition.backdrill.enabled", enable_box)
        enabled_var.trace_add("write", lambda *_: self._on_backdrill_enabled_changed())

        add("backdrill", frame, 1, "背钻方向", "via_transition.backdrill.from_side",
            values=("auto", "top", "bottom"),
            display_values={"auto": "auto", "top": "top", "bottom": "bottom"})
        from_side_widget = self.widgets["via_transition.backdrill.from_side"]
        if isinstance(from_side_widget, _legacy.ttk.Combobox):
            from_side_widget.configure(state="normal")
        self.backdrill_effective_var = tk.StringVar(value="Effective direction: —")
        self.backdrill_effective_label = _legacy.ttk.Label(
            frame, textvariable=self.backdrill_effective_var, foreground="#475569")
        self.backdrill_effective_label.grid(
            row=2, column=0, columnspan=3, sticky="w", padx=4, pady=(0, 4))
        fixed("backdrill", frame, 3, "控制模式", "via_transition.backdrill.control_mode",
              "residual_stub", "目标残桩长度控制（当前仅支持）")
        add("backdrill", frame, 4, "目标残桩（可编）",
            "via_transition.backdrill.residual_stub", length=True)
        add("backdrill", frame, 5, "背钻孔径 drill_diameter",
            "via_transition.backdrill.drill_diameter", length=True)
        self.backdrill_actual_stub_var = tk.StringVar(value="—")
        _legacy.ttk.Label(frame, text="实际残桩（派生）").grid(
            row=6, column=0, sticky="w", padx=4, pady=3)
        _legacy.ttk.Label(
            frame, textvariable=self.backdrill_actual_stub_var, foreground="#334155"
        ).grid(row=6, column=1, sticky="w", padx=4, pady=3)
        self.backdrill_span_echo_var = tk.StringVar(value="—")
        _legacy.ttk.Label(frame, text="钻孔起止层").grid(
            row=7, column=0, sticky="w", padx=4, pady=3)
        _legacy.ttk.Label(
            frame, textvariable=self.backdrill_span_echo_var, foreground="#334155"
        ).grid(row=7, column=1, sticky="w", padx=4, pady=3)

        frame = group_frame("return", "回流地孔（Return VIA）· 布局与层连接")
        add("return", frame, 0, "启用回流地孔", "via_transition.return_vias.enabled", boolean=True)
        add("return", frame, 1, "布局模式", "via_transition.return_vias.pattern",
            values=("side_pair", "four_corner", "custom"),
            display_values={"side_pair": "两侧成对", "four_corner": "四角", "custom": "自定义"})
        add("return", frame, 2, "数量", "via_transition.return_vias.count",
            numeric=True, integer=True, formula="四角固定为 4；自定义由坐标行数自动计算")
        add("return", frame, 3, "X 向偏移", "via_transition.return_vias.offset_x", length=True)
        add("return", frame, 4, "Y 向偏移", "via_transition.return_vias.offset_y", length=True)
        self.return_pitch_x_compat = add(
            "return", frame, 5, "X 向节距（只读）",
            "via_transition.return_vias.pitch_x", length=True,
            formula="当前 side_pair 布局由 X 偏移和 Y 节距决定；兼容旧配置",
        )
        self.return_pitch_x_compat.configure(state="readonly")
        add("return", frame, 6, "Y 向节距", "via_transition.return_vias.pitch_y", length=True)
        add("return", frame, 7, "起始层", "via_transition.return_vias.start_layer", values=conductive_layers)
        add("return", frame, 8, "终止层", "via_transition.return_vias.end_layer", values=conductive_layers)
        _legacy.ttk.Label(frame, text="参考连接层").grid(
            row=9, column=0, sticky="nw", padx=4, pady=5)
        self._register_hidden_variable("via_transition.return_vias.connect_layers")
        self.return_connect_matrix = _legacy.ttk.LabelFrame(
            frame, text="逐层复选（仅显示参考平面）", padding=5)
        self.return_connect_matrix.grid(row=9, column=1, columnspan=3, sticky="ew", padx=4, pady=5)
        self.return_connect_vars: dict[str, tk.BooleanVar] = {}
        self.return_connect_checks: list[tk.Widget] = []
        self.widgets["via_transition.return_vias.connect_layers"] = self.return_connect_matrix
        self._via_parameter_rows["via_transition.return_vias.connect_layers"] = (frame, 9)
        remember("return", "via_transition.return_vias.connect_layers", self.return_connect_matrix)

        _legacy.ttk.Label(frame, text="自定义坐标表").grid(
            row=10, column=0, sticky="nw", padx=4, pady=5)
        self._register_hidden_variable("via_transition.return_vias.custom_coordinates")
        self.return_coordinate_editor = _legacy.ttk.Frame(frame)
        self.return_coordinate_editor.grid(
            row=10, column=1, columnspan=3, sticky="ew", padx=4, pady=5)
        self.return_coordinate_editor.columnconfigure(0, weight=1)
        self.return_coordinate_tree = _legacy.ttk.Treeview(
            self.return_coordinate_editor, columns=("x", "y"), show="headings", height=4,
            selectmode="browse")
        self.return_coordinate_tree.heading("x", text=f"X ({self.display_unit})")
        self.return_coordinate_tree.heading("y", text=f"Y ({self.display_unit})")
        self.return_coordinate_tree.column("x", width=100, anchor="e")
        self.return_coordinate_tree.column("y", width=100, anchor="e")
        self.return_coordinate_tree.grid(row=0, column=0, columnspan=4, sticky="ew")
        self.return_coordinate_x = tk.StringVar()
        self.return_coordinate_y = tk.StringVar()
        _legacy.ttk.Entry(
            self.return_coordinate_editor, textvariable=self.return_coordinate_x, width=12).grid(
                row=1, column=0, sticky="ew", padx=(0, 3), pady=3)
        _legacy.ttk.Entry(
            self.return_coordinate_editor, textvariable=self.return_coordinate_y, width=12).grid(
                row=1, column=1, sticky="ew", padx=3, pady=3)
        _legacy.ttk.Button(
            self.return_coordinate_editor, text="添加行", command=self._return_coordinate_add).grid(
                row=1, column=2, padx=3, pady=3)
        _legacy.ttk.Button(
            self.return_coordinate_editor, text="更新选中", command=self._return_coordinate_update).grid(
                row=1, column=3, padx=3, pady=3)
        _legacy.ttk.Button(
            self.return_coordinate_editor, text="删除选中", command=self._return_coordinate_delete).grid(
                row=2, column=3, padx=3, pady=(0, 3))
        self.return_coordinate_tree.bind("<<TreeviewSelect>>", self._return_coordinate_selected)
        self.widgets["via_transition.return_vias.custom_coordinates"] = self.return_coordinate_editor
        self._via_parameter_rows["via_transition.return_vias.custom_coordinates"] = (frame, 10)
        remember("return", "via_transition.return_vias.custom_coordinates", self.return_coordinate_editor)

        frame = group_frame("return", "回流地孔（Return VIA）· 孔结构")
        add("return", frame, 0, "钻孔直径", "via_transition.return_vias.drill_diameter", length=True)
        add("return", frame, 1, "成品孔直径",
            "via_transition.return_vias.finished_hole_diameter", length=True,
            formula="= 钻孔 − 2×镀铜")
        add("return", frame, 2, "镀铜厚度", "via_transition.return_vias.plating_thickness", length=True)
        add("return", frame, 3, "焊盘直径", "via_transition.return_vias.pad_diameter", length=True)
        add("return", frame, 4, "非连接层反焊盘直径（全局）",
            "via_transition.return_vias.antipad_diameter", length=True)
        add("return", frame, 5, "筒壁模型", "via_transition.return_vias.barrel_model",
            values=("hollow", "solid"),
            display_values={"hollow": "空心镀铜桶", "solid": "实心（仅趋势近似）"})
        self.return_via_material_combo = add(
            "return", frame, 6, "导体材料", "via_transition.return_vias.material",
            values=("copper", "pec"),
            display_values={"copper": "铜 copper", "pec": "理想导体 PEC"},
        )
        self.return_structure_hint = _legacy.ttk.Label(
            frame, text="", foreground="#64748b", wraplength=430)
        self.return_structure_hint.grid(
            row=7, column=0, columnspan=4, sticky="w", padx=4, pady=(3, 0))

        frame = group_frame("traces", "输入/输出传输线")
        add("traces", frame, 0, "输入传输线长度", "via_transition.entry_trace.length", length=True)
        add("traces", frame, 1, "输入传输线宽度", "via_transition.entry_trace.width", length=True)
        add("traces", frame, 2, "输出传输线长度", "via_transition.exit_trace.length", length=True)
        add("traces", frame, 3, "输出传输线宽度", "via_transition.exit_trace.width", length=True)
        add("traces", frame, 4, "扇出方式", "via_transition.fanout.style",
            values=("deg45", "arc"), display_values={"deg45": "45° 折线", "arc": "圆弧"},
            formula="孔间距等于差分对间距时由其他参数决定")
        add("traces", frame, 5, "扇出方向", "via_transition.fanout.direction",
            values=("auto", "expand", "contract"),
            display_values={"auto": "自动", "expand": "向外", "contract": "向内"})
        add("traces", frame, 6, "耦合直线段长", "via_transition.fanout.straight_length", length=True)

        self._install_global_antipad_sync()
        self._on_backdrill_enabled_changed()

    def _return_connect_check_changed(self) -> None:
        if self._ui_sync or not getattr(self, "return_connect_vars", None):
            return
        selected = [name for name, variable in self.return_connect_vars.items()
                    if bool(variable.get())]
        value = ",".join(selected)
        variable = self.variables["via_transition.return_vias.connect_layers"]
        if variable.get() != value:
            variable.set(value)

    def _rebuild_return_connect_matrix(self, config: Mapping[str, Any]) -> None:
        matrix = getattr(self, "return_connect_matrix", None)
        if matrix is None:
            return
        selected = set(str(item) for item in config.get("via_transition", {}).get(
            "return_vias", {}).get("connect_layers", []))
        if getattr(self, "_return_connect_matrix_loaded", False) and not self._ui_sync:
            selected = {name for name, variable in self.return_connect_vars.items()
                        if bool(variable.get())}
        references = [str(layer.get("name", "")) for layer in config.get("stackup", [])
                      if layer.get("type") == "plane" and layer.get("is_reference_plane")]
        if tuple(references) == getattr(self, "_return_reference_names", ()):
            if self._ui_sync:
                for name, variable in self.return_connect_vars.items():
                    variable.set(name in selected)
            return
        for child in matrix.winfo_children():
            child.destroy()
        self.return_connect_vars.clear()
        self.return_connect_checks.clear()
        self._return_reference_names = tuple(references)
        if not references:
            _legacy.ttk.Label(
                matrix, text="当前叠层没有标记为参考平面的层。", foreground="#9a3412").grid(
                    row=0, column=0, sticky="w")
        for index, name in enumerate(references):
            variable = tk.BooleanVar(value=name in selected)
            check = _legacy.ttk.Checkbutton(
                matrix, text=name, variable=variable,
                command=self._return_connect_check_changed)
            check.grid(row=index // 4, column=index % 4, sticky="w", padx=(0, 14), pady=2)
            self.return_connect_vars[name] = variable
            self.return_connect_checks.append(check)
        self._return_connect_matrix_loaded = True
        self._return_connect_check_changed()

    def _refresh_via_custom_coordinate_table(
        self, points_mm: Any = None
    ) -> None:
        tree = getattr(self, "return_coordinate_tree", None)
        variable = self.variables.get("via_transition.return_vias.custom_coordinates")
        if tree is None or variable is None:
            return
        for item in tree.get_children():
            tree.delete(item)
        self._return_coordinate_mm_cache = {}
        self._return_coordinate_display_cache = {}
        if points_mm is None:
            raw = str(variable.get()).strip()
            display_rows = []
            for item in raw.split(";"):
                parts = [part.strip() for part in item.split(",")]
                if len(parts) == 2:
                    display_rows.append(parts)
            points_mm = [
                (
                    convert_length(float(parts[0]), self.display_unit, "mm"),
                    convert_length(float(parts[1]), self.display_unit, "mm"),
                )
                for parts in display_rows
            ]
        for point in points_mm:
            x_mm, y_mm = float(point[0]), float(point[1])
            values = (
                format_length(x_mm, self.display_unit),
                format_length(y_mm, self.display_unit),
            )
            item_id = tree.insert("", "end", values=values)
            self._return_coordinate_mm_cache[item_id] = (x_mm, y_mm)
            self._return_coordinate_display_cache[item_id] = values
        tree.heading("x", text=f"X ({self.display_unit})")
        tree.heading("y", text=f"Y ({self.display_unit})")

    def _return_coordinate_points_mm(self) -> list[list[float]]:
        mm_cache = getattr(self, "_return_coordinate_mm_cache", {})
        display_cache = getattr(self, "_return_coordinate_display_cache", {})
        result: list[list[float]] = []
        for item in self.return_coordinate_tree.get_children():
            values = self.return_coordinate_tree.item(item, "values")
            shown = (str(values[0]), str(values[1]))
            if item in mm_cache and display_cache.get(item) == shown:
                point = mm_cache[item]
            else:
                point = (
                    convert_length(float(shown[0]), self.display_unit, "mm"),
                    convert_length(float(shown[1]), self.display_unit, "mm"),
                )
                mm_cache[item] = point
                display_cache[item] = shown
            result.append([float(point[0]), float(point[1])])
        self._return_coordinate_mm_cache = mm_cache
        self._return_coordinate_display_cache = display_cache
        return result

    def _sync_return_coordinate_variable(self) -> None:
        rows = [self.return_coordinate_tree.item(item, "values")
                for item in self.return_coordinate_tree.get_children()]
        value = "; ".join(f"{values[0]},{values[1]}" for values in rows)
        variable = self.variables["via_transition.return_vias.custom_coordinates"]
        if variable.get() != value:
            variable.set(value)
        if self._backend_value("via_transition.return_vias.pattern") == "custom":
            count = str(len(rows))
            count_variable = self.variables["via_transition.return_vias.count"]
            if count_variable.get() != count:
                count_variable.set(count)

    def _return_coordinate_values(self) -> tuple[str, str] | None:
        x_text = self.return_coordinate_x.get().strip()
        y_text = self.return_coordinate_y.get().strip()
        try:
            float(x_text)
            float(y_text)
        except ValueError:
            self.status.set(f"自定义坐标必须是当前单位 {self.display_unit} 下的数字。")
            return None
        return x_text, y_text

    def _return_coordinate_add(self) -> None:
        values = self._return_coordinate_values()
        if values is None:
            return
        item_id = self.return_coordinate_tree.insert("", "end", values=values)
        point = (
            convert_length(float(values[0]), self.display_unit, "mm"),
            convert_length(float(values[1]), self.display_unit, "mm"),
        )
        self._return_coordinate_mm_cache[item_id] = point
        self._return_coordinate_display_cache[item_id] = values
        self._sync_return_coordinate_variable()

    def _return_coordinate_update(self) -> None:
        selected = self.return_coordinate_tree.selection()
        values = self._return_coordinate_values()
        if not selected or values is None:
            if not selected:
                self.status.set("请先选择要更新的自定义坐标行。")
            return
        item_id = selected[0]
        self.return_coordinate_tree.item(item_id, values=values)
        self._return_coordinate_mm_cache[item_id] = (
            convert_length(float(values[0]), self.display_unit, "mm"),
            convert_length(float(values[1]), self.display_unit, "mm"),
        )
        self._return_coordinate_display_cache[item_id] = values
        self._sync_return_coordinate_variable()

    def _return_coordinate_delete(self) -> None:
        selected = self.return_coordinate_tree.selection()
        if not selected:
            self.status.set("请先选择要删除的自定义坐标行。")
            return
        item_id = selected[0]
        self.return_coordinate_tree.delete(item_id)
        getattr(self, "_return_coordinate_mm_cache", {}).pop(item_id, None)
        getattr(self, "_return_coordinate_display_cache", {}).pop(item_id, None)
        self._sync_return_coordinate_variable()

    def _return_coordinate_selected(self, _event: Any = None) -> None:
        selected = self.return_coordinate_tree.selection()
        if not selected:
            return
        values = self.return_coordinate_tree.item(selected[0], "values")
        if len(values) == 2:
            self.return_coordinate_x.set(str(values[0]))
            self.return_coordinate_y.set(str(values[1]))

    def _set_via_parameter_row_visible(self, path: str, visible: bool) -> None:
        location = getattr(self, "_via_parameter_rows", {}).get(path)
        if location is None:
            return
        parent, row = location
        for widget in parent.grid_slaves(row=row):
            (widget.grid if visible else widget.grid_remove)()

    def _reveal_via_parameter(self, path: str, widget: tk.Widget) -> None:
        kind, key = navigation_target(path)
        if kind == "drawer":
            self.open_stackup_drawer()
        elif kind == "sheet":
            self.open_sweep_sheet()
        elif kind == "group":
            self.select_via_group(key)
            more = getattr(self, "via_more_section", None)
            hidden_in_more = path in {
                "via_transition.padstack.finished_hole_diameter",
                "via_transition.padstack.plating_thickness",
                "via_transition.padstack.nonfunctional_pad_mode",
                "via_transition.padstack.nonfunctional_pad_diameter",
                "via_transition.padstack.barrel_model",
                "via_transition.padstack.material",
            }
            if more is not None and hidden_in_more:
                more.expand()
        scroller = self.via_parameter_scrollers.get("main")
        if scroller is not None and kind == "group":
            scroller.scroll_to_widget(widget)
        try:
            widget.focus_set()
            if isinstance(widget, _legacy.ttk.Entry):
                widget.selection_range(0, "end")
                widget.icursor("end")
        except tk.TclError:
            pass

    def _build_via_transition(self) -> None:
        """Build the five v3.1 middle groups inside the VIA workspace."""
        scroller = ScrollableFrame(self.via_workspace, fill_viewport=True)
        scroller.pack(fill="both", expand=True)
        self.via_parameter_scrollers = {"main": scroller}
        self.via_parameter_groups = {}
        self.via_parameter_tabs = {}
        sections: dict[str, tuple[tk.Widget, tk.Widget]] = {}
        defaults_expanded = {"path": True, "signal": True, "backdrill": True,
                             "return": False, "traces": False}
        for key in VIA_GROUP_KEYS:
            group = CollapsibleGroup(
                scroller.content, key, VIA_GROUP_TITLES[key],
                expanded=defaults_expanded[key],
                on_toggle=lambda item: setattr(self, "via_active_group", item.key),
            )
            group.pack(fill="x", padx=4, pady=4)
            if not defaults_expanded[key]:
                summary = "未展开"
                if key == "return":
                    summary = "未展开 · 当前已启用回流孔"
                group.collapse(summary)
            self.via_parameter_groups[key] = group
            self.via_parameter_tabs[key] = group
            sections[key] = (group, group.body)
        self._build_via_authoritative_sections(sections)

        self.via_feedback_var = tk.StringVar(value="输入后自动更新。")
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

    def _on_backdrill_enabled_changed(self, *_args: Any) -> None:
        enabled = bool(self.variables.get("via_transition.backdrill.enabled", tk.BooleanVar(value=True)).get())
        try:
            self.backdrill_on_button.state(["pressed"] if enabled else ["!pressed"])
            self.backdrill_off_button.state(["!pressed"] if enabled else ["pressed"])
        except (tk.TclError, AttributeError):
            pass
        if getattr(self, "model_preview_workbench", None) is not None:
            self._schedule_refresh()
        else:
            self._update_backdrill_derived_echoes()

    def _install_global_antipad_sync(self) -> None:
        src = self.variables.get("via_transition.padstack.antipad_diameter")
        if src is None:
            return

        def sync_to_nested(*_args: Any) -> None:
            if self._ui_sync:
                return
            dst = self.variables.get("via_transition.padstack.antipad.diameter")
            if dst is None:
                return
            if str(dst.get()) != str(src.get()):
                was = self._ui_sync
                self._ui_sync = True
                try:
                    dst.set(src.get())
                finally:
                    self._ui_sync = was

        def sync_from_nested(*_args: Any) -> None:
            if self._ui_sync:
                return
            dst = self.variables.get("via_transition.padstack.antipad.diameter")
            if dst is None:
                return
            if str(src.get()) != str(dst.get()):
                was = self._ui_sync
                self._ui_sync = True
                try:
                    src.set(dst.get())
                finally:
                    self._ui_sync = was

        src.trace_add("write", sync_to_nested)
        nested = self.variables.get("via_transition.padstack.antipad.diameter")
        if nested is not None:
            nested.trace_add("write", sync_from_nested)

    def _schedule_refresh(self) -> None:
        if getattr(self, "_destroying", False) or getattr(self, "_refreshing", False):
            return
        if not self._ui_sync:
            self._mark_preview_dirty()
        super()._schedule_refresh()

    def load_json(self) -> None:
        path = _legacy.filedialog.askopenfilename(
            parent=self, filetypes=(("JSON", "*.json"), ("所有文件", "*.*")))
        if not path:
            return
        try:
            config, corrections = self.manager.load(Path(path))
            self._config_source_name = Path(path).name
            self.config_name_var.set(self._config_source_name)
            self._load_to_widgets(config)
            self._mark_preview_dirty()
            self._set_log("已载入：" + path + "\n" + "\n".join(corrections))
        except _legacy.ConfigError as exc:
            messagebox.showerror("载入失败", str(exc), parent=self)

    def _draw_via_top_view(self, config: Mapping[str, Any], geometry: Any) -> None:
        super()._draw_via_top_view(config, geometry)
        canvas = getattr(self, "via_top_canvas", None)
        if canvas is None:
            return
        vt = config.get("via_transition", {})
        unit = self.display_unit
        pair = format_length(float(vt.get("pair_pitch", 0.0) or 0.0), unit)
        hole = format_length(float(vt.get("signal_via_pitch", vt.get("pair_pitch", 0.0)) or 0.0), unit)
        width = max(2, int(canvas.winfo_width() or canvas.winfo_reqwidth() or 400))
        height = max(2, int(canvas.winfo_height() or canvas.winfo_reqheight() or 300))
        canvas.create_text(
            16, height - 28, anchor="sw",
            text=f"走线距 {pair} pair_pitch",
            fill="#334155", font="ViaCanvasFont",
            tags=("dimension-label", "dimension:via_transition.pair_pitch", "pair_pitch"),
        )
        canvas.create_text(
            width - 16, height - 28, anchor="se",
            text=f"孔距 {hole} signal_via_pitch",
            fill="#334155", font="ViaCanvasFont",
            tags=("dimension-label", "dimension:via_transition.signal_via_pitch", "signal_via_pitch"),
        )

    def _on_via_antipad_click(self, _event: tk.Event) -> None:
        self._navigate_to_parameter("via_transition.padstack.antipad.diameter")

    def _on_via_preview_object_click(self, event: tk.Event) -> None:
        canvas = event.widget
        tags = canvas.gettags("current")
        path = preview_click_path("", tags)
        if path:
            self._navigate_to_parameter(path)

    def _build_vias(self) -> None:
        self.gnd_via_workbench = GndViaWorkbench(self.tabs["GND VIA"], self)

    def _scenario_changed(self, *_args: Any) -> None:
        if self._ui_sync:
            return
        self._apply_scenario_linkage()
        if self._scenario_is_via():
            self.select_via_group("path")
            self._sync_via_fields()
            self.status.set(
                "已切换到“差分 VIA 换层”场景：请在中部五组编辑换层路径、信号孔、背钻、回流孔和进出走线。"
            )
        else:
            self.status.set(
                "已切换到“AC 耦合电容”场景：电容/GND/差分走线参数生效，VIA 换层参数停用。"
            )
        self._schedule_refresh()

    def _apply_scenario_linkage(self) -> None:
        is_via = self._scenario_is_via()
        for page, enabled in (("电容与模型", not is_via), ("GND VIA", not is_via)):
            tab_id = self.tabs.get(page)
            if tab_id is not None:
                try:
                    self.notebook.tab(tab_id, state="normal" if enabled else "disabled")
                except tk.TclError:
                    pass
        self._set_group_state(self._CAP_ONLY_PREFIXES, enabled=not is_via)
        self._set_group_state(self._VIA_ONLY_PREFIXES, enabled=is_via)
        from_side = self.widgets.get("via_transition.backdrill.from_side")
        if isinstance(from_side, _legacy.ttk.Combobox) and is_via:
            try:
                from_side.configure(state="normal")
            except tk.TclError:
                pass
        antipad_diameter = self.widgets.get("via_transition.padstack.antipad_diameter")
        if antipad_diameter is not None and is_via:
            self._configure_widget_state(antipad_diameter, True)
        if hasattr(self, "antipad_workbench"):
            self.antipad_workbench.set_scenario_editable(not is_via)
        if hasattr(self, "simulation_sweep_workbench"):
            self.simulation_sweep_workbench.set_sweep_scenario_enabled(True, "")
        self._update_scenario_hints(is_via)
        self._show_scenario_workspace()
        self._set_group_state(("plane.",), enabled=True)

    def _refresh_via_layer_selectors(self, config: Mapping[str, Any]) -> None:
        super()._refresh_via_layer_selectors(config)
        self._rebuild_return_connect_matrix(config)
        materials = {
            str(layer.get("material", "")).strip()
            for layer in config.get("stackup", [])
            if layer.get("type") in {"signal", "plane"}
            and str(layer.get("material", "")).strip()
        }
        vt = config.get("via_transition", {})
        materials.add(str(vt.get("padstack", {}).get("material", "copper")))
        materials.add(str(vt.get("return_vias", {}).get("material", "copper")))
        custom = sorted(name for name in materials if name not in {"copper", "pec", ""})
        values = ("铜 copper", "理想导体 PEC", *custom)
        for combo in (getattr(self, "signal_via_material_combo", None),
                      getattr(self, "return_via_material_combo", None)):
            if isinstance(combo, _legacy.ttk.Combobox):
                combo.configure(values=values)

    def _length_mm_from_widget(self, path: str) -> float | None:
        """Read a length field as mm, preferring the unrounded cache."""
        variable = self.variables.get(path)
        if variable is None:
            return None
        text = str(variable.get()).strip()
        if text == self._length_display_snapshot.get(path) and path in self._length_mm_cache:
            return float(self._length_mm_cache[path])
        try:
            return convert_length(float(text), self.display_unit, "mm")
        except (TypeError, ValueError, tk.TclError):
            return None

    def _commit_finished_hole_from_drill_plating(self, drill_path: str, plating_path: str,
                                                 finished_path: str) -> None:
        drill_mm = self._length_mm_from_widget(drill_path)
        plating_mm = self._length_mm_from_widget(plating_path)
        if drill_mm is None or plating_mm is None:
            return
        finished_mm = drill_mm - 2.0 * plating_mm
        finished_display = format_length(finished_mm, self.display_unit)
        was = self._ui_sync
        self._ui_sync = True
        try:
            self._length_mm_cache[finished_path] = finished_mm
            self._length_display_snapshot[finished_path] = finished_display
            self.variables[finished_path].set(finished_display)
        finally:
            self._ui_sync = was

    def _sync_via_fields(self) -> None:
        super()._sync_via_fields()
        self._commit_finished_hole_from_drill_plating(
            "via_transition.padstack.drill_diameter",
            "via_transition.padstack.plating_thickness",
            "via_transition.padstack.finished_hole_diameter",
        )
        self._commit_finished_hole_from_drill_plating(
            "via_transition.return_vias.drill_diameter",
            "via_transition.return_vias.plating_thickness",
            "via_transition.return_vias.finished_hole_diameter",
        )
        return_enabled = bool(
            self.variables["via_transition.return_vias.enabled"].get())
        pattern = self._backend_value("via_transition.return_vias.pattern")
        layout_fields = {
            "via_transition.return_vias.offset_x",
            "via_transition.return_vias.offset_y",
            "via_transition.return_vias.pitch_x",
            "via_transition.return_vias.pitch_y",
            "via_transition.return_vias.custom_coordinates",
        }
        active = {
            "side_pair": {
                "via_transition.return_vias.offset_x",
                "via_transition.return_vias.offset_y",
                "via_transition.return_vias.pitch_x",
                "via_transition.return_vias.pitch_y",
            },
            "four_corner": {
                "via_transition.return_vias.offset_x",
                "via_transition.return_vias.offset_y",
            },
            "custom": {"via_transition.return_vias.custom_coordinates"},
        }.get(pattern, set())
        for path in layout_fields:
            self._set_via_parameter_row_visible(path, return_enabled and path in active)
        for path in (
            "via_transition.return_vias.count",
            "via_transition.return_vias.start_layer",
            "via_transition.return_vias.end_layer",
            "via_transition.return_vias.connect_layers",
        ):
            self._set_via_parameter_row_visible(path, return_enabled)

        for check in getattr(self, "return_connect_checks", ()):
            check.configure(state="normal" if return_enabled else "disabled")
        coordinate_active = return_enabled and pattern == "custom"
        coordinate_editor = getattr(self, "return_coordinate_editor", None)
        if coordinate_editor is not None:
            for child in coordinate_editor.winfo_children():
                if isinstance(child, (_legacy.ttk.Entry, _legacy.ttk.Button)):
                    child.configure(state="normal" if coordinate_active else "disabled")
        for path in (
            "via_transition.return_vias.barrel_model",
            "via_transition.return_vias.material",
        ):
            widget = self.widgets.get(path)
            if widget is not None:
                self._configure_widget_state(widget, return_enabled)
        if hasattr(self, "return_pitch_x_compat"):
            self.return_pitch_x_compat.configure(
                state="readonly" if return_enabled and pattern == "side_pair" else "disabled")
        if hasattr(self, "return_structure_hint"):
            self.return_structure_hint.configure(text=(
                "回流地孔未启用：孔结构参数保留在配置中，当前模式不参与几何。"
                if not return_enabled else
                "成品孔径自动计算；实心筒壁仅用于趋势近似。"
            ))

        nfp_keep = self._backend_value(
            "via_transition.padstack.nonfunctional_pad_mode") == "keep"
        self._set_via_parameter_row_visible(
            "via_transition.padstack.nonfunctional_pad_diameter", nfp_keep)
        try:
            signal_pitch = convert_length(
                float(self.variables["via_transition.signal_via_pitch"].get()),
                self.display_unit, "mm")
            pair_pitch = convert_length(
                float(self.variables["via_transition.pair_pitch"].get()),
                self.display_unit, "mm")
            fanout_active = not math.isclose(signal_pitch, pair_pitch, abs_tol=1e-12)
        except (KeyError, TypeError, ValueError, tk.TclError):
            fanout_active = True
        for path in (
            "via_transition.fanout.style",
            "via_transition.fanout.direction",
            "via_transition.fanout.straight_length",
        ):
            self._set_via_parameter_row_visible(path, fanout_active)
        self._update_backdrill_derived_echoes()
        self._update_return_group_summary()
        from_side = self.widgets.get("via_transition.backdrill.from_side")
        if isinstance(from_side, _legacy.ttk.Combobox):
            backdrill_enabled = bool(self.variables["via_transition.backdrill.enabled"].get())
            try:
                from_side.configure(state="normal" if backdrill_enabled else "disabled")
            except tk.TclError:
                pass

    def _update_backdrill_derived_echoes(self) -> None:
        try:
            drill_start = self._backend_value("via_transition.drill_start_layer")
            drill_end = self._backend_value("via_transition.drill_end_layer")
            entry = self._backend_value("via_transition.entry_layer")
            exit_layer = self._backend_value("via_transition.exit_layer")
            side = self._backend_value("via_transition.backdrill.from_side")
        except (KeyError, tk.TclError):
            return
        if hasattr(self, "backdrill_span_echo_var"):
            self.backdrill_span_echo_var.set(f"{drill_start} – {drill_end}")
        effective = side
        if side == "auto":
            top_stub = entry != drill_start
            bottom_stub = exit_layer != drill_end
            if top_stub and not bottom_stub:
                effective = "top"
            elif bottom_stub and not top_stub:
                effective = "bottom"
            elif top_stub and bottom_stub:
                effective = "ambiguous"
            else:
                effective = "none"
            if hasattr(self, "backdrill_effective_var"):
                self.backdrill_effective_label.grid()
                self.backdrill_effective_var.set(f"Effective direction: {effective}")
        elif hasattr(self, "backdrill_effective_label"):
            self.backdrill_effective_label.grid_remove()
        self._update_footer_notes()

    def _update_return_group_summary(self) -> None:
        group = self.via_parameter_groups.get("return")
        if group is None or group.expanded:
            return
        enabled = bool(self.variables["via_transition.return_vias.enabled"].get())
        try:
            count = int(float(self.variables["via_transition.return_vias.count"].get()))
        except (TypeError, ValueError, tk.TclError):
            count = 0
        if not enabled:
            group.summary_var.set("未展开 · 未启用")
        else:
            group.summary_var.set(f"未展开 · 当前已启用 {count} 孔")

    def _build_simulation_sweeps(self) -> None:
        self.simulation_sweep_workbench = SimulationSweepWorkbench(
            self.sweep_sheet_host, self
        )

    def _build_model_preview(self) -> None:
        self.model_preview_workbench = ModelPreviewWorkbench(
            self.preview_iso_host, self, self._navigate_to_parameter
        )
        self.model_preview_workbench.pack(fill="both", expand=True)
        self.set_preview_view("俯视")

    def _build_validate_results(self) -> None:
        self.validate_workbench = ValidateResultsWorkbench(self.validate_overlay, self)
        self.log_text = self.validate_workbench.raw_text

    def _navigate_to_parameter(self, path: str) -> None:
        """Select scenario rail, then open the matching group / drawer / sheet and focus."""
        kind, key = navigation_target(path)
        if kind in {"group", "drawer"} or path.startswith("via_transition."):
            self._select_scenario_backend("via_transition")
        elif kind in {"project", "capacitor", "gnd"}:
            self._select_scenario_backend("surface_capacitor")
        if kind == "drawer":
            self.open_stackup_drawer()
        elif kind == "sheet":
            self.open_sweep_sheet()
        elif kind == "group":
            self.select_via_group(key)
        elif kind == "project":
            try:
                self.notebook.select(self.tabs["项目与走线"])
            except tk.TclError:
                pass
        elif kind == "capacitor":
            try:
                self.notebook.select(self.tabs["电容与模型"])
            except tk.TclError:
                pass
        elif kind == "gnd":
            try:
                self.notebook.select(self.tabs["GND VIA"])
            except tk.TclError:
                pass
        elif kind == "preview":
            self.set_preview_view("等轴")
        elif kind == "validate":
            self._show_validate_overlay()

        widget = self.widgets.get(path)
        if widget is None:
            self.status.set(f"已定位参数：{path}")
            return

        def focus() -> None:
            if kind == "group":
                self._reveal_via_parameter(path, widget)
            elif kind == "drawer" and path.startswith("via_transition."):
                self.antipad_workbench.reveal_via_parameter(path, widget)
            elif kind == "drawer":
                self.antipad_workbench._scroll_settings_to(widget)
            elif kind == "capacitor":
                self.cap_parameter_scroller.scroll_to_widget(widget)
            elif kind == "gnd":
                for scroller in (self.gnd_via_workbench.left_scroll,
                                 self.gnd_via_workbench.right_scroll):
                    if str(widget).startswith(str(scroller.content)):
                        content_height = max(1, scroller.content.winfo_reqheight())
                        viewport_height = max(1, scroller.canvas.winfo_height())
                        movable = max(1, content_height - viewport_height)
                        y = widget.winfo_rooty() - scroller.content.winfo_rooty()
                        scroller.canvas.yview_moveto(max(0.0, min(1.0, (y - 20) / movable)))
                        break
            try:
                widget.focus_set()
                if isinstance(widget, _legacy.ttk.Entry):
                    widget.selection_range(0, "end")
            except tk.TclError:
                pass
            self.status.set(f"已定位参数：{path}")

        self.after_idle(focus)

    def validate_model(self, show_dialog: bool = True) -> bool:
        """Run the structured local preflight without starting AEDT."""
        if self._scenario_is_via():
            try:
                self._sync_via_fields()
            except (tk.TclError, KeyError, TypeError, ValueError):
                pass
        validator = ModelValidator(self.manager)
        try:
            raw = self._collect()
        except Exception as exc:
            raw = copy.deepcopy(getattr(self, "config", {}))
            report = validator.failed_report(raw, f"无法读取当前输入：{exc}")
            self.validate_report = report
            self.validate_workbench.show_report(report)
            self._show_validate_overlay()
            self._mark_preview_dirty()
            self.footer_error_var.set("错误 1")
            if show_dialog:
                messagebox.showerror("Validate模型检查", "快速预检失败，请查看Validate结果。", parent=self)
            return False
        self.validate_workbench.set_progress("正在开始本地快速预检", 2)
        report, config, geometry = validator.run_local(raw, self.validate_workbench.set_progress)
        self.validate_report = report
        self.validate_workbench.show_report(report)
        self._show_validate_overlay()
        if config is not None:
            self.config = config
            self._validated_config = copy.deepcopy(config)
            self._validated_geometry = geometry
            if geometry is not None:
                self._validated_snapshot = make_generation_snapshot(config, geometry)
                self._preview_validation = report
                self.model_preview_workbench.show(config, geometry, report, self._validated_snapshot)
                self.model_preview_workbench.update_pending()
        valid = report.counts["errors"] == 0 and not report.incomplete
        if valid:
            self._mark_preview_clean(
                errors=int(report.counts.get("errors", 0)),
                warnings=int(report.counts.get("warnings", 0)),
            )
        else:
            self._mark_preview_dirty()
            self.footer_error_var.set(f"错误 {report.counts.get('errors', 0)}")
            self.footer_warn_var.set(f"警告 {report.counts.get('warnings', 0)}")
            self.footer_status_var.set("校验失败 · 禁止生成")
        if show_dialog:
            if not valid:
                messagebox.showerror("Validate模型检查", "快速预检失败；存在阻止建模的错误。", parent=self)
            elif report.counts["warnings"]:
                messagebox.showwarning(
                    "Validate模型检查",
                    "快速预检通过但有警告。当前模型具备建模条件，但不代表仿真一定收敛或结果准确。",
                    parent=self,
                )
            else:
                messagebox.showinfo(
                    "Validate模型检查",
                    "本地快速预检通过：当前模型具备建模条件。\n此结论不代表仿真一定收敛或结果准确；HFSS实际工程仍需建模后检查。",
                    parent=self,
                )
        return valid

    def validate_parameters(self, show_dialog: bool = True) -> bool:
        return self.validate_model(show_dialog)

    def _confirm_validate_warnings(self) -> bool:
        """Warnings never block generation; errors are gated by validate_model."""
        return True

    def _generate(self):
        snapshot = getattr(self, "_validated_snapshot", None)
        if snapshot is None:
            if not self.validate_model(show_dialog=False):
                raise _legacy.ScriptGenerationError("快速Validate存在错误，已禁止生成HFSS脚本。")
            snapshot = getattr(self, "_validated_snapshot", None)
        if snapshot is None:
            raise _legacy.ScriptGenerationError("尚未生成有效的预览快照，已停止HFSS生成。")
        try:
            current_config, _ = normalize_config(self._collect())
        except Exception as exc:
            raise _legacy.ScriptGenerationError("无法读取当前配置，已停止HFSS生成：{}".format(exc)) from exc
        current_hash = config_hash(current_config)
        if current_hash != str(snapshot["config_hash"]):
            raise _legacy.ScriptGenerationError(
                "预览与生成的 config_hash 不一致，已停止生成：预览={} 当前={}".format(
                    snapshot["config_hash"], current_hash
                )
            )
        # A debounced preview may still be pending when the user clicks
        # Generate. Commit that exact pending snapshot first, then require the
        # panel's displayed snapshot to be the same contract used by HFSS.
        preview_workbench = getattr(self, "model_preview_workbench", None)
        if preview_workbench is not None:
            if getattr(preview_workbench, "pending", None) is not None:
                preview_workbench.update_pending()
            displayed_snapshot = getattr(preview_workbench, "current_snapshot", None)
            if displayed_snapshot is None or any(
                    str(displayed_snapshot.get(key, "")) != str(snapshot.get(key, ""))
                    for key in ("generation_id", "config_hash", "geometry_hash")):
                raise _legacy.ScriptGenerationError(
                    "当前3D预览尚未提交与生成相同的快照，请先刷新/更新预览后再生成HFSS工程。"
                )
        preview_validation = getattr(self, "_preview_validation", None)
        if preview_validation is not None and getattr(preview_validation, "errors", None):
            raise _legacy.ScriptGenerationError("当前预览快照存在几何/配置错误，已停止HFSS生成。")
        if not self._confirm_validate_warnings():
            raise _legacy.ScriptGenerationError("用户未确认Validate警告，已取消生成。")
        config = copy.deepcopy(self._validated_config)
        generated = _legacy.HfssScriptGenerator(config, _legacy.SOURCE_DIRECTORY).generate(snapshot=snapshot)
        self.config = config
        self.log_text.insert(
            "end",
            "\n\nHFSS脚本生成完成\n" + str(generated.script_path) + "\n" +
            "\n".join("[警告] " + item for item in generated.warnings),
        )
        self.log_text.see("end")
        self._show_validate_overlay()
        return generated

    def generate_sweeps(self) -> None:
        """Apply the same Validate gate before emitting any sweep scripts."""
        try:
            if not self.validate_model(show_dialog=False):
                raise _legacy.ScriptGenerationError(
                    "快速Validate存在错误，已禁止创建参数扫描。"
                )
            if not self._confirm_validate_warnings():
                raise _legacy.ScriptGenerationError(
                    "用户未确认Validate警告，已取消创建参数扫描。"
                )
            config = copy.deepcopy(self._validated_config)
            root = (self.manager.output_directory(config) /
                    (config["project"]["project_name"] + "_SWEEPS"))
            generated = _legacy.generate_sweep_cases(config, root)
            manifest = json.loads(generated.manifest_path.read_text(encoding="utf-8"))
            if manifest.get("backend") == "native_optimetrics":
                from sweep_manager import assert_native_manifest_safe
                assert_native_manifest_safe(generated.manifest_path)
            self.log_text.insert(
                "end",
                f"\nsweep_backend = {manifest.get('backend')}"
                f"\nproject_count = {manifest.get('project_count')}"
                f"\ndesign_count = {manifest.get('design_count')}"
                f"\nsetup_name = {config.get('sweep_execution', {}).get('optimetrics_setup_name', 'ParametricSetup1')}"
                f"\ndesign_variation_count = {manifest.get('design_variation_count')}"
            )
            self.log_text.insert(
                "end",
                (f"\n\n已创建一个原生 HFSS 参数扫描；{manifest.get('design_variation_count')} 个 Variation "
                 "共享一个 Project、一个 Design、一个脚本。"
                 if manifest.get("backend") == "native_optimetrics" else
                 f"\n\n已创建 {manifest.get('design_count')} 个重新计算的离散 HFSS Design/case；"
                 "拓扑、clearance 与逐层轮廓未被伪装为连续变量。") +
                f"\n清单：{generated.manifest_path}",
            )
            self.log_text.see("end")
            self._show_validate_overlay()
        except Exception as exc:
            messagebox.showerror("扫描生成失败", str(exc), parent=self)

    def generate_hfss_offline(self) -> None:
        """Generate the HFSS script without launching AEDT Desktop."""
        try:
            generated = self._generate()
            self.status.set(
                f"已生成 HFSS 脚本（离线审阅，未启动 AEDT Desktop）：{generated.script_path}"
            )
        except Exception as exc:
            messagebox.showerror("生成失败", str(exc), parent=self)

    def generate_and_run(self) -> None:
        """Build the project; via_transition never Popen's AEDT Desktop."""
        if self._scenario_is_via():
            self.generate_hfss_offline()
            return
        try:
            generated = self._generate()
            command = ([sys.executable, "--run-generated-script", str(generated.script_path)]
                       if _legacy.IS_FROZEN else [sys.executable, str(generated.script_path)])
            kwargs: dict[str, Any] = {"cwd": str(generated.project_directory)}
            if sys.platform.startswith("win"):
                kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
            self._hfss_process = subprocess.Popen(command, **kwargs)
            project_name = self._validated_config["project"]["project_name"]
            self._hfss_report_path = generated.project_directory / f"{project_name}_validate_hfss.json"
            self._hfss_cancelled = False
            self.validate_workbench.set_progress("HFSS正在建模并执行工程检查", 10)
            self.status.set("已启动HFSS建模；完成后自动导入工程检查结果")
            self._poll_hfss_validate()
        except Exception as exc:
            messagebox.showerror("启动失败", str(exc), parent=self)

    def run_hfss_project_validate(self) -> None:
        self.generate_and_run()

    def _poll_hfss_validate(self) -> None:
        process = getattr(self, "_hfss_process", None)
        if process is None:
            return
        code = process.poll()
        if code is None:
            current = float(self.validate_workbench.progress["value"])
            self.validate_workbench.set_progress("HFSS工程检查进行中", min(90, int(current) + 2))
            self._hfss_poll_job = self.after(1200, self._poll_hfss_validate)
            return
        self._hfss_poll_job = None
        path = getattr(self, "_hfss_report_path", None)
        if path and Path(path).is_file():
            try:
                self.merge_hfss_report(load_report(Path(path)))
                self.status.set("HFSS工程检查结果已导入")
                return
            except Exception as exc:
                self.status.set(f"HFSS结果读取失败：{exc}")
        base = copy.deepcopy(getattr(self, "validate_report", None))
        if base is None:
            return
        level = "not_checked" if getattr(self, "_hfss_cancelled", False) else "error"
        item = ValidationItem(
            "HFSS-999", level, "HFSS实际工程",
            "HFSS工程检查已取消。" if level == "not_checked" else f"HFSS工程检查进程退出，代码 {code}，未生成结构化报告。",
            suggestion="查看生成目录中的build.log和失败现场工程。", source="hfss",
            timestamp=base.timestamp, config_revision=base.config_revision,
        )
        base.items.append(item); base.incomplete = level == "not_checked"
        self.validate_report = base; self.validate_workbench.show_report(base)

    def cancel_hfss_validate(self) -> None:
        process = getattr(self, "_hfss_process", None)
        if process is None or process.poll() is not None:
            self.status.set("当前没有正在执行的HFSS工程检查")
            return
        self._hfss_cancelled = True
        process.terminate()
        self.validate_workbench.set_progress("正在取消HFSS工程检查", 0)

    def merge_hfss_report(self, hfss_report: ValidationReport) -> None:
        local = getattr(self, "validate_report", None)
        if local is None:
            merged = hfss_report
        else:
            merged = local.merge(hfss_report)
            if hfss_report.config_revision != local.config_revision:
                merged.stale = True
        self.validate_report = merged
        self.validate_workbench.show_report(merged)
        if getattr(self, "_validated_geometry", None) is not None and getattr(self, "_validated_config", None) is not None:
            self.model_preview_workbench.show(
                self._validated_config, self._validated_geometry, merged,
                getattr(self, "_validated_snapshot", None),
            )
            self.model_preview_workbench.update_pending()
        self._show_validate_overlay()

    def locate_validate_item(self, item: ValidationItem) -> None:
        if item.object_keys:
            self.set_preview_view("等轴")
            self.model_preview_workbench.locate_validation_item(item)
        elif item.parameter_path:
            self._navigate_to_parameter(item.parameter_path)
        else:
            self.status.set(f"{item.rule_id} 没有关联的3D对象；请查看详细建议。")

    def apply_validate_auto_fix(self, item: ValidationItem) -> None:
        try:
            current = self._collect()
            updated, changes = preview_auto_fix(current, item.auto_fix_id)
            if not changes:
                messagebox.showinfo("自动修复", "当前值已经满足该安全修复规则。", parent=self)
                return
            detail = "\n".join(f"{path}: {before!r} → {after!r}" for path, before, after in changes)
            if not messagebox.askyesno("确认自动修复", "将执行以下可逆修改：\n\n" + detail + "\n\n是否继续？", parent=self):
                return
            self._load_to_widgets(updated)
            self.validate_model(show_dialog=False)
        except Exception as exc:
            messagebox.showerror("自动修复失败", str(exc), parent=self)

    def _load_to_widgets(self, config: Any) -> None:
        super()._load_to_widgets(config)
        if hasattr(self, "return_coordinate_tree"):
            points = self.config["via_transition"]["return_vias"].get(
                "custom_coordinates", []
            )
            self._refresh_via_custom_coordinate_table(points)
        if hasattr(self, "gnd_via_workbench"):
            self.gnd_via_workbench.load_config(self.config)
        if hasattr(self, "simulation_sweep_workbench"):
            self.simulation_sweep_workbench.load_config(self.config)

    def _collect(self) -> Any:
        if hasattr(self, "return_connect_vars"):
            self._return_connect_check_changed()
        if hasattr(self, "return_coordinate_tree"):
            self._sync_return_coordinate_variable()
        if hasattr(self, "simulation_sweep_workbench"):
            self.simulation_sweep_workbench.prepare_collect()
        if hasattr(self, "gnd_via_workbench"):
            self.gnd_via_workbench.prepare_collect()
        result = super()._collect()
        if hasattr(self, "return_coordinate_tree"):
            points = self._return_coordinate_points_mm()
            result["via_transition"]["return_vias"]["custom_coordinates"] = points
            if result["via_transition"]["return_vias"].get("pattern") == "custom":
                result["via_transition"]["return_vias"]["count"] = len(points)
        return result

    def _refresh_stackup(self) -> None:
        if hasattr(self, "antipad_workbench"):
            if self._scenario_is_via():
                self.antipad_workbench.refresh_table_via(self.config)
            else:
                self.antipad_workbench.refresh_table(self.config)
        if hasattr(self, "gnd_via_workbench"):
            self.gnd_via_workbench.refresh_stackup(self.config)

    def _edit_layer(self, _event: Any = None) -> None:
        self.antipad_workbench.edit_layer()

    def refresh_visuals(self) -> None:
        """沿用全局预览，再按场景选择性地刷新各独立工作台。

        VIA 换层几何结果是 ``ViaTransitionGeometryResult``，没有
        ``gnd_vias`` / ``antipads_by_layer`` / ``signal_shapes`` /
        ``capacitor_centers`` 等电容场景属性；不能喂给 surface_capacitor
        专用工作台，否则会抛 ``AttributeError`` 并连累 3D 模型预览。
        """
        if getattr(self, "_destroying", False) or getattr(self, "_refreshing", False):
            return
        self._refreshing = True
        try:
            super().refresh_visuals()
            try:
                config, validation = self.manager.validate(
                    self._collect(), check_output_directory=False
                )
                # Commit the provenance-aware follow result back to the editable
                # model before any workbench reads ``app.config``.  Numeric text
                # fields remain owned by their Tk variables; only the structured
                # stackup and automatic-layer state are synchronized here.
                self.config["stackup"] = copy.deepcopy(config["stackup"])
                self.config["antipad"]["follow_mount_side"] = bool(
                    config["antipad"].get("follow_mount_side")
                )
                self.config["antipad"]["auto_follow_layer"] = str(
                    config["antipad"].get("auto_follow_layer", "")
                )
                geometry = GeometryCalculator(config).calculate()
                for error in geometry.errors:
                    validation.add_error(error)
                for warning in geometry.warnings:
                    validation.add_warning(warning)
                snapshot = make_generation_snapshot(config, geometry)
                self._preview_snapshot = snapshot
                is_via = scenario_type(config) == VIA_TRANSITION
                if is_via:
                    self.config["via_transition"]["padstack"]["antipad"] = copy.deepcopy(
                        config["via_transition"]["padstack"]["antipad"]
                    )
                    self.config["via_transition"]["padstack"]["antipad_diameter"] = float(
                        config["via_transition"]["padstack"]["antipad_diameter"]
                    )
                    self.config["via_transition"]["padstack"]["antipad_per_layer"] = copy.deepcopy(
                        config["via_transition"]["padstack"]["antipad_per_layer"]
                    )
                    self.antipad_workbench.show_via_transition(config, geometry, validation)
                    vias = list(getattr(geometry, "signal_vias", []))
                    if vias and hasattr(self, "backdrill_actual_stub_var"):
                        self.backdrill_actual_stub_var.set(
                            f"{format_length(float(vias[0].actual_residual_stub), self.display_unit)} "
                            f"{self.display_unit}"
                        )
                else:
                    self.antipad_workbench.show(config, geometry, validation)
                    self.gnd_via_workbench.show(config, geometry, validation)
                # The simulation/frequency page is shared by both scenarios, but its
                # sweep controls are gated separately by scenario linkage.
                self.simulation_sweep_workbench.show(config, geometry, validation)
                preview_validation = getattr(self, "validate_report", None)
                if preview_validation is not None and preview_validation.config_revision != config_revision(config):
                    preview_validation = copy.deepcopy(preview_validation)
                    preview_validation.stale = True
                effective_validation = preview_validation or validation
                self._preview_validation = effective_validation
                # Live refresh never promotes a generation snapshot. Dirty until Validate.
                validated = getattr(self, "_validated_snapshot", None)
                if (validated is None
                        or str(validated.get("config_hash", "")) != str(snapshot.get("config_hash", ""))):
                    self._mark_preview_dirty()
                self.model_preview_workbench.show(config, geometry, effective_validation, snapshot)
            except Exception as exc:
                self._mark_preview_dirty()
                self.model_preview_workbench.show_error(str(exc))
                self.simulation_sweep_workbench.show_error(str(exc))
                if self._scenario_is_via():
                    self.antipad_workbench.show_via_error(str(exc))
                else:
                    self.antipad_workbench.show_error(str(exc))
                    self.gnd_via_workbench.show_error(str(exc))
        finally:
            self._refreshing = False

    def destroy(self) -> None:
        self._destroying = True
        try:
            self.withdraw()
        except tk.TclError:
            pass
        sheet = getattr(self, "sweep_sheet", None)
        if sheet is not None:
            try:
                sheet.withdraw()
                sheet.destroy()
            except tk.TclError:
                pass
        hfss_job = getattr(self, "_hfss_poll_job", None)
        if hfss_job:
            try:
                self.after_cancel(hfss_job)
            except tk.TclError:
                pass
            self._hfss_poll_job = None
        job = getattr(self, "_responsive_font_job", None)
        if job:
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
            self._responsive_font_job = None
        if hasattr(self, "gnd_via_workbench"):
            self.gnd_via_workbench.cancel_pending_callbacks()
        if hasattr(self, "model_preview_workbench"):
            self.model_preview_workbench.cancel_pending_callbacks()
        # Editors register a few short-lived ``after_idle`` redraw callbacks.
        # At application shutdown no callback is useful; cancel the complete
        # root queue so Tcl never tries to invoke a command after Tk has been
        # destroyed (observed as intermittent "invalid command name" noise).
        # Use Tcl's scheduler cancellation directly: ``self.after_cancel``
        # also deletes the callback command from ``self._tclCommands``, even
        # when that command belongs to a child widget.  The child's later
        # destroy then tries to delete the same command and aborts teardown.
        try:
            for pending_job in self.tk.call("after", "info"):
                try:
                    self.tk.call("after", "cancel", pending_job)
                except tk.TclError:
                    pass
        except tk.TclError:
            pass
        for canvas in (getattr(self, "via_top_canvas", None), getattr(self, "via_cross_canvas", None)):
            if canvas is None:
                continue
            try:
                canvas.bind("<Configure>", "")
            except tk.TclError:
                pass
        for widget in (
            getattr(self, "model_preview_workbench", None),
            getattr(self, "drawer_host", None),
            getattr(self, "via_workspace", None),
            getattr(self, "notebook", None),
            getattr(self, "shell", None),
        ):
            if widget is None:
                continue
            try:
                widget.destroy()
            except tk.TclError:
                pass
        try:
            super().destroy()
        except tk.TclError:
            # Some Tk builds can fail during Python-command cleanup after the
            # native root is already gone.  Suppress only that completed-close
            # case; an error while the window still exists must remain visible.
            try:
                root_still_exists = bool(self.winfo_exists())
            except tk.TclError:
                root_still_exists = False
            if root_still_exists:
                raise
            if getattr(tk, "_default_root", None) is self:
                tk._default_root = None


def _walk_widgets(widget: tk.Misc):
    """Yield a widget tree so packaged UI acceptance can invoke real buttons."""

    yield widget
    for child in widget.winfo_children():
        yield from _walk_widgets(child)


def _find_button(root: tk.Misc, text: str) -> tk.Misc:
    for widget in _walk_widgets(root):
        try:
            if widget.winfo_class() in {"TButton", "Button"} and widget.cget("text") == text:
                return widget
        except (tk.TclError, AttributeError):
            pass
    raise LookupError("UI button not found: " + text)


def _run_native_ui_acceptance(report_path: Path, output_parent: Path,
                              project_name: str = "FINAL_EXE_UI_ACCEPTANCE") -> int:
    """Exercise the packaged scan page through its actual Tk button commands."""

    expected_root = output_parent / (project_name + "_SWEEPS")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "executable_runtime": bool(_legacy.IS_FROZEN),
        "project_name": project_name,
        "output_root": str(expected_root),
        "actions": [],
        "passed": False,
        "software_build": get_build_info(),
    }
    app: SurfaceCapAntipadApp | None = None
    try:
        if expected_root.exists():
            raise RuntimeError("UI acceptance output must be fresh: " + str(expected_root))

        def fail_dialog(title: str, message: str, **_kwargs: Any) -> None:
            raise RuntimeError(f"{title}: {message}")

        messagebox.showerror = fail_dialog
        messagebox.showwarning = fail_dialog
        messagebox.showinfo = lambda *_args, **_kwargs: None
        messagebox.askyesno = lambda *_args, **_kwargs: True

        app = SurfaceCapAntipadApp()
        app.withdraw()
        config = copy.deepcopy(app.config)
        config["project"]["project_name"] = project_name
        config["project"]["output_directory"] = str(output_parent)
        config["project"]["overwrite_existing"] = True
        config["antipad"]["mode"] = "merged_all"
        config["sweep_execution"]["strategy"] = "native_optimetrics"
        for node in config["sweeps"].values():
            node["enabled"] = False
        app._load_to_widgets(config)
        app.update_idletasks()

        workbench = app.simulation_sweep_workbench
        from simulation_sweep_semantics import SWEEP_PARAMETER_REGISTRY

        spec = SWEEP_PARAMETER_REGISTRY["antipad.global_expansion"]
        workbench.parameter_var.set(spec.label)
        add_button = _find_button(workbench, "添加参数")
        add_button.invoke()
        app.update_idletasks()
        payload["actions"].append({"button": add_button.cget("text"), "path": spec.path})

        workbench.scan_mode_var.set("连续范围")
        workbench.scan_vars["start"].set("0.10")
        workbench.scan_vars["stop"].set("0.40")
        workbench.scan_vars["step"].set("0.05")
        apply_button = _find_button(workbench, "应用扫描值")
        apply_button.invoke()
        app.update_idletasks()
        payload["actions"].append({
            "button": apply_button.cget("text"),
            "start": "0.10mm", "stop": "0.40mm", "step": "0.05mm",
        })

        preview_button = _find_button(workbench, "预览Variations")
        preview_button.invoke()
        app.update_idletasks()
        preview_rows = [
            list(workbench.case_tree.item(item, "values"))
            for item in workbench.case_tree.get_children()
        ]
        payload["actions"].append({"button": preview_button.cget("text"), "row_count": len(preview_rows)})
        payload["preview_status"] = workbench.execution_status_var.get()
        payload["preview_rows"] = preview_rows

        create_button = _find_button(workbench, "创建参数扫描")
        create_button.invoke()
        app.update_idletasks()
        payload["actions"].append({"button": create_button.cget("text")})
        payload["generation_status"] = workbench.execution_status_var.get()
        generated = workbench._generated
        if generated is None:
            raise RuntimeError("UI create button returned without a generation result")
        manifest = json.loads(generated.manifest_path.read_text(encoding="utf-8"))
        files = sorted(
            str(path.relative_to(expected_root)).replace("\\", "/")
            for path in expected_root.rglob("*") if path.is_file()
        )
        directories = sorted(
            str(path.relative_to(expected_root)).replace("\\", "/")
            for path in expected_root.rglob("*") if path.is_dir()
        )
        case_hits = [item for item in files + directories if "CASE" in item.upper()]
        payload["manifest_path"] = str(generated.manifest_path)
        payload["manifest_summary"] = {
            key: manifest.get(key) for key in (
                "schema_version", "strategy", "backend", "project_count", "design_count",
                "optimetrics_setup_count", "design_variation_count", "software_version",
                "build_time_utc", "source_hash", "sweep_backend_version", "config_schema_version",
            )
        }
        payload["files"] = files
        payload["directories"] = directories
        payload["case_hits"] = case_hits
        payload["passed"] = (
            bool(_legacy.IS_FROZEN)
            and len(preview_rows) == 7
            and manifest.get("schema_version") == 3
            and manifest.get("backend") == "native_optimetrics"
            and manifest.get("project_count") == 1
            and manifest.get("design_count") == 1
            and manifest.get("optimetrics_setup_count") == 1
            and manifest.get("design_variation_count") == 7
            and not case_hits
        )
        if not payload["passed"]:
            raise RuntimeError("Packaged UI button acceptance assertions failed")
        return 0
    except Exception as exc:
        payload["error"] = repr(exc)
        payload["traceback"] = traceback.format_exc()
        return 2
    finally:
        if app is not None:
            try:
                app.destroy()
            except Exception:
                pass
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--run-generated-script":
        return _legacy._run_generated_script(Path(sys.argv[2]))
    if len(sys.argv) >= 4 and sys.argv[1] == "--ui-native-acceptance":
        return _run_native_ui_acceptance(
            Path(sys.argv[2]).expanduser().resolve(),
            Path(sys.argv[3]).expanduser().resolve(),
            sys.argv[4] if len(sys.argv) >= 5 else "FINAL_EXE_UI_ACCEPTANCE",
        )
    if len(sys.argv) >= 3 and sys.argv[1] == "--release-self-check":
        target = Path(sys.argv[2]).expanduser().resolve()
        try:
            from config_manager import ConfigManager
            from sweep_manager import assert_native_manifest_safe, generate_sweep_cases

            config = ConfigManager(_legacy.SOURCE_DIRECTORY).default()
            config["project"]["overwrite_existing"] = True
            config["antipad"]["mode"] = "merged_all"
            for node in config["sweeps"].values():
                node["enabled"] = False
            config["sweeps"]["antipad.global_expansion"].update(
                enabled=True, start=0.1, stop=0.4, step=0.05
            )
            with tempfile.TemporaryDirectory() as directory:
                result = generate_sweep_cases(config, Path(directory))
                assert_native_manifest_safe(result.manifest_path)
                manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
                script = result.project.script_path.read_text(encoding="utf-8")
                passed = (
                    len(result.cases) == 1
                    and len(result.variations) == 7
                    and manifest.get("backend") == "native_optimetrics"
                    and manifest.get("project_count") == 1
                    and manifest.get("design_count") == 1
                    and manifest.get("optimetrics_setup_count") == 1
                    and manifest.get("design_variation_count") == 7
                    and "_CASE_" not in json.dumps(manifest, ensure_ascii=False).upper()
                    and 'mode == "merged_all"' in script
                    and "antipad_expansion_variable(layer_name)" in script
                    and "compatibility_cases" not in script
                )
            build = get_build_info()
            payload = {
                "passed": bool(passed),
                "backend": manifest.get("backend"),
                "project_count": manifest.get("project_count"),
                "design_count": manifest.get("design_count"),
                "optimetrics_setup_count": manifest.get("optimetrics_setup_count"),
                "design_variation_count": manifest.get("design_variation_count"),
                **build,
            }
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return 0 if passed else 2
        except Exception as exc:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"passed": False, "error": str(exc)}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return 2
    if len(sys.argv) >= 4 and sys.argv[1] == "--generate-native-sweep":
        from config_manager import ConfigManager
        from sweep_manager import assert_native_manifest_safe, generate_sweep_cases

        source = Path(sys.argv[2]).expanduser().resolve()
        output_root = Path(sys.argv[3]).expanduser().resolve()
        config, _corrections = ConfigManager(_legacy.SOURCE_DIRECTORY).load(source)
        # A previously generated configuration carries an immutable snapshot
        # used to audit that run.  Regeneration is a new run, so discard the
        # old snapshot before applying the newly collected project settings.
        config.pop("_generation", None)
        config.pop("_geometry_snapshot", None)
        result = generate_sweep_cases(config, output_root)
        assert_native_manifest_safe(result.manifest_path)
        return 0
    smoke = "--smoke-test" in sys.argv
    preview_smoke = "--model-preview-smoke-test" in sys.argv
    _enable_native_fault_log()
    try:
        app = SurfaceCapAntipadApp()
        if smoke:
            app.update_idletasks()
            app.destroy()
            return 0
        if preview_smoke:
            smoke_error: list[BaseException] = []

            def exercise_real_tab_lifecycle() -> None:
                try:
                    # Exercise the same hidden -> visible -> hidden -> visible
                    # notebook lifecycle as a real user click.  The previous
                    # native Win32 VTK child crashed only on this path.
                    app.show_workspace_page("项目与走线")
                    app.variables["simulation.enabled"].set(True)
                    app.show_workspace_page("模型预览")
                    app.refresh_visuals()
                    workbench = app.model_preview_workbench
                    workbench.update_pending(); app.update_idletasks()
                    port_keys = [key for key, item in workbench.objects.items() if item.category == "Port"]
                    if len(port_keys) != 4 or "airbox" not in workbench.objects or "open_boundary" not in workbench.objects:
                        raise RuntimeError("打包自检失败：Port、空气盒或开放边界对象不完整。")
                    if not all(workbench.visibility.get(key, False) for key in (*port_keys, "airbox", "open_boundary")):
                        raise RuntimeError("打包自检失败：Port、空气盒或开放边界未默认显示。")
                    if not workbench.software_ready or len(workbench.software_canvas.find_all()) < 20:
                        raise RuntimeError("打包自检失败：稳定3D视口未完成绘制。")
                    canvas = workbench.software_canvas
                    cx, cy = canvas.winfo_width() // 2, canvas.winfo_height() // 2
                    yaw_before = workbench._camera_yaw
                    canvas.event_generate("<ButtonPress-1>", x=cx, y=cy)
                    canvas.event_generate("<B1-Motion>", x=cx + 36, y=cy + 14, state=0x100)
                    canvas.event_generate("<ButtonRelease-1>", x=cx + 36, y=cy + 14)
                    if abs(workbench._camera_yaw - yaw_before) < 1e-6:
                        raise RuntimeError("打包自检失败：左键轨道旋转事件未生效。")
                    target_before = tuple(workbench._camera_target)
                    canvas.event_generate("<ButtonPress-2>", x=cx, y=cy)
                    canvas.event_generate("<B2-Motion>", x=cx + 20, y=cy + 12, state=0x200)
                    canvas.event_generate("<ButtonRelease-2>", x=cx + 20, y=cy + 12)
                    if tuple(workbench._camera_target) == target_before:
                        raise RuntimeError("打包自检失败：中键相机平移事件未生效。")
                    scale_before = workbench._camera_scale
                    canvas.event_generate("<MouseWheel>", x=cx, y=cy, delta=120)
                    if workbench._camera_scale <= scale_before:
                        raise RuntimeError("打包自检失败：滚轮缩放事件未生效。")
                    workbench.set_view("等轴测"); workbench.fit_model()
                    workbench.select_object(port_keys[0])
                    if not app.validate_model(show_dialog=False):
                        raise RuntimeError("打包自检失败：默认仿真配置未通过 Validate。")
                    workbench.update_pending()
                    app.show_workspace_page("Validate结果")
                    app.show_workspace_page("模型预览")
                    if not all(key in workbench.objects for key in (*port_keys, "airbox", "open_boundary")):
                        raise RuntimeError("打包自检失败：Validate 后仿真预览对象消失。")
                except BaseException as exc:
                    smoke_error.append(exc)
                finally:
                    app.after(350, app.destroy)

            app.after(250, exercise_real_tab_lifecycle)
            app.mainloop()
            if smoke_error:
                raise smoke_error[0]
            return 0
        app.mainloop()
        return 0
    except BaseException as exc:
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        _write_crash_log("Fatal GUI exception", detail)
        try:
            messagebox.showerror("V2.4.0 启动失败", f"{exc}\n\n诊断日志：{CRASH_LOG_PATH}")
        except tk.TclError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
