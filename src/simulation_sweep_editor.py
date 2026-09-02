"""Componentized simulation and external design-sweep workbench."""
from __future__ import annotations

import copy
import json
import math
import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Dict, List, Mapping, Optional, Sequence

from config_manager import DEFAULT_DISPLAY_UNIT, convert_length, format_length, format_number
from gui_layout import canvas_viewport, font_linespace
from hfss_script_generator import ScriptGenerationError
from simulation_sweep_semantics import (
    FREQUENCY_PRESETS,
    SWEEP_PARAMETER_REGISTRY,
    apply_sweep_value,
    apply_frequency_preset,
    inspect_frequency_plan,
    inspect_sweep_plan,
    inspect_tdr_sweep_plan,
    iter_materialized_variations,
    iter_variations,
    preflight_sweep_geometry,
    normalize_execution_strategy,
    preset_is_modified,
    get_sweep_parameter_spec,
    scan_values,
)
from sweep_manager import (
    SweepGenerationResult,
    assert_native_manifest_safe,
    expand_sweep_cases,
    generate_sweep_cases,
    summarize_sweep_results,
    update_manifest_case_status,
)


SWEEP_TYPE_LABELS = {"Interpolating": "插值扫频", "Discrete": "离散扫频"}
BOUNDARY_LABELS = {"Radiation": "Radiation 辐射边界", "PML": "PML 完美匹配层"}
REPORT_LABELS = {
    "Sdd11": "Sdd11 · 差分回波损耗",
    "Sdd21": "Sdd21 · 差分插入损耗",
    "Sdc21": "Sdc21 · 差模到共模转换",
    "Scd21": "Scd21 · 共模到差模转换",
}


class ScrollColumn(ttk.Frame):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.canvas = tk.Canvas(self, highlightthickness=0, background="#f7f8fa")
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.content = ttk.Frame(self.canvas, padding=(3, 3))
        self.window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.canvas.configure(yscrollcommand=bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.content.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.window, width=e.width))
        for widget in (self.canvas, self.content):
            widget.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))


class FrequencyAxis(tk.Canvas):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, height=210, background="white", highlightthickness=1,
                         highlightbackground="#cbd5e1")
        self._simulation: Optional[Mapping[str, Any]] = None
        self.bind("<Configure>", lambda _e: self.after_idle(self.redraw))

    def show(self, simulation: Mapping[str, Any]) -> None:
        self._simulation = simulation
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        if not self._simulation:
            return
        sim = self._simulation
        viewport = canvas_viewport(self)
        if viewport is None:
            return
        width, height = viewport
        try:
            start = float(sim["start_frequency_ghz"])
            stop = float(sim["stop_frequency_ghz"])
            adaptive = float(sim["adaptive_frequency_ghz"])
        except (KeyError, TypeError, ValueError):
            self.create_text(width / 2, 100, text="频率参数暂不可用", fill="#b42318")
            return
        self.create_text(14, 12, anchor="nw", text="动态频率范围", font="ViaPreviewTitleFont",
                         width=max(1, width-28), tags=("hud-label",))
        margin = min(52, max(16, width * .14))
        x1, x2, y = margin, width - margin, min(height-65, max(82, height*.60))
        self.create_line(x1, y, x2, y, width=4, fill="#94a3b8")
        span = max(stop - start, 1e-12)
        def xpos(value: float) -> float:
            return x1 + (value - start) / span * (x2 - x1)
        for value, label, color, anchor in (
            (start, f"起始\n{start:g} GHz", "#2563eb", "nw"),
            (stop, f"终止\n{stop:g} GHz", "#dc2626", "ne"),
        ):
            x = xpos(value)
            self.create_line(x, y - 18, x, y + 18, width=3, fill=color)
            self.create_text(x, min(height-5, y + 25), text=label, anchor=anchor,
                             fill=color, font="ViaCanvasFont", tags=("legend-label",))
        if start <= adaptive <= stop:
            x = xpos(adaptive)
            self.create_polygon(x, y - 31, x - 7, y - 43, x + 7, y - 43, fill="#7c3aed")
            self.create_text(x, max(5, y - 49), text=f"自适应 {adaptive:g} GHz", anchor="s",
                             fill="#7c3aed", font="ViaCanvasFont", tags=("dimension-label",))
        preset = str(sim.get("pcie_generation_preset", "custom"))
        modified = "（已修改）" if preset_is_modified(sim) else ""
        plan = inspect_frequency_plan(sim)
        mode = (f"离散 · {plan.point_count or 0} 点"
                if plan.mode == "Discrete" else "插值模式 · 自适应采样")
        self.create_text(14, 45, anchor="nw", text=f"预设：{preset}{modified}    {mode}",
                         fill="#334155", font="ViaCanvasFont", width=max(1, width-28),
                         tags=("hud-label",))
        if plan.issues:
            level, message = plan.issues[0]
            self.create_text(14, height-8, anchor="sw", text=("错误：" if level == "error" else "提示：") + message,
                             fill="#b42318" if level == "error" else "#9a3412", font="ViaCanvasFont",
                             width=max(1, width-28), tags=("hud-label",))


class AirboxDiagram(tk.Canvas):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, height=105, background="white", highlightthickness=1,
                         highlightbackground="#cbd5e1")
        self._margin = 0.0
        self._unit = DEFAULT_DISPLAY_UNIT
        self.bind("<Configure>", lambda _e: self.redraw())

    def show(self, margin_mm: float, unit: str) -> None:
        self._margin, self._unit = margin_mm, unit
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        viewport = canvas_viewport(self)
        if viewport is None:
            return
        width, height = viewport
        outer = min(35, max(8, width*.09))
        inner = min(105, max(outer+18, width*.27))
        top, bottom = 18, max(35, height-17)
        mid_top, mid_bottom = top + (bottom-top)*.38, top + (bottom-top)*.66
        self.create_rectangle(outer, top, width - outer, bottom, fill="#e0f2fe", outline="#0284c7", dash=(5, 3))
        self.create_rectangle(inner, mid_top, width - inner, mid_bottom, fill="#d1fae5", outline="#047857")
        self.create_text(width / 2, (mid_top+mid_bottom)/2, text="PCB / 铜结构", font="ViaCanvasFont",
                         tags=("object-label",))
        self.create_line(outer+5, top+16, inner-3, mid_top, arrow="both", fill="#7c3aed")
        self.create_text((outer+inner)/2, top+font_linespace(self, "ViaSmallFont")/2,
                         text=f"margin {format_length(self._margin, self._unit)} {self._unit}",
                         fill="#7c3aed", font="ViaSmallFont", tags=("dimension-label",))


