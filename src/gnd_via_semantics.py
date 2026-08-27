"""GND VIA 工作台使用的纯计算、字段联动和即时校验。

该模块不依赖 Tkinter。VIA 坐标始终来自 :class:`GeometryCalculator`，这里仅对
已经生成的几何做测量、叠层分类和制造可行性检查，避免 GUI 维护第二套布局算法。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from config_manager import format_length


MODE_LABELS = {
    "four_corner": "四角布置",
    "side_pair": "两侧对称布置",
    "rectangular_fence": "四边围栏",
    "custom": "自定义坐标",
}
SIDE_LABELS = {"top_bottom": "上下两侧", "left_right": "左右两侧"}
BARREL_LABELS = {"hollow": "空心镀铜桶", "solid": "实心圆柱近似"}

MODE_DESCRIPTIONS = {
    "four_corner": ("固定 4 颗", "X/Y 净距", "适合电容区四角回流缝合"),
    "side_pair": ("总数为偶数", "一轴净距 + 另一轴节距", "适合沿两条相对边布置"),
    "rectangular_fence": ("数量为 4 的倍数", "X/Y 净距和节距", "适合形成四边回流围栏"),
    "custom": ("数量由坐标派生", "仅使用坐标表", "适合局部障碍或非规则板框"),
}

ALL_LAYOUT_FIELDS = {
    "count", "side_orientation", "clearance_x", "clearance_y", "pitch_x", "pitch_y"
}


def active_layout_fields(pattern: str, side_orientation: str = "top_bottom") -> set[str]:
    """返回当前布局真正参与 ``GeometryCalculator`` 位置计算的字段。"""
    if pattern == "four_corner":
        return {"count", "clearance_x", "clearance_y"}
    if pattern == "side_pair":
        if side_orientation == "left_right":
            return {"count", "side_orientation", "clearance_x", "pitch_y"}
        return {"count", "side_orientation", "clearance_y", "pitch_x"}
    if pattern == "rectangular_fence":
        return {"count", "clearance_x", "clearance_y", "pitch_x", "pitch_y"}
    return {"count"}


@dataclass(frozen=True)
class ViaLayerStatus:
    name: str
    index: int
    layer_type: str
    thickness: float
    reference: bool
    status: str  # connected / clearance / traversed / outside


@dataclass(frozen=True)
class ViaDerived:
    annular_ring: float
    wall_thickness: float
    antipad_clearance: float
    span_thickness: float
    aspect_ratio: float
    via_type: str
    configured_count: int
    actual_count: int
    coordinate_bounds: Optional[Tuple[float, float, float, float]]
    minimum_center_distance: Optional[float]
    minimum_pad_edge_distance: Optional[float]
    minimum_signal_clearance: Optional[float]
    layers: Tuple[ViaLayerStatus, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ViaIssue:
    level: str
    message: str
    path: str = ""


def layer_statuses(config: Mapping[str, Any]) -> Tuple[ViaLayerStatus, ...]:
    stackup = list(config.get("stackup", []))
    vias = config.get("gnd_vias", {})
    names = [str(layer.get("name", "")) for layer in stackup]
    try:
        start = names.index(str(vias.get("start_layer", "")))
        end = names.index(str(vias.get("end_layer", "")))
    except ValueError:
        start, end = 0, -1
    connected = {str(name) for name in vias.get("connect_layers", [])}
    result: List[ViaLayerStatus] = []
    for index, layer in enumerate(stackup):
        name = str(layer.get("name", ""))
        reference = bool(layer.get("type") == "plane" and layer.get("is_reference_plane"))
        if not (start <= index <= end):
            status = "outside"
        elif name in connected and reference:
            status = "connected"
        elif reference:
            status = "clearance"
        else:
            status = "traversed"
        result.append(ViaLayerStatus(
            name=name, index=index, layer_type=str(layer.get("type", "")),
            thickness=float(layer.get("thickness", 0.0)), reference=reference, status=status,
        ))
    return tuple(result)


def _point_to_segment_distance(px: float, py: float, ax: float, ay: float,
                               bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    if abs(dx) + abs(dy) < 1e-30:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _point_in_polygon(x: float, y: float, points: Sequence[Any]) -> bool:
    inside = False
    previous = len(points) - 1
    for index, point in enumerate(points):
        other = points[previous]
        if ((point.y > y) != (other.y > y)) and (
            x < (other.x - point.x) * (y - point.y) / ((other.y - point.y) or 1e-30) + point.x
        ):
            inside = not inside
        previous = index
    return inside


def _distance_to_shapes(x: float, y: float, shapes: Sequence[Any]) -> Optional[float]:
    best: Optional[float] = None
    for shape in shapes:
        points = shape.points
        distance = 0.0 if _point_in_polygon(x, y, points) else min(
            _point_to_segment_distance(x, y, point.x, point.y,
                                       points[(index + 1) % len(points)].x,
                                       points[(index + 1) % len(points)].y)
            for index, point in enumerate(points)
        )
        best = distance if best is None else min(best, distance)
    return best


def _span_thickness_from_geometry(geometry: Any) -> float:
    vias = list(getattr(geometry, "gnd_vias", []))
    if vias:
        return max(0.0, float(vias[0].z_top) - float(vias[0].z_bottom))
    return 0.0


def derive_via_values(config: Mapping[str, Any], geometry: Any) -> ViaDerived:
    vias_cfg = config["gnd_vias"]
    vias = list(getattr(geometry, "gnd_vias", []))
    drill = float(vias_cfg["drill_diameter"])
    finished = float(vias_cfg["finished_hole_diameter"])
    pad = float(vias_cfg["pad_diameter"])
    antipad = float(vias_cfg["antipad_diameter"])
    span = _span_thickness_from_geometry(geometry)
    centers = [(float(via.x), float(via.y)) for via in vias]
    center_distances = [
        math.hypot(first[0] - second[0], first[1] - second[1])
        for index, first in enumerate(centers) for second in centers[index + 1:]
    ]
    minimum_center = min(center_distances) if center_distances else None
    minimum_edge = minimum_center - pad if minimum_center is not None else None
    signal_shapes = list(geometry.all_signal_shapes()) if hasattr(geometry, "all_signal_shapes") else []
    signal_clearances = []
    for x, y in centers:
        distance = _distance_to_shapes(x, y, signal_shapes)
        if distance is not None:
            signal_clearances.append(distance - pad / 2.0)
    bounds = None
    if centers:
        xs, ys = zip(*centers)
        bounds = (min(xs), max(xs), min(ys), max(ys))
    names = [layer.name for layer in layer_statuses(config) if layer.status != "outside"]
    if not names:
        via_type = "无有效跨越"
    elif names[0] == config["stackup"][0]["name"] and names[-1] == config["stackup"][-1]["name"]:
        via_type = "通孔"
    elif names[0] == config["stackup"][0]["name"] or names[-1] == config["stackup"][-1]["name"]:
        via_type = "盲孔"
    else:
        via_type = "埋孔"
    return ViaDerived(
        annular_ring=(pad - drill) / 2.0,
        wall_thickness=(drill - finished) / 2.0,
        antipad_clearance=(antipad - pad) / 2.0,
        span_thickness=span,
        aspect_ratio=span / drill if drill > 0 else math.inf,
        via_type=via_type,
        configured_count=int(vias_cfg.get("count", 0)), actual_count=len(vias),
        coordinate_bounds=bounds,
        minimum_center_distance=minimum_center,
        minimum_pad_edge_distance=minimum_edge,
        minimum_signal_clearance=min(signal_clearances) if signal_clearances else None,
        layers=layer_statuses(config),
    )


def validate_via_workbench(config: Mapping[str, Any], geometry: Any) -> List[ViaIssue]:
    vias = config["gnd_vias"]
    if not vias.get("enabled"):
        return []
    issues: List[ViaIssue] = []
    display_unit = str(config.get("units", "mm"))
    pattern = str(vias.get("pattern", ""))
    count = int(vias.get("count", 0))
    if count <= 0:
        issues.append(ViaIssue("error", "VIA 数量必须为正整数。", "gnd_vias.count"))
    if pattern == "four_corner" and count != 4:
        issues.append(ViaIssue("error", "四角布置固定为 4 颗 VIA。", "gnd_vias.count"))
    if pattern == "side_pair" and count % 2:
        issues.append(ViaIssue("error", "两侧对称布置的 VIA 数量必须为偶数。", "gnd_vias.count"))
    if pattern == "rectangular_fence" and count % 4:
        issues.append(ViaIssue("error", "四边围栏的 VIA 数量必须为 4 的倍数。", "gnd_vias.count"))
    coordinates = [tuple(map(float, point)) for point in vias.get("custom_coordinates", [])]
    if pattern == "custom":
        if not coordinates:
            issues.append(ViaIssue("error", "自定义坐标表不能为空。", "gnd_vias.custom_coordinates"))
        if len(set(coordinates)) != len(coordinates):
            issues.append(ViaIssue("error", "自定义坐标存在重复点。", "gnd_vias.custom_coordinates"))
        if count != len(coordinates):
            issues.append(ViaIssue("error", "自定义模式数量必须等于坐标行数。", "gnd_vias.count"))
    drill = float(vias["drill_diameter"])
    finished = float(vias["finished_hole_diameter"])
    plating = float(vias["plating_thickness"])
    pad = float(vias["pad_diameter"])
    antipad = float(vias["antipad_diameter"])
    if finished >= drill:
        issues.append(ViaIssue("error", "成品孔直径必须小于钻孔直径。", "gnd_vias.finished_hole_diameter"))
    if pad <= drill:
        issues.append(ViaIssue("error", "VIA 焊盘直径必须大于钻孔直径。", "gnd_vias.pad_diameter"))
    if antipad <= pad:
        issues.append(ViaIssue("error", "非连接层避空直径必须大于焊盘直径。", "gnd_vias.antipad_diameter"))
    expected_finished = drill - 2.0 * plating
    if not math.isclose(finished, expected_finished, abs_tol=1e-9):
        issues.append(ViaIssue("error", "成品孔与钻孔/镀铜厚度不一致。", "gnd_vias.finished_hole_diameter"))
    derived = derive_via_values(config, geometry)
    if derived.annular_ring < 0.05:
        threshold = f"{format_length(0.05, display_unit)} {display_unit}"
        issues.append(ViaIssue("warning", f"环宽小于 {threshold}，请按板厂能力复核。",
                               "gnd_vias.pad_diameter"))
    if derived.aspect_ratio > 10:
        issues.append(ViaIssue("warning", "VIA 长径比超过 10:1，存在钻孔/镀铜制造风险。", "gnd_vias.drill_diameter"))
    if vias.get("barrel_model") == "solid":
        issues.append(ViaIssue("warning", "实心圆柱会高估铜体积，仅用于快速趋势预筛选。", "gnd_vias.barrel_model"))
    for message in getattr(geometry, "errors", []):
        if "VIA" in message and message not in {item.message for item in issues}:
            issues.append(ViaIssue("error", message))
    for layer in derived.layers:
        if layer.status == "clearance" and antipad <= pad:
            issues.append(ViaIssue("error", f"非连接参考层 {layer.name} 没有有效 VIA 避空。", "gnd_vias.antipad_diameter"))
    return issues


def applicable_reference_layers(config: Mapping[str, Any]) -> Dict[str, bool]:
    """返回参考平面名称及其是否位于当前 VIA 跨越范围内。"""
    return {layer.name: layer.status in {"connected", "clearance"}
            for layer in layer_statuses(config) if layer.reference}
