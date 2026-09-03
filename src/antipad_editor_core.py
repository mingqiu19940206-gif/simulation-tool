"""反焊盘可视化设计与结构化叠层编辑组件。

本模块只负责显示、交互和对 ``GeometryCalculator`` 结果的分析，不生成第二套
反焊盘几何。工程 JSON 中的英文枚举与长度（内部 mm）保持不变。
"""
from __future__ import annotations

import copy
import math
import re
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import messagebox, ttk
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from config_manager import DEFAULT_DISPLAY_UNIT, convert_length, format_length
from gui_layout import (
    CanvasLabelPlacer, attach_mousewheel, canvas_viewport, debounce_after,
    ellipsize, font_linespace, raise_canvas_labels, resolve_font,
    responsive_grid_columns,
)
from geometry_v1 import polygons_overlap
from via_antipad import contains_circle, effective_antipad, make_contour, projected_width


MODE_LABELS = {
    "independent_pad": "每个焊盘独立开窗",
    "merged_per_capacitor": "每颗电容合并开窗",
    "merged_all": "P/N 全部合并开窗",
}
LAYER_TYPE_LABELS = {
    "signal": "信号层",
    "plane": "平面层",
    "dielectric": "介质层",
    "soldermask": "阻焊层",
}
VIA_TOPOLOGY_LABELS = {
    "independent": "P/N 独立开窗",
    "merged_pair": "P/N 合并开窗",
}
VIA_SHAPE_LABELS = {
    "circle": "圆形",
    "ellipse": "椭圆形",
    "capsule": "胶囊形",
    "rounded_rectangle": "圆角矩形",
}
VIA_DIRECTION_LABELS = {
    "auto_pn": "自动沿 P/N 轴",
    "manual": "手动旋转",
}
VIA_SIZE_MODE_LABELS = {
    "auto_fit": "自动适配",
    "auto_plus_increment": "自动适配＋用户增量",
    "manual": "完全手动",
}
VIA_LAYER_STATE_LABELS = {
    "entry_connected": "输入连接焊盘",
    "exit_connected": "输出连接焊盘",
    "nonfunctional_pad": "保留非功能焊盘",
    "clearance": "生成反焊盘",
    "backdrilled_removed": "背钻影响/生成反焊盘",
    "outside_drill_span": "钻孔跨度外",
    "no_plane_copper": "无可减铜面",
}
VIA_PARAMETER_HELP = {
    "topology": "决定一个参考层上生成几个 Signal VIA 开窗：独立开窗以 P、N 各自孔心为中心；合并开窗以 P/N 中点为中心。",
    "shape": "决定实际减铜轮廓。独立开窗时 P、N 各生成一个圆形，请填写直径。",
    "direction_mode": "仅合并开窗使用。自动模式沿 P→N 中心连线定向；手动模式才采用旋转角输入。",
    "size_mode": "仅合并开窗使用。自动适配按孔距、有效孔径和净距计算尺寸；“＋用户增量”再叠加增量；完全手动才采用直径或长/宽输入。",
    "diameter": "圆形开窗的端到端直径。独立开窗时 P、N 各一个圆，请填写该直径。",
    "length": "非圆形轮廓沿局部 X 轴的端到端总长度，不是相对焊盘的单边外扩。",
    "width": "非圆形轮廓沿局部 Y 轴的端到端总宽度。",
    "corner_radius": "圆角矩形四个角的半径，范围为 0 到 min(长度, 宽度)/2。",
    "rotation_deg": "局部 X 轴相对全局 +X 的逆时针角度。",
    "clearance": "钻孔/背钻铜边最小净距。独立开窗或完全手动时只做制造预检，不增大开窗尺寸。",
    "end_clearance": "自动适配时，沿 P/N 轴两端超出孔包络的净距。",
    "side_clearance": "自动适配时，垂直 P/N 轴方向超出孔包络的净距。",
    "length_increment": "在自动适配所得长度上额外增加的总量；仅“自动适配＋用户增量”生效。",
    "width_increment": "在自动适配所得宽度上额外增加的总量；仅“自动适配＋用户增量”生效。",
    "return_diameter": "Return VIA 在非连接铜层上的圆形避空直径；连接层仍使用 Return VIA 焊盘。",
    "layer_source": "使用全局值时，本层随全局设置变化；本层覆盖会保存该层自己的拓扑、形状、方向、尺寸控制和尺寸。",
    "layer_enabled": "只对有可减铜面且位于钻孔跨度内的层有效。输入/输出连接层、保留非功能焊盘层和跨度外层不生成 Signal VIA 反焊盘。",
}
VIA_PARAMETER_TITLES = {
    "topology": "P/N 开窗拓扑", "shape": "反焊盘形状",
    "direction_mode": "合并开窗方向", "size_mode": "开窗尺寸控制",
    "diameter": "直径", "length": "端到端总长度", "width": "总宽度",
    "corner_radius": "圆角半径", "rotation_deg": "逆时针旋转角",
    "clearance": "最小铜边净距（钻孔/背钻）", "end_clearance": "端部净距",
    "side_clearance": "侧向净距", "length_increment": "自动适配长度增量",
    "width_increment": "自动适配宽度增量", "return_diameter": "Return VIA 避空直径",
    "layer_source": "本层尺寸来源", "layer_enabled": "本层是否生成反焊盘",
}
VIA_PARAMETER_VISUAL_HINTS = {
    "topology": "橙/蓝实线显示最终开窗数量与中心",
    "shape": "橙/蓝实线显示实际减铜轮廓",
    "direction_mode": "图中 X/Y 箭头显示局部方向",
    "size_mode": "橙色实线为最终轮廓，蓝虚线为规则最小轮廓",
    "diameter": "蓝色双箭头标出图中有效直径",
    "length": "蓝色双箭头标出局部 X 方向总长",
    "width": "蓝色双箭头标出局部 Y 方向总宽",
    "corner_radius": "蓝色轮廓与参数卡同步显示圆角半径",
    "rotation_deg": "图中 X/Y 箭头及角度文字显示实际方向",
    "clearance": "浅蓝虚线圆是孔/背钻外缘加最小净距后的禁入包络",
    "end_clearance": "蓝虚线最小轮廓显示 P/N 轴两端所需净距",
    "side_clearance": "蓝虚线最小轮廓显示 P/N 轴两侧所需净距",
    "length_increment": "蓝虚线到最终实线的长度差包含本项增量",
    "width_increment": "蓝虚线到最终实线的宽度差包含本项增量",
    "return_diameter": "蓝色双箭头标出 Return VIA 非连接层避空直径",
    "layer_source": "右侧参数卡标明全局继承或本层覆盖",
    "layer_enabled": "开关后当前层的实线开窗会出现或消失",
}
MODE_DETAILS = {
    "independent_pad": ("4 个", "保持实际旋转", "移除较少", "精细控制每个焊盘周围净距"),
    "merged_per_capacitor": ("2 个", "轴对齐包围盒", "移除中等", "每颗电容形成连续开窗"),
    "merged_all": ("1 个", "轴对齐包围盒", "移除最多", "需要重点检查参考平面连续性"),
}

# Global VIA antipad rows that hide/show with topology/shape/size mode.
# Help sits on row 4 between size_mode and diameter.
VIA_GLOBAL_FIELD_ROWS = {
    "direction_mode": 2, "size_mode": 3,
    "diameter": 5, "length": 6, "width": 7,
    "corner_radius": 8, "rotation_deg": 9,
    "end_clearance": 11, "side_clearance": 12,
    "length_increment": 13, "width_increment": 14,
}
_VIA_AUTO_SIZE_MODES = frozenset({"auto_fit", "auto_plus_increment"})


def via_antipad_field_states(
    topology: str,
    shape: str,
    direction_mode: str,
    size_mode: str,
) -> Dict[str, Any]:
    """Visibility/enablement for Signal VIA antipad fields.

    Independent openings are always manual (``manual_size = size_mode==manual
    or not merged``). JSON may still store ``size_mode=auto_fit``; the UI must
    not present direction/size-control as if they were in effect.
    Layer aliases reuse this helper so they cannot drift from the global box.
    """
    merged = topology == "merged_pair"
    manual_size = size_mode == "manual" or not merged
    auto_size = merged and size_mode in _VIA_AUTO_SIZE_MODES
    increment_enabled = merged and size_mode == "auto_plus_increment"
    visible = {
        "topology": True,
        "shape": True,
        "direction_mode": merged,
        "size_mode": merged,
        "diameter": shape == "circle" and manual_size,
        "length": shape != "circle" and manual_size,
        "width": shape != "circle" and manual_size,
        "corner_radius": shape == "rounded_rectangle",
        "rotation_deg": shape != "circle",
        "clearance": True,
        "end_clearance": auto_size,
        "side_clearance": auto_size,
        "length_increment": increment_enabled,
        "width_increment": increment_enabled,
    }
    enabled = dict(visible)
    enabled["rotation_deg"] = shape != "circle" and (not merged or direction_mode == "manual")
    return {
        "merged": merged,
        "manual_size": manual_size,
        "auto_size": auto_size,
        "increment_enabled": increment_enabled,
        "visible": visible,
        "enabled": enabled,
    }


def via_field_hint_sentences(
    topology: str,
    shape: str,
    direction_mode: str,
    size_mode: str,
) -> Tuple[str, ...]:
    """Grey footer: only reasons that apply to the currently visible mode."""
    states = via_antipad_field_states(topology, shape, direction_mode, size_mode)
    if not states["merged"]:
        return ()
    reasons: List[str] = []
    if direction_mode == "auto_pn" and shape != "circle":
        reasons.append("旋转角由 atan2(Ny-Py, Nx-Px) 派生，因此只读。")
    if states["auto_size"]:
        reasons.append("长度/宽度由真实孔距、逐层有效孔径和净距派生；原手动值不参与轮廓。")
    if not states["increment_enabled"]:
        reasons.append("尺寸增量仅在“自动适配＋用户增量”模式启用。")
    return tuple(reasons)


def via_parameter_help_text(
    key: str,
    *,
    topology: str = "independent",
    shape: str = "circle",
    direction_mode: str = "auto_pn",
    size_mode: str = "manual",
) -> str:
    """Blue help: only the focused field in the current topology/shape."""
    states = via_antipad_field_states(topology, shape, direction_mode, size_mode)
    merged = states["merged"]
    auto_size = states["auto_size"]
    if key == "shape":
        if shape == "circle":
            if merged:
                return "决定实际减铜轮廓。合并开窗时 P/N 共用一个圆形，直径为端到端尺寸。"
            return "决定实际减铜轮廓。独立开窗时 P、N 各生成一个圆形，请填写直径。"
        shape_name = VIA_SHAPE_LABELS.get(shape, shape)
        if merged:
            return f"决定实际减铜轮廓。{shape_name}使用局部 X 方向端到端长度和局部 Y 方向总宽度。"
        return (
            f"决定实际减铜轮廓。独立开窗时 P、N 各生成一个{shape_name}，"
            "使用局部 X 方向端到端长度和局部 Y 方向总宽度。"
        )
    if key == "diameter":
        if merged and auto_size:
            return "圆形开窗的端到端直径。自动适配时由孔包络和净距派生，输入框只读。"
        if merged:
            return "圆形开窗的端到端直径。合并开窗时 P/N 共用一个圆。"
        return "圆形开窗的端到端直径。独立开窗时 P、N 各一个圆，请填写该直径。"
    if key == "length":
        if auto_size:
            return "非圆形轮廓沿局部 X 轴的端到端总长度，不是相对焊盘的单边外扩。自动适配时显示图中有效值。"
        return "非圆形轮廓沿局部 X 轴的端到端总长度，不是相对焊盘的单边外扩。"
    if key == "width":
        if auto_size:
            return "非圆形轮廓沿局部 Y 轴的端到端总宽度。自动适配时显示图中有效值。"
        return "非圆形轮廓沿局部 Y 轴的端到端总宽度。"
    if key == "clearance":
        if auto_size:
            return (
                "钻孔/背钻铜边最小净距。"
                "合并自动适配用端部/侧向净距增大开窗；本项是制造预检，不单独撑大开窗。"
            )
        return (
            "钻孔/背钻铜边最小净距。"
            "独立开窗或完全手动时只做制造预检，不增大开窗尺寸。"
        )
    if key == "rotation_deg":
        if merged and direction_mode == "auto_pn":
            return "局部 X 轴相对全局 +X 的逆时针角度。自动沿 P/N 轴时由孔位计算，手工输入不参与几何。"
        return "局部 X 轴相对全局 +X 的逆时针角度。"
    return VIA_PARAMETER_HELP.get(key, "该参数会进入当前层的有效反焊盘配置。")


def effective_expansion(layer: Mapping[str, Any], global_expansion: float) -> Optional[float]:
    """返回某层的真实有效外扩；未启用时返回 ``None``。"""
    if not layer.get("enable_antipad"):
        return None
    override = layer.get("antipad_expansion_override")
    return float(global_expansion if override is None else override)


def expected_window_count(mode: str, enabled: bool = True) -> int:
    """返回三种兼容模式对应的工具体数量。"""
    if not enabled:
        return 0
    return {"independent_pad": 4, "merged_per_capacitor": 2, "merged_all": 1}[mode]


def _polygon_area(points: Sequence[Any]) -> float:
    return abs(sum(
        point.x * points[(index + 1) % len(points)].y
        - points[(index + 1) % len(points)].x * point.y
        for index, point in enumerate(points)
    )) / 2.0


def _shape_size(shape: Any) -> Tuple[float, float]:
    if len(shape.points) < 3:
        return shape.bounds.width, shape.bounds.height
    first, second, third = shape.points[0], shape.points[1], shape.points[2]
    return (math.hypot(second.x - first.x, second.y - first.y),
            math.hypot(third.x - second.x, third.y - second.y))


def _tool_overlap(tools: Sequence[Any]) -> bool:
    return any(
        polygons_overlap(tools[left].points, tools[right].points)
        for left in range(len(tools))
        for right in range(left + 1, len(tools))
    )


@dataclass
class AntipadAnalysis:
    values: Dict[str, str] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def analyze_antipad(
    config: Mapping[str, Any], geometry: Any, layer_name: str,
    unit: str, base_errors: Sequence[str] = (), base_warnings: Sequence[str] = (),
) -> AntipadAnalysis:
    """从真实几何结果构建摘要和本页风险，不改变任何几何。"""
    mode = str(config["antipad"]["mode"])
    global_expansion = float(config["antipad"]["global_expansion"])
    layers = list(config["stackup"])
    layer = next((item for item in layers if item["name"] == layer_name), layers[0])
    tools = list(geometry.antipads_by_layer.get(layer["name"], []))
    expansion = effective_expansion(layer, global_expansion)
    source = "本层覆盖" if layer.get("antipad_expansion_override") is not None else "使用全局值"
    sizes = [_shape_size(tool) for tool in tools]
    size_text = "；".join(
        f"{format_length(width, unit)} × {format_length(height, unit)} {unit}"
        for width, height in sizes
    ) or "—"
    total_area = sum(_polygon_area(tool.points) for tool in tools)
    area_scale = convert_length(1.0, "mm", unit) ** 2
    total_area_display = total_area * area_scale

    p_tools = [tool for tool in tools if tool.signal == "P"]
    n_tools = [tool for tool in tools if tool.signal == "N"]
    if mode == "merged_all" and tools:
        copper_gap = 0.0
        gap_text = "0（P/N 已整体合并）"
    elif p_tools and n_tools:
        copper_gap = min(
            max(0.0, p.bounds.y_min - n.bounds.y_max)
            for p in p_tools for n in n_tools
        )
        gap_text = f"{format_length(copper_gap, unit)} {unit}"
    else:
        copper_gap, gap_text = math.inf, "—"

    overlap = _tool_overlap(tools)
    outside = any(not geometry.plane_bounds.contains(tool.bounds) for tool in tools)
    deletes_plane = any(
        tool.bounds.width >= geometry.plane_bounds.width - 1e-9
        and tool.bounds.height >= geometry.plane_bounds.height - 1e-9
        for tool in tools
    )
    active_count = sum(
        bool(item.get("type") == "plane" and item.get("is_reference_plane")
             and item.get("enable_antipad")) for item in layers
    )
    values = {
        "当前模式": MODE_LABELS.get(mode, mode),
        "启用参考层": f"{active_count} 层",
        "当前层": str(layer["name"]),
        "外扩来源": source if tools else "—",
        "有效外扩": f"{format_length(expansion, unit)} {unit}" if expansion is not None else "—",
        "开窗数量": str(len(tools)) if tools else "—",
        "最终尺寸": size_text,
        "预计总面积": f"{total_area_display:.6g} {unit}²" if tools else "—",
        "P/N 最小余铜": gap_text,
        "开窗重叠": "是" if overlap else "否",
        "超出平面": "是" if outside else "否",
        "可能删除平面": "是" if deletes_plane else "否",
    }
    errors = list(dict.fromkeys(base_errors))
    warnings = list(dict.fromkeys(base_warnings))
    for message in geometry.errors:
        if message not in errors:
            errors.append(message)
    for message in geometry.warnings:
        if message not in warnings:
            warnings.append(message)
    if mode == "merged_all" and tools:
        warnings.append("P/N 全部合并开窗通常移除最多铜皮，请检查参考平面连续性。")
    rotation = abs(float(config["capacitor"].get("pad_rotation", 0.0)))
    if mode != "independent_pad" and rotation > 5.0:
        warnings.append("旋转焊盘采用轴对齐包围盒，合并开窗会额外移除铜皮。")
    if copper_gap != math.inf and copper_gap < float(config["trace"]["trace_width"]):
        warnings.append("P/N 开窗之间的最小剩余铜宽小于走线宽度。")
    if overlap:
        warnings.append("当前层多个反焊盘发生重叠或接触。")
    if active_count == 0:
        warnings.append("当前所有参考层都未启用反焊盘。")
    override = layer.get("antipad_expansion_override")
    if override is not None and abs(float(override) - global_expansion) > max(global_expansion, 0.1):
        warnings.append("当前层外扩与全局值差异较大。")
    return AntipadAnalysis(values, list(dict.fromkeys(errors)), list(dict.fromkeys(warnings)))


def layer_from_form(values: Mapping[str, Any], unit: str) -> Dict[str, Any]:
    """校验结构化编辑器内容并转换为内部 mm/JSON 字段。"""
    name = str(values.get("name", "")).strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
        raise ValueError("层名称须以英文字母开头，且只包含字母、数字和下划线。")
    kind = str(values.get("type", ""))
    if kind not in LAYER_TYPE_LABELS:
        raise ValueError("请选择有效的层类型。")
    try:
        thickness = convert_length(float(values["thickness"]), unit, "mm")
    except (KeyError, TypeError, ValueError):
        raise ValueError("厚度必须是有效数字。") from None
    if thickness <= 0:
        raise ValueError("层厚必须大于零。")
    is_reference = bool(values.get("is_reference_plane")) if kind == "plane" else False
    enable_antipad = bool(values.get("enable_antipad")) if is_reference else False
    use_global = bool(values.get("use_global", True))
    override: Optional[float] = None
    if not use_global:
        try:
            override = convert_length(float(values.get("override", "")), unit, "mm")
        except (TypeError, ValueError):
            raise ValueError("本层外扩覆盖值必须是有效数字。") from None
        if override < 0:
            raise ValueError("本层外扩覆盖值必须大于或等于零。")

    def optional_number(key: str) -> Optional[float]:
        raw = str(values.get(key, "")).strip()
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            raise ValueError(f"{key} 必须是有效数字。") from None

    if kind in {"dielectric", "soldermask"}:
        dk, df, conductivity = optional_number("dk"), optional_number("df"), None
        if dk is None or dk <= 0:
            raise ValueError("介质/阻焊层 Dk 必须大于零。")
        if df is None or df < 0:
            raise ValueError("介质/阻焊层 Df 必须大于或等于零。")
    else:
        dk, df = None, None
        conductivity = optional_number("conductivity") if kind in {"signal", "plane"} else None
        if conductivity is not None and conductivity <= 0:
            raise ValueError("电导率必须大于零。")
    material = str(values.get("material", "")).strip()
    if not material:
        raise ValueError("材料名称不能为空。")
    return {
        "name": name, "type": kind, "thickness": thickness, "material": material,
        "dk": dk, "df": df, "conductivity": conductivity,
        "is_reference_plane": is_reference, "enable_antipad": enable_antipad,
        "antipad_expansion_override": override,
    }


