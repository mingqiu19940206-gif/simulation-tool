"""V2 geometry facade extending the proven V1 signal/antipad kernel with GND vias."""
from __future__ import annotations

import copy
import math
from collections.abc import Iterator, Mapping as MappingABC
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import geometry_v1 as _v1
from geometry_v1 import *  # re-export V1 public geometry helpers
from config_manager import find_nearest_reference_plane, normalize_config
from scenario_semantics import VIA_TRANSITION, scenario_type
from via_transition_geometry import calculate_via_transition_geometry


@dataclass(frozen=True)
class ViaGeometry:
    name: str
    x: float
    y: float
    drill_diameter: float
    finished_hole_diameter: float
    pad_diameter: float
    antipad_diameter: float
    plating_thickness: float
    z_bottom: float
    z_top: float
    start_layer: str
    end_layer: str
    connect_layers: Tuple[str, ...]
    clearance_layers: Tuple[str, ...]
    traversed_layers: Tuple[str, ...]
    barrel_model: str
    material: str

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["connect_layers"] = list(self.connect_layers)
        data["clearance_layers"] = list(self.clearance_layers)
        data["traversed_layers"] = list(self.traversed_layers)
        return data


@dataclass(frozen=True)
class PortGeometry(MappingABC[str, Any]):
    """One authoritative terminal-port sheet shared by preview, Validate and HFSS."""

    name: str
    side: str
    signal: str
    x: float
    y_min: float
    y_max: float
    z_min: float
    z_max: float
    signal_conductor: str
    reference_layer: str
    reference_conductor: str
    normal_direction: str
    propagation_direction: str
    integration_line_start: Tuple[float, float, float]
    integration_line_end: Tuple[float, float, float]
    solution_type: str
    data_source: str
    validation_status: str
    source_shape: str
    sheet_name: str
    reference_distance: float
    size_mode: str
    lateral_margin: float
    size_basis: str
    clipped_by_board: bool

    def to_dict(self) -> Dict[str, Any]:
        """Serialize canonical fields plus narrow aliases for V2 compatibility."""

        return {
            "name": self.name, "side": self.side, "signal": self.signal,
            "x": self.x, "y_min": self.y_min, "y_max": self.y_max,
            "z_min": self.z_min, "z_max": self.z_max,
            "signal_conductor": self.signal_conductor,
            "reference_layer": self.reference_layer,
            "reference_conductor": self.reference_conductor,
            "normal_direction": self.normal_direction,
            "propagation_direction": self.propagation_direction,
            "integration_line_start": list(self.integration_line_start),
            "integration_line_end": list(self.integration_line_end),
            "solution_type": self.solution_type,
            "data_source": self.data_source,
            "validation_status": self.validation_status,
            "source_shape": self.source_shape,
            "sheet_name": self.sheet_name,
            "reference_distance": self.reference_distance,
            "size_mode": self.size_mode,
            "lateral_margin": self.lateral_margin,
            "size_basis": self.size_basis,
            "clipped_by_board": self.clipped_by_board,
            # Compatibility aliases. No consumer may recompute these values.
            "z_bottom": self.z_min, "z_top": self.z_max,
            "reference_conductors": [self.reference_conductor] if self.reference_conductor else [],
            "normal": self.normal_direction,
            "propagation": self.propagation_direction,
            "integration_line": [list(self.integration_line_start), list(self.integration_line_end)],
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())


