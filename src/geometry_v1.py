"""表层差分线串联电容反焊盘工具的二维/三维坐标计算模块。

本模块不导入 PyAEDT。所有结果均由普通 Python 数据结构和 dataclass 表示，
因此可供 Tkinter 预览、参数检查、几何摘要和独立 PyAEDT 脚本生成器共同使用。

坐标约定：

* X 轴：信号由左向右传播；
* Y 轴：差分线横向排列，P 在正 Y，N 在负 Y；
* Z 轴：PCB 叠层厚度方向，PCB 顶面具有最大 Z；
* JSON 叠层按顶层到底层排列，整个 PCB 底面定义为 Z=0；
* P 电容使用 ``+pad_rotation``，N 电容使用 ``-pad_rotation``，
  从而保证全部几何关于 Y=0 镜像。
"""

from __future__ import annotations

import copy
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from config_manager import find_surface_signal_layer, format_length, normalize_config


EPSILON = 1.0e-9


@dataclass(frozen=True)
class Point2D:
    """二维点或二维向量。"""

    x: float
    y: float

    def translated(self, dx: float, dy: float) -> "Point2D":
        """返回平移后的新点。"""

        return Point2D(self.x + dx, self.y + dy)

    def to_list(self) -> List[float]:
        """返回适合 JSON 序列化的 ``[x, y]``。"""

        return [self.x, self.y]


@dataclass(frozen=True)
class Bounds2D:
    """轴对齐二维包围盒。"""

    x_min: float
    x_max: float
    y_min: float
    y_max: float

    @property
    def width(self) -> float:
        """包围盒 X 方向长度。"""

        return self.x_max - self.x_min

    @property
    def height(self) -> float:
        """包围盒 Y 方向长度。"""

        return self.y_max - self.y_min

    @property
    def center(self) -> Point2D:
        """包围盒中心。"""

        return Point2D(
            (self.x_min + self.x_max) / 2.0,
            (self.y_min + self.y_max) / 2.0,
        )

    def expanded(
        self,
        left: float,
        right: Optional[float] = None,
        bottom: Optional[float] = None,
        top: Optional[float] = None,
    ) -> "Bounds2D":
        """按四个方向扩展包围盒。

        只传入 ``left`` 时执行四边等量扩展。
        """

        right_value = left if right is None else right
        bottom_value = left if bottom is None else bottom
        top_value = left if top is None else top
        return Bounds2D(
            self.x_min - left,
            self.x_max + right_value,
            self.y_min - bottom_value,
            self.y_max + top_value,
        )

    def contains(self, other: "Bounds2D", tolerance: float = EPSILON) -> bool:
        """检查当前包围盒是否完整包含另一个包围盒。"""

        return (
            self.x_min <= other.x_min + tolerance
            and self.x_max >= other.x_max - tolerance
            and self.y_min <= other.y_min + tolerance
            and self.y_max >= other.y_max - tolerance
        )

    def to_dict(self) -> Dict[str, float]:
        """转换成普通字典。"""

        return asdict(self)


@dataclass(frozen=True)
class LayerZPosition:
    """PCB单层的Z坐标范围。"""

    index: int
    name: str
    layer_type: str
    thickness: float
    z_bottom: float
    z_top: float
    z_center: float
    material: str
    is_reference_plane: bool
    enable_antipad: bool
    antipad_expansion_override: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        """转换成普通字典。"""

        return asdict(self)


@dataclass
class Shape2D:
    """可拉伸成HFSS实体的二维闭合多边形。"""

    name: str
    category: str
    signal: Optional[str]
    points: List[Point2D]
    z_bottom: float
    thickness: float
    material: str
    centerline: List[Point2D] = field(default_factory=list)
    rotation_deg: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def bounds(self) -> Bounds2D:
        """返回形状的轴对齐包围盒。"""

        return bounds_from_points(self.points)

    def to_dict(self) -> Dict[str, Any]:
        """转换成可直接写入 JSON 或生成脚本的字典。"""

        return {
            "name": self.name,
            "category": self.category,
            "signal": self.signal,
            "points": [point.to_list() for point in self.points],
            "z_bottom": self.z_bottom,
            "thickness": self.thickness,
            "material": self.material,
            "centerline": [point.to_list() for point in self.centerline],
            "rotation_deg": self.rotation_deg,
            "bounds": self.bounds.to_dict(),
            "metadata": copy.deepcopy(self.metadata),
        }