class ModeSelector(ttk.Frame):
    """Compact semantic selector; card rows follow this widget's width."""

    MODES = tuple(MODE_LABELS)

    def __init__(self, master: tk.Misc, variable: tk.StringVar, changed: Callable[[], None]) -> None:
        super().__init__(master)
        self.variable, self.changed = variable, changed
        self.cards: Dict[str, tk.Frame] = {}
        self.card_widgets: Dict[str, Tuple[tk.Widget, ...]] = {}
        self.count_labels: Dict[str, tk.Label] = {}
        self.status_labels: Dict[str, tk.Label] = {}
        self.current_columns = 1
        self._layout_job: Optional[str] = None
        self._statuses: Dict[str, Any] = {}
        for index, mode in enumerate(self.MODES):
            card = tk.Frame(
                self, background="white", highlightthickness=2,
                highlightbackground="#cbd5e1", padx=4, pady=5, cursor="hand2",
            )
            card.grid(row=index, column=0, sticky="nsew", padx=3, pady=2)
            content = tk.Frame(card, background="white")
            content.pack(fill="both", expand=True)
            diagram = tk.Canvas(
                content, width=50, height=34, background="white", highlightthickness=0,
            )
            diagram.pack(side="left", padx=(0, 6))
            self._draw_icon(diagram, mode)
            text = tk.Frame(content, background="white")
            text.pack(side="left", fill="both", expand=True)
            title = tk.Label(
                text, text=MODE_LABELS[mode], background="white", foreground="#172033",
                font="ViaCardTitleFont", anchor="w",
            )
            title.pack(fill="x")
            design = MODE_DETAILS[mode][0].replace(" 个", "")
            count_label = tk.Label(
                text, text=f"设计 {design} · 当前 —", background="white",
                foreground="#475569", font="ViaCardBodyFont", anchor="w",
            )
            count_label.pack(fill="x")
            status_label = tk.Label(
                text, text=MODE_DETAILS[mode][2], background="white",
                foreground="#64748b", font="ViaSmallFont", anchor="w",
            )
            status_label.pack(fill="x")
            widgets: Tuple[tk.Widget, ...] = (
                card, content, diagram, text, title, count_label, status_label,
            )
            for widget in widgets:
                widget.bind("<Button-1>", lambda _e, value=mode: self.select(value))
            self.cards[mode] = card
            self.card_widgets[mode] = widgets
            self.count_labels[mode] = count_label
            self.status_labels[mode] = status_label

        self.description_var = tk.StringVar()
        self.description_label = tk.Label(
            self, textvariable=self.description_var, background="#f8fafc",
            foreground="#475569", font="ViaCardBodyFont", justify="left",
            anchor="w", padx=8, pady=5,
        )
        self.description_label.grid(
            row=len(self.MODES), column=0, sticky="ew", padx=3, pady=(3, 1),
        )
        self.bind("<Configure>", self._configured, add="+")
        variable.trace_add("write", lambda *_: self._update_selection())
        self.after_idle(self._apply_layout)
        self._update_selection()

    @staticmethod
    def _draw_icon(canvas: tk.Canvas, mode: str) -> None:
        pads = ((10, 7, 20, 13), (29, 7, 39, 13), (10, 21, 20, 27), (29, 21, 39, 27))
        colors = ("#b91c1c", "#b91c1c", "#1d4ed8", "#1d4ed8")
        for coords, color in zip(pads, colors):
            canvas.create_rectangle(*coords, fill=color, outline=color)
        if mode == "independent_pad":
            for x1, y1, x2, y2 in pads:
                canvas.create_rectangle(x1 - 2, y1 - 2, x2 + 2, y2 + 2, outline="#f59e0b")
        elif mode == "merged_per_capacitor":
            canvas.create_rectangle(6, 4, 43, 16, outline="#f59e0b", width=2)
            canvas.create_rectangle(6, 18, 43, 30, outline="#f59e0b", width=2)
        else:
            canvas.create_rectangle(6, 4, 43, 30, outline="#f59e0b", width=2)

    def _configured(self, _event: tk.Event) -> None:
        debounce_after(self, "_layout_job", self._apply_layout)

    def _minimum_card_width(self) -> int:
        title_font = resolve_font(self, "ViaCardTitleFont")
        body_font = resolve_font(self, "ViaCardBodyFont")
        status_font = resolve_font(self, "ViaSmallFont")
        text_width = max(
            max(title_font.measure(MODE_LABELS[mode]) for mode in self.MODES),
            body_font.measure("设计 4 · 当前 4"),
            status_font.measure("⚠ 数量不匹配"),
        )
        return 50 + 8 + text_width + 16

    def _apply_layout(self) -> None:
        width = max(1, int(self.winfo_width()))
        columns = responsive_grid_columns(
            width, len(self.MODES), self._minimum_card_width(), choices=(3, 1), gap=6,
        )
        if columns != self.current_columns:
            self.current_columns = columns
            for column in range(len(self.MODES)):
                self.columnconfigure(
                    column, weight=1 if column < columns else 0,
                    uniform="antipad-mode" if column < columns else "",
                )
            for index, mode in enumerate(self.MODES):
                self.cards[mode].grid_configure(row=index // columns, column=index % columns)
        rows = int(math.ceil(len(self.MODES) / columns))
        self.description_label.grid_configure(row=rows, column=0, columnspan=columns)
        self.description_label.configure(wraplength=max(1, width - 22))

    def select(self, mode: str) -> None:
        self.variable.set(mode)
        self.changed()

    def update_statuses(self, statuses: Mapping[str, Any]) -> None:
        self._statuses = dict(statuses)
        for mode in self.MODES:
            status = self._statuses.get(mode)
            design = MODE_DETAILS[mode][0].replace(" 个", "")
            active = bool(status is not None and getattr(status, "tool_count", 0))
            current = str(getattr(status, "connected_count", 0)) if active else "—"
            mismatch = bool(status is not None and getattr(status, "mismatch", False))
            self.count_labels[mode].configure(
                text=f"设计 {design} · 当前 {current}",
                foreground="#b45309" if mismatch else "#475569",
            )
            self.status_labels[mode].configure(
                text="⚠ 数量不匹配" if mismatch else MODE_DETAILS[mode][2],
                foreground="#b45309" if mismatch else "#64748b",
            )
        self._update_selection()

    def _update_selection(self) -> None:
        selected = self.variable.get()
        for mode, card in self.cards.items():
            selected_card = mode == selected
            background = "#eff6ff" if selected_card else "white"
            card.configure(
                highlightbackground="#2563eb" if selected_card else "#cbd5e1",
                highlightcolor="#2563eb" if selected_card else "#cbd5e1",
                background=background,
            )
            for widget in self.card_widgets[mode][1:]:
                if isinstance(widget, (tk.Frame, tk.Label, tk.Canvas)):
                    widget.configure(background=background)
        if selected in MODE_DETAILS:
            _count, rotation, _removal, usage = MODE_DETAILS[selected]
            self.description_var.set(f"{MODE_LABELS[selected]}：{rotation}；{usage}。")
        else:
            self.description_var.set("")
        self._apply_layout()


class AntipadPreview(ttk.Frame):
    """可缩放、分层显示的局部/完整平面预览。"""
    def __init__(self, master: tk.Misc, layer_changed: Callable[[str], None]) -> None:
        super().__init__(master)
        self.layer_changed = layer_changed
        self.config: Optional[Mapping[str, Any]] = None
        self.geometry: Any = None
        self._via_mode = False
        self.navigate: Optional[Callable[[str], None]] = None
        self.active_parameter: Optional[str] = None
        self.unit = DEFAULT_DISPLAY_UNIT
        self.zoom = 1.0
        toolbar = ttk.Frame(self)
        self.toolbar = toolbar
        toolbar.pack(fill="x", pady=(0, 4))
        self.layer_label = ttk.Label(toolbar, text="预览层")
        self.layer_label.pack(side="left")
        self.layer_var = tk.StringVar()
        self.layer_combo = ttk.Combobox(toolbar, textvariable=self.layer_var, state="readonly", width=12)
        self.layer_combo.pack(side="left", padx=4)
        self.layer_combo.bind("<<ComboboxSelected>>", lambda _e: self.layer_changed(self.layer_var.get()))
        self.overlay_var = tk.BooleanVar(value=False)
        self.overlay_check = ttk.Checkbutton(
            toolbar, text="多层叠加", variable=self.overlay_var, command=self.redraw
        )
        self.overlay_check.pack(side="left", padx=3)
        self.local_var = tk.BooleanVar(value=True)
        self.local_check = ttk.Checkbutton(
            toolbar, text="局部视图", variable=self.local_var, command=self.fit
        )
        self.local_check.pack(side="left", padx=3)
        controls = ttk.Frame(self)
        self.controls = controls
        controls.pack(fill="x", pady=(0, 3))
        navigation = ttk.Frame(controls)
        self.navigation = navigation
        navigation.pack(side="left")
        for text, command in (("适配", self.fit), ("＋", lambda: self.change_zoom(1.25)),
                              ("－", lambda: self.change_zoom(0.8)), ("复位", self.reset)):
            ttk.Button(navigation, text=text, width=5, command=command).pack(side="left", padx=2)
        option_checks = ttk.Frame(controls)
        self.option_checks = option_checks
        option_checks.pack(side="right")
        self.show_signal = tk.BooleanVar(value=True)
        self.show_antipad = tk.BooleanVar(value=True)
        self.show_dimensions = tk.BooleanVar(value=True)
        self.signal_check = ttk.Checkbutton(
            option_checks, text="信号铜", variable=self.show_signal, command=self.redraw)
        self.antipad_check = ttk.Checkbutton(
            option_checks, text="反焊盘", variable=self.show_antipad, command=self.redraw)
        self.dimension_check = ttk.Checkbutton(
            option_checks, text="尺寸标注", variable=self.show_dimensions, command=self.redraw)
        for check in (self.signal_check, self.antipad_check, self.dimension_check):
            check.pack(side="left", padx=3)
        self.local_hint = ttk.Label(
            self, text="取消“局部视图”可查看完整参考平面", foreground="#64748b")
        self.local_hint.pack(fill="x", padx=4, pady=(0, 3))
        self.canvas = tk.Canvas(self, background="#f8fafc", highlightthickness=1, highlightbackground="#cbd5e1")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<Configure>", lambda _event: self._apply_toolbar_layout(), add="+")

    def _apply_toolbar_layout(self) -> None:
        compact = self.winfo_width() < 590
        self.layer_label.configure(text="层" if compact else "预览层")
        self.layer_combo.configure(width=7 if compact else 12)
        self.overlay_check.configure(text="叠加" if compact else "多层叠加")
        self.local_check.configure(text="局部" if compact else "局部视图")
        self.signal_check.configure(text="铜" if compact else "信号铜")
        self.antipad_check.configure(text="反焊盘" if compact else "反焊盘")
        self.dimension_check.configure(text="标注" if compact else "尺寸标注")
        if compact:
            if self.local_hint.winfo_manager():
                self.local_hint.pack_forget()
        elif not self.local_hint.winfo_manager():
            self.local_hint.pack(fill="x", padx=4, pady=(0, 3), before=self.canvas)

    def set_active_parameter(self, path: Optional[str]) -> None:
        """Highlight the parameter currently being explained by the editor."""
        self.active_parameter = path
        if self._via_mode:
            self.redraw()

    def set_data(self, config: Mapping[str, Any], geometry: Any, unit: str, layer_name: str) -> None:
        self.config, self.geometry, self.unit = config, geometry, unit
        self._via_mode = False
        names = [str(layer["name"]) for layer in config["stackup"]]
        self.layer_combo.configure(values=names)
        self.layer_var.set(layer_name if layer_name in names else names[0])
        self.redraw()

    def set_via_data(self, config: Mapping[str, Any], geometry: Any, unit: str, layer_name: str) -> None:
        self.config, self.geometry, self.unit = config, geometry, unit
        self._via_mode = True
        names = [str(layer["name"]) for layer in config["stackup"]]
        self.layer_combo.configure(values=names)
        self.layer_var.set(layer_name if layer_name in names else names[0])
        self.redraw()

    def change_zoom(self, factor: float) -> None:
        self.zoom = min(8.0, max(0.25, self.zoom * factor))
        self.redraw()

    def fit(self) -> None:
        self.zoom = 1.0
        self.redraw()

    def reset(self) -> None:
        self.zoom = 1.0
        self.local_var.set(True)
        self.overlay_var.set(False)
        self.show_signal.set(True)
        self.show_antipad.set(True)
        self.show_dimensions.set(True)
        self.redraw()

    def _view_bounds(self) -> Tuple[float, float, float, float]:
        if self._via_mode:
            if not self.local_var.get():
                b = self.geometry.plane_bounds
                return b.x_min, b.x_max, b.y_min, b.y_max
            xs: List[float] = []
            ys: List[float] = []
            visible_layers = ({self.layer_var.get()} if not self.overlay_var.get() else
                              {str(item.layer) for item in getattr(self.geometry, "antipad_contours", [])})
            for contour in getattr(self.geometry, "antipad_contours", []):
                if contour.layer not in visible_layers:
                    continue
                for x, y in contour.points:
                    xs.append(float(x)); ys.append(float(y))
            for via in getattr(self.geometry, "signal_vias", []):
                radius = float(via.connected_pad_diameter) / 2.0
                xs.extend((float(via.x) - radius, float(via.x) + radius))
                ys.extend((float(via.y) - radius, float(via.y) + radius))
            for via in getattr(self.geometry, "return_vias", []):
                diameter = (float(via.pad_diameter) if self.layer_var.get() in via.connect_layers
                            else float(via.antipad_diameter) if self.layer_var.get() in via.clearance_layers
                            else float(via.drill_diameter))
                radius = diameter / 2.0
                xs.extend((float(via.x) - radius, float(via.x) + radius))
                ys.extend((float(via.y) - radius, float(via.y) + radius))
            if not xs:
                return (-1.0, 1.0, -1.0, 1.0)
            margin = max(0.2, (max(xs) - min(xs) + max(ys) - min(ys)) * 0.08)
            return min(xs)-margin, max(xs)+margin, min(ys)-margin, max(ys)+margin
        if not self.local_var.get():
            b = self.geometry.plane_bounds
            return b.x_min, b.x_max, b.y_min, b.y_max
        points = [point for shape in self.geometry.pad_shapes.values() for point in shape.points]
        for outline in self.geometry.body_outlines.values():
            points.extend(outline)
        for tools in self.geometry.antipads_by_layer.values():
            for shape in tools:
                points.extend(shape.points)
        x_values, y_values = [p.x for p in points], [p.y for p in points]
        margin = max(float(self.config["antipad"]["global_expansion"]), 0.25)
        return min(x_values) - margin, max(x_values) + margin, min(y_values) - margin, max(y_values) + margin

    def redraw(self) -> None:
        canvas = self.canvas
        canvas.delete("all")
        if self.geometry is None or self.config is None:
            return
        if self._via_mode:
            self._redraw_via()
            return
        viewport = canvas_viewport(canvas)
        if viewport is None:
            return
        width, height = viewport
        title_font = ("TkDefaultFont", 10, "bold")
        title_line = font_linespace(canvas, title_font)
        layer_name = self.layer_var.get()
        mode = MODE_LABELS.get(str(self.config["antipad"]["mode"]), str(self.config["antipad"]["mode"]))
        title_text = ellipsize(canvas, f"{layer_name} · {mode} · {self.unit}", title_font, width - 24)
        if width < 180 or height < max(150, title_line * 5):
            if height < title_line + 4:
                if height >= 16 and width >= 80:
                    canvas.create_text(width / 2, height / 2, text="预览区域过小",
                                       fill="#475569", font=("TkDefaultFont", 8),
                                       tags=("hud-label", "compact-preview"))
                return
            if height < title_line * 2 + 8:
                compact_lines = (ellipsize(canvas, "预览视口过小", title_font, width - 16),)
            else:
                compact_lines = (
                    title_text,
                    ellipsize(canvas, "拖动上下窗格查看完整预览", title_font, width - 16),
                )
            first_y = height / 2 - (len(compact_lines)-1) * title_line / 2
            for index, line in enumerate(compact_lines):
                canvas.create_text(
                    width / 2, first_y + index * title_line, text=line,
                    fill="#0f172a", font=title_font,
                    tags=("hud-label", "compact-preview"),
                )
            return
        x_min, x_max, y_min, y_max = self._view_bounds()
        span_x, span_y = max(x_max - x_min, 1e-9), max(y_max - y_min, 1e-9)
        plot_top = 12 + title_line + 8
        plot_bottom = height - 58
        scale = min(max(1, width - 55) / span_x, max(1, plot_bottom - plot_top) / span_y) * self.zoom
        center_x, center_y = (x_min + x_max) / 2, (y_min + y_max) / 2
        ox, oy = width / 2 - center_x * scale, (plot_top + plot_bottom) / 2 + center_y * scale

        def xy(x: float, y: float) -> Tuple[float, float]:
            return ox + x * scale, oy - y * scale

        plane = self.geometry.plane_bounds
        px1, py1 = xy(plane.x_min, plane.y_max)
        px2, py2 = xy(plane.x_max, plane.y_min)
        canvas.create_rectangle(px1, py1, px2, py2, fill="#e8f1ec", outline="#64748b", width=2)
        if self.show_antipad.get():
            if self.overlay_var.get():
                palette = ("#8b5cf6", "#0891b2", "#65a30d", "#db2777")
                color_index = 0
                for other_name, tools in self.geometry.antipads_by_layer.items():
                    if other_name == layer_name:
                        continue
                    for shape in tools:
                        canvas.create_polygon([coordinate for point in shape.points for coordinate in xy(point.x, point.y)],
                                              fill="", outline=palette[color_index % len(palette)], dash=(5, 3), width=2)
                    color_index += 1
            for shape in self.geometry.antipads_by_layer.get(layer_name, []):
                outside = not plane.contains(shape.bounds)
                canvas.create_polygon([coordinate for point in shape.points for coordinate in xy(point.x, point.y)],
                                      fill="#ffffff", outline="#dc2626" if outside else "#f97316", width=3)
        colors, pad_colors = {"P": "#ef6b6b", "N": "#6b93ef"}, {"P": "#b91c1c", "N": "#1d4ed8"}
        if self.show_signal.get():
            for signal in ("P", "N"):
                for shape in self.geometry.signal_shapes[signal]:
                    if shape.category in {"pad", "bridge"}:
                        continue
                    canvas.create_polygon([coordinate for point in shape.points for coordinate in xy(point.x, point.y)],
                                          fill=colors[signal], outline=colors[signal])
            for shape in self.geometry.pad_shapes.values():
                canvas.create_polygon([coordinate for point in shape.points for coordinate in xy(point.x, point.y)],
                                      fill=pad_colors[shape.signal], outline="#1f2937", width=2)
        placer = CanvasLabelPlacer(canvas, width, height)
        if self.show_signal.get():
            for shape in self.geometry.pad_shapes.values():
                xs = [xy(point.x, point.y)[0] for point in shape.points]
                ys = [xy(point.x, point.y)[1] for point in shape.points]
                if xs and ys:
                    placer.reserve((min(xs), min(ys), max(xs), max(ys)))
        for name, outline in self.geometry.body_outlines.items():
            canvas.create_polygon([coordinate for point in outline for coordinate in xy(point.x, point.y)],
                                  fill="", outline="#475569", dash=(4, 3), width=2)
            xs = [point.x for point in outline]
            ys = [point.y for point in outline]
            top_x = (min(xs) + max(xs)) / 2.0
            top_y = max(ys)
            tx, ty = xy(top_x, top_y)
            placer.place(
                name.replace("C_", "") + " 电容",
                ((tx, ty - 16), (tx, ty - 32), (tx + 48, ty - 16), (tx - 48, ty - 16)),
                fill="#334155", background="#f8fafc", outline="#cbd5e1",
                tags=("object-label",),
            )
        if self.show_dimensions.get():
            tools = self.geometry.antipads_by_layer.get(layer_name, [])
            if tools:
                tool = tools[0]
                source = [self.geometry.pad_shapes[key] for key in tool.metadata.get("source_pads", [])]
                if source:
                    source_max = max(item.bounds.x_max for item in source)
                    y = tool.bounds.center.y
                    ax1, ay = xy(source_max, y)
                    ax2, _ = xy(tool.bounds.x_max, y)
                    canvas.create_line(ax1, ay, ax2, ay, arrow="both", fill="#ea580c", width=2)
                    expansion_text = (
                        f"E={format_length(float(tool.metadata['expansion']), self.unit)} {self.unit}"
                    )
                    placer.place(
                        expansion_text,
                        (((ax1 + ax2) / 2.0, ay - 16),
                         ((ax1 + ax2) / 2.0, ay - 32),
                         (ax2 + 24, ay - 16),
                         (ax1 - 24, ay - 16)),
                        fill="#c2410c", background="#fff7ed", outline="#fdba74",
                        tags=("dimension-label",), force=True,
                    )
            if not self.local_var.get():
                model = self.geometry.model_bounds
                labels = ((model.x_min, plane.x_min, plane.y_min, "输入余量"),
                          (model.x_max, plane.x_max, plane.y_min, "输出余量"),
                          (model.y_max, plane.y_max, plane.x_max, "Y 余量"))
                for first, second, fixed, label in labels:
                    if label == "Y 余量":
                        x, y1 = xy(fixed, first); _, y2 = xy(fixed, second)
                        canvas.create_line(x - 15, y1, x - 15, y2, arrow="both", fill="#7c3aed")
                        placer.place(
                            label,
                            ((x - 28, (y1 + y2) / 2.0), (x - 44, (y1 + y2) / 2.0)),
                            fill="#7c3aed", background="#f5f3ff",
                            tags=("dimension-label",),
                        )
                    else:
                        x1, y = xy(first, fixed); x2, _ = xy(second, fixed)
                        canvas.create_line(x1, y + 16, x2, y + 16, arrow="both", fill="#7c3aed")
                        placer.place(
                            label,
                            (((x1 + x2) / 2.0, y + 30), ((x1 + x2) / 2.0, y + 44)),
                            fill="#7c3aed", background="#f5f3ff",
                            tags=("dimension-label",),
                        )
        axis_x, axis_y = 34, height - 28
        canvas.create_line(axis_x, axis_y, axis_x + 42, axis_y, arrow="last", fill="#334155", width=2)
        canvas.create_line(axis_x, axis_y, axis_x, axis_y - 42, arrow="last", fill="#334155", width=2)
        canvas.create_text(min(width-8, axis_x + 48), axis_y, text="X", fill="#334155", tags=("axis-label",))
        canvas.create_text(axis_x, max(8, axis_y - 49), text="Y", fill="#334155", tags=("axis-label",))
        canvas.create_text(12, 12, anchor="nw", text=title_text,
                           fill="#0f172a", font=title_font, tags=("hud-label",))
        raise_canvas_labels(canvas)

    def _redraw_via(self) -> None:
        canvas = self.canvas
        viewport = canvas_viewport(canvas)
        if viewport is None or self.geometry is None or self.config is None:
            return
        width, height = viewport
        if height < 125 or width < 260:
            if height >= 16 and width >= 80:
                layer_name = self.layer_var.get()
                contours = [
                    item for item in getattr(self.geometry, "antipad_contours", [])
                    if item.layer == layer_name
                ]
                first = contours[0] if contours else None
                size = (
                    f"Ø{format_length(first.diameter, self.unit)}"
                    if first is not None and first.shape == "circle"
                    else f"{format_length(first.length, self.unit)}×{format_length(first.width, self.unit)}"
                    if first is not None else "—"
                )
                line_height = font_linespace(canvas, ("TkDefaultFont", 9, "bold"))
                lines = [f"{layer_name} · 信号开窗 {len(contours)} · {size} {self.unit}"]
                if height >= line_height * 2 + 12:
                    lines.append("拖动上下分隔条可展开同步参考图")
                first_y = height / 2 - (len(lines) - 1) * line_height / 2
                for index, line in enumerate(lines):
                    canvas.create_text(
                        width / 2, first_y + index * line_height,
                        text=ellipsize(canvas, line, ("TkDefaultFont", 9, "bold"), width - 16),
                        fill="#334155", font=("TkDefaultFont", 9, "bold"),
                        tags=("hud-label", "compact-preview"),
                    )
            return
        layer_name = self.layer_var.get()
        contours = [item for item in getattr(self.geometry, "antipad_contours", [])
                    if item.layer == layer_name]
        active = str(self.active_parameter or "")
        active_field = (
            "return_diameter" if active == "via_transition.return_vias.antipad_diameter"
            else active.rsplit(".", 1)[-1] if active else ""
        )
        x_min, x_max, y_min, y_max = self._view_bounds()
        title_height = font_linespace(canvas, ("TkDefaultFont", 10, "bold"))
        plot_top, plot_bottom = 12 + title_height + 8, height - 42
        show_parameter_card = width >= 640 and height >= 250
        parameter_card_width = min(270, max(215, int(width * 0.30))) if show_parameter_card else 0
        plot_left = 22
        plot_right = width - parameter_card_width - (24 if show_parameter_card else 22)
        scale = min(max(1, plot_right - plot_left) / max(x_max-x_min, 1e-9),
                    max(1, plot_bottom-plot_top) / max(y_max-y_min, 1e-9)) * self.zoom
        ox = (plot_left + plot_right) / 2 - (x_min+x_max) / 2 * scale
        oy = (plot_top+plot_bottom) / 2 + (y_min+y_max) / 2 * scale

        def xy(x: float, y: float) -> Tuple[float, float]:
            return ox + x*scale, oy - y*scale

        plane = self.geometry.plane_bounds
        px1, py1 = xy(plane.x_min, plane.y_max); px2, py2 = xy(plane.x_max, plane.y_min)
        canvas.create_rectangle(px1, py1, px2, py2, fill="#e8f1ec", outline="#64748b", width=2)
        if self.show_antipad.get():
            if self.overlay_var.get():
                palette = ("#7c3aed", "#0891b2", "#65a30d", "#db2777")
                legend_y = plot_top + 4
                color_index = 0
                for other_layer in sorted({item.layer for item in getattr(self.geometry, "antipad_contours", [])
                                           if item.layer != layer_name}):
                    color = palette[color_index % len(palette)]
                    for contour in (item for item in getattr(self.geometry, "antipad_contours", [])
                                    if item.layer == other_layer):
                        canvas.create_polygon(
                            [coordinate for point in contour.points for coordinate in xy(*point)],
                            fill="", outline=color, dash=(5, 3), width=2,
                            tags=("via-layer-overlay",))
                    canvas.create_text(plot_right - 4, legend_y, anchor="ne",
                                       text=f"{other_layer} 叠加轮廓", fill=color,
                                       tags=("via-legend",))
                    legend_y += 16
                    color_index += 1
            for contour in contours:
                signal_vias = list(getattr(self.geometry, "signal_vias", []))
                def required_radius(via: Any) -> float:
                    diameter = float(contour.effective_hole_diameter or via.drill_diameter)
                    if (getattr(via, "backdrilled", False)
                            and via.per_layer_states.get(layer_name) == "backdrilled_removed"):
                        diameter = max(diameter, float(via.backdrill_diameter))
                    return diameter / 2.0 + float(contour.clearance)
                illegal = bool(signal_vias) and not all(
                    contains_circle(contour, (float(via.x), float(via.y)), required_radius(via))
                    for via in signal_vias if contour.owner in {"merged_pair", via.signal}
                )
                outline_active = active_field in {
                    "topology", "shape", "size_mode", "corner_radius",
                    "length_increment", "width_increment",
                }
                canvas.create_polygon(
                    [coordinate for point in contour.points for coordinate in xy(*point)],
                    fill="#fecaca" if illegal else "#ffffff",
                    stipple="gray50" if illegal else "",
                    outline="#dc2626" if illegal else "#2563eb" if outline_active else "#f97316",
                    width=4 if outline_active and not illegal else 3,
                    tags=("via-antipad", "via-antipad:" + contour.owner))
                bx1, bx2, by1, by2 = contour.bounds
                sx1, sy1 = xy(bx1, by2); sx2, sy2 = xy(bx2, by1)
                canvas.create_rectangle(sx1, sy1, sx2, sy2, outline="#64748b",
                                        dash=(3, 3), tags=("via-bounds",))
                if contour.shape != "circle" and contour.required_length > 0 and contour.required_width > 0:
                    minimum = make_contour(layer_name, "minimum", {
                        "topology": contour.topology, "shape": contour.shape,
                        "diameter": contour.diameter,
                        "length": contour.required_length, "width": contour.required_width,
                        "corner_radius": min(contour.corner_radius, contour.required_width / 2.0),
                        "rotation_deg": contour.rotation_deg, "clearance": contour.clearance,
                    }, (contour.center_x, contour.center_y), "derived")
                    minimum_active = active_field in {
                        "size_mode", "clearance", "end_clearance", "side_clearance",
                        "length_increment", "width_increment",
                    }
                    canvas.create_polygon(
                        [coordinate for point in minimum.points for coordinate in xy(*point)],
                        fill="", outline="#0284c7", dash=(7, 3),
                        width=3 if minimum_active else 2,
                        tags=("via-minimum",))
            # A clearance input can be validation-only when manual dimensions
            # own the actual contour.  Draw the hole + clearance envelope so
            # that changing it still produces an honest visible response.
            if contours and self.show_dimensions.get():
                for via in getattr(self.geometry, "signal_vias", []):
                    contour = next((item for item in contours
                                    if item.owner in {"merged_pair", via.signal}), None)
                    if contour is None:
                        continue
                    effective_hole = float(contour.effective_hole_diameter or via.drill_diameter)
                    if (getattr(via, "backdrilled", False)
                            and via.per_layer_states.get(layer_name) == "backdrilled_removed"):
                        effective_hole = max(effective_hole, float(via.backdrill_diameter))
                    radius = (effective_hole / 2.0 + float(contour.clearance)) * scale
                    cx, cy = xy(float(via.x), float(via.y))
                    envelope_active = active_field in {
                        "clearance", "end_clearance", "side_clearance",
                    }
                    canvas.create_oval(
                        cx-radius, cy-radius, cx+radius, cy+radius,
                        fill="", outline="#0284c7" if envelope_active else "#38bdf8",
                        dash=(4, 3), width=3 if envelope_active else 2,
                        tags=("via-clearance-envelope", "via-dimension:clearance"),
                    )
        if self.show_signal.get():
            for via in getattr(self.geometry, "signal_vias", []):
                cx, cy = xy(float(via.x), float(via.y))
                pad_r = float(via.connected_pad_diameter)/2*scale
                drill_r = float(via.drill_diameter)/2*scale
                canvas.create_oval(cx-pad_r, cy-pad_r, cx+pad_r, cy+pad_r,
                                   fill="#b45309", outline="#78350f")
                canvas.create_oval(cx-drill_r, cy-drill_r, cx+drill_r, cy+drill_r,
                                   fill="#1f2937", outline="#020617")
                canvas.create_text(cx + pad_r + 8, cy - pad_r - 8, text=via.signal,
                                   fill="#0f172a", font=("TkDefaultFont", 9, "bold"),
                                   tags=("via-signal-label",))
            for index, via in enumerate(getattr(self.geometry, "return_vias", []), 1):
                cx, cy = xy(float(via.x), float(via.y))
                drill = float(via.drill_diameter) / 2.0 * scale
                if layer_name in via.connect_layers:
                    radius = float(via.pad_diameter) / 2.0 * scale
                    canvas.create_oval(cx-radius, cy-radius, cx+radius, cy+radius,
                                       fill="#a7f3d0", outline="#047857", width=2,
                                       tags=("return-pad",))
                elif layer_name in via.clearance_layers:
                    radius = float(via.antipad_diameter) / 2.0 * scale
                    canvas.create_oval(cx-radius, cy-radius, cx+radius, cy+radius,
                                       fill="#ecfdf5",
                                       outline="#2563eb" if active_field == "return_diameter" else "#047857",
                                       dash=(5, 3), width=3 if active_field == "return_diameter" else 2,
                                       tags=("return-clearance",))
                else:
                    radius = drill
                canvas.create_oval(cx-drill, cy-drill, cx+drill, cy+drill,
                                   fill="#334155", outline="#0f172a", tags=("return-drill",))
                canvas.create_text(cx + radius + 5, cy - radius - 4, anchor="sw",
                                   text=f"RV{index:02d}", fill="#065f46",
                                   tags=("return-label",))
        if self.show_dimensions.get() and contours:
            contour = contours[0]

            def active_style(field: str, normal_color: str) -> Tuple[str, int]:
                selected = active.endswith("." + field)
                return ("#2563eb" if selected else normal_color, 3 if selected else 2)

            def bind_dimension(field: str) -> None:
                if self.navigate is not None:
                    path = "via_transition.padstack.antipad." + field
                    canvas.tag_bind(
                        "via-dimension:" + field, "<Button-1>",
                        lambda _event, target=path: self.navigate(target),
                    )

            gap_world = 20.0 / max(scale, 1.0)
            label_gap_world = 32.0 / max(scale, 1.0)
            if contour.shape == "circle":
                dimension_contour = min(contours, key=lambda item: float(item.center_y))
                cx = float(dimension_contour.center_x)
                annotation_y = min(float(item.bounds[2]) for item in contours)
                for via in getattr(self.geometry, "signal_vias", []):
                    annotation_y = min(
                        annotation_y,
                        float(via.y) - float(via.connected_pad_diameter) / 2.0,
                    )
                for via in getattr(self.geometry, "return_vias", []):
                    diameter = (
                        float(via.pad_diameter) if layer_name in via.connect_layers
                        else float(via.antipad_diameter) if layer_name in via.clearance_layers
                        else float(via.drill_diameter)
                    )
                    annotation_y = min(annotation_y, float(via.y) - diameter / 2.0)
                annotation_y -= gap_world
                start = xy(cx - dimension_contour.diameter / 2.0, annotation_y)
                stop = xy(cx + dimension_contour.diameter / 2.0, annotation_y)
                color, line_width = active_style("diameter", "#c2410c")
                tags = ("via-dimension", "via-dimension:diameter")
                canvas.create_line(*start, *stop, arrow="both", fill=color,
                                   width=line_width, tags=tags)
                canvas.create_text(
                    plot_left + 8, plot_bottom - 8, anchor="sw",
                    text=f"直径 {format_length(dimension_contour.diameter, self.unit)} {self.unit}",
                    fill=color, tags=tags,
                )
                bind_dimension("diameter")
            else:
                theta = math.radians(float(contour.rotation_deg))
                local_x = (math.cos(theta), math.sin(theta))
                local_y = (-math.sin(theta), math.cos(theta))
                center = (float(contour.center_x), float(contour.center_y))

                def point(axis: Tuple[float, float], distance: float,
                          offset_axis: Tuple[float, float], offset: float) -> Tuple[float, float]:
                    return (
                        center[0] + axis[0] * distance + offset_axis[0] * offset,
                        center[1] + axis[1] * distance + offset_axis[1] * offset,
                    )

                length_offset = contour.width / 2.0 + gap_world
                length_start = xy(*point(local_x, -contour.length / 2.0, local_y, length_offset))
                length_stop = xy(*point(local_x, contour.length / 2.0, local_y, length_offset))
                length_color, length_width = active_style("length", "#c2410c")
                length_tags = ("via-dimension", "via-dimension:length")
                canvas.create_line(*length_start, *length_stop, arrow="both", fill=length_color,
                                   width=length_width, tags=length_tags)
                length_label = xy(*point(local_x, 0.0, local_y, length_offset + label_gap_world))
                canvas.create_text(
                    *length_label,
                    text=f"长度 {format_length(contour.length, self.unit)} {self.unit}",
                    fill=length_color, tags=length_tags,
                )

                width_offset = contour.length / 2.0 + gap_world
                width_start = xy(*point(local_y, -contour.width / 2.0, local_x, width_offset))
                width_stop = xy(*point(local_y, contour.width / 2.0, local_x, width_offset))
                width_color, width_line = active_style("width", "#7c3aed")
                width_tags = ("via-dimension", "via-dimension:width")
                canvas.create_line(*width_start, *width_stop, arrow="both", fill=width_color,
                                   width=width_line, tags=width_tags)
                width_label = xy(*point(local_y, 0.0, local_x, width_offset + label_gap_world))
                canvas.create_text(
                    *width_label,
                    text=f"宽度 {format_length(contour.width, self.unit)} {self.unit}",
                    fill=width_color, tags=width_tags,
                )
                bind_dimension("length")
                bind_dimension("width")
            if contour.shape != "circle" and active_field in {"direction_mode", "rotation_deg"}:
                theta = math.radians(float(contour.rotation_deg))
                axis_length = max(0.05, min(float(contour.length), float(contour.width)) * 0.42)
                cx, cy = float(contour.center_x), float(contour.center_y)
                x_stop = xy(cx + math.cos(theta) * axis_length,
                            cy + math.sin(theta) * axis_length)
                y_stop = xy(cx - math.sin(theta) * axis_length,
                            cy + math.cos(theta) * axis_length)
                origin = xy(cx, cy)
                canvas.create_line(*origin, *x_stop, arrow="last", fill="#2563eb", width=3,
                                   tags=("via-direction-axis",))
                canvas.create_line(*origin, *y_stop, arrow="last", fill="#7c3aed", width=2,
                                   tags=("via-direction-axis",))
                canvas.create_text(
                    x_stop[0] + 5, x_stop[1] - 5, anchor="sw",
                    text=f"X / {contour.rotation_deg:g}°", fill="#2563eb",
                    tags=("via-direction-axis",),
                )
        if self.show_dimensions.get() and active_field == "return_diameter":
            return_via = next((item for item in getattr(self.geometry, "return_vias", [])
                               if layer_name in item.clearance_layers), None)
            if return_via is not None:
                diameter = float(return_via.antipad_diameter)
                cy_world = float(return_via.y) - diameter / 2.0 - 18.0 / max(scale, 1.0)
                start = xy(float(return_via.x) - diameter / 2.0, cy_world)
                stop = xy(float(return_via.x) + diameter / 2.0, cy_world)
                tags = ("via-dimension", "via-dimension:return_diameter")
                canvas.create_line(*start, *stop, arrow="both", fill="#2563eb", width=3, tags=tags)
                canvas.create_text(
                    (start[0] + stop[0]) / 2.0, start[1] - 10,
                    text=f"Return 避空 Ø{format_length(diameter, self.unit)} {self.unit}",
                    fill="#2563eb", tags=tags,
                )
        title = (f"信号孔：{len(getattr(self.geometry, 'signal_vias', []))}；"
                 f"信号开窗：{len(contours)}"
                 f"{'（合并）' if contours and contours[0].topology == 'merged_pair' else ''}；"
                 f"回流孔：{len(getattr(self.geometry, 'return_vias', []))}；"
                 f"回流避空：{sum(1 for item in getattr(self.geometry, 'return_vias', []) if layer_name in item.clearance_layers)}；"
                 f"当前层：{layer_name} · {self.unit}")
        title = ellipsize(canvas, title, ("TkDefaultFont", 10, "bold"), max(80, plot_right - 20))
        canvas.create_text(12, 12, anchor="nw", text=title, fill="#0f172a",
                           font=("TkDefaultFont", 10, "bold"), tags=("hud-label",))
        if show_parameter_card:
            antipad = self.config.get("via_transition", {}).get("padstack", {}).get("antipad", {})
            effective, source, enabled = effective_antipad(antipad, layer_name)
            card_x1, card_x2 = plot_right + 10, width - 10
            card_y1, card_y2 = plot_top, plot_bottom
            canvas.create_rectangle(
                card_x1, card_y1, card_x2, card_y2,
                fill="#ffffff", outline="#cbd5e1", width=1,
                tags=("via-parameter-card",),
            )
            source_text = "本层覆盖" if source == "per_layer" else "全局继承"
            card_lines = [
                f"当前层有效参数 · {source_text}",
                f"{VIA_TOPOLOGY_LABELS.get(str(effective.get('topology')), effective.get('topology'))} · "
                f"{VIA_SHAPE_LABELS.get(str(effective.get('shape')), effective.get('shape'))}",
            ]
            if not enabled or not contours:
                card_lines.append("本层不生成 Signal VIA 反焊盘")
            else:
                first = contours[0]
                if first.topology == "merged_pair":
                    card_lines.append(
                        f"{VIA_DIRECTION_LABELS.get(first.direction_mode, first.direction_mode)} · "
                        f"{VIA_SIZE_MODE_LABELS.get(first.size_mode, first.size_mode)}"
                    )
                else:
                    card_lines.append("每个孔心独立定位 · 手动尺寸")
                if first.shape == "circle":
                    card_lines.append(f"图中有效直径：{format_length(first.diameter, self.unit)} {self.unit}")
                else:
                    card_lines.append(
                        f"图中有效 L×W：{format_length(first.length, self.unit)} × "
                        f"{format_length(first.width, self.unit)} {self.unit}"
                    )
                    card_lines.append(f"图中有效角度：{first.rotation_deg:g}°")
                    if first.shape == "rounded_rectangle":
                        card_lines.append(
                            f"圆角半径：{format_length(first.corner_radius, self.unit)} {self.unit}"
                        )
                card_lines.append(
                    f"制造最小净距：{format_length(first.clearance, self.unit)} {self.unit}"
                )
                derived = []
                if first.topology == "merged_pair" and first.size_mode in {"auto_fit", "auto_plus_increment"}:
                    derived.append("尺寸")
                if first.topology == "merged_pair" and first.direction_mode == "auto_pn" and first.shape != "circle":
                    derived.append("角度")
                if derived:
                    card_lines.append("提示：" + "、".join(derived) + "由规则计算")
            if active_field in VIA_PARAMETER_TITLES:
                card_lines.append("正在设置：" + VIA_PARAMETER_TITLES[active_field])
                visual_hint = VIA_PARAMETER_VISUAL_HINTS.get(active_field)
                if visual_hint:
                    card_lines.append("图中对应：" + visual_hint)
            canvas.create_text(
                card_x1 + 10, card_y1 + 9, anchor="nw",
                text="\n".join(card_lines), fill="#334155",
                font=("TkDefaultFont", 9), justify="left",
                width=max(80, card_x2 - card_x1 - 20),
                tags=("via-parameter-card", "hud-label"),
            )
        legend = (
            "正在设置：" + VIA_PARAMETER_TITLES[active_field] + " · "
            + VIA_PARAMETER_VISUAL_HINTS.get(active_field, "参考图已同步到当前有效值")
            if active_field in VIA_PARAMETER_TITLES and not show_parameter_card
            else "橙/红实线：真实开窗 · 浅蓝圆：孔＋净距包络 · 蓝虚线：最小合格轮廓 · 红底：非法"
        )
        canvas.create_text(12, height-10, anchor="sw",
                           text=ellipsize(canvas, legend, ("TkDefaultFont", 9), width - 24),
                           fill="#2563eb" if active_field and not show_parameter_card else "#475569",
                           tags=("via-legend",))

    def show_error(self, message: str) -> None:
        self.canvas.delete("all")
        viewport = canvas_viewport(self.canvas)
        if viewport is None:
            return
        width, _height = viewport
        self.canvas.create_text(20, 20, anchor="nw", fill="#b42318", width=max(1, width-40),
                                text="预览参数暂不可用\n" + message, tags=("hud-label",))


class StackupCrossSection(ttk.Frame):
    def __init__(self, master: tk.Misc, selected: Callable[[int], None]) -> None:
        super().__init__(master)
        self.selected_callback = selected
        self.geometry: Any = None
        self.unit = DEFAULT_DISPLAY_UNIT
        self.selected_index = 0
        self.hit_regions: List[Tuple[float, float, int]] = []
        self._via_mode = False
        self._via_geometry: Any = None
        self._via_validation: Any = None
        self.canvas = tk.Canvas(self, background="#f8fafc", highlightthickness=1, highlightbackground="#cbd5e1")
        self.vertical_scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.horizontal_scroll = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self.vertical_scroll.set, xscrollcommand=self.horizontal_scroll.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vertical_scroll.grid(row=0, column=1, sticky="ns")
        self.horizontal_scroll.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.canvas.bind("<Configure>", lambda _e: self.winfo_toplevel().after_idle(self.redraw))
        self.canvas.bind("<Button-1>", self._clicked)
        self.canvas.bind("<MouseWheel>", self._wheel)

    def set_data(self, geometry: Any, unit: str, selected_index: int) -> None:
        changed = selected_index != self.selected_index
        self.geometry, self.unit, self.selected_index = geometry, unit, selected_index
        self._via_mode = False
        self._via_geometry = None
        self._via_validation = None
        self.redraw()
        if changed:
            self.winfo_toplevel().after_idle(self._see_selected)

    def set_via_data(self, geometry: Any, unit: str, selected_index: Optional[int] = None,
                     validation: Any = None) -> None:
        """Render the via_transition antipad cross-section over the stackup Z axis."""
        self._via_mode = True
        self._via_geometry = geometry
        self._via_validation = validation
        self.geometry = geometry
        self.unit = unit
        if selected_index is not None:
            self.selected_index = selected_index
        self.redraw()

    def _wheel(self, event: tk.Event) -> str:
        self.canvas.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def _clicked(self, event: tk.Event) -> None:
        content_y = self.canvas.canvasy(event.y)
        for top, bottom, index in self.hit_regions:
            if top <= content_y <= bottom:
                self.selected_callback(index)
                return

    def _see_selected(self) -> None:
        if not self.hit_regions or not (0 <= self.selected_index < len(self.hit_regions)):
            return
        top, bottom, _index = self.hit_regions[self.selected_index]
        region = self.canvas.cget("scrollregion")
        values = [float(value) for value in str(region).split()] if region else []
        if len(values) != 4:
            return
        total = max(1.0, values[3] - values[1])
        viewport = max(1.0, float(self.canvas.winfo_height()))
        current_top = float(self.canvas.canvasy(0))
        current_bottom = current_top + viewport
        if top < current_top or bottom > current_bottom:
            target = max(0.0, min(1.0, (top - viewport * .25) / max(1.0, total - viewport)))
            self.canvas.yview_moveto(target)

    def redraw(self) -> None:
        if self._via_mode:
            self._redraw_via()
            return
        canvas = self.canvas
        if self.geometry is None:
            return
        viewport = canvas_viewport(canvas)
        if viewport is None:
            return
        canvas.delete("all")
        self.hit_regions.clear()
        layers = self.geometry.stackup
        if not layers:
            return
        width, height = viewport
        pad = 12
        title_font = ("TkDefaultFont", 10, "bold")
        label_font = "TkDefaultFont"
        title_line = font_linespace(canvas, title_font)
        label_line = font_linespace(canvas, label_font)
        row_minimum = max(label_line * 2 + 8, 30)
        total = sum(layer.thickness for layer in layers)
        title_y = pad
        note_y = title_y + title_line + 3
        y = note_y + label_line + 9
        label_lines = []
        label_width = 0
        label_metric = resolve_font(canvas, label_font)
        for layer in layers:
            flags = ("  [参考]" if layer.is_reference_plane else "") + ("  [反焊盘]" if layer.enable_antipad else "")
            first = f"{layer.name} · {LAYER_TYPE_LABELS.get(layer.layer_type, layer.layer_type)}{flags}"
            second = f"{format_length(layer.thickness, self.unit)} {self.unit}"
            label_lines.append((first, second))
            label_width = max(label_width, label_metric.measure(first), label_metric.measure(second))
        geometry_width = max(110, min(260, int(width * .42)))
        x1, x2 = pad, pad + geometry_width
        label_x = x2 + 10
        content_width = max(width, label_x + label_width + pad + 6)
        readable_target = max(height - y - title_line - pad * 2, len(layers) * row_minimum)
        proportional_budget = max(readable_target, len(layers) * row_minimum * 1.25)
        heights = [max(float(row_minimum), proportional_budget * layer.thickness / max(total, 1e-12))
                   for layer in layers]
        colors = {"signal": "#d97706", "plane": "#a16207", "dielectric": "#a7d8f0", "soldermask": "#70b77e"}
        for index, (layer, layer_height, lines) in enumerate(zip(layers, heights, label_lines)):
            top, bottom = y, y + layer_height
            selected = index == self.selected_index
            canvas.create_rectangle(x1, top, x2, bottom, fill=colors.get(layer.layer_type, "#cbd5e1"),
                                    outline="#f97316" if selected else "#334155", width=4 if selected else 1,
                                    tags=("layer-row", f"stack-layer-{index}",
                                          "selected-layer" if selected else "unselected-layer"))
            canvas.create_text(label_x, (top + bottom) / 2, anchor="w",
                               text=f"{lines[0]}\n{lines[1]}", fill="#0f172a", font=label_font,
                               tags=("layer-label", f"stack-layer-{index}",
                                     "selected-layer-label" if selected else "unselected-layer-label"))
            self.hit_regions.append((top, bottom, index))
            y = bottom
        selected = layers[min(self.selected_index, len(layers) - 1)]
        canvas.create_text(pad, title_y, anchor="nw",
                           text=f"PCB 叠层截面 · 总厚度 {format_length(total, self.unit)} {self.unit}",
                           font=title_font, tags=("hud-label", "cross-title"))
        canvas.create_text(pad, note_y, anchor="nw",
                           text="显示高度经过可读性增强（非严格厚度比例）",
                           fill="#9a3412", font=label_font, tags=("hud-label", "scale-note"))
        footer_y = y + pad
        canvas.create_text(pad, footer_y, anchor="nw",
                           text=f"当前 {selected.name}：Z {format_length(selected.z_bottom, self.unit)} ～ "
                                f"{format_length(selected.z_top, self.unit)} {self.unit}",
                           fill="#c2410c", font=label_font,
                           tags=("footer-label", "selected-layer-footer"))
        content_height = footer_y + label_line + pad
        content_box = canvas.bbox("all")
        if content_box:
            content_width = max(content_width, content_box[2] + pad)
            content_height = max(content_height, content_box[3] + pad)
        canvas.configure(scrollregion=(0, 0, content_width, content_height))

    def _redraw_via(self) -> None:
        """Render the via_transition antipad cross-section (方案 B).

        Reuses the capacitor stackup Z-axis band and overlays, for each signal
        via, the drilled barrel (or effective barrel when back-drilled), the
        per-layer functional/non-functional pads and antipad clearances, the
        back-drill removed segment, plus the return vias.  No ``antipads_by_layer``
        / ``signal_shapes`` / ``gnd_vias`` attribute is ever touched here.
        """
        canvas = self.canvas
        geometry = self._via_geometry
        viewport = canvas_viewport(canvas)
        if viewport is None:
            return
        canvas.delete("all")
        self.hit_regions.clear()
        if geometry is None:
            canvas.create_text(12, 20, anchor="nw", text="暂无有效 VIA 几何。", fill="#94a3b8")
            return
        layers = list(getattr(geometry, "stackup", []))
        if not layers:
            canvas.create_text(12, 20, anchor="nw", text="暂无有效叠层。", fill="#94a3b8")
            return
        width, height = viewport
        pad = 12
        title_font = ("TkDefaultFont", 10, "bold")
        label_font = "TkDefaultFont"
        title_line = font_linespace(canvas, title_font)
        label_line = font_linespace(canvas, label_font)
        row_minimum = max(label_line * 2 + 8, 30)
        total = sum(float(layer.thickness) for layer in layers) or 1.0
        label_metric = resolve_font(canvas, label_font)
        label_lines: List[Tuple[str, str]] = []
        label_width = 0
        for layer in layers:
            flags = ("  [参考]" if layer.is_reference_plane else "") + ("  [反焊盘]" if layer.enable_antipad else "")
            first = f"{layer.name} · {LAYER_TYPE_LABELS.get(layer.layer_type, layer.layer_type)}{flags}"
            second = f"{format_length(layer.thickness, self.unit)} {self.unit}"
            label_lines.append((first, second))
            label_width = max(label_width, label_metric.measure(first), label_metric.measure(second))
        geometry_width = max(220, min(520, int(width * 0.60)))
        x1, x2 = pad, pad + geometry_width
        label_x = x2 + 12
        content_width = max(width, label_x + label_width + pad + 6)
        title_y = pad
        note_y = title_y + title_line + 3
        y = note_y + label_line + 9
        readable_target = max(height - y - title_line - pad * 2, len(layers) * row_minimum)
        proportional_budget = max(readable_target, len(layers) * row_minimum * 1.25)
        heights = [max(float(row_minimum), proportional_budget * float(layer.thickness) / total)
                   for layer in layers]
        colors = {"signal": "#d97706", "plane": "#a16207", "dielectric": "#a7d8f0", "soldermask": "#70b77e"}
        layer_boxes: List[Tuple[Any, float, float]] = []
        for index, (layer, layer_height, lines) in enumerate(zip(layers, heights, label_lines)):
            top, bottom = y, y + layer_height
            selected = index == self.selected_index
            canvas.create_rectangle(x1, top, x2, bottom, fill=colors.get(layer.layer_type, "#cbd5e1"),
                                    outline="#f97316" if selected else "#334155",
                                    width=4 if selected else 1,
                                    tags=("layer-row", f"stack-layer-{index}",
                                          "selected-layer" if selected else "unselected-layer"))
            canvas.create_text(label_x, (top + bottom) / 2, anchor="w",
                               text=f"{lines[0]}\n{lines[1]}", fill="#0f172a", font=label_font,
                               tags=("layer-label", f"stack-layer-{index}",
                                     "selected-layer-label" if selected else "unselected-layer-label"))
            self.hit_regions.append((top, bottom, index))
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

        signal_vias = list(getattr(geometry, "signal_vias", []))
        return_vias = list(getattr(geometry, "return_vias", []))
        center_y = (sum(float(via.y) for via in signal_vias) / len(signal_vias)) if signal_vias else 0.0
        all_ys = [float(via.y) for via in signal_vias] + [float(via.y) for via in return_vias]
        y_span = (max(all_ys) - min(all_ys)) if all_ys else 0.8
        max_anti = 0.2
        for contour in getattr(geometry, "antipad_contours", []):
            max_anti = max(max_anti, projected_width(contour, "y"))
        for via in return_vias:
            max_anti = max(max_anti, float(via.antipad_diameter or 0.0))
        lateral_span = max(y_span + max_anti, 0.5)
        px_per_mm = max(30.0, (geometry_width - 28) / lateral_span)
        center_px = (x1 + x2) / 2.0

        def vx(lateral_y: float) -> float:
            return center_px + (lateral_y - center_y) * px_per_mm

        def draw_barrel(cx: float, z_bottom: float, z_top: float, diameter: float,
                        hole_diameter: float, fill: str, outline: str, tags: Sequence[str] = ()) -> None:
            y_bottom = z_to_y(z_bottom)
            y_top = z_to_y(z_top)
            half = max(1.0, diameter * px_per_mm / 2.0)
            canvas.create_rectangle(cx - half, y_top, cx + half, y_bottom, fill=fill, outline=outline, tags=tuple(tags))
            if hole_diameter > 0.0 and hole_diameter < diameter:
                hole_half = max(0.5, hole_diameter * px_per_mm / 2.0)
                canvas.create_rectangle(cx - hole_half, y_top, cx + hole_half, y_bottom,
                                        fill="white", outline="#334155")

        def draw_layer_pad(cx: float, top: float, bottom: float, diameter: float,
                           fill: str, outline: str, tags: Sequence[str] = ()) -> None:
            half = max(1.0, diameter * px_per_mm / 2.0)
            canvas.create_rectangle(cx - half, top + 1, cx + half, bottom - 1, fill=fill,
                                    outline=outline, tags=tuple(tags))

        def draw_antipad(cx: float, top: float, bottom: float, diameter: float) -> None:
            half = max(1.0, diameter * px_per_mm / 2.0)
            canvas.create_rectangle(cx - half, top + 1, cx + half, bottom - 1, fill="#ffffff", outline="")
            canvas.create_rectangle(cx - half, top + 1, cx + half, bottom - 1, outline="#f59e0b",
                                    width=1, dash=(3, 2))

        for via in signal_vias:
            cx = vx(float(via.y))
            if via.backdrilled:
                draw_barrel(cx, float(via.effective_barrel_z_bottom), float(via.effective_barrel_z_top),
                            float(via.drill_diameter), float(via.finished_hole_diameter),
                            "#b87333", "#7c2d12", tags=("via-barrel",))
                bd = float(via.backdrill_diameter or 0.0)
                half = max(1.0, bd * px_per_mm / 2.0)
                if via.backdrill_from_side == "bottom":
                    y1, y2 = z_to_y(float(via.effective_barrel_z_bottom)), z_to_y(float(via.z_bottom))
                else:
                    y1, y2 = z_to_y(float(via.z_top)), z_to_y(float(via.effective_barrel_z_top))
                if y2 < y1:
                    y1, y2 = y2, y1
                canvas.create_rectangle(cx - half, y1, cx + half, y2, fill="", outline="#dc2626",
                                        width=2, dash=(5, 3), tags=("backdrill",))
            else:
                draw_barrel(cx, float(via.z_bottom), float(via.z_top),
                            float(via.drill_diameter), float(via.finished_hole_diameter),
                            "#b87333", "#7c2d12", tags=("via-barrel",))

        for via in signal_vias:
            cx = vx(float(via.y))
            for name in getattr(via, "functional_pad_layers", []):
                entry = layer_index.get(str(name))
                if entry is None:
                    continue
                _layer, top, bottom = entry
                draw_layer_pad(cx, top, bottom, float(via.connected_pad_diameter),
                               "#dc2626", "#7f1d1d", tags=("functional-pad",))
            if getattr(via, "nonfunctional_pad_mode", "remove") == "keep":
                for name in getattr(via, "nonfunctional_pad_layers", []):
                    entry = layer_index.get(str(name))
                    if entry is None:
                        continue
                    _layer, top, bottom = entry
                    draw_layer_pad(cx, top, bottom, float(via.nonfunctional_pad_diameter),
                                   "#f59e0b", "#92400e", tags=("nonfunctional-pad",))

        for contour in getattr(geometry, "antipad_contours", []):
            entry = layer_index.get(str(contour.layer))
            if entry is None:
                continue
            _layer, top, bottom = entry
            draw_antipad(vx(float(contour.center_y)), top, bottom,
                         projected_width(contour, "y"))

        for via in return_vias:
            cx = vx(float(via.y))
            draw_barrel(cx, float(via.z_bottom), float(via.z_top),
                        float(via.drill_diameter), float(via.finished_hole_diameter),
                        "#64748b", "#334155", tags=("return-via-barrel",))
            for name in getattr(via, "connect_layers", []):
                entry = layer_index.get(str(name))
                if entry is None:
                    continue
                _layer, top, bottom = entry
                draw_layer_pad(cx, top, bottom, float(via.pad_diameter),
                               "#94a3b8", "#475569", tags=("return-pad",))
            for name in getattr(via, "clearance_layers", []):
                entry = layer_index.get(str(name))
                if entry is None:
                    continue
                _layer, top, bottom = entry
                draw_antipad(cx, top, bottom, float(via.antipad_diameter))

        def mark_layer(name: str, label: str, color: str) -> None:
            entry = layer_index.get(str(name))
            if entry is None:
                return
            _layer, top, bottom = entry
            mid = (top + bottom) / 2.0
            canvas.create_line(x1 - 8, mid, x1, mid, fill=color, width=3, arrow="last", tags=("entry-exit-marker",))
            canvas.create_text(x1 - 10, mid, anchor="e", text=label, fill=color,
                               font=("TkDefaultFont", 9, "bold"), tags=("entry-exit-marker",))

        if signal_vias:
            via0 = signal_vias[0]
            mark_layer(getattr(via0, "entry_layer", ""), "entry", "#166534")
            mark_layer(getattr(via0, "exit_layer", ""), "exit", "#b45309")

        canvas.create_text(pad, title_y, anchor="nw",
                           text=f"VIA 反焊盘截面 · 单位 {self.unit}",
                           font=title_font, tags=("hud-label", "cross-title"))
        canvas.create_text(pad, note_y, anchor="nw",
                           text="信号孔（Signal VIA）/回流地孔（Return VIA）的逐层反焊盘由本页配置；非圆形显示真实投影宽度。",
                           fill="#9a3412", font=label_font, tags=("hud-label", "scale-note"))
        summary_lines: List[str] = []
        if signal_vias:
            v = signal_vias[0]
            contours = list(getattr(geometry, "antipad_contours", []))
            shape_text = (f"{contours[0].shape} {format_length(contours[0].length, self.unit)}×"
                          f"{format_length(contours[0].width, self.unit)}"
                          if contours and contours[0].shape != "circle" else
                          f"circle Ø{format_length(v.antipad_diameter, self.unit)}")
            summary_lines.append(
                f"信号孔：钻孔 Ø{format_length(v.drill_diameter, self.unit)} "
                f"成品孔 Ø{format_length(v.finished_hole_diameter, self.unit)} "
                f"功能焊盘 Ø{format_length(v.connected_pad_diameter, self.unit)} "
                f"反焊盘 {shape_text} {self.unit}"
            )
        if return_vias:
            r = return_vias[0]
            summary_lines.append(
                f"回流孔 ×{len(return_vias)}：钻孔 Ø{format_length(r.drill_diameter, self.unit)} "
                f"焊盘 Ø{format_length(r.pad_diameter, self.unit)} "
                f"反焊盘 Ø{format_length(r.antipad_diameter, self.unit)} {self.unit}"
            )
        for v in signal_vias:
            if getattr(v, "backdrilled", False):
                summary_lines.append(
                    f"{v.name} 背钻（{v.backdrill_from_side}）：Ø{format_length(v.backdrill_diameter, self.unit)} "
                    f"残桩 {format_length(v.actual_residual_stub, self.unit)} {self.unit}"
                )
        for message in list(getattr(geometry, "errors", []))[:2]:
            summary_lines.append("错误：" + str(message))
        if not summary_lines:
            summary_lines.append("未生成信号孔/回流孔几何。")
        footer_y = y + pad
        summary_item = canvas.create_text(pad, footer_y, anchor="nw", text="\n".join(summary_lines),
                                          fill="#334155", font=label_font, tags=("via-summary",))
        summary_box = canvas.bbox(summary_item)
        if summary_box:
            footer_y = summary_box[3] + pad
        content_box = canvas.bbox("all")
        if content_box:
            content_width = max(content_width, content_box[2] + pad)
            footer_y = max(footer_y, content_box[3] + pad)
        canvas.configure(scrollregion=(0, 0, content_width, footer_y))


class LayerEditorDialog(tk.Toplevel):
    """带字段联动和即时校验的结构化叠层编辑器。"""
    def __init__(self, parent: tk.Misc, layer: Mapping[str, Any], unit: str,
                 existing_names: Sequence[str], title: str = "编辑叠层") -> None:
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)
        self.unit, self.existing_names = unit, set(existing_names)
        self.result: Optional[Dict[str, Any]] = None
        self._syncing = False
        self.vars: Dict[str, tk.Variable] = {
            "name": tk.StringVar(value=str(layer.get("name", ""))),
            "type": tk.StringVar(value=str(layer.get("type", "dielectric"))),
            "thickness": tk.StringVar(value=format_length(float(layer.get("thickness", 0.1)), unit)),
            "material": tk.StringVar(value=str(layer.get("material", "FR4"))),
            "dk": tk.StringVar(value="" if layer.get("dk") is None else str(layer.get("dk"))),
            "df": tk.StringVar(value="" if layer.get("df") is None else str(layer.get("df"))),
            "conductivity": tk.StringVar(value="" if layer.get("conductivity") is None else str(layer.get("conductivity"))),
            "is_reference_plane": tk.BooleanVar(value=bool(layer.get("is_reference_plane"))),
            "enable_antipad": tk.BooleanVar(value=bool(layer.get("enable_antipad"))),
            "use_global": tk.BooleanVar(value=layer.get("antipad_expansion_override") is None),
            "override": tk.StringVar(value="" if layer.get("antipad_expansion_override") is None else
                                      format_length(float(layer["antipad_expansion_override"]), unit)),
        }
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        fields = (("层名称", "name"), ("层类型", "type"), (f"厚度 ({unit})", "thickness"),
                  ("材料", "material"), ("Dk", "dk"), ("Df", "df"), ("电导率 (S/m)", "conductivity"))
        self.entries: Dict[str, tk.Widget] = {}
        for row, (label, key) in enumerate(fields):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", padx=4, pady=3)
            if key == "type":
                widget: tk.Widget = ttk.Combobox(body, textvariable=self.vars[key], values=tuple(LAYER_TYPE_LABELS), state="readonly")
            else:
                widget = ttk.Entry(body, textvariable=self.vars[key], width=34)
            widget.grid(row=row, column=1, sticky="ew", padx=4, pady=3)
            self.entries[key] = widget
        checks = ttk.LabelFrame(body, text="参考平面与反焊盘", padding=8)
        checks.grid(row=7, column=0, columnspan=2, sticky="ew", pady=8)
        self.ref_check = ttk.Checkbutton(checks, text="作为信号参考平面", variable=self.vars["is_reference_plane"])
        self.ref_check.grid(row=0, column=0, sticky="w")
        self.antipad_check = ttk.Checkbutton(checks, text="在本层生成反焊盘", variable=self.vars["enable_antipad"])
        self.antipad_check.grid(row=1, column=0, sticky="w", pady=3)
        ttk.Radiobutton(checks, text="使用全局值", variable=self.vars["use_global"], value=True).grid(row=2, column=0, sticky="w")
        ttk.Radiobutton(checks, text="本层覆盖", variable=self.vars["use_global"], value=False).grid(row=3, column=0, sticky="w")
        self.override_entry = ttk.Entry(checks, textvariable=self.vars["override"], width=18)
        self.override_entry.grid(row=3, column=1, sticky="w", padx=6)
        ttk.Label(checks, text=unit).grid(row=3, column=2, sticky="w")
        self.error_var = tk.StringVar()
        ttk.Label(body, textvariable=self.error_var, foreground="#b42318", wraplength=430).grid(
            row=8, column=0, columnspan=2, sticky="w", pady=(3, 6))
        buttons = ttk.Frame(body)
        buttons.grid(row=9, column=0, columnspan=2, sticky="e")
        self.save_button = ttk.Button(buttons, text="保存", command=self._save)
        self.save_button.pack(side="right", padx=4)
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right", padx=4)
        for variable in self.vars.values():
            variable.trace_add("write", lambda *_: self._sync_state())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._sync_state()
        self.grab_set()
        self.wait_visibility()
        self.focus_set()

    def _values(self) -> Dict[str, Any]:
        return {key: value.get() for key, value in self.vars.items()}

    def _sync_state(self) -> None:
        if self._syncing:
            return
        self._syncing = True
        kind = str(self.vars["type"].get())
        plane = kind == "plane"
        if not plane:
            self.vars["is_reference_plane"].set(False)
            self.vars["enable_antipad"].set(False)
        if not bool(self.vars["is_reference_plane"].get()):
            self.vars["enable_antipad"].set(False)
        self.ref_check.configure(state="normal" if plane else "disabled")
        self.antipad_check.configure(state="normal" if plane and self.vars["is_reference_plane"].get() else "disabled")
        dielectric = kind in {"dielectric", "soldermask"}
        self.entries["dk"].configure(state="normal" if dielectric else "disabled")
        self.entries["df"].configure(state="normal" if dielectric else "disabled")
        self.entries["conductivity"].configure(state="normal" if kind in {"signal", "plane"} else "disabled")
        self.override_entry.configure(state="disabled" if self.vars["use_global"].get() else "normal")
        self._syncing = False
        try:
            result = layer_from_form(self._values(), self.unit)
            if result["name"] in self.existing_names:
                raise ValueError("层名称与现有层重复。")
            self.error_var.set("")
            self.save_button.configure(state="normal")
        except ValueError as exc:
            self.error_var.set(str(exc))
            self.save_button.configure(state="disabled")

    def _save(self) -> None:
        try:
            result = layer_from_form(self._values(), self.unit)
            if result["name"] in self.existing_names:
                raise ValueError("层名称与现有层重复。")
            self.result = result
            self.destroy()
        except ValueError as exc:
            self.error_var.set(str(exc))


