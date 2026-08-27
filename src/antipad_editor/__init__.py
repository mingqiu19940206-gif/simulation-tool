"""V2.1 反焊盘公开入口：预览修复、模式语义约束与动态建议。"""
from __future__ import annotations

import math
import tkinter as tk
from typing import Any, Dict, Mapping, Sequence

import antipad_editor_legacy as _legacy
from antipad_editor_legacy import *  # noqa: F401,F403 - 保持既有公开 API
from antipad_semantic import (
    EXPECTED_COUNTS,
    ModeSemanticStatus,
    analyze_all_modes,
    connected_window_count,
    topology_sweep_warnings,
)
from config_manager import format_length
from gui_layout import debounce_after


_MODE_LABELS = {
    "independent_pad": "每个焊盘独立开窗",
    "merged_per_capacitor": "每颗电容合并开窗",
    "merged_all": "P/N 全部合并开窗",
}
_GENERIC_OVERLAP_WARNINGS = {
    "当前层多个反焊盘发生重叠或接触。",
    "工具体发生接触或重叠，最终布尔相减后的实际连通开窗少于工具体数量。",
}


def _length_text(value: float, unit: str) -> str:
    return f"{format_length(value, unit)} {unit}"


def _finite_length_text(value: float, unit: str) -> str:
    return "—" if math.isinf(value) else _length_text(value, unit)


def analyze_antipad(
    config: Mapping[str, Any], geometry: Any, layer_name: str,
    unit: str, base_errors: Sequence[str] = (), base_warnings: Sequence[str] = (),
) -> Any:
    """在既有摘要上追加真实拓扑、动态临界值和扫描风险。"""
    analysis = _legacy.analyze_antipad(
        config, geometry, layer_name, unit, base_errors, base_warnings
    )
    statuses = analyze_all_modes(config, geometry, layer_name)
    mode = str(config["antipad"]["mode"])
    status = statuses[mode]
    analysis.warnings = [
        message for message in analysis.warnings
        if message not in _GENERIC_OVERLAP_WARNINGS
    ]
    analysis.infos = []
    analysis.semantic = status
    analysis.all_semantics = statuses
    if not status.tool_count:
        analysis.values["设计工具体"] = "—"
        analysis.values["实际连通开窗"] = "—"
        analysis.values["最小工具间余铜"] = "—"
        analysis.values["开窗到平面边缘"] = "—"
        analysis.values["拓扑临界净距"] = "—"
        analysis.infos.append("当前层未启用反焊盘，不执行模式语义约束。")
        return analysis

    analysis.values["设计工具体"] = str(status.expected_count)
    analysis.values["实际连通开窗"] = str(status.connected_count)
    analysis.values["最小工具间余铜"] = _finite_length_text(status.minimum_gap, unit)
    analysis.values["开窗到平面边缘"] = _finite_length_text(status.plane_edge_margin, unit)
    analysis.values["拓扑临界净距"] = (
        "—" if status.critical_expansion is None
        else _length_text(status.critical_expansion, unit)
    )

    if status.mismatch:
        contacts = "；接触对：" + "、".join(status.contact_pairs) if status.contact_pairs else ""
        analysis.warnings.append(
            f"模式语义不匹配：{_MODE_LABELS[mode]}期望 {status.expected_count} 个实际开窗，"
            f"当前只有 {status.connected_count} 个{contacts}。"
        )
    if status.critical_expansion is not None:
        condition = f"E < {_length_text(status.critical_expansion, unit)}"
        if status.recommended_expansion is not None:
            suggestion = _length_text(status.recommended_expansion, unit)
            analysis.infos.append(
                f"保持 {status.expected_count} 个实际开窗需 {condition}；"
                f"建议净距不大于 {suggestion}（临界值预留 5%/至少 "
                f"{_length_text(0.001, unit)} 裕量）。"
            )
        else:
            analysis.warnings.append(
                f"源焊盘在 E=0 时已经无法保持 {status.expected_count} 个独立开窗。"
            )
    else:
        analysis.infos.append("P/N 全部合并模式固定为 1 个连通开窗，应重点检查参考平面余铜。")

    trace_width = float(config["trace"]["trace_width"])
    if status.plane_edge_margin < trace_width and status.plane_edge_margin > 0:
        analysis.warnings.append("开窗到参考平面边缘的最小余铜小于走线宽度。")
    analysis.warnings.extend(topology_sweep_warnings(config, layer_name, status))
    analysis.errors = list(dict.fromkeys(analysis.errors))
    analysis.warnings = list(dict.fromkeys(analysis.warnings))
    analysis.infos = list(dict.fromkeys(analysis.infos))
    return analysis