class SimulationSweepWorkbench(ttk.Frame):
    """Three-step, scroll-safe workflow for simulation, case design and execution."""

    def __init__(self, parent: tk.Misc, app: Any) -> None:
        super().__init__(parent)
        self.pack(fill="both", expand=True)
        self.app = app
        self.current_config: Mapping[str, Any] = app.config
        self.sweeps: Dict[str, Dict[str, Any]] = copy.deepcopy(app.config.get("sweeps", {}))
        self._generated: Optional[SweepGenerationResult] = None
        self._process: Optional[subprocess.Popen[Any]] = None
        self._running_case = 0
        self._run_queue: List[int] = []
        self._paused = False
        self._build()

    @staticmethod
    def _section(parent: tk.Misc, title: str) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, text=title, padding=(8, 6))
        frame.pack(fill="x", expand=True, padx=3, pady=4)
        frame.columnconfigure(1, weight=1)
        return frame

    def _build(self) -> None:
        flow = ttk.Frame(self, padding=(4, 2))
        flow.pack(fill="x")
        for index, text in enumerate(("1  配置仿真", "2  设计参数扫描", "3  创建参数扫描与执行")):
            ttk.Label(flow, text=text, font="ViaCardTitleFont", foreground="#1d4ed8").grid(
                row=0, column=index * 2, sticky="ew", padx=8)
            flow.columnconfigure(index * 2, weight=1)
            if index < 2:
                ttk.Label(flow, text="→", foreground="#64748b").grid(row=0, column=index * 2 + 1)
        self.steps = ttk.Notebook(self)
        self.steps.pack(fill="both", expand=True, padx=3, pady=(2, 3))
        self.plan_page = ttk.Frame(self.steps)
        self.design_page = ttk.Frame(self.steps)
        self.execution_page = ttk.Frame(self.steps)
        self.steps.add(self.plan_page, text="1 仿真方案与频率扫描")
        self.steps.add(self.design_page, text="2 设计参数扫描")
        self.steps.add(self.execution_page, text="3 创建参数扫描、运行与汇总")
        self._build_simulation_plan()
        self._build_design_sweep()
        self._build_execution()

    def _build_simulation_plan(self) -> None:
        panes = ttk.PanedWindow(self.plan_page, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left, right = ScrollColumn(panes), ScrollColumn(panes)
        self.plan_left, self.plan_right = left, right
        panes.add(left, weight=3); panes.add(right, weight=2)

        enable = self._section(left.content, "自动仿真状态")
        self.auto_enable = self.app._add(enable, 0, "启用自动仿真", "simulation.enabled", boolean=True)
        self.auto_mode_var = tk.StringVar()
        self.auto_mode_label = ttk.Label(enable, textvariable=self.auto_mode_var, wraplength=680,
                                         foreground="#9a3412")
        self.auto_mode_label.grid(row=1, column=0, columnspan=4, sticky="w", padx=4, pady=(5, 1))

        ports = self._section(left.content, "端口设置")
        self.solution_type = self.app._add(ports, 0, "HFSS 解类型", "project.solution_type",
                                           values=("Terminal", "Modal"),
                                           display_values={"Terminal": "Terminal（自动混合模）", "Modal": "Modal（仅几何/手动端口）"})
        self.port_enable = self.app._add(ports, 1, "创建差分端口", "simulation.create_differential_ports", boolean=True)
        ttk.Label(ports, text="预计端口：PORT_IN_P / PORT_IN_N / PORT_OUT_P / PORT_OUT_N\n"
                  "混合模映射：IN_DIFF / IN_COMM，OUT_DIFF / OUT_COMM",
                  foreground="#0f766e", wraplength=680).grid(row=2, column=0, columnspan=4, sticky="w", padx=4)
        ttk.Label(ports, text="生成后必须检查积分线、参考导体、端口模式编号与去嵌设置。",
                  foreground="#9a3412", wraplength=680).grid(row=3, column=0, columnspan=4, sticky="w", padx=4, pady=(3, 1))

        air = self._section(left.content, "开放区域")
        self.airbox_enable = self.app._add(air, 0, "创建空气盒", "simulation.create_airbox", boolean=True)
        self.airbox_margin = self.app._add(air, 1, "空气盒边距", "simulation.airbox_margin", length=True)
        self.open_boundary = self.app._add(air, 2, "开放边界", "simulation.open_boundary",
                                           values=("Radiation", "PML"), display_values=BOUNDARY_LABELS)
        self.airbox_hint = ttk.Label(air, text="空气盒在模型包围盒六个方向各扩展 margin；开放边界施加在空气区域外表面。",
                                     foreground="#475569", wraplength=680)
        self.airbox_hint.grid(row=3, column=0, columnspan=4, sticky="w", padx=4, pady=(4, 1))
        self.airbox_diagram = AirboxDiagram(air)
        self.airbox_diagram.grid(row=4, column=0, columnspan=4, sticky="ew", padx=4, pady=(4, 1))

        setup = self._section(left.content, "Setup 与收敛")
        self.setup_name = self.app._add(setup, 0, "Setup 名称", "simulation.setup_name")
        self.sweep_name = self.app._add(setup, 1, "Sweep 名称", "simulation.sweep_name")
        self.adaptive = self.app._add(setup, 2, "自适应频率", "simulation.adaptive_frequency_ghz", numeric=True, unit="GHz")
        self.max_passes = self.app._add(setup, 3, "最大迭代次数", "simulation.max_passes", numeric=True, integer=True)
        self.max_delta = self.app._add(setup, 4, "最大 Delta S", "simulation.max_delta_s", numeric=True)
        self.causal_materials = self.app._add(
            setup, 5, "自动使用因果材料", "simulation.automatically_use_causal_materials", boolean=True
        )

        frequency = self._section(left.content, "频率扫描（同一 HFSS Design 内）")
        self.preset_combo = self.app._add(frequency, 0, "PCIe 频率预设", "simulation.pcie_generation_preset",
                                          values=("custom", "Gen3", "Gen4", "Gen5", "Gen6"))
        self.preset_combo.bind("<<ComboboxSelected>>", self._preset_selected, add="+")
        preset_bar = ttk.Frame(frequency); preset_bar.grid(row=1, column=0, columnspan=4, sticky="ew", padx=4)
        ttk.Button(preset_bar, text="应用/重新应用预设", command=self._apply_preset).pack(side="left")
        self.preset_state_var = tk.StringVar(value="选择预设后先显示差异，不会静默覆盖手动值。")
        ttk.Label(preset_bar, textvariable=self.preset_state_var, foreground="#7c3aed").pack(side="left", padx=8)
        ttk.Label(frequency, text="PCIe 代际预设仅用于初始化仿真频率，不构成 PCI-SIG 合规结论。",
                  foreground="#9a3412", wraplength=680).grid(row=2, column=0, columnspan=4, sticky="w", padx=4, pady=(4, 2))
        self.sweep_type = self.app._add(frequency, 3, "扫频类型", "simulation.sweep_type",
                                        values=("Interpolating", "Discrete"), display_values=SWEEP_TYPE_LABELS)
        self.start_frequency = self.app._add(frequency, 4, "起始频率", "simulation.start_frequency_ghz", numeric=True, unit="GHz")
        self.stop_frequency = self.app._add(frequency, 5, "终止频率", "simulation.stop_frequency_ghz", numeric=True, unit="GHz")
        self.step_frequency = self.app._add(frequency, 6, "离散频率步进", "simulation.step_frequency_ghz", numeric=True, unit="GHz")
        self.interp_tolerance = self.app._add(frequency, 7, "插值误差", "simulation.interpolating_tolerance", numeric=True)
        self.interp_max = self.app._add(frequency, 8, "插值最大采样点", "simulation.interpolating_max_solutions", numeric=True, integer=True)
        self.save_fields = self.app._add(frequency, 9, "保存场数据", "simulation.save_fields", boolean=True)
        self.frequency_detail_var = tk.StringVar()
        ttk.Label(frequency, textvariable=self.frequency_detail_var, foreground="#0f766e", wraplength=680).grid(
            row=10, column=0, columnspan=4, sticky="w", padx=4, pady=(5, 1))

        axis_box = self._section(right.content, "频率范围图")
        self.frequency_axis = FrequencyAxis(axis_box)
        self.frequency_axis.grid(row=0, column=0, columnspan=4, sticky="ew")

        reports = self._section(right.content, "输出报告")
        self.generate_reports = self.app._add(reports, 0, "启用 S 参数报告", "simulation.generate_reports", boolean=True)
        self.report_widgets: Dict[str, tk.Widget] = {}
        for row, (name, label) in enumerate(REPORT_LABELS.items(), 1):
            self.report_widgets[name] = self.app._add(
                reports, row, label, f"simulation.report_outputs.{name}", boolean=True)
        self.tdr_request = self.app._add(reports, 5, "生成差分 TDR", "simulation.generate_tdr", boolean=True)
        self.tdr_sweep_enable = self.app._add(
            reports, 6, "启用独立离散 Sweep", "simulation.tdr_sweep.enabled", boolean=True)
        self.tdr_sweep_name = self.app._add(
            reports, 7, "TDR Sweep 名称", "simulation.tdr_sweep.sweep_name")
        self.tdr_start = self.app._add(
            reports, 8, "TDR 起始频率", "simulation.tdr_sweep.start_frequency_ghz", numeric=True, unit="GHz")
        self.tdr_stop = self.app._add(
            reports, 9, "TDR 终止频率", "simulation.tdr_sweep.stop_frequency_ghz", numeric=True, unit="GHz")
        self.tdr_step = self.app._add(
            reports, 10, "TDR 离散步进", "simulation.tdr_sweep.step_frequency_ghz", numeric=True, unit="GHz")
        self.tdr_max_points = self.app._add(
            reports, 11, "TDR 最大频点数", "simulation.tdr_sweep.max_points", numeric=True, integer=True)
        ttk.Label(
            reports,
            text="TDR 使用独立真实离散解；起始频率必须等于步进，终止频率必须为步进的整数倍。",
            foreground="#9a3412", wraplength=500,
        ).grid(row=12, column=0, columnspan=4, sticky="w", padx=4, pady=(4, 1))

        summary = self._section(right.content, "仿真方案摘要")
        self.sim_summary_var = tk.StringVar()
        ttk.Label(summary, textvariable=self.sim_summary_var, justify="left", wraplength=520,
                  font="ViaMonoFont").grid(row=0, column=0, columnspan=4, sticky="w", padx=4)
        self.sim_issue_var = tk.StringVar()
        ttk.Label(summary, textvariable=self.sim_issue_var, justify="left", wraplength=520,
                  foreground="#b42318").grid(row=1, column=0, columnspan=4, sticky="w", padx=4, pady=(6, 1))

        self.simulation_widgets: Sequence[tk.Widget] = (
            self.solution_type, self.port_enable, self.airbox_enable, self.airbox_margin, self.open_boundary,
            self.setup_name, self.sweep_name, self.adaptive, self.max_passes, self.max_delta, self.causal_materials,
            self.preset_combo, self.sweep_type, self.start_frequency, self.stop_frequency,
            self.step_frequency, self.interp_tolerance, self.interp_max, self.save_fields,
            self.generate_reports, *self.report_widgets.values(), self.tdr_request,
            self.tdr_sweep_enable, self.tdr_sweep_name, self.tdr_start,
            self.tdr_stop, self.tdr_step, self.tdr_max_points,
        )

    def _build_design_sweep(self) -> None:
        panes = ttk.PanedWindow(self.design_page, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left, right = ScrollColumn(panes), ScrollColumn(panes)
        self.design_left, self.design_right = left, right
        panes.add(left, weight=3); panes.add(right, weight=2)

        picker = self._section(left.content, "扫描参数选择器 · 来自统一注册表")
        labels = tuple(spec.label for spec in SWEEP_PARAMETER_REGISTRY.values())
        self.spec_by_label = {spec.label: spec for spec in SWEEP_PARAMETER_REGISTRY.values()}
        self.parameter_var = tk.StringVar(value=labels[0])
        self.parameter_combo = ttk.Combobox(
            picker, textvariable=self.parameter_var, values=labels, state="readonly")
        self.parameter_combo.grid(row=0, column=0, sticky="ew", padx=4)
        ttk.Button(picker, text="添加参数", command=self._add_sweep_parameter).grid(row=0, column=1, padx=4)
        picker.columnconfigure(0, weight=1)

        selected = self._section(left.content, "已选参数与依赖状态")
        self.sweep_tree = ttk.Treeview(selected, columns=("parameter", "source", "mode", "values", "count", "status"),
                                       show="headings", height=10)
        for key, label, width in (("parameter", "参数", 210), ("source", "来源页面", 100),
                                  ("mode", "方式", 75), ("values", "范围/离散值", 260),
                                  ("count", "点数", 55), ("status", "依赖", 180)):
            self.sweep_tree.heading(key, text=label); self.sweep_tree.column(key, width=width, anchor="w")
        self.sweep_tree.grid(row=0, column=0, columnspan=4, sticky="nsew")
        self.sweep_tree.bind("<<TreeviewSelect>>", self._sweep_selected)
        ttk.Button(selected, text="移除所选参数", command=self._remove_sweep_parameter).grid(row=1, column=0, sticky="w", padx=4, pady=4)

        editor = self._section(right.content, "扫描值编辑")
        self.edit_path_var = tk.StringVar(value="尚未选择参数")
        ttk.Label(editor, textvariable=self.edit_path_var, font="ViaCardTitleFont").grid(
            row=0, column=0, columnspan=4, sticky="w", padx=4)
        self.scan_mode_var = tk.StringVar(value="连续范围")
        ttk.Label(editor, text="扫描方式").grid(row=1, column=0, sticky="w", padx=4, pady=3)
        self.scan_mode_combo = ttk.Combobox(editor, textvariable=self.scan_mode_var,
                                            values=("连续范围", "离散值"), state="readonly")
        self.scan_mode_combo.grid(row=1, column=1, sticky="ew", padx=4)
        self.scan_mode_combo.bind("<<ComboboxSelected>>", lambda _e: self._update_scan_editor_state())
        self.scan_vars = {key: tk.StringVar() for key in ("start", "stop", "step", "values")}
        self.scan_entries: Dict[str, ttk.Entry] = {}
        for row, (key, label) in enumerate((("start", "起始值"), ("stop", "终止值"), ("step", "步进"),
                                            ("values", "离散值（逗号分隔）")), 2):
            ttk.Label(editor, text=label).grid(row=row, column=0, sticky="w", padx=4, pady=3)
            entry = ttk.Entry(editor, textvariable=self.scan_vars[key])
            entry.grid(row=row, column=1, sticky="ew", padx=4)
            self.scan_entries[key] = entry
        self.scan_unit_var = tk.StringVar(value="")
        ttk.Label(editor, textvariable=self.scan_unit_var, foreground="#475569").grid(row=6, column=0, columnspan=4, sticky="w", padx=4)
        self.scan_editor_issue_var = tk.StringVar(value="")
        ttk.Label(editor, textvariable=self.scan_editor_issue_var, foreground="#9a3412", wraplength=500).grid(
            row=7, column=0, columnspan=4, sticky="w", padx=4, pady=(4, 1))
        ttk.Button(editor, text="应用扫描值", command=self._save_sweep_editor).grid(row=8, column=0, padx=4, pady=6, sticky="w")

        strategy = self._section(right.content, "扫描策略与工作量")
        strategy_labels = {
            "hybrid": "Hybrid：纯数值 Optimetrics + 拓扑离散 case",
            "native_optimetrics": "HFSS 原生 Optimetrics",
            "topology_variants": "拓扑变体：独立 Design/case",
            "legacy_discrete": "兼容：外部离散案例",
        }
        self.strategy = self.app._add(
            strategy, 0, "策略", "sweep_execution.strategy",
            values=tuple(strategy_labels), display_values=strategy_labels,
        )
        self.max_cases = self.app._add(strategy, 1, "Variation 安全上限", "sweep_execution.max_cases", numeric=True, integer=True)
        self.parallel_jobs = self.app._add(strategy, 2, "HFSS 进程数（原生扫描固定为 1）", "sweep_execution.parallel_jobs", numeric=True, integer=True)
        self.parallel_jobs.configure(state="readonly", style="Derived.TEntry")
        self.variation_mode = self.app._add(
            strategy, 5, "Variation 组合", "sweep_execution.variation_mode",
            values=("full_factorial", "synchronous", "one_at_a_time", "custom"),
            display_values={"full_factorial": "全因子", "synchronous": "同步配对",
                            "one_at_a_time": "One-at-a-time", "custom": "自定义 Variation 表"},
        )
        # Replace the legacy helper text in-place so old serialized layouts do
        # not continue telling users that every Variation becomes a CASE file.
        for child in strategy.winfo_children():
            if str(child.grid_info().get("row")) == "4":
                child.configure(text="纯数值轴使用 Design Variable + Optimetrics；拓扑、clearance 和逐层轴生成重新计算的离散 Design/case。")
        self.workload_var = tk.StringVar()
        ttk.Label(strategy, textvariable=self.workload_var, justify="left", wraplength=500,
                  foreground="#0f766e").grid(row=3, column=0, columnspan=4, sticky="w", padx=4, pady=(5, 1))
        ttk.Label(strategy, text="纯数值参数在同一 HFSS Design 内扫描；任何拓扑/逐层轮廓变化都改用独立离散 case。",
                  foreground="#475569", wraplength=500).grid(row=4, column=0, columnspan=4, sticky="w", padx=4, pady=(5, 1))

        # The legacy note is created last; replace it after all strategy rows
        # exist so the UI cannot advertise independent CASE generation.
        for child in strategy.winfo_children():
            if str(child.grid_info().get("row")) == "4":
                child.configure(text="纯数值轴使用 Design Variable + Optimetrics；拓扑、clearance 和逐层轴生成重新计算的离散 Design/case。")

    def _build_execution(self) -> None:
        root = ttk.Frame(self.execution_page, padding=5)
        root.pack(fill="both", expand=True)
        actions = ttk.LabelFrame(root, text="完整工作流", padding=(8, 6))
        actions.pack(fill="x")
        for text, command in (
            ("全量预检", self.preflight_cases), ("预览Variations", self.preview_cases),
            ("创建参数扫描", self.generate_cases), ("运行扫描", self.run_cases),
            ("暂停运行", self.toggle_pause), ("失败重试", self.retry_failed),
            ("汇总结果", self.summarize_results),
            ("回填所选Variation", self.apply_selected_variation),
        ):
            ttk.Button(actions, text=text, command=command).pack(side="left", padx=3)
        self.execution_status_var = tk.StringVar(value="尚未创建参数扫描。")
        ttk.Label(actions, textvariable=self.execution_status_var, foreground="#0f766e").pack(side="right", padx=6)
        self.output_var = tk.StringVar(value="输出目录将在预检后显示。")
        ttk.Label(root, textvariable=self.output_var, foreground="#475569").pack(fill="x", pady=(5, 2))
        table = ttk.LabelFrame(root, text="Design Variations / 离散几何 cases", padding=(6, 5))
        table.pack(fill="both", expand=True)
        self.case_tree = ttk.Treeview(table, columns=("index", "variation", "geometry", "solve", "reports", "status"),
                                      show="headings", height=18)
        for key, label, width in (("index", "Variation", 95), ("variation", "参数组合", 430),
                                  ("geometry", "几何绑定", 170), ("solve", "执行方式", 110),
                                  ("reports", "报告", 180), ("status", "状态", 150)):
            self.case_tree.heading(key, text=label); self.case_tree.column(key, width=width, anchor="w")
        ybar = ttk.Scrollbar(table, orient="vertical", command=self.case_tree.yview)
        self.case_tree.configure(yscrollcommand=ybar.set)
        self.case_tree.pack(side="left", fill="both", expand=True)
        ybar.pack(side="right", fill="y")

    def load_config(self, config: Mapping[str, Any]) -> None:
        self.current_config = config
        self.sweeps = copy.deepcopy(config.get("sweeps", {}))
        self._refresh_parameter_specs(config)
        self._refresh_sweep_tree(config)
        self._refresh_simulation(config)

    def _refresh_parameter_specs(self, config: Mapping[str, Any]) -> None:
        """Add saved per-layer VIA axes to the normal parameter picker."""

        specs = {spec.path: spec for spec in SWEEP_PARAMETER_REGISTRY.values()}
        per_layer = (config.get("via_transition", {}).get("padstack", {})
                     .get("antipad", {}).get("per_layer", {}))
        if isinstance(per_layer, Mapping):
            for layer_name, override in per_layer.items():
                if not isinstance(override, Mapping) or bool(override.get("inherit_global", False)):
                    continue
                shape = str(override.get("shape", "circle"))
                fields = ["diameter"] if shape == "circle" else ["length", "width", "rotation_deg"]
                if shape == "rounded_rectangle":
                    fields.append("corner_radius")
                fields.append("clearance")
                fields.extend(field for field in (
                    "end_clearance", "side_clearance", "length_increment", "width_increment"
                ) if field in override)
                for field in fields:
                    path = f"via_transition.padstack.antipad.per_layer.{layer_name}.{field}"
                    spec = get_sweep_parameter_spec(path)
                    if spec is not None and field in override:
                        specs[path] = spec
        # Also retain dynamically loaded axes from a saved JSON even if its
        # layer override has since become invalid; the tree can then report the
        # dependency/path error instead of silently hiding that scan.
        for path in self.sweeps:
            spec = get_sweep_parameter_spec(path)
            if spec is not None:
                specs[path] = spec
        ordered = list(specs.values())
        self.spec_by_label = {spec.label: spec for spec in ordered}
        labels = tuple(self.spec_by_label)
        self.parameter_combo.configure(values=labels)
        if self.parameter_var.get() not in self.spec_by_label and labels:
            self.parameter_var.set(labels[0])

    def prepare_collect(self) -> None:
        self.app.config["sweeps"] = copy.deepcopy(self.sweeps)

    def show(self, config: Mapping[str, Any], _geometry: Any = None, _validation: Any = None) -> None:
        self.current_config = config
        self._refresh_parameter_specs(config)
        self._refresh_simulation(config)
        self._refresh_sweep_tree(config)

    def show_error(self, message: str) -> None:
        self.sim_issue_var.set(message)

    def set_sweep_scenario_enabled(self, enabled: bool, reason: str = "") -> None:
        """Gate design-sweep/execution only when a scenario has no supported backend."""
        if not hasattr(self, "steps"):
            return
        state = "normal" if enabled else "disabled"
        for page in (getattr(self, "design_page", None), getattr(self, "execution_page", None)):
            if page is not None:
                try:
                    self.steps.tab(page, state=state)
                except tk.TclError:
                    pass
        if not enabled:
            self.execution_status_var.set(reason or "该场景参数扫描尚未开放。")

    def on_font_scale_changed(self, tier: str) -> None:
        self.frequency_axis.configure(height={"normal": 210, "large": 260, "ultra": 310}[tier])
        self.airbox_diagram.configure(height={"normal": 105, "large": 135, "ultra": 160}[tier])
        self.frequency_axis.redraw()
        self.airbox_diagram.redraw()

    def _backend(self, path: str) -> str:
        return self.app._backend_value(path)

    def _widget_state(self, widget: tk.Widget, enabled: bool, readonly: bool = False) -> None:
        if isinstance(widget, ttk.Combobox):
            widget.configure(state="readonly" if enabled else "disabled")
        else:
            widget.configure(state="readonly" if enabled and readonly else "normal" if enabled else "disabled")

    def _refresh_simulation(self, config: Mapping[str, Any]) -> None:
        sim = config.get("simulation", {})
        enabled = bool(sim.get("enabled"))
        self.auto_mode_var.set("自动仿真已启用：将创建 Setup、频率扫描和所选报告。" if enabled else
                               "当前为仅生成几何模式，不会自动创建 Setup、求解频率或 S 参数报告。")
        for widget in self.simulation_widgets:
            self._widget_state(widget, enabled)
        self._widget_state(self.auto_enable, True)
        air_enabled = enabled and bool(sim.get("create_airbox"))
        self._widget_state(self.airbox_margin, air_enabled)
        self._widget_state(self.open_boundary, air_enabled)
        self.airbox_diagram.show(float(sim.get("airbox_margin", 0.0)), self.app.display_unit)
        sweep_type = str(sim.get("sweep_type", "Interpolating"))
        self._widget_state(self.step_frequency, enabled and sweep_type == "Discrete")
        self._widget_state(self.interp_tolerance, enabled and sweep_type == "Interpolating")
        self._widget_state(self.interp_max, enabled and sweep_type == "Interpolating")
        self._widget_state(self.save_fields, enabled and sweep_type == "Interpolating")
        reports_enabled = enabled and bool(sim.get("generate_reports")) and bool(sim.get("create_differential_ports"))
        for widget in self.report_widgets.values():
            self._widget_state(widget, reports_enabled)
        tdr_requested = enabled and bool(sim.get("generate_tdr"))
        self._widget_state(self.tdr_sweep_enable, tdr_requested)
        tdr_sweep_enabled = tdr_requested and bool(sim.get("tdr_sweep", {}).get("enabled"))
        for widget in (self.tdr_sweep_name, self.tdr_start, self.tdr_stop,
                       self.tdr_step, self.tdr_max_points):
            self._widget_state(widget, tdr_sweep_enabled)
        plan = inspect_frequency_plan(sim)
        tdr_plan = inspect_tdr_sweep_plan(sim)
        if sweep_type == "Discrete":
            last = "—" if plan.last_frequency_ghz is None else f"{plan.last_frequency_ghz:g} GHz"
            self.frequency_detail_var.set(f"离散频点：{plan.point_count or 0}；实际最后频点：{last}")
        else:
            self.frequency_detail_var.set(
                f"插值控制：误差 {sim.get('interpolating_tolerance')}；最大采样点 {sim.get('interpolating_max_solutions')}；"
                f"保存场：{'是' if sim.get('save_fields') else '否'}")
        self.frequency_axis.show(sim)
        preset = str(sim.get("pcie_generation_preset", "custom"))
        self.preset_state_var.set(preset + ("（已修改）" if preset_is_modified(sim) else ""))
        selected = [name for name in REPORT_LABELS if sim.get("report_outputs", {}).get(name)]
        lines = [
            "几何脚本：同一Design内参数变化",
            f"自动求解：{'开启' if enabled else '关闭'}",
            f"频率扫描：{SWEEP_TYPE_LABELS.get(sweep_type, sweep_type) if enabled else '不会创建'}",
            f"S 参数结果：{', '.join(selected) if enabled and sim.get('generate_reports') and sim.get('create_differential_ports') else '不会生成'}",
            (
                f"TDR：{tdr_plan.sweep_name}，{tdr_plan.point_count} 个真实离散频点"
                if tdr_plan.enabled and tdr_plan.point_count is not None
                else "TDR：已请求，但独立 Sweep 未就绪"
                if sim.get("generate_tdr") else "TDR：不请求"
            ),
        ]
        self.sim_summary_var.set("\n".join(lines))
        self.sim_issue_var.set("\n".join(("错误：" if level == "error" else "提示：") + message
                                         for level, message in plan.issues[:4]))
        self._refresh_workload(config)

    def _preset_selected(self, _event: Any = None) -> None:
        name = self._backend("simulation.pcie_generation_preset")
        if name not in FREQUENCY_PRESETS:
            self.preset_state_var.set("自定义频率，不应用代际初始化规则。")
            return
        preset = FREQUENCY_PRESETS[name]
        self.preset_state_var.set(
            f"待应用：自适应 {preset.adaptive_frequency_ghz:g}，范围 {preset.start_frequency_ghz:g}–{preset.stop_frequency_ghz:g} GHz；当前值尚未覆盖。")

    def _apply_preset(self) -> None:
        name = self._backend("simulation.pcie_generation_preset")
        if name not in FREQUENCY_PRESETS:
            self.preset_state_var.set("请选择 Gen3、Gen4、Gen5 或 Gen6。")
            return
        before = {
            key: self.app.variables[f"simulation.{key}"].get()
            for key in ("adaptive_frequency_ghz", "start_frequency_ghz", "stop_frequency_ghz", "sweep_type")
        }
        source = {"simulation": {**before, "pcie_generation_preset": name}}
        applied, changes = apply_frequency_preset(source, name)
        self.app._ui_sync = True
        try:
            for key, value in applied["simulation"].items():
                path = "simulation." + key
                if path in self.app.variables:
                    shown = SWEEP_TYPE_LABELS.get(str(value), value) if key == "sweep_type" else value
                    self.app.variables[path].set(str(shown))
        finally:
            self.app._ui_sync = False
        detail = "；".join(f"{key}: {old} → {new}" for key, (old, new) in changes.items()) or "数值已与预设一致"
        self.preset_state_var.set(f"已应用 {name}：{detail}")
        self.app._schedule_refresh()

    def _display_scan_value(self, spec: Any, value: Any) -> str:
        if spec.unit_type == "length":
            return format_length(float(value), self.app.display_unit)
        return format_number(float(value), 6) if isinstance(value, (int, float)) else str(value)

    def _parse_scan_value(self, spec: Any, text: str) -> Any:
        value: Any = text.strip()
        if spec.value_type != "choice":
            value = float(value)
            if spec.unit_type == "length":
                value = convert_length(value, self.app.display_unit, "mm")
            if spec.value_type == "integer":
                value = int(value)
        return value

    def _add_sweep_parameter(self) -> None:
        spec = self.spec_by_label[self.parameter_var.get()]
        current = spec.current_value(self.current_config)
        node = copy.deepcopy(self.sweeps.get(spec.path, {}))
        node["enabled"] = True
        if spec.value_type == "choice":
            node = {"enabled": True, "values": [current]}
        elif not ({"start", "stop", "step"} <= set(node)):
            number = float(current)
            step = 1 if spec.value_type == "integer" else max(abs(number) * 0.1, 0.01)
            node.update(start=number, stop=number, step=step)
        self.sweeps[spec.path] = node
        self.prepare_collect()
        self._refresh_sweep_tree(self.current_config)
        self.sweep_tree.selection_set(spec.path)
        self._sweep_selected()
        self.app._schedule_refresh()

    def _remove_sweep_parameter(self) -> None:
        selection = self.sweep_tree.selection()
        if not selection:
            return
        for path in selection:
            if path in self.sweeps:
                self.sweeps[path]["enabled"] = False
        self.prepare_collect(); self._refresh_sweep_tree(self.current_config); self.app._schedule_refresh()

    def _sweep_selected(self, _event: Any = None) -> None:
        selection = self.sweep_tree.selection()
        if not selection:
            return
        path = selection[0]
        spec, node = get_sweep_parameter_spec(path), self.sweeps[path]
        if spec is None:
            return
        self.edit_path_var.set(f"{spec.label}  ·  {path}")
        mode = "离散值" if "values" in node else "连续范围"
        self.scan_mode_var.set(mode)
        for key in ("start", "stop", "step"):
            self.scan_vars[key].set(self._display_scan_value(spec, node.get(key, "")) if key in node else "")
        self.scan_vars["values"].set(", ".join(self._display_scan_value(spec, value) for value in node.get("values", [])))
        self.scan_unit_var.set(f"单位：{self.app.display_unit if spec.unit_type == 'length' else '计数' if spec.unit_type == 'count' else '无'}；来源：{spec.source_page}")
        self.scan_editor_issue_var.set("；".join(spec.dependency_issues(self.current_config)))
        self._update_scan_editor_state()

    def _update_scan_editor_state(self) -> None:
        discrete = self.scan_mode_var.get() == "离散值"
        for key in ("start", "stop", "step"):
            self.scan_entries[key].configure(state="disabled" if discrete else "normal")
        self.scan_entries["values"].configure(state="normal" if discrete else "disabled")

    def _save_sweep_editor(self) -> None:
        selection = self.sweep_tree.selection()
        if not selection:
            return
        path = selection[0]; spec = get_sweep_parameter_spec(path)
        if spec is None:
            return
        try:
            if self.scan_mode_var.get() == "离散值":
                values = [self._parse_scan_value(spec, item) for item in self.scan_vars["values"].get().replace("；", ",").split(",") if item.strip()]
                node = {"enabled": True, "values": values}
            else:
                if "range" not in spec.scan_modes:
                    raise ValueError("此参数只支持离散值扫描。")
                node = {"enabled": True, **{key: self._parse_scan_value(spec, self.scan_vars[key].get())
                                           for key in ("start", "stop", "step")}}
            scan_values(path, node)
            self.sweeps[path] = node
            self.prepare_collect(); self._refresh_sweep_tree(self.current_config); self.app._schedule_refresh()
            self.scan_editor_issue_var.set("扫描值已应用。")
        except Exception as exc:
            self.scan_editor_issue_var.set("错误：" + str(exc))

    def _refresh_sweep_tree(self, config: Mapping[str, Any]) -> None:
        if not hasattr(self, "sweep_tree"):
            return
        selected = set(self.sweep_tree.selection())
        self.sweep_tree.delete(*self.sweep_tree.get_children())
        merged = copy.deepcopy(dict(config)); merged["sweeps"] = copy.deepcopy(self.sweeps)
        for path, node in self.sweeps.items():
            if not isinstance(node, Mapping) or not node.get("enabled") or get_sweep_parameter_spec(path) is None:
                continue
            spec = get_sweep_parameter_spec(path)
            try:
                values = scan_values(path, node)
                display = ", ".join(self._display_scan_value(spec, value) for value in values[:5])
                if len(values) > 5: display += " …"
                status = "有效" if not spec.dependency_issues(merged) else "；".join(spec.dependency_issues(merged))
                count = len(values)
            except Exception as exc:
                display, count, status = "—", 0, "错误：" + str(exc)
            self.sweep_tree.insert("", "end", iid=path, values=(spec.label, spec.source_page,
                "离散" if "values" in node else "范围", display, count, status))
        for item in selected:
            if self.sweep_tree.exists(item): self.sweep_tree.selection_add(item)
        self._refresh_workload(merged)

    def _refresh_workload(self, config: Mapping[str, Any]) -> None:
        if not hasattr(self, "workload_var"):
            return
        merged = copy.deepcopy(dict(config))
        merged["sweeps"] = copy.deepcopy(self.sweeps)
        native_plan = inspect_sweep_plan(merged)
        native_axes = " × ".join(
            f"{get_sweep_parameter_spec(path).label if get_sweep_parameter_spec(path) else path}:{len(values)}"
            for path, values in native_plan.native_axes
        ) or "无原生参数轴"
        topology_axes = " × ".join(
            f"{get_sweep_parameter_spec(path).label if get_sweep_parameter_spec(path) else path}:{len(values)}"
            for path, values in native_plan.topology_axes
        ) or "无拓扑变体"
        has_discrete = bool(native_plan.topology_axes)
        project_count = native_plan.design_count if has_discrete else 1
        setup_count = 0 if has_discrete else native_plan.optimetrics_setup_count
        total_cases = native_plan.case_count * native_plan.design_count
        self.workload_var.set(
            f"执行后端：{native_plan.strategy}；Variation 组合：{native_plan.variation_mode}\n"
            f"原生参数轴：{native_axes}\n拓扑 Design：{topology_axes}\n"
            f"HFSS Project/Design={project_count}；Optimetrics Setup={setup_count}\n"
            f"总执行 case/Variation={total_cases}；离散几何 case={native_plan.design_count}\n"
            + ("；".join(message for _, message in native_plan.issues) or "扫描计划与依赖检查通过")
        )

    def _collect_valid_config(self) -> Mapping[str, Any]:
        config, validation = self.app.manager.validate(self.app._collect(), check_output_directory=False)
        if not validation.is_valid:
            raise ScriptGenerationError("配置预检失败：\n" + "\n".join(validation.errors))
        return config

    def _variation_rows(self, config: Mapping[str, Any], full: bool) -> List[tuple[int, Mapping[str, Any], str]]:
        """Preview the selected Variation mode instead of expanding legacy CASE files."""
        if normalize_execution_strategy(config) == "legacy_discrete":
            cases = expand_sweep_cases(config, int(config.get("sweep_execution", {}).get("max_cases", 200)))
            return [(index, variation, "passed" if not full else "passed")
                    for index, (_case, variation) in enumerate(cases, 1)]

        plan = inspect_sweep_plan(config, int(config.get("sweep_execution", {}).get("max_cases", 200)))
        variations = iter_materialized_variations(plan, config)
        report = preflight_sweep_geometry(config) if full else None
        state = "passed" if not report or not report.errors else "failed: " + report.errors[0]
        return [(index, variation, state) for index, variation in enumerate(variations, 1)]

    def _populate_case_preview(self, config: Mapping[str, Any], rows: Sequence[tuple[int, Mapping[str, Any], str]]) -> None:
        self.case_tree.delete(*self.case_tree.get_children())
        self._variation_by_iid: Dict[str, Dict[str, Any]] = {}
        sim = config["simulation"]
        report_names = [name for name in REPORT_LABELS if sim.get("report_outputs", {}).get(name)]
        report_text = ",".join(report_names) if sim.get("enabled") and sim.get("generate_reports") and sim.get("create_differential_ports") else "无"
        plan = inspect_sweep_plan(config)
        geometry_text = "重新计算的离散case" if plan.topology_axes else "同一Design内参数变化"
        for index, variation, status in rows:
            self._variation_by_iid[str(index)] = dict(variation)
            variation_text = "; ".join(f"{get_sweep_parameter_spec(k).label if get_sweep_parameter_spec(k) else k}={v}" for k, v in variation.items()) or "名义参数"
            self.case_tree.insert("", "end", iid=str(index), values=(f"Variation {index:03d}", variation_text, geometry_text,
                "待运行" if sim.get("enabled") else "关闭", report_text, status))

    def preview_cases(self) -> None:
        try:
            config = self._collect_valid_config()
            rows = self._variation_rows(config, False)
            self._populate_case_preview(config, rows)
            plan = inspect_sweep_plan(config)
            detail = ("将生成独立离散 Design/case。" if plan.topology_axes else
                      "共享一个 Project/Design，由原生 Optimetrics 执行。")
            self.execution_status_var.set(f"已预览 {len(rows)} 个 Variation；{detail}")
            self.steps.select(self.execution_page)
        except Exception as exc:
            messagebox.showerror("Variation 预览失败", str(exc), parent=self.app)

    def preflight_cases(self) -> None:
        try:
            config = self._collect_valid_config()
            rows = self._variation_rows(config, True)
            self._populate_case_preview(config, rows)
            failures = sum(status != "passed" for _, _, status in rows)
            self.execution_status_var.set(f"全量预检完成：{len(rows)} 个 Variation，失败 {failures} 个。")
            self.output_var.set(str(self.app.manager.output_directory(config) / (config["project"]["project_name"] + "_SWEEPS")))
            self.steps.select(self.execution_page)
        except Exception as exc:
            messagebox.showerror("全量预检失败", str(exc), parent=self.app)

    def generate_cases(self) -> None:
        try:
            config = self._collect_valid_config()
            root = self.app.manager.output_directory(config) / (config["project"]["project_name"] + "_SWEEPS")
            limit = int(config.get("sweep_execution", {}).get("max_cases", 200))
            self._generated = generate_sweep_cases(config, root, limit)
            manifest = json.loads(self._generated.manifest_path.read_text(encoding="utf-8"))
            if manifest.get("backend") == "native_optimetrics":
                assert_native_manifest_safe(self._generated.manifest_path)
            rows = [(i, variation, "generated") for i, variation, _status in self._variation_rows(config, False)]
            self._populate_case_preview(config, rows)
            self.output_var.set(str(root))
            if manifest.get("backend") == "native_optimetrics":
                status = f"已生成原生 HFSS 扫描：{len(rows)} 个 Variation，共用一个 Project/Design。"
            else:
                status = (f"已生成 {manifest.get('design_count', len(rows))} 个重新计算的"
                          "离散 HFSS Design/case；拓扑与逐层轮廓未被冻结。")
            self.execution_status_var.set(status)
        except Exception as exc:
            messagebox.showerror("创建参数扫描失败", str(exc), parent=self.app)

    def run_cases(self) -> None:
        if not self._generated:
            self.generate_cases()
        if not self._generated:
            return
        manifest = json.loads(self._generated.manifest_path.read_text(encoding="utf-8"))
        if manifest.get("execution_ready") is False:
            messagebox.showwarning("Topology sweep not executable", manifest.get("execution_blocked_reason", "Topology variants require same-project multi-Design generation."), parent=self.app)
            return
        self._paused = False
        native = manifest.get("backend") == "native_optimetrics"
        self._run_queue = [1] if native else list(range(1, len(self._generated.cases) + 1))
        self.execution_status_var.set(
            "Optimetrics 已启动：仅运行一次建模脚本。" if native else
            f"离散几何扫描已启动：将顺序运行 {len(self._run_queue)} 个独立 Design/case。"
        )
        self._start_next_case()

    def _start_next_case(self) -> None:
        if self._process is not None or self._paused or not self._generated:
            return
        if not self._run_queue:
            self.execution_status_var.set("扫描运行完成；可点击“汇总结果”。")
            return
        index = self._run_queue.pop(0)
        manifest = json.loads(self._generated.manifest_path.read_text(encoding="utf-8"))
        native = manifest.get("backend") == "native_optimetrics"
        result = self._generated.project if native else self._generated.cases[index - 1]
        command = ([sys.executable, "--run-generated-script", str(result.script_path)]
                   if getattr(sys, "frozen", False) else [sys.executable, str(result.script_path)])
        kwargs: Dict[str, Any] = {"cwd": str(result.project_directory)}
        child_environment = os.environ.copy()
        if native:
            child_environment["SURFACE_CAP_RUN_OPTIMETRICS"] = "1"
        kwargs["env"] = child_environment
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        try:
            self._process = subprocess.Popen(command, **kwargs)
            self._running_case = index
            update_manifest_case_status(self._generated.manifest_path, index, "running")
            if self.case_tree.exists(str(index)):
                self.case_tree.set(str(index), "status", "Optimetrics运行中" if native else "离散case运行中")
            self.after(700, self._poll_process)
        except Exception as exc:
            update_manifest_case_status(self._generated.manifest_path, index, "failed", message=str(exc))
            if self.case_tree.exists(str(index)):
                self.case_tree.set(str(index), "status", "启动失败")
            self._process = None; self.after_idle(self._start_next_case)

    def _poll_process(self) -> None:
        if self._process is None or not self._generated:
            return
        code = self._process.poll()
        if code is None:
            self.after(700, self._poll_process); return
        index = self._running_case
        status = "completed" if code == 0 else "failed"
        update_manifest_case_status(self._generated.manifest_path, index, status, code,
                                    "" if code == 0 else f"进程退出码 {code}")
        if self.case_tree.exists(str(index)):
            self.case_tree.set(str(index), "status", "完成" if code == 0 else f"失败 ({code})")
        self._process = None; self._running_case = 0
        self.after_idle(self._start_next_case)

    def toggle_pause(self) -> None:
        self._paused = not self._paused
        self.execution_status_var.set("运行已暂停；当前 HFSS 进程会继续运行。" if self._paused else "HFSS 扫描已恢复。")
        if not self._paused: self._start_next_case()

    def retry_failed(self) -> None:
        if not self._generated:
            return
        manifest = json.loads(self._generated.manifest_path.read_text(encoding="utf-8"))
        if manifest.get("backend") == "native_optimetrics":
            failed_indices = [1] if manifest.get("execution", {}).get("status") == "failed" else []
        else:
            failed_indices = [int(item["index"]) for item in manifest.get("cases", [])
                              if item.get("status") == "failed"]
        for index in failed_indices:
            if index not in self._run_queue:
                self._run_queue.append(index)
        self._paused = False
        self.execution_status_var.set(
            f"已加入 {len(failed_indices)} 个失败扫描重试。" if failed_indices else "没有失败的扫描运行。")
        self._start_next_case()

    def summarize_results(self) -> None:
        if not self._generated:
            messagebox.showinfo("结果汇总", "尚未创建参数扫描。", parent=self.app); return
        try:
            target = summarize_sweep_results(self._generated.manifest_path)
            self.execution_status_var.set("结果状态已汇总：" + target.name)
            self.output_var.set(str(target))
        except Exception as exc:
            messagebox.showerror("结果汇总失败", str(exc), parent=self.app)

    def apply_selected_variation(self) -> None:
        """Backfill one audited Variation; never invents missing solve results."""
        selection = self.case_tree.selection()
        iid = str(selection[0]) if selection else ""
        variation = getattr(self, "_variation_by_iid", {}).get(iid)
        if not variation:
            messagebox.showinfo(
                "回填Variation", "请先预览 Variations，并在表格中选择一行。",
                parent=self.app,
            )
            return
        try:
            updated = copy.deepcopy(self.app._collect())
            for path, value in variation.items():
                apply_sweep_value(updated, path, value)
            self.app._load_to_widgets(updated)
            self.execution_status_var.set(
                "已回填所选 Variation；这只应用真实参数，不会伪造 S 参数、TDR 或评分。"
            )
        except Exception as exc:
            messagebox.showerror("回填Variation失败", str(exc), parent=self.app)
