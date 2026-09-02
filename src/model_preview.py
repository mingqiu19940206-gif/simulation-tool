"""Interactive offline 3D model preview backed by the HFSS geometry result."""
from __future__ import annotations

import json
import math
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import ttk
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from config_manager import format_length, format_number, make_generation_snapshot
from gui_layout import (
    CanvasLabelPlacer, canvas_viewport, ellipsize, font_linespace, resolve_font,
)
from gnd_via_semantics import validate_via_workbench
from scenario_semantics import VIA_TRANSITION, require_known_scenario, scenario_type

try:
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPlane, vtkPolyData, vtkPolygon, vtkPolyLine
    from vtkmodules.vtkFiltersCore import vtkCleanPolyData, vtkTriangleFilter
    from vtkmodules.vtkFiltersGeneral import vtkBooleanOperationPolyDataFilter
    from vtkmodules.vtkFiltersModeling import vtkLinearExtrusionFilter
    from vtkmodules.vtkFiltersSources import vtkConeSource, vtkCubeSource, vtkDiskSource, vtkLineSource, vtkSphereSource
    from vtkmodules.vtkInteractionStyle import vtkInteractorStyleTrackballCamera
    from vtkmodules.vtkInteractionWidgets import vtkOrientationMarkerWidget
    from vtkmodules.vtkRenderingAnnotation import vtkAxesActor
    from vtkmodules.vtkRenderingCore import (
        vtkActor, vtkBillboardTextActor3D, vtkCellPicker, vtkPolyDataMapper, vtkRenderer,
        vtkRenderWindowInteractor,
    )
    from vtkmodules.vtkRenderingOpenGL2 import vtkWin32OpenGLRenderWindow
    from shapely.geometry import Point as ShapelyPoint
    from shapely.geometry import Polygon as ShapelyPolygon
    from shapely.ops import triangulate as shapely_triangulate
    from shapely.ops import unary_union as shapely_union
    # Keep the existing compact ``vtk.vtkClass`` call sites without importing
    # VTK's all-modules compatibility namespace (which would bloat the EXE).
    vtk = SimpleNamespace(**{
        cls.__name__: cls for cls in (
            vtkActor, vtkAxesActor, vtkBooleanOperationPolyDataFilter, vtkCellArray,
            vtkBillboardTextActor3D, vtkCellPicker, vtkCleanPolyData, vtkConeSource,
            vtkCubeSource, vtkDiskSource,
            vtkInteractorStyleTrackballCamera, vtkLinearExtrusionFilter, vtkLineSource,
            vtkOrientationMarkerWidget, vtkPlane, vtkPoints, vtkPolyData, vtkPolyDataMapper,
            vtkPolygon, vtkPolyLine, vtkRenderer, vtkRenderWindowInteractor, vtkSphereSource,
            vtkTriangleFilter, vtkWin32OpenGLRenderWindow,
        )
    })
    VTK_AVAILABLE = True
    VTK_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - exercised only on incomplete deployments
    vtk = None  # type: ignore[assignment]
    VTK_AVAILABLE = False
    VTK_IMPORT_ERROR = str(exc)

SHAPELY_AVAILABLE = VTK_AVAILABLE


COLORS: Dict[str, Tuple[float, float, float]] = {
    "P": (0.84, 0.16, 0.16), "N": (0.12, 0.36, 0.78),
    "copper": (0.82, 0.55, 0.16), "dielectric": (0.55, 0.82, 0.88),
    "plane": (0.35, 0.55, 0.47), "soldermask": (0.20, 0.68, 0.36),
    "antipad": (0.96, 0.50, 0.10), "via": (0.78, 0.47, 0.12),
    "hole": (0.12, 0.15, 0.20), "body": (0.82, 0.78, 0.66),
    "airbox": (0.50, 0.78, 0.96), "port": (0.68, 0.32, 0.88),
    "boundary": (0.20, 0.65, 0.85), "selected": (1.0, 0.88, 0.05),
    "error": (0.88, 0.08, 0.08), "warning": (0.95, 0.46, 0.04),
    "success": (0.10, 0.64, 0.34),
    "disabled": (0.48, 0.52, 0.58),
}

CATEGORY_ORDER = (
    "PCB叠层", "信号走线", "电容与焊盘", "电气模型", "反焊盘", "GND VIA",
    "安装面GND铜皮", "输入走线", "输出走线", "信号VIA", "功能焊盘",
    "非功能焊盘", "VIA反焊盘", "背钻腔体", "回流孔", "Port", "开放边界", "空气盒",
)

SIMULATION_CATEGORIES = {"Port", "开放边界", "空气盒"}

# The Win32 VTK render window cannot be embedded safely in a Tk notebook with
# the wheels shipped for Python 3.14.  On affected systems merely exposing the
# hidden notebook page can terminate the process in ucrtbase.dll (0xc0000409),
# before Python has a chance to report an exception.  Keep VTK for geometry
# generation/tests, but use the native Tk software viewport for the product UI.
NATIVE_VTK_EMBED_ENABLED = False


def _display_unit(config: Mapping[str, Any]) -> str:
    unit = str(config.get("units", "mm"))
    return unit if unit in {"mm", "mil"} else "mm"


def _length_text(config: Mapping[str, Any], mm_value: float) -> str:
    unit = _display_unit(config)
    return f"{format_length(float(mm_value), unit)} {unit}"


class VTKTkRenderFrame(tk.Frame):
    """Embed VTK's native Win32 OpenGL child window in a Tk frame.

    Official Windows VTK wheels do not ship ``vtkRenderingTk.dll``.  VTK's
    Win32 render window does support a native parent HWND, so Tk events are
    forwarded to the standard VTK interactor without a pseudo-3D canvas.
    """

    def __init__(self, master: tk.Misc, **kwargs: Any) -> None:
        super().__init__(master, background="#f6f8fb", **kwargs)
        self.update_idletasks()
        self.render_window = vtk.vtkWin32OpenGLRenderWindow()
        self.render_window.SetParentInfo(str(self.winfo_id()))
        self.render_window.SetSize(max(2, self.winfo_width()), max(2, self.winfo_height()))
        self.interactor = vtk.vtkRenderWindowInteractor()
        self.interactor.SetRenderWindow(self.render_window)
        self.interactor.Initialize()
        self.bind("<Configure>", self._configure, add="+")
        for button, vtk_name in ((1, "Left"), (2, "Middle"), (3, "Right")):
            self.bind(f"<ButtonPress-{button}>",
                      lambda event, name=vtk_name: self._button(event, name + "ButtonPressEvent"), add="+")
            self.bind(f"<ButtonRelease-{button}>",
                      lambda event, name=vtk_name: self._button(event, name + "ButtonReleaseEvent"), add="+")
        self.bind("<B1-Motion>", self._motion, add="+")
        self.bind("<B2-Motion>", self._motion, add="+")
        self.bind("<B3-Motion>", self._motion, add="+")
        self.bind("<Motion>", self._motion, add="+")
        self.bind("<MouseWheel>", self._wheel, add="+")
        self.bind("<KeyPress>", self._key, add="+")

    def _event_information(self, event: tk.Event, key: str = "") -> None:
        key_code = key[:1] if key else "\0"
        self.interactor.SetEventInformationFlipY(
            int(event.x), int(event.y), int(bool(event.state & 0x4)),
            int(bool(event.state & 0x1)), key_code, 0, key,
        )

    def _configure(self, event: tk.Event) -> None:
        self.render_window.SetSize(max(2, int(event.width)), max(2, int(event.height)))
        self.render_window.Render()

    def _button(self, event: tk.Event, vtk_event: str) -> str:
        self.focus_set(); self._event_information(event); self.interactor.InvokeEvent(vtk_event)
        return "break"

    def _motion(self, event: tk.Event) -> str:
        self._event_information(event); self.interactor.InvokeEvent("MouseMoveEvent")
        return "break"

    def _wheel(self, event: tk.Event) -> str:
        self._event_information(event)
        self.interactor.InvokeEvent("MouseWheelForwardEvent" if event.delta > 0 else "MouseWheelBackwardEvent")
        return "break"

    def _key(self, event: tk.Event) -> str:
        key = str(event.char or "")[:1]
        self._event_information(event, key)
        self.interactor.InvokeEvent("KeyPressEvent"); self.interactor.InvokeEvent("CharEvent")
        return "break"

    def GetRenderWindow(self) -> Any:
        return self.render_window

    def Finalize(self) -> None:
        self.render_window.Finalize()


@dataclass
class SceneObject:
    key: str
    name: str
    category: str
    object_type: str
    layer: str = ""
    material: str = ""
    z_bottom: float = 0.0
    z_top: float = 0.0
    points_xy: Tuple[Tuple[float, float], ...] = ()
    bounds_xy: Optional[Tuple[float, float, float, float]] = None
    center_xy: Optional[Tuple[float, float]] = None
    outer_radius: float = 0.0
    inner_radius: float = 0.0
    holes_xy: Tuple[Tuple[Tuple[float, float], ...], ...] = ()
    circular_holes: Tuple[Tuple[float, float, float], ...] = ()
    source_paths: Tuple[str, ...] = ()
    details: Dict[str, Any] = field(default_factory=dict)
    color_key: str = "copper"
    opacity: float = 1.0
    default_visible: bool = True
    layer_index: Optional[int] = None
    status: str = "本地预估"
    data_source: str = "本地统一几何计算"
    validate_result: str = "未运行 Validate"

    def fingerprint(self) -> str:
        data = {
            "key": self.key, "type": self.object_type, "layer": self.layer,
            "material": self.material, "z": (self.z_bottom, self.z_top),
            "points": self.points_xy, "bounds": self.bounds_xy, "center": self.center_xy,
            "radii": (self.outer_radius, self.inner_radius), "holes": self.holes_xy,
            "circular": self.circular_holes, "details": self.details,
            "status": self.status, "source": self.data_source,
            "validate": self.validate_result,
        }
        return json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)


@dataclass(frozen=True)
class SceneIssue:
    level: str
    message: str
    object_keys: Tuple[str, ...] = ()
    path: str = ""
    suggestion: str = ""
    view: str = "等轴测"


@dataclass
class ModelScene:
    objects: List[SceneObject]
    issues: List[SceneIssue]
    bounds: Tuple[float, float, float, float, float, float]
    pcb_thickness: float


def _rectangle(bounds: Any) -> Tuple[Tuple[float, float], ...]:
    return ((float(bounds.x_min), float(bounds.y_min)),
            (float(bounds.x_max), float(bounds.y_min)),
            (float(bounds.x_max), float(bounds.y_max)),
            (float(bounds.x_min), float(bounds.y_max)))


def _shape_points(shape: Any) -> Tuple[Tuple[float, float], ...]:
    return tuple((float(point.x), float(point.y)) for point in shape.points)


def _layer_map(geometry: Any) -> Dict[str, Any]:
    return {str(layer.name): layer for layer in geometry.stackup}


def _object_keys_for_message(message: str, objects: Sequence[SceneObject]) -> Tuple[str, ...]:
    matched = [item.key for item in objects if item.name in message]
    if matched:
        return tuple(matched)
    upper = message.upper()
    if "VIA" in upper:
        return tuple(item.key for item in objects if item.category == "GND VIA")
    if "焊盘" in message or "桥接" in message or "电容" in message:
        return tuple(item.key for item in objects if item.category == "电容与焊盘")
    if "反焊盘" in message:
        return tuple(item.key for item in objects if item.category == "反焊盘")
    if "Port" in message or "端口" in message:
        return tuple(item.key for item in objects if item.category == "Port")
    if "空气盒" in message:
        return tuple(item.key for item in objects if item.category in {"空气盒", "开放边界"})
    if "平面" in message or "边界" in message:
        return tuple(item.key for item in objects if item.category in {"PCB叠层", "开放边界"})
    return ()


def _apply_validation_states(objects: Sequence[SceneObject], validation: Optional[Any]) -> None:
    """Attach honest local/HFSS state without promoting inferred geometry to HFSS objects."""
    if validation is None:
        return
    report_items = list(getattr(validation, "items", ()) or ())
    stale = bool(getattr(validation, "stale", False))
    verified_port_rules = {
        str(getattr(item, "rule_id", "")) for item in report_items
        if getattr(item, "source", "local") == "hfss" and getattr(item, "level", "") == "info"
    }
    ports_actually_verified = (
        not stale
        and not bool(getattr(validation, "incomplete", False))
        and {"PORT-011", "PORT-012", "PORT-013"}.issubset(verified_port_rules)
    )
    for obj in objects:
        matches = []
        for issue in report_items:
            keys = tuple(str(value) for value in getattr(issue, "object_keys", ()) or ())
            if obj.key in keys or obj.name in keys:
                matches.append(issue)
                continue
            if obj.key == "open_boundary" and "airbox" in keys:
                matches.append(issue)
        if stale:
            obj.status = "Validate结果已过期"
            obj.validate_result = "已过期，需重新运行 Validate"
            continue
        levels = {str(getattr(issue, "level", "")) for issue in matches}
        hfss_matches = [issue for issue in matches if getattr(issue, "source", "local") == "hfss"]
        if "error" in levels:
            obj.status = "创建失败" if any(getattr(issue, "level", "") == "error" for issue in hfss_matches) else "有错误"
            obj.validate_result = "错误"
        elif "warning" in levels:
            obj.status = "有警告"
            obj.validate_result = "警告"
        elif (obj.category != "Port" or ports_actually_verified) and any(
                getattr(issue, "level", "") == "info" for issue in hfss_matches):
            obj.status = "HFSS已验证"
            obj.data_source = "HFSS实际工程"
            obj.validate_result = "通过"
        elif obj.category == "Port" and hfss_matches and not ports_actually_verified:
            obj.status = "本地预估"
            obj.validate_result = "HFSS实际端口验证未完成"
        elif report_items:
            obj.validate_result = "通过（无对象级问题）"


def _electrical_model_details(config: Mapping[str, Any]) -> Dict[str, Any]:
    model = config.get("capacitor_model", {})
    mode = str(model.get("mode", "ideal_bridge"))
    mode_text = {
        "ideal_bridge": "理想短路（不模拟电容）",
        "lumped_rlc": "串联 Lumped RLC",
        "sparameter": "Touchstone S参数",
        "open": "开路（仅焊盘）",
        "touchstone": "Touchstone S参数",
    }.get(mode, mode)
    touchstone = (model.get("touchstone_file") or
                  " / ".join(filter(None, (str(model.get("touchstone_file_p", "")),
                                             str(model.get("touchstone_file_n", ""))))) or "—")
    return {
        "说明": "说明性电气模型标记（非实体几何）",
        "模型模式": mode_text,
        "电容值": f"{format_number(float(model.get('capacitance_pf', 0.0)), 6)} pF",
        "ESR": f"{format_number(float(model.get('esr_ohm', 0.0)), 6)} Ω",
        "ESL": f"{format_number(float(model.get('esl_nh', 0.0)), 6)} nH",
        "Touchstone文件": touchstone,
    }