class GeometryResultV2:
    """Delegates V1 attributes and extends serialization with real via geometry."""
    def __init__(self, base: _v1.GeometryResult, vias: Sequence[ViaGeometry], *,
                 capacitor_bodies: Sequence[Mapping[str, Any]] = (),
                 surface_ground: Optional[Mapping[str, Any]] = None,
                 soldermask_openings: Sequence[Mapping[str, Any]] = (),
                 reference_layer_name: str = "",
                 ports: Sequence[PortGeometry] = (),
                 airbox: Optional[Mapping[str, Any]] = None,
                 port_placeholders: Sequence[PortGeometry] = (),
                 airbox_placeholder: Optional[Mapping[str, Any]] = None) -> None:
        self._base = base
        self.gnd_vias = list(vias)
        self.capacitor_bodies = [dict(item) for item in capacitor_bodies]
        self.surface_ground = dict(surface_ground) if surface_ground else None
        # Compatibility alias retained for existing callers and saved snapshots.
        self.top_ground = self.surface_ground
        self.soldermask_openings = [dict(item) for item in soldermask_openings]
        self.reference_layer_name = str(reference_layer_name)
        self.ports = list(ports)
        self.airbox = dict(airbox) if airbox else None
        self.port_placeholders = list(port_placeholders)
        self.airbox_placeholder = dict(airbox_placeholder) if airbox_placeholder else None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

    def to_dict(self) -> Dict[str, Any]:
        data = self._base.to_dict()
        data["units"] = "mm"
        data["gnd_vias"] = [via.to_dict() for via in self.gnd_vias]
        data["capacitor_bodies"] = copy.deepcopy(self.capacitor_bodies)
        data["surface_ground"] = copy.deepcopy(self.surface_ground)
        data["top_ground"] = copy.deepcopy(self.surface_ground)
        data["soldermask_openings"] = copy.deepcopy(self.soldermask_openings)
        data["reference_layer_name"] = self.reference_layer_name
        data["ports"] = [port.to_dict() for port in self.ports]
        data["airbox"] = copy.deepcopy(self.airbox)
        return data


def _point_lists(points: Sequence[_v1.Point2D]) -> List[List[float]]:
    return [[float(point.x), float(point.y)] for point in points]


def _bounds_dict(bounds: _v1.Bounds2D) -> Dict[str, float]:
    return {
        "x_min": float(bounds.x_min), "x_max": float(bounds.x_max),
        "y_min": float(bounds.y_min), "y_max": float(bounds.y_max),
    }


def _calculate_capacitor_bodies(config: Mapping[str, Any],
                                base: _v1.GeometryResult) -> List[Dict[str, Any]]:
    height = float(config["capacitor"]["body_height"])
    outward = float(base.component_outward_z)
    top = base.mount_side == "top"
    return [
        {
            "name": f"BODY_{name}", "capacitor": name,
            "points": _point_lists(points),
            "z_bottom": outward if top else outward - height,
            "z_top": outward + height if top else outward,
            "height": height, "material": "CAP_BODY_CERAMIC",
            "mount_side": base.mount_side,
            "surface_normal": list(base.surface_normal),
            "hfss_enabled": bool(config["capacitor"].get("create_body_in_hfss", False)),
        }
        for name, points in base.body_outlines.items()
    ]


def _calculate_surface_ground(config: Mapping[str, Any],
                              base: _v1.GeometryResult) -> Optional[Dict[str, Any]]:
    via = config["gnd_vias"]
    if not via.get("enabled") or not via.get("create_top_ground_copper"):
        return None
    clearance = float(via.get("top_ground_clearance", 0.0))
    keepouts: List[Dict[str, Any]] = []
    for signal in ("P", "N"):
        for index, shape in enumerate(base.signal_shapes[signal], 1):
            bounds = shape.bounds.expanded(clearance)
            keepouts.append({
                "name": f"{base.mount_side.upper()}_GND_KEEPOUT_{signal}_{index:02d}",
                "signal": signal, "bounds": _bounds_dict(bounds),
                "source_shape": shape.name,
            })
    surface_layer = next(layer for layer in base.stackup
                         if layer.name == base.signal_layer_name)
    return {
        "name": f"{base.mount_side.upper()}_GROUND_COPPER", "layer": surface_layer.name,
        "mount_side": base.mount_side,
        "material": str(via.get("material", "copper")),
        "bounds": _bounds_dict(base.plane_bounds), "keepouts": keepouts,
        "z_bottom": float(surface_layer.z_bottom), "z_top": float(surface_layer.z_top),
        "clearance": clearance,
    }


