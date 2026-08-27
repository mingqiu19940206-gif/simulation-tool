"""可静态打包的反焊盘模式语义、连通拓扑与安全范围分析。"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from config_manager import find_nearest_reference_plane

from geometry_v1 import calculate_antipad_bounds, polygons_overlap


SUPPORTED_MODES = ("independent_pad", "merged_per_capacitor", "merged_all")
EXPECTED_COUNTS = {"independent_pad": 4, "merged_per_capacitor": 2, "merged_all": 1}
_EPSILON = 1.0e-9


def connected_components(tools: Sequence[Any]) -> List[List[int]]:
    parents = list(range(len(tools)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        first, second = find(left), find(right)
        if first != second:
            parents[second] = first

    for left in range(len(tools)):
        for right in range(left + 1, len(tools)):
            if polygons_overlap(tools[left].points, tools[right].points):
                union(left, right)
    groups: Dict[int, List[int]] = {}
    for index in range(len(tools)):
        groups.setdefault(find(index), []).append(index)
    return list(groups.values())


def connected_window_count(tools: Sequence[Any]) -> int:
    return len(connected_components(tools))


def _config_for_mode(config: Mapping[str, Any], mode: str) -> Dict[str, Any]:
    candidate = dict(config)
    candidate["antipad"] = dict(config["antipad"])
    candidate["antipad"]["mode"] = mode
    return candidate


def tools_for_mode(
    config: Mapping[str, Any], geometry: Any, layer_name: str, mode: str,
    expansion: Optional[float] = None,
) -> List[Any]:
    stackup = list(geometry.stackup)
    if not any(layer.name == layer_name and layer.enable_antipad for layer in stackup):
        return []
    if expansion is not None:
        stackup = [
            replace(layer, antipad_expansion_override=float(expansion))
            if layer.name == layer_name else layer
            for layer in stackup
        ]
    return list(calculate_antipad_bounds(
        _config_for_mode(config, mode), stackup, geometry.pad_shapes
    ).get(layer_name, []))


def _point_segment_distance(point: Any, start: Any, end: Any) -> float:
    dx, dy = end.x - start.x, end.y - start.y
    length_squared = dx * dx + dy * dy
    if length_squared <= _EPSILON:
        return math.hypot(point.x - start.x, point.y - start.y)
    position = ((point.x - start.x) * dx + (point.y - start.y) * dy) / length_squared
    position = min(1.0, max(0.0, position))
    nearest_x, nearest_y = start.x + position * dx, start.y + position * dy
    return math.hypot(point.x - nearest_x, point.y - nearest_y)


def polygon_distance(first: Sequence[Any], second: Sequence[Any]) -> float:
    if polygons_overlap(first, second):
        return 0.0
    distance = math.inf
    for points, other in ((first, second), (second, first)):
        for point in points:
            for index, start in enumerate(other):
                distance = min(distance, _point_segment_distance(
                    point, start, other[(index + 1) % len(other)]
                ))
    return distance


def minimum_tool_gap(tools: Sequence[Any]) -> float:
    if len(tools) < 2:
        return math.inf
    return min(
        polygon_distance(tools[left].points, tools[right].points)
        for left in range(len(tools)) for right in range(left + 1, len(tools))
    )


def minimum_plane_edge_margin(tools: Sequence[Any], plane: Any) -> float:
    if not tools:
        return math.inf
    return min(
        min(tool.bounds.x_min - plane.x_min, plane.x_max - tool.bounds.x_max,
            tool.bounds.y_min - plane.y_min, plane.y_max - tool.bounds.y_max)
        for tool in tools
    )


def _critical_expansion(
    config: Mapping[str, Any], geometry: Any, layer_name: str, mode: str,
    current_expansion: float,
) -> Optional[float]:
    expected = EXPECTED_COUNTS[mode]
    if expected <= 1:
        return None

    def count_at(value: float) -> int:
        return connected_window_count(tools_for_mode(
            config, geometry, layer_name, mode, value
        ))

    if count_at(0.0) < expected:
        unsafe, safe = 0.0, -0.001
        for _ in range(40):
            if count_at(safe) >= expected:
                break
            unsafe, safe = safe, safe * 2.0
        else:
            return 0.0
        for _ in range(52):
            middle = (safe + unsafe) / 2.0
            if count_at(middle) >= expected:
                safe = middle
            else:
                unsafe = middle
        return unsafe

    safe, unsafe = 0.0, max(float(current_expansion), 0.001)
    for _ in range(40):
        if count_at(unsafe) < expected:
            break
        safe, unsafe = unsafe, unsafe * 2.0
    else:
        return None
    for _ in range(52):
        middle = (safe + unsafe) / 2.0
        if count_at(middle) < expected:
            unsafe = middle
        else:
            safe = middle
    return unsafe


def _suggested_mode(tools: Sequence[Any]) -> Optional[str]:
    groups = connected_components(tools)
    if len(groups) == 1 and tools:
        return "merged_all"
    if len(groups) == 4:
        return "independent_pad"
    if len(groups) != 2:
        return None
    signals = []
    for group in groups:
        values = {tools[index].signal for index in group}
        if None in values or len(values) != 1:
            return None
        signals.append(next(iter(values)))
    return "merged_per_capacitor" if set(signals) == {"P", "N"} else None


def _contact_pairs(tools: Sequence[Any]) -> Tuple[str, ...]:
    pairs = []
    for left in range(len(tools)):
        for right in range(left + 1, len(tools)):
            if polygons_overlap(tools[left].points, tools[right].points):
                first = "/".join(tools[left].metadata.get("source_pads", [])) or tools[left].name
                second = "/".join(tools[right].metadata.get("source_pads", [])) or tools[right].name
                pairs.append(f"{first} ↔ {second}")
    return tuple(pairs)


@dataclass(frozen=True)
class ModeSemanticStatus:
    mode: str
    expected_count: int
    tool_count: int
    connected_count: int
    expansion: Optional[float]
    critical_expansion: Optional[float]
    recommended_expansion: Optional[float]
    minimum_gap: float
    plane_edge_margin: float
    contact_pairs: Tuple[str, ...]
    suggested_mode: Optional[str]

    @property
    def mismatch(self) -> bool:
        return bool(self.tool_count and self.connected_count < self.expected_count)


def analyze_mode_semantics(
    config: Mapping[str, Any], geometry: Any, layer_name: str, mode: str,
) -> ModeSemanticStatus:
    layer = next((item for item in config["stackup"] if item["name"] == layer_name), None)
    enabled = bool(layer and layer.get("enable_antipad"))
    expansion = None
    if enabled and layer is not None:
        override = layer.get("antipad_expansion_override")
        expansion = float(config["antipad"]["global_expansion"] if override is None else override)
    tools = tools_for_mode(config, geometry, layer_name, mode) if enabled else []
    critical = _critical_expansion(
        config, geometry, layer_name, mode, expansion or 0.0
    ) if tools else None
    recommended = None
    if critical is not None and critical > _EPSILON:
        recommended = max(0.0, critical - max(critical * 0.05, 0.001))
    return ModeSemanticStatus(
        mode, EXPECTED_COUNTS[mode] if enabled else 0, len(tools),
        connected_window_count(tools), expansion, critical, recommended,
        minimum_tool_gap(tools), minimum_plane_edge_margin(tools, geometry.plane_bounds),
        _contact_pairs(tools), _suggested_mode(tools),
    )


def analyze_all_modes(
    config: Mapping[str, Any], geometry: Any, layer_name: str,
) -> Dict[str, ModeSemanticStatus]:
    return {mode: analyze_mode_semantics(config, geometry, layer_name, mode)
            for mode in SUPPORTED_MODES}


def topology_sweep_warnings(
    config: Mapping[str, Any], layer_name: str, status: ModeSemanticStatus,
) -> List[str]:
    critical = status.critical_expansion
    if critical is None:
        return []
    layer = next((item for item in config["stackup"] if item["name"] == layer_name), None)
    if layer is None:
        return []
    candidates: List[Tuple[str, Mapping[str, Any]]] = []
    sweeps = config.get("sweeps", {})
    if layer.get("antipad_expansion_override") is None:
        candidates.append(("全局反焊盘净距", sweeps.get("antipad.global_expansion", {})))
    first_reference = find_nearest_reference_plane(
        config["stackup"], str(config.get("capacitor", {}).get("mount_side", "top"))
    )
    if first_reference is not None and first_reference.get("name") == layer_name:
        candidates.append(("安装面最近参考层反焊盘外扩",
                           sweeps.get("antipad.first_reference_expansion", {})))
    messages = []
    for label, sweep in candidates:
        if not sweep.get("enabled"):
            continue
        lower, upper = sorted((float(sweep["start"]), float(sweep["stop"])))
        if lower - _EPSILON <= critical <= upper + _EPSILON:
            messages.append(
                f"{label}扫描范围跨越开窗拓扑临界值，扫描过程中实际连通开窗数会变化；建议在临界值两侧拆分扫描。"
            )
    return messages