class AntipadWorkbench(_legacy.AntipadWorkbench):
    """加入模式语义提示，同时保留去抖后的 Treeview 行为。"""

    def __init__(self, parent: tk.Misc, app: Any) -> None:
        self._last_semantic: ModeSemanticStatus | None = None
        self._mode_detail_labels: Dict[str, tk.Label] = {}
        super().__init__(parent, app)
        self._last_follow_layer = str(
            app.config.get("antipad", {}).get("auto_follow_layer", "")
        )

    def _build(self) -> None:
        super()._build()
        self._mode_detail_labels.update(self.mode_selector.count_labels)

        geometry_box = self.app.widgets["antipad.global_expansion"].master
        self.semantic_hint_var = tk.StringVar(value="正在计算模式安全范围…")
        self.semantic_hint_label = tk.Label(
            geometry_box, textvariable=self.semantic_hint_var, justify="left",
            anchor="w", background="#eff6ff", foreground="#1d4ed8",
            padx=7, pady=5,
        )
        self.semantic_hint_label.grid(
            row=2, column=0, columnspan=4, sticky="ew", padx=4, pady=(3, 2)
        )
        actions = tk.Frame(geometry_box, background="#f5f5f5")
        actions.grid(row=3, column=0, columnspan=4, sticky="w", padx=4, pady=(1, 4))
        self.safe_button = tk.Button(
            actions, text="应用建议净距", command=self._apply_recommended_expansion,
            padx=8, pady=2,
        )
        self.safe_button.pack(side="left", padx=(0, 6))
        self.switch_button = tk.Button(
            actions, text="切换为匹配模式", command=self._switch_to_matching_mode,
            padx=8, pady=2,
        )
        self.switch_button.pack(side="left")
        self.safe_button.configure(state="disabled")
        self.switch_button.configure(state="disabled")
        self.issue_text.tag_configure("info", foreground="#1d4ed8")

        def resize_semantic_hint() -> None:
            self.semantic_hint_label.configure(
                wraplength=max(1, geometry_box.winfo_width() - 22)
            )

        geometry_box.bind(
            "<Configure>",
            lambda _event: debounce_after(
                self.semantic_hint_label, "_semantic_wrap_job", resize_semantic_hint
            ),
            add="+",
        )
        geometry_box.after_idle(resize_semantic_hint)

    def select_layer(self, index: int) -> None:
        layers = self.app.config.get("stackup", [])
        if not (0 <= index < len(layers)):
            return
        changed = index != self.selected_index
        self.selected_index = index
        self.tree.selection_set(str(index))
        self.tree.see(str(index))
        if getattr(self, "_via_mode", False):
            self.preview.layer_var.set(str(layers[index]["name"]))
            self._sync_layer_panel()
            if self.geometry is not None:
                self.cross_section.set_via_data(self.geometry, self.app.display_unit, index)
                self.preview.redraw()
        else:
            self.preview.layer_var.set(str(layers[index]["name"]))
            self._sync_layer_panel()
            if self.geometry is not None:
                self.cross_section.set_data(self.geometry, self.app.display_unit, index)
                self.preview.redraw()
        if changed:
            self.app._schedule_refresh()

    def _tree_selected(self, _event: Any = None) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        index = int(selected[0])
        if index == self.selected_index:
            self._sync_layer_panel()
            return
        self.select_layer(index)

    def _update_mode_cards(self, statuses: Mapping[str, ModeSemanticStatus]) -> None:
        self.mode_selector.update_statuses(statuses)

    def _update_semantic_controls(self, status: ModeSemanticStatus) -> None:
        self._last_semantic = status
        unit = self.app.display_unit
        if not status.tool_count:
            self.semantic_hint_var.set("ℹ 当前层未启用反焊盘。")
            self.semantic_hint_label.configure(background="#eff6ff", foreground="#1d4ed8")
            self.safe_button.configure(state="disabled")
            self.switch_button.configure(state="disabled")
            return
        current = _length_text(float(status.expansion or 0.0), unit)
        if status.critical_expansion is None:
            message = f"ℹ 当前有效 E={current}；全部合并模式固定为 1 个实际连通开窗。"
            background, foreground = "#eff6ff", "#1d4ed8"
        else:
            critical = _length_text(status.critical_expansion, unit)
            suggestion = (
                _length_text(status.recommended_expansion, unit)
                if status.recommended_expansion is not None else "无可用建议值"
            )
            prefix = "⚠" if status.mismatch else "✓"
            message = (
                f"{prefix} 当前有效 E={current}；设计 {status.expected_count} 个，实际 {status.connected_count} 个。\n"
                f"保持模式语义需 E < {critical}；建议值 ≤ {suggestion}。"
            )
            background, foreground = (
                ("#fff7ed", "#b45309") if status.mismatch else ("#ecfdf3", "#166534")
            )
        self.semantic_hint_var.set(message)
        self.semantic_hint_label.configure(background=background, foreground=foreground)
        self.safe_button.configure(
            state="normal" if status.recommended_expansion is not None else "disabled"
        )
        switchable = bool(
            status.mismatch and status.suggested_mode
            and status.suggested_mode != status.mode
        )
        self.switch_button.configure(state="normal" if switchable else "disabled")
        if switchable:
            self.switch_button.configure(
                text=f"切换为：{_MODE_LABELS[str(status.suggested_mode)]}"
            )
        else:
            self.switch_button.configure(text="切换为匹配模式")

    def _apply_recommended_expansion(self) -> None:
        status = self._last_semantic
        if status is None or status.recommended_expansion is None:
            return
        layer = self._selected_layer()
        if layer is not None and layer.get("antipad_expansion_override") is not None:
            layer["antipad_expansion_override"] = status.recommended_expansion
            self._sync_layer_panel()
        else:
            self.app.variables["antipad.global_expansion"].set(
                format_length(status.recommended_expansion, self.app.display_unit)
            )
        self.app._schedule_refresh()

    def _switch_to_matching_mode(self) -> None:
        status = self._last_semantic
        if status is None or not status.suggested_mode or status.suggested_mode == status.mode:
            return
        self.app.variables["antipad.mode"].set(status.suggested_mode)
        self.app._schedule_refresh()

    def show(self, config: Mapping[str, Any], geometry: Any, validation: Any) -> None:
        follow_layer = (
            str(config["antipad"].get("auto_follow_layer", ""))
            if config["antipad"].get("follow_mount_side") else ""
        )
        if follow_layer and follow_layer != self._last_follow_layer:
            target = next(
                (index for index, layer in enumerate(config["stackup"])
                 if str(layer.get("name", "")) == follow_layer),
                None,
            )
            if target is not None:
                self.selected_index = target
        self._last_follow_layer = follow_layer
        super().show(config, geometry, validation)
        layers = list(config["stackup"])
        self.selected_index = min(self.selected_index, len(layers) - 1)
        layer_name = str(layers[self.selected_index]["name"])
        analysis = analyze_antipad(
            config, geometry, layer_name, self.app.display_unit,
            validation.errors, validation.warnings,
        )
        self.summary_var.set("\n".join(f"{key}：{value}" for key, value in analysis.values.items()))
        self._update_mode_cards(analysis.all_semantics)
        self._update_semantic_controls(analysis.semantic)
        self.issue_text.configure(state="normal")
        self.issue_text.delete("1.0", "end")
        if not analysis.errors and not analysis.warnings:
            self.issue_text.insert("end", "✓ 当前配置未发现阻断性反焊盘或叠层风险。\n", "good")
        for message in analysis.errors:
            self.issue_text.insert("end", "● " + message + "\n", "error")
        for message in analysis.warnings:
            self.issue_text.insert("end", "▲ " + message + "\n", "warning")
        for message in analysis.infos:
            self.issue_text.insert("end", "ℹ " + message + "\n", "info")
        self.issue_text.configure(state="disabled")