def _calculate_soldermask_openings(base: _v1.GeometryResult) -> List[Dict[str, Any]]:
    """Expose the selected surface structures in every exterior soldermask layer."""

    layers = {layer.name: layer for layer in base.stackup}
    openings: List[Dict[str, Any]] = []
    for layer_name in base.surface_soldermask_layers:
        layer = layers[layer_name]
        for signal in ("P", "N"):
            for index, shape in enumerate(base.signal_shapes[signal], 1):
                openings.append({
                    "name": f"SM_OPEN_{base.mount_side.upper()}_{signal}_{index:02d}",
                    "layer": layer_name,
                    "mount_side": base.mount_side,
                    "source_shape": shape.name,
                    "signal": signal,
                    "points": _point_lists(shape.points),
                    "z_bottom": float(layer.z_bottom),
                    "z_top": float(layer.z_top),
                })
    return openings


def _selected_port_reference(config: Mapping[str, Any],
                             base: _v1.GeometryResult) -> Optional[_v1.LayerZPosition]:
    """Return the manual port reference or the shared mount-side nearest plane."""

    references = [layer for layer in base.stackup
                  if layer.layer_type == "plane" and layer.is_reference_plane]
    simulation = config["simulation"]
    if simulation.get("port_reference_mode", "auto_nearest") == "manual":
        requested = str(simulation.get("port_reference_layer", "")).strip()
        return next((layer for layer in references if layer.name == requested), None)
    target = find_nearest_reference_plane(config.get("stackup", []), base.mount_side)
    target_name = str(target.get("name", "")) if isinstance(target, Mapping) else ""
    return next((layer for layer in references if layer.name == target_name), None)


def _calculate_ports(config: Mapping[str, Any], base: _v1.GeometryResult, *,
                     allow_disabled_placeholder: bool = False) -> List[PortGeometry]:
    simulation = config["simulation"]
    if not simulation.get("create_differential_ports"):
        return []
    if not simulation.get("enabled") and not allow_disabled_placeholder:
        return []
    result: List[PortGeometry] = []
    reference = _selected_port_reference(config, base)
    if reference is None:
        return result
    trace_width = float(config["trace"]["trace_width"])
    trace_pitch = float(config["trace"].get("trace_pitch", trace_width * 2.0))
    size_mode = str(simulation.get("port_size_mode", "auto"))
    configured_margin = float(simulation.get("port_lateral_margin", trace_width * 3.0))
    center_midpoint = sum(float(base.trace_centers[name]) for name in ("P", "N")) / 2.0
    reference_conductor = f"PLANE_{reference.name}"
    if base.mount_side == "top":
        reference_distance = float(base.trace_z_bottom - reference.z_top)
        port_z_min, port_z_max = float(reference.z_bottom), float(base.trace_z_top)
        reference_contact_z = float(reference.z_top)
    else:
        reference_distance = float(reference.z_bottom - base.trace_z_top)
        port_z_min, port_z_max = float(base.trace_z_bottom), float(reference.z_top)
        reference_contact_z = float(reference.z_bottom)
    for signal in ("P", "N"):
        by_category = {shape.category: shape for shape in base.signal_shapes[signal]}
        for side, category, x_side in (("IN", "trace_in", "min"),
                                       ("OUT", "trace_out", "max")):
            shape = by_category[category]
            bounds = shape.bounds
            x = bounds.x_min if x_side == "min" else bounds.x_max
            signal_center = (float(bounds.y_min) + float(bounds.y_max)) / 2.0
            # Local engineering estimate only: actual adequacy still requires
            # an AEDT/HFSS port-mode check. Auto mode considers conductor width,
            # pair pitch and signal-to-reference height instead of hard-coding
            # 5*trace_width. The two single-ended sheets share only the midpoint
            # edge and do not overlap in area.
            if size_mode == "manual":
                requested_margin = configured_margin
                size_basis = "manual port_lateral_margin"
            else:
                candidates = {
                    "3*trace_width": 3.0 * trace_width,
                    "1.5*trace_pitch": 1.5 * trace_pitch,
                    "2*reference_distance": 2.0 * reference_distance,
                    "minimum_margin": configured_margin,
                }
                requested_margin = max(candidates.values())
                size_basis = "auto max(" + ", ".join(candidates) + ")"
            if signal_center >= center_midpoint:
                y_min = center_midpoint
                requested_edge = float(bounds.y_max) + requested_margin
                y_max = min(float(base.plane_bounds.y_max), requested_edge)
            else:
                requested_edge = float(bounds.y_min) - requested_margin
                y_min = max(float(base.plane_bounds.y_min), requested_edge)
                y_max = center_midpoint
            actual_margin = ((y_max - float(bounds.y_max)) if signal_center >= center_midpoint
                             else (float(bounds.y_min) - y_min))
            clipped_by_board = actual_margin + _v1.EPSILON < requested_margin
            name = f"PORT_{side}_{signal}"
            integration_start = (
                float(x), signal_center,
                float((base.trace_z_bottom + base.trace_z_top) / 2.0),
            )
            integration_end = (float(x), signal_center, reference_contact_z)
            result.append(PortGeometry(
                name=name, side=side, signal=signal,
                x=float(x), y_min=float(y_min), y_max=float(y_max),
                z_min=port_z_min, z_max=port_z_max,
                signal_conductor=(
                    f"SIGNAL_{signal}"
                    if str(config.get("capacitor_model", {}).get("mode")) == "ideal_bridge"
                    else f"SIGNAL_{signal}_{side}"
                ),
                reference_layer=reference.name,
                reference_conductor=reference_conductor,
                normal_direction="+X" if side == "IN" else "-X",
                propagation_direction="+X" if side == "IN" else "-X",
                integration_line_start=integration_start,
                integration_line_end=integration_end,
                solution_type=str(config.get("project", {}).get("solution_type", "Terminal")),
                data_source="Unified PortGeometry: explicit YZ sheet; locally estimated size",
                validation_status=(
                    "disabled_preview"
                    if allow_disabled_placeholder and not simulation.get("enabled")
                    else "local_estimate_not_hfss_verified"
                ),
                source_shape=shape.name,
                sheet_name=f"PORT_SHEET_{side}_{signal}",
                reference_distance=reference_distance,
                size_mode=size_mode,
                lateral_margin=actual_margin,
                size_basis=size_basis,
                clipped_by_board=clipped_by_board,
            ))
    return result