def build_model_scene(config: Mapping[str, Any], geometry: Any,
                      validation: Optional[Any] = None) -> ModelScene:
    """Translate calculated HFSS geometry into render objects without new placement math."""
    require_known_scenario(config)
    if scenario_type(config) == VIA_TRANSITION:
        return build_via_transition_scene(config, geometry, validation)
    objects: List[SceneObject] = []
    layers = list(geometry.stackup)
    by_layer = _layer_map(geometry)
    plane_points = _rectangle(geometry.plane_bounds)
    layer_indices = {layer.name: index for index, layer in enumerate(layers)}
    surface_signal = next(layer for layer in layers
                          if layer.name == geometry.signal_layer_name)
    surface_signal_index = layer_indices[surface_signal.name]
    soldermask_holes: Dict[str, Tuple[Tuple[Tuple[float, float], ...], ...]] = {}
    for layer_name in geometry.surface_soldermask_layers:
        soldermask_holes[layer_name] = tuple(
            tuple((float(point[0]), float(point[1])) for point in opening["points"])
            for opening in geometry.soldermask_openings
            if opening["layer"] == layer_name
        )

    for index, layer in enumerate(layers):
        common = dict(
            key=f"layer:{layer.name}", name=str(layer.name), category="PCB叠层",
            layer=str(layer.name), material=str(layer.material),
            z_bottom=float(layer.z_bottom), z_top=float(layer.z_top),
            points_xy=plane_points, source_paths=(f"stackup.{index}",), layer_index=index,
            details={"层类型": layer.layer_type, "厚度": float(layer.thickness),
                     "Z bottom": float(layer.z_bottom), "Z top": float(layer.z_top)},
        )
        if layer.layer_type in {"dielectric", "soldermask"}:
            objects.append(SceneObject(
                **common,
                object_type=("plane_with_holes" if layer.layer_type == "soldermask"
                             and soldermask_holes.get(layer.name) else "polygon_prism"),
                holes_xy=soldermask_holes.get(layer.name, ()),
                color_key="soldermask" if layer.layer_type == "soldermask" else "dielectric",
                opacity=.24 if layer.layer_type == "dielectric" else .34,
            ))
        elif layer.layer_type == "plane" and layer.is_reference_plane:
            antipads = tuple(_shape_points(shape) for shape in geometry.antipads_by_layer.get(layer.name, []))
            via_holes = tuple((float(via.x), float(via.y), float(via.antipad_diameter) / 2.0)
                              for via in geometry.gnd_vias if layer.name in via.clearance_layers)
            objects.append(SceneObject(
                **common, object_type="plane_with_holes", holes_xy=antipads,
                circular_holes=via_holes, color_key="plane", opacity=.70,
            ))
        else:
            objects.append(SceneObject(
                **common, object_type="outline", color_key="copper", opacity=.45,
                default_visible=False,
            ))

    for signal in ("P", "N"):
        for shape in geometry.signal_shapes[signal]:
            if shape.category in {"pad", "bridge"}:
                continue
            objects.append(SceneObject(
                key=f"signal:{shape.name}", name=shape.name, category="信号走线",
                object_type="polygon_prism", layer=surface_signal.name,
                material=str(shape.material), z_bottom=float(shape.z_bottom),
                z_top=float(shape.z_bottom + shape.thickness), points_xy=_shape_points(shape),
                source_paths=("trace.trace_width", "capacitor.mount_side"), color_key=signal,
                layer_index=surface_signal_index,
                details={"信号": signal, "拓扑段": shape.category,
                         "厚度": float(shape.thickness), **shape.metadata},
            ))

    for name, shape in geometry.pad_shapes.items():
        objects.append(SceneObject(
            key=f"pad:{name}", name=shape.name, category="电容与焊盘",
            object_type="polygon_prism", layer=surface_signal.name, material=str(shape.material),
            z_bottom=float(shape.z_bottom), z_top=float(shape.z_bottom + shape.thickness),
            points_xy=_shape_points(shape), source_paths=("capacitor.pad_length", "capacitor.pad_width",
                                                              "capacitor.pad_pitch", "capacitor.pad_rotation"),
            color_key="copper", layer_index=surface_signal_index,
            details={"结构": "电容焊盘", "旋转角": float(shape.rotation_deg), **shape.metadata},
        ))
    for signal, shape in geometry.bridge_shapes.items():
        objects.append(SceneObject(
            key=f"bridge:{signal}", name=shape.name, category="电容与焊盘",
            object_type="polygon_prism", layer=surface_signal.name, material=str(shape.material),
            z_bottom=float(shape.z_bottom), z_top=float(shape.z_bottom + shape.thickness),
            points_xy=_shape_points(shape), source_paths=("capacitor.bridge_width", "capacitor.bridge_length"),
            color_key=signal, layer_index=surface_signal_index,
            details={"结构": "理想短路铜桥", "信号": signal,
                     "警告": "电容值、ESR和ESL不参与仿真", **shape.metadata},
        ))

    electrical_details = _electrical_model_details(config)
    for signal, shape in geometry.capacitor_model_shapes.items():
        xs = [float(point.x) for point in shape.points]
        ys = [float(point.y) for point in shape.points]
        objects.append(SceneObject(
            key=f"electrical:{signal}", name=shape.name, category="电气模型",
            object_type="outline", layer=surface_signal.name, material="boundary sheet",
            z_bottom=float(shape.z_bottom), z_top=float(shape.z_bottom),
            points_xy=_shape_points(shape),
            source_paths=("capacitor_model.mode", "capacitor_model.sheet_width"),
            color_key="selected", opacity=1.0, layer_index=surface_signal_index,
            details={
                "结构": "RLC Sheet" if shape.metadata.get("mode") == "lumped_rlc" else "Touchstone连接Sheet",
                "信号": signal,
                "integration_line_start": tuple(shape.metadata.get("integration_line_start", ())),
                "integration_line_end": tuple(shape.metadata.get("integration_line_end", ())),
                "积分线方向": "输入PAD内侧边缘 → 输出PAD内侧边缘",
                **electrical_details,
            },
        ))
    for body in geometry.capacitor_bodies:
        points = tuple((float(point[0]), float(point[1])) for point in body["points"])
        objects.append(SceneObject(
            key=f"body:{body['capacitor']}", name=str(body["name"]), category="电容与焊盘",
            object_type="polygon_prism", layer=surface_signal.name, material=str(body["material"]),
            z_bottom=float(body["z_bottom"]), z_top=float(body["z_top"]), points_xy=points,
            source_paths=("capacitor.body_length", "capacitor.body_width", "capacitor.body_height"),
            color_key="body", opacity=.78, layer_index=surface_signal_index,
            details={"结构": "电容本体", "HFSS中创建": bool(body["hfss_enabled"]),
                     "长度": float(config["capacitor"]["body_length"]),
                     "宽度": float(config["capacitor"]["body_width"]),
                     "高度": float(body["height"])},
        ))

    for layer_name, shapes in geometry.antipads_by_layer.items():
        layer = by_layer[layer_name]
        for shape in shapes:
            objects.append(SceneObject(
                key=f"antipad:{layer_name}:{shape.name}", name=shape.name, category="反焊盘",
                object_type="outline", layer=layer_name, material="vacuum/opening",
                z_bottom=float(layer.z_bottom), z_top=float(layer.z_top), points_xy=_shape_points(shape),
                source_paths=("antipad.mode", "antipad.global_expansion"), color_key="antipad",
                layer_index=layer_indices[layer_name],
                details={"模式": shape.metadata.get("mode"), "外扩": shape.metadata.get("expansion"),
                         "源焊盘": ", ".join(shape.metadata.get("source_pads", []))},
            ))

    for via in geometry.gnd_vias:
        barrel_inner = float(via.finished_hole_diameter) / 2.0 if via.barrel_model == "hollow" else 0.0
        objects.append(SceneObject(
            key=f"via:{via.name}:barrel", name=f"{via.name}_BARREL", category="GND VIA",
            object_type="tube", material=str(via.material), z_bottom=float(via.z_bottom),
            z_top=float(via.z_top), center_xy=(float(via.x), float(via.y)),
            outer_radius=float(via.drill_diameter) / 2.0, inner_radius=barrel_inner,
            source_paths=("gnd_vias.drill_diameter", "gnd_vias.plating_thickness", "gnd_vias.barrel_model"),
            color_key="via", details={"结构": "镀铜桶壁" if via.barrel_model == "hollow" else "实心圆柱",
                                      "起始层": via.start_layer, "终止层": via.end_layer,
                                      "跨越层": ", ".join(via.traversed_layers)},
        ))
        objects.append(SceneObject(
            key=f"via:{via.name}:hole", name=f"{via.name}_FINISHED_HOLE", category="GND VIA",
            object_type="tube", material="air", z_bottom=float(via.z_bottom), z_top=float(via.z_top),
            center_xy=(float(via.x), float(via.y)), outer_radius=float(via.finished_hole_diameter) / 2.0,
            source_paths=("gnd_vias.finished_hole_diameter",), color_key="hole", opacity=.32,
            details={"结构": "成品孔", "直径": float(via.finished_hole_diameter)},
        ))
        for layer_name in via.connect_layers:
            layer = by_layer[layer_name]
            objects.append(SceneObject(
                key=f"via:{via.name}:pad:{layer_name}", name=f"{via.name}_PAD_{layer_name}",
                category="GND VIA", object_type="tube", layer=layer_name, material=str(via.material),
                z_bottom=float(layer.z_bottom), z_top=float(layer.z_top),
                center_xy=(float(via.x), float(via.y)), outer_radius=float(via.pad_diameter) / 2.0,
                inner_radius=barrel_inner, source_paths=("gnd_vias.pad_diameter", "gnd_vias.connect_layers"),
                color_key="copper", layer_index=layer_indices[layer_name],
                details={"结构": "连接层焊盘", "直径": float(via.pad_diameter)},
            ))
        for layer_name in via.clearance_layers:
            layer = by_layer[layer_name]
            objects.append(SceneObject(
                key=f"via:{via.name}:clearance:{layer_name}", name=f"{via.name}_ANTIPAD_{layer_name}",
                category="GND VIA", object_type="circle_outline", layer=layer_name,
                material="vacuum/opening", z_bottom=float(layer.z_bottom), z_top=float(layer.z_top),
                center_xy=(float(via.x), float(via.y)), outer_radius=float(via.antipad_diameter) / 2.0,
                source_paths=("gnd_vias.antipad_diameter",), color_key="antipad",
                layer_index=layer_indices[layer_name],
                details={"结构": "非连接层避空", "直径": float(via.antipad_diameter)},
            ))

    if geometry.surface_ground:
        top = geometry.surface_ground
        b = top["bounds"]
        keepouts = tuple(((float(item["bounds"]["x_min"]), float(item["bounds"]["y_min"])),
                          (float(item["bounds"]["x_max"]), float(item["bounds"]["y_min"])),
                          (float(item["bounds"]["x_max"]), float(item["bounds"]["y_max"])),
                          (float(item["bounds"]["x_min"]), float(item["bounds"]["y_max"])))
                         for item in top["keepouts"])
        objects.append(SceneObject(
            key="top_ground", name=str(top["name"]), category="安装面GND铜皮",
            object_type="plane_with_holes", layer=str(top["layer"]), material=str(top["material"]),
            z_bottom=float(top["z_bottom"]), z_top=float(top["z_top"]),
            points_xy=((b["x_min"], b["y_min"]), (b["x_max"], b["y_min"]),
                       (b["x_max"], b["y_max"]), (b["x_min"], b["y_max"])),
            holes_xy=keepouts, source_paths=("gnd_vias.create_top_ground_copper", "gnd_vias.top_ground_clearance"),
            color_key="plane", opacity=.48, layer_index=layer_indices[str(top["layer"])],
            details={"结构": "当前安装面GND铜皮", "安装面": geometry.mount_side,
                     "保守AABB净距": float(top["clearance"])},
        ))

    rendered_ports = list(geometry.ports)
    port_placeholders = not rendered_ports and bool(getattr(geometry, "port_placeholders", ()))
    if port_placeholders:
        rendered_ports = list(geometry.port_placeholders)
    for port in rendered_ports:
        reference_name = str(port.get("reference_conductor", ""))
        height = float(port["z_top"]) - float(port["z_bottom"])
        width = float(port["y_max"]) - float(port["y_min"])
        objects.append(SceneObject(
            key=f"port:{port['name']}", name=str(port["name"]), category="Port",
            object_type="port", layer=surface_signal.name, material="port sheet",
            z_bottom=float(port["z_bottom"]), z_top=float(port["z_top"]),
            bounds_xy=(float(port["x"]), float(port["x"]), float(port["y_min"]), float(port["y_max"])),
            source_paths=("simulation.create_differential_ports",),
            color_key="port",
            opacity=.55 if port_placeholders else .82,
            default_visible=True, layer_index=surface_signal_index,
            status="未启用" if port_placeholders else "本地预估",
            validate_result="尚未启用，不会生成到HFSS" if port_placeholders else "未运行 Validate",
            details={
                "占位说明": "灰色预估占位；尚未启用，不会生成到HFSS" if port_placeholders else "实际待生成对象",
                "端口类型": "Wave Port / Terminal",
                "输入/输出": "输入" if port["side"] == "IN" else "输出",
                "P/N信号": port["signal"],
                "位置": f"X={_length_text(config, float(port['x']))}, "
                         f"Y={_length_text(config, (float(port['y_min']) + float(port['y_max'])) / 2.0)}, "
                         f"Z={_length_text(config, (float(port['z_bottom']) + float(port['z_top'])) / 2.0)}",
                "端口平面尺寸": f"{_length_text(config, width)} × {_length_text(config, height)} (Y×Z)",
                "法向方向": port.get("normal_direction", port.get("normal", "—")),
                "传播方向": port.get("propagation_direction", port.get("propagation", "—")),
                "积分线方向": "Signal → Reference",
                "integration_line_start": tuple(port.get("integration_line_start", ())),
                "integration_line_end": tuple(port.get("integration_line_end", ())),
                "积分线起点": tuple(port.get("integration_line_start", ())),
                "积分线终点": tuple(port.get("integration_line_end", ())),
                "信号导体": port.get("signal_conductor", port["source_shape"]),
                "参考导体": reference_name or "未找到参考平面",
                "参考层": port.get("reference_layer", "未找到参考平面"),
                "信号到参考层距离": _length_text(config, float(port.get('reference_distance', 0.0))),
                "端口所在截面": f"X={_length_text(config, float(port['x']))}",
                "Solution Type": port.get("solution_type", config.get("project", {}).get("solution_type", "—")),
                "数据源": port.get("data_source", "Unified PortGeometry"),
                "尺寸模式": port.get("size_mode", "auto"),
                "Y向实际外扩": _length_text(config, float(port.get('lateral_margin', 0.0))),
                "尺寸计算依据": port.get("size_basis", "旧配置：未记录"),
                "板框截断": "是" if port.get("clipped_by_board", False) else "否",
                "几何校验状态": port.get("validation_status", "local_estimate_not_hfss_verified"),
                "源对象": port["source_shape"],
            },
        ))
    air = geometry.airbox or getattr(geometry, "airbox_placeholder", None)
    airbox_is_placeholder = geometry.airbox is None and air is not None
    if air:
        x_size = float(air["x_max"]) - float(air["x_min"])
        y_size = float(air["y_max"]) - float(air["y_min"])
        z_size = float(air["z_top"]) - float(air["z_bottom"])
        objects.append(SceneObject(
            key="airbox", name=str(air["name"]), category="空气盒", object_type="box_outline",
            material="air", z_bottom=float(air["z_bottom"]), z_top=float(air["z_top"]),
            bounds_xy=(float(air["x_min"]), float(air["x_max"]),
                       float(air["y_min"]), float(air["y_max"])),
            source_paths=("simulation.create_airbox", "simulation.airbox_margin"),
            color_key="disabled" if airbox_is_placeholder else "airbox",
            opacity=.34 if airbox_is_placeholder else .72, default_visible=True,
            status="未启用" if airbox_is_placeholder else "本地预估",
            validate_result="尚未启用，不会生成到HFSS" if airbox_is_placeholder else "未运行 Validate",
            details={
                "占位说明": "灰色预估占位；尚未启用，不会生成到HFSS" if airbox_is_placeholder else "实际待生成对象",
                "显示模式": "仅线框（默认）",
                "x_min / x_max": f"{_length_text(config, float(air['x_min']))} / {_length_text(config, float(air['x_max']))}",
                "y_min / y_max": f"{_length_text(config, float(air['y_min']))} / {_length_text(config, float(air['y_max']))}",
                "z_bottom / z_top": f"{_length_text(config, float(air['z_bottom']))} / {_length_text(config, float(air['z_top']))}",
                "X/Y/Z尺寸": f"{_length_text(config, x_size)} × {_length_text(config, y_size)} × {_length_text(config, z_size)}",
                "airbox_margin": _length_text(config, float(air['margin'])),
                "开放边界": air["boundary"],
                "模型包围关系": "PCB完全位于空气盒内部",
                "最近距离": _length_text(config, float(air['margin'])),
            },
        ))
        objects.append(SceneObject(
            key="open_boundary", name=f"OPEN_{str(air['boundary']).upper()}", category="开放边界",
            object_type="box_outline", material=str(air["boundary"]),
            z_bottom=float(air["z_bottom"]), z_top=float(air["z_top"]),
            bounds_xy=(float(air["x_min"]), float(air["x_max"]),
                       float(air["y_min"]), float(air["y_max"])),
            source_paths=("simulation.open_boundary",),
            color_key="disabled" if airbox_is_placeholder else "boundary",
            opacity=.30 if airbox_is_placeholder else .45,
            default_visible=True, details={
                "边界类型": air["boundary"], "分配对象/表面": str(air["name"]) + " 外表面",
                "HFSS分配状态": "未启用，不会生成到HFSS" if airbox_is_placeholder else "待生成（本地预估）",
            },
            status="未启用" if airbox_is_placeholder else "本地预估",
            validate_result="尚未启用，不会生成到HFSS" if airbox_is_placeholder else "未运行 Validate",
        ))

    _apply_validation_states(objects, validation)

    issues: List[SceneIssue] = []
    for message in getattr(geometry, "errors", []):
        issues.append(SceneIssue("error", message, _object_keys_for_message(message, objects),
                                 suggestion="前往对应参数页修正碰撞、净距或边界。", view="顶视图"))
    for message in getattr(geometry, "warnings", []):
        issues.append(SceneIssue("warning", message, _object_keys_for_message(message, objects),
                                 suggestion="复核该结构及制造约束。"))
    for issue in validate_via_workbench(config, geometry):
        if issue.message not in {item.message for item in issues}:
            keys = _object_keys_for_message(issue.message, objects)
            if not keys and issue.path.startswith("gnd_vias."):
                keys = tuple(item.key for item in objects if item.category == "GND VIA")
            issues.append(SceneIssue(issue.level, issue.message, keys, issue.path,
                                     "前往 GND VIA 页面调整制造或布局参数。", "叠层剖视图"))
    if validation is not None:
        known = {item.message for item in issues}
        structured_items = list(getattr(validation, "items", ()) or ())
        if structured_items:
            for report_item in structured_items:
                level = str(getattr(report_item, "level", ""))
                message = str(getattr(report_item, "message", ""))
                if level not in {"error", "warning"} or not message or message in known:
                    continue
                requested_keys = tuple(str(value) for value in getattr(report_item, "object_keys", ()) or ())
                keys = tuple(key for key in requested_keys if key in {item.key for item in objects})
                if not keys:
                    keys = _object_keys_for_message(message, objects)
                issues.append(SceneIssue(
                    level, message, keys,
                    str(getattr(report_item, "parameter_path", "")),
                    str(getattr(report_item, "suggestion", "")) or "运行Validate模型检查并修正对应输入。",
                ))
        else:
            for level, messages in (("error", getattr(validation, "errors", ())),
                                    ("warning", getattr(validation, "warnings", ()))):
                for message in messages:
                    if message not in known:
                        issues.append(SceneIssue(level, message, _object_keys_for_message(message, objects),
                                                 suggestion="运行Validate模型检查并修正对应输入。"))
    if config["simulation"].get("enabled") and config["simulation"].get("create_differential_ports") and len(geometry.ports) != 4:
        issues.append(SceneIssue("error", "端口位置数据不完整，期望四个终端端面。",
                                 tuple(item.key for item in objects if item.category == "Port"),
                                 "simulation.create_differential_ports", "检查解类型和输入/输出直线段。", "顶视图"))
    if geometry.airbox and float(geometry.airbox["margin"]) < max(float(config["trace"]["trace_width"]) * 5.0,
                                                                    geometry.pcb_top_z - geometry.pcb_bottom_z):
        issues.append(SceneIssue("warning", "空气盒边距相对信号宽度或PCB厚度偏小，请复核开放边界距离。",
                                 ("airbox", "open_boundary"), "simulation.airbox_margin",
                                 "增大 airbox_margin，并重新检查边界收敛。", "等轴测"))

    b = geometry.plane_bounds
    z_min, z_max = float(geometry.pcb_bottom_z), float(geometry.pcb_top_z)
    if geometry.airbox:
        z_min, z_max = float(geometry.airbox["z_bottom"]), float(geometry.airbox["z_top"])
    return ModelScene(objects, issues,
                      (float(b.x_min), float(b.x_max), float(b.y_min), float(b.y_max), z_min, z_max),
                      float(geometry.pcb_top_z - geometry.pcb_bottom_z))


