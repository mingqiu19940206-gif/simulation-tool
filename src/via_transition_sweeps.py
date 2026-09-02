"""via_transition 参数扫描：安全数值轴、拓扑护栏与预算（规格 §11.1/§11.2/§11.3）。

- 安全数值轴（§11.1）：拓扑不变的尺寸变量，可进入原生 Optimetrics。
- 拓扑轴（§11.2）：改变保留对象集合的变量，不得进入同一原生 Parametric Setup；
  统一扫描工作流会将它们交给独立重算的 Design/case 后端。
- 预算（§11.3）：默认最大 Variation 数不超过 200，超预算禁止生成。
- ``residual_stub`` 仅在扫描区间不跨越铜层边界时才视为同一拓扑内的数值变量。
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from config_manager import format_length
from geometry_v1 import calculate_stackup_z_positions
from via_transition_geometry import derive_functional_barrel_span

# 规格 §11.1：拓扑不变的数值轴（可进入原生 Optimetrics）。
VIA_SAFE_SWEEP_PATHS: Tuple[str, ...] = (
    "via_transition.pair_pitch",
    "via_transition.entry_trace.gap",
    "via_transition.exit_trace.gap",
    "via_transition.padstack.drill_diameter",
    "via_transition.padstack.connected_pad_diameter",
    "via_transition.padstack.antipad.diameter",
    "via_transition.padstack.antipad.length",
    "via_transition.padstack.antipad.width",
    "via_transition.padstack.antipad.corner_radius",
    "via_transition.padstack.antipad.rotation_deg",
    "via_transition.padstack.antipad.clearance",
    "via_transition.padstack.antipad.end_clearance",
    "via_transition.padstack.antipad.side_clearance",
    "via_transition.padstack.antipad.length_increment",
    "via_transition.padstack.antipad.width_increment",
    "via_transition.backdrill.residual_stub",
    "via_transition.return_vias.offset_x",
    "via_transition.return_vias.offset_y",
    "via_transition.return_vias.pitch_x",
    "via_transition.return_vias.pitch_y",
)

# 首批已完成 HFSS 表达式绑定、可安全进入同一 Design 原生 Optimetrics 的轴。
# 其余 VIA_SAFE_SWEEP_PATHS 仍可用于离线计划/评分，但在对应坐标或拓扑表达式完成前
# 必须在原生生成入口 fail closed，避免出现“变量在扫、HFSS 几何不动”的假扫描。
VIA_NATIVE_SWEEP_PATHS: Tuple[str, ...] = (
    "via_transition.padstack.drill_diameter",
    "via_transition.padstack.connected_pad_diameter",
    "via_transition.padstack.antipad.diameter",
    "via_transition.padstack.antipad_diameter",
    "via_transition.padstack.antipad.length",
    "via_transition.padstack.antipad.width",
    "via_transition.padstack.antipad.corner_radius",
    "via_transition.padstack.antipad.rotation_deg",
    "via_transition.padstack.antipad.end_clearance",
    "via_transition.padstack.antipad.side_clearance",
    "via_transition.padstack.antipad.length_increment",
    "via_transition.padstack.antipad.width_increment",
)

# 规格 §11.2：拓扑轴必须被阻止进入同一原生 Parametric Setup，
# 但可由 sweep_manager 生成独立重算的 Design/case。
VIA_TOPOLOGY_SWEEP_PATHS: Tuple[str, ...] = (
    "scenario.type",
    "via_transition.entry_layer",
    "via_transition.exit_layer",
    "via_transition.drill_start_layer",
    "via_transition.drill_end_layer",
    "via_transition.padstack.nonfunctional_pad_mode",
    "via_transition.backdrill.enabled",
    "via_transition.backdrill.from_side",
    "via_transition.return_vias.enabled",
    "via_transition.return_vias.pattern",
    "via_transition.return_vias.count",
    "via_transition.padstack.antipad.topology",
    "via_transition.padstack.antipad.shape",
    "via_transition.padstack.antipad.clearance",
)


@dataclass(frozen=True)
class ViaSweepSpec:
    path: str
    label: str
    minimum: Optional[float]
    maximum: Optional[float]
    signed: bool = False


# (label, minimum, maximum, signed) 保守默认界，mm 内部单位。
_SPECS = {
    "via_transition.pair_pitch": ("差分对中心距", 0.2, 2.0, False),
    "via_transition.entry_trace.gap": ("输入传输线 GAP", 0.0, 2.0, False),
    "via_transition.exit_trace.gap": ("输出传输线 GAP", 0.0, 2.0, False),
    "via_transition.padstack.drill_diameter": ("信号孔钻孔直径", 0.05, 1.0, False),
    "via_transition.padstack.connected_pad_diameter": ("功能焊盘直径", 0.1, 2.0, False),
    "via_transition.padstack.antipad.diameter": ("圆形反焊盘直径", 0.2, 3.0, False),
    "via_transition.padstack.antipad_diameter": ("反焊盘直径", 0.2, 3.0, False),
    "via_transition.padstack.antipad.length": ("非圆反焊盘长度", 0.2, 5.0, False),
    "via_transition.padstack.antipad.width": ("非圆反焊盘宽度", 0.2, 5.0, False),
    "via_transition.padstack.antipad.corner_radius": ("圆角矩形圆角半径", 0.0, 2.5, False),
    "via_transition.padstack.antipad.rotation_deg": ("非圆反焊盘旋转角", -360.0, 360.0, True),
    "via_transition.padstack.antipad.clearance": ("最小铜边净距", 0.0, 2.0, False),
    "via_transition.padstack.antipad.end_clearance": ("端部净距", 0.0, 2.0, False),
    "via_transition.padstack.antipad.side_clearance": ("侧向净距", 0.0, 2.0, False),
    "via_transition.padstack.antipad.length_increment": ("自动适配长度增量", 0.0, 2.0, False),
    "via_transition.padstack.antipad.width_increment": ("自动适配宽度增量", 0.0, 2.0, False),
    "via_transition.backdrill.residual_stub": ("目标残桩长度", 0.0, 5.0, False),
    "via_transition.return_vias.offset_x": ("回流孔 X 向偏移", None, None, True),
    "via_transition.return_vias.offset_y": ("回流孔 Y 向偏移", None, None, True),
    "via_transition.return_vias.pitch_x": ("回流孔 X 向节距", 0.05, 3.0, False),
    "via_transition.return_vias.pitch_y": ("回流孔 Y 向节距", 0.05, 3.0, False),
}

VIA_SAFE_SWEEP_SPECS: Dict[str, ViaSweepSpec] = {
    path: ViaSweepSpec(path, label, minimum, maximum, signed)
    for path, (label, minimum, maximum, signed) in _SPECS.items()
}


def is_via_safe_sweep_path(path: str) -> bool:
    return path in VIA_SAFE_SWEEP_PATHS or path == "via_transition.padstack.antipad_diameter"


def is_via_topology_sweep_path(path: str) -> bool:
    return path in VIA_TOPOLOGY_SWEEP_PATHS


@dataclass(frozen=True)
class ViaSweepPlan:
    axes: Tuple[Tuple[str, Tuple[Any, ...]], ...]
    native_axes: Tuple[Tuple[str, Tuple[Any, ...]], ...]
    topology_axes: Tuple[Tuple[str, Tuple[Any, ...]], ...]
    case_count: int
    issues: Tuple[Tuple[str, str], ...]


def _nested_get(config: Mapping[str, Any], path: str) -> Any:
    node: Any = config
    for part in path.split("."):
        node = node[part]
    return node


def via_scan_values(path: str, sweep: Mapping[str, Any]) -> List[Any]:
    """展开一个 VIA 扫描轴的采样值。"""
    spec = VIA_SAFE_SWEEP_SPECS.get(path)
    if spec is None:
        raise KeyError(path)
    if "values" in sweep or str(sweep.get("scan_mode", sweep.get("mode", ""))) == "Explicit Values":
        values = list(sweep.get("values") or [])
    else:
        start = float(sweep["start"])
        stop = float(sweep["stop"])
        mode = str(sweep.get("scan_mode", sweep.get("mode", "LinearStep")))
        if mode in {"LinearCount", "Log"}:
            count = int(sweep.get("count", sweep.get("step", 0)))
            if count < 2:
                raise ValueError(f"{spec.label}：LinearCount/Log 至少需要 2 个采样点。")
            if mode == "Log" and (start <= 0 or stop <= 0):
                raise ValueError(f"{spec.label}：Log 扫描的 start/stop 必须大于 0。")
            if mode == "Log":
                ratio = (stop / start) ** (1.0 / (count - 1))
                values = [start * ratio ** index for index in range(count)]
            else:
                values = [start + (stop - start) * index / (count - 1) for index in range(count)]
        else:
            step = float(sweep.get("step", 0))
            if step <= 0 or stop < start:
                raise ValueError(f"{spec.label}：必须满足 stop >= start 且 step > 0。")
            count = int(math.floor((stop - start) / step + 1e-9)) + 1
            values = [start + index * step for index in range(count)]
            if values and values[-1] < stop - 1e-9:
                values.append(stop)
    result: List[Any] = []
    for value in values:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"{spec.label}：必须为有限数值。")
        if spec.minimum is not None and number < spec.minimum:
            raise ValueError(f"{spec.label}：不得小于 {spec.minimum:g}。")
        if spec.maximum is not None and number > spec.maximum:
            raise ValueError(f"{spec.label}：不得大于 {spec.maximum:g}。")
        result.append(number)
    if not result:
        raise ValueError(f"{spec.label}：扫描值列表不能为空。")
    return result


def _residual_stub_crossings(config: Mapping[str, Any]) -> List[Tuple[float, str]]:
    """返回 residual_stub 跨越铜层边界时的残桩值与层名。

    对于 bottom 背钻：残桩值 ``conn_boundary - layer.z_top`` 处有效孔桶端跨过该层上边界。
    对于 top 背钻：``layer.z_bottom - conn_boundary`` 处跨过该层下边界。
    """
    vt = config.get("via_transition", {})
    backdrill = vt.get("backdrill", {})
    entry = str(vt.get("entry_layer", ""))
    exit_layer = str(vt.get("exit_layer", ""))
    drill_start = str(vt.get("drill_start_layer", ""))
    drill_end = str(vt.get("drill_end_layer", ""))
    stackup = calculate_stackup_z_positions(config)
    names = [layer.name for layer in stackup]
    if entry not in names or exit_layer not in names or drill_start not in names or drill_end not in names:
        return []
    by_name = {layer.name: layer for layer in stackup}
    functional_span = derive_functional_barrel_span(
        names, entry, exit_layer, drill_start, drill_end)
    if functional_span is None:
        return []
    top_stub = functional_span.top_stub
    bottom_stub = functional_span.bottom_stub
    side = str(backdrill.get("from_side", "auto"))
    if side == "auto":
        if top_stub and bottom_stub:
            return []
        side = "top" if top_stub else ("bottom" if bottom_stub else "")
    copper = [layer for layer in stackup if layer.layer_type in {"signal", "plane"}]
    crossings: List[Tuple[float, str]] = []
    if side == "bottom":
        conn = by_name[functional_span.lower_functional_layer].z_bottom
        for layer in copper:
            if functional_span.lower_index < layer.index <= functional_span.drill_end_index:
                crossings.append((conn - layer.z_top, layer.name))
    elif side == "top":
        conn = by_name[functional_span.upper_functional_layer].z_top
        for layer in copper:
            if functional_span.drill_start_index <= layer.index < functional_span.upper_index:
                crossings.append((layer.z_bottom - conn, layer.name))
    return sorted(crossings)


def inspect_via_sweep_plan(config: Mapping[str, Any], max_cases: Optional[int] = None) -> ViaSweepPlan:
    """检查 VIA 场景扫描计划：数值轴、拓扑护栏、残桩跨界与预算。"""
    issues: List[Tuple[str, str]] = []
    display_unit = str(config.get("units", "mm"))
    axes: List[Tuple[str, Tuple[Any, ...]]] = []
    native_axes: List[Tuple[str, Tuple[Any, ...]]] = []
    topology_axes: List[Tuple[str, Tuple[Any, ...]]] = []
    limit = int(max_cases or config.get("sweep_execution", {}).get("max_cases", 200))
    antipad = config.get("via_transition", {}).get("padstack", {}).get("antipad", {})
    size_mode = str(antipad.get("size_mode", "manual"))
    direction_mode = str(antipad.get("direction_mode", "auto_pn"))

    for path, sweep in config.get("sweeps", {}).items():
        if not isinstance(sweep, Mapping) or not sweep.get("enabled"):
            continue
        if not str(path).startswith("via_transition.") and path != "scenario.type":
            continue  # 电容场景的扫描轴不属于 VIA 计划。
        if is_via_topology_sweep_path(path):
            topology_axes.append((path, (str(sweep.get("values", [""])[0]),)))
            issues.append((
                "error",
                f"{path} 是拓扑参数，会改变保留对象集合；"
                "不能进入本原生 Optimetrics plan，统一 sweep_manager 会将其"
                "生成为独立重算的 HFSS Design/case。",
            ))
            continue
        if not is_via_safe_sweep_path(path):
            issues.append(("error", f"扫描参数 {path} 未在 VIA 安全数值轴中登记。"))
            continue
        if path in {
                "via_transition.padstack.antipad.diameter",
                "via_transition.padstack.antipad_diameter",
                "via_transition.padstack.antipad.length",
                "via_transition.padstack.antipad.width",
        } and size_mode != "manual":
            issues.append(("error", f"{path} 在自动尺寸模式中不是有效 HFSS 轴；请扫描净距或自动适配增量。"))
            continue
        if path == "via_transition.padstack.antipad.rotation_deg" and direction_mode != "manual":
            issues.append(("error", "自动沿 P/N 轴时 rotation_deg 是只读派生值，不能加入扫描。"))
            continue
        if path in {
                "via_transition.padstack.antipad.end_clearance",
                "via_transition.padstack.antipad.side_clearance",
        } and size_mode not in {"auto_fit", "auto_plus_increment"}:
            issues.append(("error", f"{path} 只有在自动尺寸模式中才会驱动 HFSS 轮廓。"))
            continue
        if path in {
                "via_transition.padstack.antipad.length_increment",
                "via_transition.padstack.antipad.width_increment",
        } and size_mode != "auto_plus_increment":
            issues.append(("error", f"{path} 需要 auto_plus_increment 尺寸模式。"))
            continue
        try:
            values = tuple(via_scan_values(path, sweep))
        except (KeyError, TypeError, ValueError) as exc:
            issues.append(("error", f"{path}：{exc}"))
            continue
        axes.append((path, values))
        native_axes.append((path, values))

    # residual_stub 拓扑护栏（规格 §11.1）：扫描区间不得跨越铜层边界。
    residual = next((values for path, values in native_axes
                     if path == "via_transition.backdrill.residual_stub"), ())
    if residual:
        r_min, r_max = min(residual), max(residual)
        for crossing, layer in _residual_stub_crossings(config):
            if r_min + 1e-12 < crossing < r_max - 1e-12:
                issues.append((
                    "error",
                    f"via_transition.backdrill.residual_stub 扫描区间跨越铜层 {layer} 边界"
                    f"（残桩 {format_length(crossing, display_unit)} {display_unit}），"
                    f"属于拓扑变化，不能放入原生 Optimetrics。",
                ))

    case_count = math.prod(len(values) for _, values in axes) if axes else 1
    if topology_axes:
        issues.append(("error", "扫描范围包含拓扑轴；它们不能进入本原生 Optimetrics plan，"
                                "应由统一工作流生成独立重算的 HFSS Design/case。"))
    if case_count > limit:
        issues.append(("error", f"全因子扫描将生成 {case_count} 个 Variation，超过安全上限 {limit}。"))
    if limit < case_count and limit < 200:
        issues.append(("warning", "建议顺序：单因素敏感度 → 2～3 个主要参数粗扫 → 最优邻域细化。"))

    return ViaSweepPlan(
        tuple(axes), tuple(native_axes), tuple(topology_axes), case_count, tuple(issues)
    )


def iter_via_variations(plan: ViaSweepPlan) -> List[Dict[str, Any]]:
    """展开全因子 Variation（每个为 {path: value}）。"""
    axes = list(plan.native_axes)
    if not axes:
        return [dict()]
    return [dict(zip((path for path, _ in axes), combination))
            for combination in itertools.product(*(values for _, values in axes))]