@dataclass
class GeometryResult:
    """一次完整几何计算的结果。"""

    units: str
    stackup: List[LayerZPosition]
    pcb_top_z: float
    pcb_bottom_z: float
    mount_side: str
    surface_normal: Tuple[float, float, float]
    signal_layer_name: str
    signal_z_bottom: float
    signal_z_top: float
    component_outward_z: float
    surface_soldermask_layers: List[str]
    trace_z_bottom: float
    trace_z_top: float
    trace_centers: Dict[str, float]
    capacitor_centers: Dict[str, Point2D]
    pad_centers: Dict[str, Point2D]
    pad_connection_points: Dict[str, Point2D]
    transition_points: Dict[str, Dict[str, Point2D]]
    trace_centerline_paths: Dict[str, Dict[str, List[Point2D]]]
    transition_nominal_offset: float
    transition_nominal_real_length: float
    signal_shapes: Dict[str, List[Shape2D]]
    conductor_groups: Dict[str, List[Shape2D]]
    pad_shapes: Dict[str, Shape2D]
    bridge_shapes: Dict[str, Shape2D]
    capacitor_model_shapes: Dict[str, Shape2D]
    body_outlines: Dict[str, List[Point2D]]
    model_bounds: Bounds2D
    plane_bounds: Bounds2D
    antipads_by_layer: Dict[str, List[Shape2D]]
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def all_signal_shapes(self) -> List[Shape2D]:
        """按 P、N 顺序返回全部顶层信号导体。"""

        return list(self.signal_shapes.get("P", [])) + list(
            self.signal_shapes.get("N", [])
        )

    def to_dict(self) -> Dict[str, Any]:
        """转换成完全可序列化的普通字典。"""

        return {
            "units": self.units,
            "stackup": [layer.to_dict() for layer in self.stackup],
            "pcb_top_z": self.pcb_top_z,
            "pcb_bottom_z": self.pcb_bottom_z,
            "mount_side": self.mount_side,
            "surface_normal": list(self.surface_normal),
            "signal_layer_name": self.signal_layer_name,
            "signal_z_bottom": self.signal_z_bottom,
            "signal_z_top": self.signal_z_top,
            "component_outward_z": self.component_outward_z,
            "surface_soldermask_layers": list(self.surface_soldermask_layers),
            "trace_z_bottom": self.trace_z_bottom,
            "trace_z_top": self.trace_z_top,
            "trace_centers": copy.deepcopy(self.trace_centers),
            "capacitor_centers": {
                name: point.to_list()
                for name, point in self.capacitor_centers.items()
            },
            "pad_centers": {
                name: point.to_list() for name, point in self.pad_centers.items()
            },
            "pad_connection_points": {
                name: point.to_list()
                for name, point in self.pad_connection_points.items()
            },
            "transition_points": {
                name: {
                    point_name: point.to_list()
                    for point_name, point in points.items()
                }
                for name, points in self.transition_points.items()
            },
            "trace_centerline_paths": {
                signal: {
                    side: [point.to_list() for point in path]
                    for side, path in sides.items()
                }
                for signal, sides in self.trace_centerline_paths.items()
            },
            "transition_nominal_offset": self.transition_nominal_offset,
            "transition_nominal_real_length": self.transition_nominal_real_length,
            "signal_shapes": {
                signal: [shape.to_dict() for shape in shapes]
                for signal, shapes in self.signal_shapes.items()
            },
            "conductor_groups": {
                name: [shape.to_dict() for shape in shapes]
                for name, shapes in self.conductor_groups.items()
            },
            "pad_shapes": {
                name: shape.to_dict() for name, shape in self.pad_shapes.items()
            },
            "bridge_shapes": {
                name: shape.to_dict() for name, shape in self.bridge_shapes.items()
            },
            "capacitor_model_shapes": {
                name: shape.to_dict()
                for name, shape in self.capacitor_model_shapes.items()
            },
            "body_outlines": {
                name: [point.to_list() for point in points]
                for name, points in self.body_outlines.items()
            },
            "model_bounds": self.model_bounds.to_dict(),
            "plane_bounds": self.plane_bounds.to_dict(),
            "antipads_by_layer": {
                layer_name: [shape.to_dict() for shape in shapes]
                for layer_name, shapes in self.antipads_by_layer.items()
            },
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


def rotate_local_point_to_global(
    center: Point2D, local_point: Point2D, rotation_deg: float
) -> Point2D:
    """使用标准二维旋转公式把局部坐标转换到全局坐标。

    ``rotation_deg`` 使用角度制；正角度为从 +X 朝 +Y 逆时针旋转。
    """

    theta = math.radians(rotation_deg)
    cos_theta = math.cos(theta)
    sin_theta = math.sin(theta)
    return Point2D(
        center.x + local_point.x * cos_theta - local_point.y * sin_theta,
        center.y + local_point.x * sin_theta + local_point.y * cos_theta,
    )


def calculate_pad_local_coordinates(
    pad_length: float, pad_width: float
) -> List[Point2D]:
    """返回以焊盘中心为原点的局部矩形四角。

    局部 X 对应 ``pad_length``，局部 Y 对应 ``pad_width``。
    """

    half_length = pad_length / 2.0
    half_width = pad_width / 2.0
    return [
        Point2D(-half_length, -half_width),
        Point2D(half_length, -half_width),
        Point2D(half_length, half_width),
        Point2D(-half_length, half_width),
    ]


def rectangle_points(
    center: Point2D, length: float, width: float, rotation_deg: float = 0.0
) -> List[Point2D]:
    """创建旋转矩形的四个全局坐标角点。"""

    return [
        rotate_local_point_to_global(center, point, rotation_deg)
        for point in calculate_pad_local_coordinates(length, width)
    ]


def bounds_from_points(points: Sequence[Point2D]) -> Bounds2D:
    """根据至少一个二维点计算轴对齐包围盒。"""

    if not points:
        raise ValueError("计算包围盒时至少需要一个点。")
    return Bounds2D(
        min(point.x for point in points),
        max(point.x for point in points),
        min(point.y for point in points),
        max(point.y for point in points),
    )


def combine_bounds(bounds: Iterable[Bounds2D]) -> Bounds2D:
    """合并多个包围盒。"""

    items = list(bounds)
    if not items:
        raise ValueError("合并包围盒时至少需要一个包围盒。")
    return Bounds2D(
        min(item.x_min for item in items),
        max(item.x_max for item in items),
        min(item.y_min for item in items),
        max(item.y_max for item in items),
    )


def segment_polygon(
    start: Point2D,
    end: Point2D,
    width: float,
    overlap_start: float = 0.0,
    overlap_end: float = 0.0,
) -> List[Point2D]:
    """为中心线段创建恒定真实宽度的矩形多边形。

    两端可沿中心线方向分别外延，用于保证布尔合并时不是点接触或线接触。
    """

    dx = end.x - start.x
    dy = end.y - start.y
    length = math.hypot(dx, dy)
    if length <= EPSILON:
        raise ValueError("不能为零长度中心线创建导体多边形。")

    unit_x = dx / length
    unit_y = dy / length
    normal_x = -unit_y
    normal_y = unit_x
    half_width = width / 2.0
    extended_start = Point2D(
        start.x - overlap_start * unit_x,
        start.y - overlap_start * unit_y,
    )
    extended_end = Point2D(
        end.x + overlap_end * unit_x,
        end.y + overlap_end * unit_y,
    )
    return [
        Point2D(
            extended_start.x - half_width * normal_x,
            extended_start.y - half_width * normal_y,
        ),
        Point2D(
            extended_end.x - half_width * normal_x,
            extended_end.y - half_width * normal_y,
        ),
        Point2D(
            extended_end.x + half_width * normal_x,
            extended_end.y + half_width * normal_y,
        ),
        Point2D(
            extended_start.x + half_width * normal_x,
            extended_start.y + half_width * normal_y,
        ),
    ]


def _line_intersection(
    first_point: Point2D,
    first_direction: Point2D,
    second_point: Point2D,
    second_direction: Point2D,
) -> Optional[Point2D]:
    """Return the intersection of two infinite directed lines."""

    denominator = (
        first_direction.x * second_direction.y
        - first_direction.y * second_direction.x
    )
    if abs(denominator) <= EPSILON:
        return None
    delta = Point2D(
        second_point.x - first_point.x,
        second_point.y - first_point.y,
    )
    distance = (
        delta.x * second_direction.y - delta.y * second_direction.x
    ) / denominator
    return Point2D(
        first_point.x + distance * first_direction.x,
        first_point.y + distance * first_direction.y,
    )


def continuous_strip_polygon(
    centerline: Sequence[Point2D],
    width: float,
    join_style: str = "limited_miter",
    miter_limit: float = 2.0,
) -> List[Point2D]:
    """Offset one complete centerline into a constant-width simple polygon.

    Unlike a union of independently extended rectangles, this constructs both
    strip boundaries from adjacent-line intersections.  A turn therefore has
    a real miter/bevel join and cannot contain the triangular outside-corner
    void caused by segment-only overlap.
    """

    cleaned: List[Point2D] = []
    for point in centerline:
        if not cleaned or math.hypot(
            point.x - cleaned[-1].x, point.y - cleaned[-1].y
        ) > EPSILON:
            cleaned.append(point)
    if len(cleaned) < 2:
        raise ValueError("连续走线中心线至少需要两个不同的点。")
    if width <= EPSILON:
        raise ValueError("连续走线宽度必须大于零。")
    if join_style not in {"miter", "limited_miter", "bevel"}:
        raise ValueError(f"不支持的走线转角连接方式：{join_style}")
    if miter_limit < 1.0:
        raise ValueError("miter_limit 必须不小于 1。")

    directions: List[Point2D] = []
    normals: List[Point2D] = []
    for first, second in zip(cleaned, cleaned[1:]):
        dx, dy = second.x - first.x, second.y - first.y
        length = math.hypot(dx, dy)
        direction = Point2D(dx / length, dy / length)
        directions.append(direction)
        normals.append(Point2D(-direction.y, direction.x))

    half_width = width / 2.0

    def offset(point: Point2D, normal: Point2D, side: float) -> Point2D:
        return Point2D(
            point.x + side * half_width * normal.x,
            point.y + side * half_width * normal.y,
        )

    def side_points(side: float) -> List[Point2D]:
        boundary = [offset(cleaned[0], normals[0], side)]
        for index in range(1, len(cleaned) - 1):
            vertex = cleaned[index]
            incoming, outgoing = directions[index - 1], directions[index]
            first = offset(vertex, normals[index - 1], side)
            second = offset(vertex, normals[index], side)
            cross = incoming.x * outgoing.y - incoming.y * outgoing.x
            if abs(cross) <= EPSILON:
                boundary.append(second)
                continue
            intersection = _line_intersection(first, incoming, second, outgoing)
            outer_side = (cross > 0.0 and side > 0.0) or (
                cross < 0.0 and side < 0.0
            )
            miter_ratio = (
                math.hypot(intersection.x - vertex.x, intersection.y - vertex.y)
                / half_width
                if intersection is not None
                else float("inf")
            )
            bevel = outer_side and (
                join_style == "bevel"
                or (join_style == "limited_miter" and miter_ratio > miter_limit)
            )
            if bevel:
                boundary.extend((first, second))
            elif intersection is not None:
                boundary.append(intersection)
            else:
                boundary.append(second)
        boundary.append(offset(cleaned[-1], normals[-1], side))
        return boundary

    left = side_points(1.0)
    right = side_points(-1.0)
    polygon = left + list(reversed(right))
    if abs(sum(
        polygon[index].x * polygon[(index + 1) % len(polygon)].y
        - polygon[(index + 1) % len(polygon)].x * polygon[index].y
        for index in range(len(polygon))
    )) <= EPSILON:
        raise ValueError("连续走线偏置后形成零面积多边形。")
    return polygon


def calculate_stackup_z_positions(
    config: Mapping[str, Any],
) -> List[LayerZPosition]:
    """按“顶层到下层”的JSON顺序累计每层Z坐标。

    PCB底面固定为 Z=0，顶面为全部层厚之和。相邻层共用边界，不产生间隙。
    """

    stackup = config["stackup"]
    total_thickness = sum(float(layer["thickness"]) for layer in stackup)
    current_top = total_thickness
    positions: List[LayerZPosition] = []

    for index, layer in enumerate(stackup):
        thickness = float(layer["thickness"])
        z_top = current_top
        z_bottom = z_top - thickness
        positions.append(
            LayerZPosition(
                index=index,
                name=str(layer["name"]),
                layer_type=str(layer["type"]),
                thickness=thickness,
                z_bottom=z_bottom,
                z_top=z_top,
                z_center=(z_bottom + z_top) / 2.0,
                material=str(layer["material"]),
                is_reference_plane=bool(layer.get("is_reference_plane", False)),
                enable_antipad=bool(layer.get("enable_antipad", False)),
                antipad_expansion_override=(
                    None
                    if layer.get("antipad_expansion_override") is None
                    else float(layer["antipad_expansion_override"])
                ),
            )
        )
        current_top = z_bottom

    if abs(current_top) <= EPSILON:
        current_top = 0.0
    return positions


def calculate_trace_centerlines(config: Mapping[str, Any]) -> Dict[str, float]:
    """计算输入/输出平行差分线的中心Y坐标。"""

    half_pitch = float(config["trace"]["trace_pitch"]) / 2.0
    return {"P": half_pitch, "N": -half_pitch}


def calculate_transition_offset(config: Mapping[str, Any]) -> Dict[str, float]:
    """计算需求定义的名义严格45°过渡尺寸。"""

    trace_pitch = float(config["trace"]["trace_pitch"])
    cap_pitch = float(config["capacitor"]["cap_pitch"])
    delta_y = abs(cap_pitch - trace_pitch) / 2.0
    if cap_pitch > trace_pitch + EPSILON:
        direction = "contract"
    elif cap_pitch < trace_pitch - EPSILON:
        direction = "expand"
    else:
        direction = "none"
    return {
        "delta_y": delta_y,
        "transition_x": delta_y,
        "real_length": math.sqrt(2.0) * delta_y,
        "direction": direction,
    }


def calculate_capacitor_centers(
    config: Mapping[str, Any],
) -> Dict[str, Point2D]:
    """计算 C_P 和 C_N 的几何中心。"""

    capacitor = config["capacitor"]
    center_x = float(capacitor["cap_center_x"]) + float(
        capacitor.get("cap_center_x_offset", 0.0)
    )
    half_pitch = float(capacitor["cap_pitch"]) / 2.0
    return {
        "C_P": Point2D(center_x, half_pitch),
        "C_N": Point2D(center_x, -half_pitch),
    }


def _signal_rotation(config: Mapping[str, Any], signal: str) -> float:
    """返回保证P/N镜像所需的焊盘旋转角。"""

    rotation = float(config["capacitor"]["pad_rotation"])
    return rotation if signal == "P" else -rotation


def calculate_capacitor_pad_centers(
    config: Mapping[str, Any],
    capacitor_centers: Optional[Mapping[str, Point2D]] = None,
) -> Dict[str, Point2D]:
    """计算四个焊盘中心，焊盘间距沿各自局部长度方向。"""

    centers = (
        dict(capacitor_centers)
        if capacitor_centers is not None
        else calculate_capacitor_centers(config)
    )
    half_pitch = float(config["capacitor"]["pad_pitch"]) / 2.0
    result: Dict[str, Point2D] = {}

    for signal, capacitor_name in (("P", "C_P"), ("N", "C_N")):
        rotation = _signal_rotation(config, signal)
        center = centers[capacitor_name]
        result[f"{signal}_IN"] = rotate_local_point_to_global(
            center, Point2D(-half_pitch, 0.0), rotation
        )
        result[f"{signal}_OUT"] = rotate_local_point_to_global(
            center, Point2D(half_pitch, 0.0), rotation
        )
    return result


def calculate_pad_connection_points(
    config: Mapping[str, Any],
    capacitor_centers: Optional[Mapping[str, Point2D]] = None,
) -> Dict[str, Point2D]:
    """计算走线连接到四个焊盘外侧短边中心的位置。"""

    centers = (
        dict(capacitor_centers)
        if capacitor_centers is not None
        else calculate_capacitor_centers(config)
    )
    capacitor = config["capacitor"]
    outer_distance = (
        float(capacitor["pad_pitch"]) + float(capacitor["pad_length"])
    ) / 2.0
    result: Dict[str, Point2D] = {}

    for signal, capacitor_name in (("P", "C_P"), ("N", "C_N")):
        rotation = _signal_rotation(config, signal)
        center = centers[capacitor_name]
        result[f"{signal}_IN"] = rotate_local_point_to_global(
            center, Point2D(-outer_distance, 0.0), rotation
        )
        result[f"{signal}_OUT"] = rotate_local_point_to_global(
            center, Point2D(outer_distance, 0.0), rotation
        )
    return result


def _unit_direction(rotation_deg: float) -> Point2D:
    """返回指定角度的单位方向向量。"""

    theta = math.radians(rotation_deg)
    return Point2D(math.cos(theta), math.sin(theta))


def calculate_transition_points(
    config: Mapping[str, Any],
    trace_centers: Optional[Mapping[str, float]] = None,
    pad_connection_points: Optional[Mapping[str, Point2D]] = None,
) -> Dict[str, Dict[str, Point2D]]:
    """Return canonical escape anchors used by preview, validation and HFSS.

    ``direct_45`` starts at the rotated outer short-edge centre.  Only the
    explicit compatibility mode ``lead_then_45`` moves that anchor by a pad
    lead.  S-bend sampling is performed by :func:`calculate_trace_centerline_paths`.
    """

    trace_y_values = (
        dict(trace_centers)
        if trace_centers is not None
        else calculate_trace_centerlines(config)
    )
    connections = (
        dict(pad_connection_points)
        if pad_connection_points is not None
        else calculate_pad_connection_points(config)
    )
    trace = config["trace"]
    mode = str(trace.get("pad_escape_mode", "direct_45"))
    result: Dict[str, Dict[str, Point2D]] = {}

    for signal in ("P", "N"):
        direction = _unit_direction(_signal_rotation(config, signal))
        input_connection = connections[f"{signal}_IN"]
        output_connection = connections[f"{signal}_OUT"]

        lead_in_length = float(trace["pad_lead_in_length"]) if mode == "lead_then_45" else 0.0
        lead_out_length = float(trace["pad_lead_out_length"]) if mode == "lead_then_45" else 0.0
        lead_in_start = Point2D(
            input_connection.x - lead_in_length * direction.x,
            input_connection.y - lead_in_length * direction.y,
        )
        lead_out_end = Point2D(
            output_connection.x + lead_out_length * direction.x,
            output_connection.y + lead_out_length * direction.y,
        )

        trace_y = float(trace_y_values[signal])
        input_delta_y = lead_in_start.y - trace_y
        output_delta_y = trace_y - lead_out_end.y
        if mode == "smooth_s_bend":
            input_transition_start = Point2D(
                lead_in_start.x - float(trace["transition_length"]), trace_y
            )
        else:
            input_transition_start = Point2D(
                lead_in_start.x - abs(input_delta_y), trace_y
            )
        if mode == "smooth_s_bend":
            output_transition_end = Point2D(
                lead_out_end.x + float(trace["transition_length"]), trace_y
            )
        else:
            output_transition_end = Point2D(
                lead_out_end.x + abs(output_delta_y), trace_y
            )
        input_start = Point2D(
            input_transition_start.x - float(trace["input_straight_length"]),
            trace_y,
        )
        output_end = Point2D(
            output_transition_end.x + float(trace["output_straight_length"]),
            trace_y,
        )

        result[signal] = {
            "input_start": input_start,
            "input_straight_end": input_transition_start,
            "transition_in_start": input_transition_start,
            "transition_in_end": lead_in_start,
            "lead_in_start": lead_in_start,
            "lead_in_end": input_connection,
            "pad_in_connection": input_connection,
            "pad_out_connection": output_connection,
            "lead_out_start": output_connection,
            "lead_out_end": lead_out_end,
            "transition_out_start": lead_out_end,
            "transition_out_end": output_transition_end,
            "output_straight_start": output_transition_end,
            "output_end": output_end,
        }
    return result


def _clean_path(points: Sequence[Point2D]) -> List[Point2D]:
    """Remove adjacent duplicates without replacing zero by a small number."""

    cleaned: List[Point2D] = []
    for point in points:
        if not cleaned or math.hypot(
            point.x - cleaned[-1].x, point.y - cleaned[-1].y
        ) > EPSILON:
            cleaned.append(point)
    if len(cleaned) < 2:
        raise ValueError("走线中心线路径退化，至少需要两个不同点。")
    return cleaned


def _cubic_bezier_points(
    start: Point2D,
    end: Point2D,
    start_tangent: Point2D,
    end_tangent: Point2D,
    samples: int = 64,
) -> List[Point2D]:
    """Sample a cubic Bezier whose endpoint derivatives are prescribed."""

    chord = math.hypot(end.x - start.x, end.y - start.y)
    if chord <= EPSILON:
        return [start]
    handle = chord / 3.0
    control_1 = Point2D(
        start.x + handle * start_tangent.x,
        start.y + handle * start_tangent.y,
    )
    control_2 = Point2D(
        end.x - handle * end_tangent.x,
        end.y - handle * end_tangent.y,
    )
    result: List[Point2D] = []
    for index in range(samples + 1):
        t = index / samples
        one_minus = 1.0 - t
        result.append(Point2D(
            one_minus ** 3 * start.x
            + 3.0 * one_minus ** 2 * t * control_1.x
            + 3.0 * one_minus * t ** 2 * control_2.x
            + t ** 3 * end.x,
            one_minus ** 3 * start.y
            + 3.0 * one_minus ** 2 * t * control_1.y
            + 3.0 * one_minus * t ** 2 * control_2.y
            + t ** 3 * end.y,
        ))
    return _clean_path(result)


def calculate_trace_centerline_paths(
    config: Mapping[str, Any],
    transition_points: Optional[Mapping[str, Mapping[str, Point2D]]] = None,
) -> Dict[str, Dict[str, List[Point2D]]]:
    """Build the complete input/output centerline used by every geometry consumer."""

    points_by_signal = (
        transition_points
        if transition_points is not None
        else calculate_transition_points(config)
    )
    trace = config["trace"]
    mode = str(trace.get("pad_escape_mode", "direct_45"))
    overlap = float(config["geometry"]["overlap"])
    result: Dict[str, Dict[str, List[Point2D]]] = {}
    for signal in ("P", "N"):
        points = points_by_signal[signal]
        local_direction = _unit_direction(_signal_rotation(config, signal))
        input_contact = points["pad_in_connection"]
        output_contact = points["pad_out_connection"]
        input_overlap_direction = local_direction
        output_overlap_direction = local_direction
        if mode == "direct_45" or (
            mode == "lead_then_45"
            and float(trace["pad_lead_in_length"]) <= EPSILON
        ):
            input_dx = input_contact.x - points["transition_in_start"].x
            input_dy = input_contact.y - points["transition_in_start"].y
            input_distance = math.hypot(input_dx, input_dy)
            if input_distance > EPSILON:
                input_overlap_direction = Point2D(
                    input_dx / input_distance, input_dy / input_distance
                )
        if mode == "direct_45" or (
            mode == "lead_then_45"
            and float(trace["pad_lead_out_length"]) <= EPSILON
        ):
            output_dx = points["transition_out_end"].x - output_contact.x
            output_dy = points["transition_out_end"].y - output_contact.y
            output_distance = math.hypot(output_dx, output_dy)
            if output_distance > EPSILON:
                output_overlap_direction = Point2D(
                    output_dx / output_distance, output_dy / output_distance
                )
        input_inside = Point2D(
            input_contact.x + overlap * input_overlap_direction.x,
            input_contact.y + overlap * input_overlap_direction.y,
        )
        output_inside = Point2D(
            output_contact.x - overlap * output_overlap_direction.x,
            output_contact.y - overlap * output_overlap_direction.y,
        )
        if mode == "smooth_s_bend":
            input_anchor = points["transition_in_end"]
            input_horizontal = points["transition_in_start"]
            input_outward = Point2D(-local_direction.x, -local_direction.y)
            output_anchor = points["transition_out_start"]
            output_horizontal = points["transition_out_end"]
            input_curve_outward = _cubic_bezier_points(
                input_anchor, input_horizontal, input_outward, Point2D(-1.0, 0.0)
            )
            input_path = [points["input_start"]] + list(reversed(input_curve_outward)) + [input_inside]
            output_curve = _cubic_bezier_points(
                output_anchor, output_horizontal, local_direction, Point2D(1.0, 0.0)
            )
            output_path = [output_inside] + output_curve + [points["output_end"]]
        else:
            input_route = points["transition_in_start"]
            if math.hypot(
                input_route.x - points["lead_in_start"].x,
                input_route.y - points["lead_in_start"].y,
            ) <= EPSILON:
                input_route = Point2D(
                    (points["input_start"].x + points["lead_in_start"].x) / 2.0,
                    (points["input_start"].y + points["lead_in_start"].y) / 2.0,
                )
            output_route = points["transition_out_end"]
            if math.hypot(
                output_route.x - points["lead_out_end"].x,
                output_route.y - points["lead_out_end"].y,
            ) <= EPSILON:
                output_route = Point2D(
                    (points["lead_out_end"].x + points["output_end"].x) / 2.0,
                    (points["lead_out_end"].y + points["output_end"].y) / 2.0,
                )
            input_path = [
                points["input_start"], input_route,
                points["lead_in_start"], input_contact, input_inside,
            ]
            output_path = [
                output_inside, output_contact, points["lead_out_end"],
                output_route, points["output_end"],
            ]
        result[signal] = {
            "input": _clean_path(input_path),
            "output": _clean_path(output_path),
        }
    return result


def _surface_signal_layer(
    config: Mapping[str, Any], layers: Sequence[LayerZPosition],
) -> LayerZPosition:
    """Return the real exterior signal copper selected by capacitor.mount_side."""

    mount_side = str(config.get("capacitor", {}).get("mount_side", "top"))
    if mount_side not in {"top", "bottom"}:
        raise ValueError(
            "capacitor.mount_side 只能为 top 或 bottom；当前值为 {!r}。".format(mount_side)
        )
    selected = find_surface_signal_layer(layers, mount_side)
    if selected is None:
        raise ValueError("叠层中没有 signal 层。")
    outside = layers[:selected.index] if mount_side == "top" else layers[selected.index + 1:]
    if any(layer.layer_type != "soldermask" for layer in outside):
        side_label = "顶层" if mount_side == "top" else "底层"
        raise ValueError(
            f"{side_label}安装找不到有效外层 signal 铜：所选 signal 层外侧只能存在 soldermask。"
            "请修正叠层或切换安装面。"
        )
    return selected


def _top_signal_layer(layers: Sequence[LayerZPosition]) -> LayerZPosition:
    """Compatibility helper for callers that explicitly require the top surface."""

    return _surface_signal_layer({"capacitor": {"mount_side": "top"}}, layers)


def _create_segment_shape(
    name: str,
    category: str,
    signal: str,
    start: Point2D,
    end: Point2D,
    width: float,
    z_bottom: float,
    thickness: float,
    material: str,
    overlap: float,
) -> Optional[Shape2D]:
    """创建带两端重叠的线段导体；零长度时返回 ``None``。"""

    length = math.hypot(end.x - start.x, end.y - start.y)
    if length <= EPSILON:
        return None
    points = segment_polygon(
        start,
        end,
        width,
        overlap_start=overlap,
        overlap_end=overlap,
    )
    angle = math.degrees(math.atan2(end.y - start.y, end.x - start.x))
    return Shape2D(
        name=name,
        category=category,
        signal=signal,
        points=points,
        z_bottom=z_bottom,
        thickness=thickness,
        material=material,
        centerline=[start, end],
        rotation_deg=angle,
        metadata={"centerline_length": length, "width": width},
    )


def _conductor_material(config: Mapping[str, Any]) -> str:
    """根据导体模式返回生成HFSS对象时使用的材料名。"""

    materials = config["materials"]
    mode = materials["conductor_mode"]
    if mode == "pec":
        return "pec"
    if mode == "custom":
        return str(materials["custom_conductor_name"])
    return str(materials.get("conductor_material", "copper"))


def _create_pad_shapes(
    config: Mapping[str, Any],
    stackup: Sequence[LayerZPosition],
    pad_centers: Mapping[str, Point2D],
) -> Dict[str, Shape2D]:
    """创建四个旋转PCB焊盘形状。"""

    capacitor = config["capacitor"]
    top_layer = _surface_signal_layer(config, stackup)
    material = _conductor_material(config)
    pad_length = float(capacitor["pad_length"])
    pad_width = float(capacitor["pad_width"])
    names = {
        "P_IN": "CAP_P_PAD_IN",
        "P_OUT": "CAP_P_PAD_OUT",
        "N_IN": "CAP_N_PAD_IN",
        "N_OUT": "CAP_N_PAD_OUT",
    }
    result: Dict[str, Shape2D] = {}

    for key, object_name in names.items():
        signal = key[0]
        rotation = _signal_rotation(config, signal)
        result[key] = Shape2D(
            name=object_name,
            category="pad",
            signal=signal,
            points=rectangle_points(
                pad_centers[key], pad_length, pad_width, rotation
            ),
            z_bottom=top_layer.z_bottom,
            thickness=top_layer.thickness,
            material=material,
            rotation_deg=rotation,
            metadata={
                "center": pad_centers[key].to_list(),
                "length": pad_length,
                "width": pad_width,
            },
        )
    return result


def calculate_bridge_geometry(
    config: Mapping[str, Any],
    stackup: Optional[Sequence[LayerZPosition]] = None,
    capacitor_centers: Optional[Mapping[str, Point2D]] = None,
) -> Dict[str, Shape2D]:
    """计算 C_P、C_N 两个顶层理想导体桥。

    桥接矩形与焊盘局部长度方向一致，并使用配置中的实际 ``bridge_length``。
    自动模式下该长度已由配置管理器归一化为 ``pad_gap + 2×overlap``。
    """

    if str(config.get("capacitor_model", {}).get("mode", "ideal_bridge")) != "ideal_bridge":
        return {}
    layers = (
        list(stackup)
        if stackup is not None
        else calculate_stackup_z_positions(config)
    )
    centers = (
        dict(capacitor_centers)
        if capacitor_centers is not None
        else calculate_capacitor_centers(config)
    )
    top_layer = _surface_signal_layer(config, layers)
    capacitor = config["capacitor"]
    material = (
        str(capacitor.get("bridge_material", "")).strip()
        or _conductor_material(config)
    )
    if material == "copper":
        material = _conductor_material(config)
    result: Dict[str, Shape2D] = {}

    for signal, capacitor_name in (("P", "C_P"), ("N", "C_N")):
        rotation = _signal_rotation(config, signal)
        object_name = f"CAP_{signal}_BRIDGE"
        result[signal] = Shape2D(
            name=object_name,
            category="bridge",
            signal=signal,
            points=rectangle_points(
                centers[capacitor_name],
                float(capacitor["bridge_length"]),
                float(capacitor["bridge_width"]),
                rotation,
            ),
            z_bottom=top_layer.z_bottom,
            thickness=top_layer.thickness,
            material=material,
            rotation_deg=rotation,
            metadata={
                "center": centers[capacitor_name].to_list(),
                "length": float(capacitor["bridge_length"]),
                "width": float(capacitor["bridge_width"]),
            },
        )
    return result


def _create_body_outlines(
    config: Mapping[str, Any],
    capacitor_centers: Mapping[str, Point2D],
) -> Dict[str, List[Point2D]]:
    """创建仅供GUI预览使用的0402本体虚线轮廓。"""

    capacitor = config["capacitor"]
    outlines: Dict[str, List[Point2D]] = {}
    for signal, capacitor_name in (("P", "C_P"), ("N", "C_N")):
        outlines[capacitor_name] = rectangle_points(
            capacitor_centers[capacitor_name],
            float(capacitor["body_length"]),
            float(capacitor["body_width"]),
            _signal_rotation(config, signal),
        )
    return outlines


def _create_trace_shapes(
    config: Mapping[str, Any],
    stackup: Sequence[LayerZPosition],
    centerline_paths: Mapping[str, Mapping[str, Sequence[Point2D]]],
) -> Dict[str, List[Shape2D]]:
    """Create one continuous constant-width polygon on each side of each pad."""

    trace = config["trace"]
    top_layer = _surface_signal_layer(config, stackup)
    material = _conductor_material(config)
    width = float(trace["trace_width"])
    join_style = str(config["geometry"].get("trace_join", "limited_miter"))
    miter_limit = float(config["geometry"].get("miter_limit", 2.0))
    result: Dict[str, List[Shape2D]] = {"P": [], "N": []}

    for signal in ("P", "N"):
        paths = (
            ("IN", "trace_in", centerline_paths[signal]["input"]),
            ("OUT", "trace_out", centerline_paths[signal]["output"]),
        )
        for side, category, centerline in paths:
            cleaned = _clean_path(centerline)
            result[signal].append(Shape2D(
                name=f"SIG_{signal}_{side}_TRACE",
                category=category,
                signal=signal,
                points=continuous_strip_polygon(cleaned, width, join_style, miter_limit),
                z_bottom=top_layer.z_bottom,
                thickness=top_layer.thickness,
                material=material,
                centerline=cleaned,
                metadata={
                    "width": width,
                    "join_style": join_style,
                    "miter_limit": miter_limit,
                    "continuous_centerline": True,
                    "pad_contact_overlap": float(config["geometry"]["overlap"]),
                    "pad_escape_mode": str(trace.get("pad_escape_mode", "direct_45")),
                    "centerline_length": sum(
                        math.hypot(second.x-first.x, second.y-first.y)
                        for first, second in zip(cleaned, cleaned[1:])
                    ),
                },
            ))
    return result


def _create_capacitor_model_shapes(
    config: Mapping[str, Any],
    stackup: Sequence[LayerZPosition],
    capacitor_centers: Mapping[str, Point2D],
) -> Dict[str, Shape2D]:
    """Create rotated zero-thickness sheets spanning the real inner-pad gap."""

    mode = str(config.get("capacitor_model", {}).get("mode", "ideal_bridge"))
    if mode not in {"lumped_rlc", "sparameter"}:
        return {}
    top_layer = _surface_signal_layer(config, stackup)
    model_z = top_layer.z_top if str(config["capacitor"].get("mount_side", "top")) == "top" else top_layer.z_bottom
    gap = float(config["capacitor"]["pad_gap"])
    sheet_width = float(config["capacitor_model"]["sheet_width"])
    result: Dict[str, Shape2D] = {}
    for signal, capacitor_name in (("P", "C_P"), ("N", "C_N")):
        center = capacitor_centers[capacitor_name]
        rotation = _signal_rotation(config, signal)
        direction = _unit_direction(rotation)
        start = Point2D(center.x-gap*direction.x/2.0, center.y-gap*direction.y/2.0)
        end = Point2D(center.x+gap*direction.x/2.0, center.y+gap*direction.y/2.0)
        prefix = "RLC" if mode == "lumped_rlc" else "SPARAM"
        result[signal] = Shape2D(
            name=f"CAP_{prefix}_{signal}_SHEET",
            category=f"{mode}_sheet",
            signal=signal,
            points=rectangle_points(center, gap, sheet_width, rotation),
            z_bottom=model_z,
            thickness=0.0,
            material="vacuum",
            centerline=[start, end],
            rotation_deg=rotation,
            metadata={
                "mode": mode,
                "sheet_width": sheet_width,
                "gap": gap,
                "integration_line_start": [start.x, start.y, model_z],
                "integration_line_end": [end.x, end.y, model_z],
                "surface_normal": [0.0, 0.0, 1.0 if config["capacitor"].get("mount_side", "top") == "top" else -1.0],
                "input_conductor": f"SIGNAL_{signal}_IN",
                "output_conductor": f"SIGNAL_{signal}_OUT",
                "port_mapping": "1=input_pad,2=output_pad",
            },
        )
    return result


def calculate_model_bounds(
    shapes: Iterable[Shape2D],
) -> Bounds2D:
    """计算全部顶层信号实体的轴对齐包围盒。"""

    shape_list = list(shapes)
    if not shape_list:
        raise ValueError("模型至少需要一个信号形状。")
    return combine_bounds(shape.bounds for shape in shape_list)


def calculate_plane_bounds(
    config: Mapping[str, Any], model_bounds: Bounds2D
) -> Bounds2D:
    """根据完整信号包围盒和三个方向边距计算参考平面边界。"""

    plane = config["plane"]
    return Bounds2D(
        model_bounds.x_min - float(plane["margin_input"]),
        model_bounds.x_max + float(plane["margin_output"]),
        model_bounds.y_min - float(plane["margin_y"]),
        model_bounds.y_max + float(plane["margin_y"]),
    )


def _axis_aligned_rectangle(bounds: Bounds2D) -> List[Point2D]:
    """把轴对齐包围盒转换成顺时针闭合面的四个角点。"""

    return [
        Point2D(bounds.x_min, bounds.y_min),
        Point2D(bounds.x_max, bounds.y_min),
        Point2D(bounds.x_max, bounds.y_max),
        Point2D(bounds.x_min, bounds.y_max),
    ]


def calculate_antipad_bounds(
    config: Mapping[str, Any],
    stackup: Sequence[LayerZPosition],
    pad_shapes: Mapping[str, Shape2D],
) -> Dict[str, List[Shape2D]]:
    """为启用反焊盘的参考平面层创建工具体几何。

    ``independent_pad`` 保持焊盘旋转角；
    ``merged_per_capacitor`` 使用每个电容两个旋转焊盘的实际包围盒；
    ``merged_all`` 使用四个旋转焊盘的整体实际包围盒。
    """

    mode = str(config["antipad"]["mode"])
    global_expansion = float(config["antipad"]["global_expansion"])
    result: Dict[str, List[Shape2D]] = {}

    for layer in stackup:
        if (
            layer.layer_type != "plane"
            or not layer.is_reference_plane
            or not layer.enable_antipad
        ):
            continue
        expansion = (
            layer.antipad_expansion_override
            if layer.antipad_expansion_override is not None
            else global_expansion
        )
        tools: List[Shape2D] = []

        if mode == "independent_pad":
            for pad_key in ("P_IN", "P_OUT", "N_IN", "N_OUT"):
                pad = pad_shapes[pad_key]
                pad_center = Point2D(
                    float(pad.metadata["center"][0]),
                    float(pad.metadata["center"][1]),
                )
                length = float(pad.metadata["length"]) + 2.0 * expansion
                width = float(pad.metadata["width"]) + 2.0 * expansion
                tools.append(
                    Shape2D(
                        name=f"ANTIPAD_{pad_key}_{layer.name}",
                        category="antipad_tool",
                        signal=pad.signal,
                        points=rectangle_points(
                            pad_center, length, width, pad.rotation_deg
                        ),
                        z_bottom=layer.z_bottom,
                        thickness=layer.thickness,
                        material="vacuum",
                        rotation_deg=pad.rotation_deg,
                        metadata={
                            "mode": mode,
                            "layer": layer.name,
                            "expansion": expansion,
                            "source_pads": [pad_key],
                        },
                    )
                )
        elif mode == "merged_per_capacitor":
            for signal in ("P", "N"):
                source_keys = [f"{signal}_IN", f"{signal}_OUT"]
                bounds = combine_bounds(
                    pad_shapes[key].bounds for key in source_keys
                ).expanded(expansion)
                tools.append(
                    Shape2D(
                        name=f"ANTIPAD_{signal}_MERGED_{layer.name}",
                        category="antipad_tool",
                        signal=signal,
                        points=_axis_aligned_rectangle(bounds),
                        z_bottom=layer.z_bottom,
                        thickness=layer.thickness,
                        material="vacuum",
                        metadata={
                            "mode": mode,
                            "layer": layer.name,
                            "expansion": expansion,
                            "source_pads": source_keys,
                        },
                    )
                )
        elif mode == "merged_all":
            source_keys = ["P_IN", "P_OUT", "N_IN", "N_OUT"]
            bounds = combine_bounds(
                pad_shapes[key].bounds for key in source_keys
            ).expanded(expansion)
            tools.append(
                Shape2D(
                    name=f"ANTIPAD_ALL_{layer.name}",
                    category="antipad_tool",
                    signal=None,
                    points=_axis_aligned_rectangle(bounds),
                    z_bottom=layer.z_bottom,
                    thickness=layer.thickness,
                    material="vacuum",
                    metadata={
                        "mode": mode,
                        "layer": layer.name,
                        "expansion": expansion,
                        "source_pads": source_keys,
                    },
                )
            )
        else:
            raise ValueError(f"不支持的反焊盘模式：{mode}")

        result[layer.name] = tools
    return result


def _projection_interval(
    polygon: Sequence[Point2D], axis: Point2D
) -> Tuple[float, float]:
    """把凸多边形投影到指定单位轴。"""

    values = [point.x * axis.x + point.y * axis.y for point in polygon]
    return min(values), max(values)


def polygons_overlap(
    first: Sequence[Point2D],
    second: Sequence[Point2D],
    tolerance: float = EPSILON,
) -> bool:
    """使用分离轴定理检查两个凸多边形是否重叠或接触。"""

    for polygon in (first, second):
        for index, point in enumerate(polygon):
            next_point = polygon[(index + 1) % len(polygon)]
            edge_x = next_point.x - point.x
            edge_y = next_point.y - point.y
            edge_length = math.hypot(edge_x, edge_y)
            if edge_length <= tolerance:
                continue
            axis = Point2D(-edge_y / edge_length, edge_x / edge_length)
            first_min, first_max = _projection_interval(first, axis)
            second_min, second_max = _projection_interval(second, axis)
            if first_max < second_min - tolerance:
                return False
            if second_max < first_min - tolerance:
                return False
    return True


def detect_pad_overlap(
    pad_shapes: Mapping[str, Shape2D],
) -> List[Tuple[str, str]]:
    """返回发生重叠或接触的非预期焊盘对象组合。"""

    overlaps: List[Tuple[str, str]] = []
    keys = list(pad_shapes)
    for first_index, first_key in enumerate(keys):
        for second_key in keys[first_index + 1 :]:
            if polygons_overlap(
                pad_shapes[first_key].points, pad_shapes[second_key].points
            ):
                overlaps.append((first_key, second_key))
    return overlaps


def detect_signal_overlap(
    positive_shapes: Sequence[Shape2D],
    negative_shapes: Sequence[Shape2D],
) -> List[Tuple[str, str]]:
    """检查正线与负线任意实体之间是否相交或接触。"""

    overlaps: List[Tuple[str, str]] = []
    for positive in positive_shapes:
        for negative in negative_shapes:
            if polygons_overlap(positive.points, negative.points):
                overlaps.append((positive.name, negative.name))
    return overlaps


def validate_45deg_geometry(
    transition_points: Mapping[str, Mapping[str, Point2D]],
    tolerance: float = 1.0e-7,
    unit: str = "mm",
) -> List[str]:
    """验证所有非零输入/输出过渡中心线是否为严格45°。"""

    errors: List[str] = []
    for signal in ("P", "N"):
        points = transition_points[signal]
        for label, start_key, end_key in (
            ("输入", "transition_in_start", "transition_in_end"),
            ("输出", "transition_out_start", "transition_out_end"),
        ):
            start = points[start_key]
            end = points[end_key]
            delta_x = abs(end.x - start.x)
            delta_y = abs(end.y - start.y)
            if delta_x <= tolerance and delta_y <= tolerance:
                continue
            if not math.isclose(delta_x, delta_y, rel_tol=0.0, abs_tol=tolerance):
                errors.append(
                    f"{signal}线{label}过渡不是严格45°："
                    f"|Δx|={format_length(delta_x, unit)} {unit}，"
                    f"|Δy|={format_length(delta_y, unit)} {unit}。"
                )
    return errors


def _validate_mirror_symmetry(
    positive_shapes: Sequence[Shape2D],
    negative_shapes: Sequence[Shape2D],
    tolerance: float = 1.0e-7,
) -> List[str]:
    """通过对象类别和镜像包围盒检查P/N几何对称性。"""

    errors: List[str] = []
    if len(positive_shapes) != len(negative_shapes):
        errors.append(
            f"P/N信号对象数量不同：P={len(positive_shapes)}，N={len(negative_shapes)}。"
        )
    for positive, negative in zip(positive_shapes, negative_shapes):
        if positive.category != negative.category:
            errors.append(
                f"{positive.name} 与 {negative.name} 的拓扑类别不一致。"
            )
            continue
        p_bounds = positive.bounds
        n_bounds = negative.bounds
        symmetric = (
            math.isclose(p_bounds.x_min, n_bounds.x_min, abs_tol=tolerance)
            and math.isclose(p_bounds.x_max, n_bounds.x_max, abs_tol=tolerance)
            and math.isclose(p_bounds.y_min, -n_bounds.y_max, abs_tol=tolerance)
            and math.isclose(p_bounds.y_max, -n_bounds.y_min, abs_tol=tolerance)
        )
        if not symmetric:
            errors.append(f"{positive.name} 与 {negative.name} 不关于Y=0镜像。")
    return errors


class GeometryCalculator:
    """根据已配置参数生成完整且可复用的几何结果。"""

    def __init__(self, config: Mapping[str, Any]) -> None:
        """保存规范化配置，不执行任何PyAEDT调用。"""

        self.config, self.normalization_messages = normalize_config(config)

    def calculate(self) -> GeometryResult:
        """执行全部叠层、信号、焊盘、桥接、平面和反焊盘计算。"""

        config = self.config
        stackup = calculate_stackup_z_positions(config)
        top_layer = _surface_signal_layer(config, stackup)
        mount_side = str(config["capacitor"].get("mount_side", "top"))
        outside_layers = (stackup[:top_layer.index] if mount_side == "top"
                          else stackup[top_layer.index + 1:])
        trace_centers = calculate_trace_centerlines(config)
        capacitor_centers = calculate_capacitor_centers(config)
        pad_centers = calculate_capacitor_pad_centers(config, capacitor_centers)
        pad_connections = calculate_pad_connection_points(
            config, capacitor_centers
        )
        transition_points = calculate_transition_points(
            config, trace_centers, pad_connections
        )
        trace_centerline_paths = calculate_trace_centerline_paths(
            config, transition_points
        )
        transition_summary = calculate_transition_offset(config)

        trace_shapes = _create_trace_shapes(config, stackup, trace_centerline_paths)
        pad_shapes = _create_pad_shapes(config, stackup, pad_centers)
        bridge_shapes = calculate_bridge_geometry(
            config, stackup, capacitor_centers
        )
        capacitor_model_shapes = _create_capacitor_model_shapes(
            config, stackup, capacitor_centers
        )
        body_outlines = _create_body_outlines(config, capacitor_centers)

        mode = str(config.get("capacitor_model", {}).get("mode", "ideal_bridge"))
        # signal_shapes remains the P/N aggregate used for clearances and bounds;
        # conductor_groups is the authoritative HFSS Unite topology.
        signal_shapes: Dict[str, List[Shape2D]] = {"P": [], "N": []}
        conductor_groups: Dict[str, List[Shape2D]] = {}
        for signal in ("P", "N"):
            trace_by_category: Dict[str, Shape2D] = {
                shape.category: shape for shape in trace_shapes[signal]
            }
            input_parts = [trace_by_category["trace_in"], pad_shapes[f"{signal}_IN"]]
            output_parts = [pad_shapes[f"{signal}_OUT"], trace_by_category["trace_out"]]
            if mode == "ideal_bridge":
                complete = input_parts + [bridge_shapes[signal]] + output_parts
                conductor_groups[f"SIGNAL_{signal}"] = complete
                signal_shapes[signal] = complete
            else:
                conductor_groups[f"SIGNAL_{signal}_IN"] = input_parts
                conductor_groups[f"SIGNAL_{signal}_OUT"] = output_parts
                signal_shapes[signal] = input_parts + output_parts

        all_signal_shapes = signal_shapes["P"] + signal_shapes["N"]
        model_bounds = calculate_model_bounds(all_signal_shapes)
        plane_bounds = calculate_plane_bounds(config, model_bounds)
        antipads = calculate_antipad_bounds(config, stackup, pad_shapes)

        warnings: List[str] = list(self.normalization_messages)
        errors: List[str] = []
        if config["trace"].get("pad_escape_mode") != "smooth_s_bend":
            errors.extend(validate_45deg_geometry(transition_points, unit=str(config["units"])))
        errors.extend(
            _validate_mirror_symmetry(signal_shapes["P"], signal_shapes["N"])
        )

        pad_overlaps = detect_pad_overlap(pad_shapes)
        if pad_overlaps:
            for first, second in pad_overlaps:
                errors.append(f"焊盘 {first} 与 {second} 发生重叠或接触。")

        signal_overlaps = detect_signal_overlap(
            signal_shapes["P"], signal_shapes["N"]
        )
        if signal_overlaps:
            for positive_name, negative_name in signal_overlaps:
                errors.append(
                    f"正负信号对象 {positive_name} 与 {negative_name} 发生相交或接触。"
                )

        if not plane_bounds.contains(model_bounds):
            errors.append("参考平面边界未能完整覆盖信号模型。")
        for layer_name, tools in antipads.items():
            for tool in tools:
                if not plane_bounds.contains(tool.bounds):
                    errors.append(
                        f"{layer_name} 的反焊盘 {tool.name} 超出参考平面边界。"
                    )
                if (
                    tool.bounds.width >= plane_bounds.width - EPSILON
                    and tool.bounds.height >= plane_bounds.height - EPSILON
                ):
                    errors.append(
                        f"{layer_name} 的反焊盘 {tool.name} 可能完全删除参考平面。"
                    )

        # 检查Z层高连续性。
        for first, second in zip(stackup, stackup[1:]):
            if not math.isclose(
                first.z_bottom, second.z_top, rel_tol=0.0, abs_tol=EPSILON
            ):
                errors.append(
                    f"叠层 {first.name} 与 {second.name} 之间存在间隙或重叠。"
                )

        return GeometryResult(
            # GeometryResult coordinates are always normalized internal mm;
            # config.units controls only display/save/HFSS expression units.
            units="mm",
            stackup=stackup,
            pcb_top_z=stackup[0].z_top,
            pcb_bottom_z=stackup[-1].z_bottom,
            mount_side=mount_side,
            surface_normal=(0.0, 0.0, 1.0 if mount_side == "top" else -1.0),
            signal_layer_name=top_layer.name,
            signal_z_bottom=top_layer.z_bottom,
            signal_z_top=top_layer.z_top,
            component_outward_z=(stackup[0].z_top if mount_side == "top" else stackup[-1].z_bottom),
            surface_soldermask_layers=[layer.name for layer in outside_layers
                                      if layer.layer_type == "soldermask"],
            trace_z_bottom=top_layer.z_bottom,
            trace_z_top=top_layer.z_top,
            trace_centers=trace_centers,
            capacitor_centers=capacitor_centers,
            pad_centers=pad_centers,
            pad_connection_points=pad_connections,
            transition_points=transition_points,
            trace_centerline_paths=trace_centerline_paths,
            transition_nominal_offset=float(transition_summary["transition_x"]),
            transition_nominal_real_length=float(
                transition_summary["real_length"]
            ),
            signal_shapes=signal_shapes,
            conductor_groups=conductor_groups,
            pad_shapes=pad_shapes,
            bridge_shapes=bridge_shapes,
            capacitor_model_shapes=capacitor_model_shapes,
            body_outlines=body_outlines,
            model_bounds=model_bounds,
            plane_bounds=plane_bounds,
            antipads_by_layer=antipads,
            warnings=warnings,
            errors=errors,
        )

    def geometry_summary(self, result: Optional[GeometryResult] = None) -> str:
        """生成满足交付要求的中文几何摘要文本。"""

        geometry = result if result is not None else self.calculate()
        config = self.config
        trace = config["trace"]
        capacitor = config["capacitor"]
        lines: List[str] = [
            "表层差分线串联电容反焊盘几何摘要",
            "=" * 52,
            f"默认单位：{geometry.units}",
            f"走线宽度：{trace['trace_width']} {geometry.units}",
            f"差分线中心距：{trace['trace_pitch']} {geometry.units}",
            f"差分线边到边间距：{trace['trace_gap']} {geometry.units}",
            f"PAD出线模式：{trace['pad_escape_mode']}",
            f"正负电容中心距：{capacitor['cap_pitch']} {geometry.units}",
            (
                "45°名义横向偏移/X投影："
                f"{geometry.transition_nominal_offset:g} {geometry.units}"
            ),
            (
                "45°名义实际中心线长度："
                f"{geometry.transition_nominal_real_length:g} {geometry.units}"
            ),
            "",
            "四个焊盘中心坐标：",
        ]
        for name in ("P_IN", "P_OUT", "N_IN", "N_OUT"):
            point = geometry.pad_centers[name]
            lines.append(
                f"  {name}: X={point.x:g}, Y={point.y:g} {geometry.units}"
            )

        lines.extend(
            [
                "",
                f"焊盘长度：{capacitor['pad_length']} {geometry.units}",
                f"焊盘宽度：{capacitor['pad_width']} {geometry.units}",
                f"焊盘间隙：{capacitor['pad_gap']} {geometry.units}",
                f"焊盘中心距：{capacitor['pad_pitch']} {geometry.units}",
                (
                    f"桥接尺寸：{capacitor['bridge_length']} × "
                    f"{capacitor['bridge_width']} {geometry.units}"
                ),
                "",
                "PCB叠层Z坐标：",
            ]
        )
        for layer in geometry.stackup:
            lines.append(
                f"  {layer.name}: bottom={layer.z_bottom:g}, "
                f"center={layer.z_center:g}, top={layer.z_top:g} "
                f"{geometry.units}"
            )

        lines.extend(
            [
                "",
                (
                    "参考平面边界："
                    f"X=[{geometry.plane_bounds.x_min:g}, "
                    f"{geometry.plane_bounds.x_max:g}], "
                    f"Y=[{geometry.plane_bounds.y_min:g}, "
                    f"{geometry.plane_bounds.y_max:g}] {geometry.units}"
                ),
                "",
                "各层反焊盘：",
            ]
        )
        if geometry.antipads_by_layer:
            for layer_name, tools in geometry.antipads_by_layer.items():
                expansion = (
                    tools[0].metadata["expansion"] if tools else "N/A"
                )
                lines.append(
                    f"  {layer_name}: mode={config['antipad']['mode']}, "
                    f"expansion={expansion} {geometry.units}, "
                    f"tools={len(tools)}"
                )
        else:
            lines.append("  无启用反焊盘的参考平面")

        lines.extend(["", f"几何警告（{len(geometry.warnings)}）："])
        lines.extend(
            [f"  [警告] {message}" for message in geometry.warnings] or ["  无"]
        )
        lines.extend(["", f"几何错误（{len(geometry.errors)}）："])
        lines.extend(
            [f"  [错误] {message}" for message in geometry.errors] or ["  无"]
        )
        return "\n".join(lines)


def calculate_geometry(config: Mapping[str, Any]) -> GeometryResult:
    """模块级便捷函数：一次性计算完整几何。"""

    return GeometryCalculator(config).calculate()