def build_via_transition_scene(config: Mapping[str, Any], geometry: Any,
                               validation: Optional[Any] = None) -> ModelScene:
    """Render the via_transition geometry result into the shared 3D scene model."""
    objects: List[SceneObject] = []
    layers = list(geometry.stackup)
    by_layer = {str(layer.name): layer for layer in layers}
    layer_indices = {str(layer.name): index for index, layer in enumerate(layers)}
    plane_points = _rectangle(geometry.plane_bounds)

    # 每层铜平面上的避空（信号孔 + 回流孔）。
    plane_holes: Dict[str, List[Tuple[float, float, float]]] = {}
    plane_polygon_holes: Dict[str, List[Tuple[Tuple[float, float], ...]]] = {}
    for contour in getattr(geometry, "antipad_contours", []):
        layer = by_layer.get(contour.layer)
        if layer is None or not layer.is_reference_plane:
            continue
        plane_polygon_holes.setdefault(contour.layer, []).append(tuple(contour.points))
    for via in geometry.return_vias:
        for layer_name in via.clearance_layers:
            layer = by_layer.get(layer_name)
            if layer is None or not layer.is_reference_plane:
                continue
            plane_holes.setdefault(layer_name, []).append(
                (float(via.x), float(via.y), float(via.antipad_diameter) / 2.0))

    for index, layer in enumerate(layers):
        common = dict(
            key=f"layer:{layer.name}", name=str(layer.name), category="PCB叠层",
            layer=str(layer.name), material=str(layer.material),
            z_bottom=float(layer.z_bottom), z_top=float(layer.z_top),
            points_xy=plane_points, source_paths=(f"stackup.{index}",), layer_index=index,
            details={"层类型": layer.layer_type, "厚度": float(layer.thickness),
                     "Z bottom": float(layer.z_bottom), "Z top": float(layer.z_top)},
        )
        if layer.layer_type in {"dielectric", "soldermask"}:
            objects.append(SceneObject(
                **common, object_type="polygon_prism",
                color_key="soldermask" if layer.layer_type == "soldermask" else "dielectric",
                opacity=.24 if layer.layer_type == "dielectric" else .34,
            ))
        elif layer.layer_type == "plane" and layer.is_reference_plane:
            objects.append(SceneObject(
                **common, object_type="plane_with_holes",
                circular_holes=tuple(plane_holes.get(layer.name, ())),
                holes_xy=tuple(plane_polygon_holes.get(layer.name, ())),
                color_key="plane", opacity=.70,
            ))
        else:
            objects.append(SceneObject(
                **common, object_type="outline", color_key="copper", opacity=.45,
                default_visible=False,
            ))

    # entry/exit 差分走线。
    for trace in list(geometry.entry_traces) + list(geometry.exit_traces):
        is_entry = str(trace["name"]).startswith("ENTRY")
        category = "输入走线" if is_entry else "输出走线"
        trace_side = "entry_trace" if is_entry else "exit_trace"
        trace_config = config.get("via_transition", {}).get(trace_side, {})
        legacy_pair_pitch = float(config.get("via_transition", {}).get("pair_pitch", 0.0))
        trace_gap = float(trace_config.get("gap", legacy_pair_pitch - float(trace["width"])))
        trace_pitch = float(trace["width"]) + trace_gap
        objects.append(SceneObject(
            key=f"trace:{trace['name']}", name=str(trace["name"]), category=category,
            object_type="polygon_prism", layer=str(trace["layer"]), material=str(trace["material"]),
            z_bottom=float(trace["z_bottom"]), z_top=float(trace["z_top"]),
            points_xy=tuple((float(point[0]), float(point[1])) for point in trace["points"]),
            source_paths=(f"via_transition.{trace_side}.length",
                          f"via_transition.{trace_side}.width",
                          f"via_transition.{trace_side}.gap",
                          "via_transition.signal_via_pitch", "via_transition.fanout.style",
                          "via_transition.fanout.straight_length"),
            color_key=str(trace["signal"]), layer_index=layer_indices[str(trace["layer"])],
            details={"信号": str(trace["signal"]), "宽度": float(trace["width"]),
                     "GAP": trace_gap, "中心距": trace_pitch,
                     "长度": float(trace["length"])},
        ))

    # 信号孔：桶壁、成品孔、功能焊盘、非功能焊盘、逐层避空。
    for via in geometry.signal_vias:
        barrel_inner = float(via.finished_hole_diameter) / 2.0 if via.barrel_model == "hollow" else 0.0
        eff_bottom = float(via.effective_barrel_z_bottom)
        eff_top = float(via.effective_barrel_z_top)
        objects.append(SceneObject(
            key=f"via:{via.name}:barrel", name=f"{via.name}_BARREL", category="信号VIA",
            object_type="tube", material=str(via.material), z_bottom=eff_bottom,
            z_top=eff_top, center_xy=(float(via.x), float(via.y)),
            outer_radius=float(via.drill_diameter) / 2.0, inner_radius=barrel_inner,
            source_paths=("via_transition.padstack.drill_diameter",
                          "via_transition.padstack.plating_thickness",
                          "via_transition.padstack.barrel_model",
                          "via_transition.signal_via_pitch"),
            color_key="via", details={"结构": "镀铜桶壁" if via.barrel_model == "hollow" else "实心圆柱",
                                      "信号": via.signal, "起始层": via.drill_start_layer,
                                      "终止层": via.drill_end_layer,
                                      "背钻": "是" if via.backdrilled else "否"},
        ))
        objects.append(SceneObject(
            key=f"via:{via.name}:hole", name=f"{via.name}_FINISHED_HOLE", category="信号VIA",
            object_type="tube", material="air", z_bottom=eff_bottom, z_top=eff_top,
            center_xy=(float(via.x), float(via.y)),
            outer_radius=float(via.finished_hole_diameter) / 2.0,
            source_paths=("via_transition.padstack.finished_hole_diameter",),
            color_key="hole", opacity=.32,
            details={"结构": "成品孔", "直径": float(via.finished_hole_diameter)},
        ))
        if via.backdrilled:
            objects.append(SceneObject(
                key=f"via:{via.name}:backdrill", name=f"{via.name}_BACKDRILL_CAVITY",
                category="背钻腔体", object_type="tube", material="vacuum",
                z_bottom=float(via.backdrill_z_bottom), z_top=float(via.backdrill_z_top),
                center_xy=(float(via.x), float(via.y)),
                outer_radius=float(via.backdrill_diameter) / 2.0,
                source_paths=("via_transition.backdrill.drill_diameter",
                              "via_transition.backdrill.residual_stub"),
                color_key="antipad", opacity=.28,
                details={"结构": "背钻腔体", "方向": via.backdrill_from_side,
                         "目标残桩": float(via.target_residual_stub),
                         "实际残桩": float(via.actual_residual_stub)},
            ))
        for layer_name in via.functional_pad_layers:
            layer = by_layer[layer_name]
            objects.append(SceneObject(
                key=f"via:{via.name}:pad:{layer_name}", name=f"{via.name}_PAD_{layer_name}",
                category="功能焊盘", object_type="tube", layer=str(layer_name),
                material=str(via.material), z_bottom=float(layer.z_bottom), z_top=float(layer.z_top),
                center_xy=(float(via.x), float(via.y)),
                outer_radius=float(via.connected_pad_diameter) / 2.0, inner_radius=barrel_inner,
                source_paths=("via_transition.padstack.connected_pad_diameter",),
                color_key="copper", layer_index=layer_indices[layer_name],
                details={"结构": "功能焊盘", "直径": float(via.connected_pad_diameter)},
            ))
        for layer_name in via.nonfunctional_pad_layers:
            layer = by_layer[layer_name]
            objects.append(SceneObject(
                key=f"via:{via.name}:nfp:{layer_name}", name=f"{via.name}_NFP_{layer_name}",
                category="非功能焊盘", object_type="tube", layer=str(layer_name),
                material=str(via.material), z_bottom=float(layer.z_bottom), z_top=float(layer.z_top),
                center_xy=(float(via.x), float(via.y)),
                outer_radius=float(via.nonfunctional_pad_diameter) / 2.0,
                source_paths=("via_transition.padstack.nonfunctional_pad_diameter",),
                color_key="copper", layer_index=layer_indices[layer_name], opacity=.55,
                details={"结构": "非功能焊盘", "直径": float(via.nonfunctional_pad_diameter)},
            ))

    # Authoritative signal-via openings: independent produces P/N objects,
    # merged_pair produces exactly one object per layer.
    for contour in getattr(geometry, "antipad_contours", []):
        layer = by_layer.get(contour.layer)
        if layer is None:
            continue
        objects.append(SceneObject(
            key=f"via:antipad:{contour.layer}:{contour.owner}",
            name=f"VIA_ANTIPAD_{contour.owner}_{contour.layer}",
            category="VIA反焊盘", object_type="polygon_outline", layer=contour.layer,
            material="vacuum/opening", z_bottom=float(layer.z_bottom), z_top=float(layer.z_top),
            points_xy=tuple(contour.points),
            source_paths=("via_transition.padstack.antipad",),
            color_key="antipad", layer_index=layer_indices[contour.layer],
            details={"结构": "非连接层避空", "拓扑": contour.topology,
                     "形状": contour.shape, "来源": contour.source,
                     "面积": contour.area, "旋转角": contour.rotation_deg},
        ))

    # 回流孔。
    for via in geometry.return_vias:
        barrel_inner = float(via.finished_hole_diameter) / 2.0 if via.barrel_model == "hollow" else 0.0
        objects.append(SceneObject(
            key=f"via:{via.name}:barrel", name=f"{via.name}_BARREL", category="回流孔",
            object_type="tube", material=str(via.material), z_bottom=float(via.z_bottom),
            z_top=float(via.z_top), center_xy=(float(via.x), float(via.y)),
            outer_radius=float(via.drill_diameter) / 2.0, inner_radius=barrel_inner,
            source_paths=("via_transition.return_vias.drill_diameter",),
            color_key="via", details={"结构": "回流孔桶壁", "起始层": via.start_layer,
                                      "终止层": via.end_layer},
        ))
        for layer_name in via.connect_layers:
            layer = by_layer[layer_name]
            objects.append(SceneObject(
                key=f"via:{via.name}:pad:{layer_name}", name=f"{via.name}_PAD_{layer_name}",
                category="回流孔", object_type="tube", layer=str(layer_name),
                material=str(via.material), z_bottom=float(layer.z_bottom), z_top=float(layer.z_top),
                center_xy=(float(via.x), float(via.y)),
                outer_radius=float(via.pad_diameter) / 2.0, inner_radius=barrel_inner,
                source_paths=("via_transition.return_vias.pad_diameter",),
                color_key="copper", layer_index=layer_indices[layer_name],
                details={"结构": "回流孔连接焊盘", "直径": float(via.pad_diameter)},
            ))

    # 端口。
    for port in geometry.ports:
        pd = port.to_dict()
        width = ((float(pd["x_max"]) - float(pd["x_min"]))
                 if pd.get("orientation") == "XZ"
                 else (float(pd["y_max"]) - float(pd["y_min"])))
        height = float(pd["z_max"]) - float(pd["z_min"])
        objects.append(SceneObject(
            key=f"port:{pd['name']}", name=str(pd["name"]), category="Port",
            object_type="port", layer=pd["signal_layer"], material="port sheet",
            z_bottom=float(pd["z_min"]), z_top=float(pd["z_max"]),
            bounds_xy=(float(pd["x_min"]), float(pd["x_max"]),
                       float(pd["y"]) if pd.get("orientation") == "XZ" else float(pd["y_min"]),
                       float(pd["y"]) if pd.get("orientation") == "XZ" else float(pd["y_max"])),
            source_paths=("simulation.create_differential_ports",),
            color_key="port", opacity=.82, default_visible=True,
            layer_index=layer_indices[pd["signal_layer"]],
            details={
                "端口类型": "Wave Port / Terminal",
                "输入/输出": "输入" if pd["side"] == "IN" else "输出",
                "P/N信号": pd["signal"],
                "信号层": pd["signal_layer"],
                "参考层": pd["reference_layer"] or "未找到参考平面",
                "参考导体": pd["reference_conductor"] or "未找到参考平面",
                "端口平面尺寸": f"{_length_text(config, width)} × {_length_text(config, height)} "
                              f"({'X' if pd.get('orientation') == 'XZ' else 'Y'}×Z)",
                "传播方向": pd["propagation_direction"],
                "积分线方向": "Signal → Reference",
                "integration_line_start": tuple(pd["integration_line_start"]),
                "integration_line_end": tuple(pd["integration_line_end"]),
            },
        ))

    # 空气盒 + 开放边界。
    air = geometry.airbox
    if air:
        objects.append(SceneObject(
            key="airbox", name=str(air["name"]), category="空气盒", object_type="box_outline",
            material="air", z_bottom=float(air["z_bottom"]), z_top=float(air["z_top"]),
            bounds_xy=(float(air["x_min"]), float(air["x_max"]),
                       float(air["y_min"]), float(air["y_max"])),
            source_paths=("simulation.create_airbox", "simulation.airbox_margin"),
            color_key="airbox", opacity=.72, default_visible=True,
            details={"airbox_margin": _length_text(config, float(air['margin'])),
                     "开放边界": air["boundary"]},
        ))
        objects.append(SceneObject(
            key="open_boundary", name=f"OPEN_{str(air['boundary']).upper()}", category="开放边界",
            object_type="box_outline", material=str(air["boundary"]),
            z_bottom=float(air["z_bottom"]), z_top=float(air["z_top"]),
            bounds_xy=(float(air["x_min"]), float(air["x_max"]),
                       float(air["y_min"]), float(air["y_max"])),
            source_paths=("simulation.open_boundary",), color_key="boundary",
            opacity=.45, default_visible=True,
            details={"边界类型": air["boundary"], "分配对象/表面": str(air["name"]) + " 外表面"},
        ))

    _apply_validation_states(objects, validation)

    issues: List[SceneIssue] = []
    for message in getattr(geometry, "errors", []):
        issues.append(SceneIssue("error", message, _object_keys_for_message(message, objects),
                                 suggestion="前往 VIA 换层页面修正碰撞、净距或边界。", view="顶视图"))
    for message in getattr(geometry, "warnings", []):
        issues.append(SceneIssue("warning", message, _object_keys_for_message(message, objects),
                                 suggestion="复核该结构及制造约束。"))
    if validation is not None:
        known = {item.message for item in issues}
        structured_items = list(getattr(validation, "items", ()) or ())
        for report_item in structured_items:
            level = str(getattr(report_item, "level", ""))
            message = str(getattr(report_item, "message", ""))
            if level not in {"error", "warning"} or not message or message in known:
                continue
            requested_keys = tuple(str(value) for value in getattr(report_item, "object_keys", ()) or ())
            keys = tuple(key for key in requested_keys if key in {item.key for item in objects})
            if not keys:
                keys = _object_keys_for_message(message, objects)
            issues.append(SceneIssue(
                level, message, keys,
                str(getattr(report_item, "parameter_path", "")),
                str(getattr(report_item, "suggestion", "")) or "运行Validate模型检查并修正对应输入。",
            ))

    b = geometry.plane_bounds
    z_min, z_max = float(geometry.pcb_bottom_z), float(geometry.pcb_top_z)
    if geometry.airbox:
        z_min, z_max = float(geometry.airbox["z_bottom"]), float(geometry.airbox["z_top"])
    return ModelScene(objects, issues,
                      (float(b.x_min), float(b.x_max), float(b.y_min), float(b.y_max), z_min, z_max),
                      float(geometry.pcb_top_z - geometry.pcb_bottom_z))


def _polygon_polydata(points_xy: Sequence[Tuple[float, float]], z: float) -> Any:
    points = vtk.vtkPoints()
    polygon = vtk.vtkPolygon()
    polygon.GetPointIds().SetNumberOfIds(len(points_xy))
    for index, (x, y) in enumerate(points_xy):
        points.InsertNextPoint(x, y, z)
        polygon.GetPointIds().SetId(index, index)
    cells = vtk.vtkCellArray()
    cells.InsertNextCell(polygon)
    data = vtk.vtkPolyData()
    data.SetPoints(points)
    data.SetPolys(cells)
    return data


def _extrude_polygon(points_xy: Sequence[Tuple[float, float]], z_bottom: float, z_top: float) -> Any:
    extrusion = vtk.vtkLinearExtrusionFilter()
    extrusion.SetInputData(_polygon_polydata(points_xy, z_bottom))
    extrusion.SetExtrusionTypeToVectorExtrusion()
    extrusion.SetVector(0, 0, z_top - z_bottom)
    extrusion.CappingOn()
    extrusion.Update()
    triangle = vtk.vtkTriangleFilter()
    triangle.SetInputConnection(extrusion.GetOutputPort())
    triangle.Update()
    output = vtk.vtkPolyData()
    output.DeepCopy(triangle.GetOutput())
    return output


def _box_polydata(bounds_xy: Tuple[float, float, float, float], z_bottom: float, z_top: float) -> Any:
    source = vtk.vtkCubeSource()
    source.SetBounds(bounds_xy[0], bounds_xy[1], bounds_xy[2], bounds_xy[3], z_bottom, z_top)
    source.Update()
    triangle = vtk.vtkTriangleFilter()
    triangle.SetInputConnection(source.GetOutputPort())
    triangle.Update()
    output = vtk.vtkPolyData()
    output.DeepCopy(triangle.GetOutput())
    return output


def _port_sheet_polydata(bounds_xy: Tuple[float, float, float, float],
                         z_bottom: float, z_top: float) -> Any:
    """Return the exact zero-thickness YZ PortGeometry sheet (no visual padding)."""

    x = bounds_xy[0]
    coordinates = ((x, bounds_xy[2], z_bottom), (x, bounds_xy[3], z_bottom),
                   (x, bounds_xy[3], z_top), (x, bounds_xy[2], z_top))
    points, cells, polygon = vtk.vtkPoints(), vtk.vtkCellArray(), vtk.vtkPolygon()
    polygon.GetPointIds().SetNumberOfIds(4)
    for index, point in enumerate(coordinates):
        points.InsertNextPoint(*point); polygon.GetPointIds().SetId(index, index)
    cells.InsertNextCell(polygon)
    data = vtk.vtkPolyData(); data.SetPoints(points); data.SetPolys(cells)
    return data


def _box_outline_polydata(bounds_xy: Tuple[float, float, float, float],
                          z_bottom: float, z_top: float) -> Any:
    """Return only the 12 physical cuboid edges, with no triangulation diagonals."""
    x_min, x_max, y_min, y_max = bounds_xy
    coordinates = (
        (x_min, y_min, z_bottom), (x_max, y_min, z_bottom),
        (x_max, y_max, z_bottom), (x_min, y_max, z_bottom),
        (x_min, y_min, z_top), (x_max, y_min, z_top),
        (x_max, y_max, z_top), (x_min, y_max, z_top),
    )
    edges = ((0, 1), (1, 2), (2, 3), (3, 0),
             (4, 5), (5, 6), (6, 7), (7, 4),
             (0, 4), (1, 5), (2, 6), (3, 7))
    points, lines = vtk.vtkPoints(), vtk.vtkCellArray()
    for point in coordinates:
        points.InsertNextPoint(*point)
    for first, second in edges:
        lines.InsertNextCell(2); lines.InsertCellPoint(first); lines.InsertCellPoint(second)
    data = vtk.vtkPolyData(); data.SetPoints(points); data.SetLines(lines)
    return data


def _tube_polydata(center: Tuple[float, float], outer: float, inner: float,
                   z_bottom: float, z_top: float) -> Any:
    disk = vtk.vtkDiskSource()
    disk.SetCenter(center[0], center[1], z_bottom)
    disk.SetInnerRadius(max(0.0, inner))
    disk.SetOuterRadius(outer)
    disk.SetRadialResolution(2)
    disk.SetCircumferentialResolution(40)
    extrusion = vtk.vtkLinearExtrusionFilter()
    extrusion.SetInputConnection(disk.GetOutputPort())
    extrusion.SetExtrusionTypeToVectorExtrusion()
    extrusion.SetVector(0, 0, z_top - z_bottom)
    extrusion.CappingOn()
    extrusion.Update()
    triangle = vtk.vtkTriangleFilter()
    triangle.SetInputConnection(extrusion.GetOutputPort())
    triangle.Update()
    output = vtk.vtkPolyData()
    output.DeepCopy(triangle.GetOutput())
    return output


def _boolean_difference(base: Any, tool: Any) -> Any:
    clean_a, clean_b = vtk.vtkCleanPolyData(), vtk.vtkCleanPolyData()
    clean_a.SetInputData(base); clean_a.Update()
    clean_b.SetInputData(tool); clean_b.Update()
    operation = vtk.vtkBooleanOperationPolyDataFilter()
    operation.SetOperationToDifference()
    operation.SetInputData(0, clean_a.GetOutput())
    operation.SetInputData(1, clean_b.GetOutput())
    operation.Update()
    if operation.GetOutput().GetNumberOfCells() == 0:
        return base
    output = vtk.vtkPolyData()
    output.DeepCopy(operation.GetOutput())
    return output


def _plane_with_holes_polydata(item: SceneObject) -> Any:
    """Triangulate the exact 2D opening union, then extrude by real layer thickness."""
    surface = ShapelyPolygon(item.points_xy)
    openings: List[Any] = [ShapelyPolygon(points) for points in item.holes_xy]
    openings.extend(ShapelyPoint(x, y).buffer(radius, quad_segs=20)
                    for x, y, radius in item.circular_holes)
    if openings:
        surface = surface.difference(shapely_union(openings))
    vtk_points, cells = vtk.vtkPoints(), vtk.vtkCellArray()

    def add_face(coords: Sequence[Tuple[float, float, float]]) -> None:
        face = vtk.vtkPolygon(); face.GetPointIds().SetNumberOfIds(len(coords))
        for index, point in enumerate(coords):
            point_id = vtk_points.InsertNextPoint(*point); face.GetPointIds().SetId(index, point_id)
        cells.InsertNextCell(face)

    polygons = list(surface.geoms) if hasattr(surface, "geoms") else [surface]
    for polygon in polygons:
        for triangle in shapely_triangulate(polygon):
            if not polygon.covers(triangle.representative_point()):
                continue
            coords = tuple((float(x), float(y)) for x, y in list(triangle.exterior.coords)[:-1])
            add_face([(x, y, item.z_top) for x, y in coords])
            add_face([(x, y, item.z_bottom) for x, y in reversed(coords)])
        for ring in (polygon.exterior, *polygon.interiors):
            coords = [(float(x), float(y)) for x, y in list(ring.coords)]
            for first, second in zip(coords, coords[1:]):
                add_face(((first[0], first[1], item.z_bottom),
                          (second[0], second[1], item.z_bottom),
                          (second[0], second[1], item.z_top),
                          (first[0], first[1], item.z_top)))
    output = vtk.vtkPolyData(); output.SetPoints(vtk_points); output.SetPolys(cells)
    return output


def mesh_for_object(item: SceneObject) -> Any:
    eps = max(1e-5, abs(item.z_top - item.z_bottom) * .05)
    if item.object_type == "polygon_prism":
        return _extrude_polygon(item.points_xy, item.z_bottom, item.z_top)
    if item.object_type in {"box", "port"}:
        if item.object_type == "port":
            return _port_sheet_polydata(item.bounds_xy or (0, 0, 0, 0), item.z_bottom, item.z_top)
        return _box_polydata(item.bounds_xy or (0, 0, 0, 0), item.z_bottom, item.z_top)
    if item.object_type == "box_outline":
        return _box_outline_polydata(item.bounds_xy or (0, 0, 0, 0), item.z_bottom, item.z_top)
    if item.object_type == "tube":
        return _tube_polydata(item.center_xy or (0, 0), item.outer_radius, item.inner_radius,
                              item.z_bottom, item.z_top)
    if item.object_type == "plane_with_holes":
        return _plane_with_holes_polydata(item)
    # Outline types are represented as line polydata.
    points = item.points_xy
    if item.object_type == "circle_outline":
        center = item.center_xy or (0, 0)
        points = tuple((center[0] + item.outer_radius * math.cos(index * 2 * math.pi / 48),
                        center[1] + item.outer_radius * math.sin(index * 2 * math.pi / 48))
                       for index in range(48))
    vtk_points, lines = vtk.vtkPoints(), vtk.vtkCellArray()
    polyline = vtk.vtkPolyLine()
    polyline.GetPointIds().SetNumberOfIds(len(points) + 1)
    z = (item.z_bottom + item.z_top) / 2.0
    for index, (x, y) in enumerate(points):
        vtk_points.InsertNextPoint(x, y, z)
        polyline.GetPointIds().SetId(index, index)
    polyline.GetPointIds().SetId(len(points), 0)
    lines.InsertNextCell(polyline)
    data = vtk.vtkPolyData(); data.SetPoints(vtk_points); data.SetLines(lines)
    return data