def _calculate_airbox(config: Mapping[str, Any], base: _v1.GeometryResult, *,
                      allow_disabled_placeholder: bool = False) -> Optional[Dict[str, Any]]:
    simulation = config["simulation"]
    if not simulation.get("create_airbox"):
        return None
    if not simulation.get("enabled") and not allow_disabled_placeholder:
        return None
    margin = float(simulation.get("airbox_margin", 1.0))
    # The PCB plane bounds cover traces and vias, but not necessarily the
    # ceramic package body.  The airbox is a real simulation object, so its
    # manifest must include every solid that will be emitted to HFSS.
    x_min, x_max = float(base.plane_bounds.x_min), float(base.plane_bounds.x_max)
    y_min, y_max = float(base.plane_bounds.y_min), float(base.plane_bounds.y_max)
    for outline in base.body_outlines.values():
        body_bounds = _v1.bounds_from_points(outline)
        x_min, x_max = min(x_min, body_bounds.x_min), max(x_max, body_bounds.x_max)
        y_min, y_max = min(y_min, body_bounds.y_min), max(y_max, body_bounds.y_max)
    body_height = float(config["capacitor"].get("body_height", 0.0))
    body_bottom = (float(base.pcb_bottom_z - body_height)
                   if base.mount_side == "bottom" else float(base.pcb_bottom_z))
    body_top = (float(base.pcb_top_z + body_height)
                if base.mount_side == "top" else float(base.pcb_top_z))
    return {
        "name": "AIRBOX", "material": "air", "boundary": simulation.get("open_boundary", "Radiation"),
        "margin": margin,
        "x_min": x_min - margin, "x_max": x_max + margin,
        "y_min": y_min - margin, "y_max": y_max + margin,
        "z_bottom": body_bottom - margin, "z_top": body_top + margin,
    }


def _bounds_of(shapes: Sequence[_v1.Shape2D]) -> _v1.Bounds2D:
    return _v1.combine_bounds(shape.bounds for shape in shapes)


