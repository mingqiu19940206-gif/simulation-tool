"""反焊盘工作台的预览校正层。

核心界面仍在 :mod:`antipad_editor_core`；本模块只校正最终开窗边界、连通
开窗计数和外扩尺寸标注。所有轮廓均来自 ``GeometryCalculator``，不会在
GUI 中重新实现反焊盘模式几何。
"""
from __future__ import annotations

import math
from typing import Any, List, Mapping, Sequence, Tuple

import antipad_editor_core as _core
from antipad_editor_core import *  # noqa: F401,F403 - 保持既有公开接口
from geometry_v1 import polygons_overlap


PointTuple = Tuple[float, float]
SegmentTuple = Tuple[PointTuple, PointTuple]
_EPSILON = 1.0e-9


def connected_window_count(tools: Sequence[Any]) -> int:
    """返回工具体布尔并集的连通分量数；接触也视为同一实际开窗。"""
    count = len(tools)
    if count == 0:
        return 0
    parents = list(range(count))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        first, second = find(left), find(right)
        if first != second:
            parents[second] = first

    for left in range(count):
        for right in range(left + 1, count):
            if polygons_overlap(tools[left].points, tools[right].points):
                union(left, right)
    return len({find(index) for index in range(count)})


def _cross(ax: float, ay: float, bx: float, by: float) -> float:
    return ax * by - ay * bx


def _edge_parameters(a: Any, b: Any, c: Any, d: Any) -> List[float]:
    """返回线段 AB 与 CD 的交点/共线端点在 AB 上的参数。"""
    rx, ry = b.x - a.x, b.y - a.y
    sx, sy = d.x - c.x, d.y - c.y
    qx, qy = c.x - a.x, c.y - a.y
    denominator = _cross(rx, ry, sx, sy)
    if abs(denominator) > _EPSILON:
        t = _cross(qx, qy, sx, sy) / denominator
        u = _cross(qx, qy, rx, ry) / denominator
        if -_EPSILON <= t <= 1.0 + _EPSILON and -_EPSILON <= u <= 1.0 + _EPSILON:
            return [min(1.0, max(0.0, t))]
        return []
    if abs(_cross(qx, qy, rx, ry)) > _EPSILON:
        return []
    length_squared = rx * rx + ry * ry
    if length_squared <= _EPSILON:
        return []
    values = []
    for point in (c, d):
        value = ((point.x - a.x) * rx + (point.y - a.y) * ry) / length_squared
        if -_EPSILON <= value <= 1.0 + _EPSILON:
            values.append(min(1.0, max(0.0, value)))
    return values


def _inside_convex(x: float, y: float, polygon: Sequence[Any]) -> bool:
    positive = negative = False
    for index, point in enumerate(polygon):
        following = polygon[(index + 1) % len(polygon)]
        value = _cross(following.x - point.x, following.y - point.y,
                       x - point.x, y - point.y)
        positive = positive or value > _EPSILON
        negative = negative or value < -_EPSILON
        if positive and negative:
            return False
    return True


def _inside_union(x: float, y: float, polygons: Sequence[Sequence[Any]]) -> bool:
    return any(_inside_convex(x, y, polygon) for polygon in polygons)


def union_boundary_segments(polygons: Sequence[Sequence[Any]]) -> List[SegmentTuple]:
    """提取凸多边形并集的真实外边界线段。

    先按所有交点拆分原始边，再比较线段两侧是否分别位于并集内外。这样
    相互重叠或接触的工具体内部边不会被误画成最终开窗边界。
    """
    if not polygons:
        return []
    all_x = [point.x for polygon in polygons for point in polygon]
    all_y = [point.y for polygon in polygons for point in polygon]
    probe = max(max(all_x) - min(all_x), max(all_y) - min(all_y), 1.0) * 1.0e-7
    result: List[SegmentTuple] = []
    seen = set()
    for polygon in polygons:
        for edge_index, start in enumerate(polygon):
            end = polygon[(edge_index + 1) % len(polygon)]
            parameters = [0.0, 1.0]
            for other in polygons:
                for other_index, other_start in enumerate(other):
                    other_end = other[(other_index + 1) % len(other)]
                    parameters.extend(_edge_parameters(start, end, other_start, other_end))
            ordered: List[float] = []
            for value in sorted(parameters):
                if not ordered or abs(value - ordered[-1]) > _EPSILON:
                    ordered.append(value)
            dx, dy = end.x - start.x, end.y - start.y
            length = math.hypot(dx, dy)
            if length <= _EPSILON:
                continue
            nx, ny = -dy / length, dx / length
            for lower, upper in zip(ordered, ordered[1:]):
                if upper - lower <= _EPSILON:
                    continue
                first = (start.x + dx * lower, start.y + dy * lower)
                second = (start.x + dx * upper, start.y + dy * upper)
                midpoint = ((first[0] + second[0]) / 2.0, (first[1] + second[1]) / 2.0)
                left_inside = _inside_union(midpoint[0] + nx * probe, midpoint[1] + ny * probe, polygons)
                right_inside = _inside_union(midpoint[0] - nx * probe, midpoint[1] - ny * probe, polygons)
                if left_inside == right_inside:
                    continue
                rounded_first = (round(first[0], 9), round(first[1], 9))
                rounded_second = (round(second[0], 9), round(second[1], 9))
                key = tuple(sorted((rounded_first, rounded_second)))
                if key not in seen:
                    seen.add(key)
                    result.append((first, second))
    return result