class AntipadWorkbench:
    """组合预览、参数、叠层表格、截面图、摘要与即时校验。"""

    TREE_COLUMNS = (
        ("name", "层名称", 110), ("type", "层类型", 100), ("thickness", "厚度", 88),
        ("material", "材料", 220), ("dk", "Dk", 55), ("df", "Df", 55),
        ("reference", "信号参考平面", 105), ("enabled", "在本层生成反焊盘", 130),
        ("source", "外扩来源", 85), ("override", "本层覆盖值", 90),
        ("effective", "最终有效外扩", 105), ("count", "反焊盘数量", 90),
        ("signal_count", "信号开窗数量", 95),
        ("hfss_status", "HFSS 执行状态", 125),
        ("return_state", "回流地孔（Return VIA）状态", 145),
        ("return_effective", "回流地孔避空", 110),
    )

    def __init__(self, parent: ttk.Frame, app: Any) -> None:
        self.parent, self.app = parent, app
        self.selected_index = 0
        self.geometry: Any = None
        self.validation: Any = None
        self._syncing = False
        self._via_mode = False
        self._build()

    def _build(self) -> None:
        vertical = ttk.PanedWindow(self.parent, orient="vertical")
        self.vertical_panes = vertical
        vertical.pack(fill="both", expand=True)
        upper, lower = ttk.Frame(vertical), ttk.Frame(vertical)
        vertical.add(upper, weight=3)
        vertical.add(lower, weight=2)
        upper_panes = ttk.PanedWindow(upper, orient="horizontal")
        self.upper_panes = upper_panes
        upper_panes.pack(fill="both", expand=True)
        preview_box = ttk.LabelFrame(upper_panes, text="反焊盘局部实时预览", padding=6)
        settings_box = ttk.LabelFrame(upper_panes, text="设计参数与结果", padding=4)
        self.preview_box = preview_box
        self.settings_box = settings_box
        upper_panes.add(preview_box, weight=3)
        upper_panes.add(settings_box, weight=2)
        self.preview = AntipadPreview(preview_box, self.select_layer_by_name)
        self.preview.navigate = self._navigate_via_preview_parameter
        self.preview.pack(fill="both", expand=True)

        settings_canvas = tk.Canvas(settings_box, highlightthickness=0, background="#f5f5f5")
        self.settings_canvas = settings_canvas
        settings_scroll = ttk.Scrollbar(settings_box, orient="vertical", command=settings_canvas.yview)
        settings_canvas.configure(yscrollcommand=settings_scroll.set)
        settings_scroll.pack(side="right", fill="y")
        settings_canvas.pack(side="left", fill="both", expand=True)
        content = ttk.Frame(settings_canvas, padding=3)
        self.settings_content = content
        window = settings_canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda _e: settings_canvas.configure(scrollregion=settings_canvas.bbox("all")))
        settings_canvas.bind("<Configure>", lambda e: settings_canvas.itemconfigure(window, width=e.width))
        settings_box._forwards_mousewheel = True  # type: ignore[attr-defined]
        settings_box.canvas = settings_canvas  # type: ignore[attr-defined]
        attach_mousewheel(settings_box, settings_canvas, content)

        mode_box = ttk.LabelFrame(content, text="反焊盘几何 · 选择模式", padding=5)
        mode_box.pack(fill="x", pady=3)
        mode_var = tk.StringVar(value="independent_pad")
        self.app.variables["antipad.mode"] = mode_var
        mode_var.trace_add("write", lambda *_: self.app._schedule_refresh())
        self.mode_selector = ModeSelector(mode_box, mode_var, self.app._schedule_refresh)
        self.mode_selector.pack(fill="x")
        geometry_box = ttk.LabelFrame(content, text="反焊盘几何", padding=7)
        geometry_box.pack(fill="x", pady=3)
        geometry_box.columnconfigure(1, weight=1)
        self.app._add(geometry_box, 0, "默认反焊盘单边净距", "antipad.global_expansion", length=True)
        self.formula_var = tk.StringVar(value="")
        ttk.Label(geometry_box, textvariable=self.formula_var, foreground="#475569", justify="left", wraplength=430).grid(
            row=1, column=0, columnspan=4, sticky="w", padx=4, pady=4)

        # This page owns only layer-related VIA settings.  Whole-transition,
        # Signal VIA/backdrill and Return VIA controls are constructed once on
        # the dedicated “VIA 换层” page.
        via_content = content

        self.via_box = ttk.LabelFrame(via_content, text="信号孔（Signal VIA）· 全局反焊盘默认值", padding=7)
        self.via_box.columnconfigure(1, weight=1)
        self.via_topology = self.app._add(
            self.via_box, 0, "P/N 开窗拓扑", "via_transition.padstack.antipad.topology",
            values=("independent", "merged_pair"),
            display_values=VIA_TOPOLOGY_LABELS)
        self.via_shape = self.app._add(
            self.via_box, 1, "反焊盘形状", "via_transition.padstack.antipad.shape",
            values=("circle", "ellipse", "capsule", "rounded_rectangle"),
            display_values=VIA_SHAPE_LABELS)
        self.via_direction = self.app._add(
            self.via_box, 2, "合并开窗方向", "via_transition.padstack.antipad.direction_mode",
            values=("auto_pn", "manual"),
            display_values=VIA_DIRECTION_LABELS)
        self.via_size_mode = self.app._add(
            self.via_box, 3, "开窗尺寸控制", "via_transition.padstack.antipad.size_mode",
            values=("auto_fit", "auto_plus_increment", "manual"),
            display_values=VIA_SIZE_MODE_LABELS)
        self.via_diameter = self.app._add(
            self.via_box, 5, "直径", "via_transition.padstack.antipad.diameter", length=True)
        self.via_length = self.app._add(
            self.via_box, 6, "端到端总长度", "via_transition.padstack.antipad.length", length=True)
        self.via_width = self.app._add(
            self.via_box, 7, "总宽度", "via_transition.padstack.antipad.width", length=True)
        self.via_corner = self.app._add(
            self.via_box, 8, "圆角半径", "via_transition.padstack.antipad.corner_radius", length=True)
        self.via_rotation = self.app._add(
            self.via_box, 9, "逆时针旋转角（派生/输入）", "via_transition.padstack.antipad.rotation_deg",
            numeric=True, unit="°")
        self.via_clearance = self.app._add(
            self.via_box, 10, "最小铜边净距（钻孔/背钻）", "via_transition.padstack.antipad.clearance", length=True)
        self.via_end_clearance = self.app._add(
            self.via_box, 11, "端部净距", "via_transition.padstack.antipad.end_clearance", length=True)
        self.via_side_clearance = self.app._add(
            self.via_box, 12, "侧向净距", "via_transition.padstack.antipad.side_clearance", length=True)
        self.via_length_increment = self.app._add(
            self.via_box, 13, "自动适配长度增量", "via_transition.padstack.antipad.length_increment", length=True)
        self.via_width_increment = self.app._add(
            self.via_box, 14, "自动适配宽度增量", "via_transition.padstack.antipad.width_increment", length=True)
        self.via_shape.bind("<<ComboboxSelected>>", lambda _event: self._update_via_field_states())
        self.via_topology.bind("<<ComboboxSelected>>", lambda _event: self._update_via_field_states())
        self.via_direction.bind("<<ComboboxSelected>>", lambda _event: self._update_via_field_states())
        self.via_size_mode.bind("<<ComboboxSelected>>", lambda _event: self._update_via_field_states())
        self.via_field_hint = tk.StringVar(value="")
        self.via_field_hint_label = ttk.Label(
            self.via_box, textvariable=self.via_field_hint, foreground="#475569",
            wraplength=430, justify="left")
        self.via_field_hint_label.grid(row=15, column=0, columnspan=3, sticky="w", pady=3)
        self._via_box_row_slaves: Dict[int, List[tk.Widget]] = {
            row: list(self.via_box.grid_slaves(row=row))
            for row in VIA_GLOBAL_FIELD_ROWS.values()
        }
        self._via_help_key = "topology"
        self._via_help_scope = "全局设置"
        help_box = ttk.LabelFrame(self.via_box, text="参数说明 · 聚焦输入框后同步到参考图", padding=5)
        help_box.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(3, 5))
        self.via_help_var = tk.StringVar(value=VIA_PARAMETER_HELP["topology"])
        self.via_help_label = ttk.Label(
            help_box, textvariable=self.via_help_var, foreground="#1d4ed8",
            wraplength=420, justify="left",
        )
        self.via_help_label.pack(fill="x")
        self.via_geometry_status = tk.StringVar(value="")
        self.via_geometry_status_label = ttk.Label(
            self.via_box, textvariable=self.via_geometry_status, foreground="#334155",
            wraplength=430, justify="left")
        self.via_geometry_status_label.grid(row=16, column=0, columnspan=3, sticky="ew", pady=(3, 8))
        self.via_scan_status = tk.StringVar(value="circle 直径可进入原生 Optimetrics；拓扑/形状切换属于拓扑轴。")
        self.via_scan_status_label = ttk.Label(
            self.via_box, textvariable=self.via_scan_status, foreground="#0f766e",
            wraplength=430, justify="left")
        self.via_scan_status_label.grid(row=17, column=0, columnspan=3, sticky="w", pady=4)
        actions = ttk.Frame(self.via_box)
        actions.grid(row=18, column=0, columnspan=3, sticky="w")
        ttk.Button(actions, text="将当前尺寸加入参数扫描", command=self._add_via_antipad_sweep).pack(side="left", padx=(0, 4))
        ttk.Button(actions, text="前往扫描/结果", command=self._open_via_sweep).pack(side="left")

        for key, widget in (
            ("topology", self.via_topology), ("shape", self.via_shape),
            ("direction_mode", self.via_direction), ("size_mode", self.via_size_mode),
            ("diameter", self.via_diameter), ("length", self.via_length),
            ("width", self.via_width), ("corner_radius", self.via_corner),
            ("rotation_deg", self.via_rotation), ("clearance", self.via_clearance),
            ("end_clearance", self.via_end_clearance), ("side_clearance", self.via_side_clearance),
            ("length_increment", self.via_length_increment), ("width_increment", self.via_width_increment),
        ):
            self._bind_via_help(widget, key)

        self.via_return_box = ttk.LabelFrame(via_content, text="回流地孔（Return VIA）· 非连接层避让规则", padding=7)
        self.via_return_box.columnconfigure(1, weight=1)
        self.via_return_holder = ttk.Frame(self.via_return_box)
        self.via_return_holder.pack(fill="x")
        self.via_return_holder.columnconfigure(1, weight=1)
        ttk.Label(self.via_return_holder, text="非连接层反焊盘直径").grid(
            row=0, column=0, sticky="w", padx=4, pady=3)
        self.via_return_summary_var = tk.StringVar(value="—")
        self.via_return_summary = ttk.Label(
            self.via_return_holder,
            textvariable=self.via_return_summary_var,
            foreground="#334155",
        )
        self.via_return_summary.grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(self.via_return_holder, textvariable=self.app.unit_var, width=5).grid(
            row=0, column=2, sticky="w")
        ttk.Label(
            self.via_return_box,
            text="只读逐层规则摘要；权威输入位于 VIA 换层 → 回流地孔。",
            foreground="#64748b",
        ).pack(fill="x", padx=4, pady=(0, 3))
        self._bind_via_help(self.via_return_summary, "return_diameter")

        follow_box = ttk.LabelFrame(content, text="安装面联动", padding=7)
        follow_box.pack(fill="x", pady=3)
        follow_box.columnconfigure(1, weight=1)
        self.follow_mount_side_widget = self.app._add(
            follow_box, 0, "反焊盘层跟随电容安装面（推荐）",
            "antipad.follow_mount_side", boolean=True,
        )
        self.follow_hint_var = tk.StringVar(value="")
        ttk.Label(
            follow_box, textvariable=self.follow_hint_var, foreground="#0f766e",
            justify="left", wraplength=430,
        ).grid(row=1, column=0, columnspan=4, sticky="w", padx=4, pady=(4, 1))

        self.layer_box = ttk.LabelFrame(via_content, text="当前层反焊盘设置", padding=7)
        self.layer_box.pack(fill="x", pady=3)
        self.layer_box.columnconfigure(1, weight=1)
        self._via_layer_input_job: Optional[str] = None
        self.via_layer_only_widgets: List[tk.Widget] = []
        self.layer_name_var = tk.StringVar(value="—")
        ttk.Label(self.layer_box, textvariable=self.layer_name_var, font=("TkDefaultFont", 9, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w")
        self.layer_effective_var = tk.StringVar(value="")
        self.layer_effective_label = ttk.Label(
            self.layer_box, textvariable=self.layer_effective_var,
            foreground="#1d4ed8", justify="left", wraplength=420,
        )
        self.layer_effective_label.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(2, 4))
        self.via_layer_only_widgets.append(self.layer_effective_label)
        self.layer_enable_var = tk.BooleanVar()
        self.layer_enable = ttk.Checkbutton(self.layer_box, text="在本层生成反焊盘", variable=self.layer_enable_var,
                                            command=lambda: self._layer_control_changed("layer_enabled"))
        self.layer_enable.grid(row=2, column=0, columnspan=3, sticky="w")
        self.layer_source_var = tk.StringVar(value="global")
        self.global_radio = ttk.Radiobutton(self.layer_box, text="使用全局值", variable=self.layer_source_var,
                                            value="global", command=lambda: self._layer_control_changed("layer_source"))
        self.global_radio.grid(row=3, column=0, sticky="w")
        self.override_radio = ttk.Radiobutton(self.layer_box, text="本层覆盖", variable=self.layer_source_var,
                                              value="override", command=lambda: self._layer_control_changed("layer_source"))
        self.override_radio.grid(row=4, column=0, sticky="w")
        self.layer_override_var = tk.StringVar()
        self.layer_override_entry = ttk.Entry(self.layer_box, textvariable=self.layer_override_var, width=14)
        self.layer_override_entry.grid(row=4, column=1, sticky="w", padx=5)
        self.layer_unit_label = ttk.Label(self.layer_box, text=DEFAULT_DISPLAY_UNIT)
        self.layer_unit_label.grid(row=4, column=2, sticky="w")
        self.layer_override_var.trace_add("write", lambda *_: self._layer_option_changed())

        self.via_layer_topology_var = tk.StringVar(value=VIA_TOPOLOGY_LABELS["independent"])
        self.via_layer_shape_var = tk.StringVar(value=VIA_SHAPE_LABELS["circle"])
        self.via_layer_direction_var = tk.StringVar(value=VIA_DIRECTION_LABELS["auto_pn"])
        self.via_layer_size_mode_var = tk.StringVar(value=VIA_SIZE_MODE_LABELS["manual"])
        self.via_layer_topology = ttk.Combobox(
            self.layer_box, textvariable=self.via_layer_topology_var,
            values=tuple(VIA_TOPOLOGY_LABELS.values()), state="readonly", width=24)
        self.via_layer_shape = ttk.Combobox(
            self.layer_box, textvariable=self.via_layer_shape_var,
            values=tuple(VIA_SHAPE_LABELS.values()), state="readonly", width=24)
        self.via_layer_direction = ttk.Combobox(
            self.layer_box, textvariable=self.via_layer_direction_var,
            values=tuple(VIA_DIRECTION_LABELS.values()), state="readonly", width=24)
        self.via_layer_size_mode = ttk.Combobox(
            self.layer_box, textvariable=self.via_layer_size_mode_var,
            values=tuple(VIA_SIZE_MODE_LABELS.values()), state="readonly", width=24)
        self.via_layer_enum_labels: Dict[str, tk.Widget] = {}
        for row, (text, widget, key) in enumerate((
            ("本层拓扑", self.via_layer_topology, "topology"),
            ("本层形状", self.via_layer_shape, "shape"),
            ("本层合并方向", self.via_layer_direction, "direction_mode"),
            ("本层尺寸控制", self.via_layer_size_mode, "size_mode"),
        ), 5):
            label = ttk.Label(self.layer_box, text=text)
            label.grid(row=row, column=0, sticky="w")
            widget.grid(row=row, column=1, sticky="ew", padx=5)
            widget.bind(
                "<<ComboboxSelected>>",
                lambda _event, field=key: self._layer_control_changed(field),
            )
            self._bind_via_help(widget, key, scope="本层覆盖")
            self.via_layer_enum_labels[key] = label
            self.via_layer_only_widgets.extend((label, widget))
        self.via_layer_vars = {key: tk.StringVar() for key in
                               ("diameter", "length", "width", "corner_radius", "rotation_deg", "clearance",
                                "end_clearance", "side_clearance", "length_increment", "width_increment")}
        self.via_layer_widgets: Dict[str, tk.Widget] = {}
        self.via_layer_field_labels: Dict[str, tk.Widget] = {}
        for row, (key, label) in enumerate((
            ("diameter", "本层直径"), ("length", "本层长度"), ("width", "本层宽度"),
            ("corner_radius", "本层圆角"), ("rotation_deg", "本层角度 °"),
            ("clearance", "本层最小铜边净距"), ("end_clearance", "本层端部净距"),
            ("side_clearance", "本层侧向净距"), ("length_increment", "本层长度增量"),
            ("width_increment", "本层宽度增量")), 9):
            field_label = ttk.Label(self.layer_box, text=label)
            field_label.grid(row=row, column=0, sticky="w")
            widget = ttk.Entry(self.layer_box, textvariable=self.via_layer_vars[key], width=14)
            widget.grid(row=row, column=1, sticky="ew", padx=5)
            self.via_layer_widgets[key] = widget
            self.via_layer_field_labels[key] = field_label
            self.via_layer_vars[key].trace_add("write", lambda *_: self._layer_value_changed())
            self._bind_via_help(widget, key, scope="本层覆盖")
            self.via_layer_only_widgets.extend((field_label, widget))
        self.layer_unit_note_var = tk.StringVar(
            value=f"本层长度参数按当前显示单位输入：{DEFAULT_DISPLAY_UNIT}"
        )
        self.layer_unit_note = ttk.Label(
            self.layer_box, textvariable=self.layer_unit_note_var, foreground="#475569",
        )
        self.layer_unit_note.grid(row=19, column=0, columnspan=3, sticky="w", pady=(3, 0))
        self.via_layer_only_widgets.append(self.layer_unit_note)
        self.layer_help_var = tk.StringVar(value="提示：" + VIA_PARAMETER_HELP["layer_source"])
        self.layer_help_label = ttk.Label(
            self.layer_box, textvariable=self.layer_help_var, foreground="#1d4ed8",
            wraplength=420, justify="left",
        )
        self.layer_help_label.grid(row=20, column=0, columnspan=3, sticky="ew", pady=(4, 2))
        self.via_layer_only_widgets.append(self.layer_help_label)
        self.layer_hint_var = tk.StringVar()
        self.layer_hint_label = ttk.Label(
            self.layer_box, textvariable=self.layer_hint_var, foreground="#b42318",
            wraplength=420, justify="left",
        )
        self.layer_hint_label.grid(row=21, column=0, columnspan=3, sticky="w")
        self.layer_navigation = ttk.Frame(self.layer_box)
        self.layer_navigation.grid(row=22, column=0, columnspan=3, sticky="w", pady=(5, 0))
        ttk.Button(
            self.layer_navigation, text="查看全局默认",
            command=lambda: self._scroll_settings_to(self.via_box),
        ).pack(side="left", padx=(0, 4))
        ttk.Button(
            self.layer_navigation, text="前往回流地孔参数",
            command=lambda: self.app._navigate_to_parameter(
                "via_transition.return_vias.antipad_diameter"),
        ).pack(side="left")
        self.via_layer_only_widgets.append(self.layer_navigation)

        # Order for VIA mode: global defaults, Return layer rule summary, then
        # the selected-layer override.  The compact status matrix and full
        # stackup table below show inheritance/effective results in between.
        self.via_box.pack(fill="x", pady=3, before=self.layer_box)
        self.via_return_box.pack(fill="x", pady=3, before=self.layer_box)
        self.via_status_box = ttk.LabelFrame(
            content, text="逐层状态和继承结果（只读；选择一行可编辑该层）", padding=5)
        self.via_status_tree = ttk.Treeview(
            self.via_status_box,
            columns=("layer", "signal", "source", "effective", "return"),
            show="headings", height=5, selectmode="browse")
        for key, title, width in (
            ("layer", "层", 72), ("signal", "信号孔状态", 120),
            ("source", "来源", 82), ("effective", "有效反焊盘", 115),
            ("return", "回流地孔", 105),
        ):
            self.via_status_tree.heading(key, text=title)
            self.via_status_tree.column(key, width=width, minwidth=55, anchor="center")
        self.via_status_tree.pack(fill="x")
        self.via_status_tree.bind("<<TreeviewSelect>>", self._via_status_selected)
        self.via_status_box.pack(fill="x", pady=3, before=self.layer_box)

        plane_box = ttk.LabelFrame(content, text="模型边界 / 地平面范围", padding=7)
        plane_box.pack(fill="x", pady=3)
        plane_box.columnconfigure(1, weight=1)
        self.app._add(plane_box, 0, "输入端平面余量", "plane.margin_input", length=True)
        self.app._add(plane_box, 1, "输出端平面余量", "plane.margin_output", length=True)
        self.app._add(plane_box, 2, "Y 方向平面余量", "plane.margin_y", length=True)
        ttk.Label(plane_box, text="输入/输出余量分别相对完整信号模型 X 最小/最大端；Y 余量同时作用于上下两侧。",
                  foreground="#475569", wraplength=430).grid(row=3, column=0, columnspan=4, sticky="w", padx=4, pady=4)

        summary_box = ttk.LabelFrame(content, text="反焊盘结果摘要", padding=7)
        summary_box.pack(fill="x", pady=3)
        self.summary_var = tk.StringVar()
        ttk.Label(summary_box, textvariable=self.summary_var, justify="left", wraplength=440,
                  foreground="#334155").pack(fill="x")
        issue_box = ttk.LabelFrame(content, text="即时校验与制造风险", padding=5)
        issue_box.pack(fill="both", expand=True, pady=3)
        self.issue_text = tk.Text(issue_box, height=7, wrap="word", borderwidth=0, background="#fffdf8")
        self.issue_text.tag_configure("error", foreground="#b42318")
        self.issue_text.tag_configure("warning", foreground="#c2410c")
        self.issue_text.tag_configure("good", foreground="#166534")
        self.issue_text.pack(fill="both", expand=True)
        self.issue_text.configure(state="disabled")
        # Scenario-aware visibility: in via_transition the capacitor parameter
        # panes are hidden while summary/issue stay visible for via info.
        self.summary_box = summary_box
        self.param_panes = (mode_box, geometry_box, follow_box)

        lower_panes = ttk.PanedWindow(lower, orient="horizontal")
        self.lower_panes = lower_panes
        lower_panes.pack(fill="both", expand=True)
        table_box = ttk.LabelFrame(lower_panes, text="PCB 叠层 · 结构化编辑", padding=5)
        cross_box = ttk.LabelFrame(lower_panes, text="动态叠层截面", padding=5)
        lower_panes.add(table_box, weight=3)
        lower_panes.add(cross_box, weight=2)
        button_bar = ttk.Frame(table_box)
        self.table_toolbar = button_bar
        button_bar.pack(fill="x", pady=(0, 4))
        button_rows = tuple(ttk.Frame(button_bar) for _ in range(3))
        self.table_action_rows = button_rows
        self.table_action_buttons: List[tk.Widget] = []
        actions = (("添加层", self.add_layer), ("编辑层", self.edit_layer), ("删除层", self.delete_layer),
                   ("复制层", self.duplicate_layer), ("上移", lambda: self.move_layer(-1)),
                   ("下移", lambda: self.move_layer(1)), ("恢复默认叠层", self.restore_stackup))
        for index, (text, command) in enumerate(actions):
            button = ttk.Button(button_bar, text=text, command=command)
            button.pack(in_=button_rows[0], side="left", padx=2)
            self.table_action_buttons.append(button)
        self.via_matrix_bar = ttk.Frame(button_bar)
        self.via_matrix_bar.pack(fill="x", pady=(2, 0))
        matrix_rows = tuple(ttk.Frame(self.via_matrix_bar) for _ in range(3))
        self.via_matrix_rows = matrix_rows
        self.via_matrix_buttons: List[tk.Widget] = []
        for index, (text, command) in enumerate((
            ("应用到选中层", self._apply_via_to_selected),
            ("全选有效层", self._select_via_valid),
            ("只选参考平面", self._select_via_references),
            ("只选背钻影响层", self._select_via_backdrill),
            ("选中层恢复全局", self._restore_via_inherit),
            ("禁用全部有效层", self._clear_all_via_layers),
        )):
            button = ttk.Button(self.via_matrix_bar, text=text, command=command)
            button.pack(in_=matrix_rows[0], side="left", padx=2)
            self.via_matrix_buttons.append(button)
        self._toolbar_layout_job: Optional[str] = None
        self.table_toolbar.bind("<Configure>", lambda _event: self._schedule_table_toolbar_layout())
        self.table_toolbar.after_idle(self._layout_table_toolbar)
        tree_frame = ttk.Frame(table_box)
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=[item[0] for item in self.TREE_COLUMNS], show="headings", height=4)
        stretchy = {"name", "type", "material", "hfss_status"}
        for key, label, width in self.TREE_COLUMNS:
            self.tree.heading(key, text=label)
            minwidth = 96 if key == "type" else 180 if key == "material" else 80 if key in stretchy else 45
            self.tree.column(
                key, width=width,
                minwidth=minwidth,
                anchor="w" if key in stretchy else "center",
                stretch=key in stretchy,
            )
        tree_y = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        tree_x = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=tree_y.set, xscrollcommand=tree_x.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        tree_y.grid(row=0, column=1, sticky="ns")
        tree_x.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self._tree_selected)
        self.tree.bind("<Double-1>", lambda _e: self.edit_layer())
        self.app.stack_tree = self.tree
        self.cross_section = StackupCrossSection(cross_box, self.select_layer)
        self.cross_section.pack(fill="both", expand=True)

    def _selected_layer(self) -> Optional[Dict[str, Any]]:
        layers = self.app.config.get("stackup", [])
        if not layers:
            return None
        self.selected_index = min(self.selected_index, len(layers) - 1)
        return layers[self.selected_index]

    def _scroll_settings_to(self, widget: tk.Widget) -> None:
        """Reveal one layer-related settings section in the sole right scroller."""
        self.settings_canvas.update_idletasks()
        content_height = max(1, self.settings_content.winfo_reqheight())
        viewport_height = max(1, self.settings_canvas.winfo_height())
        movable = max(1, content_height - viewport_height)
        y = widget.winfo_rooty() - self.settings_content.winfo_rooty()
        self.settings_canvas.yview_moveto(max(0.0, min(1.0, (y - 8) / movable)))

    def _schedule_table_toolbar_layout(self) -> None:
        if self._toolbar_layout_job:
            try:
                self.table_toolbar.after_cancel(self._toolbar_layout_job)
            except tk.TclError:
                pass
        self._toolbar_layout_job = self.table_toolbar.after_idle(self._layout_table_toolbar)

    def _layout_table_toolbar(self) -> None:
        """Greedily wrap toolbar buttons for the current width/font tier."""
        self._toolbar_layout_job = None
        available = max(120, self.table_toolbar.winfo_width() - 8)

        def flow(rows: Sequence[tk.Widget], buttons: Sequence[tk.Widget]) -> None:
            for button in buttons:
                button.pack_forget()
            for row in rows:
                row.pack_forget()
            row_index, used = 0, 0
            rows[row_index].pack(fill="x", pady=(0, 2))
            for button in buttons:
                required = max(1, button.winfo_reqwidth()) + 4
                if used and used + required > available and row_index < len(rows) - 1:
                    row_index += 1
                    used = 0
                    rows[row_index].pack(fill="x", pady=(0, 2))
                button.pack(in_=rows[row_index], side="left", padx=2)
                used += required

        flow(self.table_action_rows, self.table_action_buttons)
        flow(self.via_matrix_rows, self.via_matrix_buttons)

    @staticmethod
    def _via_backend_label(value: str, labels: Mapping[str, str]) -> str:
        return next((backend for backend, display in labels.items() if display == value), value)

    def _via_help_context(self, scope: str) -> Dict[str, str]:
        """Read topology/shape from the same pane the focused widget belongs to."""
        if scope.startswith("本层") and hasattr(self, "via_layer_topology_var"):
            return {
                "topology": self._via_backend_label(
                    self.via_layer_topology_var.get(), VIA_TOPOLOGY_LABELS),
                "shape": self._via_backend_label(
                    self.via_layer_shape_var.get(), VIA_SHAPE_LABELS),
                "direction_mode": self._via_backend_label(
                    self.via_layer_direction_var.get(), VIA_DIRECTION_LABELS),
                "size_mode": self._via_backend_label(
                    self.via_layer_size_mode_var.get(), VIA_SIZE_MODE_LABELS),
            }
        return {
            "topology": self.app._backend_value("via_transition.padstack.antipad.topology"),
            "shape": self.app._backend_value("via_transition.padstack.antipad.shape"),
            "direction_mode": self.app._backend_value(
                "via_transition.padstack.antipad.direction_mode"),
            "size_mode": self.app._backend_value("via_transition.padstack.antipad.size_mode"),
        }

    def _show_via_help(self, key: str, scope: str = "全局设置") -> None:
        self._via_help_key = key
        self._via_help_scope = scope
        explanation = via_parameter_help_text(key, **self._via_help_context(scope))
        self.via_help_var.set(f"{scope}｜{explanation}")
        if scope.startswith("本层") and hasattr(self, "layer_help_var"):
            self.layer_help_var.set("提示：" + explanation)
        path = (
            "via_transition.return_vias.antipad_diameter"
            if key == "return_diameter"
            else "via_transition.padstack.antipad." + key
        )
        self.preview.set_active_parameter(path)

    def _bind_via_help(self, widget: tk.Widget, key: str, scope: str = "全局设置") -> None:
        callback = lambda _event, field=key, origin=scope: self._show_via_help(field, origin)
        widget.bind("<FocusIn>", callback, add="+")
        widget.bind("<Enter>", callback, add="+")
        if isinstance(widget, ttk.Combobox):
            widget.bind("<<ComboboxSelected>>", callback, add="+")

    def _navigate_via_preview_parameter(self, path: str) -> None:
        """Route a diagram callout to the value that actually owns this layer."""
        if not self._via_mode or not path.startswith("via_transition.padstack.antipad."):
            navigate = getattr(self.app, "_navigate_to_parameter", None)
            if navigate is not None:
                navigate(path)
            return
        layer = self._selected_layer()
        if layer is None:
            return
        antipad = self.app.config.get("via_transition", {}).get("padstack", {}).get("antipad", {})
        _effective, source, _enabled = effective_antipad(antipad, str(layer.get("name", "")))
        field = path.rsplit(".", 1)[-1]
        if source != "per_layer" or field not in self.via_layer_widgets:
            navigate = getattr(self.app, "_navigate_to_parameter", None)
            if navigate is not None:
                navigate(path)
            return
        widget: tk.Widget = self.via_layer_widgets[field]
        if str(widget.cget("state")) == "disabled":
            if field in {"diameter", "length", "width"}:
                widget = self.via_layer_size_mode
                self._show_via_help("size_mode", "本层设置")
            elif field == "rotation_deg":
                widget = self.via_layer_direction
                self._show_via_help("direction_mode", "本层设置")
        else:
            self._show_via_help(field, "本层覆盖")

        def focus() -> None:
            self._scroll_settings_to(widget)
            try:
                widget.focus_set()
                if isinstance(widget, ttk.Entry):
                    widget.selection_range(0, "end")
            except tk.TclError:
                pass

        self.layer_box.after_idle(focus)

    def _layer_control_changed(self, key: str) -> None:
        self._show_via_help(key, "本层设置")
        self._layer_option_changed()

    def _layer_value_changed(self) -> None:
        """Debounce numeric typing so normalization cannot move the caret mid-edit."""
        if self._syncing:
            return
        job = self._via_layer_input_job
        if job:
            try:
                self.layer_box.after_cancel(job)
            except tk.TclError:
                pass
        self._via_layer_input_job = self.layer_box.after(220, self._commit_layer_value_edit)

    def _commit_layer_value_edit(self) -> None:
        job = self._via_layer_input_job
        self._via_layer_input_job = None
        if job:
            try:
                self.layer_box.after_cancel(job)
            except tk.TclError:
                pass
        self._layer_option_changed(resync_panel=False)

    def bind_via_shared_controls(self) -> None:
        """Compatibility no-op: centralized construction no longer binds late."""
        return

    def reveal_via_parameter(self, path: str, widget: tk.Widget) -> None:
        """Reveal a Signal VIA global/per-layer field on this layer page."""
        self._scroll_settings_to(widget)
        try:
            widget.focus_set()
            if isinstance(widget, ttk.Entry):
                widget.selection_range(0, "end")
                widget.icursor("end")
        except tk.TclError:
            pass

    def _apply_via_layer_enum_visibility(self, states: Mapping[str, Any]) -> None:
        """Show/hide current-layer direction/size-control aliases with the same helper."""
        if not hasattr(self, "via_layer_enum_labels"):
            return
        for key, widget in (
            ("direction_mode", self.via_layer_direction),
            ("size_mode", self.via_layer_size_mode),
        ):
            row_visible = bool(states["visible"][key])
            (widget.grid if row_visible else widget.grid_remove)()
            (self.via_layer_enum_labels[key].grid if row_visible
             else self.via_layer_enum_labels[key].grid_remove)()

    def _set_via_box_row_visible(self, row: int, visible: bool) -> None:
        for slave in self._via_box_row_slaves.get(row, ()):
            (slave.grid if visible else slave.grid_remove)()

    def _update_via_field_states(self) -> None:
        if not self._via_mode:
            return
        shape = self.app._backend_value("via_transition.padstack.antipad.shape")
        topology = self.app._backend_value("via_transition.padstack.antipad.topology")
        direction = self.app._backend_value("via_transition.padstack.antipad.direction_mode")
        size_mode = self.app._backend_value("via_transition.padstack.antipad.size_mode")
        states = via_antipad_field_states(topology, shape, direction, size_mode)
        visible = states["visible"]
        enabled = states["enabled"]
        auto_size = states["auto_size"]
        widgets = {
            "diameter": self.via_diameter, "length": self.via_length,
            "width": self.via_width, "corner_radius": self.via_corner,
            "rotation_deg": self.via_rotation,
            "direction_mode": self.via_direction, "size_mode": self.via_size_mode,
            "end_clearance": self.via_end_clearance, "side_clearance": self.via_side_clearance,
            "length_increment": self.via_length_increment,
            "width_increment": self.via_width_increment,
        }
        for key, widget in widgets.items():
            if key in {"direction_mode", "size_mode"}:
                widget.configure(state="readonly" if enabled[key] else "disabled")
            else:
                widget.configure(state="normal" if enabled[key] else "disabled")
        for key, row in VIA_GLOBAL_FIELD_ROWS.items():
            self._set_via_box_row_visible(row, visible[key])
        if getattr(self, "layer_source_var", None) is not None and self.layer_source_var.get() == "override":
            layer_states = via_antipad_field_states(
                self._via_backend_label(self.via_layer_topology_var.get(), VIA_TOPOLOGY_LABELS),
                self._via_backend_label(self.via_layer_shape_var.get(), VIA_SHAPE_LABELS),
                self._via_backend_label(self.via_layer_direction_var.get(), VIA_DIRECTION_LABELS),
                self._via_backend_label(self.via_layer_size_mode_var.get(), VIA_SIZE_MODE_LABELS),
            )
        else:
            layer_states = states
        self._apply_via_layer_enum_visibility(layer_states)
        hint = " ".join(via_field_hint_sentences(topology, shape, direction, size_mode))
        self.via_field_hint.set(hint)
        (self.via_field_hint_label.grid if hint else self.via_field_hint_label.grid_remove)()
        if self._via_help_key:
            explanation = via_parameter_help_text(
                self._via_help_key, **self._via_help_context(self._via_help_scope))
            self.via_help_var.set(f"{self._via_help_scope}｜{explanation}")
        blocked = [item for item in getattr(self.validation, "items", [])
                   if getattr(item, "rule_id", "") in {"VIA-TRN-090", "VIA-TRN-092"}]
        if blocked:
            self.via_scan_status.set("扫描与 HFSS 已阻止：" + blocked[0].message)
            self.via_scan_status_label.configure(foreground="#b42318")
        elif auto_size:
            axes = "端部净距 / 侧向净距"
            if states["increment_enabled"]:
                axes += " / 长度增量 / 宽度增量"
            self.via_scan_status.set(f"原生 Optimetrics 轴：{axes}；每个 Variation 都执行真实轮廓包含性预检。")
            self.via_scan_status_label.configure(foreground="#0f766e")
        elif shape == "circle":
            self.via_scan_status.set("原生 Optimetrics 轴：circle diameter；clearance 为离散校验/Design 轴。")
            self.via_scan_status_label.configure(foreground="#0f766e")
        else:
            axes = "length / width / rotation"
            if shape == "rounded_rectangle":
                axes += " / corner radius"
            self.via_scan_status.set(f"原生 Optimetrics 轴：{axes}；clearance 为离散校验/Design 轴。")
            self.via_scan_status_label.configure(foreground="#0f766e")

    def _add_via_antipad_sweep(self) -> None:
        shape = self.app._backend_value("via_transition.padstack.antipad.shape")
        if any(getattr(item, "rule_id", "") in {"VIA-TRN-090", "VIA-TRN-092"}
               for item in getattr(self.validation, "items", [])):
            self.via_scan_status.set("未加入：当前反焊盘未通过孔包络/制造净距预检。")
            self.via_scan_status_label.configure(foreground="#b42318")
            return
        try:
            anti = self.app._collect()["via_transition"]["padstack"]["antipad"]
            topology = str(anti.get("topology", "independent"))
            size_mode = str(anti.get("size_mode", "manual"))
            direction_mode = str(anti.get("direction_mode", "auto_pn"))
            states = via_antipad_field_states(topology, shape, direction_mode, size_mode)
            if states["auto_size"]:
                fields = ["end_clearance", "side_clearance"]
                if states["increment_enabled"]:
                    fields += ["length_increment", "width_increment"]
            else:
                fields = ["diameter"] if shape == "circle" else ["length", "width"]
            if shape == "rounded_rectangle" and states["manual_size"]:
                fields.append("corner_radius")
            if states["enabled"]["rotation_deg"]:
                fields.append("rotation_deg")
            for field in fields:
                value = float(anti[field])
                path = "via_transition.padstack.antipad." + field
                if field == "rotation_deg":
                    sweep = {"enabled": True, "start": value - 10.0,
                             "stop": value + 10.0, "step": 5.0}
                elif "increment" in field:
                    sweep = {"enabled": True, "start": max(0.0, value),
                             "stop": max(0.05, value + 0.2), "step": 0.05}
                else:
                    sweep = {"enabled": True, "start": max(0.0, value * 0.9),
                             "stop": value * 1.1, "step": max(0.001, value * 0.05)}
                self.app.config.setdefault("sweeps", {})[path] = sweep
            workbench = getattr(self.app, "simulation_sweep_workbench", None)
            if workbench is not None:
                for field in fields:
                    path = "via_transition.padstack.antipad." + field
                    workbench.sweeps[path] = copy.deepcopy(self.app.config["sweeps"][path])
                workbench.load_config(self.app.config)
            self.via_scan_status.set("已加入真实 HFSS 几何轴；请前往扫描页复核 Variation 和逐点包含性预检。")
            self.via_scan_status_label.configure(foreground="#0f766e")
        except (KeyError, TypeError, ValueError) as exc:
            self.via_scan_status.set("加入扫描失败：" + str(exc))

    def _open_via_sweep(self) -> None:
        self.app.notebook.select(self.app.tabs["仿真与扫描"])

    def select_layer(self, index: int) -> None:
        layers = self.app.config.get("stackup", [])
        if not (0 <= index < len(layers)):
            return
        self.selected_index = index
        self.tree.selection_set(str(index))
        self.tree.see(str(index))
        if hasattr(self, "via_status_tree") and self.via_status_tree.exists(str(index)):
            self.via_status_tree.selection_set(str(index))
            self.via_status_tree.see(str(index))
        self.preview.layer_var.set(str(layers[index]["name"]))
        self._sync_layer_panel()
        if self.geometry is not None:
            if self._via_mode:
                self.cross_section.set_via_data(
                    self.geometry, self.app.display_unit, index, self.validation)
            else:
                self.cross_section.set_data(self.geometry, self.app.display_unit, index)
            self.preview.redraw()
        self.app._schedule_refresh()

    def select_layer_by_name(self, name: str) -> None:
        for index, layer in enumerate(self.app.config.get("stackup", [])):
            if layer["name"] == name:
                self.select_layer(index)
                break

    def _tree_selected(self, _event: Any = None) -> None:
        selected = self.tree.selection()
        if selected:
            # Preserve extended multi-selection; calling select_layer() here
            # would collapse it back to one row.
            self.selected_index = int(selected[0])
            layer = self._selected_layer()
            if layer is not None:
                self.preview.layer_var.set(str(layer["name"]))
            self._sync_layer_panel()
            if self.geometry is not None:
                if self._via_mode:
                    self.cross_section.set_via_data(
                        self.geometry, self.app.display_unit, self.selected_index, self.validation)
                else:
                    self.cross_section.set_data(
                        self.geometry, self.app.display_unit, self.selected_index)
                self.preview.redraw()

    def _via_status_selected(self, _event: Any = None) -> None:
        selected = self.via_status_tree.selection()
        if selected:
            self.select_layer(int(selected[0]))

    def _via_state(self, index: int) -> str:
        layers = self.app.config.get("stackup", [])
        if not (0 <= index < len(layers)) or self.geometry is None:
            return "outside_drill_span"
        vias = list(getattr(self.geometry, "signal_vias", []))
        return (vias[0].per_layer_states.get(str(layers[index]["name"]), "outside_drill_span")
                if vias else "outside_drill_span")

    def _select_via_rows(self, predicate: Any) -> None:
        rows = [str(index) for index, layer in enumerate(self.app.config.get("stackup", []))
                if predicate(index, layer)]
        self.tree.selection_set(rows)
        if rows:
            self.tree.see(rows[0])

    def _select_via_valid(self) -> None:
        self._select_via_rows(lambda index, _layer:
                              self._via_state(index) in {"clearance", "backdrilled_removed"})

    def _select_via_references(self) -> None:
        self._select_via_rows(lambda index, layer:
                              layer.get("type") == "plane" and layer.get("is_reference_plane")
                              and self._via_state(index) in {"clearance", "backdrilled_removed"})

    def _select_via_backdrill(self) -> None:
        self._select_via_rows(lambda index, _layer: self._via_state(index) == "backdrilled_removed")

    def _apply_via_to_selected(self) -> None:
        selected = [int(item) for item in self.tree.selection()]
        if not selected:
            return
        self._layer_option_changed()
        layers = self.app.config.get("stackup", [])
        antipad = self.app.config["via_transition"]["padstack"]["antipad"]
        per_layer = antipad.setdefault("per_layer", {})
        source_name = str(layers[self.selected_index]["name"])
        source = copy.deepcopy(per_layer.get(source_name, {"enabled": True, "inherit_global": True}))
        for index in selected:
            if self._via_state(index) in {"clearance", "backdrilled_removed"}:
                per_layer[str(layers[index]["name"])] = copy.deepcopy(source)
        self.app._schedule_refresh()

    def _restore_via_inherit(self) -> None:
        layers = self.app.config.get("stackup", [])
        per_layer = self.app.config["via_transition"]["padstack"]["antipad"].setdefault("per_layer", {})
        for item in self.tree.selection():
            index = int(item)
            if self._via_state(index) in {"clearance", "backdrilled_removed"}:
                per_layer[str(layers[index]["name"])] = {"enabled": True, "inherit_global": True}
        self.app._schedule_refresh()

    def _clear_all_via_layers(self) -> None:
        layers = self.app.config.get("stackup", [])
        per_layer = self.app.config["via_transition"]["padstack"]["antipad"].setdefault("per_layer", {})
        for index, layer in enumerate(layers):
            if self._via_state(index) in {"clearance", "backdrilled_removed"}:
                per_layer[str(layer["name"])] = {"enabled": False, "inherit_global": True}
        self.app._schedule_refresh()

    def _sync_layer_panel(self) -> None:
        layer = self._selected_layer()
        if layer is None:
            return
        if self._via_mode:
            self._syncing = True
            layer_name = str(layer.get("name", ""))
            vt = self.app.config.get("via_transition", {})
            antipad = vt.get("padstack", {}).get("antipad", {})
            physical_state = self._via_state(self.selected_index)
            eligible = physical_state in {"clearance", "backdrilled_removed"}
            effective, source, enabled = effective_antipad(antipad, layer_name)
            self.layer_name_var.set(f"{layer_name} · {LAYER_TYPE_LABELS.get(str(layer['type']), layer['type'])}")
            self.layer_enable_var.set(bool(enabled))
            self.layer_source_var.set("global" if source == "global" else "override")
            topology = str(effective.get("topology", "independent"))
            shape = str(effective.get("shape", "circle"))
            direction_mode = str(effective.get("direction_mode", "auto_pn"))
            size_mode = str(effective.get("size_mode", "manual"))
            states = via_antipad_field_states(topology, shape, direction_mode, size_mode)
            self.via_layer_topology_var.set(VIA_TOPOLOGY_LABELS.get(topology, topology))
            self.via_layer_shape_var.set(VIA_SHAPE_LABELS.get(shape, shape))
            self.via_layer_direction_var.set(VIA_DIRECTION_LABELS.get(direction_mode, direction_mode))
            self.via_layer_size_mode_var.set(VIA_SIZE_MODE_LABELS.get(size_mode, size_mode))
            self.layer_override_var.set("")
            for key, variable in self.via_layer_vars.items():
                value = float(effective.get(key, 0.0))
                variable.set(str(value) if key == "rotation_deg" else format_length(value, self.app.display_unit))
            self.layer_unit_note_var.set(
                f"本层长度参数按当前显示单位输入：{self.app.display_unit}；角度单位：°"
            )
            contours = [
                item for item in getattr(self.geometry, "antipad_contours", [])
                if item.layer == layer_name
            ]
            source_text = "本层覆盖" if source == "per_layer" else "全局继承"
            if contours:
                first = contours[0]
                actual_size = (
                    f"Ø{format_length(first.diameter, self.app.display_unit)}"
                    if first.shape == "circle"
                    else f"{format_length(first.length, self.app.display_unit)}×"
                         f"{format_length(first.width, self.app.display_unit)} @{first.rotation_deg:g}°"
                )
                derived = []
                if first.topology == "merged_pair" and first.size_mode in {"auto_fit", "auto_plus_increment"}:
                    derived.append("尺寸自动计算")
                if first.topology == "merged_pair" and first.direction_mode == "auto_pn" and first.shape != "circle":
                    derived.append("角度自动计算")
                suffix = "；" + "、".join(derived) if derived else "；采用手工尺寸/角度"
                self.layer_effective_var.set(
                    f"图中有效：{source_text} · {VIA_TOPOLOGY_LABELS.get(first.topology, first.topology)} · "
                    f"{VIA_SHAPE_LABELS.get(first.shape, first.shape)} · {actual_size} {self.app.display_unit}{suffix}"
                )
            else:
                self.layer_effective_var.set(f"图中有效：{source_text} · 当前层不生成 Signal VIA 反焊盘")
            base_state = "normal" if eligible else "disabled"
            self.layer_enable.configure(state=base_state)
            self.global_radio.configure(state=base_state)
            self.override_radio.configure(state=base_state)
            override_enabled = eligible and source == "per_layer"
            self.via_layer_topology.configure(state="readonly" if override_enabled else "disabled")
            self.via_layer_shape.configure(state="readonly" if override_enabled else "disabled")
            merged = states["merged"]
            self.via_layer_direction.configure(
                state="readonly" if override_enabled and merged else "disabled"
            )
            self.via_layer_size_mode.configure(
                state="readonly" if override_enabled and merged else "disabled"
            )
            self._apply_via_layer_enum_visibility(states)
            applicable = states["visible"]
            enabled = states["enabled"]
            for key, widget in self.via_layer_widgets.items():
                widget.configure(state="normal" if override_enabled and enabled[key] else "disabled")
                visible = bool(applicable[key])
                (widget.grid if visible else widget.grid_remove)()
                label_widget = self.via_layer_field_labels[key]
                (label_widget.grid if visible else label_widget.grid_remove)()
            unavailable = {
                "entry_connected": "连接焊盘/N/A：entry 功能连接层不得生成反焊盘。",
                "exit_connected": "连接焊盘/N/A：exit 功能连接层不得生成反焊盘。",
                "nonfunctional_pad": "非功能焊盘/N/A：保留 NFP 的层不得生成反焊盘。",
                "no_plane_copper": "N/A：该 signal 层没有可供 HFSS subtract 的整面铜对象。",
                "outside_drill_span": "N/A：当前层位于钻孔跨度外。",
            }
            self.layer_hint_var.set("" if eligible else unavailable.get(
                physical_state, "N/A：当前层没有可减除铜面。"))
            self._syncing = False
            return
        self._syncing = True
        eligible = layer.get("type") == "plane" and layer.get("is_reference_plane")
        self.layer_name_var.set(f"{layer['name']} · {LAYER_TYPE_LABELS.get(str(layer['type']), layer['type'])}")
        self.layer_enable_var.set(bool(layer.get("enable_antipad")))
        override = layer.get("antipad_expansion_override")
        self.layer_source_var.set("global" if override is None else "override")
        self.layer_override_var.set("" if override is None else format_length(float(override), self.app.display_unit))
        self.layer_unit_label.configure(text=self.app.display_unit)
        state = "normal" if eligible else "disabled"
        antipad = self.app.config.get("antipad", {})
        auto_managed = bool(
            eligible and antipad.get("follow_mount_side")
            and str(layer.get("name", "")) == str(antipad.get("auto_follow_layer", ""))
        )
        self.layer_enable.configure(state="disabled" if auto_managed else state)
        self.global_radio.configure(state=state)
        self.override_radio.configure(state=state)
        self.layer_override_entry.configure(state="normal" if eligible and override is not None else "disabled")
        if not eligible:
            self.layer_hint_var.set("只有信号参考 plane 层可启用反焊盘。")
        elif auto_managed:
            self.layer_hint_var.set("当前层由安装面自动跟随管理；关闭自动跟随后可手工关闭。")
        else:
            self.layer_hint_var.set("")
        self._syncing = False

    def _layer_option_changed(self, *, resync_panel: bool = True) -> None:
        if self._syncing:
            return
        layer = self._selected_layer()
        if layer is None:
            return
        if self._via_mode:
            if resync_panel and self._via_layer_input_job:
                try:
                    self.layer_box.after_cancel(self._via_layer_input_job)
                except tk.TclError:
                    pass
                self._via_layer_input_job = None
            layer_name = str(layer.get("name", ""))
            antipad = self.app.config.setdefault("via_transition", {}).setdefault(
                "padstack", {}).setdefault("antipad", {})
            per_layer = antipad.setdefault("per_layer", {})
            item: Dict[str, Any] = {
                "enabled": bool(self.layer_enable_var.get()),
                "inherit_global": self.layer_source_var.get() == "global",
            }
            if not item["inherit_global"]:
                item["topology"] = self._via_backend_label(
                    self.via_layer_topology_var.get(), VIA_TOPOLOGY_LABELS
                )
                item["shape"] = self._via_backend_label(
                    self.via_layer_shape_var.get(), VIA_SHAPE_LABELS
                )
                item["direction_mode"] = self._via_backend_label(
                    self.via_layer_direction_var.get(), VIA_DIRECTION_LABELS
                )
                item["size_mode"] = self._via_backend_label(
                    self.via_layer_size_mode_var.get(), VIA_SIZE_MODE_LABELS
                )
                try:
                    for key, variable in self.via_layer_vars.items():
                        raw = float(variable.get())
                        item[key] = (raw if key == "rotation_deg" else
                                     convert_length(raw, self.app.display_unit, "mm"))
                    self.layer_hint_var.set("")
                except ValueError:
                    self.layer_hint_var.set("本层覆盖尺寸必须是有效数字。")
                    return
            per_layer[layer_name] = item
            if resync_panel:
                self._sync_layer_panel()
            self.app._schedule_refresh()
            return
        eligible = layer.get("type") == "plane" and layer.get("is_reference_plane")
        if not eligible:
            layer["enable_antipad"] = False
            return
        layer["enable_antipad"] = bool(self.layer_enable_var.get())
        if self.layer_source_var.get() == "global":
            layer["antipad_expansion_override"] = None
            self.layer_hint_var.set("")
        else:
            try:
                value = convert_length(float(self.layer_override_var.get()), self.app.display_unit, "mm")
                if value < 0:
                    raise ValueError
                layer["antipad_expansion_override"] = value
                self.layer_hint_var.set("")
            except ValueError:
                self.layer_hint_var.set("本层覆盖值必须是大于或等于零的数字。")
        self._sync_layer_panel()
        self.app._schedule_refresh()

    def refresh_table(self, config: Mapping[str, Any], geometry: Any = None) -> None:
        for key, label, _width in self.TREE_COLUMNS:
            self.tree.heading(key, text=label)
        selected_name = None
        layer = self._selected_layer()
        if layer:
            selected_name = layer.get("name")
        for item in self.tree.get_children():
            self.tree.delete(item)
        global_expansion = float(config["antipad"]["global_expansion"])
        mode = str(config["antipad"]["mode"])
        for index, layer in enumerate(config["stackup"]):
            enabled = bool(layer.get("enable_antipad"))
            override = layer.get("antipad_expansion_override")
            effective = effective_expansion(layer, global_expansion)
            actual_count = len(geometry.antipads_by_layer.get(layer["name"], [])) if geometry is not None else expected_window_count(mode, enabled)
            values = (
                layer["name"], LAYER_TYPE_LABELS.get(str(layer["type"]), layer["type"]),
                f"{format_length(float(layer['thickness']), self.app.display_unit)} {self.app.display_unit}", layer["material"],
                "—" if layer.get("dk") is None else str(layer["dk"]), "—" if layer.get("df") is None else str(layer["df"]),
                "是" if layer.get("is_reference_plane") else "否", "是" if enabled else "否",
                ("本层覆盖" if override is not None else "使用全局值") if enabled else "—",
                f"{format_length(float(override), self.app.display_unit)} {self.app.display_unit}" if override is not None else "—",
                f"{format_length(float(effective), self.app.display_unit)} {self.app.display_unit}" if effective is not None else "—",
                str(actual_count) if enabled else "—",
                "—", "—", "—", "—",
            )
            self.tree.insert("", "end", iid=str(index), values=values)
            if selected_name == layer["name"]:
                self.selected_index = index
        if config["stackup"]:
            self.selected_index = min(self.selected_index, len(config["stackup"]) - 1)
            self.tree.selection_set(str(self.selected_index))
        self._sync_layer_panel()

    def _open_editor(self, layer: Mapping[str, Any], existing_names: Sequence[str], title: str) -> Optional[Dict[str, Any]]:
        dialog = LayerEditorDialog(self.app, layer, self.app.display_unit, existing_names, title)
        self.app.wait_window(dialog)
        return dialog.result

    def add_layer(self) -> None:
        index = self.selected_index + 1
        template = {"name": f"DIEL{len(self.app.config['stackup']) + 1}", "type": "dielectric", "thickness": 0.1,
                    "material": "FR4", "dk": 3.8, "df": 0.015, "conductivity": None,
                    "is_reference_plane": False, "enable_antipad": False, "antipad_expansion_override": None}
        updated = self._open_editor(template, [layer["name"] for layer in self.app.config["stackup"]], "添加叠层")
        if updated:
            self.app.config["stackup"].insert(index, updated)
            self.selected_index = index
            self._refresh_table(self.app.config)
            self.app._schedule_refresh()

    def edit_layer(self) -> None:
        layer = self._selected_layer()
        if layer is None:
            return
        names = [item["name"] for index, item in enumerate(self.app.config["stackup"]) if index != self.selected_index]
        updated = self._open_editor(copy.deepcopy(layer), names, "编辑所选层")
        if updated:
            self.app.config["stackup"][self.selected_index] = updated
            self._refresh_table(self.app.config)
            self.app._schedule_refresh()

    def delete_layer(self) -> None:
        layer = self._selected_layer()
        if layer is None or len(self.app.config["stackup"]) <= 1:
            return
        if messagebox.askyesno("删除叠层", f"确定删除 {layer['name']}？", parent=self.app):
            del self.app.config["stackup"][self.selected_index]
            self.selected_index = min(self.selected_index, len(self.app.config["stackup"]) - 1)
            self._refresh_table(self.app.config)
            self.app._schedule_refresh()

    def duplicate_layer(self) -> None:
        layer = self._selected_layer()
        if layer is None:
            return
        clone = copy.deepcopy(layer)
        base, suffix = str(clone["name"]), 2
        names = {item["name"] for item in self.app.config["stackup"]}
        clone["name"] = f"{base}_COPY"
        while clone["name"] in names:
            clone["name"] = f"{base}_COPY{suffix}"
            suffix += 1
        self.app.config["stackup"].insert(self.selected_index + 1, clone)
        self.selected_index += 1
        self._refresh_table(self.app.config)
        self.app._schedule_refresh()

    def move_layer(self, delta: int) -> None:
        target = self.selected_index + delta
        layers = self.app.config["stackup"]
        if not (0 <= target < len(layers)):
            return
        layers[self.selected_index], layers[target] = layers[target], layers[self.selected_index]
        self.selected_index = target
        self._refresh_table(self.app.config)
        self.app._schedule_refresh()

    def restore_stackup(self) -> None:
        prompt = (
            "将恢复默认 PCB 叠层，并同步 L1 铜厚、VIA 起止/连接层及端口参考层；"
            "其他参数保持不变。是否继续？"
        )
        if messagebox.askyesno("恢复默认叠层", prompt, parent=self.app):
            defaults = self.app.manager.default()
            updated = copy.deepcopy(self.app.config)
            updated["stackup"] = copy.deepcopy(defaults["stackup"])
            updated["trace"]["copper_thickness"] = defaults["trace"]["copper_thickness"]
            # The M6G workbook identifies reference layers but not their net
            # ownership. Keep the restored baseline fail-safe until the user
            # explicitly confirms which plane(s) are GND stitching targets.
            updated["gnd_vias"]["enabled"] = False
            for key in ("start_layer", "end_layer", "connect_layers"):
                updated["gnd_vias"][key] = copy.deepcopy(defaults["gnd_vias"][key])
            updated["simulation"]["port_reference_layer"] = defaults["simulation"]["port_reference_layer"]
            self.selected_index = 0
            self.app._load_to_widgets(updated)

    def show(self, config: Mapping[str, Any], geometry: Any, validation: Any) -> None:
        self.geometry, self.validation = geometry, validation
        if config["antipad"].get("follow_mount_side"):
            target = str(config["antipad"].get("auto_follow_layer", "")) or "未找到"
            side = "Top" if config.get("capacitor", {}).get("mount_side", "top") == "top" else "Bottom"
            self.follow_hint_var.set(
                f"当前 {side} 安装面自动管理层：{target}；其他手工启用层保持不变。"
            )
        else:
            self.follow_hint_var.set("自动跟随已关闭；切换 Top/Bottom 不会修改任何手工 enable_antipad。")
        self.refresh_table(config, geometry)
        layers = list(config["stackup"])
        self.selected_index = min(self.selected_index, len(layers) - 1)
        layer_name = str(layers[self.selected_index]["name"])
        self.preview.set_data(config, geometry, self.app.display_unit, layer_name)
        self.cross_section.set_data(geometry, self.app.display_unit, self.selected_index)
        mode = str(config["antipad"]["mode"])
        if mode == "independent_pad":
            formula = "外扩从单个焊盘轮廓向外测量。\n反焊盘长度 = 焊盘长度 + 2 × E；宽度 = 焊盘宽度 + 2 × E。"
        else:
            formula = "先计算源旋转焊盘的轴对齐包围盒，再向四周外扩 E；总长和总宽各增加 2E。"
        self.formula_var.set(formula + " 每层可单独覆盖默认值。")
        analysis = analyze_antipad(config, geometry, layer_name, self.app.display_unit,
                                    validation.errors, validation.warnings)
        self.summary_var.set("\n".join(f"{key}：{value}" for key, value in analysis.values.items()))
        self.issue_text.configure(state="normal")
        self.issue_text.delete("1.0", "end")
        if not analysis.errors and not analysis.warnings:
            self.issue_text.insert("end", "✓ 当前配置未发现反焊盘或叠层风险。", "good")
        for message in analysis.errors:
            self.issue_text.insert("end", "● " + message + "\n", "error")
        for message in analysis.warnings:
            self.issue_text.insert("end", "▲ " + message + "\n", "warning")
        self.issue_text.configure(state="disabled")

    def set_scenario_editable(self, editable: bool) -> None:
        """Show only the layer controls that belong to the active scenario."""
        self._via_mode = not editable
        summary = getattr(self, "summary_box", None)
        for pane in getattr(self, "param_panes", ()):
            try:
                if editable:
                    if not pane.winfo_manager():
                        pane.pack(fill="x", pady=3, before=summary)
                else:
                    if pane.winfo_manager():
                        pane.pack_forget()
            except tk.TclError:
                pass
        try:
            if editable:
                self.via_matrix_bar.pack_forget()
                if self.via_box.winfo_manager():
                    self.via_box.pack_forget()
                if self.via_return_box.winfo_manager():
                    self.via_return_box.pack_forget()
                if self.via_status_box.winfo_manager():
                    self.via_status_box.pack_forget()
                if not self.layer_box.winfo_manager():
                    self.layer_box.pack(fill="x", pady=3, before=summary)
                self.layer_override_entry.grid()
                self.layer_unit_label.grid()
                for widget in self.via_layer_only_widgets:
                    widget.grid_remove()
            else:
                if not self.via_matrix_bar.winfo_manager():
                    self.via_matrix_bar.pack(fill="x", pady=(2, 0))
                if not self.via_box.winfo_manager():
                    self.via_box.pack(fill="x", pady=3, before=self.layer_box)
                if not self.via_return_box.winfo_manager():
                    self.via_return_box.pack(fill="x", pady=3, before=self.layer_box)
                if not self.via_status_box.winfo_manager():
                    self.via_status_box.pack(fill="x", pady=3, before=self.layer_box)
                if not self.layer_box.winfo_manager():
                    self.layer_box.pack(fill="x", pady=3, before=summary)
                self.layer_override_entry.grid_remove()
                self.layer_unit_label.grid_remove()
                for widget in self.via_layer_only_widgets:
                    widget.grid()
                self._update_via_field_states()
                self._sync_layer_panel()
        except tk.TclError:
            pass

    def refresh_table_via(self, config: Mapping[str, Any], geometry: Any = None) -> None:
        """Fill the stackup table with audited VIA layer state and dimensions."""
        via_headers = {
            "enabled": "信号孔逐层状态", "source": "拓扑", "override": "形状",
            "effective": "尺寸来源", "count": "最终有效尺寸",
            "signal_count": "信号开窗数量", "return_state": "回流地孔状态",
            "hfss_status": "HFSS 减铜状态",
            "return_effective": "回流地孔有效尺寸",
        }
        for key, label, _width in self.TREE_COLUMNS:
            self.tree.heading(key, text=via_headers.get(key, label))
        selected_name = None
        layer = self._selected_layer()
        if layer:
            selected_name = layer.get("name")
        for item in self.tree.get_children():
            self.tree.delete(item)
        for item in self.via_status_tree.get_children():
            self.via_status_tree.delete(item)
        for index, layer in enumerate(config["stackup"]):
            layer_name = str(layer["name"])
            contours = [item for item in getattr(geometry, "antipad_contours", [])
                        if item.layer == layer_name] if geometry is not None else []
            spec, source, enabled = effective_antipad(
                config.get("via_transition", {}).get("padstack", {}).get("antipad", {}),
                layer_name)
            signal_vias = list(getattr(geometry, "signal_vias", []))
            state = (signal_vias[0].per_layer_states.get(layer_name, "outside_drill_span")
                     if signal_vias else "outside_drill_span")
            if contours:
                first = contours[0]
                if first.shape == "circle":
                    effective_text = "Ø" + format_length(first.diameter, self.app.display_unit)
                else:
                    effective_text = (format_length(first.length, self.app.display_unit) + "×" +
                                      format_length(first.width, self.app.display_unit) +
                                      f" @{first.rotation_deg:g}°")
                topology, shape = first.topology, first.shape
            else:
                effective_text, topology, shape = "—", str(spec.get("topology", "—")), str(spec.get("shape", "—"))
            return_vias = list(getattr(geometry, "return_vias", []))
            if return_vias:
                rv = return_vias[0]
                if layer_name in rv.connect_layers:
                    return_state, return_effective = "连接焊盘", "Ø" + format_length(rv.pad_diameter, self.app.display_unit)
                elif layer_name in rv.clearance_layers:
                    return_state, return_effective = "避空", "Ø" + format_length(rv.antipad_diameter, self.app.display_unit)
                else:
                    return_state, return_effective = "跨度外", "—"
            else:
                return_state, return_effective = "未启用", "—"
            values = (
                layer_name, LAYER_TYPE_LABELS.get(str(layer["type"]), layer["type"]),
                f"{format_length(float(layer['thickness']), self.app.display_unit)} {self.app.display_unit}",
                layer["material"],
                "—" if layer.get("dk") is None else str(layer["dk"]),
                "—" if layer.get("df") is None else str(layer["df"]),
                "是" if layer.get("is_reference_plane") else "否",
                VIA_LAYER_STATE_LABELS.get(state, state),
                VIA_TOPOLOGY_LABELS.get(topology, topology) if contours else "—",
                VIA_SHAPE_LABELS.get(shape, shape) if contours else "—",
                "全局" if source == "global" else "本层覆盖",
                effective_text, str(len(contours)) if enabled else "0",
                ("将执行真实减铜" if contours else
                 "N/A（无可减铜面）" if state in {"no_plane_copper", "nonfunctional_pad",
                                                   "entry_connected", "exit_connected"}
                 else "未启用/跨度外"),
                return_state, return_effective,
            )
            self.tree.insert("", "end", iid=str(index), values=values)
            self.via_status_tree.insert(
                "", "end", iid=str(index), values=(
                    layer_name,
                    VIA_LAYER_STATE_LABELS.get(state, state),
                    "全局继承" if source == "global" else "本层覆盖",
                    effective_text,
                    return_state,
                ))
            if selected_name == layer["name"]:
                self.selected_index = index
        if config["stackup"]:
            self.selected_index = min(self.selected_index, len(config["stackup"]) - 1)
            self.tree.selection_set(str(self.selected_index))
            self.via_status_tree.selection_set(str(self.selected_index))
            self.via_status_tree.see(str(self.selected_index))

    def _refresh_table(self, config: Mapping[str, Any]) -> None:
        if self._via_mode:
            self.refresh_table_via(config, self.geometry)
        else:
            self.refresh_table(config)

    def show_via_transition(self, config: Mapping[str, Any], geometry: Any, validation: Any) -> None:
        """Render the via_transition stackup + via antipad cross-section.

        This is the via-side counterpart of ``show()``: it refreshes the editable
        stackup table, feeds the cross-section a via-specific renderer, and fills
        the summary/issue boxes from via geometry + validation — never calling the
        capacitor ``analyze_antipad`` path.
        """
        self.geometry = geometry
        self.validation = validation
        self._via_mode = True
        self.refresh_table_via(config, geometry)
        self.selected_index = min(self.selected_index, len(config["stackup"]) - 1)
        layer_name = str(config["stackup"][self.selected_index]["name"])
        self.preview.set_via_data(config, geometry, self.app.display_unit, layer_name)
        selected_contour = next((item for item in getattr(geometry, "antipad_contours", [])
                                 if item.layer == layer_name), None)
        if selected_contour is not None:
            if selected_contour.direction_mode == "auto_pn":
                variable = self.app.variables.get("via_transition.padstack.antipad.rotation_deg")
                if variable is not None:
                    derived = f"{selected_contour.rotation_deg:.9g}"
                    if variable.get() != derived:
                        variable.set(derived)
            unit = self.app.display_unit
            if selected_contour.shape == "circle":
                status = (f"当前 Ø{format_length(selected_contour.diameter, unit)} {unit}；"
                          f"逐层有效孔径 Ø{format_length(selected_contour.effective_hole_diameter, unit)} {unit}")
            else:
                status = (
                    f"当前长度/宽度 {format_length(selected_contour.length, unit)}/"
                    f"{format_length(selected_contour.width, unit)} {unit}；"
                    f"所需最小 {format_length(selected_contour.required_length, unit)}/"
                    f"{format_length(selected_contour.required_width, unit)} {unit}；"
                    f"P/N 实际净距 {format_length(selected_contour.p_clearance, unit)}/"
                    f"{format_length(selected_contour.n_clearance, unit)} {unit}；"
                    f"角度 {selected_contour.rotation_deg:g}°。"
                )
            invalid = any(getattr(item, "rule_id", "") == "VIA-TRN-090"
                          for item in getattr(validation, "items", []))
            self.via_geometry_status.set(("非法：" if invalid else "合格：") + status)
            self.via_geometry_status_label.configure(
                foreground="#b42318" if invalid else "#166534")
        else:
            self.via_geometry_status.set("当前层没有实际 HFSS Signal 反焊盘 subtract，显示 N/A。")
            self.via_geometry_status_label.configure(foreground="#475569")
        self._sync_layer_panel()
        self.cross_section.set_via_data(geometry, self.app.display_unit, self.selected_index, validation)
        self._update_via_summary(config, geometry, validation)

    def _update_via_summary(self, config: Mapping[str, Any], geometry: Any, validation: Any) -> None:
        lines: List[str] = []
        return_config = config.get("via_transition", {}).get("return_vias", {})
        return_diameter = float(return_config.get("antipad_diameter", 0.0))
        self.via_return_summary_var.set(
            ("已启用；" if return_config.get("enabled") else "未启用；")
            + f"非连接铜层避空 Ø{format_length(return_diameter, self.app.display_unit)} "
            + self.app.display_unit
        )
        contours = list(getattr(geometry, "antipad_contours", []))
        layer = config["stackup"][self.selected_index]
        layer_name = str(layer["name"])
        layer_contours = [item for item in contours if item.layer == layer_name]
        signal_count = len(getattr(geometry, "signal_vias", []))
        return_vias = list(getattr(geometry, "return_vias", []))
        return_clearance_count = sum(1 for item in return_vias if layer_name in item.clearance_layers)
        topology_text = ("（合并）" if layer_contours and layer_contours[0].topology == "merged_pair" else "")
        lines.append(
            f"信号孔（Signal VIA）：{signal_count}；信号开窗：{len(layer_contours)}{topology_text}；"
            f"回流地孔（Return VIA）：{len(return_vias)}；回流避空：{return_clearance_count}；"
            f"当前层：{layer_name} / {LAYER_TYPE_LABELS.get(str(layer['type']), layer['type'])}"
        )
        if contours:
            first = contours[0]
            dimensions = (f"Ø{format_length(first.diameter, self.app.display_unit)}"
                          if first.shape == "circle" else
                          f"{format_length(first.length, self.app.display_unit)}×"
                          f"{format_length(first.width, self.app.display_unit)} @{first.rotation_deg:g}°")
            layers = sorted({item.layer for item in contours})
            lines.append(
                f"信号孔（Signal VIA）：{first.topology} / {first.shape} / {dimensions} {self.app.display_unit}；"
                f"{len(contours)} 个开窗，作用层 {', '.join(layers)}"
            )
        if return_vias:
            r = return_vias[0]
            lines.append(
                f"回流孔 ×{len(geometry.return_vias)}：钻孔 Ø{format_length(r.drill_diameter, self.app.display_unit)} "
                f"焊盘 Ø{format_length(r.pad_diameter, self.app.display_unit)} "
                f"反焊盘 Ø{format_length(r.antipad_diameter, self.app.display_unit)} {self.app.display_unit}"
            )
        for via in getattr(geometry, "signal_vias", []):
            if getattr(via, "backdrilled", False):
                lines.append(
                    f"{via.name} 背钻（{via.backdrill_from_side}）："
                    f"Ø{format_length(via.backdrill_diameter, self.app.display_unit)} "
                    f"残桩 {format_length(via.actual_residual_stub, self.app.display_unit)} {self.app.display_unit}"
                )
        self.summary_var.set("\n".join(lines) or "未生成信号孔/回流孔几何。")
        self.issue_text.configure(state="normal")
        self.issue_text.delete("1.0", "end")
        errors = list(getattr(geometry, "errors", []))
        warnings = list(getattr(geometry, "warnings", []))
        for message in getattr(validation, "errors", []):
            text = str(message)
            if ("VIA" in text or "via_transition" in text) and text not in errors:
                errors.append(text)
        for message in getattr(validation, "warnings", []):
            text = str(message)
            if ("VIA" in text or "via_transition" in text) and text not in warnings:
                warnings.append(text)
        for item in getattr(validation, "items", []):
            if getattr(item, "level", "") == "error" and str(getattr(item, "rule_id", "")).startswith("VIA-"):
                if item.message not in errors:
                    errors.append(item.message)
            elif getattr(item, "level", "") == "warning" and str(getattr(item, "rule_id", "")).startswith("VIA-"):
                if item.message not in warnings:
                    warnings.append(item.message)
        if not errors and not warnings:
            self.issue_text.insert("end", "✓ VIA 反焊盘配置、轮廓、顶视图、截面与 HFSS 快照一致。", "good")
        for message in errors:
            self.issue_text.insert("end", "● " + str(message) + "\n", "error")
        for message in warnings:
            self.issue_text.insert("end", "▲ " + str(message) + "\n", "warning")
        self.issue_text.configure(state="disabled")

    def show_error(self, message: str) -> None:
        self.preview.show_error(message)
        self.issue_text.configure(state="normal")
        self.issue_text.delete("1.0", "end")
        self.issue_text.insert("end", "● " + message, "error")
        self.issue_text.configure(state="disabled")

    def show_via_error(self, message: str) -> None:
        """Error path for via_transition: clear the cross-section, keep the summary."""
        self.issue_text.configure(state="normal")
        self.issue_text.delete("1.0", "end")
        self.issue_text.insert("end", "● " + message, "error")
        self.issue_text.configure(state="disabled")
        self.cross_section.set_via_data(None, self.app.display_unit, self.selected_index)