def calculate_gnd_via_positions(config: Mapping[str, Any], signal_bounds: _v1.Bounds2D,
                                collision_bounds: Optional[_v1.Bounds2D] = None) -> List[_v1.Point2D]:
    """Return deterministic, axis-symmetric via centers in internal mm.

    ``signal_bounds`` 是需求规定的焊盘/桥接局部基准；左右边 VIA 还必须避开
    沿 X 方向延伸的完整走线，因此其 X 位置使用 ``collision_bounds`` 外缘。
    """
    normalized, _ = normalize_config(config)
    via = normalized["gnd_vias"]
    if not via.get("enabled"):
        return []
    pattern = via["pattern"]
    count = int(via["count"])
    radius = float(via["pad_diameter"]) / 2.0
    left = signal_bounds.x_min - float(via["clearance_x"]) - radius
    right = signal_bounds.x_max + float(via["clearance_x"]) + radius
    full = collision_bounds or signal_bounds
    outer_left = full.x_min - float(via["clearance_x"]) - radius
    outer_right = full.x_max + float(via["clearance_x"]) + radius
    bottom = signal_bounds.y_min - float(via["clearance_y"]) - radius
    top = signal_bounds.y_max + float(via["clearance_y"]) + radius
    cx = (signal_bounds.x_min + signal_bounds.x_max) / 2.0
    cy = 0.0

    if pattern == "four_corner":
        return [_v1.Point2D(x, y) for x in (left, right) for y in (bottom, top)]
    if pattern == "custom":
        return [_v1.Point2D(float(point[0]), float(point[1])) for point in via["custom_coordinates"]]
    if pattern == "side_pair":
        half = max(1, count // 2)
        if via.get("side_orientation") == "left_right":
            offsets = [(index - (half - 1) / 2.0) * float(via["pitch_y"]) for index in range(half)]
            return [_v1.Point2D(x, cy + offset) for x in (outer_left, outer_right) for offset in offsets]
        offsets = [(index - (half - 1) / 2.0) * float(via["pitch_x"]) for index in range(half)]
        return [_v1.Point2D(cx + offset, y) for y in (bottom, top) for offset in offsets]
    # rectangular_fence: equal populations on all four sides, no duplicate corners.
    per_side = max(1, count // 4)
    x_offsets = [(index - (per_side - 1) / 2.0) * float(via["pitch_x"]) for index in range(per_side)]
    y_offsets = [(index - (per_side - 1) / 2.0) * float(via["pitch_y"]) for index in range(per_side)]
    points = [_v1.Point2D(cx + offset, y) for y in (bottom, top) for offset in x_offsets]
    points.extend(_v1.Point2D(x, cy + offset) for x in (outer_left, outer_right) for offset in y_offsets)
    return points


def _point_in_polygon(point: _v1.Point2D, polygon: Sequence[_v1.Point2D]) -> bool:
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        a, b = polygon[i], polygon[j]
        if ((a.y > point.y) != (b.y > point.y)) and (
            point.x < (b.x - a.x) * (point.y - a.y) / ((b.y - a.y) or 1e-30) + a.x
        ):
            inside = not inside
        j = i
    return inside


def _distance_to_segment(p: _v1.Point2D, a: _v1.Point2D, b: _v1.Point2D) -> float:
    dx, dy = b.x - a.x, b.y - a.y
    if abs(dx) + abs(dy) < 1e-30:
        return math.hypot(p.x - a.x, p.y - a.y)
    t = max(0.0, min(1.0, ((p.x - a.x) * dx + (p.y - a.y) * dy) / (dx * dx + dy * dy)))
    return math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy))


def _circle_hits_polygon(center: _v1.Point2D, radius: float, polygon: Sequence[_v1.Point2D]) -> bool:
    if _point_in_polygon(center, polygon):
        return True
    return any(_distance_to_segment(center, polygon[index], polygon[(index + 1) % len(polygon)]) <= radius + _v1.EPSILON
               for index in range(len(polygon)))


def _symmetric(points: Sequence[_v1.Point2D]) -> bool:
    rounded = {(round(point.x, 9), round(point.y, 9)) for point in points}
    return all((-round(point.x, 9), round(point.y, 9)) in rounded or
               (round(point.x, 9), -round(point.y, 9)) in rounded
               for point in points)


def _via_z_range(stackup: Sequence[_v1.LayerZPosition], start: str, end: str) -> Tuple[float, float]:
    by_name = {layer.name: layer for layer in stackup}
    return by_name[end].z_bottom, by_name[start].z_top