class ModelPreviewWorkbench(ttk.Frame):
    """Three-column VTK workbench with selection, measurement and layer controls."""

    def __init__(self, master: tk.Misc, app: Any,
                 navigate: Callable[[str], None]) -> None:
        super().__init__(master)
        self.pack(fill="both", expand=True)
        self.app, self.navigate = app, navigate
        self.scene: Optional[ModelScene] = None
        self.current_config: Mapping[str, Any] = {}
        self.current_snapshot: Optional[Mapping[str, Any]] = None
        self.current_validation: Optional[Any] = None
        self.pending: Optional[Tuple[Mapping[str, Any], Any, Any, Mapping[str, Any]]] = None
        self._update_job: Optional[str] = None
        self._selection_job: Optional[str] = None
        self._hover_job: Optional[str] = None
        self._hover_position: Tuple[int, int] = (0, 0)
        self._hover_related: set[str] = set()
        self._selection_sync = False
        self.objects: Dict[str, SceneObject] = {}
        self.actors: Dict[str, Any] = {}
        self.decoration_actors: Dict[str, List[Any]] = {}
        self.actor_keys: Dict[str, str] = {}
        self.issue_levels: Dict[str, str] = {}
        self.fingerprints: Dict[str, str] = {}
        self.visibility: Dict[str, bool] = {}
        self.opacity_overrides: Dict[str, float] = {}
        self.locked: set[str] = set()
        self.selected_key = ""
        self.measure_points: List[Tuple[float, float, float]] = []
        self.measure_actors: List[Any] = []
        self.validation_marker_actors: List[Any] = []
        self.measure_mode = tk.BooleanVar(value=False)
        self.realtime = tk.BooleanVar(value=True)
        self.exploded = tk.BooleanVar(value=False)
        self.explosion = tk.DoubleVar(value=.18)
        self.xray = tk.BooleanVar(value=False)
        self.hide_dielectric = tk.BooleanVar(value=False)
        # Display controls never mutate SceneObject geometry.  Real scale is the
        # safe default; enhanced stackup is an explicit inspection aid.
        self.z_display_mode = tk.StringVar(value="真实比例")
        self.z_scale_var = tk.StringVar(value="1×")
        self.slice_axis = tk.StringVar(value="无")
        self.slice_position = tk.DoubleVar(value=50.0)
        self.status_var = tk.StringVar(value="等待模型数据")
        self.stale_var = tk.StringVar(value="")
        self.hover_var = tk.StringVar(value="悬停对象可查看名称与层信息")
        self.detail_var = tk.StringVar(value="尚未选择对象")
        self.summary_var = tk.StringVar(value="等待仿真配置")
        self.measure_var = tk.StringVar(value="测距：关闭")
        self.vtk_ready = False
        self.software_ready = False
        self.software_view = "等轴测"
        self._software_fit_keys: set[str] = set()
        self._canvas_job: Optional[str] = None
        self._canvas_tag_keys: Dict[str, str] = {}
        self._camera_yaw = math.radians(-45.0)
        self._camera_pitch = math.radians(28.0)
        self._camera_target = [0.0, 0.0, 0.0]
        self._camera_distance = 10.0
        self._camera_scale = 100.0
        self._camera_initialized = False
        self._last_visual_z_scale = 1.0
        self._last_visual_mode = "真实比例"
        self._last_fit_signature: Optional[Tuple[float, ...]] = None
        self._pointer_mode = ""
        self._pointer_start = (0, 0)
        self._pointer_last = (0, 0)
        self._pointer_dragged = False
        self.diagnostics_enabled = tk.BooleanVar(value=False)
        self.quick_vars = {
            "Port": tk.BooleanVar(value=True),
            "空气盒": tk.BooleanVar(value=True),
            "开放边界": tk.BooleanVar(value=True),
            "PCB叠层": tk.BooleanVar(value=True),
            "电气模型": tk.BooleanVar(value=True),
            "电容本体": tk.BooleanVar(value=True),
        }
        self.only_simulation = tk.BooleanVar(value=False)
        self.airbox_mode = tk.StringVar(value="仅线框")
        self.quick_buttons: Dict[str, ttk.Checkbutton] = {}
        self._clip_plane = None
        self._build_layout()

    def _shown_length(self, mm_value: float) -> str:
        unit = str(getattr(self.app, "display_unit", "") or _display_unit(self.current_config))
        return f"{format_length(float(mm_value), unit)} {unit}"

    def _build_layout(self) -> None:
        toolbar = ttk.Frame(self, padding=(3, 3))
        self.toolbar = toolbar
        toolbar.pack(fill="x")
        toolbar_views = ttk.Frame(toolbar)
        toolbar_views.pack(fill="x")
        toolbar_fit = ttk.Frame(toolbar)
        toolbar_fit.pack(fill="x", pady=(2, 0))
        toolbar_options = ttk.Frame(toolbar)
        toolbar_options.pack(fill="x", pady=(2, 0))
        for text, view in (("等轴", "等轴测"), ("顶", "顶视图"), ("底", "底视图"),
                           ("前", "前视图"), ("后", "后视图"), ("左", "左视图"),
                           ("右", "右视图"), ("剖视", "叠层剖视图")):
            ttk.Button(toolbar_views, text=text, width=4,
                       command=lambda value=view: self.set_view(value)).pack(side="left", padx=1)
        ttk.Button(toolbar_fit, text="适应模型", command=self.fit_model).pack(side="left", padx=(1, 1))
        ttk.Button(toolbar_fit, text="适应全部", command=self.fit_all).pack(side="left", padx=1)
        ttk.Button(toolbar_fit, text="默认视角", command=lambda: self.set_view("等轴测")).pack(side="left", padx=1)
        ttk.Label(toolbar_options, text="显示：").pack(side="left", padx=(1, 1))
        display_combo = ttk.Combobox(toolbar_options, textvariable=self.z_display_mode,
                                     values=("真实比例", "增强叠层"), state="readonly", width=8)
        display_combo.pack(side="left")
        display_combo.bind("<<ComboboxSelected>>", lambda _e: self._display_mode_changed())
        ttk.Label(toolbar_options, text="统一Z：").pack(side="left", padx=(5, 1))
        z_combo = ttk.Combobox(toolbar_options, textvariable=self.z_scale_var,
                               values=("1×", "2×", "5×", "10×"), state="readonly", width=5)
        z_combo.pack(side="left"); z_combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_transforms())
        ttk.Checkbutton(
            toolbar_options, text="3D诊断", variable=self.diagnostics_enabled,
            command=self._software_redraw,
        ).pack(side="left", padx=(8, 1))
        ttk.Checkbutton(toolbar_options, text="实时刷新", variable=self.realtime,
                        command=self._realtime_changed).pack(side="right")
        ttk.Button(toolbar_options, text="更新预览", command=self.update_pending).pack(side="right", padx=3)

        quick = ttk.LabelFrame(self, text="快捷显示（与结构树同步）", padding=(5, 3))
        self.quick_bar = quick
        quick.pack(fill="x", padx=3, pady=(0, 3))
        quick_categories = ttk.Frame(quick)
        quick_categories.pack(fill="x")
        quick_options = ttk.Frame(quick)
        quick_options.pack(fill="x", pady=(2, 0))
        for category, label in (("Port", "Port"), ("空气盒", "空气盒"),
                                 ("开放边界", "开放边界"), ("PCB叠层", "PCB叠层"),
                                 ("电气模型", "电气模型"), ("电容本体", "电容本体")):
            button = ttk.Checkbutton(
                quick_categories, text=label, variable=self.quick_vars[category],
                command=lambda value=category: self._quick_toggle(value),
            )
            button.pack(side="left", padx=3)
            self.quick_buttons[category] = button
        self.only_simulation_button = ttk.Checkbutton(
            quick_options, text="仅显示仿真对象", variable=self.only_simulation,
            command=self._only_simulation_changed,
        )
        self.only_simulation_button.pack(side="left", padx=(3, 3))
        ttk.Label(quick_options, text="空气盒：").pack(side="left", padx=(10, 1))
        self.air_mode_combo = ttk.Combobox(
            quick_options, textvariable=self.airbox_mode, state="readonly", width=11,
            values=("隐藏", "仅线框", "表面透明", "完整透明体", "仅开放边界"),
        )
        self.air_mode_combo.pack(side="left")
        self.air_mode_combo.bind("<<ComboboxSelected>>", lambda _e: self._airbox_mode_changed())
        ttk.Button(quick_options, text="恢复默认显示", command=self.restore_visibility).pack(side="right", padx=3)

        panes = ttk.PanedWindow(self, orient="horizontal")
        self.panes = panes
        self._pane_layout_initialized = False
        panes.bind("<Map>", self._initialize_pane_layout, add="+")
        panes.bind("<Configure>", self._schedule_pane_constraints, add="+")
        panes.pack(fill="both", expand=True)
        left, center, right = ttk.Frame(panes, padding=4), ttk.Frame(panes, padding=4), ttk.Frame(panes, padding=4)
        panes.add(left, weight=1); panes.add(center, weight=4); panes.add(right, weight=2)

        tree_box = ttk.LabelFrame(left, text="模型结构树", padding=5)
        tree_box.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_box, show="tree", selectmode="browse")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._tree_selected)
        self.tree.bind("<Double-1>", lambda _e: self.goto_parameter())
        self.tree.bind("<Button-3>", self._show_context_menu)
        controls = ttk.Frame(tree_box); controls.pack(fill="x", pady=(5, 0))
        for text, command in (("显/隐", self.toggle_visibility), ("独显", self.isolate_selected),
                              ("当前层", self.show_selected_layer), ("锁定", self.toggle_lock),
                              ("全部显示", self.restore_visibility)):
            ttk.Button(controls, text=text, command=command).pack(side="left", padx=1)
        ttk.Label(tree_box, text="透明度").pack(anchor="w", pady=(5, 0))
        self.opacity_scale = ttk.Scale(tree_box, from_=0.05, to=1.0, value=1.0,
                                       command=self._opacity_changed)
        self.opacity_scale.pack(fill="x")

        view_controls = ttk.Frame(center)
        view_controls.pack(fill="x")
        ttk.Checkbutton(view_controls, text="爆炸视图", variable=self.exploded,
                        command=self._apply_transforms).pack(side="left")
        ttk.Label(view_controls, text="距离").pack(side="left", padx=(4, 0))
        ttk.Scale(view_controls, from_=0.02, to=.8, variable=self.explosion,
                  command=lambda _v: self._apply_transforms()).pack(side="left", fill="x", expand=True)
        ttk.Checkbutton(view_controls, text="X-Ray", variable=self.xray,
                        command=self._apply_appearance).pack(side="left", padx=4)
        ttk.Checkbutton(view_controls, text="隐藏介质", variable=self.hide_dielectric,
                        command=self._apply_appearance).pack(side="left")
        ttk.Label(view_controls, text="剖切").pack(side="left", padx=(7, 1))
        slice_combo = ttk.Combobox(view_controls, textvariable=self.slice_axis,
                                   values=("无", "X", "Y", "Z"), width=3, state="readonly")
        slice_combo.pack(side="left"); slice_combo.bind("<<ComboboxSelected>>", lambda _e: self._update_clip())
        ttk.Scale(view_controls, from_=0, to=100, variable=self.slice_position,
                  command=lambda _v: self._update_clip()).pack(side="left", fill="x", expand=True, padx=(3, 0))

        self.zoom_notice = ttk.Label(center, text="", foreground="#b45309",
                                     font=("TkDefaultFont", 9, "bold"))
        self.zoom_notice.pack(anchor="center")
        vtk_error = VTK_IMPORT_ERROR
        if VTK_AVAILABLE and NATIVE_VTK_EMBED_ENABLED:
            try:
                self.vtk_widget = VTKTkRenderFrame(center, width=720, height=560)
                self.vtk_widget.pack(fill="both", expand=True)
                self.renderer = vtk.vtkRenderer()
                self.renderer.SetBackground(0.965, 0.975, 0.985)
                self.render_window = self.vtk_widget.GetRenderWindow()
                self.render_window.AddRenderer(self.renderer)
                self.interactor = self.vtk_widget.interactor
                self.interactor.SetInteractorStyle(vtk.vtkInteractorStyleTrackballCamera())
                self.picker = vtk.vtkCellPicker(); self.picker.SetTolerance(.004)
                self.picker.PickFromListOn()
                self.interactor.AddObserver("LeftButtonPressEvent", self._left_click, 1.0)
                self.interactor.AddObserver("MouseMoveEvent", self._mouse_move, 1.0)
                axes = vtk.vtkAxesActor(); axes.AxisLabelsOff()
                marker = vtk.vtkOrientationMarkerWidget()
                marker.SetOrientationMarker(axes); marker.SetInteractor(self.interactor)
                marker.SetViewport(0.0, 0.0, .16, .16); marker.SetEnabled(1); marker.InteractiveOff()
                self.orientation_marker = marker
                self.vtk_ready = True
            except Exception as exc:
                vtk_error = str(exc)
                self.vtk_ready = False
        if not self.vtk_ready:
            ttk.Label(
                center,
                text="软件3D预览（兼容降级，非 VTK 等价渲染） · 左键旋转 · 中键平移 · 滚轮缩放",
                foreground="#2457a7", font=("TkDefaultFont", 9, "bold"),
            ).pack(fill="x", pady=(2, 3))
            self.software_canvas = tk.Canvas(
                center, background="#f6f8fb", highlightthickness=1,
                highlightbackground="#cbd5e1", width=720, height=560,
            )
            self.software_canvas.pack(fill="both", expand=True)
            self.software_canvas.bind("<Configure>", self._software_canvas_configure, add="+")
            self.software_canvas.bind("<ButtonPress-1>", self._software_left_press, add="+")
            self.software_canvas.bind("<B1-Motion>", self._software_left_drag, add="+")
            self.software_canvas.bind("<ButtonRelease-1>", self._software_left_release, add="+")
            self.software_canvas.bind("<Double-Button-1>", self._software_double_click, add="+")
            self.software_canvas.bind("<ButtonPress-2>", self._software_middle_press, add="+")
            self.software_canvas.bind("<B2-Motion>", self._software_middle_drag, add="+")
            self.software_canvas.bind("<ButtonRelease-2>", self._software_pointer_release, add="+")
            self.software_canvas.bind("<MouseWheel>", self._software_mouse_wheel, add="+")
            self.software_canvas.bind("<Button-3>", self._software_context_menu, add="+")
            self.software_canvas.bind("<Motion>", self._software_canvas_motion, add="+")
            self.software_ready = True
        ttk.Label(center, textvariable=self.hover_var, foreground="#475569").pack(fill="x")
        ttk.Label(center, textvariable=self.status_var).pack(fill="x")
        ttk.Label(center, textvariable=self.stale_var, foreground="#b45309",
                  font=("TkDefaultFont", 9, "bold")).pack(fill="x")

        summary_box = ttk.LabelFrame(right, text="仿真模型状态", padding=6)
        summary_box.pack(fill="x")
        ttk.Label(summary_box, textvariable=self.summary_var, justify="left", wraplength=340).pack(fill="x")
        summary_actions = (ttk.Frame(summary_box), ttk.Frame(summary_box))
        for row in summary_actions:
            row.pack(fill="x", pady=(4, 0))
        ttk.Button(summary_actions[0], text="端口", command=lambda: self.navigate("simulation.create_differential_ports")).pack(side="left")
        ttk.Button(summary_actions[0], text="空气盒", command=lambda: self.navigate("simulation.airbox_margin")).pack(side="left", padx=2)
        ttk.Button(summary_actions[0], text="Setup", command=lambda: self.navigate("simulation.setup_name")).pack(side="left", padx=2)
        ttk.Button(summary_actions[1], text="Sweep", command=lambda: self.navigate("simulation.sweep_name")).pack(side="left")
        ttk.Button(summary_actions[1], text="运行 Validate", command=self._run_validate).pack(side="left", padx=2)
        ttk.Button(summary_box, text="前往仿真与扫描", command=lambda: self.navigate("simulation.enabled")).pack(fill="x", pady=(4, 0))

        detail_box = ttk.LabelFrame(right, text="选中对象", padding=6)
        detail_box.pack(fill="x")
        ttk.Label(detail_box, textvariable=self.detail_var, justify="left", wraplength=320).pack(fill="x")
        actions = (ttk.Frame(detail_box), ttk.Frame(detail_box))
        for row in actions:
            row.pack(fill="x", pady=(4, 0))
        ttk.Button(actions[0], text="前往参数", command=self.goto_parameter).pack(side="left")
        ttk.Button(actions[0], text="聚焦", command=self.focus_selected).pack(side="left", padx=4)
        ttk.Button(actions[1], text="单独显示", command=self.isolate_selected).pack(side="left")
        ttk.Button(actions[1], text="清除选择", command=self.clear_selection).pack(side="left", padx=4)
        conductor_actions = ttk.Frame(detail_box); conductor_actions.pack(fill="x", pady=(4, 0))
        ttk.Button(conductor_actions, text="定位信号导体", command=self.locate_signal_conductor).pack(side="left")
        ttk.Button(conductor_actions, text="定位参考导体", command=self.locate_reference_conductor).pack(side="left", padx=4)
        port_actions = (ttk.Frame(detail_box), ttk.Frame(detail_box))
        for row in port_actions:
            row.pack(fill="x", pady=(4, 0))
        ttk.Button(port_actions[0], text="正视 Port", command=self.port_front_view).pack(side="left")
        ttk.Button(port_actions[0], text="聚焦 Port", command=self.focus_port).pack(side="left", padx=4)
        ttk.Button(port_actions[1], text="仅看端口关系", command=self.show_port_relationship).pack(side="left")
        ttk.Button(port_actions[1], text="查看 Validate", command=self._show_validate_results).pack(side="left", padx=4)

        measure_box = ttk.LabelFrame(right, text="两点测距", padding=6)
        measure_box.pack(fill="x", pady=5)
        ttk.Checkbutton(measure_box, text="启用测距", variable=self.measure_mode,
                        command=self._measure_mode_changed).pack(anchor="w")
        ttk.Label(measure_box, textvariable=self.measure_var, justify="left", wraplength=320).pack(fill="x")
        ttk.Button(measure_box, text="清除测量", command=self.clear_measurement).pack(anchor="w", pady=(4, 0))

        issues_box = ttk.LabelFrame(right, text="检查结果", padding=5)
        issues_box.pack(fill="both", expand=True, pady=5)
        self.issue_tree = ttk.Treeview(issues_box, columns=("level", "message"), show="headings", height=8)
        self.issue_tree.heading("level", text="级别"); self.issue_tree.heading("message", text="原因")
        self.issue_tree.column("level", width=45, anchor="center"); self.issue_tree.column("message", width=245)
        self.issue_tree.pack(fill="both", expand=True)
        self.issue_tree.bind("<<TreeviewSelect>>", self._issue_selected)
        ttk.Button(issues_box, text="定位问题并前往参数", command=self._goto_issue).pack(fill="x", pady=(4, 0))

        legend = ttk.LabelFrame(right, text="图例", padding=5)
        legend.pack(fill="x")
        legend_text = ("P信号—红  N信号—蓝  铜/焊盘—铜金\n"
                       "介质—浅蓝透明  参考平面—灰绿透明\n"
                       "反焊盘/避空—橙色轮廓  VIA桶—铜色\n"
                       "Port—紫  空气盒—浅蓝线框  开放边界—青\n"
                       "◇=本地预估（不代表HFSS正确）  ●=HFSS实际验证  橙=警告  红=失败")
        ttk.Label(legend, text=legend_text, justify="left", wraplength=320).pack(fill="x")

    def _initialize_pane_layout(self, _event: Any = None) -> None:
        """Give the software viewport a readable initial share without overriding user drags."""
        if self._pane_layout_initialized:
            return
        width = int(self.panes.winfo_width())
        if width < 700:
            self.app.after_idle(self._initialize_pane_layout)
            return
        first = max(155, int(width * .17))
        second = max(first + 360, min(width - 270, int(width * .73)))
        try:
            self.panes.sashpos(0, first)
            self.panes.sashpos(1, second)
        except tk.TclError:
            return
        self._pane_layout_initialized = True

    def _schedule_pane_constraints(self, _event: Any = None) -> None:
        job = getattr(self, "_pane_constraint_job", None)
        if job:
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
        self._pane_constraint_job = self.app.after_idle(self._enforce_pane_constraints)

    def _enforce_pane_constraints(self) -> None:
        self._pane_constraint_job = None
        width = int(self.panes.winfo_width())
        if width < 700:
            return
        try:
            first, second = self.panes.sashpos(0), self.panes.sashpos(1)
        except tk.TclError:
            return
        minimum_left = max(155, int(width * .15))
        minimum_center = max(360, int(width * .44))
        minimum_right = max(245, int(width * .21))
        target_first = max(first, minimum_left)
        target_second = min(second, width - minimum_right)
        if target_second - target_first < minimum_center:
            target_first = minimum_left
            target_second = min(width - minimum_right, target_first + minimum_center)
        if abs(target_first-first) > 2:
            self.panes.sashpos(0, target_first)
        if abs(target_second-second) > 2:
            self.panes.sashpos(1, target_second)

    @staticmethod
    def _software_hex(color_key: str) -> str:
        color = COLORS.get(color_key, COLORS["disabled"])
        return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, round(value * 255))) for value in color))

    def _software_canvas_configure(self, _event: Any = None) -> None:
        if self._canvas_job:
            try:
                self.app.after_cancel(self._canvas_job)
            except tk.TclError:
                pass
        self._canvas_job = self.app.after(40, self._software_redraw)

    def _software_canvas_object_key(self) -> str:
        current = self.software_canvas.find_withtag("current")
        if not current:
            return ""
        for tag in self.software_canvas.gettags(current[-1]):
            key = self._canvas_tag_keys.get(tag)
            if key:
                return key
        return ""

    def _software_left_press(self, event: tk.Event) -> str:
        self.software_canvas.focus_set()
        self._pointer_mode = "orbit"
        self._pointer_start = self._pointer_last = (int(event.x), int(event.y))
        self._pointer_dragged = False
        return "break"

    def _software_left_drag(self, event: tk.Event) -> str:
        x, y = int(event.x), int(event.y)
        dx, dy = x - self._pointer_last[0], y - self._pointer_last[1]
        if math.hypot(x - self._pointer_start[0], y - self._pointer_start[1]) >= 3.0:
            self._pointer_dragged = True
        if self._pointer_dragged:
            self._camera_yaw += dx * .008
            self._camera_pitch = max(math.radians(-89.5), min(math.radians(89.5),
                                                               self._camera_pitch - dy * .008))
            self.software_view = "自由轨道"
            self._software_redraw()
        self._pointer_last = (x, y)
        return "break"

    def _software_left_release(self, _event: tk.Event) -> str:
        if not self._pointer_dragged:
            key = self._software_canvas_object_key()
            if key:
                self.select_object(key)
            else:
                self.clear_selection()
        self._software_pointer_release()
        return "break"

    def _software_double_click(self, _event: tk.Event) -> str:
        key = self._software_canvas_object_key()
        if key:
            self.select_object(key)
            self._fit_keys((key,))
        return "break"

    def _software_middle_press(self, event: tk.Event) -> str:
        self.software_canvas.focus_set()
        self._pointer_mode = "pan"
        self._pointer_start = self._pointer_last = (int(event.x), int(event.y))
        self._pointer_dragged = False
        return "break"

    def _software_middle_drag(self, event: tk.Event) -> str:
        x, y = int(event.x), int(event.y)
        dx, dy = x - self._pointer_last[0], y - self._pointer_last[1]
        if dx or dy:
            right, up, _forward, _out = self._camera_basis()
            units = 1.0 / max(self._camera_scale, 1e-9)
            for index in range(3):
                self._camera_target[index] -= right[index] * dx * units
                self._camera_target[index] += up[index] * dy * units
            self._pointer_dragged = True
            self._software_redraw()
        self._pointer_last = (x, y)
        return "break"

    def _software_pointer_release(self, _event: Any = None) -> str:
        self._pointer_mode = ""
        self._pointer_dragged = False
        return "break"

    def _software_mouse_wheel(self, event: tk.Event) -> str:
        old_scale = max(self._camera_scale, 1e-9)
        factor = 1.12 if int(event.delta) > 0 else 1.0 / 1.12
        new_scale = max(4.0, min(5000.0, old_scale * factor))
        viewport = canvas_viewport(self.software_canvas)
        if viewport is None:
            return "break"
        width, height = viewport
        offset_x, offset_y = float(event.x) - width / 2.0, height / 2.0 - float(event.y)
        right, up, _forward, _out = self._camera_basis()
        shift_x = offset_x / old_scale - offset_x / new_scale
        shift_y = offset_y / old_scale - offset_y / new_scale
        for index in range(3):
            self._camera_target[index] += right[index] * shift_x + up[index] * shift_y
        self._camera_scale = new_scale
        self._camera_distance = max(.001, self._camera_distance / factor)
        self._software_redraw()
        return "break"

    def _software_context_menu(self, event: tk.Event) -> str:
        key = self._software_canvas_object_key()
        if key:
            self.select_object(key)
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="显示 / 隐藏", command=self.toggle_visibility)
        menu.add_command(label="单独显示", command=self.isolate_selected)
        menu.add_command(label="聚焦", command=self.focus_selected)
        menu.add_separator()
        menu.add_command(label="前往参数", command=self.goto_parameter)
        menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def _software_canvas_motion(self, _event: Any = None) -> None:
        if not self.software_ready:
            return
        current = self.software_canvas.find_withtag("current")
        key = ""
        if current:
            key = next((self._canvas_tag_keys.get(tag, "")
                        for tag in self.software_canvas.gettags(current[-1])
                        if tag in self._canvas_tag_keys), "")
        item = self.objects.get(key)
        if item:
            self.hover_var.set(f"{item.name} · {item.category} · {item.status}")
        else:
            self.hover_var.set("点击对象可查看名称、参数来源和 Validate 状态")

    @staticmethod
    def _normalize_vector(vector: Sequence[float]) -> Tuple[float, float, float]:
        length = math.sqrt(sum(float(value) ** 2 for value in vector)) or 1.0
        return tuple(float(value) / length for value in vector)  # type: ignore[return-value]

    @staticmethod
    def _cross(first: Sequence[float], second: Sequence[float]) -> Tuple[float, float, float]:
        return (first[1] * second[2] - first[2] * second[1],
                first[2] * second[0] - first[0] * second[2],
                first[0] * second[1] - first[1] * second[0])

    @staticmethod
    def _dot(first: Sequence[float], second: Sequence[float]) -> float:
        return sum(float(a) * float(b) for a, b in zip(first, second))

    def _camera_basis(self) -> Tuple[Tuple[float, float, float], ...]:
        cp = math.cos(self._camera_pitch)
        outward = self._normalize_vector((cp * math.cos(self._camera_yaw),
                                          cp * math.sin(self._camera_yaw),
                                          math.sin(self._camera_pitch)))
        forward = tuple(-value for value in outward)
        if abs(forward[2]) > .98:
            up_hint = (0.0, 1.0 if forward[2] < 0 else -1.0, 0.0)
        else:
            up_hint = (0.0, 0.0, 1.0)
        right = self._normalize_vector(self._cross(forward, up_hint))
        up = self._normalize_vector(self._cross(right, forward))
        return right, up, forward, outward

    def _software_visual_point(self, point: Sequence[float], item: Optional[SceneObject] = None) -> Tuple[float, float, float]:
        offset = self._visual_layer_offset(item) if item is not None else 0.0
        return float(point[0]), float(point[1]), self._map_visual_z(float(point[2])) + offset

    def _software_raw_projection(self, point: Sequence[float], item: Optional[SceneObject] = None) -> Tuple[float, float]:
        visual = self._software_visual_point(point, item)
        relative = tuple(visual[index] - self._camera_target[index] for index in range(3))
        right, up, _forward, _outward = self._camera_basis()
        return self._dot(relative, right), self._dot(relative, up)

    def _software_depth(self, item: SceneObject) -> float:
        points = self._software_world_points(item)
        if not points:
            return 0.0
        visual = [self._software_visual_point(point, item) for point in points]
        center = tuple(sum(point[index] for point in visual) / len(visual) for index in range(3))
        _right, _up, _forward, outward = self._camera_basis()
        return self._dot(tuple(center[index] - self._camera_target[index] for index in range(3)), outward)

    @staticmethod
    def _software_world_points(item: SceneObject) -> List[Tuple[float, float, float]]:
        z0, z1 = item.z_bottom, item.z_top
        if item.bounds_xy:
            x0, x1, y0, y1 = item.bounds_xy
            return [(x, y, z) for z in (z0, z1) for x, y in
                    ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
        if item.points_xy:
            return [(x, y, z) for z in (z0, z1) for x, y in item.points_xy]
        if item.center_xy and item.outer_radius:
            cx, cy = item.center_xy; radius = item.outer_radius
            return [(cx + math.cos(index * math.tau / 24) * radius,
                     cy + math.sin(index * math.tau / 24) * radius, z)
                    for z in (z0, z1) for index in range(24)]
        return []

    def _software_redraw(self) -> None:
        self._canvas_job = None
        if not self.software_ready or not self.software_canvas.winfo_exists():
            return
        canvas = self.software_canvas
        viewport = canvas_viewport(canvas)
        if viewport is None:
            return
        canvas.delete("all"); self._canvas_tag_keys.clear()
        self._pending_port_labels: List[Dict[str, Any]] = []
        self._pending_screen_labels: List[Dict[str, Any]] = []
        width, height = viewport
        if not self.scene or not self.objects:
            canvas.create_text(width / 2, height / 2, text="等待有效模型数据",
                               fill="#64748b", font=("TkDefaultFont", 12, "bold"),
                               width=max(1, width-24), tags=("hud-label",))
            return
        visible = [item for item in self.objects.values()
                   if self.visibility.get(item.key, item.default_visible)
                   and not (self.hide_dielectric.get() and item.color_key in {"dielectric", "soldermask"})]
        if not self._camera_initialized:
            self._software_fit_objects(
                [item for item in visible if item.category not in SIMULATION_CATEGORIES],
                width=width, height=height,
            )

        def screen(point: Sequence[float], item: Optional[SceneObject] = None) -> Tuple[float, float]:
            u, v = self._software_raw_projection(point, item)
            return width / 2.0 + u * self._camera_scale, height / 2.0 - v * self._camera_scale

        # Airbox/boundary are line-only backgrounds.  All solid geometry uses
        # a camera-depth painter order so translucent layers cannot randomly
        # cover nearer conductors when the camera moves.
        draw_order = sorted(
            visible,
            key=lambda item: (
                0 if item.category in {"空气盒", "开放边界"} else 1,
                self._software_depth(item),
                item.key,
            ),
        )
        tag_by_key: Dict[str, str] = {}
        for index, item in enumerate(draw_order):
            tag = f"scene_object_{index}"; self._canvas_tag_keys[tag] = item.key
            tag_by_key[item.key] = tag
            self._software_draw_object(
                item, tag,
                lambda point, current=item: screen(point, current),
                self._camera_scale,
            )
        simulation_items = {item.category: item for item in visible
                            if item.category in {"空气盒", "开放边界"}}
        if simulation_items:
            anchor_item = simulation_items.get("空气盒") or simulation_items["开放边界"]
            if anchor_item.bounds_xy:
                x0, _x1, _y0, y1 = anchor_item.bounds_xy
                leader = screen((x0, y1, anchor_item.z_top), anchor_item)
                boundary_type = str(self.current_config.get("simulation", {}).get("open_boundary", "Radiation")).upper()
                if {"空气盒", "开放边界"} <= set(simulation_items):
                    label = f"空气盒 AIRBOX · 开放边界 {boundary_type}（表面）"
                elif "空气盒" in simulation_items:
                    label = "空气盒 AIRBOX"
                else:
                    label = f"开放边界 {boundary_type}（表面）"
                tag = tag_by_key.get(anchor_item.key, "")
                self._pending_screen_labels.append({
                    "text": label, "desired": leader, "leader": leader,
                    "color": "#0369a1", "background": "#e0f2fe",
                    "tag": tag, "role": "object-label", "priority": 1,
                })
        label_layout = CanvasLabelPlacer(canvas, width, height, safe=8)
        port_count = sum(item.category == "Port" for item in visible)
        badges = []
        if port_count:
            badges.append((f"PORT ×{port_count}", "#7e22ce"))
        if any(item.category == "空气盒" for item in visible):
            badges.append(("AIRBOX", "#0284c7"))
        if any(item.category == "开放边界" for item in visible):
            boundary_type = str(self.current_config.get("simulation", {}).get("open_boundary", "Radiation")).upper()
            badges.append((boundary_type, "#0e7490" if boundary_type == "RADIATION" else "#be185d"))
        badge_font = ("TkDefaultFont", 9, "bold")
        badge_line = font_linespace(canvas, badge_font)
        metric = resolve_font(canvas, badge_font)
        compact_badges = width < 800 and len(badges) > 1
        visible_badges = (
            [(" · ".join(label for label, _color in badges), "#475569")]
            if compact_badges else badges
        )
        badge_occupied_width = 0
        for index, (label, badge_color) in enumerate(visible_badges):
            half = metric.measure(label) / 2 + 6
            badge_occupied_width = max(badge_occupied_width, int(half * 2))
            label_layout.place(
                label,
                ((width-half-10, 10+badge_line/2 + index*(badge_line+9)),
                 (width-half-10, 10+badge_line/2 + (index+1)*(badge_line+9))),
                font=badge_font, fill="#ffffff", background=badge_color,
                tags=("hud-label", "status_badge"), priority=2, padding=(6, 3),
            )
        title = f"{self.software_view}  ·  软件3D预览  ·  {self._display_summary()}"
        title_limit = max(72, width - badge_occupied_width - 34) if compact_badges else max(1, width-24)
        title = ellipsize(canvas, title, ("TkDefaultFont", 9, "bold"), title_limit)
        title_half = resolve_font(canvas, ("TkDefaultFont", 9, "bold")).measure(title) / 2
        label_layout.place(
            title,
            ((12+title_half, 10+badge_line/2),
             (12+title_half, 10+badge_line*1.7),
             (width/2, 10+badge_line*2.8)),
            font=("TkDefaultFont", 9, "bold"), fill="#334155", background="#f6f8fb",
            tags=("hud-label", "view-title"), priority=2, padding=(3, 2),
        )
        if self.diagnostics_enabled.get():
            label_layout.reserve(self._software_draw_diagnostics(width, height))
        self._software_draw_port_labels(width, height, label_layout)

    def _software_fit_objects(self, items: Sequence[SceneObject], *,
                              width: Optional[int] = None, height: Optional[int] = None) -> None:
        if not items:
            return
        points = [self._software_visual_point(point, item)
                  for item in items for point in self._software_world_points(item)]
        if not points:
            return
        low = [min(point[index] for point in points) for index in range(3)]
        high = [max(point[index] for point in points) for index in range(3)]
        self._camera_target = [(low[index] + high[index]) / 2.0 for index in range(3)]
        projected = []
        for point in points:
            relative = tuple(point[index] - self._camera_target[index] for index in range(3))
            right, up, _forward, _outward = self._camera_basis()
            projected.append((self._dot(relative, right), self._dot(relative, up)))
        span_u = max(max(point[0] for point in projected) - min(point[0] for point in projected), 1e-6)
        span_v = max(max(point[1] for point in projected) - min(point[1] for point in projected), 1e-6)
        if width is None or height is None:
            viewport = canvas_viewport(self.software_canvas)
            if viewport is None:
                return
            actual_width, actual_height = viewport
        else:
            actual_width, actual_height = int(width), int(height)
        canvas_width = int(width) if width is not None else actual_width
        canvas_height = int(height) if height is not None else actual_height
        margin = 48.0
        self._camera_scale = max(
            4.0,
            min((canvas_width - margin * 2.0) / span_u,
                (canvas_height - margin * 2.0) / span_v),
        )
        diagonal = math.sqrt(sum((high[index] - low[index]) ** 2 for index in range(3)))
        self._camera_distance = max(diagonal * 2.5, 1.0)
        self._camera_initialized = True
        self._last_visual_z_scale = self._z_scale()
        self._last_visual_mode = self.z_display_mode.get()

    @staticmethod
    def _true_bounds_for_objects(items: Sequence[SceneObject]) -> Optional[Tuple[float, float, float, float, float, float]]:
        points = [point for item in items for point in ModelPreviewWorkbench._software_world_points(item)]
        if not points:
            return None
        return (
            min(point[0] for point in points), max(point[0] for point in points),
            min(point[1] for point in points), max(point[1] for point in points),
            min(point[2] for point in points), max(point[2] for point in points),
        )

    @staticmethod
    def _bounds_changed_materially(previous: Optional[Tuple[float, ...]],
                                   current: Optional[Tuple[float, ...]]) -> bool:
        if previous is None or current is None:
            return previous != current
        previous_spans = [max(previous[index + 1] - previous[index], 1e-9) for index in (0, 2, 4)]
        current_spans = [max(current[index + 1] - current[index], 1e-9) for index in (0, 2, 4)]
        if any(max(old / new, new / old) > 1.35 for old, new in zip(previous_spans, current_spans)):
            return True
        previous_center = [(previous[index] + previous[index + 1]) / 2.0 for index in (0, 2, 4)]
        current_center = [(current[index] + current[index + 1]) / 2.0 for index in (0, 2, 4)]
        return any(abs(old - new) > span * .25
                   for old, new, span in zip(previous_center, current_center, previous_spans))

    def _software_draw_diagnostics(self, width: int, height: int) -> Optional[Tuple[int, int, int, int]]:
        right, up, _forward, outward = self._camera_basis()
        camera = tuple(self._camera_target[index] + outward[index] * self._camera_distance
                       for index in range(3))
        z_scale = self._z_scale()
        focal_true = (self._camera_target[0], self._camera_target[1],
                      self._unmap_visual_z(self._camera_target[2]))
        model_items = [item for item in self.objects.values()
                       if item.category not in {"空气盒", "开放边界", "Port"}]
        bounds = self._true_bounds_for_objects(model_items)
        selected = self.objects.get(self.selected_key)
        selected_bounds = self._true_bounds_for_objects([selected]) if selected else None
        dpi_scale = float(self.app.tk.call("tk", "scaling"))
        lines = [
            "3D诊断（视觉坐标仅Z应用当前比例）",
            "相机位置：({:.4g}, {:.4g}, {:.4g})".format(*camera),
            "焦点/旋转中心：({:.4g}, {:.4g}, {:.4g}) （内部 mm）".format(*focal_true),
            f"显示策略：{self._display_summary()}（名义倍率 {z_scale:.4g}×）",
            f"模型边界：{bounds or '—'}",
            f"Actor/对象数：{len(self.objects)} / 画布项 {len(self.software_canvas.find_all())}",
            f"当前选中：{self.selected_key or '—'}",
            f"选中对象XYZ范围：{selected_bounds or '—'}",
            f"视口：{width}×{height} px · Tk DPI比例 {dpi_scale:.4g}",
            "相机Right：({:.3g}, {:.3g}, {:.3g}) · Up：({:.3g}, {:.3g}, {:.3g})".format(*right, *up),
        ]
        text_id = self.software_canvas.create_text(
            12, height - 12, anchor="sw", text="\n".join(lines), justify="left",
            fill="#e2e8f0", font=("TkFixedFont", 8), width=max(1, width-24),
            tags=("diagnostics", "hud-label"),
        )
        bbox = self.software_canvas.bbox(text_id)
        if bbox:
            background = self.software_canvas.create_rectangle(
                bbox[0]-6, bbox[1]-5, bbox[2]+6, bbox[3]+5,
                fill="#0f172a", outline="#475569", width=1,
                tags=("diagnostics", "hud-label"),
            )
            self.software_canvas.tag_lower(background, text_id)
            return (bbox[0]-6, bbox[1]-5, bbox[2]+6, bbox[3]+5)
        return None

    @staticmethod
    def _software_plane_polygons(item: SceneObject) -> List[List[Tuple[float, float]]]:
        """Return exact visible plane regions after subtracting shared openings."""
        if not item.points_xy or not SHAPELY_AVAILABLE:
            return [list(item.points_xy)]
        region = ShapelyPolygon(item.points_xy)
        cutters = [ShapelyPolygon(points) for points in item.holes_xy if len(points) >= 3]
        cutters.extend(ShapelyPoint(x, y).buffer(radius, quad_segs=24)
                       for x, y, radius in item.circular_holes if radius > 0)
        if cutters:
            region = region.difference(shapely_union(cutters))
        polygons: List[List[Tuple[float, float]]] = []
        for triangle in shapely_triangulate(region):
            clipped = triangle.intersection(region)
            geometries = list(getattr(clipped, "geoms", (clipped,)))
            for geometry in geometries:
                if getattr(geometry, "area", 0.0) <= 1e-12 or not hasattr(geometry, "exterior"):
                    continue
                polygons.append([(float(x), float(y)) for x, y in list(geometry.exterior.coords)[:-1]])
        return polygons or [list(item.points_xy)]

    @staticmethod
    def _projected_quad_is_valid(points: Sequence[Tuple[float, float]]) -> bool:
        if len(points) != 4:
            return False
        unique = {(round(x, 5), round(y, 5)) for x, y in points}
        if len(unique) != 4:
            return False
        area = abs(sum(points[index][0] * points[(index + 1) % 4][1]
                       - points[(index + 1) % 4][0] * points[index][1]
                       for index in range(4))) / 2.0
        if area < 4.0:
            return False
        def orientation(a: Tuple[float, float], b: Tuple[float, float], c: Tuple[float, float]) -> float:
            return (b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])
        return not (orientation(points[0], points[1], points[2]) * orientation(points[0], points[1], points[3]) < 0
                    and orientation(points[2], points[3], points[0]) * orientation(points[2], points[3], points[1]) < 0)

    def _software_draw_port_labels(self, width: int, height: int,
                                   layout: Optional[CanvasLabelPlacer] = None) -> None:
        """Lay out HUD, object and Port labels in one occupied screen space."""
        placer = layout or CanvasLabelPlacer(self.software_canvas, width, height, safe=8)
        ports = sorted(getattr(self, "_pending_port_labels", ()),
                       key=lambda entry: (entry["side"], entry["signal"]))
        ordinary = list(getattr(self, "_pending_screen_labels", ()))
        entries = [*ports, *ordinary]
        entries.sort(key=lambda entry: (-int(entry.get("priority", 2)),
                                        str(entry.get("side", "")), str(entry.get("signal", ""))))
        for entry in entries:
            x, y = entry["desired"]
            role = str(entry.get("role", "port-label"))
            if role == "port-label":
                side_sign = -1 if entry["side"] == "IN" else 1
                signal_sign = -1 if entry["signal"] == "P" else 1
                quadrant_x = width * (.24 if entry["side"] == "IN" else .76)
                quadrant_y = height * (.30 if entry["signal"] == "P" else .70)
                font = ("TkDefaultFont", 8, "bold")
                if width < 700:
                    text = (f"{entry['side']}-{entry['signal']} · "
                            f"{entry.get('direction', '')} · S→{entry.get('reference', '')}")
                    quadrant_y = height * (.44 if entry["signal"] == "P" else .76)
                    candidates = (
                        (quadrant_x, quadrant_y),
                        (quadrant_x, height * (.48 if entry["signal"] == "P" else .82)),
                        (x + side_sign * 64, y + signal_sign * 28),
                    )
                    label_padding = (4, 0)
                else:
                    text = entry["text"]
                    candidates = (
                        (x + side_sign * 72, y + signal_sign * 24),
                        (quadrant_x, quadrant_y),
                        (x + side_sign * 100, y + signal_sign * 52),
                        (quadrant_x, height * (.42 if entry["signal"] == "P" else .82)),
                        (x - side_sign * 85, y + signal_sign * 72),
                    )
                    label_padding = (4, 2)
                foreground = "#ffffff"
                background = entry["color"]
                outline = "#ffffff"
                tags = (entry["tag"], "port_label", "port-label", "screen-label")
            else:
                candidates = tuple(entry.get("candidates", ())) or (
                    (x + 20, y - 22), (x - 20, y - 22),
                    (x + 28, y + 24), (x - 28, y + 24),
                    (width/2, height*.86),
                )
                font = entry.get("font", ("TkDefaultFont", 8, "bold"))
                foreground = entry.get("foreground", entry.get("color", "#334155"))
                background = entry.get("background", "#f8fafc")
                outline = entry.get("outline", entry.get("color", "#94a3b8"))
                tags = tuple(tag for tag in (entry.get("tag", ""), role, "screen-label") if tag)
                text = ellipsize(self.software_canvas, entry["text"], font, max(1, width-24))
                label_padding = (4, 2)
            placer.place(
                text, candidates, font=font, fill=foreground,
                background=background, outline=outline, tags=tags,
                leader=entry.get("leader"), leader_color=entry.get("color"),
                priority=int(entry.get("priority", 2)), padding=label_padding,
            )
        self._hidden_screen_labels = tuple(placer.hidden)

    def _software_draw_object(self, item: SceneObject, tag: str,
                              screen: Callable[[Sequence[float]], Tuple[float, float]],
                              scale: float) -> None:
        canvas = self.software_canvas
        color = self._software_hex(item.color_key)
        selected = item.key == self.selected_key or item.key in self.validation_marker_actors
        effective_opacity = self.opacity_overrides.get(item.key, item.opacity)
        faded = effective_opacity < .10 and not selected
        if faded:
            color = "#dce4e8"
        outline = "#f59e0b" if selected else color
        width = 4 if selected else (3 if item.category in SIMULATION_CATEGORIES else 1)
        if item.details.get("结构") == "电容本体" and not selected:
            outline = "#786b50"
            width = 2
        tags = (tag, "model_object")

        def flat(points: Sequence[Sequence[float]]) -> List[float]:
            return [coordinate for point in points for coordinate in screen(point)]

        if item.category in {"空气盒", "开放边界"} and item.bounds_xy:
            x0, x1, y0, y1 = item.bounds_xy; z0, z1 = item.z_bottom, item.z_top
            corners = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
                       (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
            edge_color = "#38bdf8" if item.category == "空气盒" else "#0891b2"
            if self.selected_key and self.objects.get(self.selected_key, item).category not in SIMULATION_CATEGORIES:
                edge_color = "#bae6fd" if item.category == "空气盒" else "#a5f3fc"
                width = 1
            if item.category == "空气盒":
                for first, second in ((0,1),(1,2),(2,3),(3,0),(4,5),(5,6),(6,7),(7,4),(0,4),(1,5),(2,6),(3,7)):
                    canvas.create_line(*flat((corners[first], corners[second])), fill=edge_color,
                                       width=max(1, width), tags=tags)
            else:
                # The boundary is a surface assignment on AIRBOX, not a second
                # box.  Cyan corner brackets express the assignment without a
                # duplicate twelve-edge wireframe.
                projected = [screen(corner) for corner in corners]
                arm = 9 if not selected else 14
                for px, py in projected:
                    canvas.create_line(px-arm, py, px+arm, py, fill=edge_color,
                                       width=3 if selected else 2, tags=tags)
                    canvas.create_line(px, py-arm, px, py+arm, fill=edge_color,
                                       width=3 if selected else 2, tags=tags)
            return

        if item.category == "Port" and item.bounds_xy:
            x = item.bounds_xy[0]; y0, y1 = item.bounds_xy[2], item.bounds_xy[3]
            signal = str(item.details.get("P/N信号", ""))
            port_color = "#d946ef" if signal == "P" else "#7c3aed"
            dark_port_color = "#a21caf" if signal == "P" else "#5b21b6"
            face = [(x, y0, item.z_bottom), (x, y1, item.z_bottom),
                    (x, y1, item.z_top), (x, y0, item.z_top)]
            projected_face = [screen(point) for point in face]
            polygon = [coordinate for point in projected_face for coordinate in point]
            # In top view the vertical sheet collapses to a line; its heavy
            # outline plus the arrow/label below still makes the port explicit.
            if self._projected_quad_is_valid(projected_face):
                canvas.create_polygon(*polygon, fill=port_color, outline="#fef3c7" if not selected else outline,
                                      width=max(3, width), stipple="gray25", tags=tags)
            else:
                canvas.create_line(*polygon, *projected_face[0], fill=outline if selected else port_color,
                                   width=max(5, width), tags=tags)
            midpoint = ((y0 + y1) / 2.0)
            side = item.details.get("输入/输出", "输入")
            side_code = "IN" if side == "输入" else "OUT"
            direction = 1.0 if side == "输入" else -1.0
            center_x = sum(point[0] for point in projected_face) / 4.0
            center_y = sum(point[1] for point in projected_face) / 4.0
            arrow_sign = -1 if side == "输入" else 1
            canvas.create_line(center_x, center_y, center_x + arrow_sign * 34, center_y,
                               fill=dark_port_color, width=4, arrow=tk.LAST,
                               arrowshape=(11, 13, 5), tags=tags)
            direction_text = str(item.details.get("法向方向", "+X"))
            integration_start = item.details.get("integration_line_start", ())
            integration_end = item.details.get("integration_line_end", ())
            reference_layer = str(item.details.get("参考层", "Reference"))
            if len(integration_start) == 3 and len(integration_end) == 3:
                start_screen = screen(integration_start)
                end_screen = screen(integration_end)
                canvas.create_line(*start_screen, *end_screen, fill="#c026d3", width=3,
                                   dash=(6, 4), arrow=tk.LAST, arrowshape=(10, 12, 5), tags=tags)
                if selected:
                    self._pending_screen_labels.append({
                        "text": f"Reference: {reference_layer}",
                        "desired": (end_screen[0], end_screen[1]),
                        "leader": (end_screen[0], end_screen[1]),
                        "candidates": ((end_screen[0]+70, end_screen[1]+28),
                                       (end_screen[0]-70, end_screen[1]+28),
                                       (end_screen[0]+70, end_screen[1]-28)),
                        "color": "#6b21a8", "foreground": "#ffffff",
                        "background": "#6b21a8", "outline": "#ffffff",
                        "tag": tag, "role": "selection-label", "priority": 3,
                    })
            label = f"{item.name} · {signal} · {direction_text} · S→{reference_layer}"
            leader = screen((x, y1 if signal == "P" else y0, item.z_top))
            self._pending_port_labels.append({
                "text": label, "side": side_code, "signal": signal, "leader": leader,
                "desired": leader, "color": dark_port_color, "tag": tag,
                "role": "port-label", "priority": 2, "name": item.name,
                "direction": direction_text, "reference": reference_layer,
            })
            return

        if item.category == "电气模型":
            line_start = item.details.get("integration_line_start", ())
            line_end = item.details.get("integration_line_end", ())
            if len(line_start) == 3 and len(line_end) == 3:
                start_screen, end_screen = screen(line_start), screen(line_end)
                canvas.create_line(*start_screen, *end_screen, fill="#facc15", width=4,
                                   dash=(6, 3), arrow=tk.LAST,
                                   arrowshape=(12, 14, 6), tags=tags)
                midpoint = ((start_screen[0]+end_screen[0])/2,
                            (start_screen[1]+end_screen[1])/2)
                self._pending_screen_labels.append({
                    "text": "积分线 IN → OUT", "desired": midpoint, "leader": midpoint,
                    "color": "#a16207", "foreground": "#713f12", "background": "#fef9c3",
                    "tag": tag, "role": "object-label", "priority": 1,
                })

        points_xy = list(item.points_xy)
        if not points_xy and item.center_xy and item.outer_radius:
            cx, cy = item.center_xy
            points_xy = [(cx + math.cos(index * math.tau / 24) * item.outer_radius,
                          cy + math.sin(index * math.tau / 24) * item.outer_radius)
                         for index in range(24)]
        if not points_xy and item.bounds_xy:
            x0, x1, y0, y1 = item.bounds_xy
            points_xy = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        if not points_xy:
            return
        top = [(x, y, item.z_top) for x, y in points_xy]
        bottom = [(x, y, item.z_bottom) for x, y in points_xy]
        outline_only = item.object_type in {"outline", "circle_outline"}
        fill_color = "" if outline_only else color
        stipple = ""
        if faded:
            stipple = "gray12"
        elif item.color_key in {"dielectric", "soldermask", "plane"} or self.xray.get():
            stipple = "gray50"
        elif item.details.get("结构") == "电容本体":
            stipple = "gray75"
        closed_solid = (
            item.category == "PCB叠层"
            or item.details.get("结构") == "电容本体"
            or item.object_type == "tube"
        )
        if closed_solid and item.z_top - item.z_bottom > 1e-7 and not outline_only:
            # Solid side faces are filled polygons.  They intentionally have no
            # per-vertex outline, so a cylinder or triangulated layer cannot
            # leak a forest of software-renderer vertical lines.
            for index, first in enumerate(points_xy):
                second = points_xy[(index + 1) % len(points_xy)]
                side_face = ((first[0], first[1], item.z_bottom),
                             (second[0], second[1], item.z_bottom),
                             (second[0], second[1], item.z_top),
                             (first[0], first[1], item.z_top))
                canvas.create_polygon(*flat(side_face), fill=color, outline="",
                                      stipple=stipple, tags=tags)
        if item.object_type == "plane_with_holes":
            for polygon in self._software_plane_polygons(item):
                face = [(x, y, item.z_top) for x, y in polygon]
                canvas.create_polygon(*flat(face), fill=fill_color, outline="", width=0,
                                      stipple=stipple, tags=tags)
            canvas.create_polygon(*flat(top), fill="", outline=outline, width=width, tags=tags)
            for opening in item.holes_xy:
                canvas.create_polygon(*flat([(x, y, item.z_top) for x, y in opening]),
                                      fill="", outline="#f59e0b", width=2, tags=tags)
            for x, y, radius in item.circular_holes:
                circle = [(x + math.cos(index * math.tau / 32) * radius,
                           y + math.sin(index * math.tau / 32) * radius, item.z_top)
                          for index in range(32)]
                canvas.create_polygon(*flat(circle), fill="", outline="#f59e0b", width=2, tags=tags)
        else:
            canvas.create_polygon(*flat(top), fill=fill_color, outline=outline, width=width,
                                  stipple=stipple, tags=tags)
            if item.details.get("结构") == "电容本体":
                projected_top = [screen(point) for point in top]
                label_x = sum(point[0] for point in projected_top) / len(projected_top)
                label_y = sum(point[1] for point in projected_top) / len(projected_top)
                self._pending_screen_labels.append({
                    "text": item.key.split(":", 1)[-1], "desired": (label_x, label_y),
                    "leader": (label_x, label_y), "color": "#786b50",
                    "foreground": "#4a4030", "background": "#fefce8",
                    "tag": tag, "role": "object-label", "priority": 0,
                })
        if selected and item.z_top - item.z_bottom > 1e-7 and not outline_only and not closed_solid:
            # Selection-only thickness aids: at most four extrema, never every
            # polygon/triangulation vertex.
            indices = {
                min(range(len(points_xy)), key=lambda index: points_xy[index][0]),
                max(range(len(points_xy)), key=lambda index: points_xy[index][0]),
                min(range(len(points_xy)), key=lambda index: points_xy[index][1]),
                max(range(len(points_xy)), key=lambda index: points_xy[index][1]),
            }
            for index in sorted(indices):
                canvas.create_line(*flat((bottom[index], top[index])), fill=outline,
                                   width=1, dash=(3, 3), tags=tags)

    def show(self, config: Mapping[str, Any], geometry: Any, validation: Any,
             snapshot: Optional[Mapping[str, Any]] = None) -> None:
        authoritative_snapshot = dict(snapshot or make_generation_snapshot(config, geometry))
        self.pending = (config, geometry, validation, authoritative_snapshot)
        if not self.realtime.get():
            self.stale_var.set("参数已变化：当前预览不是最新状态，请点击“更新预览”")
            return
        self.status_var.set("正在更新模型…")
        if self._update_job:
            try:
                self.app.after_cancel(self._update_job)
            except tk.TclError:
                pass
        # Register the debounce callback on the application root.  This keeps
        # callback ownership compatible with callers that cancel root jobs
        # before destroying a test or host window.
        self._update_job = self.app.after(80, self.update_pending)

    def show_error(self, message: str) -> None:
        self.pending = None
        self.stale_var.set("当前参数无效，已保留上一次有效模型；预览不是最新状态")
        self.status_var.set(message)

    def update_pending(self) -> None:
        # ``update_pending`` is also used as an explicit "refresh now" entry
        # point by Validate and tests.  Cancel the debounced callback first so
        # it cannot survive the window and later fire against a destroyed Tk
        # command.
        job = self._update_job
        self._update_job = None
        if job:
            try:
                self.app.after_cancel(job)
            except tk.TclError:
                pass
        if not self.pending:
            return
        config, geometry, validation, snapshot = self.pending
        self.pending = None
        try:
            self.current_config = config
            self.current_snapshot = snapshot
            self.current_validation = validation
            scene = build_model_scene(config, geometry, validation)
            self.scene = scene
            self._apply_scene(scene)
            self._update_summary()
            self.stale_var.set("")
            errors = sum(item.level == "error" for item in scene.issues)
            warnings = sum(item.level == "warning" for item in scene.issues)
            self.status_var.set(f"模型已更新 · {len(scene.objects)} 个对象 · 错误 {errors} / 警告 {warnings}")
        except Exception as exc:
            self.show_error(str(exc))

    def _apply_scene(self, scene: ModelScene) -> None:
        if not self.vtk_ready:
            incoming = {item.key: item for item in scene.objects}
            for removed in set(self.objects).difference(incoming):
                self.visibility.pop(removed, None)
                self.opacity_overrides.pop(removed, None)
                self.locked.discard(removed)
            self.objects = incoming
            for key, item in self.objects.items():
                self.visibility.setdefault(key, item.default_visible)
            self._rebuild_issues(scene.issues); self._rebuild_tree(); self._update_quick_control_states()
            self._software_fit_keys = {
                key for key, item in self.objects.items()
                if item.category not in SIMULATION_CATEGORIES
            }
            model_bounds = self._true_bounds_for_objects(
                [item for item in self.objects.values() if item.category not in SIMULATION_CATEGORIES]
            )
            if self._bounds_changed_materially(self._last_fit_signature, model_bounds):
                self._camera_initialized = False
            self._last_fit_signature = model_bounds
            self._software_redraw()
            return
        incoming = {item.key: item for item in scene.objects}
        for key in set(self.actors).difference(incoming):
            actor = self.actors.pop(key)
            self.actor_keys.pop(actor.GetAddressAsString(""), None)
            self.picker.DeletePickList(actor)
            self.renderer.RemoveActor(actor); self.fingerprints.pop(key, None)
            self.visibility.pop(key, None); self.opacity_overrides.pop(key, None)
            self._remove_decorations(key)
        for key, item in incoming.items():
            fingerprint = item.fingerprint()
            if self.fingerprints.get(key) == fingerprint:
                continue
            if key in self.actors:
                old_actor = self.actors[key]
                self.actor_keys.pop(old_actor.GetAddressAsString(""), None)
                self.renderer.RemoveActor(old_actor)
                self._remove_decorations(key)
            mapper = vtk.vtkPolyDataMapper(); mapper.SetInputData(mesh_for_object(item))
            actor = vtk.vtkActor(); actor.SetMapper(mapper)
            color = COLORS[item.color_key]; actor.GetProperty().SetColor(*color)
            actor.GetProperty().SetOpacity(item.opacity)
            if item.object_type in {"outline", "circle_outline", "box_outline"}:
                actor.GetProperty().SetRepresentationToWireframe(); actor.GetProperty().SetLineWidth(2.2)
            self.renderer.AddActor(actor); self.actors[key] = actor
            self.picker.AddPickList(actor)
            self.actor_keys[actor.GetAddressAsString("")] = key
            self.fingerprints[key] = fingerprint
            self.visibility.setdefault(key, item.default_visible)
            self._create_decorations(item)
        self.objects = incoming
        self._rebuild_tree(); self._rebuild_issues(scene.issues)
        self._apply_transforms(render=False); self._apply_appearance(render=False); self._update_clip(render=False)
        self._update_quick_control_states()
        self.set_view("等轴测", fit=False); self.fit_model()

    def _remove_decorations(self, key: str) -> None:
        if not self.vtk_ready:
            return
        for actor in self.decoration_actors.pop(key, []):
            self.renderer.RemoveActor(actor)

    def _text_actor(self, text: str, position: Tuple[float, float, float],
                    color: Tuple[float, float, float]) -> Any:
        actor = vtk.vtkBillboardTextActor3D()
        actor.SetInput(text); actor.SetPosition(*position)
        prop = actor.GetTextProperty(); prop.SetColor(*color); prop.SetFontSize(16); prop.BoldOn()
        prop.SetBackgroundColor(.97, .98, 1.0); prop.SetBackgroundOpacity(.78)
        return actor

    def _create_decorations(self, item: SceneObject) -> None:
        if not self.vtk_ready or item.category not in SIMULATION_CATEGORIES:
            return
        actors: List[Any] = []
        if item.category == "Port" and item.bounds_xy:
            x = item.bounds_xy[0]; y = (item.bounds_xy[2] + item.bounds_xy[3]) / 2.0
            z = item.z_top + max(.03, self.scene.pcb_thickness * .08 if self.scene else .03)
            direction = 1.0 if item.details.get("输入/输出") == "输入" else -1.0
            span = max(.16, (item.bounds_xy[3] - item.bounds_xy[2]) * .55)
            start, end = (x - direction * span, y, z), (x + direction * span, y, z)
            line = vtk.vtkLineSource(); line.SetPoint1(*start); line.SetPoint2(*end); line.Update()
            mapper = vtk.vtkPolyDataMapper(); mapper.SetInputConnection(line.GetOutputPort())
            line_actor = vtk.vtkActor(); line_actor.SetMapper(mapper)
            line_actor.GetProperty().SetColor(*COLORS[item.color_key]); line_actor.GetProperty().SetLineWidth(3)
            cone = vtk.vtkConeSource(); cone.SetCenter(*end); cone.SetDirection(direction, 0, 0)
            cone.SetHeight(span * .34); cone.SetRadius(span * .15); cone.SetResolution(20); cone.Update()
            cone_mapper = vtk.vtkPolyDataMapper(); cone_mapper.SetInputConnection(cone.GetOutputPort())
            cone_actor = vtk.vtkActor(); cone_actor.SetMapper(cone_mapper); cone_actor.GetProperty().SetColor(*COLORS[item.color_key])
            label = self._text_actor(
                f"{item.name}  [{item.details.get('P/N信号', '')}]  {item.details.get('输入/输出', '')}",
                (x, item.bounds_xy[3] + span * .18, z), COLORS[item.color_key],
            )
            actors.extend((line_actor, cone_actor, label))
            integration_start = tuple(item.details.get("integration_line_start", ()))
            integration_end = tuple(item.details.get("integration_line_end", ()))
            if len(integration_start) == 3 and len(integration_end) == 3:
                integration = vtk.vtkLineSource()
                integration.SetPoint1(*integration_start); integration.SetPoint2(*integration_end); integration.Update()
                integration_mapper = vtk.vtkPolyDataMapper()
                integration_mapper.SetInputConnection(integration.GetOutputPort())
                integration_actor = vtk.vtkActor(); integration_actor.SetMapper(integration_mapper)
                integration_actor.GetProperty().SetColor(.75, .10, .82)
                integration_actor.GetProperty().SetLineWidth(4)
                vector = tuple(float(integration_end[index]) - float(integration_start[index]) for index in range(3))
                vector_length = math.sqrt(sum(value * value for value in vector)) or 1.0
                direction_vector = tuple(value / vector_length for value in vector)
                integration_cone = vtk.vtkConeSource(); integration_cone.SetCenter(*integration_end)
                integration_cone.SetDirection(*direction_vector)
                integration_cone.SetHeight(max(.04, vector_length * .10))
                integration_cone.SetRadius(max(.018, vector_length * .04)); integration_cone.SetResolution(20)
                integration_cone.Update()
                integration_cone_mapper = vtk.vtkPolyDataMapper()
                integration_cone_mapper.SetInputConnection(integration_cone.GetOutputPort())
                integration_cone_actor = vtk.vtkActor(); integration_cone_actor.SetMapper(integration_cone_mapper)
                integration_cone_actor.GetProperty().SetColor(.75, .10, .82)
                integration_label = self._text_actor(
                    f"Signal -> {item.details.get('参考层', 'Reference')}",
                    tuple((float(integration_start[index]) + float(integration_end[index])) / 2.0
                          for index in range(3)), (.75, .10, .82),
                )
                actors.extend((integration_actor, integration_cone_actor, integration_label))
        elif item.bounds_xy:
            label = item.name
            if item.category == "空气盒":
                label += f" · margin {item.details.get('airbox_margin', '—')}"
            else:
                label += f" · {item.details.get('边界类型', '')}"
            actors.append(self._text_actor(
                label, (item.bounds_xy[0], item.bounds_xy[3], item.z_top),
                COLORS[item.color_key],
            ))
        for actor in actors:
            self.renderer.AddActor(actor)
        self.decoration_actors[item.key] = actors

    def _validation_summary(self) -> str:
        validation = self.current_validation
        if validation is None:
            return "未运行"
        if getattr(validation, "stale", False):
            return "已过期"
        counts = getattr(validation, "counts", None)
        if isinstance(counts, Mapping):
            if counts.get("errors"): return f"错误 {counts['errors']}"
            if counts.get("warnings"): return f"通过，警告 {counts['warnings']}"
            return "通过"
        errors = len(getattr(validation, "errors", ()) or ())
        warnings = len(getattr(validation, "warnings", ()) or ())
        return f"输入检查：错误 {errors} / 警告 {warnings}"

    def _update_summary(self) -> None:
        config = self.current_config
        if not config:
            return
        sim = config.get("simulation", {}); enabled = bool(sim.get("enabled"))
        port_count = sum(obj.category == "Port" and obj.status != "未启用" for obj in self.objects.values())
        expected_ports = 4 if enabled and sim.get("create_differential_ports") else 0
        air_state = ("未创建：仿真功能未启用（视口为灰色预估占位）" if not enabled else
                     ("已创建（本地预估）" if "airbox" in self.objects else "未创建：空气盒功能已关闭"))
        port_state = f"{port_count}/{expected_ports}" if expected_ports else (
            "未生成：仿真功能未启用" if not enabled else "未创建：端口功能已关闭")
        active_ports = [obj for obj in self.objects.values()
                        if obj.category == "Port" and obj.status != "未启用"]
        port_verification = ("HFSS已创建并验证" if active_ports and
                             all(obj.status == "HFSS已验证" for obj in active_ports)
                             else "本地PortGeometry一致，但尚未经过HFSS实际验证")
        outputs = sim.get("report_outputs", {})
        selected_outputs = ", ".join(name for name, value in outputs.items() if value) or "无"
        source = "HFSS实际工程" if any(obj.data_source == "HFSS实际工程" for obj in self.objects.values()) else "本地统一几何计算"
        lines = [
            f"仿真：{'启用' if enabled else '关闭'}",
            f"generation_id：{str((self.current_snapshot or {}).get('generation_id', '未生成'))}",
            f"config_hash：{str((self.current_snapshot or {}).get('config_hash', '未生成'))}",
            f"geometry_hash：{str((self.current_snapshot or {}).get('geometry_hash', '未生成'))}",
            f"Solution Type：{config.get('project', {}).get('solution_type', '—')}",
            f"Port：{port_state}", f"Port验证：{port_verification}", f"空气盒：{air_state}",
            f"空气盒边距：{self._shown_length(float(sim.get('airbox_margin', 0.0)))}",
            f"开放边界：{sim.get('open_boundary', '—') if enabled and sim.get('create_airbox') else '依赖空气盒'}",
            f"Setup：{(str(sim.get('setup_name', '—')) + ' · ' + format_number(float(sim.get('adaptive_frequency_ghz', 0.0)), 6) + ' GHz') if enabled else '未创建'}",
            f"最大迭代 / Delta S：{sim.get('max_passes', '—')} / {sim.get('max_delta_s', '—')}",
            f"Sweep：{(str(sim.get('sweep_name', '—')) + ' · ' + str(sim.get('sweep_type', '—'))) if enabled else '未创建'}",
            f"频率：{format_number(float(sim.get('start_frequency_ghz', 0.0)), 6)} … "
            f"{format_number(float(sim.get('stop_frequency_ghz', 0.0)), 6)} GHz · 步长 "
            f"{format_number(float(sim.get('step_frequency_ghz', 0.0)), 6)} GHz",
            f"报告输出：{selected_outputs if sim.get('generate_reports') else '关闭'}",
            f"Validate：{self._validation_summary()}", f"当前数据来源：{source}",
        ]
        self.summary_var.set("\n".join(lines))

    def _run_validate(self) -> None:
        self.app.validate_model(show_dialog=False)

    def _rebuild_tree(self) -> None:
        selection = self.selected_key
        for item in self.tree.get_children(): self.tree.delete(item)
        by_category: Dict[str, List[SceneObject]] = {name: [] for name in CATEGORY_ORDER}
        for item in self.objects.values(): by_category.setdefault(item.category, []).append(item)
        for category in CATEGORY_ORDER:
            children = sorted(by_category.get(category, []), key=lambda item: (item.layer, item.name))
            parent = f"cat:{category}"
            visible_count = sum(self.visibility.get(item.key, item.default_visible) for item in children)
            issue_level = ""
            if any(self.issue_levels.get(item.key) == "error" for item in children): issue_level = " ✕"
            elif any(self.issue_levels.get(item.key) == "warning" for item in children): issue_level = " ▲"
            elif children: issue_level = " ✓"
            count_text = f"（{visible_count}/{len(children)}）" if children else "（0）"
            self.tree.insert("", "end", iid=parent,
                             text=("☑ " if visible_count else "☐ ") + category + count_text + issue_level,
                             open=category in {"Port", "开放边界", "空气盒"})
            for item in children:
                prefix = "🔒" if item.key in self.locked else ("☑" if self.visibility.get(item.key, item.default_visible) else "☐")
                suffix = f"  [{item.layer}]" if item.layer else ""
                state_icon = {"HFSS已验证": "●", "有警告": "▲", "创建失败": "✕",
                              "Validate结果已过期": "◷"}.get(item.status, "◇")
                self.tree.insert(parent, "end", iid=f"obj:{item.key}",
                                 text=f"{prefix} {state_icon} {item.name}{suffix} · {item.status}")
            if not children:
                reason = self._empty_category_reason(category)
                self.tree.insert(parent, "end", iid=f"placeholder:{category}", text="— " + reason)
        if selection and self.tree.exists(f"obj:{selection}"):
            self.tree.selection_set(f"obj:{selection}")

    def _rebuild_issues(self, issues: Sequence[SceneIssue]) -> None:
        self.issues = list(issues)
        self.issue_levels.clear()
        for issue in self.issues:
            for key in issue.object_keys:
                current = self.issue_levels.get(key)
                if current != "error" or issue.level == "error":
                    self.issue_levels[key] = issue.level
        for item in self.issue_tree.get_children(): self.issue_tree.delete(item)
        for index, issue in enumerate(self.issues):
            self.issue_tree.insert("", "end", iid=str(index),
                                   values=("错误" if issue.level == "error" else "警告", issue.message),
                                   tags=(issue.level,))
        self.issue_tree.tag_configure("error", foreground="#b42318")
        self.issue_tree.tag_configure("warning", foreground="#b45309")

    def _selected_keys(self) -> List[str]:
        selected = self.tree.selection()
        if not selected: return []
        iid = selected[0]
        if iid.startswith("obj:"): return [iid[4:]]
        if iid.startswith("cat:"):
            category = iid[4:]
            return [item.key for item in self.objects.values() if item.category == category]
        return []

    def _empty_category_reason(self, category: str) -> str:
        sim = self.current_config.get("simulation", {}) if self.current_config else {}
        if category == "Port":
            if not sim.get("enabled"): return "未生成：仿真功能未启用"
            if not sim.get("create_differential_ports"): return "Port未创建：端口功能已关闭"
            return "生成失败：统一几何未返回 Port"
        if category == "空气盒":
            if not sim.get("enabled"): return "未生成：仿真功能未启用"
            if not sim.get("create_airbox"): return "空气盒未创建：空气盒功能已关闭"
            return "生成失败：统一几何未返回空气盒"
        if category == "开放边界":
            if not sim.get("enabled"): return "依赖空气盒：仿真功能未启用"
            if not sim.get("create_airbox"): return "开放边界无法应用：未创建空气盒"
            return "边界对象未生成"
        return "当前配置未创建对象"

    def _tree_selected(self, _event: Any = None) -> None:
        if self._selection_sync:
            return
        keys = self._selected_keys()
        if len(keys) == 1 and keys[0] != self.selected_key:
            self.select_object(keys[0], sync_tree=False)

    def toggle_visibility(self) -> None:
        keys = self._selected_keys()
        if not keys: return
        target = not all(self.visibility.get(key, True) for key in keys)
        for key in keys:
            if key not in self.locked: self.visibility[key] = target
        self._apply_appearance()

    def _quick_toggle(self, category: str) -> None:
        target = bool(self.quick_vars[category].get())
        for key, item in self.objects.items():
            matches = (item.category == category or
                       (category == "电容本体" and item.details.get("结构") == "电容本体"))
            if matches and key not in self.locked:
                self.visibility[key] = target
        if category == "空气盒":
            self.airbox_mode.set("仅线框" if target else "隐藏")
        self._apply_appearance()

    def _only_simulation_changed(self) -> None:
        if not self.only_simulation.get():
            self.restore_visibility()
            return
        for key, item in self.objects.items():
            reference = item.category == "PCB叠层" and item.details.get("层类型") == "plane"
            self.visibility[key] = item.category in SIMULATION_CATEGORIES or reference
        if "airbox" in self.objects:
            self.airbox_mode.set("表面透明")
        self._apply_appearance()

    def _airbox_mode_changed(self) -> None:
        mode = self.airbox_mode.get()
        if "airbox" in self.visibility:
            self.visibility["airbox"] = mode not in {"隐藏", "仅开放边界"}
        if "open_boundary" in self.visibility and mode == "仅开放边界":
            self.visibility["open_boundary"] = True
        self._apply_appearance()

    def _update_quick_control_states(self) -> None:
        sim = self.current_config.get("simulation", {}) if self.current_config else {}
        for category, variable in self.quick_vars.items():
            keys = [key for key, item in self.objects.items()
                    if item.category == category or
                    (category == "电容本体" and item.details.get("结构") == "电容本体")]
            variable.set(bool(keys) and all(self.visibility.get(key, self.objects[key].default_visible) for key in keys))
            button = self.quick_buttons.get(category)
            if button:
                configured = bool(keys)
                if category == "Port":
                    configured = configured and bool(sim.get("enabled")) and bool(sim.get("create_differential_ports"))
                elif category in {"空气盒", "开放边界"}:
                    configured = configured and bool(sim.get("enabled")) and bool(sim.get("create_airbox"))
                button.configure(state="normal" if configured else "disabled")
        simulation_ready = bool(sim.get("enabled")) and any(
            item.category in SIMULATION_CATEGORIES and item.status != "未启用"
            for item in self.objects.values()
        )
        self.only_simulation_button.configure(state="normal" if simulation_ready else "disabled")
        self.air_mode_combo.configure(state="readonly" if sim.get("enabled") and sim.get("create_airbox") else "disabled")

    def _show_context_menu(self, event: tk.Event) -> None:
        row = self.tree.identify_row(event.y)
        if not row or row.startswith("placeholder:"):
            return
        self.tree.selection_set(row)
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="显示 / 隐藏", command=self.toggle_visibility)
        menu.add_command(label="单独显示", command=self.isolate_selected)
        menu.add_command(label="聚焦", command=self.focus_selected)
        menu.add_separator(); menu.add_command(label="前往参数", command=self.goto_parameter)
        menu.add_command(label="查看 Validate 结果", command=lambda: self.navigate(""))
        menu.tk_popup(event.x_root, event.y_root)

    def isolate_selected(self) -> None:
        keys = set(self._selected_keys())
        if not keys: return
        for key in self.objects:
            if key not in self.locked: self.visibility[key] = key in keys
        self._apply_appearance()

    def show_selected_layer(self) -> None:
        keys = self._selected_keys()
        if len(keys) != 1 or not self.objects[keys[0]].layer:
            return
        layer = self.objects[keys[0]].layer
        for key, item in self.objects.items():
            if key not in self.locked:
                self.visibility[key] = item.layer == layer
        self._apply_appearance()

    def toggle_lock(self) -> None:
        for key in self._selected_keys():
            if key in self.locked: self.locked.remove(key)
            else: self.locked.add(key)
        self._rebuild_tree()

    def restore_visibility(self) -> None:
        for key, item in self.objects.items(): self.visibility[key] = item.default_visible
        self.only_simulation.set(False); self.airbox_mode.set("仅线框")
        self.opacity_overrides.clear(); self.opacity_scale.set(1.0)
        self._apply_appearance()

    def _opacity_changed(self, value: str) -> None:
        for key in self._selected_keys(): self.opacity_overrides[key] = float(value)
        self._apply_appearance()

    def _apply_appearance(self, render: bool = True) -> None:
        if not self.vtk_ready:
            self._rebuild_tree()
            self._update_quick_control_states()
            self._software_redraw()
            return
        for key, actor in self.actors.items():
            item = self.objects[key]
            visible = self.visibility.get(key, item.default_visible)
            if self.hide_dielectric.get() and item.color_key in {"dielectric", "soldermask"}: visible = False
            actor.SetVisibility(visible)
            opacity = self.opacity_overrides.get(key, item.opacity)
            if key == "airbox":
                mode = self.airbox_mode.get()
                if mode == "仅线框":
                    actor.GetProperty().SetRepresentationToWireframe(); opacity = min(opacity, .72)
                elif mode == "表面透明":
                    actor.GetProperty().SetRepresentationToSurface(); opacity = min(opacity, .07)
                elif mode == "完整透明体":
                    actor.GetProperty().SetRepresentationToSurface(); opacity = min(opacity, .16)
                if self.selected_key and self.selected_key not in {"airbox", "open_boundary"} and mode in {"表面透明", "完整透明体"}:
                    opacity = min(opacity, .025)
            if self.xray.get() and item.object_type not in {"outline", "circle_outline"}:
                opacity = min(opacity, .18 if item.color_key in {"dielectric", "plane"} else .42)
            actor.GetProperty().SetOpacity(opacity)
            if item.category == "Port":
                actor.GetProperty().SetEdgeVisibility(True); actor.GetProperty().SetEdgeColor(*COLORS[item.color_key])
                actor.GetProperty().SetLineWidth(2.8)
            for decoration in self.decoration_actors.get(key, ()):
                decoration.SetVisibility(visible)
        self._highlight_selected()
        self._rebuild_tree()
        self._update_quick_control_states()
        if render: self.render_window.Render()

    def _z_scale(self) -> float:
        """Return the nominal visual scale; authoritative object Z never changes."""
        if self.z_display_mode.get() == "增强叠层":
            return 3.0
        return {"1×": 1.0, "2×": 2.0, "5×": 5.0, "10×": 10.0}.get(
            self.z_scale_var.get(), 1.0
        )

    def _pcb_z_range(self) -> Tuple[float, float]:
        layers = [item for item in self.objects.values() if item.category == "PCB叠层"]
        if not layers:
            return (0.0, self.scene.pcb_thickness if self.scene else 0.0)
        return min(item.z_bottom for item in layers), max(item.z_top for item in layers)

    def _map_visual_z(self, z: float, *, mode: Optional[str] = None,
                      scale: Optional[float] = None) -> float:
        mode = mode or self.z_display_mode.get()
        nominal = float(scale if scale is not None else self._z_scale())
        if mode != "增强叠层":
            return float(z) * nominal
        low, high = self._pcb_z_range()
        center = (low + high) / 2.0
        mapped_low = center + (low - center) * nominal
        mapped_high = center + (high - center) * nominal
        if z < low:
            return mapped_low + (z - low)
        if z >= high:
            return mapped_high + (z - high)
        return center + (z - center) * nominal

    def _unmap_visual_z(self, visual_z: float, *, mode: Optional[str] = None,
                        scale: Optional[float] = None) -> float:
        mode = mode or self.z_display_mode.get()
        nominal = float(scale if scale is not None else self._z_scale())
        if mode != "增强叠层":
            return float(visual_z) / max(nominal, 1e-12)
        low, high = self._pcb_z_range()
        center = (low + high) / 2.0
        mapped_low = center + (low - center) * nominal
        mapped_high = center + (high - center) * nominal
        if visual_z < mapped_low:
            return low + (visual_z - mapped_low)
        if visual_z >= mapped_high:
            return high + (visual_z - mapped_high)
        return center + (visual_z - center) / max(nominal, 1e-12)

    def _visual_layer_offset(self, item: Optional[SceneObject]) -> float:
        if item is None:
            return 0.0
        multiplier = 3.0 if self.z_display_mode.get() == "增强叠层" else self._z_scale()
        return self._layer_offset(item) * multiplier

    def _display_summary(self) -> str:
        if self.z_display_mode.get() == "增强叠层":
            return "增强叠层 3×（板外对象保持真实高度）"
        return f"真实/统一比例 {self._z_scale():.1f}×"

    def _display_mode_changed(self) -> None:
        if self.z_display_mode.get() == "增强叠层":
            self.z_scale_var.set("1×")
        self._apply_transforms()

    def _layer_offset(self, item: SceneObject) -> float:
        if not self.exploded.get() or item.layer_index is None or not self.scene: return 0.0
        count = max((obj.layer_index or 0) for obj in self.objects.values()) + 1
        return ((count - 1) / 2.0 - item.layer_index) * float(self.explosion.get())

    def _apply_transforms(self, render: bool = True) -> None:
        previous_mode = self._last_visual_mode
        previous_scale = max(float(self._last_visual_z_scale), 1e-12)
        scale = self._z_scale()
        if self._camera_initialized and (previous_mode != self.z_display_mode.get()
                                         or abs(scale - previous_scale) > 1e-12):
            true_target_z = self._unmap_visual_z(
                self._camera_target[2], mode=previous_mode, scale=previous_scale
            )
            self._camera_target[2] = self._map_visual_z(true_target_z)
        self._last_visual_z_scale = scale
        self._last_visual_mode = self.z_display_mode.get()
        if not self.vtk_ready:
            notices = []
            if self.z_display_mode.get() == "增强叠层":
                notices.append("增强叠层显示，不代表真实比例；电容高度保持真实")
            elif scale > 2.0:
                notices.append(f"警告：统一Z放大 {scale:.1f}×，当前不代表真实比例")
            elif abs(scale - 1.0) > 1e-6:
                notices.append(f"统一Z放大 {scale:.1f}×（属性仍为真实尺寸）")
            if self.exploded.get(): notices.append("爆炸视图：当前为非真实位置")
            self.zoom_notice.configure(text="  ·  ".join(notices))
            self._software_redraw()
            return
        for key, actor in self.actors.items():
            item = self.objects[key]
            z0, z1 = item.z_bottom, item.z_top
            mapped0, mapped1 = self._map_visual_z(z0), self._map_visual_z(z1)
            actor_scale = ((mapped1 - mapped0) / (z1 - z0)) if abs(z1 - z0) > 1e-12 else 1.0
            actor.SetScale(1.0, 1.0, actor_scale)
            actor.SetPosition(0.0, 0.0, mapped0 - z0 * actor_scale + self._visual_layer_offset(item))
        notices = []
        if self.z_display_mode.get() == "增强叠层":
            notices.append("增强叠层显示，不代表真实比例；板外对象保持真实高度")
        elif scale > 2.0:
            notices.append(f"警告：统一Z放大 {scale:.1f}×，当前不代表真实比例")
        elif abs(scale - 1.0) > 1e-6:
            notices.append(f"统一Z放大 {scale:.1f}×（属性仍为真实尺寸）")
        if self.exploded.get(): notices.append("爆炸视图：当前为非真实位置")
        self.zoom_notice.configure(text="  ·  ".join(notices))
        self._update_clip(render=False)
        if render: self.render_window.Render()

    def _update_clip(self, render: bool = True) -> None:
        if not self.vtk_ready:
            if render:
                self._software_redraw()
            return
        if not self.scene: return
        for actor in self.actors.values(): actor.GetMapper().RemoveAllClippingPlanes()
        axis = self.slice_axis.get()
        if axis != "无":
            index = {"X": 0, "Y": 1, "Z": 2}[axis]
            low, high = (self.scene.bounds[0], self.scene.bounds[1]) if index == 0 else (
                (self.scene.bounds[2], self.scene.bounds[3]) if index == 1 else (self.scene.bounds[4], self.scene.bounds[5]))
            position = low + (high - low) * float(self.slice_position.get()) / 100.0
            origin = [0.0, 0.0, 0.0]; origin[index] = self._map_visual_z(position) if index == 2 else position
            normal = [0.0, 0.0, 0.0]; normal[index] = -1.0
            plane = vtk.vtkPlane(); plane.SetOrigin(*origin); plane.SetNormal(*normal)
            self._clip_plane = plane
            for actor in self.actors.values(): actor.GetMapper().AddClippingPlane(plane)
        if render: self.render_window.Render()

    def set_view(self, view: str, fit: bool = True) -> None:
        if not self.vtk_ready:
            requested = "前视图" if view == "叠层剖视图" else view
            orientations = {
                "等轴测": (-45.0, 28.0),
                "顶视图": (0.0, 90.0),
                "底视图": (0.0, -90.0),
                "前视图": (-90.0, 0.0),
                "后视图": (90.0, 0.0),
                "左视图": (180.0, 0.0),
                "右视图": (0.0, 0.0),
            }
            yaw, pitch = orientations.get(requested, orientations["等轴测"])
            self._camera_yaw = math.radians(yaw)
            self._camera_pitch = math.radians(pitch)
            self.software_view = requested
            if view == "叠层剖视图":
                self.slice_axis.set("Y"); self.slice_position.set(50)
            if fit:
                self.fit_model()
            else:
                self._software_redraw()
            return
        if view == "叠层剖视图":
            self.slice_axis.set("Y"); self.slice_position.set(50); self._update_clip(render=False)
            view = "前视图"
        camera = self.renderer.GetActiveCamera()
        center = (0.0, 0.0, 0.0)
        if self.scene:
            b = self.scene.bounds; center = ((b[0]+b[1])/2, (b[2]+b[3])/2,
                                               self._map_visual_z((b[4]+b[5])/2))
        directions = {
            "顶视图": (0, 0, 1), "底视图": (0, 0, -1), "前视图": (0, -1, 0),
            "后视图": (0, 1, 0), "左视图": (-1, 0, 0), "右视图": (1, 0, 0),
            "等轴测": (1, -1, .75),
        }
        direction = directions.get(view, directions["等轴测"])
        distance = max(10.0, math.dist((0, 0, 0), self.scene.bounds[1::2] if self.scene else (10, 10, 10)))
        camera.SetFocalPoint(*center)
        camera.SetPosition(center[0] + direction[0]*distance,
                           center[1] + direction[1]*distance,
                           center[2] + direction[2]*distance)
        camera.SetViewUp(*(0, 1, 0) if abs(direction[2]) > .9 else (0, 0, 1))
        if view in {"顶视图", "底视图", "前视图", "后视图", "左视图", "右视图"}:
            camera.ParallelProjectionOn()
        else: camera.ParallelProjectionOff()
        if fit: self.fit_model()

    def fit_view(self) -> None:
        self.fit_model()

    def _fit_keys(self, keys: Iterable[str]) -> None:
        if not self.vtk_ready:
            selected_keys = list(dict.fromkeys(keys))
            self._software_fit_keys = set(selected_keys)
            items = [self.objects[key] for key in selected_keys
                     if key in self.objects and self.visibility.get(key, self.objects[key].default_visible)]
            self._software_fit_objects(items)
            self._software_redraw()
            return
        bounds = [self.actors[key].GetBounds() for key in keys
                  if key in self.actors and self.visibility.get(key, True)]
        bounds = [value for value in bounds if value and value[0] <= value[1]]
        if not bounds:
            return
        merged = (min(value[0] for value in bounds), max(value[1] for value in bounds),
                  min(value[2] for value in bounds), max(value[3] for value in bounds),
                  min(value[4] for value in bounds), max(value[5] for value in bounds))
        self.renderer.ResetCamera(*merged); self.renderer.ResetCameraClippingRange(); self.render_window.Render()

    def fit_model(self) -> None:
        """Fit physical PCB geometry while deliberately ignoring the simulation domain."""
        self._fit_keys(key for key, item in self.objects.items() if item.category not in SIMULATION_CATEGORIES)

    def fit_all(self) -> None:
        self._fit_keys(self.objects)

    def focus_selected(self) -> None:
        keys = self._selected_keys() or ([self.selected_key] if self.selected_key else [])
        self._fit_keys(keys)

    def _actor_key(self, actor: Any) -> str:
        return self.actor_keys.get(actor.GetAddressAsString(""), "") if actor else ""

    def _left_click(self, interactor: Any, _event: Any) -> None:
        x, y = interactor.GetEventPosition()
        self.picker.Pick(x, y, 0, self.renderer)
        actor = self.picker.GetActor(); key = self._actor_key(actor)
        if self.measure_mode.get() and actor and key:
            point = self._true_pick_point(key, self.picker.GetPickPosition())
            self.app.after_idle(lambda picked=point: self._add_measure_point(picked))
        elif key:
            if self._selection_job:
                try:
                    self.app.after_cancel(self._selection_job)
                except tk.TclError:
                    pass
            # Rendering while VTK is dispatching its mouse event can re-enter
            # the native interactor.  Defer the Tk/tree/highlight update until
            # the current native event has completely returned.
            self._selection_job = self.app.after_idle(
                lambda selected=key: self._apply_deferred_selection(selected)
            )

    def _mouse_move(self, interactor: Any, _event: Any) -> None:
        self._hover_position = tuple(map(int, interactor.GetEventPosition()))
        if self._hover_job:
            return
        self._hover_job = self.app.after(60, self._apply_hover_pick)

    def _apply_hover_pick(self) -> None:
        self._hover_job = None
        if not self.vtk_ready or not self.winfo_exists():
            return
        x, y = self._hover_position
        self.picker.Pick(x, y, 0, self.renderer)
        key = self._actor_key(self.picker.GetActor())
        if key and key in self.objects:
            item = self.objects[key]
            self.hover_var.set(f"{item.name} · {item.object_type} · {item.status} · {item.layer or '跨层'}")
            related = {key}
            if item.category == "Port":
                signal = item.details.get("P/N信号")
                related.update(obj_key for obj_key, obj in self.objects.items()
                               if obj.category == "信号走线" and obj.details.get("信号") == signal)
                for reference in str(item.details.get("参考导体", "")).split(","):
                    reference = reference.strip()
                    if reference.startswith("PLANE_") and f"layer:{reference[6:]}" in self.objects:
                        related.add(f"layer:{reference[6:]}")
            if related != self._hover_related:
                self._hover_related = related; self._highlight_selected(); self.render_window.Render()
        else:
            self.hover_var.set("悬停对象可查看名称、类型和状态")
            if self._hover_related:
                self._hover_related.clear(); self._highlight_selected(); self.render_window.Render()

    def _apply_deferred_selection(self, key: str) -> None:
        self._selection_job = None
        if self.winfo_exists():
            self.select_object(key)

    def _true_pick_point(self, key: str, point: Sequence[float]) -> Tuple[float, float, float]:
        item = self.objects[key]
        visual_z = float(point[2]) - self._visual_layer_offset(item)
        return float(point[0]), float(point[1]), self._unmap_visual_z(visual_z)

    def select_object(self, key: str, sync_tree: bool = True) -> None:
        if key not in self.objects: return
        self.selected_key = key
        tree_key = f"obj:{key}"
        if sync_tree and self.tree.exists(tree_key) and self.tree.selection() != (tree_key,):
            self._selection_sync = True
            try:
                self.tree.selection_set(tree_key); self.tree.see(tree_key)
            finally:
                self._selection_sync = False
        item = self.objects[key]
        x_text = y_text = "—"
        if item.center_xy: x_text, y_text = (self._shown_length(item.center_xy[0]), self._shown_length(item.center_xy[1]))
        elif item.points_xy:
            x_text = self._shown_length(sum(p[0] for p in item.points_xy)/len(item.points_xy))
            y_text = self._shown_length(sum(p[1] for p in item.points_xy)/len(item.points_xy))
        elif item.bounds_xy:
            x_text = self._shown_length((item.bounds_xy[0] + item.bounds_xy[1]) / 2.0)
            y_text = self._shown_length((item.bounds_xy[2] + item.bounds_xy[3]) / 2.0)
        lines = [f"名称：{item.name}", f"类型：{item.object_type}", f"类别：{item.category}",
                 f"所在层：{item.layer or '跨层'}", f"材料：{item.material or '—'}",
                 f"X/Y：{x_text} / {y_text}",
                 f"Z：{self._shown_length(item.z_bottom)} … {self._shown_length(item.z_top)}",
                 f"真实厚度/高度：{self._shown_length(item.z_top-item.z_bottom)}",
                 "参数来源：" + (", ".join(item.source_paths) or "统一几何结果"),
                 f"当前状态：{item.status}", f"数据来源：{item.data_source}",
                 f"Validate检查结果：{item.validate_result}"]
        if item.outer_radius:
            lines.append(f"外径：{self._shown_length(item.outer_radius * 2.0)}")
            if item.inner_radius:
                lines.append(f"内径：{self._shown_length(item.inner_radius * 2.0)}")
        if item.points_xy:
            xs = [point[0] for point in item.points_xy]; ys = [point[1] for point in item.points_xy]
            lines.append(f"XY占用范围：{self._shown_length(max(xs)-min(xs))} × "
                         f"{self._shown_length(max(ys)-min(ys))}")
        lines.extend(f"{name}：{value}" for name, value in item.details.items())
        self.detail_var.set("\n".join(lines)); self._highlight_selected(); self._apply_appearance(render=False)
        if self.vtk_ready:
            if item.category == "空气盒": self.fit_all()
            elif item.category == "Port": self.focus_selected()
            else: self.render_window.Render()
        elif self.software_ready:
            self._software_redraw()

    def _highlight_selected(self) -> None:
        if not self.vtk_ready: return
        for key, actor in self.actors.items():
            item = self.objects.get(key)
            if not item: continue
            if key == self.selected_key:
                color = COLORS["selected"]
            elif key in self._hover_related:
                color = COLORS["port"]
            elif key in self.issue_levels:
                color = COLORS[self.issue_levels[key]]
            elif item.status == "HFSS已验证":
                color = COLORS["success"]
            elif item.status == "创建失败":
                color = COLORS["error"]
            elif item.status == "有警告":
                color = COLORS["warning"]
            else:
                color = COLORS[item.color_key]
            actor.GetProperty().SetColor(*color)
            actor.GetProperty().SetEdgeVisibility(key == self.selected_key)
            if key == self.selected_key: actor.GetProperty().SetEdgeColor(*COLORS["selected"]); actor.GetProperty().SetLineWidth(3)

    def clear_selection(self) -> None:
        self.selected_key = ""; self.detail_var.set("尚未选择对象")
        self._clear_validation_markers()
        self._apply_appearance()

    def locate_signal_conductor(self) -> None:
        item = self.objects.get(self.selected_key)
        if not item or item.category != "Port":
            self.status_var.set("请先选择一个 Port")
            return
        signal = item.details.get("P/N信号")
        keys = [key for key, obj in self.objects.items()
                if obj.category == "信号走线" and obj.details.get("信号") == signal]
        if keys:
            self.select_object(keys[0]); self.set_view("顶视图")

    def _selected_port(self) -> Optional[SceneObject]:
        item = self.objects.get(self.selected_key)
        if not item or item.category != "Port":
            self.status_var.set("请先选择一个 Port")
            return None
        return item

    def _port_related_keys(self, port: SceneObject) -> List[str]:
        signal = str(port.details.get("P/N信号", ""))
        source = str(port.details.get("源对象", ""))
        reference_layer = str(port.details.get("参考层", ""))
        keys = [port.key]
        keys.extend(key for key, item in self.objects.items()
                    if item.name == source
                    or (item.category == "信号走线" and item.details.get("信号") == signal
                        and item.name == source)
                    or item.key == f"layer:{reference_layer}")
        # Keep only the dielectric between signal and reference; it gives the
        # cross-section context without burying the Port in the full model.
        z0, z1 = sorted((port.z_bottom, port.z_top))
        keys.extend(key for key, item in self.objects.items()
                    if item.color_key == "dielectric" and item.z_top >= z0 and item.z_bottom <= z1)
        return list(dict.fromkeys(key for key in keys if key in self.objects))

    def port_front_view(self) -> None:
        port = self._selected_port()
        if not port:
            return
        relevant = set(self._port_related_keys(port))
        for key, item in self.objects.items():
            if item.category == "Port":
                self.visibility[key] = key == port.key
            self.opacity_overrides[key] = item.opacity if key in relevant else min(item.opacity, .05)
        self._apply_appearance(render=False)
        view = "右视图" if port.details.get("输入/输出") == "输入" else "左视图"
        self.set_view(view, fit=False)
        self._fit_keys((port.key,))
        self.status_var.set(f"{port.name} 正视图 · YZ真实边界面 · Signal → {port.details.get('参考层', 'Reference')}")

    def focus_port(self) -> None:
        port = self._selected_port()
        if not port:
            return
        relevant = set(self._port_related_keys(port))
        for key, item in self.objects.items():
            self.visibility[key] = True
            self.opacity_overrides[key] = item.opacity if key in relevant else min(item.opacity, .06)
        self._apply_appearance(render=False)
        self._fit_keys(relevant)
        self.status_var.set(f"已聚焦 {port.name}；无关结构已淡化")

    def show_port_relationship(self) -> None:
        port = self._selected_port()
        if not port:
            return
        relevant = set(self._port_related_keys(port))
        for key in self.objects:
            self.visibility[key] = key in relevant
        self.opacity_overrides.clear()
        self._apply_appearance(render=False)
        self.port_front_view()
        self.status_var.set(f"仅显示 {port.name}、信号导体、{port.details.get('参考层', 'Reference')} 与必要介质")

    def _show_validate_results(self) -> None:
        if hasattr(self.app, "tabs") and hasattr(self.app, "notebook") and "Validate结果" in self.app.tabs:
            self.app.notebook.select(self.app.tabs["Validate结果"])
        else:
            self._run_validate()

    def locate_reference_conductor(self) -> None:
        item = self._selected_port()
        if not item:
            return
        for reference in str(item.details.get("参考导体", "")).split(","):
            reference = reference.strip()
            key = f"layer:{reference[6:]}" if reference.startswith("PLANE_") else ""
            if key in self.objects:
                self.select_object(key); self.set_view("叠层剖视图"); return
        self.status_var.set("当前 Port 没有可定位的参考导体")

    def _clear_validation_markers(self) -> None:
        if self.vtk_ready:
            for actor in self.validation_marker_actors:
                self.renderer.RemoveActor(actor)
        self.validation_marker_actors.clear()

    def locate_validation_item(self, item: Any) -> None:
        """Focus a structured Validate item in the authoritative 3D scene."""
        keys: List[str] = []
        for requested in getattr(item, "object_keys", ()):
            if requested in self.objects:
                keys.append(requested)
                continue
            name = str(requested or "")
            exact = [key for key, obj in self.objects.items() if obj.name == name]
            if exact:
                keys.extend(exact)
                continue
            if name in {"SIGNAL_P", "SIGNAL_N"}:
                signal = name[-1]
                keys.extend(
                    key for key, obj in self.objects.items()
                    if obj.details.get("信号") == signal
                    or (key.startswith("pad:") and key.split(":", 1)[1].startswith(signal + "_"))
                )
            elif name.startswith("PLANE_") or name.startswith("DIEL_"):
                layer_name = name.split("_", 1)[1]
                if f"layer:{layer_name}" in self.objects:
                    keys.append(f"layer:{layer_name}")
            elif name.startswith("BODY_") and f"body:{name[5:]}" in self.objects:
                keys.append(f"body:{name[5:]}")
            elif name.startswith("CAP_MODEL_") and f"bridge:{name[-1]}" in self.objects:
                keys.append(f"bridge:{name[-1]}")
        keys = list(dict.fromkeys(keys))
        if not keys:
            self.status_var.set(f"{getattr(item, 'rule_id', 'Validate')} 没有可定位的3D对象")
            return
        relevant = set(keys)
        for key, obj in self.objects.items():
            self.visibility[key] = True
            self.opacity_overrides[key] = obj.opacity if key in relevant else min(obj.opacity, .07)
        if not self.vtk_ready:
            self._clear_validation_markers()
            self.validation_marker_actors.extend(keys)
            self.select_object(keys[0])
            category = str(getattr(item, "category", ""))
            self.set_view("顶视图" if category in {"信号几何", "反焊盘", "端口与边界"} else "等轴测")
            self.status_var.set(f"已定位 {getattr(item, 'rule_id', 'Validate')} · {len(keys)} 个相关对象")
            return
        category = str(getattr(item, "category", ""))
        if category in {"GND VIA", "叠层与材料"}:
            self.xray.set(True)
            view = "叠层剖视图"
        elif category in {"信号几何", "反焊盘", "端口与边界"}:
            view = "顶视图"
        else:
            view = "等轴测"
        self._clear_validation_markers()
        marker_color = COLORS["error" if getattr(item, "level", "warning") == "error" else "warning"]
        span = 1.0
        if self.scene:
            span = max(self.scene.bounds[1]-self.scene.bounds[0], self.scene.bounds[3]-self.scene.bounds[2])
        for key in keys:
            center = self.actors[key].GetCenter()
            sphere = vtk.vtkSphereSource(); sphere.SetCenter(*center); sphere.SetRadius(max(.035, span*.012)); sphere.Update()
            mapper = vtk.vtkPolyDataMapper(); mapper.SetInputConnection(sphere.GetOutputPort())
            actor = vtk.vtkActor(); actor.SetMapper(mapper); actor.GetProperty().SetColor(*marker_color)
            actor.GetProperty().SetRepresentationToWireframe(); actor.GetProperty().SetLineWidth(3)
            self.renderer.AddActor(actor); self.validation_marker_actors.append(actor)
        self.select_object(keys[0])
        self._apply_appearance(render=False); self.set_view(view)
        self.detail_var.set(
            f"{self.detail_var.get()}\n\nValidate {getattr(item, 'rule_id', '')}：{getattr(item, 'message', '')}"
            f"\n修改建议：{getattr(item, 'suggestion', '') or '复核相关结构。'}"
        )
        self.status_var.set(f"已定位 {getattr(item, 'rule_id', 'Validate')} · {len(keys)} 个相关对象")

    def goto_parameter(self) -> None:
        if self.selected_key and self.selected_key in self.objects:
            paths = self.objects[self.selected_key].source_paths
            if paths: self.navigate(paths[0])

    def _measure_mode_changed(self) -> None:
        if not self.vtk_ready and self.measure_mode.get():
            self.measure_mode.set(False)
            self.measure_var.set("稳定3D模式暂不提供表面测距；对象属性仍显示真实尺寸")
            return
        self.measure_var.set("测距：请依次点击两个3D对象表面" if self.measure_mode.get() else "测距：关闭")

    def _add_measure_point(self, point: Tuple[float, float, float]) -> None:
        if len(self.measure_points) >= 2: self.clear_measurement()
        self.measure_points.append(point)
        sphere = vtk.vtkSphereSource(); sphere.SetCenter(point[0], point[1], self._map_visual_z(point[2])); sphere.SetRadius(.045); sphere.Update()
        mapper = vtk.vtkPolyDataMapper(); mapper.SetInputConnection(sphere.GetOutputPort())
        actor = vtk.vtkActor(); actor.SetMapper(mapper); actor.GetProperty().SetColor(*COLORS["selected"])
        self.renderer.AddActor(actor); self.measure_actors.append(actor)
        if len(self.measure_points) == 2:
            first, second = self.measure_points
            dx, dy, dz = second[0]-first[0], second[1]-first[1], second[2]-first[2]
            distance = math.sqrt(dx*dx + dy*dy + dz*dz)
            self.measure_var.set(
                f"ΔX={self._shown_length(dx)}\nΔY={self._shown_length(dy)}\n"
                f"ΔZ={self._shown_length(dz)}\n空间距离={self._shown_length(distance)}（真实尺寸）"
            )
            line = vtk.vtkLineSource(); line.SetPoint1(first[0], first[1], self._map_visual_z(first[2])); line.SetPoint2(second[0], second[1], self._map_visual_z(second[2]))
            mapper = vtk.vtkPolyDataMapper(); mapper.SetInputConnection(line.GetOutputPort())
            actor = vtk.vtkActor(); actor.SetMapper(mapper); actor.GetProperty().SetColor(*COLORS["selected"]); actor.GetProperty().SetLineWidth(3)
            self.renderer.AddActor(actor); self.measure_actors.append(actor)
        else:
            self.measure_var.set(
                f"点1：({self._shown_length(point[0])}, {self._shown_length(point[1])}, "
                f"{self._shown_length(point[2])})"
            )
        self.render_window.Render()

    def clear_measurement(self) -> None:
        if self.vtk_ready:
            for actor in self.measure_actors: self.renderer.RemoveActor(actor)
        self.measure_actors.clear(); self.measure_points.clear()
        self.measure_var.set("测距：请依次点击两个3D对象表面" if self.measure_mode.get() else "测距：关闭")
        if self.vtk_ready: self.render_window.Render()

    def _issue_selected(self, _event: Any = None) -> None:
        selected = self.issue_tree.selection()
        if not selected: return
        issue = self.issues[int(selected[0])]
        if issue.object_keys:
            self.select_object(issue.object_keys[0])
            relevant = set(issue.object_keys)
            for key, item in self.objects.items():
                self.visibility[key] = True
                self.opacity_overrides[key] = item.opacity if key in relevant else min(item.opacity, .08)
            self._apply_appearance()
        self.set_view(issue.view)
        self.detail_var.set(f"{issue.message}\n\n建议：{issue.suggestion}\n参数：{issue.path or '运行Validate模型检查'}")

    def _goto_issue(self) -> None:
        selected = self.issue_tree.selection()
        if not selected: return
        issue = self.issues[int(selected[0])]
        if issue.path: self.navigate(issue.path)
        elif issue.object_keys:
            item = self.objects.get(issue.object_keys[0])
            if item and item.source_paths: self.navigate(item.source_paths[0])

    def _realtime_changed(self) -> None:
        if self.realtime.get() and self.pending: self.update_pending()

    def cancel_pending_callbacks(self) -> None:
        for attribute in ("_update_job", "_selection_job", "_hover_job", "_canvas_job",
                          "_pane_constraint_job"):
            job = getattr(self, attribute, None)
            if job:
                try:
                    self.app.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, attribute, None)
        if self.vtk_ready:
            try: self.vtk_widget.Finalize()
            except Exception: pass
