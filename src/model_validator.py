"""Structured two-stage model validation shared by GUI and HFSS generation."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from capacitor_editor import inspect_touchstone
from config_manager import (
    ConfigError, ConfigManager, find_nearest_reference_plane, format_length,
)
from geometry_calculator import GeometryCalculator
from gnd_via_semantics import derive_via_values, validate_via_workbench
from simulation_sweep_semantics import inspect_frequency_plan, inspect_sweep_plan
from scenario_semantics import VIA_TRANSITION, scenario_type
from via_transition_semantics import validate_via_transition

try:
    from shapely.geometry import LineString, Polygon
    from shapely.ops import unary_union
except Exception:  # pragma: no cover - deployment validation reports this explicitly
    Polygon = None  # type: ignore[assignment]
    unary_union = None  # type: ignore[assignment]


CATEGORIES = (
    "参数与文件", "叠层与材料", "信号几何", "反焊盘", "GND VIA",
    "端口与边界", "Setup与Sweep", "HFSS实际工程",
)
LEVELS = {"error", "warning", "info", "not_checked"}


def _shown_length(config: Mapping[str, Any], mm_value: float,
                  significant: int = 9) -> str:
    unit = str(config.get("units", "mm"))
    return f"{format_length(mm_value, unit, significant)} {unit}"


def config_revision(config: Mapping[str, Any]) -> str:
    payload = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class ValidationItem:
    rule_id: str
    level: str
    category: str
    message: str
    object_keys: Tuple[str, ...] = ()
    parameter_path: str = ""
    suggestion: str = ""
    source: str = "local"
    auto_fix_id: str = ""
    timestamp: str = ""
    config_revision: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["object_keys"] = list(self.object_keys)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ValidationItem":
        return cls(
            rule_id=str(data.get("rule_id", "HFSS-999")),
            level=str(data.get("level", "not_checked")),
            category=str(data.get("category", "HFSS实际工程")),
            message=str(data.get("message", "未提供消息")),
            object_keys=tuple(str(value) for value in data.get("object_keys", [])),
            parameter_path=str(data.get("parameter_path", "")),
            suggestion=str(data.get("suggestion", "")),
            source=str(data.get("source", "hfss")),
            auto_fix_id=str(data.get("auto_fix_id", "")),
            timestamp=str(data.get("timestamp", "")),
            config_revision=str(data.get("config_revision", "")),
        )


@dataclass
class ValidationReport:
    mode: str
    config_revision: str
    timestamp: str
    items: List[ValidationItem] = field(default_factory=list)
    raw_log: str = ""
    stale: bool = False
    incomplete: bool = False

    @property
    def counts(self) -> Dict[str, int]:
        return {
            "passed": sum(item.level == "info" for item in self.items),
            "errors": sum(item.level == "error" for item in self.items),
            "warnings": sum(item.level == "warning" for item in self.items),
            "not_checked": sum(item.level == "not_checked" for item in self.items),
        }

    @property
    def can_continue(self) -> bool:
        return not self.stale and not self.incomplete and self.counts["errors"] == 0

    @property
    def state(self) -> str:
        if self.stale:
            return "stale"
        if self.incomplete:
            return "incomplete"
        if self.counts["errors"]:
            return "failed"
        if self.counts["warnings"]:
            return "passed_with_warnings"
        return "passed"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": 1, "mode": self.mode,
            "config_revision": self.config_revision, "timestamp": self.timestamp,
            "state": self.state, "counts": self.counts,
            "stale": self.stale, "incomplete": self.incomplete,
            "items": [item.to_dict() for item in self.items], "raw_log": self.raw_log,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ValidationReport":
        return cls(
            mode=str(data.get("mode", "HFSS工程检查")),
            config_revision=str(data.get("config_revision", "")),
            timestamp=str(data.get("timestamp", "")),
            items=[ValidationItem.from_dict(item) for item in data.get("items", [])],
            raw_log=str(data.get("raw_log", "")),
            stale=bool(data.get("stale", False)), incomplete=bool(data.get("incomplete", False)),
        )

    def merge(self, other: "ValidationReport") -> "ValidationReport":
        items = {(item.source, item.rule_id, item.message): item for item in self.items}
        for item in other.items:
            items[(item.source, item.rule_id, item.message)] = item
        return ValidationReport(
            mode="本地快速预检 + HFSS工程检查",
            config_revision=self.config_revision or other.config_revision,
            timestamp=other.timestamp or self.timestamp,
            items=list(items.values()), raw_log="\n\n".join(filter(None, (self.raw_log, other.raw_log))),
            stale=self.stale or other.stale, incomplete=self.incomplete or other.incomplete,
        )


def export_report(report: ValidationReport, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".json":
        path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return
    counts = report.counts
    lines = [
        "模型检查报告", f"检查模式：{report.mode}", f"检查时间：{report.timestamp}",
        f"配置版本：{report.config_revision}",
        f"状态：{report.state}；通过 {counts['passed']}，错误 {counts['errors']}，警告 {counts['warnings']}，未检查 {counts['not_checked']}",
        "说明：通过仅表示具备建模或求解条件，不保证收敛或结果准确。", "",
    ]
    for item in report.items:
        lines.extend([
            f"[{item.level.upper()}] {item.rule_id} | {item.category} | {item.source}",
            item.message,
            f"对象：{', '.join(item.object_keys) or '—'}",
            f"参数：{item.parameter_path or '—'}",
            f"建议：{item.suggestion or '—'}", "",
        ])
    if report.raw_log:
        lines.extend(["原始日志", report.raw_log])
    path.write_text("\n".join(lines), encoding="utf-8")


def load_report(path: Path) -> ValidationReport:
    return ValidationReport.from_dict(json.loads(Path(path).read_text(encoding="utf-8-sig")))


class ModelValidator:
    def __init__(self, manager: ConfigManager) -> None:
        self.manager = manager

    def run_local(self, raw_config: Mapping[str, Any],
                  progress: Optional[Callable[[str, int], None]] = None) -> Tuple[ValidationReport, Optional[Dict[str, Any]], Any]:
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        raw_revision = config_revision(raw_config)
        items: List[ValidationItem] = []

        def stage(text: str, percent: int) -> None:
            if progress:
                progress(text, percent)

        def add(rule: str, level: str, category: str, message: str, *,
                objects: Sequence[str] = (), path: str = "", suggestion: str = "",
                fix: str = "") -> None:
            if level not in LEVELS:
                raise ValueError(level)
            item = ValidationItem(rule, level, category, message, tuple(objects), path,
                                  suggestion, "local", fix, now, raw_revision)
            key = (item.rule_id, item.level, item.message, item.parameter_path)
            if key not in {(old.rule_id, old.level, old.message, old.parameter_path) for old in items}:
                items.append(item)

        stage("正在检查参数与文件", 8)
        try:
            config, basic = self.manager.validate(raw_config, check_output_directory=True)
        except (ConfigError, KeyError, TypeError, ValueError) as exc:
            add("PAR-001", "error", "参数与文件", f"配置无法规范化：{exc}",
                suggestion="检查空值、数值格式和必填字段后重新运行 Validate。")
            return ValidationReport("本地快速预检", raw_revision, now, items,
                                    raw_log=str(exc)), None, None

        revision = config_revision(config)
        raw_revision = revision
        # Existing validators remain the authoritative parameter contract.
        for message in basic.errors:
            rule, category, path, suggestion, fix = _classify_message(message)
            add(rule, "error", category, message, path=path, suggestion=suggestion, fix=fix)
        for message in basic.warnings:
            rule, category, path, suggestion, fix = _classify_message(message)
            add(rule, "warning", category, message, path=path, suggestion=suggestion, fix=fix)

        scenario = scenario_type(config)
        if scenario != VIA_TRANSITION:
            self._check_touchstone(config, add)
        stage("正在计算统一几何", 24)
        geometry = None
        if not any(item.level == "error" for item in items):
            try:
                geometry = GeometryCalculator(config).calculate()
            except Exception as exc:
                add("GEO-001", "error", "信号几何", f"统一几何计算失败：{exc}",
                    suggestion="修正几何参数；Validate 不会显示无意义的替代模型。")

        if scenario == VIA_TRANSITION:
            stage("正在检查VIA换层", 52)
            self._check_via_transition(config, geometry, add)
        else:
            stage("正在检查叠层与材料", 38)
            self._check_stackup(config, geometry, add)
            if geometry is not None:
                stage("正在检查信号几何", 52)
                self._check_signal_geometry(config, geometry, add)
                stage("正在检查反焊盘", 65)
                self._check_antipads(config, geometry, add)
                stage("正在检查GND VIA", 75)
                self._check_vias(config, geometry, add)
                stage("正在检查端口与边界", 84)
                self._check_ports_boundaries(config, geometry, add)
            elif config.get("simulation", {}).get("enabled"):
                stage("正在检查端口与边界", 84)
                self._check_ports_boundaries(config, None, add)
        stage("正在检查Setup与Sweep", 92)
        self._check_setup_sweep(config, add)

        # One positive result per completed category keeps the summary useful
        # without generating hundreds of "pass" rows.
        for index, category in enumerate(CATEGORIES[:-1], 1):
            if not any(item.category == category and item.level in {"error", "warning"} for item in items):
                add(f"{_category_prefix(category)}-000", "info", category,
                    f"{category}检查通过。", suggestion="无需修改。")
        add("HFSS-000", "not_checked", "HFSS实际工程",
            "尚未启动HFSS；实际对象、端口、边界、Setup、Sweep及原生Validate需建模后检查。",
            suggestion="生成并运行HFSS工程后执行HFSS工程检查。")
        stage("检查完成", 100)
        report = ValidationReport("本地快速预检", revision, now, items)
        report.raw_log = "\n".join(f"[{item.level}] {item.rule_id} {item.message}" for item in items)
        return report, config, geometry

    def failed_report(self, raw_config: Mapping[str, Any], message: str) -> ValidationReport:
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        revision = config_revision(raw_config)
        item = ValidationItem("PAR-001", "error", "参数与文件", message, suggestion="检查当前输入后重新运行 Validate。",
                              source="local", timestamp=now, config_revision=revision)
        return ValidationReport("本地快速预检", revision, now, [item], raw_log=message)

    @staticmethod
    def _check_via_transition(config: Mapping[str, Any], geometry: Any,
                              add: Callable[..., None]) -> None:
        """via_transition 场景的本地结构化校验。"""
        if geometry is not None:
            for message in getattr(geometry, "errors", []):
                add("VIA-GEO-001", "error", "信号几何", message,
                    suggestion="修正VIA换层几何参数。")
            for message in getattr(geometry, "warnings", []):
                add("VIA-GEO-002", "warning", "信号几何", message,
                    suggestion="复核该结构及制造约束。")
        for rule_id, level, message, path, suggestion in validate_via_transition(config, geometry):
            add(rule_id, level, "信号几何", message, path=path, suggestion=suggestion)

    @staticmethod
    def _check_touchstone(config: Mapping[str, Any], add: Callable[..., None]) -> None:
        model = config["capacitor_model"]
        if model.get("mode") != "sparameter":
            return
        common = str(model.get("touchstone_file", "")).strip()
        info, errors = inspect_touchstone(common, Path(__file__).resolve().parent)
        for message in errors:
            add("PAR-006", "error", "参数与文件", message, path="capacitor_model.touchstone_file",
                suggestion="选择可读的2端口或4端口Touchstone文件。")
        if info and config["simulation"].get("enabled") and info.frequency_min is not None:
            scale = {"HZ": 1e-9, "KHZ": 1e-6, "MHZ": 1e-3, "GHZ": 1.0}.get(info.frequency_unit, 1.0)
            low, high = info.frequency_min * scale, info.frequency_max * scale
            sim_low = float(config["simulation"]["start_frequency_ghz"])
            sim_high = float(config["simulation"]["stop_frequency_ghz"])
            if low > sim_low or high < sim_high:
                add("PAR-007", "error", "参数与文件",
                    f"Touchstone频率范围 {low:g}–{high:g} GHz 未覆盖仿真范围 {sim_low:g}–{sim_high:g} GHz。",
                    path="capacitor_model.touchstone_file", suggestion="更换模型或缩小扫频范围。")

    @staticmethod
    def _check_stackup(config: Mapping[str, Any], geometry: Any, add: Callable[..., None]) -> None:
        stackup = config.get("stackup", [])
        conductor = config.get("materials", {})
        if float(conductor.get("copper_conductivity", 0.0)) <= 0:
            add("MAT-003", "error", "叠层与材料", "导体电导率必须为正数。",
                path="materials.copper_conductivity", suggestion="输入有效的导体电导率。")
        for index, layer in enumerate(stackup):
            path = f"stackup.{index}"
            if not str(layer.get("material", "")).strip():
                add("MAT-001", "error", "叠层与材料", f"层 {index} 未指定材料。", path=path,
                    suggestion="为该层指定HFSS可识别材料。")
            if layer.get("type") == "dielectric":
                dk, df = float(layer.get("dk", 0)), float(layer.get("df", -1))
                if dk <= 1 or not 0 <= df < 1:
                    add("MAT-002", "error", "叠层与材料",
                        f"介质层 {layer.get('name')} 的Dk/Df无效。", objects=(f"layer:{layer.get('name')}",), path=path,
                        suggestion="设置 Dk>1 且 0≤Df<1。")
        if geometry is not None:
            layers = list(geometry.stackup)
            for first, second in zip(layers, layers[1:]):
                if not math.isclose(float(first.z_bottom), float(second.z_top), abs_tol=1e-9):
                    add("STK-004", "error", "叠层与材料",
                        f"层 {first.name} 与 {second.name} 的Z坐标不连续。",
                        objects=(f"layer:{first.name}", f"layer:{second.name}"), path="stackup",
                        suggestion="修正叠层顺序或厚度，禁止重叠和无意间隙。")
            configured_side = str(config.get("capacitor", {}).get("mount_side", "top"))
            expected_signal = next(
                (layer for layer in (layers if configured_side == "top" else reversed(layers))
                 if layer.layer_type == "signal"), None
            )
            if (geometry.mount_side != configured_side or expected_signal is None
                    or geometry.signal_layer_name != expected_signal.name):
                add("STK-020", "error", "叠层与材料", "安装面与统一几何选择的外层信号铜不一致。",
                    path="capacitor.mount_side",
                    suggestion="修正叠层外层 signal 铜后重新计算，禁止在预览/HFSS中单独镜像。")

    @staticmethod
    def _check_signal_geometry(config: Mapping[str, Any], geometry: Any, add: Callable[..., None]) -> None:
        for message in geometry.errors:
            rule, category, path, suggestion, _fix = _classify_message(message)
            objects = _objects_for_message(message, geometry)
            add(rule, "error", category, message, objects=objects, path=path, suggestion=suggestion)
        for message in geometry.warnings:
            rule, category, path, suggestion, _fix = _classify_message(message)
            add(rule, "warning", category, message, objects=_objects_for_message(message, geometry),
                path=path, suggestion=suggestion)
        expected_z = (float(geometry.signal_z_bottom), float(geometry.signal_z_top))
        surface_shapes = [shape for signal in ("P", "N") for shape in geometry.signal_shapes[signal]]
        surface_shapes.extend(geometry.pad_shapes.values())
        surface_shapes.extend(geometry.bridge_shapes.values())
        misplaced = [shape.name for shape in surface_shapes if not (
            math.isclose(float(shape.z_bottom), expected_z[0], abs_tol=1e-9)
            and math.isclose(float(shape.z_bottom + shape.thickness), expected_z[1], abs_tol=1e-9)
        )]
        if misplaced:
            add("GEO-020", "error", "信号几何",
                "走线/焊盘/桥接未位于所选外层信号铜：" + ", ".join(misplaced),
                path="capacitor.mount_side",
                suggestion="全部表层结构必须由统一 signal_z_bottom/signal_z_top 驱动。")
        expected_model_z = expected_z[1] if geometry.mount_side == "top" else expected_z[0]
        wrong_sheets = [shape.name for shape in geometry.capacitor_model_shapes.values()
                        if not math.isclose(float(shape.z_bottom), expected_model_z, abs_tol=1e-9)]
        if wrong_sheets:
            add("CAP-020", "error", "信号几何",
                "电容电气模型Sheet不在所选铜层外表面：" + ", ".join(wrong_sheets),
                path="capacitor.mount_side", suggestion="RLC/S参数Sheet必须使用统一几何的外向表面Z。")
        for body in geometry.capacitor_bodies:
            outward = float(geometry.component_outward_z)
            outside = ((geometry.mount_side == "top"
                        and math.isclose(float(body["z_bottom"]), outward, abs_tol=1e-9)
                        and float(body["z_top"]) > outward)
                       or (geometry.mount_side == "bottom"
                           and math.isclose(float(body["z_top"]), outward, abs_tol=1e-9)
                           and float(body["z_bottom"]) < outward))
            if not outside:
                add("CAP-021", "error", "信号几何", f"{body['name']} 未从所选安装面向PCB外侧延伸。",
                    objects=(f"body:{body['capacitor']}",), path="capacitor.mount_side",
                    suggestion="本体不得嵌入介质或穿过铜层。")
        expected_mask_layers = set(geometry.surface_soldermask_layers)
        actual_mask_layers = {str(item["layer"]) for item in geometry.soldermask_openings}
        if actual_mask_layers != expected_mask_layers:
            add("GEO-021", "error", "信号几何", "所选安装面的阻焊开窗层不完整。",
                path="capacitor.mount_side", suggestion="开窗必须跟随当前安装面，不得遗留在另一表面。")
        if geometry.surface_ground and str(geometry.surface_ground.get("layer")) != geometry.signal_layer_name:
            add("GEO-022", "error", "信号几何", "安装面GND铜皮仍位于错误表面。",
                path="gnd_vias.create_top_ground_copper",
                suggestion="旧字段语义已扩展为当前安装面GND，必须使用统一 signal_layer_name。")
        if geometry.airbox:
            uncovered_bodies = [
                str(body["name"]) for body in geometry.capacitor_bodies
                if (float(geometry.airbox["z_bottom"]) > float(body["z_bottom"]) + 1e-9
                    or float(geometry.airbox["z_top"]) < float(body["z_top"]) - 1e-9)
            ]
            if uncovered_bodies:
                add("BND-020", "error", "端口与边界",
                    "空气盒未覆盖安装面外侧的电容本体：" + ", ".join(uncovered_bodies),
                    objects=("airbox",), path="simulation.airbox_margin",
                    suggestion="空气盒上下边界必须直接包络统一几何中的 capacitor_bodies。")
        if Polygon is None:
            add("GEO-099", "not_checked", "信号几何", "Shapely不可用，未执行精确多边形拓扑检查。",
                suggestion="安装 requirements.txt 中的 Shapely。")
            return
        mode = str(config["capacitor_model"].get("mode", "ideal_bridge"))
        polys: Dict[str, List[Any]] = {"P": [], "N": []}
        for signal in ("P", "N"):
            for shape in geometry.signal_shapes[signal]:
                polygon = Polygon([(point.x, point.y) for point in shape.points])
                if not polygon.is_valid or not polygon.exterior.is_simple:
                    add("GEO-006", "error", "信号几何", f"对象 {shape.name} 自相交或轮廓无效。",
                        objects=(f"signal:{shape.name}",), path="geometry.trace_join",
                        suggestion="改用受限miter/bevel并检查中心线路径。")
                if polygon.area <= 1e-12 or float(shape.thickness) <= 0:
                    add("GEO-004", "error", "信号几何", f"对象 {shape.name} 为零面积或零厚度。",
                        objects=(f"signal:{shape.name}",), path="trace.copper_thickness",
                        suggestion="增大对应长度、宽度或铜厚。")
                if polygon.area > 0 and min(polygon.bounds[2]-polygon.bounds[0], polygon.bounds[3]-polygon.bounds[1]) < 0.001:
                    add("GEO-005", "warning", "信号几何",
                        f"对象 {shape.name} 存在小于{_shown_length(config, 0.001)}的狭长尺寸。",
                        objects=(f"signal:{shape.name}",), suggestion="复核重叠量和过渡尺寸，避免极小碎片。")
                edge_lengths = [
                    math.dist(polygon.exterior.coords[index], polygon.exterior.coords[index + 1])
                    for index in range(len(polygon.exterior.coords) - 1)
                ]
                if edge_lengths and min(edge_lengths) <= 1e-8:
                    add("GEO-007", "error", "信号几何", f"对象 {shape.name} 含退化短边。",
                        objects=(f"signal:{shape.name}",), path="geometry.trace_join",
                        suggestion="检查转角拓扑边界并移除重合顶点。")
                if shape.category in {"trace_in", "trace_out"}:
                    centerline = LineString([(point.x, point.y) for point in shape.centerline])
                    configured_join = str(shape.metadata.get("join_style", "limited_miter"))
                    requested = centerline.buffer(
                        float(config["trace"]["trace_width"]) / 2.0,
                        cap_style="flat",
                        join_style="bevel" if configured_join == "bevel" else "mitre",
                        mitre_limit=float(shape.metadata.get("miter_limit", 2.0)),
                    )
                    if not polygon.buffer(1e-9).covers(requested):
                        add("GEO-008", "error", "信号几何",
                            f"对象 {shape.name} 在折角处未保持完整线宽，存在缺口风险。",
                            objects=(f"signal:{shape.name}",), path="geometry.trace_join",
                            suggestion="使用连续中心线偏置生成一张简单多边形铜皮。")
                polys[signal].append(polygon)
        union_p, union_n = unary_union(polys["P"]), unary_union(polys["N"])
        trace_lengths: Dict[str, float] = {}
        for signal in ("P", "N"):
            trace_shapes = [shape for shape in geometry.signal_shapes[signal]
                            if shape.category in {"trace_in", "trace_out"}]
            trace_lengths[signal] = sum(
                math.dist((first.x, first.y), (second.x, second.y))
                for shape in trace_shapes
                for first, second in zip(shape.centerline, shape.centerline[1:])
            )
            for shape in trace_shapes:
                segment_lengths = [
                    math.dist((first.x, first.y), (second.x, second.y))
                    for first, second in zip(shape.centerline, shape.centerline[1:])
                ]
                if not segment_lengths or min(segment_lengths) <= 1e-9:
                    add("GEO-012", "error", "信号几何",
                        f"对象 {shape.name} 的中心线含零长度或重复路径点。",
                        objects=(f"signal:{shape.name}",), path="trace.pad_escape_mode",
                        suggestion="移除重复控制点并重新生成完整中心线。")
                centerline_geometry = LineString(
                    [(point.x, point.y) for point in shape.centerline]
                )
                if not centerline_geometry.is_simple:
                    add("GEO-017", "error", "信号几何",
                        f"对象 {shape.name} 的中心线自交。",
                        objects=(f"signal:{shape.name}",), path="trace.transition_length",
                        suggestion="增大S弯过渡长度或减小旋转/间距变化。")
                first, second = shape.centerline[0], shape.centerline[1]
                before_last, last = shape.centerline[-2], shape.centerline[-1]
                if second.x <= first.x + 1e-9 or last.x <= before_last.x + 1e-9:
                    add("GEO-013", "error", "信号几何",
                        f"对象 {shape.name} 的输入/输出传播方向不是全局 +X。",
                        objects=(f"signal:{shape.name}",), path="capacitor.pad_rotation",
                        suggestion="调整旋转角或改用平滑S弯，禁止反向折返。")
        length_difference = abs(trace_lengths.get("P", 0.0) - trace_lengths.get("N", 0.0))
        if length_difference > 1e-7:
            add("GEO-010", "error", "信号几何",
                f"P/N 路径不等长：P={_shown_length(config, trace_lengths['P'])}，"
                f"N={_shown_length(config, trace_lengths['N'])}，"
                f"差={_shown_length(config, length_difference)}。",
                path="capacitor.symmetric_about_y", suggestion="保持P/N控制点关于Y=0镜像。")
        else:
            add("GEO-010", "info", "信号几何",
                f"P路径={_shown_length(config, trace_lengths['P'])}，"
                f"N路径={_shown_length(config, trace_lengths['N'])}，"
                f"长度差={_shown_length(config, length_difference)}。",
                path="trace.pad_escape_mode", suggestion="路径镜像且等长。")
        actual_gap = float(union_p.distance(union_n))
        add("GEO-011", "info", "信号几何",
            f"P/N 实际最小边到边间距={_shown_length(config, actual_gap)}；"
            f"远端目标 trace_gap={_shown_length(config, float(config['trace']['trace_gap']))}。",
            path="trace.trace_gap", suggestion="实际最小值同时受PAD间距和过渡路径约束。")
        if union_p.intersection(union_n).area > 1e-12 or union_p.distance(union_n) <= 1e-9:
            add("GEO-002", "error", "信号几何", "P/N信号铜皮发生重叠或接触。",
                objects=tuple(f"signal:{shape.name}" for signal in ("P", "N") for shape in geometry.signal_shapes[signal]),
                path="trace.trace_pitch", suggestion="增大差分间距或电容中心距。")
        for signal, union in (("P", union_p), ("N", union_n)):
            components = len(getattr(union, "geoms", (union,)))
            expected_components = 1 if mode == "ideal_bridge" else 2
            if components != expected_components:
                add("GEO-003", "error", "信号几何", f"{signal}信号从输入到输出不连续。",
                    objects=tuple(f"signal:{shape.name}" for shape in geometry.signal_shapes[signal]),
                    path="geometry.overlap", suggestion="检查焊盘引入段、焊盘和桥接重叠。")
            for side, trace_category in (("IN", "trace_in"), ("OUT", "trace_out")):
                trace_shape = next(shape for shape in geometry.signal_shapes[signal]
                                   if shape.category == trace_category)
                pad = geometry.pad_shapes[f"{signal}_{side}"]
                trace_polygon = Polygon([(point.x, point.y) for point in trace_shape.points])
                pad_polygon = Polygon([(point.x, point.y) for point in pad.points])
                contact_area = trace_polygon.intersection(pad_polygon).area
                if contact_area <= 1e-12:
                    add("GEO-014", "error", "信号几何",
                        f"{trace_shape.name} 与 {pad.name} 仅边/点接触或已断裂。",
                        objects=(f"signal:{trace_shape.name}", f"pad:{signal}_{side}"),
                        path="geometry.overlap", suggestion="增加受控PAD overlap以形成正面积连接。")
                for wrong_key, wrong_pad in geometry.pad_shapes.items():
                    if wrong_key == f"{signal}_{side}":
                        continue
                    wrong_polygon = Polygon([(point.x, point.y) for point in wrong_pad.points])
                    if trace_polygon.intersection(wrong_polygon).area > 1e-12:
                        add("GEO-015", "error", "信号几何",
                            f"{trace_shape.name} 与错误PAD {wrong_key} 发生面积相交。",
                            objects=(f"signal:{trace_shape.name}", f"pad:{wrong_key}"),
                            path="trace.pad_escape_mode", suggestion="调整出线模式、旋转角或间距。")
        endpoint_pitch_in = abs(
            geometry.trace_centerline_paths["P"]["input"][0].y
            - geometry.trace_centerline_paths["N"]["input"][0].y
        )
        endpoint_pitch_out = abs(
            geometry.trace_centerline_paths["P"]["output"][-1].y
            - geometry.trace_centerline_paths["N"]["output"][-1].y
        )
        target_pitch = float(config["trace"]["trace_pitch"])
        if not (math.isclose(endpoint_pitch_in, target_pitch, abs_tol=1e-8)
                and math.isclose(endpoint_pitch_out, target_pitch, abs_tol=1e-8)):
            add("GEO-016", "error", "信号几何",
                f"远端中心距不满足目标：输入={_shown_length(config, endpoint_pitch_in)}，"
                f"输出={_shown_length(config, endpoint_pitch_out)}，"
                f"目标={_shown_length(config, target_pitch)}。",
                path="trace.trace_pitch", suggestion="统一预览/HFSS中心线端点定义。")
        bridge_count = len(geometry.bridge_shapes)
        sheets = geometry.capacitor_model_shapes
        if mode == "ideal_bridge":
            if bridge_count != 2 or sheets:
                add("CAP-001", "error", "信号几何", "理想短路模式必须有两座铜桥且不得有电气Sheet。",
                    path="capacitor_model.mode", suggestion="重新生成模式化拓扑。")
        else:
            if bridge_count:
                add("CAP-002", "error", "信号几何", f"{mode}模式禁止任何PAD间铜桥。",
                    objects=tuple(f"bridge:{key}" for key in geometry.bridge_shapes),
                    path="capacitor_model.mode", suggestion="删除CAP_P_BRIDGE/CAP_N_BRIDGE。")
            expected_groups = {"SIGNAL_P_IN", "SIGNAL_P_OUT", "SIGNAL_N_IN", "SIGNAL_N_OUT"}
            if set(geometry.conductor_groups) != expected_groups:
                add("CAP-003", "error", "信号几何", "非理想模式必须保持四个端部导体独立。",
                    path="capacitor_model.mode", suggestion="按IN/OUT分别Unite。")
        if mode == "lumped_rlc":
            if set(sheets) != {"P", "N"}:
                add("CAP-004", "error", "信号几何", "Lumped RLC模式必须有P/N两个跨隙Sheet。",
                    path="capacitor_model.sheet_width", suggestion="按PAD内侧边缘重建Sheet。")
            else:
                for signal, shape in sheets.items():
                    start = shape.metadata.get("integration_line_start", ())
                    end = shape.metadata.get("integration_line_end", ())
                    length = math.sqrt(sum((float(end[i])-float(start[i]))**2 for i in range(3)))
                    if length <= 1e-9 or not math.isclose(length, float(config["capacitor"]["pad_gap"]), abs_tol=1e-8):
                        add("CAP-005", "error", "信号几何", f"{signal} RLC积分线未沿PAD间隙从IN指向OUT。",
                            objects=(f"electrical:{signal}",), path="capacitor_model.sheet_width",
                            suggestion="使用电容局部坐标生成Sheet和积分线。")
        elif mode == "sparameter" and set(sheets) != {"P", "N"}:
            add("CAP-006", "error", "信号几何", "S参数模式必须有P/N网络连接Sheet。",
                path="capacitor_model.touchstone_file", suggestion="重建端口映射。")
        elif mode == "open" and sheets:
            add("CAP-007", "error", "信号几何", "open模式不得创建任何电气模型。",
                path="capacitor_model.mode", suggestion="移除RLC/Touchstone Sheet。")

    @staticmethod
    def _check_antipads(config: Mapping[str, Any], geometry: Any, add: Callable[..., None]) -> None:
        reference = [layer for layer in geometry.stackup if layer.layer_type == "plane" and layer.is_reference_plane]
        enabled = [layer for layer in reference if layer.enable_antipad]
        if not enabled:
            add("ANTI-001", "warning", "反焊盘", "所有参考层均未启用反焊盘。",
                path="stackup", suggestion="确认是否确实需要完整参考铜平面。")
            return
        mode = str(config["antipad"].get("mode"))
        expected = {"independent_pad": 4, "merged_per_capacitor": 2, "merged_all": 1}.get(mode)
        for layer in enabled:
            tools = geometry.antipads_by_layer.get(layer.name, [])
            keys = tuple(f"antipad:{layer.name}:{tool.name}" for tool in tools)
            if not tools:
                add("ANTI-002", "error", "反焊盘", f"参考层 {layer.name} 未生成反焊盘开口。",
                    objects=(f"layer:{layer.name}",), path="antipad.mode", suggestion="检查层开关和外扩值。")
            elif expected is not None and len(tools) != expected:
                add("ANTI-003", "error", "反焊盘", f"参考层 {layer.name} 的模式 {mode} 应生成 {expected} 个开口，实际 {len(tools)}。",
                    objects=keys, path="antipad.mode", suggestion="重新计算统一几何并检查模式。")
            for tool in tools:
                if not geometry.plane_bounds.contains(tool.bounds):
                    add("ANTI-004", "error", "反焊盘", f"{tool.name} 超出参考平面边界。",
                        objects=(f"antipad:{layer.name}:{tool.name}", f"layer:{layer.name}"),
                        path="plane.margin_y", suggestion="增大平面边界或减小反焊盘外扩。")
                edge = min(tool.bounds.x_min - geometry.plane_bounds.x_min,
                           geometry.plane_bounds.x_max - tool.bounds.x_max,
                           tool.bounds.y_min - geometry.plane_bounds.y_min,
                           geometry.plane_bounds.y_max - tool.bounds.y_max)
                if 0 <= edge < 0.1:
                    add("ANTI-005", "warning", "反焊盘",
                        f"{tool.name} 到平面边缘的剩余铜宽仅 {_shown_length(config, edge)}。",
                        objects=(f"antipad:{layer.name}:{tool.name}",), path="plane.margin_y",
                        suggestion="复核回流连续性并增大平面余量。")
                if Polygon is not None:
                    tool_polygon = Polygon([(point.x, point.y) for point in tool.points])
                    for pad_key in tool.metadata.get("source_pads", []):
                        pad = geometry.pad_shapes.get(pad_key)
                        if pad is None:
                            add("ANTI-007", "error", "反焊盘",
                                f"{tool.name} 引用了不存在的目标焊盘 {pad_key}。",
                                objects=(f"antipad:{layer.name}:{tool.name}",),
                                path="antipad.mode",
                                suggestion="重新计算共享几何并检查反焊盘来源映射。")
                            continue
                        pad_polygon = Polygon([(point.x, point.y) for point in pad.points])
                        if not tool_polygon.buffer(1e-9).covers(pad_polygon):
                            add("ANTI-007", "error", "反焊盘",
                                f"{layer.name} 的 {tool.name} 未完整覆盖目标焊盘 {pad_key}。",
                                objects=(f"antipad:{layer.name}:{tool.name}", f"pad:{pad_key}"),
                                path="antipad.global_expansion",
                                suggestion="增大对应层外扩，或检查旋转焊盘与开口模式。")
            if Polygon is not None and mode == "independent_pad":
                polygons = [Polygon([(point.x, point.y) for point in tool.points]) for tool in tools]
                for first_index, first in enumerate(polygons):
                    for second_index in range(first_index + 1, len(polygons)):
                        if first.intersection(polygons[second_index]).area > 1e-12:
                            add("ANTI-008", "info", "反焊盘",
                                f"{layer.name} 的独立反焊盘 {tools[first_index].name} 与 "
                                f"{tools[second_index].name} 已相交；已按实际布尔结果识别为合并开口。",
                                objects=(f"antipad:{layer.name}:{tools[first_index].name}",
                                         f"antipad:{layer.name}:{tools[second_index].name}"),
                                path="antipad.global_expansion",
                                suggestion="若需要保持独立开口，请减小外扩；当前结果本身不阻止建模。")
        if abs(float(config["capacitor"].get("pad_rotation", 0))) > 1e-9:
            add("ANTI-006", "warning", "反焊盘", "旋转焊盘的包围盒/外扩可能额外移除铜皮。",
                objects=tuple(key for layer in enabled for key in
                              (f"layer:{layer.name}",)), path="capacitor.pad_rotation",
                suggestion="在3D顶视图复核真实开口与最小剩余铜宽。")

    @staticmethod
    def _check_vias(config: Mapping[str, Any], geometry: Any, add: Callable[..., None]) -> None:
        vias = config["gnd_vias"]
        if not vias.get("enabled"):
            return
        keys = tuple(f"via:{via.name}:barrel" for via in geometry.gnd_vias)
        if len(geometry.gnd_vias) != int(vias.get("count", 0)):
            add("VIA-001", "error", "GND VIA", "VIA配置数量与实际生成数量不一致。",
                objects=keys, path="gnd_vias.count", suggestion="修正布局模式、count或自定义坐标。")
        for issue in validate_via_workbench(config, geometry):
            rule = "VIA-010" if "环宽" in issue.message else "VIA-011" if "长径比" in issue.message else "VIA-002"
            add(rule, issue.level, "GND VIA", issue.message, objects=keys, path=issue.path,
                suggestion="前往GND VIA页面调整制造关系或布局。",
                fix="FIX-VIA-DUP" if "重复" in issue.message else "")
        derived = derive_via_values(config, geometry)
        clearance_layers = {layer.name for layer in derived.layers if layer.status == "clearance"}
        for via in geometry.gnd_vias:
            if set(via.clearance_layers) != clearance_layers:
                add("VIA-012", "error", "GND VIA", f"{via.name} 的非连接层避空清单与叠层分类不一致。",
                    objects=(f"via:{via.name}:barrel",), path="gnd_vias.connect_layers",
                    suggestion="检查起止层和连接层后重新计算。")

    @staticmethod
    def _check_ports_boundaries(config: Mapping[str, Any], geometry: Any, add: Callable[..., None]) -> None:
        sim = config["simulation"]
        if not sim.get("enabled"):
            return
        if sim.get("create_differential_ports"):
            configured_references = [layer for layer in config.get("stackup", [])
                                     if layer.get("type") == "plane" and layer.get("is_reference_plane")]
            requested_layer = str(sim.get("port_reference_layer", "")).strip()
            if not configured_references or (
                sim.get("port_reference_mode", "auto_nearest") == "manual"
                and requested_layer not in {str(layer.get("name")) for layer in configured_references}
            ):
                add("PORT-003", "error", "端口与边界", "端口找不到有效的参考导体/参考层。",
                    path="simulation.port_reference_layer",
                    suggestion="使用auto_nearest，或明确选择存在的参考平面层；禁止自动求解。")
            if geometry is None:
                return
            port_keys = tuple(f"port:{port['name']}" for port in geometry.ports)
            if len(geometry.ports) != 4:
                add("PORT-001", "error", "端口与边界", f"期望4个P/N输入输出端口，实际 {len(geometry.ports)}。",
                    objects=port_keys, path="simulation.create_differential_ports",
                    suggestion="使用Terminal解类型并检查输入/输出直线端面。")
            mapping = {(str(port["signal"]).upper(), str(port["side"]).upper()) for port in geometry.ports}
            if mapping != {("P", "IN"), ("N", "IN"), ("P", "OUT"), ("N", "OUT")}:
                add("PORT-009", "error", "端口与边界", "端口P/N或输入输出映射不完整。",
                    objects=port_keys, path="simulation.create_differential_ports", suggestion="检查端口映射。")
            layers = {layer.name: layer for layer in geometry.stackup}
            nearest_config = find_nearest_reference_plane(
                config.get("stackup", []), geometry.mount_side
            )
            nearest_name = str(nearest_config.get("name", "")) if nearest_config else ""
            nearest = next(
                (layer for layer in geometry.stackup if layer.name == nearest_name), None
            )
            shapes = {shape.name: shape for signal in ("P", "N")
                      for shape in geometry.signal_shapes[signal]}
            plane = geometry.plane_bounds

            def inside(value: float, low: float, high: float) -> bool:
                return low - 1e-8 <= value <= high + 1e-8

            for port in geometry.ports:
                key = (f"port:{port['name']}",)
                expected_signal = (
                    f"SIGNAL_{str(port['signal']).upper()}"
                    if str(config["capacitor_model"].get("mode")) == "ideal_bridge"
                    else f"SIGNAL_{str(port['signal']).upper()}_{str(port['side']).upper()}"
                )
                if port.get("signal_conductor") != expected_signal or port.get("source_shape") not in shapes:
                    add("PORT-002", "error", "端口与边界", f"{port['name']}找不到正确的信号导体。",
                        objects=key, path="simulation.create_differential_ports",
                        suggestion="检查PortGeometry.signal_conductor/source_shape。")
                    continue
                reference_layer = layers.get(str(port.get("reference_layer", "")))
                expected_reference = f"PLANE_{reference_layer.name}" if reference_layer else ""
                if (reference_layer is None or not reference_layer.is_reference_plane
                        or port.get("reference_conductor") != expected_reference):
                    add("PORT-003", "error", "端口与边界", f"{port['name']}找不到正确的参考导体。",
                        objects=key, path="simulation.port_reference_layer",
                        suggestion="选择一个有效参考平面并重新计算PortGeometry。")
                    continue
                references = tuple(port.get("reference_conductors", ()))
                if references != (expected_reference,):
                    add("PORT-014", "error", "端口与边界", f"{port['name']}包含未明确的多个参考导体。",
                        objects=key, path="simulation.port_reference_layer",
                        suggestion="每个默认微带端口只传入一个明确参考导体。")
                if (sim.get("port_reference_mode", "auto_nearest") == "auto_nearest"
                        and nearest is not None and reference_layer.name != nearest.name):
                    add("PORT-004", "error", "端口与边界", f"{port['name']}未选择最近有效参考层{nearest.name}。",
                        objects=key, path="simulation.port_reference_mode",
                        suggestion="重新运行auto_nearest参考层选择。")
                shape = shapes[str(port["source_shape"])]
                covers_cross_section = (
                    float(port["y_min"]) <= shape.bounds.y_min + 1e-8
                    and float(port["y_max"]) >= shape.bounds.y_max - 1e-8
                    and float(port["z_min"]) <= reference_layer.z_bottom + 1e-8
                    and float(port["z_max"]) >= geometry.trace_z_top - 1e-8
                )
                if not covers_cross_section:
                    add("PORT-005", "error", "端口与边界", f"{port['name']}未覆盖信号到参考层的完整截面。",
                        objects=key, path="simulation.create_differential_ports",
                        suggestion="端口YZ范围必须同时覆盖信号端面和所选参考导体。")
                start = tuple(float(value) for value in port.get("integration_line_start", ()))
                end = tuple(float(value) for value in port.get("integration_line_end", ()))
                if len(start) != 3 or not (
                    math.isclose(start[0], float(port["x"]), abs_tol=1e-8)
                    and inside(start[1], shape.bounds.y_min, shape.bounds.y_max)
                    and inside(start[2], geometry.trace_z_bottom, geometry.trace_z_top)
                ):
                    add("PORT-006", "error", "端口与边界", f"{port['name']}积分线起点不在信号导体端面。",
                        objects=key, path="simulation.create_differential_ports",
                        suggestion="积分线起点应位于对应SIGNAL_P/N端面中心。")
                if len(end) != 3 or not (
                    math.isclose(end[0], float(port["x"]), abs_tol=1e-8)
                    and inside(end[1], plane.y_min, plane.y_max)
                    and inside(end[2], reference_layer.z_bottom, reference_layer.z_top)
                ):
                    add("PORT-007", "error", "端口与边界", f"{port['name']}积分线终点不在参考导体。",
                        objects=key, path="simulation.port_reference_layer",
                        suggestion="积分线终点应位于所选参考平面的上表面。")
                expected_reference_z = (float(reference_layer.z_top)
                                        if geometry.mount_side == "top"
                                        else float(reference_layer.z_bottom))
                if (len(end) == 3 and not math.isclose(end[2], expected_reference_z, abs_tol=1e-8)):
                    add("PORT-015", "error", "端口与边界",
                        f"{port['name']}积分线未指向参考层朝向信号的表面。",
                        objects=key, path="capacitor.mount_side",
                        suggestion="保持信号到参考导体的电气方向，不要因Z镜像交换端子或极性。")
                expected_direction = "+X" if str(port["side"]).upper() == "IN" else "-X"
                if (port.get("normal_direction") != expected_direction
                        or port.get("propagation_direction") != expected_direction):
                    add("PORT-008", "error", "端口与边界", f"{port['name']}输入/输出法向或传播方向错误。",
                        objects=key, path="simulation.create_differential_ports",
                        suggestion="IN使用+X，OUT使用-X。")
                if geometry.airbox:
                    air = geometry.airbox
                    contained = (
                        inside(float(port["x"]), float(air["x_min"]), float(air["x_max"]))
                        and float(port["y_min"]) >= float(air["y_min"]) - 1e-8
                        and float(port["y_max"]) <= float(air["y_max"]) + 1e-8
                        and float(port["z_min"]) >= float(air["z_bottom"]) - 1e-8
                        and float(port["z_max"]) <= float(air["z_top"]) + 1e-8
                    )
                    if not contained:
                        add("PORT-010", "error", "端口与边界", f"{port['name']}超出空气盒。",
                            objects=key + ("airbox",), path="simulation.airbox_margin",
                            suggestion="扩大空气盒或修正PortGeometry，禁止仅修改预览padding。")
            for side in ("IN", "OUT"):
                side_ports = [port for port in geometry.ports if port["side"] == side]
                if len(side_ports) == 2:
                    overlap = min(float(port["y_max"]) for port in side_ports) - max(
                        float(port["y_min"]) for port in side_ports)
                    if overlap > 1e-8:
                        add("PORT-005", "error", "端口与边界", f"{side}侧P/N端口平面发生重叠。",
                            objects=tuple(f"port:{port['name']}" for port in side_ports),
                            path="simulation.create_differential_ports",
                            suggestion="按P/N中线分割端口横向范围。")
            add("PORT-011", "not_checked", "HFSS实际工程", "预览PortGeometry与HFSS实际Port Sheet尚未对照。",
                objects=port_keys, suggestion="运行生成脚本后由HFSS读取Port Sheet边界并对照。")
            add("PORT-012", "not_checked", "HFSS实际工程", "HFSS端口创建状态尚未检查。",
                objects=port_keys, suggestion="运行生成脚本并检查四个wave_port返回值。")
            add("PORT-013", "not_checked", "HFSS实际工程", "输入/输出差分对设置状态尚未检查。",
                objects=port_keys, suggestion="运行生成脚本并检查set_differential_pair返回值。")
        if sim.get("create_airbox"):
            if not geometry.airbox:
                add("BND-001", "error", "端口与边界", "已请求空气盒但统一几何未生成空气盒。",
                    path="simulation.create_airbox", suggestion="检查空气盒边距和模型边界。")
            elif float(geometry.airbox["margin"]) < max(float(config["trace"]["trace_width"]) * 5,
                                                         geometry.pcb_top_z - geometry.pcb_bottom_z):
                add("BND-002", "warning", "端口与边界", "空气盒边距相对信号宽度或PCB厚度偏小。",
                    objects=("airbox", "open_boundary"), path="simulation.airbox_margin",
                    suggestion="增大空气盒边距并进行边界收敛复核。")
        elif sim.get("open_boundary"):
            add("BND-003", "warning", "端口与边界", "未创建空气盒，开放边界不会应用。",
                path="simulation.create_airbox", suggestion="创建空气盒或清除开放边界请求。")

    @staticmethod
    def _check_setup_sweep(config: Mapping[str, Any], add: Callable[..., None]) -> None:
        sim = config["simulation"]
        if sim.get("enabled"):
            for level, message in inspect_frequency_plan(sim).issues:
                add("SWP-001", level, "Setup与Sweep", message,
                    path="simulation.adaptive_frequency_ghz" if "自适应" in message else "simulation.start_frequency_ghz",
                    suggestion="修正频率范围、步进或插值参数。",
                    fix="FIX-ADAPTIVE" if "自适应频率" in message else "")
            if int(sim.get("max_passes", 0)) <= 0:
                add("SETUP-001", "error", "Setup与Sweep", "max_passes必须为正整数。",
                    path="simulation.max_passes", suggestion="设置至少1次自适应迭代。")
            if sim.get("generate_reports") and not sim.get("create_differential_ports"):
                add("SETUP-002", "warning", "Setup与Sweep", "未创建端口，不能生成依赖端口的混合模报告。",
                    path="simulation.generate_reports", suggestion="启用端口或关闭报告。")
            if sim.get("generate_tdr"):
                add("SETUP-003", "warning", "Setup与Sweep", "TDR当前只能记录复核请求，不能完整自动创建。",
                    path="simulation.generate_tdr", suggestion="在HFSS中复核端口模式、时域变换和上升时间。")
        plan = inspect_sweep_plan(config)
        for level, message in plan.issues:
            add("SWP-002", level, "Setup与Sweep", message, path="sweep_execution.max_cases",
                suggestion="减少扫描轴/离散值或提高经评估后的安全上限。")
        topology_axes = [name for name, sweep in config.get("sweeps", {}).items()
                         if isinstance(sweep, Mapping) and sweep.get("enabled")
                         and name in {"antipad.global_expansion",
                                      "antipad.first_reference_expansion"}]
        if topology_axes:
            add("SWP-003", "warning", "Setup与Sweep",
                "反焊盘外扩扫描可能跨越开口相交/分离的拓扑边界：" + "、".join(topology_axes),
                path=topology_axes[0],
                suggestion="生成扫描前检查端点及临界值附近的3D开口，避免把不同拓扑直接视为连续响应。")


def _category_prefix(category: str) -> str:
    return {"参数与文件": "PAR", "叠层与材料": "STK", "信号几何": "GEO", "反焊盘": "ANTI",
            "GND VIA": "VIA", "端口与边界": "PORT", "Setup与Sweep": "SWP"}.get(category, "VAL")


def _classify_message(message: str) -> Tuple[str, str, str, str, str]:
    text = str(message)
    path_match = re.search(r"((?:project|trace|capacitor|capacitor_model|antipad|plane|geometry|gnd_vias|simulation|sweep_execution|stackup)(?:\[[0-9]+\]|\.[A-Za-z0-9_]+)+)", text)
    path = path_match.group(1).replace("[", ".").replace("]", "") if path_match else ""
    if "场景" in text or "scenario" in text or "尚未实现" in text:
        return "SCN-001", "参数与文件", path or "scenario.type", "Phase 2 实现前请使用 surface_capacitor 场景。", ""
    if "Touchstone" in text or "S 参数模型" in text:
        return "PAR-006", "参数与文件", path or "capacitor_model.touchstone_file", "检查模型文件、端口数和P/N一致性。", ""
    if "输出目录" in text:
        return "PAR-004", "参数与文件", "project.output_directory", "选择存在且可写的输出目录。", ""
    if "aedt_version" in text or "solution_type" in text:
        return "PAR-005", "参数与文件", path, "选择支持的AEDT版本和Solution Type。", ""
    if "trace_pitch" in text or "trace_gap" in text:
        return "GEO-002", "信号几何", path or "trace.trace_pitch", "检查中心距、线宽和边到边间距关系。", ""
    if "扫描" in text or "频率" in text or "Sweep" in text or "TDR" in text or "报告" in text:
        return "SWP-001", "Setup与Sweep", path, "检查扫频、Setup及报告依赖。", "FIX-ADAPTIVE" if "自适应" in text else ""
    if "叠层" in text or "参考平面" in text or "stackup" in text or "介质" in text:
        return "STK-001", "叠层与材料", path or "stackup", "检查层序、类型、材料和厚度。", ""
    if "VIA" in text or "钻孔" in text or "成品孔" in text or "环宽" in text or "长径比" in text:
        return "VIA-002", "GND VIA", path, "检查VIA布局和制造关系。", "FIX-VIA-DUP" if "重复" in text else ""
    if "反焊盘" in text or "开口" in text or "开窗" in text:
        return "ANTI-002", "反焊盘", path or "antipad.global_expansion", "检查反焊盘模式、外扩和平面边界。", ""
    if any(word in text for word in ("重叠", "相交", "接触", "断裂", "短路", "镜像", "焊盘", "几何", "45")):
        return "GEO-002", "信号几何", path, "检查间距、连续性、对称性和过渡几何。", ""
    return "PAR-002", "参数与文件", path, "修正对应参数后重新检查。", ""


def _objects_for_message(message: str, geometry: Any) -> Tuple[str, ...]:
    text = str(message)
    objects: List[str] = []
    for signal in ("P", "N"):
        if signal in text or "信号" in text or "短路" in text:
            objects.extend(f"signal:{shape.name}" for shape in geometry.signal_shapes[signal])
    for name in geometry.pad_shapes:
        if name in text or "焊盘" in text:
            objects.append(f"pad:{name}")
    for via in geometry.gnd_vias:
        if via.name in text or "VIA" in text:
            objects.append(f"via:{via.name}:barrel")
    return tuple(dict.fromkeys(objects))


def preview_auto_fix(config: Mapping[str, Any], fix_id: str) -> Tuple[Dict[str, Any], List[Tuple[str, Any, Any]]]:
    updated = copy.deepcopy(dict(config))
    changes: List[Tuple[str, Any, Any]] = []
    if fix_id == "FIX-ADAPTIVE":
        sim = updated["simulation"]
        old = sim.get("adaptive_frequency_ghz")
        new = min(max(float(old), float(sim["start_frequency_ghz"])), float(sim["stop_frequency_ghz"]))
        if old != new:
            sim["adaptive_frequency_ghz"] = new; changes.append(("simulation.adaptive_frequency_ghz", old, new))
    elif fix_id == "FIX-VIA-DUP":
        coords = updated["gnd_vias"].get("custom_coordinates", [])
        unique = list(dict.fromkeys(tuple(map(float, point)) for point in coords))
        old = copy.deepcopy(coords); new = [list(point) for point in unique]
        if old != new:
            updated["gnd_vias"]["custom_coordinates"] = new
            updated["gnd_vias"]["count"] = len(new)
            changes.append(("gnd_vias.custom_coordinates", old, new))
            changes.append(("gnd_vias.count", config["gnd_vias"].get("count"), len(new)))
    return updated, changes
