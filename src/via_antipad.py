"""Authoritative 2-D signal-via antipad semantics.

The normalized configuration, Tk preview, software 3-D scene and generated
HFSS script all consume the contours produced here.  Dimensions are millimetres;
``length``/``width`` are end-to-end local X/Y sizes and ``rotation_deg`` is
counter-clockwise from global +X.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple


TOPOLOGIES = frozenset({"independent", "merged_pair"})
SHAPES = frozenset({"circle", "ellipse", "capsule", "rounded_rectangle"})
DIRECTION_MODES = frozenset({"auto_pn", "manual"})
SIZE_MODES = frozenset({"auto_fit", "auto_plus_increment", "manual"})
DIMENSION_FIELDS = (
    "diameter", "length", "width", "corner_radius", "rotation_deg", "clearance",
    "end_clearance", "side_clearance", "length_increment", "width_increment",
)


@dataclass(frozen=True)
class ViaAntipadContour:
    layer: str
    owner: str
    topology: str
    shape: str
    center_x: float
    center_y: float
    diameter: float
    length: float
    width: float
    corner_radius: float
    rotation_deg: float
    clearance: float
    source: str
    points: Tuple[Tuple[float, float], ...]
    bounds: Tuple[float, float, float, float]
    area: float
    direction_mode: str = "auto_pn"
    size_mode: str = "manual"
    required_length: float = 0.0
    required_width: float = 0.0
    effective_hole_diameter: float = 0.0
    p_clearance: float = 0.0
    n_clearance: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "layer": self.layer, "owner": self.owner,
            "topology": self.topology, "shape": self.shape,
            "center": [self.center_x, self.center_y],
            "diameter": self.diameter, "length": self.length,
            "width": self.width, "corner_radius": self.corner_radius,
            "rotation_deg": self.rotation_deg, "clearance": self.clearance,
            "source": self.source,
            "direction_mode": self.direction_mode, "size_mode": self.size_mode,
            "required_length": self.required_length, "required_width": self.required_width,
            "effective_hole_diameter": self.effective_hole_diameter,
            "p_clearance": self.p_clearance, "n_clearance": self.n_clearance,
            "points": [list(point) for point in self.points],
            "bounds": {
                "x_min": self.bounds[0], "x_max": self.bounds[1],
                "y_min": self.bounds[2], "y_max": self.bounds[3],
            },
            "area": self.area,
        }


def canonical_antipad(raw: Mapping[str, Any] | None = None) -> Dict[str, Any]:
    """Return a complete canonical antipad mapping without mutating input."""
    source = dict(raw or {})
    result: Dict[str, Any] = {
        "topology": str(source.get("topology", "independent")),
        "shape": str(source.get("shape", "circle")),
        "diameter": float(source.get("diameter", 0.6)),
        "length": float(source.get("length", 0.8)),
        "width": float(source.get("width", 0.6)),
        "corner_radius": float(source.get("corner_radius", 0.1)),
        "rotation_deg": float(source.get("rotation_deg", 0.0)),
        # Old configurations had no direction contract.  They migrate to the
        # safe behaviour: a merged opening follows the actual P/N centreline.
        "direction_mode": str(source.get("direction_mode", "auto_pn")),
        # Preserve old physical dimensions by default.  New defaults choose
        # auto_fit explicitly; this distinction makes migration lossless while
        # still rejecting an undersized legacy opening fail closed.
        "size_mode": str(source.get("size_mode", "manual")),
        # Manufacturing copper-edge clearance.  This is deliberately a real
        # engineering input, not a floating-point epsilon used as clearance.
        "clearance": float(source.get("clearance", 0.1)),
        "end_clearance": float(source.get("end_clearance", source.get("clearance", 0.1))),
        "side_clearance": float(source.get("side_clearance", source.get("clearance", 0.1))),
        "length_increment": float(source.get("length_increment", 0.0)),
        "width_increment": float(source.get("width_increment", 0.0)),
        "per_layer": {},
    }
    per_layer = source.get("per_layer", {})
    if isinstance(per_layer, Mapping):
        for name, value in per_layer.items():
            if not isinstance(value, Mapping):
                continue
            item = dict(value)
            normalized: Dict[str, Any] = {
                "enabled": bool(item.get("enabled", True)),
                "inherit_global": bool(item.get("inherit_global", False)),
            }
            for key in ("topology", "shape", "direction_mode", "size_mode"):
                if key in item:
                    normalized[key] = str(item[key])
            for key in DIMENSION_FIELDS:
                if key in item and item[key] not in (None, ""):
                    normalized[key] = float(item[key])
            result["per_layer"][str(name)] = normalized
    return result


def effective_antipad(global_config: Mapping[str, Any], layer: str) -> Tuple[Dict[str, Any], str, bool]:
    """Resolve per-layer enable/inherit/override state."""
    base = canonical_antipad(global_config)
    overrides = base.pop("per_layer")
    raw = overrides.get(str(layer))
    if raw is None:
        return base, "global", True
    enabled = bool(raw.get("enabled", True))
    if bool(raw.get("inherit_global", False)):
        return base, "global", enabled
    effective = dict(base)
    effective.update({key: value for key, value in raw.items()
                      if key not in {"enabled", "inherit_global"}})
    return effective, "per_layer", enabled


def _rotate(x: float, y: float, degrees: float) -> Tuple[float, float]:
    theta = math.radians(degrees)
    c, s = math.cos(theta), math.sin(theta)
    return x * c - y * s, x * s + y * c


def _arc(points: List[Tuple[float, float]], cx: float, cy: float, radius: float,
         start: float, stop: float, segments: int) -> None:
    for index in range(segments + 1):
        angle = start + (stop - start) * index / segments
        points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))


def local_outline(spec: Mapping[str, Any], segments: int = 72) -> Tuple[Tuple[float, float], ...]:
    shape = str(spec.get("shape", "circle"))
    diameter = float(spec.get("diameter", 0.0))
    length = float(spec.get("length", 0.0))
    width = float(spec.get("width", 0.0))
    radius = float(spec.get("corner_radius", 0.0))
    points: List[Tuple[float, float]] = []
    if shape == "circle":
        r = diameter / 2.0
        return tuple((r * math.cos(2 * math.pi * i / segments),
                      r * math.sin(2 * math.pi * i / segments)) for i in range(segments))
    if shape == "ellipse":
        return tuple((length / 2.0 * math.cos(2 * math.pi * i / segments),
                      width / 2.0 * math.sin(2 * math.pi * i / segments)) for i in range(segments))
    if shape == "capsule":
        r = width / 2.0
        straight = max(0.0, length / 2.0 - r)
        half = max(8, segments // 2)
        _arc(points, straight, 0.0, r, -math.pi / 2, math.pi / 2, half)
        _arc(points, -straight, 0.0, r, math.pi / 2, 3 * math.pi / 2, half)
        return tuple(points)
    if shape == "rounded_rectangle":
        r = min(max(0.0, radius), min(length, width) / 2.0)
        if r <= 0:
            return ((length / 2, width / 2), (-length / 2, width / 2),
                    (-length / 2, -width / 2), (length / 2, -width / 2))
        quarter = max(4, segments // 4)
        for cx, cy, start, stop in (
            (length / 2 - r, width / 2 - r, 0, math.pi / 2),
            (-length / 2 + r, width / 2 - r, math.pi / 2, math.pi),
            (-length / 2 + r, -width / 2 + r, math.pi, 3 * math.pi / 2),
            (length / 2 - r, -width / 2 + r, 3 * math.pi / 2, 2 * math.pi),
        ):
            _arc(points, cx, cy, r, start, stop, quarter)
        return tuple(points)
    raise ValueError("unsupported antipad shape: {!r}".format(shape))


def contour_area(spec: Mapping[str, Any]) -> float:
    shape = str(spec.get("shape", "circle"))
    if shape == "circle":
        return math.pi * (float(spec["diameter"]) / 2.0) ** 2
    length, width = float(spec["length"]), float(spec["width"])
    if shape == "ellipse":
        return math.pi * length * width / 4.0
    if shape == "capsule":
        return max(0.0, length - width) * width + math.pi * (width / 2.0) ** 2
    radius = float(spec.get("corner_radius", 0.0))
    return length * width - (4.0 - math.pi) * radius * radius


def make_contour(layer: str, owner: str, spec: Mapping[str, Any],
                 center: Tuple[float, float], source: str) -> ViaAntipadContour:
    local = local_outline(spec)
    rotation = float(spec.get("rotation_deg", 0.0))
    points = tuple((center[0] + _rotate(x, y, rotation)[0],
                    center[1] + _rotate(x, y, rotation)[1]) for x, y in local)
    xs, ys = [point[0] for point in points], [point[1] for point in points]
    return ViaAntipadContour(
        layer=str(layer), owner=str(owner), topology=str(spec["topology"]),
        shape=str(spec["shape"]), center_x=float(center[0]), center_y=float(center[1]),
        diameter=float(spec.get("diameter", 0.0)), length=float(spec.get("length", 0.0)),
        width=float(spec.get("width", 0.0)), corner_radius=float(spec.get("corner_radius", 0.0)),
        rotation_deg=rotation, clearance=float(spec.get("clearance", 0.0)),
        source=str(source), points=points,
        bounds=(min(xs), max(xs), min(ys), max(ys)), area=contour_area(spec),
        direction_mode=str(spec.get("direction_mode", "auto_pn")),
        size_mode=str(spec.get("size_mode", "manual")),
        required_length=float(spec.get("_required_length", 0.0)),
        required_width=float(spec.get("_required_width", 0.0)),
        effective_hole_diameter=float(spec.get("_effective_hole_diameter", 0.0)),
        p_clearance=float(spec.get("_p_clearance", 0.0)),
        n_clearance=float(spec.get("_n_clearance", 0.0)),
    )


def minimum_merged_circle_diameter(signal_via_pitch: float, effective_hole_diameter: float,
                                   clearance: float) -> float:
    """Minimum circular merged opening that contains both P/N hole envelopes."""
    return (float(signal_via_pitch) + float(effective_hole_diameter)
            + 2.0 * float(clearance))


def pn_axis_angle(p_center: Tuple[float, float], n_center: Tuple[float, float]) -> float:
    """Counter-clockwise angle of the real P→N centreline in degrees."""
    return math.degrees(math.atan2(n_center[1] - p_center[1],
                                   n_center[0] - p_center[0]))


def _clearance_to_outline(contour: ViaAntipadContour,
                          center: Tuple[float, float], hole_radius: float,
                          samples: int = 144) -> float:
    """Conservative sampled copper-edge clearance for diagnostics."""
    theta = math.radians(-contour.rotation_deg)
    c, s = math.cos(theta), math.sin(theta)
    dx, dy = center[0] - contour.center_x, center[1] - contour.center_y
    cx, cy = dx * c - dy * s, dx * s + dy * c
    best = float("inf")
    for index in range(samples):
        angle = 2.0 * math.pi * index / samples
        ux, uy = math.cos(angle), math.sin(angle)
        lo, hi = 0.0, max(contour.length, contour.width, contour.diameter, 1.0)
        for _ in range(48):
            radius = (lo + hi) / 2.0
            probe = (contour.center_x + _rotate(cx + ux * radius, cy + uy * radius,
                                                contour.rotation_deg)[0],
                     contour.center_y + _rotate(cx + ux * radius, cy + uy * radius,
                                                contour.rotation_deg)[1])
            if contains_circle(contour, probe, 0.0, samples=1):
                lo = radius
            else:
                hi = radius
        best = min(best, lo)
    return max(0.0, best - hole_radius)


def minimum_merged_dimensions(spec: Mapping[str, Any],
                              p_center: Tuple[float, float],
                              n_center: Tuple[float, float],
                              effective_hole_diameter: float) -> Tuple[float, float, float]:
    """Return conservative (length, width, diameter) requirements.

    The capsule result is the exact end-to-end contract required by the UI:
    ``Wmin = hole + 2*side_clearance`` and ``Lmin = distance + Wmin``.
    Other shapes start from the same engineering envelope and are enlarged by
    :func:`resolved_antipad_spec` until their true rotated outline contains the
    two required circles.
    """
    distance = math.hypot(n_center[0] - p_center[0], n_center[1] - p_center[1])
    end = float(spec.get("end_clearance", spec.get("clearance", 0.0)))
    side = float(spec.get("side_clearance", spec.get("clearance", 0.0)))
    width = float(effective_hole_diameter) + 2.0 * side
    length = distance + float(effective_hole_diameter) + 2.0 * end
    diameter = distance + float(effective_hole_diameter) + 2.0 * max(end, side)
    return length, width, diameter


def resolved_antipad_spec(spec: Mapping[str, Any],
                          p_center: Tuple[float, float],
                          n_center: Tuple[float, float],
                          effective_hole_diameter: float) -> Dict[str, Any]:
    """Resolve direction and automatic dimensions before contour creation."""
    result = dict(spec)
    if str(result.get("topology", "independent")) != "merged_pair":
        return result
    if str(result.get("direction_mode", "auto_pn")) == "auto_pn":
        result["rotation_deg"] = pn_axis_angle(p_center, n_center)
    required_length, required_width, required_diameter = minimum_merged_dimensions(
        result, p_center, n_center, effective_hole_diameter)
    result["clearance"] = max(
        float(result.get("clearance", 0.0)),
        float(result.get("end_clearance", result.get("clearance", 0.0))),
        float(result.get("side_clearance", result.get("clearance", 0.0))),
    )
    result["_required_length"] = required_length
    result["_required_width"] = required_width
    result["_effective_hole_diameter"] = float(effective_hole_diameter)
    mode = str(result.get("size_mode", "manual"))
    if mode in {"auto_fit", "auto_plus_increment"}:
        length_extra = float(result.get("length_increment", 0.0)) if mode == "auto_plus_increment" else 0.0
        width_extra = float(result.get("width_increment", 0.0)) if mode == "auto_plus_increment" else 0.0
        if str(result.get("shape")) == "circle":
            result["diameter"] = required_diameter + max(length_extra, width_extra)
        else:
            result["length"] = required_length + length_extra
            result["width"] = required_width + width_extra

            # Ellipses and small-radius rounded rectangles need more than the
            # capsule formula.  Verify the actual rotated outline, enlarging
            # deterministically until both required circles are contained.
            center = ((p_center[0] + n_center[0]) / 2.0,
                      (p_center[1] + n_center[1]) / 2.0)
            radius = effective_hole_diameter / 2.0 + max(
                float(result.get("end_clearance", result.get("clearance", 0.0))),
                float(result.get("side_clearance", result.get("clearance", 0.0))),
            )
            for _ in range(200):
                contour = make_contour("", "merged_pair", result, center, "derived")
                if (contains_circle(contour, p_center, radius)
                        and contains_circle(contour, n_center, radius)):
                    break
                step = max(1.0e-6, radius * 0.01)
                result["length"] += step
                result["width"] += step
            result["_required_length"] = max(required_length,
                                                result["length"] - length_extra)
            result["_required_width"] = max(required_width,
                                               result["width"] - width_extra)
    return result


def build_layer_contours(antipad: Mapping[str, Any], layer: str,
                         p_center: Tuple[float, float], n_center: Tuple[float, float],
                         effective_hole_diameter: float = 0.0) -> List[ViaAntipadContour]:
    spec, source, enabled = effective_antipad(antipad, layer)
    if not enabled:
        return []
    if str(spec["topology"]) == "merged_pair":
        spec = resolved_antipad_spec(spec, p_center, n_center,
                                     effective_hole_diameter)
        center = ((p_center[0] + n_center[0]) / 2.0, (p_center[1] + n_center[1]) / 2.0)
        contour = make_contour(layer, "merged_pair", spec, center, source)
        hole_radius = effective_hole_diameter / 2.0
        return [make_contour(layer, "merged_pair", {
            **spec,
            "_p_clearance": _clearance_to_outline(contour, p_center, hole_radius),
            "_n_clearance": _clearance_to_outline(contour, n_center, hole_radius),
        }, center, source)]
    return [make_contour(layer, owner, spec, center, source)
            for owner, center in (("P", p_center), ("N", n_center))]


def contains_circle(contour: ViaAntipadContour, center: Tuple[float, float], radius: float,
                    samples: int = 96) -> bool:
    """Actual-outline containment check used by manufacturing validation."""
    theta = math.radians(-contour.rotation_deg)
    c, s = math.cos(theta), math.sin(theta)

    def inside(px: float, py: float) -> bool:
        dx, dy = px - contour.center_x, py - contour.center_y
        x, y = dx * c - dy * s, dx * s + dy * c
        if contour.shape == "circle":
            return x * x + y * y <= (contour.diameter / 2.0) ** 2 + 1e-12
        if contour.shape == "ellipse":
            a, b = contour.length / 2.0, contour.width / 2.0
            return a > 0 and b > 0 and (x / a) ** 2 + (y / b) ** 2 <= 1.0 + 1e-12
        if contour.shape == "capsule":
            r = contour.width / 2.0
            straight = max(0.0, contour.length / 2.0 - r)
            nearest_x = min(straight, max(-straight, x))
            return (x - nearest_x) ** 2 + y * y <= r * r + 1e-12
        hx, hy = contour.length / 2.0, contour.width / 2.0
        r = min(contour.corner_radius, hx, hy)
        qx, qy = abs(x) - (hx - r), abs(y) - (hy - r)
        outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
        return outside + min(max(qx, qy), 0.0) <= r + 1e-12

    return all(inside(center[0] + radius * math.cos(2 * math.pi * i / samples),
                      center[1] + radius * math.sin(2 * math.pi * i / samples))
               for i in range(samples))


def circle_intersects_contour(contour: ViaAntipadContour,
                              center: Tuple[float, float], radius: float) -> bool:
    """Return whether a circle touches or overlaps the sampled true contour."""
    points = contour.points
    inside = False
    j = len(points) - 1
    for i, (xi, yi) in enumerate(points):
        xj, yj = points[j]
        if ((yi > center[1]) != (yj > center[1])
                and center[0] < (xj - xi) * (center[1] - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    if inside:
        return True
    radius_sq = radius * radius
    for start, stop in zip(points, points[1:] + points[:1]):
        dx, dy = stop[0] - start[0], stop[1] - start[1]
        length_sq = dx * dx + dy * dy
        if length_sq <= 1e-24:
            nearest = start
        else:
            t = ((center[0] - start[0]) * dx + (center[1] - start[1]) * dy) / length_sq
            t = min(1.0, max(0.0, t))
            nearest = (start[0] + t * dx, start[1] + t * dy)
        if (center[0] - nearest[0]) ** 2 + (center[1] - nearest[1]) ** 2 <= radius_sq + 1e-12:
            return True
    return False


def projected_width(contour: ViaAntipadContour, axis: str = "x") -> float:
    """Exact sampled projection used by the Z cross-section annotation."""
    index = 0 if axis == "x" else 1
    values = [point[index] for point in contour.points]
    return max(values) - min(values)
