"""差分信号过孔换层（via_transition）几何：Phase 2 基础差分贯通孔。

Phase 2 范围：L1 entry、L14 exit、L1–L14 机械贯通、无背钻。

坐标约定沿用 ``geometry_v1``：
* X 轴信号由左向右传播（输入在 -X，输出在 +X）；
* Y 轴差分横向排列，P 在 +Y，N 在 -Y；
* Z 轴 PCB 厚度方向，PCB 底面 Z=0、顶面最大；
* 叠层 JSON 自顶向下排列。

本模块不导入 PyAEDT，只产生纯 Python 数据结构，供预览、Validate 与
独立 HFSS 脚本共同使用。
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from config_manager import normalize_config
from geometry_v1 import (
    EPSILON,
    Bounds2D,
    LayerZPosition,
    Point2D,
    bounds_from_points,
    calculate_stackup_z_positions,
    combine_bounds,
)
from via_antipad import (ViaAntipadContour, build_layer_contours,
                         circle_intersects_contour, effective_antipad)


@dataclass(frozen=True)
class FunctionalBarrelSpan:
    """功能连接层与机械钻孔之间的物理上下关系。"""

    upper_functional_layer: str
    lower_functional_layer: str
    upper_index: int
    lower_index: int
    drill_start_index: int
    drill_end_index: int
    top_stub: bool
    bottom_stub: bool


@dataclass(frozen=True)
class ViaTransitionPort:
    """单个差分端口（Terminal Port 的局部工程估计）。"""

    name: str
    signal: str
    side: str  # "IN" | "OUT"
    x: float
    y_min: float
    y_max: float
    z_min: float
    z_max: float
    signal_layer: str
    reference_layer: str
    reference_conductor: str
    propagation_direction: str
    integration_line_start: Tuple[float, float, float]
    integration_line_end: Tuple[float, float, float]
    orientation: str = "YZ"
    x_min: Optional[float] = None
    x_max: Optional[float] = None
    y: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "signal": self.signal,
            "side": self.side,
            "x": self.x,
            "y_min": self.y_min,
            "y_max": self.y_max,
            "z_min": self.z_min,
            "z_max": self.z_max,
            "signal_layer": self.signal_layer,
            "reference_layer": self.reference_layer,
            "reference_conductor": self.reference_conductor,
            "propagation_direction": self.propagation_direction,
            "integration_line_start": list(self.integration_line_start),
            "integration_line_end": list(self.integration_line_end),
            "orientation": self.orientation,
            "x_min": self.x if self.x_min is None else self.x_min,
            "x_max": self.x if self.x_max is None else self.x_max,
            "y": ((self.y_min + self.y_max) / 2.0 if self.y is None else self.y),
            "sheet_name": f"PORT_SHEET_{self.side}_{self.signal}",
            "source_shape": f"{'ENTRY' if self.side == 'IN' else 'EXIT'}_TRACE_{self.signal}",
        }


@dataclass(frozen=True)
class SignalViaGeometry:
    """单个信号孔的几何结果（见规格 §6.2）。"""

    name: str
    signal: str
    x: float
    y: float
    drill_start_layer: str
    drill_end_layer: str
    entry_layer: str
    exit_layer: str
    upper_functional_layer: str
    lower_functional_layer: str
    top_stub_present: bool
    bottom_stub_present: bool
    z_bottom: float
    z_top: float
    drill_diameter: float
    finished_hole_diameter: float
    plating_thickness: float
    connected_pad_diameter: float
    nonfunctional_pad_mode: str
    nonfunctional_pad_diameter: float
    antipad_diameter: float
    barrel_model: str
    material: str
    functional_pad_layers: Tuple[str, ...]
    nonfunctional_pad_layers: Tuple[str, ...]
    antipad_layers: Tuple[str, ...]
    antipad_per_layer: Dict[str, float]
    per_layer_states: Dict[str, str]
    # Phase 3 背钻：原始孔桶范围保留在 z_bottom/z_top，背钻后的有效范围见 effective_*。
    backdrilled: bool = False
    backdrill_from_side: str = ""
    backdrill_diameter: float = 0.0
    backdrill_z_bottom: float = 0.0
    backdrill_z_top: float = 0.0
    effective_barrel_z_bottom: float = 0.0
    effective_barrel_z_top: float = 0.0
    target_residual_stub: float = 0.0
    actual_residual_stub: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "signal": self.signal,
            "x": self.x,
            "y": self.y,
            "drill_start_layer": self.drill_start_layer,
            "drill_end_layer": self.drill_end_layer,
            "entry_layer": self.entry_layer,
            "exit_layer": self.exit_layer,
            "upper_functional_layer": self.upper_functional_layer,
            "lower_functional_layer": self.lower_functional_layer,
            "top_stub_present": self.top_stub_present,
            "bottom_stub_present": self.bottom_stub_present,
            "z_bottom": self.z_bottom,
            "z_top": self.z_top,
            "drill_diameter": self.drill_diameter,
            "finished_hole_diameter": self.finished_hole_diameter,
            "plating_thickness": self.plating_thickness,
            "connected_pad_diameter": self.connected_pad_diameter,
            "nonfunctional_pad_mode": self.nonfunctional_pad_mode,
            "nonfunctional_pad_diameter": self.nonfunctional_pad_diameter,
            "antipad_diameter": self.antipad_diameter,
            "barrel_model": self.barrel_model,
            "material": self.material,
            "functional_pad_layers": list(self.functional_pad_layers),
            "nonfunctional_pad_layers": list(self.nonfunctional_pad_layers),
            "antipad_layers": list(self.antipad_layers),
            "antipad_per_layer": copy.deepcopy(self.antipad_per_layer),
            "per_layer_states": copy.deepcopy(self.per_layer_states),
            "backdrilled": self.backdrilled,
            "backdrill_from_side": self.backdrill_from_side,
            "backdrill_diameter": self.backdrill_diameter,
            "backdrill_z_bottom": self.backdrill_z_bottom,
            "backdrill_z_top": self.backdrill_z_top,
            "effective_barrel_z_bottom": self.effective_barrel_z_bottom,
            "effective_barrel_z_top": self.effective_barrel_z_top,
            "target_residual_stub": self.target_residual_stub,
            "actual_residual_stub": self.actual_residual_stub,
        }


@dataclass(frozen=True)
class ReturnViaGeometry:
    """单个回流孔的几何结果（GND 网络）。"""

    name: str
    x: float
    y: float
    start_layer: str
    end_layer: str
    connect_layers: Tuple[str, ...]
    clearance_layers: Tuple[str, ...]
    drill_diameter: float
    finished_hole_diameter: float
    plating_thickness: float
    pad_diameter: float
    antipad_diameter: float
    z_bottom: float
    z_top: float
    barrel_model: str
    material: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "start_layer": self.start_layer,
            "end_layer": self.end_layer,
            "connect_layers": list(self.connect_layers),
            "clearance_layers": list(self.clearance_layers),
            "drill_diameter": self.drill_diameter,
            "finished_hole_diameter": self.finished_hole_diameter,
            "plating_thickness": self.plating_thickness,
            "pad_diameter": self.pad_diameter,
            "antipad_diameter": self.antipad_diameter,
            "z_bottom": self.z_bottom,
            "z_top": self.z_top,
            "barrel_model": self.barrel_model,
            "material": self.material,
        }


@dataclass
class ViaTransitionGeometryResult:
    """via_transition 场景的完整几何结果，供预览/Validate/HFSS 共用。"""

    units: str
    stackup: List[LayerZPosition]
    pcb_top_z: float
    pcb_bottom_z: float
    signal_vias: List[SignalViaGeometry]
    return_vias: List[ReturnViaGeometry]
    entry_traces: List[Dict[str, Any]]
    exit_traces: List[Dict[str, Any]]
    ports: List[ViaTransitionPort]
    airbox: Optional[Dict[str, Any]]
    conductor_groups: Dict[str, List[str]]
    plane_bounds: Bounds2D
    model_bounds: Bounds2D
    antipad_contours: List[ViaAntipadContour] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    differential_pairs: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "units": self.units,
            "stackup": [layer.to_dict() for layer in self.stackup],
            "pcb_top_z": self.pcb_top_z,
            "pcb_bottom_z": self.pcb_bottom_z,
            "scenario": "via_transition",
            "signal_vias": [via.to_dict() for via in self.signal_vias],
            "return_vias": [via.to_dict() for via in self.return_vias],
            "entry_traces": copy.deepcopy(self.entry_traces),
            "exit_traces": copy.deepcopy(self.exit_traces),
            "ports": [port.to_dict() for port in self.ports],
            "differential_pairs": copy.deepcopy(self.differential_pairs),
            "airbox": copy.deepcopy(self.airbox),
            "conductor_groups": copy.deepcopy(self.conductor_groups),
            "plane_bounds": self.plane_bounds.to_dict(),
            "model_bounds": self.model_bounds.to_dict(),
            "antipad_contours": [contour.to_dict() for contour in self.antipad_contours],
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


def _layer_map(stackup: Sequence[LayerZPosition]) -> Dict[str, LayerZPosition]:
    return {layer.name: layer for layer in stackup}


def _copper_layers(stackup: Sequence[LayerZPosition]) -> List[LayerZPosition]:
    return [layer for layer in stackup if layer.layer_type in {"signal", "plane"}]


def derive_functional_barrel_span(
    layer_names: Sequence[str],
    entry_layer: str,
    exit_layer: str,
    drill_start_layer: str,
    drill_end_layer: str,
) -> Optional[FunctionalBarrelSpan]:
    """按叠层索引推导功能孔桶和上下残桩，不赋予 entry/exit 物理方向。"""

    indices = {str(name): index for index, name in enumerate(layer_names)}
    required = (entry_layer, exit_layer, drill_start_layer, drill_end_layer)
    if any(name not in indices for name in required):
        return None
    drill_start_index = indices[drill_start_layer]
    drill_end_index = indices[drill_end_layer]
    if drill_start_index >= drill_end_index:
        return None
    entry_index = indices[entry_layer]
    exit_index = indices[exit_layer]
    upper_index = min(entry_index, exit_index)
    lower_index = max(entry_index, exit_index)
    if upper_index < drill_start_index or lower_index > drill_end_index:
        return None
    upper_layer = entry_layer if entry_index == upper_index else exit_layer
    lower_layer = entry_layer if entry_index == lower_index else exit_layer
    return FunctionalBarrelSpan(
        upper_functional_layer=upper_layer,
        lower_functional_layer=lower_layer,
        upper_index=upper_index,
        lower_index=lower_index,
        drill_start_index=drill_start_index,
        drill_end_index=drill_end_index,
        top_stub=drill_start_index < upper_index,
        bottom_stub=lower_index < drill_end_index,
    )


def _layer_span(stackup: Sequence[LayerZPosition], start: str, end: str) -> List[LayerZPosition]:
    names = [layer.name for layer in stackup]
    if start not in names or end not in names:
        return []
    start_index = names.index(start)
    end_index = names.index(end)
    if start_index > end_index:
        start_index, end_index = end_index, start_index
    return list(stackup[start_index:end_index + 1])


def _rectangle_points(x_min: float, x_max: float, y_min: float, y_max: float) -> List[Point2D]:
    return [
        Point2D(x_min, y_min),
        Point2D(x_max, y_min),
        Point2D(x_max, y_max),
        Point2D(x_min, y_max),
    ]


def _fanout_centerline(x_via: float, y_via: float, x_port: float, y_trace: float,
                       delta: float, style: str) -> List[Point2D]:
    """计算从信号孔中心到端口的走线中心线折点。

    - ``delta = y_trace - y_via``：从孔到耦合直线的横向位移（含符号），
      扇出段的 X 投影恒为 ``|delta|``。
    - ``deg45``：孔中心沿 45° 折到 ``(x_via ± |delta|, y_trace)``（折点朝端口
      一侧），再沿 ``y_trace`` 直线走到端口。
    - ``arc``：单段 90° 圆弧（半径 = |delta|，终点与耦合直线相切）按角度步长
      ≤5° 离散成折线，再沿 ``y_trace`` 直线走到端口。
    - ``delta == 0``：退化为 ``(x_via, y_trace) → (x_port, y_trace)`` 直连。
    """
    if abs(delta) <= EPSILON:
        return [Point2D(x_via, y_trace), Point2D(x_port, y_trace)]
    x_dir = 1.0 if x_port > x_via else -1.0
    fold_x = x_via + x_dir * abs(delta)
    # 长度不足容纳扇出时（校验 VIA-TRN-071 会报错）退化为直连斜线，保证不崩溃。
    # 恰好相等是合法边界：deg45 只有一段 45° 线，arc 仍必须保留完整 90° 圆弧，
    # 不能因为没有后续直线段而退化为被需求明确禁止的斜直线。
    if abs(fold_x - x_via) > abs(x_port - x_via) + EPSILON:
        return [Point2D(x_via, y_via), Point2D(x_port, y_trace)]
    if style != "arc":
        if abs(fold_x - x_port) <= EPSILON:
            return [Point2D(x_via, y_via), Point2D(x_port, y_trace)]
        return [
            Point2D(x_via, y_via),
            Point2D(fold_x, y_trace),
            Point2D(x_port, y_trace),
        ]
    radius = abs(delta)
    arc_cx, arc_cy = fold_x, y_via
    start_angle = math.pi if x_dir > 0 else 0.0
    end_angle = math.pi / 2.0 if delta > 0 else -math.pi / 2.0
    sweep = 1.0 if (delta > 0) == (x_dir < 0) else -1.0
    steps = max(2, int(math.ceil((math.pi / 2.0) / math.radians(5.0))))
    points: List[Point2D] = []
    for index in range(steps + 1):
        theta = start_angle + sweep * (math.pi / 2.0) * index / steps
        points.append(Point2D(
            arc_cx + radius * math.cos(theta),
            arc_cy + radius * math.sin(theta),
        ))
    if math.hypot(points[-1].x - x_port, points[-1].y - y_trace) > EPSILON:
        points.append(Point2D(x_port, y_trace))
    return points


def _offset_polyline(centerline: Sequence[Point2D], width: float) -> List[Point2D]:
    """沿中心线法向 ±width/2 偏移描边，返回闭合走线轮廓多边形。

    内部折点用斜接（miter）交线：``p + (w/2)·(n1+n2)/(1+n1·n2)``，直线处精确、
    45°/90° 拐角处为精确斜接角；弧离散段间夹角极小，轮廓平滑且不自交。
    """
    points = [Point2D(point.x, point.y) for point in centerline]
    if len(points) < 2:
        return []
    half = max(0.0, float(width)) / 2.0
    normals: List[Tuple[float, float]] = []
    for index in range(len(points) - 1):
        dx = points[index + 1].x - points[index].x
        dy = points[index + 1].y - points[index].y
        length = math.hypot(dx, dy)
        normals.append((-dy / length, dx / length) if length > EPSILON else (0.0, 1.0))

    def offset_at(index: int, nx: float, ny: float) -> Tuple[float, float]:
        return points[index].x + nx * half, points[index].y + ny * half

    plus: List[Tuple[float, float]] = [offset_at(0, *normals[0])]
    minus: List[Tuple[float, float]] = [offset_at(0, -normals[0][0], -normals[0][1])]
    for index in range(1, len(points) - 1):
        n1, n2 = normals[index - 1], normals[index]
        denom = max(1.0 + n1[0] * n2[0] + n1[1] * n2[1], 1e-9)
        mx = (n1[0] + n2[0]) / denom
        my = (n1[1] + n2[1]) / denom
        plus.append(offset_at(index, mx, my))
        minus.append(offset_at(index, -mx, -my))
    plus.append(offset_at(len(points) - 1, *normals[-1]))
    minus.append(offset_at(len(points) - 1, -normals[-1][0], -normals[-1][1]))
    outline = plus + list(reversed(minus))
    return [Point2D(x, y) for x, y in outline]


def _fanout_trace_points(x_via: float, y_via: float, x_port: float, y_trace: float,
                         width: float, style: str) -> List[Point2D]:
    """扇出走线轮廓：中心线 + 法向描边；``delta == 0`` 时退化为直连矩形。"""
    delta = y_trace - y_via
    if abs(delta) <= EPSILON:
        x_min, x_max = sorted((float(x_via), float(x_port)))
        return _rectangle_points(x_min, x_max, y_trace - width / 2.0, y_trace + width / 2.0)
    return _offset_polyline(_fanout_centerline(x_via, y_via, x_port, y_trace, delta, style), width)


def _trace_dict(name: str, signal: str, layer: str, material: str,
                points: List[Point2D], z_bottom: float, z_top: float,
                width: float, length: float) -> Dict[str, Any]:
    return {
        "name": name,
        "signal": signal,
        "layer": layer,
        "material": material,
        "points": [point.to_list() for point in points],
        "z_bottom": z_bottom,
        "z_top": z_top,
        "width": width,
        "length": length,
    }


def _rotate_xy_90(x: float, y: float, center_x: float,
                  center_y: float) -> Tuple[float, float]:
    """Map route-local X coordinates to route-Y with one +90° transform."""
    return center_x - (float(y) - center_y), center_y + (float(x) - center_x)


def _rotate_bounds_90(bounds: Bounds2D, center_x: float, center_y: float) -> Bounds2D:
    points = [_rotate_xy_90(x, y, center_x, center_y) for x, y in (
        (bounds.x_min, bounds.y_min), (bounds.x_min, bounds.y_max),
        (bounds.x_max, bounds.y_min), (bounds.x_max, bounds.y_max))]
    return Bounds2D(min(x for x, _ in points), max(x for x, _ in points),
                    min(y for _, y in points), max(y for _, y in points))


def _return_via_positions(config: Mapping[str, Any]) -> List[Point2D]:
    """按 return_vias 配置计算回流孔中心（内部 mm）。"""
    via = config["via_transition"]
    rv = via["return_vias"]
    if not rv.get("enabled"):
        return []
    cx = float(via["center_x"])
    cy = float(via["center_y"])
    ox = float(rv.get("offset_x", 0.0))
    oy = float(rv.get("offset_y", 0.0))
    pattern = str(rv.get("pattern", "side_pair"))
    count = int(rv.get("count", 0))
    if pattern == "custom":
        return [Point2D(float(point[0]), float(point[1]))
                for point in rv.get("custom_coordinates", [])]
    if pattern == "four_corner":
        return [Point2D(cx + sx * ox, cy + sy * oy)
                for sx in (-1.0, 1.0) for sy in (-1.0, 1.0)]
    # side_pair：差分对上下两侧（Y 向）对称各放一颗。
    if pattern == "side_pair":
        half = max(1, count // 2)
        pitch = float(rv.get("pitch_y", 0.0))
        offsets = [(index - (half - 1) / 2.0) * pitch for index in range(half)]
        points: List[Point2D] = []
        for offset in offsets:
            points.append(Point2D(cx + ox, cy + oy + offset))
            points.append(Point2D(cx + ox, cy - oy - offset))
        return points
    return []


def _nearest_reference(
    stackup: Sequence[LayerZPosition], layer_name: str, toward_layer: str,
) -> Optional[LayerZPosition]:
    """返回从信号层朝另一功能连接层方向遇到的最近参考平面。"""
    names = [layer.name for layer in stackup]
    if layer_name not in names or toward_layer not in names or layer_name == toward_layer:
        return None
    index = names.index(layer_name)
    toward_index = names.index(toward_layer)
    step = 1 if toward_index > index else -1
    stop = len(stackup) if step > 0 else -1
    for candidate_index in range(index + step, stop, step):
        candidate = stackup[candidate_index]
        if candidate.layer_type == "plane" and candidate.is_reference_plane:
            return candidate
    return None


def _resolve_backdrill(vt: Mapping[str, Any], drill_start: str, drill_end: str,
                       by_name: Dict[str, LayerZPosition],
                       functional_span: FunctionalBarrelSpan,
                       errors: List[str]) -> Optional[Dict[str, Any]]:
    """计算 Phase 3 背钻：方向推导、工具 Z 范围、有效孔桶范围和残桩。

    返回 None 表示不适用（未启用或无残桩，错误已写入 ``errors``）。
    """
    backdrill = vt.get("backdrill", {})
    enabled = bool(backdrill.get("enabled"))
    from_side = str(backdrill.get("from_side", "auto"))
    bd_diameter = float(backdrill.get("drill_diameter", 0.0))
    residual_stub = float(backdrill.get("residual_stub", 0.0))
    drill_d = float(vt["padstack"]["drill_diameter"])

    upper = by_name[functional_span.upper_functional_layer]
    lower = by_name[functional_span.lower_functional_layer]
    drill_top = by_name[drill_start].z_top
    drill_bottom = by_name[drill_end].z_bottom

    top_stub = functional_span.top_stub
    bottom_stub = functional_span.bottom_stub

    if not enabled:
        if top_stub or bottom_stub:
            errors.append(
                "存在未背钻残桩：内层退出必须启用 backdrill，否则会保留错误残桩。")
        return None

    if bd_diameter <= drill_d:
        errors.append(
            f"backdrill.drill_diameter {bd_diameter} 必须大于原始 drill_diameter {drill_d}。")
        return None
    if residual_stub < 0:
        errors.append("backdrill.residual_stub 必须 >= 0。")
        return None

    side = from_side
    if side == "auto":
        if top_stub and bottom_stub:
            errors.append(
                "backdrill.from_side=auto 无法唯一推导方向：两侧都有残桩，请显式指定 top 或 bottom。")
            return None
        if top_stub:
            side = "top"
        elif bottom_stub:
            side = "bottom"
        else:
            errors.append("backdrill.enabled=true 但不存在残桩（外层到外层贯通孔无需背钻）。")
            return None
    elif side == "top":
        if not top_stub:
            errors.append(
                "backdrill.from_side=top 但不存在顶部残桩（上方功能连接层已位于机械钻孔顶部）。")
            return None
    elif side == "bottom":
        if not bottom_stub:
            errors.append(
                "backdrill.from_side=bottom 但不存在底部残桩（下方功能连接层已位于机械钻孔底部）。")
            return None
    else:
        errors.append(f"backdrill.from_side={from_side!r} 非法；仅支持 auto/top/bottom。")
        return None

    if side == "bottom":
        # Z 自板底向上增大。底部残桩从下方功能层的下铜边界朝 -Z 计算。
        conn_boundary = lower.z_bottom
        effective_top = drill_top
        effective_bottom = conn_boundary - residual_stub
        tool_top = effective_bottom
        tool_bottom = drill_bottom
        if effective_bottom < drill_bottom:
            errors.append("backdrill.residual_stub 过大：背钻终点低于板底。")
            return None
        if effective_bottom > conn_boundary:
            errors.append("背钻越过下方功能连接层铜边界。")
            return None
        actual_stub = conn_boundary - effective_bottom
    else:  # top
        # 顶部残桩从上方功能层的上铜边界朝 +Z 计算。
        conn_boundary = upper.z_top
        effective_bottom = drill_bottom
        effective_top = conn_boundary + residual_stub
        tool_bottom = effective_top
        tool_top = drill_top
        if effective_top > drill_top:
            errors.append("backdrill.residual_stub 过大：背钻终点高于板顶。")
            return None
        if effective_top < conn_boundary:
            errors.append("背钻越过上方功能连接层铜边界。")
            return None
        actual_stub = effective_top - conn_boundary

    return {
        "side": side,
        "diameter": bd_diameter,
        "tool_bottom": tool_bottom,
        "tool_top": tool_top,
        "effective_bottom": effective_bottom,
        "effective_top": effective_top,
        "target_stub": residual_stub,
        "actual_stub": actual_stub,
    }


def _backdrill_removes_layer(layer: LayerZPosition, info: Optional[Dict[str, Any]]) -> bool:
    """判断某铜层是否整体位于背钻去除范围内。"""
    if not info:
        return False
    if info["side"] == "bottom":
        return layer.z_top <= info["effective_bottom"] + EPSILON
    return layer.z_bottom >= info["effective_top"] - EPSILON


def _differential_pairs() -> List[Dict[str, Any]]:
    """四个 Terminal Port 组成的两个差分对（IN/OUT），各自含差分与共模。"""
    return [
        {"name": "IN", "p_port": "PORT_IN_P", "n_port": "PORT_IN_N",
         "differential_name": "IN_DIFF", "common_name": "IN_COMM",
         "reference_impedance": 100.0},
        {"name": "OUT", "p_port": "PORT_OUT_P", "n_port": "PORT_OUT_N",
         "differential_name": "OUT_DIFF", "common_name": "OUT_COMM",
         "reference_impedance": 100.0},
    ]


class ViaTransitionGeometryCalculator:
    """计算差分贯通孔换层场景的完整几何。"""

    def __init__(self, config: Mapping[str, Any]) -> None:
        self.config, self.normalization_messages = normalize_config(config)

    def calculate(self) -> ViaTransitionGeometryResult:
        cfg = self.config
        vt = cfg["via_transition"]
        stackup = calculate_stackup_z_positions(cfg)
        by_name = _layer_map(stackup)
        pcb_top = stackup[0].z_top
        pcb_bottom = stackup[-1].z_bottom
        warnings: List[str] = list(self.normalization_messages)
        errors: List[str] = []

        entry_layer = str(vt["entry_layer"])
        exit_layer = str(vt["exit_layer"])
        drill_start = str(vt["drill_start_layer"])
        drill_end = str(vt["drill_end_layer"])

        if entry_layer not in by_name:
            errors.append(f"entry_layer {entry_layer!r} 不存在于叠层。")
        if exit_layer not in by_name:
            errors.append(f"exit_layer {exit_layer!r} 不存在于叠层。")
        if drill_start not in by_name or drill_end not in by_name:
            errors.append("drill_start_layer/drill_end_layer 不存在于叠层。")
        functional_span = None
        if not errors:
            functional_span = derive_functional_barrel_span(
                [layer.name for layer in stackup], entry_layer, exit_layer,
                drill_start, drill_end,
            )
            if functional_span is None:
                errors.append("功能连接层必须位于自上而下的机械钻孔跨度内。")
        if errors:
            return ViaTransitionGeometryResult(
                units="mm", stackup=stackup, pcb_top_z=pcb_top,
                pcb_bottom_z=pcb_bottom, signal_vias=[], return_vias=[],
                entry_traces=[], exit_traces=[], ports=[], airbox=None,
                conductor_groups={}, plane_bounds=Bounds2D(0, 0, 0, 0),
                model_bounds=Bounds2D(0, 0, 0, 0), warnings=warnings, errors=errors,
            )

        span = _layer_span(stackup, drill_start, drill_end)
        span_names = {layer.name for layer in span}

        # ---- 信号孔 P/N ----
        padstack = vt["padstack"]
        drill_d = float(padstack["drill_diameter"])
        finished_d = float(padstack["finished_hole_diameter"])
        plating = float(padstack["plating_thickness"])
        connected_pad_d = float(padstack["connected_pad_diameter"])
        nfp_mode = str(padstack["nonfunctional_pad_mode"])
        nfp_d = float(padstack["nonfunctional_pad_diameter"])
        antipad_config = padstack["antipad"]
        antipad_d = float(antipad_config["diameter"])
        copper_in_span = [layer for layer in span if layer.layer_type in {"signal", "plane"}]
        antipad_per_layer = {}
        for layer in copper_in_span:
            effective, _source, enabled = effective_antipad(antipad_config, layer.name)
            if enabled and str(effective["shape"]) == "circle":
                antipad_per_layer[layer.name] = float(effective["diameter"])
        barrel_model = str(padstack.get("barrel_model", "hollow"))
        material = str(padstack.get("material", "copper"))
        pair_pitch = float(vt["pair_pitch"])
        # 信号孔中心距独立于走线中心距；旧配置经 normalize 回填为 pair_pitch。
        signal_via_pitch = float(vt.get("signal_via_pitch", pair_pitch))
        center_x = float(vt["center_x"])
        center_y = float(vt["center_y"])

        assert functional_span is not None
        backdrill_info = _resolve_backdrill(
            vt, drill_start, drill_end, by_name, functional_span, errors)

        signal_vias: List[SignalViaGeometry] = []
        for signal, sign in (("P", 1.0), ("N", -1.0)):
            y = center_y + sign * signal_via_pitch / 2.0
            z_top = by_name[drill_start].z_top
            z_bottom = by_name[drill_end].z_bottom
            functional_layers: List[str] = []
            nfp_layers: List[str] = []
            antipad_layers: List[str] = []
            per_layer_states: Dict[str, str] = {}
            for layer in copper_in_span:
                _layer_antipad, _layer_source, layer_antipad_enabled = effective_antipad(
                    antipad_config, layer.name)
                if layer.name == entry_layer:
                    functional_layers.append(layer.name)
                    per_layer_states[layer.name] = "entry_connected"
                elif layer.name == exit_layer:
                    functional_layers.append(layer.name)
                    per_layer_states[layer.name] = "exit_connected"
                elif _backdrill_removes_layer(layer, backdrill_info):
                    # 背钻范围内的铜层：桶壁/非功能焊盘被去除，仍保留对铜平面的避空。
                    per_layer_states[layer.name] = ("backdrilled_removed" if layer.layer_type == "plane"
                                                    else "no_plane_copper")
                    if layer.layer_type == "plane" and layer_antipad_enabled:
                        antipad_layers.append(layer.name)
                elif layer.layer_type == "signal":
                    if nfp_mode == "keep":
                        nfp_layers.append(layer.name)
                        per_layer_states[layer.name] = "nonfunctional_pad"
                    else:
                        # This product does not create a full copper sheet for
                        # arbitrary internal signal layers.  Claiming an
                        # antipad here would make the GUI disagree with HFSS,
                        # which has no blank object to subtract.
                        per_layer_states[layer.name] = "no_plane_copper"
                else:
                    if layer_antipad_enabled:
                        antipad_layers.append(layer.name)
                    per_layer_states[layer.name] = "clearance"

            effective_bottom = z_bottom
            effective_top = z_top
            backdrilled = False
            bd_side = ""
            bd_diameter = 0.0
            bd_z_bottom = 0.0
            bd_z_top = 0.0
            target_stub = 0.0
            actual_stub = 0.0
            if backdrill_info is not None:
                backdrilled = True
                bd_side = str(backdrill_info["side"])
                bd_diameter = float(backdrill_info["diameter"])
                bd_z_bottom = float(backdrill_info["tool_bottom"])
                bd_z_top = float(backdrill_info["tool_top"])
                effective_bottom = float(backdrill_info["effective_bottom"])
                effective_top = float(backdrill_info["effective_top"])
                target_stub = float(backdrill_info["target_stub"])
                actual_stub = float(backdrill_info["actual_stub"])

            signal_vias.append(SignalViaGeometry(
                name=f"VIA_{signal}",
                signal=signal,
                x=center_x,
                y=y,
                drill_start_layer=drill_start,
                drill_end_layer=drill_end,
                entry_layer=entry_layer,
                exit_layer=exit_layer,
                upper_functional_layer=functional_span.upper_functional_layer,
                lower_functional_layer=functional_span.lower_functional_layer,
                top_stub_present=functional_span.top_stub,
                bottom_stub_present=functional_span.bottom_stub,
                z_bottom=z_bottom,
                z_top=z_top,
                drill_diameter=drill_d,
                finished_hole_diameter=finished_d,
                plating_thickness=plating,
                connected_pad_diameter=connected_pad_d,
                nonfunctional_pad_mode=nfp_mode,
                nonfunctional_pad_diameter=nfp_d,
                antipad_diameter=antipad_d,
                barrel_model=barrel_model,
                material=material,
                functional_pad_layers=tuple(functional_layers),
                nonfunctional_pad_layers=tuple(nfp_layers),
                antipad_layers=tuple(antipad_layers),
                antipad_per_layer=antipad_per_layer,
                per_layer_states=per_layer_states,
                backdrilled=backdrilled,
                backdrill_from_side=bd_side,
                backdrill_diameter=bd_diameter,
                backdrill_z_bottom=bd_z_bottom,
                backdrill_z_top=bd_z_top,
                effective_barrel_z_bottom=effective_bottom,
                effective_barrel_z_top=effective_top,
                target_residual_stub=target_stub,
                actual_residual_stub=actual_stub,
            ))

        # The actual openings are generated once per layer/topology.  A merged
        # pair therefore produces one contour rather than two overlapping tools.
        antipad_contours: List[ViaAntipadContour] = []
        if len(signal_vias) == 2:
            by_signal = {via.signal: via for via in signal_vias}
            common_layers = sorted(set(by_signal["P"].antipad_layers)
                                   | set(by_signal["N"].antipad_layers))
            for layer_name in common_layers:
                # A plane crossed by the backdrill must clear the larger tool,
                # not merely the original mechanical drill.  This effective
                # diameter is passed into the one canonical contour resolver,
                # so preview, validation and HFSS receive the same result.
                effective_hole_diameter = drill_d
                if any(via.backdrilled
                       and via.per_layer_states.get(layer_name) == "backdrilled_removed"
                       for via in signal_vias):
                    effective_hole_diameter = max(
                        drill_d, *(via.backdrill_diameter for via in signal_vias)
                    )
                antipad_contours.extend(build_layer_contours(
                    antipad_config, layer_name,
                    (by_signal["P"].x, by_signal["P"].y),
                    (by_signal["N"].x, by_signal["N"].y),
                    effective_hole_diameter,
                ))

        # ---- 回流孔 ----
        return_vias: List[ReturnViaGeometry] = []
        rv = vt["return_vias"]
        rv_positions = _return_via_positions(cfg)
        if rv.get("enabled") and rv_positions:
            rv_start = str(rv["start_layer"])
            rv_end = str(rv["end_layer"])
            connect = tuple(str(x) for x in rv.get("connect_layers", []))
            connected_set = set(connect)
            rv_span = _layer_span(stackup, rv_start, rv_end)
            clearance = tuple(
                layer.name for layer in rv_span
                if layer.layer_type in {"signal", "plane"} and layer.name not in connected_set
            )
            for index, point in enumerate(rv_positions, 1):
                return_vias.append(ReturnViaGeometry(
                    name=f"GND_VIA_{index:02d}",
                    x=point.x,
                    y=point.y,
                    start_layer=rv_start,
                    end_layer=rv_end,
                    connect_layers=connect,
                    clearance_layers=clearance,
                    drill_diameter=float(rv["drill_diameter"]),
                    finished_hole_diameter=float(rv["finished_hole_diameter"]),
                    plating_thickness=float(rv["plating_thickness"]),
                    pad_diameter=float(rv["pad_diameter"]),
                    antipad_diameter=float(rv["antipad_diameter"]),
                    z_bottom=by_name[rv_end].z_bottom if rv_end in by_name else 0.0,
                    z_top=by_name[rv_start].z_top if rv_start in by_name else 0.0,
                    barrel_model=str(rv.get("barrel_model", "hollow")),
                    material=str(rv.get("material", "copper")),
                ))

        # ---- entry/exit 差分走线 ----
        entry_trace = vt["entry_trace"]
        exit_trace = vt["exit_trace"]
        entry_w = float(entry_trace["width"])
        entry_len = float(entry_trace["length"])
        exit_w = float(exit_trace["width"])
        exit_len = float(exit_trace["length"])
        entry_layer_obj = by_name[entry_layer]
        exit_layer_obj = by_name[exit_layer]
        entry_traces: List[Dict[str, Any]] = []
        exit_traces: List[Dict[str, Any]] = []
        conductor_groups: Dict[str, List[str]] = {"P": [], "N": []}
        fanout_cfg = vt.get("fanout") if isinstance(vt.get("fanout"), Mapping) else {}
        fanout_style = str(fanout_cfg.get("style", "deg45"))
        for signal, sign in (("P", 1.0), ("N", -1.0)):
            y_port = center_y + sign * pair_pitch / 2.0          # 端口端中心（走线中心距）
            y_via = center_y + sign * signal_via_pitch / 2.0     # 孔端中心（信号孔中心距）
            entry_points = _fanout_trace_points(
                center_x, y_via, center_x - entry_len, y_port, entry_w, fanout_style,
            )
            entry_name = f"ENTRY_TRACE_{signal}"
            entry_traces.append(_trace_dict(
                entry_name, signal, entry_layer, material, entry_points,
                entry_layer_obj.z_bottom, entry_layer_obj.z_top, entry_w, entry_len,
            ))
            exit_points = _fanout_trace_points(
                center_x, y_via, center_x + exit_len, y_port, exit_w, fanout_style,
            )
            exit_name = f"EXIT_TRACE_{signal}"
            exit_traces.append(_trace_dict(
                exit_name, signal, exit_layer, material, exit_points,
                exit_layer_obj.z_bottom, exit_layer_obj.z_top, exit_w, exit_len,
            ))
            conductor_groups[signal].extend([
                entry_name, f"VIA_{signal}", exit_name,
                f"PAD_{signal}_{entry_layer}", f"PAD_{signal}_{exit_layer}",
            ])

        # ---- 端口 ----
        ports: List[ViaTransitionPort] = []
        ref_in = _nearest_reference(stackup, entry_layer, exit_layer)
        ref_out = _nearest_reference(stackup, exit_layer, entry_layer)
        trace_pitch_ref = pair_pitch
        port_margin = float(cfg["simulation"].get("port_lateral_margin", 0.25))
        for signal, sign in (("P", 1.0), ("N", -1.0)):
            y_center = center_y + sign * pair_pitch / 2.0
            ref = ref_in
            ref_cond = f"PLANE_{ref.name}" if ref else ""
            for side, x_edge, prop in (("IN", center_x - entry_len, "+X"),
                                       ("OUT", center_x + exit_len, "-X")):
                if side == "OUT":
                    ref = ref_out
                    ref_cond = f"PLANE_{ref.name}" if ref else ""
                layer_obj = by_name[entry_layer] if side == "IN" else by_name[exit_layer]
                half_y = max(trace_pitch_ref, entry_w) / 2.0 + port_margin
                # A terminal sheet belongs to one member of the differential
                # pair.  Keeping each sheet on its own side of the pair
                # centreline prevents the P/N sheets at the same X location
                # from overlapping in area (AEDT ValidateDesign rejects such
                # overlapping lumped-port sheets).  The sheets may share only
                # the zero-area centreline edge.
                if signal == "P":
                    y_min = center_y
                    y_max = y_center + half_y
                else:
                    y_min = y_center - half_y
                    y_max = center_y
                z_min = layer_obj.z_bottom
                z_max = layer_obj.z_top
                if ref is not None:
                    z_min = min(z_min, ref.z_bottom)
                    z_max = max(z_max, ref.z_top)
                integration_start = (x_edge, y_center, (layer_obj.z_bottom + layer_obj.z_top) / 2.0)
                integration_end = (x_edge, y_center, ref.z_center if ref else layer_obj.z_center)
                ports.append(ViaTransitionPort(
                    name=f"PORT_{side}_{signal}",
                    signal=signal,
                    side=side,
                    x=x_edge,
                    y_min=y_min,
                    y_max=y_max,
                    z_min=z_min,
                    z_max=z_max,
                    signal_layer=layer_obj.name,
                    reference_layer=ref.name if ref else "",
                    reference_conductor=ref_cond,
                    propagation_direction=prop,
                    integration_line_start=integration_start,
                    integration_line_end=integration_end,
                ))

        # ---- 空气盒 ----
        airbox: Optional[Dict[str, Any]] = None
        sim = cfg["simulation"]
        if sim.get("create_airbox"):
            margin = float(sim.get("airbox_margin", 1.0))
            # The open Region must contain the complete PCB solids, including
            # reference planes, not only the routed traces.  Otherwise native
            # ValidateDesign reports that every board part lies outside the
            # Region even though the signal route itself is enclosed.
            plane_margin_x = float(cfg["plane"].get("margin_input", 2.0))
            plane_margin_out = float(cfg["plane"].get("margin_output", 2.0))
            plane_margin_y = float(cfg["plane"].get("margin_y", 2.0))
            model_half_y = (
                max(pair_pitch, signal_via_pitch) / 2.0
                + max(entry_w, exit_w) / 2.0
            )
            x_min = center_x - entry_len - plane_margin_x - margin
            x_max = center_x + exit_len + plane_margin_out + margin
            y_extent = model_half_y + plane_margin_y + margin
            airbox = {
                "name": "AIRBOX",
                "material": "air",
                "boundary": sim.get("open_boundary", "Radiation"),
                "margin": margin,
                "x_min": x_min,
                "x_max": x_max,
                "y_min": center_y - y_extent,
                "y_max": center_y + y_extent,
                "z_bottom": pcb_bottom - margin,
                "z_top": pcb_top + margin,
            }

        # ---- 边界 ----
        model_x_min = center_x - entry_len
        model_x_max = center_x + exit_len
        # 信号孔可能比走线中心距更靠外，边界取两者较大值，避免扇出越出边界。
        half_y = max(pair_pitch, signal_via_pitch) / 2.0 + max(entry_w, exit_w) / 2.0
        model_bounds = Bounds2D(model_x_min, model_x_max, center_y - half_y, center_y + half_y)
        plane_margin_x = float(cfg["plane"].get("margin_input", 2.0))
        plane_margin_out = float(cfg["plane"].get("margin_output", 2.0))
        plane_margin_y = float(cfg["plane"].get("margin_y", 2.0))
        plane_bounds = Bounds2D(
            model_x_min - plane_margin_x, model_x_max + plane_margin_out,
            center_y - half_y - plane_margin_y, center_y + half_y + plane_margin_y,
        )

        # ---- 几何级错误检查 ----
        for via in signal_vias:
            if not math.isclose(plating, (drill_d - finished_d) / 2.0, abs_tol=EPSILON):
                errors.append(f"{via.name} plating_thickness 必须等于钻孔与成品孔的半径差。")
            if connected_pad_d <= drill_d:
                errors.append(f"{via.name} connected_pad_diameter 必须大于 drill_diameter。")
        for i, first in enumerate(signal_vias):
            for second in signal_vias[i + 1:]:
                if math.hypot(first.x - second.x, first.y - second.y) <= connected_pad_d + EPSILON:
                    errors.append("P/N 信号孔功能焊盘发生重叠或接触。")
        for via in return_vias:
            for sig in signal_vias:
                distance = math.hypot(via.x - sig.x, via.y - sig.y)
                if distance <= (
                        via.pad_diameter / 2.0 + sig.connected_pad_diameter / 2.0 + EPSILON):
                    errors.append(f"{via.name} 侵入信号孔 {sig.name} 的焊盘。")
                shared_clearance_layers = set(via.clearance_layers).intersection(sig.antipad_layers)
                conflicting_layers = []
                for layer_name in shared_clearance_layers:
                    candidates = [contour for contour in antipad_contours
                                  if contour.layer == layer_name
                                  and contour.owner in {sig.signal, "merged_pair"}]
                    if any(circle_intersects_contour(
                            contour, (via.x, via.y), via.antipad_diameter / 2.0)
                           for contour in candidates):
                        conflicting_layers.append(layer_name)
                if conflicting_layers:
                    errors.append(
                        f"{via.name} 与信号孔 {sig.name} 的避空相交或相切（层 "
                        f"{', '.join(sorted(conflicting_layers))}）；这会形成 non-manifold 平面。"
                    )
        if entry_layer == exit_layer:
            errors.append("entry_layer 与 exit_layer 不得相同。")

        # Author all geometry once in route-local coordinates
        # (longitudinal=X, transverse=P/N separation). route_axis=y consumes
        # that same snapshot after one +90° transform, so preview, validation
        # and HFSS cannot drift into separate geometry branches.
        if str(vt.get("route_axis", "x")) == "y":
            signal_vias = [replace(via, x=_rotate_xy_90(via.x, via.y, center_x, center_y)[0],
                                   y=_rotate_xy_90(via.x, via.y, center_x, center_y)[1])
                           for via in signal_vias]
            return_vias = [replace(via, x=_rotate_xy_90(via.x, via.y, center_x, center_y)[0],
                                   y=_rotate_xy_90(via.x, via.y, center_x, center_y)[1])
                           for via in return_vias]
            for trace in entry_traces + exit_traces:
                trace["points"] = [list(_rotate_xy_90(point[0], point[1], center_x, center_y))
                                   for point in trace["points"]]
            rotated_contours: List[ViaAntipadContour] = []
            for contour in antipad_contours:
                points = tuple(_rotate_xy_90(x, y, center_x, center_y)
                               for x, y in contour.points)
                cx, cy = _rotate_xy_90(contour.center_x, contour.center_y, center_x, center_y)
                rotated_contours.append(replace(
                    contour, center_x=cx, center_y=cy,
                    rotation_deg=(contour.rotation_deg + 90.0) % 360.0,
                    points=points,
                    bounds=(min(x for x, _ in points), max(x for x, _ in points),
                            min(y for _, y in points), max(y for _, y in points))))
            antipad_contours = rotated_contours
            rotated_ports: List[ViaTransitionPort] = []
            for port in ports:
                p1 = _rotate_xy_90(port.x, port.y_min, center_x, center_y)
                p2 = _rotate_xy_90(port.x, port.y_max, center_x, center_y)
                start_xy = _rotate_xy_90(port.integration_line_start[0],
                                        port.integration_line_start[1], center_x, center_y)
                end_xy = _rotate_xy_90(port.integration_line_end[0],
                                      port.integration_line_end[1], center_x, center_y)
                rotated_ports.append(replace(
                    port, x=(p1[0] + p2[0]) / 2.0, y_min=p1[1], y_max=p1[1],
                    x_min=min(p1[0], p2[0]), x_max=max(p1[0], p2[0]), y=p1[1],
                    orientation="XZ",
                    propagation_direction={"+X": "+Y", "-X": "-Y"}.get(
                        port.propagation_direction, port.propagation_direction),
                    integration_line_start=(start_xy[0], start_xy[1], port.integration_line_start[2]),
                    integration_line_end=(end_xy[0], end_xy[1], port.integration_line_end[2])))
            ports = rotated_ports
            model_bounds = _rotate_bounds_90(model_bounds, center_x, center_y)
            plane_bounds = _rotate_bounds_90(plane_bounds, center_x, center_y)
            if airbox is not None:
                rb = _rotate_bounds_90(Bounds2D(airbox["x_min"], airbox["x_max"],
                                                airbox["y_min"], airbox["y_max"]),
                                       center_x, center_y)
                airbox.update({"x_min": rb.x_min, "x_max": rb.x_max,
                               "y_min": rb.y_min, "y_max": rb.y_max})

        return ViaTransitionGeometryResult(
            units="mm",
            stackup=stackup,
            pcb_top_z=pcb_top,
            pcb_bottom_z=pcb_bottom,
            signal_vias=signal_vias,
            return_vias=return_vias,
            entry_traces=entry_traces,
            exit_traces=exit_traces,
            ports=ports,
            differential_pairs=_differential_pairs(),
            airbox=airbox,
            conductor_groups=conductor_groups,
            plane_bounds=plane_bounds,
            model_bounds=model_bounds,
            antipad_contours=antipad_contours,
            warnings=warnings,
            errors=errors,
        )


def calculate_via_transition_geometry(config: Mapping[str, Any]) -> ViaTransitionGeometryResult:
    return ViaTransitionGeometryCalculator(config).calculate()