def _via_layer_sets(config: Mapping[str, Any]) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """返回跨越范围内的非连接参考平面和全部跨越层。"""
    stackup = list(config["stackup"])
    names = [str(layer["name"]) for layer in stackup]
    start = names.index(str(config["gnd_vias"]["start_layer"]))
    end = names.index(str(config["gnd_vias"]["end_layer"]))
    traversed = tuple(names[start:end + 1])
    connected = {str(name) for name in config["gnd_vias"].get("connect_layers", [])}
    clearance = tuple(
        str(layer["name"]) for layer in stackup[start:end + 1]
        if layer.get("type") == "plane" and layer.get("is_reference_plane")
        and str(layer["name"]) not in connected
    )
    return clearance, traversed


class GeometryCalculator(_v1.GeometryCalculator):
    def __init__(self, config: Mapping[str, Any]) -> None:
        self.config, self.normalization_messages = normalize_config(config)

    def calculate(self) -> Any:
        if scenario_type(self.config) == VIA_TRANSITION:
            return calculate_via_transition_geometry(self.config)
        base = super().calculate()
        cfg = self.config
        selected_reference = _selected_port_reference(cfg, base)
        nearest_reference_config = find_nearest_reference_plane(cfg["stackup"], base.mount_side)
        nearest_reference_name = (
            str(nearest_reference_config.get("name", ""))
            if isinstance(nearest_reference_config, Mapping) else ""
        )
        via_cfg = cfg["gnd_vias"]
        shapes = base.all_signal_shapes()
        local_shapes = list(base.pad_shapes.values()) + list(base.bridge_shapes.values())
        if not local_shapes:
            local_shapes = list(base.pad_shapes.values())
        signal_bounds = _bounds_of(local_shapes)
        centers = calculate_gnd_via_positions(cfg, signal_bounds, _bounds_of(shapes))
        vias: List[ViaGeometry] = []
        if centers:
            z_bottom, z_top = _via_z_range(base.stackup, via_cfg["start_layer"], via_cfg["end_layer"])
            clearance_layers, traversed_layers = _via_layer_sets(cfg)
            for index, center in enumerate(centers, 1):
                vias.append(ViaGeometry(
                    name=f"GND_VIA_{index:02d}", x=center.x, y=center.y,
                    drill_diameter=float(via_cfg["drill_diameter"]),
                    finished_hole_diameter=float(via_cfg["finished_hole_diameter"]),
                    pad_diameter=float(via_cfg["pad_diameter"]),
                    antipad_diameter=float(via_cfg["antipad_diameter"]),
                    plating_thickness=float(via_cfg["plating_thickness"]),
                    z_bottom=z_bottom, z_top=z_top,
                    start_layer=str(via_cfg["start_layer"]), end_layer=str(via_cfg["end_layer"]),
                    connect_layers=tuple(str(item) for item in via_cfg["connect_layers"]),
                    clearance_layers=clearance_layers, traversed_layers=traversed_layers,
                    barrel_model=str(via_cfg["barrel_model"]), material=str(via_cfg["material"])))
            radius = float(via_cfg["pad_diameter"]) / 2.0
            for via, center in zip(vias, centers):
                for shape in shapes:
                    if _circle_hits_polygon(center, radius, shape.points):
                        base.errors.append(f"{via.name} 焊盘侵入信号铜皮 {shape.name}。")
                # Antipad tools are vacuum cut-outs in reference copper, not
                # signal copper.  A via pad may enter that empty region while
                # remaining diagonally clear of the real signal polygons;
                # only the polygon checks above constitute signal collision.
                via_bounds = _v1.Bounds2D(center.x - radius, center.x + radius,
                                          center.y - radius, center.y + radius)
                if not base.plane_bounds.contains(via_bounds):
                    base.errors.append(f"{via.name} 超出参考平面/板框边界。")
            for i, first in enumerate(centers):
                for second in centers[i + 1:]:
                    if math.hypot(first.x - second.x, first.y - second.y) <= 2.0 * radius + _v1.EPSILON:
                        base.errors.append("GND VIA 焊盘之间发生重叠或接触。")
            if not _symmetric(centers):
                message = "GND VIA 布局未保持差分中心对称。"
                if via_cfg["pattern"] == "custom":
                    base.warnings.append(message + " 自定义模式允许非对称，但请确认回流路径意图。")
                else:
                    base.errors.append("自动 " + message)
            if via_cfg["pattern"] == "four_corner" and len(vias) != 4:
                base.errors.append("four_corner 必须生成 4 颗 VIA。")
            if via_cfg["pattern"] == "side_pair" and len(vias) != int(via_cfg["count"]):
                base.errors.append("side_pair 的 count 必须为偶数。")
            if via_cfg["pattern"] == "rectangular_fence" and len(vias) != int(via_cfg["count"]):
                base.errors.append("rectangular_fence 的 count 必须为 4 的倍数。")
        return GeometryResultV2(
            base, vias,
            capacitor_bodies=_calculate_capacitor_bodies(cfg, base),
            surface_ground=_calculate_surface_ground(cfg, base),
            soldermask_openings=_calculate_soldermask_openings(base),
            reference_layer_name=nearest_reference_name,
            ports=_calculate_ports(cfg, base),
            airbox=_calculate_airbox(cfg, base),
            port_placeholders=(
                _calculate_ports(cfg, base, allow_disabled_placeholder=True)
                if not cfg["simulation"].get("enabled") else ()
            ),
            airbox_placeholder=(
                _calculate_airbox(cfg, base, allow_disabled_placeholder=True)
                if not cfg["simulation"].get("enabled") else None
            ),
        )

    def geometry_summary(self, result: Optional[GeometryResultV2] = None) -> str:
        geometry = result if result is not None else self.calculate()
        unit = self.config.get("units", "mm")
        from config_manager import format_length
        def fl(value: float) -> str:
            return f"{format_length(value, unit)} {unit}"
        cap = self.config["capacitor"]
        trace = self.config["trace"]
        lines = ["PCIe AC耦合电容区域 V2 几何摘要", "=" * 48,
                 f"显示/文件/HFSS单位：{unit}（内部计算：mm）",
                 f"走线宽度：{fl(trace['trace_width'])}",
                 f"差分走线中心距：{fl(trace['trace_pitch'])}",
                 f"P/N两颗电容中心距 cap_pitch：{fl(cap['cap_pitch'])}",
                 f"单颗电容两个焊盘中心距 pad_pitch：{fl(cap['pad_pitch'])}",
                 f"焊盘：{fl(cap['pad_length'])} × {fl(cap['pad_width'])}",
                 f"45°过渡X投影：{fl(geometry.transition_nominal_offset)}",
                 f"电容模型：{self.config['capacitor_model']['mode']}", "", "电容中心："]
        for name in ("C_P", "C_N"):
            point = geometry.capacitor_centers[name]
            lines.append(f"  {name}: X={fl(point.x)}, Y={fl(point.y)}")
        b = geometry.plane_bounds
        lines.extend(["", f"参考平面：X=[{fl(b.x_min)}, {fl(b.x_max)}]，Y=[{fl(b.y_min)}, {fl(b.y_max)}]",
                      "", "PCB叠层Z："])
        for layer in geometry.stackup:
            lines.append(f"  {layer.name}: bottom={fl(layer.z_bottom)}, top={fl(layer.z_top)}")
        lines.extend(["", f"GND VIA：{'启用' if self.config['gnd_vias']['enabled'] else '关闭'}，数量={len(geometry.gnd_vias)}"])
        for via in geometry.gnd_vias:
            lines.append(f"  {via.name}: ({fl(via.x)}, {fl(via.y)})，孔={fl(via.finished_hole_diameter)}，盘={fl(via.pad_diameter)}")
        lines.extend(["", f"几何警告（{len(geometry.warnings)}）："])
        lines.extend([f"  [警告] {item}" for item in geometry.warnings] or ["  无"])
        lines.extend(["", f"几何错误（{len(geometry.errors)}）："])
        lines.extend([f"  [错误] {item}" for item in geometry.errors] or ["  无"])
        return "\n".join(lines)

def calculate_geometry(config: Mapping[str, Any]) -> GeometryResultV2:
    return GeometryCalculator(config).calculate()