def expansion_dimension_points(geometry: Any, tools: Sequence[Any], mode: str) -> Tuple[PointTuple, PointTuple, float] | None:
    """返回 E 标注的真实测量端点，独立模式沿焊盘局部长度方向。"""
    if not tools:
        return None
    tool = max(tools, key=lambda item: item.bounds.x_max)
    expansion = float(tool.metadata.get("expansion", 0.0))
    source_keys = list(tool.metadata.get("source_pads", []))
    if not source_keys:
        return None
    if mode == "independent_pad":
        pad = geometry.pad_shapes[source_keys[0]]
        center_x, center_y = map(float, pad.metadata["center"])
        angle = math.radians(float(pad.rotation_deg))
        ux, uy = math.cos(angle), math.sin(angle)
        half_length = float(pad.metadata["length"]) / 2.0
        return ((center_x + ux * half_length, center_y + uy * half_length),
                (center_x + ux * (half_length + expansion), center_y + uy * (half_length + expansion)),
                expansion)
    source_max = max(geometry.pad_shapes[key].bounds.x_max for key in source_keys)
    y_value = tool.bounds.center.y
    return (source_max, y_value), (tool.bounds.x_max, y_value), expansion


class AntipadPreview(_core.AntipadPreview):
    """显示布尔相减后的实际连通开窗，而不是误导性的工具体内边线。"""

    def _view_bounds(self) -> Tuple[float, float, float, float]:
        if getattr(self, "_via_mode", False):
            return super()._view_bounds()
        if not self.local_var.get():
            bounds = self.geometry.plane_bounds
            return bounds.x_min, bounds.x_max, bounds.y_min, bounds.y_max
        points = [point for shape in self.geometry.pad_shapes.values() for point in shape.points]
        for outline in self.geometry.body_outlines.values():
            points.extend(outline)
        current = self.layer_var.get()
        layer_names = list(self.geometry.antipads_by_layer) if self.overlay_var.get() else [current]
        expansions = [0.25]
        for layer_name in layer_names:
            for shape in self.geometry.antipads_by_layer.get(layer_name, []):
                points.extend(shape.points)
                expansions.append(float(shape.metadata.get("expansion", 0.0)))
        if not points:
            bounds = self.geometry.plane_bounds
            return bounds.x_min, bounds.x_max, bounds.y_min, bounds.y_max
        x_values, y_values = [point.x for point in points], [point.y for point in points]
        margin = max(expansions)
        return min(x_values) - margin, max(x_values) + margin, min(y_values) - margin, max(y_values) + margin

    def redraw(self) -> None:
        canvas = self.canvas
        original_polygon = canvas.create_polygon

        def polygon_without_false_internal_outline(*args: Any, **kwargs: Any) -> Any:
            if kwargs.get("fill") == "#ffffff" and kwargs.get("outline") in {"#f97316", "#dc2626"}:
                kwargs["outline"] = ""
            return original_polygon(*args, **kwargs)

        canvas.create_polygon = polygon_without_false_internal_outline  # type: ignore[assignment]
        try:
            super().redraw()
        finally:
            canvas.create_polygon = original_polygon  # type: ignore[assignment]
        if self.geometry is None or self.config is None:
            return
        if getattr(self, "_via_mode", False):
            return
        if canvas.find_withtag("compact-preview"):
            return
        for item in canvas.find_all():
            item_type = canvas.type(item)
            if item_type == "line" and canvas.itemcget(item, "fill") == "#ea580c":
                canvas.delete(item)
            elif (item_type == "text" and canvas.itemcget(item, "fill") == "#c2410c"
                  and canvas.itemcget(item, "text").startswith("E=")):
                canvas.delete(item)
        viewport = canvas_viewport(canvas)
        if viewport is None:
            return
        width, height = viewport
        x_min, x_max, y_min, y_max = self._view_bounds()
        span_x, span_y = max(x_max - x_min, 1e-9), max(y_max - y_min, 1e-9)
        # Keep this transform exactly aligned with AntipadPreview.redraw() in
        # antipad_editor_core.  The base preview reserves a title band and a
        # bottom HUD band; centering this corrected union outline against the
        # full canvas made the opening drift away from its source pad.
        title_font = ("TkDefaultFont", 10, "bold")
        title_line = font_linespace(canvas, title_font)
        plot_top = 12 + title_line + 8
        plot_bottom = height - 58
        scale = min(
            max(1, width - 55) / span_x,
            max(1, plot_bottom - plot_top) / span_y,
        ) * self.zoom
        center_x, center_y = (x_min + x_max) / 2.0, (y_min + y_max) / 2.0
        offset_x = width / 2.0 - center_x * scale
        offset_y = (plot_top + plot_bottom) / 2.0 + center_y * scale

        def xy(x_value: float, y_value: float) -> PointTuple:
            return offset_x + x_value * scale, offset_y - y_value * scale

        layer_name = self.layer_var.get()
        tools = list(self.geometry.antipads_by_layer.get(layer_name, []))
        if self.show_antipad.get() and tools:
            plane = self.geometry.plane_bounds
            outside = any(not plane.contains(tool.bounds) for tool in tools)
            boundary_color = "#dc2626" if outside else "#f97316"
            for first, second in union_boundary_segments([tool.points for tool in tools]):
                x1, y1 = xy(*first)
                x2, y2 = xy(*second)
                canvas.create_line(x1, y1, x2, y2, fill=boundary_color, width=3)
        if self.show_dimensions.get():
            dimension = expansion_dimension_points(
                self.geometry, tools, str(self.config["antipad"]["mode"])
            )
            if dimension is not None:
                first, second, expansion = dimension
                x1, y1 = xy(*first)
                x2, y2 = xy(*second)
                canvas.create_line(x1, y1, x2, y2, arrow="both", fill="#ea580c", width=2)
                canvas.create_text((x1 + x2) / 2.0, min(y1, y2) - 14,
                                   text=f"E={format_length(expansion, self.unit)} {self.unit}", fill="#c2410c",
                                   tags=("dimension-label",))
        if tools:
            connected = connected_window_count(tools)
            detail = f"工具体 {len(tools)} · 实际连通开窗 {connected}"
            if connected < len(tools):
                detail += "（存在接触/重叠）"
            # Keep this independent audit summary away from the layer/mode
            # title.  At narrow widths the two formerly shared the same top
            # line, overlapped, and the right-hand text extended off-canvas.
            detail_font = ("TkDefaultFont", 9, "bold")
            detail = ellipsize(canvas, detail, detail_font, max(80, width - 125))
            canvas.create_text(width - 12, height - 10, anchor="se", text=detail,
                               fill="#9a3412" if connected < len(tools) else "#334155",
                               font=detail_font, tags=("hud-label",))


_original_analyze_antipad = _core.analyze_antipad


def analyze_antipad(config: Mapping[str, Any], geometry: Any, layer_name: str,
                    unit: str, base_errors: Sequence[str] = (),
                    base_warnings: Sequence[str] = ()) -> Any:
    analysis = _original_analyze_antipad(
        config, geometry, layer_name, unit, base_errors, base_warnings
    )
    tools = list(geometry.antipads_by_layer.get(layer_name, []))
    analysis.values["实际连通开窗"] = str(connected_window_count(tools)) if tools else "—"
    if tools and connected_window_count(tools) < len(tools):
        message = "工具体发生接触或重叠，最终布尔相减后的实际连通开窗少于工具体数量。"
        if message not in analysis.warnings:
            analysis.warnings.append(message)
    return analysis


_core.AntipadPreview = AntipadPreview
_core.analyze_antipad = analyze_antipad


class AntipadWorkbench(_core.AntipadWorkbench):
    """使用校正后预览与摘要的工作台。"""
