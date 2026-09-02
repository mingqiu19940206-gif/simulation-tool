"""Shared semantics for frequency sweeps and HFSS design-parameter sweeps.

The default path is a native HFSS Optimetrics sweep.  ``legacy_discrete`` is
kept as an explicit compatibility backend for old projects; it is never used
implicitly by the new workbench.
"""
from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from config_manager import find_nearest_reference_plane


@dataclass(frozen=True)
class FrequencyPreset:
    name: str
    generation: str
    line_rate_gts: float
    adaptive_frequency_ghz: float
    start_frequency_ghz: float
    stop_frequency_ghz: float
    sweep_type: str = "Interpolating"


# Engineering initialization rule: solve adaptively at one half the line rate
# and inspect through twice the line rate.  It is intentionally labelled as an
# initialization aid, not a PCI-SIG compliance limit.
FREQUENCY_PRESETS: Dict[str, FrequencyPreset] = {
    "Gen3": FrequencyPreset("Gen3", "PCIe Gen3", 8.0, 4.0, 0.01, 16.0),
    "Gen4": FrequencyPreset("Gen4", "PCIe Gen4", 16.0, 8.0, 0.01, 32.0),
    "Gen5": FrequencyPreset("Gen5", "PCIe Gen5", 32.0, 16.0, 0.01, 64.0),
    "Gen6": FrequencyPreset("Gen6", "PCIe Gen6", 64.0, 32.0, 0.01, 128.0),
}


def apply_frequency_preset(config: Mapping[str, Any], name: str) -> Tuple[Dict[str, Any], Dict[str, Tuple[Any, Any]]]:
    result = copy.deepcopy(dict(config))
    if name not in FREQUENCY_PRESETS:
        result.setdefault("simulation", {})["pcie_generation_preset"] = "custom"
        return result, {}
    preset = FREQUENCY_PRESETS[name]
    sim = result.setdefault("simulation", {})
    changes: Dict[str, Tuple[Any, Any]] = {}
    values = {
        "adaptive_frequency_ghz": preset.adaptive_frequency_ghz,
        "start_frequency_ghz": preset.start_frequency_ghz,
        "stop_frequency_ghz": preset.stop_frequency_ghz,
        "sweep_type": preset.sweep_type,
    }
    for key, value in values.items():
        if sim.get(key) != value:
            changes[key] = (sim.get(key), value)
        sim[key] = value
    sim["pcie_generation_preset"] = name
    return result, changes


def preset_is_modified(simulation: Mapping[str, Any]) -> bool:
    name = str(simulation.get("pcie_generation_preset", "custom"))
    preset = FREQUENCY_PRESETS.get(name)
    if not preset:
        return False
    expected = (
        ("adaptive_frequency_ghz", preset.adaptive_frequency_ghz),
        ("start_frequency_ghz", preset.start_frequency_ghz),
        ("stop_frequency_ghz", preset.stop_frequency_ghz),
        ("sweep_type", preset.sweep_type),
    )
    return any(str(simulation.get(key)) != str(value) for key, value in expected)


@dataclass(frozen=True)
class FrequencyPlan:
    mode: str
    point_count: Optional[int]
    last_frequency_ghz: Optional[float]
    issues: Tuple[Tuple[str, str], ...]


@dataclass(frozen=True)
class TdrSweepPlan:
    enabled: bool
    sweep_name: str
    point_count: Optional[int]
    first_frequency_ghz: Optional[float]
    last_frequency_ghz: Optional[float]
    step_frequency_ghz: Optional[float]
    issues: Tuple[Tuple[str, str], ...]


def inspect_tdr_sweep_plan(simulation: Mapping[str, Any]) -> TdrSweepPlan:
    """Validate the dedicated DC-anchored sweep used by offline TDR.

    The main S-parameter sweep may remain interpolating.  TDR is allowed only
    from an explicit discrete grid ``freq[k] = (k + 1) * df`` so the export
    path never invents samples by silently resampling the solved response.
    """

    if not bool(simulation.get("generate_tdr", False)):
        return TdrSweepPlan(False, "", None, None, None, None, ())
    raw = simulation.get("tdr_sweep", {})
    if not isinstance(raw, Mapping):
        return TdrSweepPlan(
            False, "", None, None, None, None,
            (("error", "simulation.tdr_sweep 必须是对象。"),),
        )
    enabled = bool(raw.get("enabled", False))
    name = str(raw.get("sweep_name", "TDRSweep")).strip()
    if not enabled:
        return TdrSweepPlan(
            False, name, None, None, None, None,
            (("warning", "已请求 TDR，但独立 DC 锚定 TDR Sweep 未启用；不会输出 TDR 曲线。"),),
        )

    issues: List[Tuple[str, str]] = []
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
        issues.append(("error", "TDR Sweep 名称必须以字母开头且只包含字母、数字和下划线。"))
    if name == str(simulation.get("sweep_name", "Sweep1")):
        issues.append(("error", "TDR Sweep 名称必须与主 S 参数 Sweep 名称不同。"))
    try:
        start = float(raw.get("start_frequency_ghz", 0))
        stop = float(raw.get("stop_frequency_ghz", 0))
        step = float(raw.get("step_frequency_ghz", 0))
        point_limit = int(raw.get("max_points", 4096))
    except (TypeError, ValueError):
        return TdrSweepPlan(
            True, name, None, None, None, None,
            tuple(issues + [("error", "TDR Sweep 的频率和 max_points 必须为数值。")]),
        )

    if start <= 0 or step <= 0:
        issues.append(("error", "TDR Sweep 起始频率和步进必须大于 0 GHz。"))
    if stop <= start:
        issues.append(("error", "TDR Sweep 终止频率必须大于起始频率。"))
    if point_limit < 2:
        issues.append(("error", "TDR Sweep max_points 必须至少为 2。"))
    if start > 0 and step > 0 and not math.isclose(start, step, rel_tol=0, abs_tol=1e-9):
        issues.append((
            "error",
            "TDR Sweep 必须 DC 锚定：start_frequency_ghz 必须等于 step_frequency_ghz。",
        ))

    point_count: Optional[int] = None
    last: Optional[float] = None
    if step > 0 and stop > 0:
        nearest = int(round(stop / step))
        last = nearest * step
        if nearest < 2:
            issues.append(("error", "TDR Sweep 至少需要 2 个频点。"))
        elif not math.isclose(last, stop, rel_tol=0, abs_tol=1e-9):
            issues.append((
                "error",
                "TDR Sweep 终止频率必须是步进的整数倍，不能补点或插值。",
            ))
        else:
            point_count = nearest
            if point_count > point_limit:
                issues.append((
                    "error",
                    f"TDR Sweep 将生成 {point_count} 个频点，超过安全上限 {point_limit}。",
                ))
            elif point_count > 1000:
                issues.append((
                    "warning",
                    f"TDR Sweep 将求解 {point_count} 个离散频点，预计耗时较长。",
                ))

    return TdrSweepPlan(
        True, name, point_count, start, last, step, tuple(issues)
    )


def inspect_frequency_plan(simulation: Mapping[str, Any], point_limit: int = 20001) -> FrequencyPlan:
    issues: List[Tuple[str, str]] = []
    try:
        start = float(simulation.get("start_frequency_ghz", 0))
        stop = float(simulation.get("stop_frequency_ghz", 0))
        adaptive = float(simulation.get("adaptive_frequency_ghz", 0))
    except (TypeError, ValueError):
        return FrequencyPlan("invalid", None, None, (("error", "频率字段必须为数值。"),))
    if start <= 0:
        issues.append(("error", "起始频率必须大于 0 GHz。"))
    if stop <= start:
        issues.append(("error", "终止频率必须大于起始频率。"))
    if not start <= adaptive <= stop:
        issues.append(("error", "自适应频率必须位于扫频范围内。"))
    sweep_type = str(simulation.get("sweep_type", "Interpolating"))
    if sweep_type == "Discrete":
        try:
            step = float(simulation.get("step_frequency_ghz", 0))
        except (TypeError, ValueError):
            step = 0
        if step <= 0:
            issues.append(("error", "离散扫频步进必须大于 0 GHz。"))
            return FrequencyPlan(sweep_type, None, None, tuple(issues))
        count = max(0, int(math.floor((stop - start) / step + 1e-12)) + 1) if stop > start else 0
        last = start + max(0, count - 1) * step if count else None
        if count > point_limit:
            issues.append(("error", f"离散扫频将生成 {count} 个频点，超过安全上限 {point_limit}。"))
        if last is not None and not math.isclose(last, stop, rel_tol=0, abs_tol=1e-9):
            issues.append(("warning", f"步进不能整除频宽，实际最后一个频点为 {last:g} GHz。"))
        return FrequencyPlan(sweep_type, count, last, tuple(issues))
    if sweep_type == "Interpolating":
        try:
            tolerance = float(simulation.get("interpolating_tolerance", 0))
            maximum = int(simulation.get("interpolating_max_solutions", 0))
        except (TypeError, ValueError):
            tolerance, maximum = 0, 0
        if tolerance <= 0:
            issues.append(("error", "插值误差必须大于 0。"))
        if maximum < 2:
            issues.append(("error", "插值最大采样点必须至少为 2。"))
        return FrequencyPlan(sweep_type, None, None, tuple(issues))
    issues.append(("error", "扫频类型只能为 Interpolating 或 Discrete。"))
    return FrequencyPlan(sweep_type, None, None, tuple(issues))


Dependency = Callable[[Mapping[str, Any]], Optional[str]]
ApplyValue = Callable[[Dict[str, Any], Any], None]


def _nested_get(config: Mapping[str, Any], path: str) -> Any:
    node: Any = config
    for part in path.split("."):
        node = node[part]
    return node


def _nested_set(config: Dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node: Dict[str, Any] = config
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _need_vias(config: Mapping[str, Any]) -> Optional[str]:
    return None if config.get("gnd_vias", {}).get("enabled") else "依赖：请先在 GND VIA 页面启用 GND VIA。"


def _need_variable_via_count(config: Mapping[str, Any]) -> Optional[str]:
    pattern = str(config.get("gnd_vias", {}).get("pattern", ""))
    if pattern == "four_corner":
        return "四角模式数量固定为 4，不能扫描数量。"
    if pattern == "custom":
        return "自定义模式数量由坐标行数派生，不能扫描数量。"
    return None


def _need_auto_gap(config: Mapping[str, Any]) -> Optional[str]:
    return None if config.get("capacitor", {}).get("pad_pitch_mode") != "advanced_pitch" else "当前 pad_gap 为自动派生值，不能直接扫描。"


def _mount_reference(config: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    return find_nearest_reference_plane(
        config.get("stackup", []),
        str(config.get("capacitor", {}).get("mount_side", "top")),
    )


def _need_reference(config: Mapping[str, Any]) -> Optional[str]:
    layer = _mount_reference(config)
    if layer is None:
        return "依赖：当前安装面向 PCB 内部找不到有效参考平面。"
    if not layer.get("enable_antipad"):
        return f"依赖：请先在安装面最近参考层 {layer.get('name')} 启用反焊盘。"
    return None


def _apply_first_reference(config: Dict[str, Any], value: Any) -> None:
    layer = _mount_reference(config)
    if layer is None:
        raise ValueError("扫描安装面最近参考层反焊盘时找不到参考平面。")
    layer["antipad_expansion_override"] = value


def _stackup_variable_name(path: str) -> Optional[str]:
    """Map a user-named stackup field to the generator's Design Variable."""
    parts = str(path).split(".")
    if len(parts) != 3 or parts[0] != "stackup" or parts[2] not in {"thickness", "dk", "df"}:
        return None
    safe = "".join(ch.lower() if ch.isalnum() else "_" for ch in parts[1]).strip("_")
    return "{}_{}".format(safe or "layer", parts[2])


def _is_native_stackup_path(path: str) -> bool:
    return _stackup_variable_name(path) is not None


def _apply_stackup_value(config: Dict[str, Any], path: str, value: Any) -> None:
    _, layer_name, field = str(path).split(".")
    for layer in config.get("stackup", []):
        if str(layer.get("name")) == layer_name:
            layer[field] = value
            return
    raise ValueError("找不到可扫描的叠层：{}".format(layer_name))


def _apply_package(config: Dict[str, Any], value: Any) -> None:
    from config_manager import PACKAGE_PRESETS
    package = str(value)
    config["capacitor"]["package"] = package
    if package in PACKAGE_PRESETS:
        config["capacitor"].update(copy.deepcopy(PACKAGE_PRESETS[package]))


def _apply_via_signal_drill(config: Dict[str, Any], value: Any) -> None:
    """扫描机械钻孔时保持成品孔与镀铜厚度的唯一派生关系。"""

    padstack = config.setdefault("via_transition", {}).setdefault("padstack", {})
    drill = float(value)
    plating = float(padstack.get("plating_thickness", 0.0))
    padstack["drill_diameter"] = drill
    padstack["finished_hole_diameter"] = drill - 2.0 * plating


def _need_via_transition(config: Mapping[str, Any]) -> Optional[str]:
    scenario = config.get("scenario", {})
    return (None if str(scenario.get("type", "surface_capacitor")) == "via_transition"
            else "依赖：请先把 scenario.type 切换为 via_transition。")


def _need_via_circle_antipad(config: Mapping[str, Any]) -> Optional[str]:
    reason = _need_via_transition(config)
    if reason:
        return reason
    antipad = config.get("via_transition", {}).get("padstack", {}).get("antipad", {})
    if str(antipad.get("shape", "circle")) != "circle":
        return "依赖：原生直径轴仅适用于 circle；非圆形 length/width 尚未变量化，已 fail closed。"
    return None


def _need_via_non_circle_antipad(config: Mapping[str, Any]) -> Optional[str]:
    reason = _need_via_transition(config)
    if reason:
        return reason
    shape = str(config.get("via_transition", {}).get("padstack", {}).get("antipad", {}).get("shape", "circle"))
    return None if shape in {"ellipse", "capsule", "rounded_rectangle"} else \
        "依赖：length/width 轴仅适用于 ellipse/capsule/rounded_rectangle。"


def _need_manual_antipad_size(config: Mapping[str, Any]) -> Optional[str]:
    antipad = config.get("via_transition", {}).get("padstack", {}).get("antipad", {})
    if str(antipad.get("topology", "independent")) != "merged_pair":
        return None
    return None if str(antipad.get("size_mode", "manual")) == "manual" else \
        "依赖：完全手动尺寸模式才允许扫描 diameter/length/width。自动尺寸请扫描净距或尺寸增量。"


def _need_auto_antipad_size(config: Mapping[str, Any]) -> Optional[str]:
    antipad = config.get("via_transition", {}).get("padstack", {}).get("antipad", {})
    return None if str(antipad.get("size_mode", "manual")) in {"auto_fit", "auto_plus_increment"} else \
        "依赖：自动适配或自动适配＋用户增量模式。"


def _need_auto_plus_increment(config: Mapping[str, Any]) -> Optional[str]:
    antipad = config.get("via_transition", {}).get("padstack", {}).get("antipad", {})
    return None if str(antipad.get("size_mode", "manual")) == "auto_plus_increment" else \
        "依赖：尺寸模式必须为自动适配＋用户增量。"


def _need_manual_antipad_rotation(config: Mapping[str, Any]) -> Optional[str]:
    antipad = config.get("via_transition", {}).get("padstack", {}).get("antipad", {})
    if str(antipad.get("topology", "independent")) != "merged_pair":
        return None
    return None if str(antipad.get("direction_mode", "auto_pn")) == "manual" else \
        "依赖：手动旋转模式；自动沿 P/N 轴时角度是只读派生值。"


def _need_via_rounded_rectangle(config: Mapping[str, Any]) -> Optional[str]:
    reason = _need_via_transition(config)
    if reason:
        return reason
    shape = str(config.get("via_transition", {}).get("padstack", {}).get("antipad", {}).get("shape", "circle"))
    return None if shape == "rounded_rectangle" else "依赖：corner_radius 轴仅适用于 rounded_rectangle。"


def _apply_via_antipad_diameter(config: Dict[str, Any], value: Any) -> None:
    padstack = config.setdefault("via_transition", {}).setdefault("padstack", {})
    antipad = padstack.setdefault("antipad", {})
    antipad["diameter"] = float(value)
    padstack["antipad_diameter"] = float(value)  # one-way compatibility alias


@dataclass(frozen=True)
class SweepParameterSpec:
    path: str
    label: str
    source_page: str
    value_type: str
    unit_type: str
    scan_modes: Tuple[str, ...]
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    dependencies: Tuple[Dependency, ...] = ()
    derived: bool = False
    apply_value: Optional[ApplyValue] = None

    def current_value(self, config: Mapping[str, Any]) -> Any:
        if self.path == "antipad.first_reference_expansion":
            layer = _mount_reference(config)
            if layer is not None:
                value = layer.get("antipad_expansion_override")
                return config["antipad"]["global_expansion"] if value is None else value
        if _is_native_stackup_path(self.path):
            _, layer_name, field = self.path.split(".")
            for layer in config.get("stackup", []):
                if str(layer.get("name")) == layer_name:
                    return layer.get(field)
        return _nested_get(config, self.path)

    def apply(self, config: Dict[str, Any], value: Any) -> None:
        if self.apply_value is not None:
            self.apply_value(config, value)
        else:
            _nested_set(config, self.path, value)

    def dependency_issues(self, config: Mapping[str, Any]) -> Tuple[str, ...]:
        return tuple(message for dependency in self.dependencies if (message := dependency(config)))

    def validate_value(self, value: Any) -> Optional[str]:
        if self.value_type == "choice":
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "需要数值。"
        if not math.isfinite(number):
            return "必须为有限数值。"
        if self.value_type == "integer" and not number.is_integer():
            return "需要整数。"
        if self.minimum is not None and number < self.minimum:
            return f"不得小于 {self.minimum:g}。"
        if self.maximum is not None and number > self.maximum:
            return f"不得大于 {self.maximum:g}。"
        return None


# These are the variables emitted by ``hfss_script_generator.py``.  Keeping the
# mapping here makes the GUI, preflight, generated scripts and migration code
# agree on the exact Design Variable name rather than matching display labels.
NATIVE_HFSS_VARIABLES: Dict[str, str] = {
    "trace.trace_width": "trace_width",
    "trace.trace_pitch": "trace_pitch",
    "trace.transition_length": "transition_length",
    "capacitor.cap_pitch": "cap_pitch",
    "capacitor.pad_length": "pad_length",
    "capacitor.pad_width": "pad_width",
    "capacitor.pad_gap": "pad_gap",
    "capacitor.pad_rotation": "pad_rotation",
    "antipad.global_expansion": "antipad_expansion_global",
    "gnd_vias.clearance_x": "gnd_via_clearance_x",
    "gnd_vias.clearance_y": "gnd_via_clearance_y",
    "gnd_vias.drill_diameter": "gnd_via_drill_diameter",
    "gnd_vias.pad_diameter": "gnd_via_pad_diameter",
    "gnd_vias.antipad_diameter": "gnd_via_antipad_diameter",
    "capacitor_model.capacitance_pf": "capacitance_pf",
    "capacitor_model.esr_ohm": "cap_esr",
    "capacitor_model.esl_nh": "cap_esl",
    "via_transition.padstack.drill_diameter": "via_signal_drill_diameter",
    "via_transition.padstack.connected_pad_diameter": "via_signal_connected_pad_diameter",
    "via_transition.padstack.antipad.diameter": "via_signal_antipad_diameter",
    "via_transition.padstack.antipad_diameter": "via_signal_antipad_diameter",
    "via_transition.padstack.antipad.length": "via_signal_antipad_length",
    "via_transition.padstack.antipad.width": "via_signal_antipad_width",
    "via_transition.padstack.antipad.corner_radius": "via_signal_antipad_corner_radius",
    "via_transition.padstack.antipad.rotation_deg": "via_signal_antipad_rotation_input",
    "via_transition.padstack.antipad.end_clearance": "via_signal_antipad_end_clearance",
    "via_transition.padstack.antipad.side_clearance": "via_signal_antipad_side_clearance",
    "via_transition.padstack.antipad.length_increment": "via_signal_antipad_length_increment",
    "via_transition.padstack.antipad.width_increment": "via_signal_antipad_width_increment",
}

TOPOLOGY_SWEEP_PATHS = frozenset({
    "gnd_vias.count", "gnd_vias.enabled", "gnd_vias.pattern",
    "capacitor.package", "capacitor_model.mode", "antipad.mode",
    "project.solution_type", "stackup.layer_count", "stackup.reference_layer",
    "scenario.type", "via_transition.entry_layer", "via_transition.exit_layer",
    "via_transition.drill_start_layer", "via_transition.drill_end_layer",
    "via_transition.padstack.nonfunctional_pad_mode",
    "via_transition.backdrill.enabled", "via_transition.backdrill.from_side",
    "via_transition.return_vias.enabled", "via_transition.return_vias.pattern",
    "via_transition.return_vias.count",
    "via_transition.padstack.antipad.topology", "via_transition.padstack.antipad.shape",
    "via_transition.padstack.antipad.direction_mode", "via_transition.padstack.antipad.size_mode",
    # Clearance is a manufacturing constraint, not an HFSS geometry variable;
    # sweep it as explicit independently validated Design cases.
    "via_transition.padstack.antipad.clearance",
})
SUPPORTED_EXECUTION_STRATEGIES = frozenset({
    "hybrid", "native_optimetrics", "topology_variants", "legacy_discrete",
})
SUPPORTED_VARIATION_MODES = frozenset({
    "full_factorial", "synchronous", "one_at_a_time", "custom",
})
SUPPORTED_SCAN_MODES = frozenset({
    "LinearStep", "LinearCount", "Log", "Explicit Values",
})


def normalize_execution_strategy(config: Mapping[str, Any]) -> str:
    """Return the migrated execution strategy without mutating ``config``."""

    raw = str(config.get("sweep_execution", {}).get("strategy", "hybrid"))
    # V2 used ``full_factorial`` to mean the whole external-case engine.  That
    # value now describes the Variation combination mode, not the backend.
    if raw == "full_factorial":
        return "hybrid"
    return raw if raw in SUPPORTED_EXECUTION_STRATEGIES else "hybrid"


def normalize_variation_mode(config: Mapping[str, Any]) -> str:
    raw = str(config.get("sweep_execution", {}).get("variation_mode", "full_factorial"))
    return raw if raw in SUPPORTED_VARIATION_MODES else "full_factorial"


def hfss_variable_name(path: str, config: Optional[Mapping[str, Any]] = None) -> Optional[str]:
    if path in NATIVE_HFSS_VARIABLES:
        return NATIVE_HFSS_VARIABLES[path]
    stackup_name = _stackup_variable_name(path)
    if stackup_name:
        return stackup_name
    if path == "antipad.first_reference_expansion" and config is not None:
        layer = _mount_reference(config)
        if layer is not None:
            safe = "".join(ch if ch.isalnum() else "_" for ch in str(layer.get("name", ""))).strip("_")
            return "antipad_expansion_" + (safe or "reference")
    return None


def is_topology_parameter(path: str) -> bool:
    return (path in TOPOLOGY_SWEEP_PATHS or
            (path not in NATIVE_HFSS_VARIABLES and not _is_native_stackup_path(path) and
             path != "antipad.first_reference_expansion"))


def _aedt_value(path: str, value: Any, spec: SweepParameterSpec) -> str:
    if spec.unit_type == "length":
        return f"{float(value):.12g}mm"
    if spec.unit_type == "angle":
        return f"{float(value):.12g}deg"
    if spec.unit_type == "frequency":
        return f"{float(value):.12g}GHz"
    return str(value)


def aedt_sweep_data(path: str, sweep: Mapping[str, Any]) -> str:
    """Render one Optimetrics sweep definition in AEDT's compact syntax."""

    spec = SWEEP_PARAMETER_REGISTRY.get(path) or _dynamic_sweep_spec(path)
    if spec is None:
        raise KeyError(path)
    mode = str(sweep.get("scan_mode", sweep.get("mode", "LinearStep")))
    if "values" in sweep or mode == "Explicit Values":
        values = scan_values(path, sweep)
        return ", ".join(_aedt_value(path, item, spec) for item in values)
    if mode == "LinearCount":
        return "LINC {} {} {}".format(
            _aedt_value(path, sweep["start"], spec),
            _aedt_value(path, sweep["stop"], spec),
            int(sweep.get("count", sweep.get("step", 2))),
        )
    if mode == "Log":
        return "LOG {} {} {}".format(
            _aedt_value(path, sweep["start"], spec),
            _aedt_value(path, sweep["stop"], spec),
            int(sweep.get("count", sweep.get("step", 2))),
        )
    return "LIN {} {} {}".format(
        _aedt_value(path, sweep["start"], spec),
        _aedt_value(path, sweep["stop"], spec),
        _aedt_value(path, sweep["step"], spec),
    )


def _spec(path: str, label: str, page: str, value_type: str = "float", unit: str = "length",
          modes: Tuple[str, ...] = ("range", "values"), minimum: Optional[float] = 0.0,
          maximum: Optional[float] = None, deps: Tuple[Dependency, ...] = (),
          apply: Optional[ApplyValue] = None) -> SweepParameterSpec:
    return SweepParameterSpec(path, label, page, value_type, unit, modes, minimum, maximum, deps, False, apply)


def _dynamic_stackup_spec(path: str) -> Optional[SweepParameterSpec]:
    parts = str(path).split(".")
    if len(parts) != 3 or parts[0] != "stackup" or parts[2] not in {"thickness", "dk", "df"}:
        return None
    field = parts[2]
    unit = "length" if field == "thickness" else "none"
    label = "叠层 {} {}".format(parts[1], field)
    return _spec(path, label, "叠层", "float", unit, ("range", "values"), minimum=0.0,
                 apply=lambda c, v, p=path: _apply_stackup_value(c, p, v))


def _apply_via_per_layer_value(config: Dict[str, Any], path: str, value: Any) -> None:
    parts = str(path).split(".")
    if len(parts) != 6:
        raise ValueError("逐层反焊盘扫描路径无效。")
    layer_name, field = parts[4], parts[5]
    antipad = (config.setdefault("via_transition", {}).setdefault("padstack", {})
               .setdefault("antipad", {}))
    override = antipad.setdefault("per_layer", {}).setdefault(layer_name, {})
    override["enabled"] = True
    override["inherit_global"] = False
    override[field] = value


def _dynamic_via_per_layer_spec(path: str) -> Optional[SweepParameterSpec]:
    """Expose saved per-layer dimensions as explicit, recalculated cases."""

    parts = str(path).split(".")
    if len(parts) != 6 or parts[:4] != [
            "via_transition", "padstack", "antipad", "per_layer"]:
        return None
    layer_name, field = parts[4], parts[5]
    fields = {
        "diameter": ("直径", "length", 0.2, 3.0),
        "length": ("长度", "length", 0.2, 5.0),
        "width": ("宽度", "length", 0.2, 5.0),
        "corner_radius": ("圆角半径", "length", 0.0, 2.5),
        "rotation_deg": ("旋转角", "angle", -360.0, 360.0),
        "clearance": ("最小铜边净距", "length", 0.0, 2.0),
        "end_clearance": ("端部净距覆盖", "length", 0.0, 2.0),
        "side_clearance": ("侧向净距覆盖", "length", 0.0, 2.0),
        "length_increment": ("长度增量覆盖", "length", 0.0, 2.0),
        "width_increment": ("宽度增量覆盖", "length", 0.0, 2.0),
    }
    if field not in fields:
        return None
    field_label, unit, minimum, maximum = fields[field]
    return _spec(
        path, f"{layer_name} 本层反焊盘{field_label}", "反焊盘与叠层",
        "float", unit, ("range", "values"), minimum=minimum, maximum=maximum,
        deps=(_need_via_transition,),
        apply=lambda c, v, p=path: _apply_via_per_layer_value(c, p, v),
    )


def _dynamic_sweep_spec(path: str) -> Optional[SweepParameterSpec]:
    return _dynamic_stackup_spec(path) or _dynamic_via_per_layer_spec(path)


SWEEP_PARAMETER_REGISTRY: Dict[str, SweepParameterSpec] = {
    spec.path: spec for spec in (
        _spec("capacitor.cap_pitch", "P/N 两颗电容中心距", "电容与模型"),
        _spec("capacitor.pad_length", "焊盘长度", "电容与模型"),
        _spec("capacitor.pad_width", "焊盘宽度", "电容与模型"),
        _spec("capacitor.pad_gap", "同颗电容焊盘间隙", "电容与模型", deps=(_need_auto_gap,)),
        _spec("antipad.global_expansion", "全局反焊盘单边外扩", "反焊盘与叠层"),
        _spec("antipad.first_reference_expansion", "安装面最近参考层反焊盘外扩", "反焊盘与叠层", deps=(_need_reference,), apply=_apply_first_reference),
        _spec("gnd_vias.count", "GND VIA 数量", "GND VIA", "integer", "count", minimum=0,
              deps=(_need_vias, _need_variable_via_count)),
        _spec("gnd_vias.clearance_x", "GND VIA X 向有符号偏移", "GND VIA", minimum=None, deps=(_need_vias,)),
        _spec("gnd_vias.clearance_y", "GND VIA Y 向有符号偏移", "GND VIA", minimum=None, deps=(_need_vias,)),
        _spec("gnd_vias.drill_diameter", "GND VIA 钻孔直径", "GND VIA", deps=(_need_vias,)),
        _spec("gnd_vias.pad_diameter", "GND VIA 焊盘直径", "GND VIA", deps=(_need_vias,)),
        _spec("gnd_vias.antipad_diameter", "GND VIA 非连接层避空直径", "GND VIA", deps=(_need_vias,)),
        _spec("capacitor.package", "电容封装方案", "电容与模型", "choice", "none", ("values",), None, None, apply=_apply_package),
        _spec("via_transition.entry_trace.gap", "输入 P/N 铜边间距 GAP", "VIA 换层",
              minimum=0.001, maximum=2.0, deps=(_need_via_transition,)),
        _spec("via_transition.exit_trace.gap", "输出 P/N 铜边间距 GAP", "VIA 换层",
              minimum=0.001, maximum=2.0, deps=(_need_via_transition,)),
        _spec("via_transition.padstack.drill_diameter", "信号孔钻孔直径", "VIA 换层",
              minimum=0.05, maximum=1.0, deps=(_need_via_transition,), apply=_apply_via_signal_drill),
        _spec("via_transition.padstack.connected_pad_diameter", "信号孔功能焊盘直径", "VIA 换层",
              minimum=0.1, maximum=2.0, deps=(_need_via_transition,)),
        _spec("via_transition.padstack.antipad.diameter", "信号孔圆形反焊盘直径", "反焊盘与叠层",
              minimum=0.2, maximum=3.0, deps=(_need_via_circle_antipad, _need_manual_antipad_size),
              apply=_apply_via_antipad_diameter),
        _spec("via_transition.padstack.antipad_diameter", "兼容：信号孔圆形反焊盘直径", "反焊盘与叠层",
              minimum=0.2, maximum=3.0, deps=(_need_via_circle_antipad, _need_manual_antipad_size),
              apply=_apply_via_antipad_diameter),
        _spec("via_transition.padstack.antipad.length", "非圆反焊盘长度", "反焊盘与叠层",
              minimum=0.2, maximum=5.0, deps=(_need_via_non_circle_antipad, _need_manual_antipad_size)),
        _spec("via_transition.padstack.antipad.width", "非圆反焊盘宽度", "反焊盘与叠层",
              minimum=0.2, maximum=5.0, deps=(_need_via_non_circle_antipad, _need_manual_antipad_size)),
        _spec("via_transition.padstack.antipad.corner_radius", "圆角矩形圆角半径", "反焊盘与叠层",
              minimum=0.0, maximum=2.5, deps=(_need_via_rounded_rectangle,)),
        _spec("via_transition.padstack.antipad.rotation_deg", "非圆反焊盘旋转角", "反焊盘与叠层",
              unit="angle", minimum=-360.0, maximum=360.0,
              deps=(_need_via_non_circle_antipad, _need_manual_antipad_rotation)),
        _spec("via_transition.padstack.antipad.clearance", "反焊盘最小铜边净距（离散校验轴）", "反焊盘与叠层",
              minimum=0.0, maximum=2.0, deps=(_need_via_transition,)),
        _spec("via_transition.padstack.antipad.end_clearance", "合并开窗端部净距", "反焊盘与叠层",
              minimum=0.0, maximum=2.0, deps=(_need_via_transition, _need_auto_antipad_size)),
        _spec("via_transition.padstack.antipad.side_clearance", "合并开窗侧向净距", "反焊盘与叠层",
              minimum=0.0, maximum=2.0, deps=(_need_via_transition, _need_auto_antipad_size)),
        _spec("via_transition.padstack.antipad.length_increment", "自动适配长度增量", "反焊盘与叠层",
              minimum=0.0, maximum=2.0, deps=(_need_via_transition, _need_auto_plus_increment)),
        _spec("via_transition.padstack.antipad.width_increment", "自动适配宽度增量", "反焊盘与叠层",
              minimum=0.0, maximum=2.0, deps=(_need_via_transition, _need_auto_plus_increment)),
    )
}


def apply_sweep_value(config: Dict[str, Any], path: str, value: Any) -> None:
    spec = SWEEP_PARAMETER_REGISTRY.get(path) or _dynamic_sweep_spec(path)
    if not spec:
        raise ValueError(f"未注册的设计扫描参数：{path}")
    error = spec.validate_value(value)
    if error:
        raise ValueError(f"{spec.label}：{error}")
    spec.apply(config, int(round(float(value))) if spec.value_type == "integer" else value)


def scan_values(path: str, sweep: Mapping[str, Any]) -> List[Any]:
    spec = SWEEP_PARAMETER_REGISTRY.get(path) or _dynamic_sweep_spec(path)
    if spec is None:
        raise KeyError(path)
    if "values" in sweep:
        values = list(sweep.get("values") or [])
    else:
        start, stop, step = float(sweep["start"]), float(sweep["stop"]), float(sweep["step"])
        if step <= 0 or stop < start:
            raise ValueError("必须满足 stop >= start 且 step > 0。")
        count = int(math.floor((stop - start) / step + 1e-9)) + 1
        values = [start + index * step for index in range(count)]
        if values and values[-1] < stop - 1e-9:
            values.append(stop)
    result: List[Any] = []
    for value in values:
        error = spec.validate_value(value)
        if error:
            raise ValueError(error)
        if spec.value_type == "integer":
            result.append(int(round(float(value))))
        elif isinstance(value, (int, float)):
            result.append(float("{:.12g}".format(float(value))))
        else:
            result.append(value)
    if not result:
        raise ValueError("离散值列表不能为空。")
    return result


# Re-declared after the legacy implementation so old serialized configs keep
# working while the new scan-mode fields gain first-class semantics.
def scan_values(path: str, sweep: Mapping[str, Any]) -> List[Any]:
    spec = SWEEP_PARAMETER_REGISTRY.get(path) or _dynamic_sweep_spec(path)
    if spec is None:
        raise KeyError(path)
    mode = str(sweep.get("scan_mode", sweep.get("mode", "")))
    if "values" in sweep or mode == "Explicit Values":
        values = list(sweep.get("values") or [])
    else:
        start, stop = float(sweep["start"]), float(sweep["stop"])
        if stop < start:
            raise ValueError("扫描范围必须满足 stop >= start。")
        if mode in {"LinearCount", "Log"}:
            count = int(sweep.get("count", sweep.get("step", 0)))
            if count < 2:
                raise ValueError("LinearCount/Log 至少需要 2 个采样点。")
            if mode == "Log" and (start <= 0 or stop <= 0):
                raise ValueError("Log 扫描的 start/stop 必须大于 0。")
            if mode == "Log":
                ratio = (stop / start) ** (1.0 / (count - 1))
                values = [start * ratio ** index for index in range(count)]
            else:
                values = [start + (stop - start) * index / (count - 1) for index in range(count)]
        else:
            step = float(sweep.get("step", 0))
            if step <= 0:
                raise ValueError("LinearStep 的 step 必须大于 0。")
            count = int(math.floor((stop - start) / step + 1e-9)) + 1
            values = [start + index * step for index in range(count)]
            if values and values[-1] < stop - 1e-9:
                values.append(stop)
    result: List[Any] = []
    for value in values:
        error = spec.validate_value(value)
        if error:
            raise ValueError(error)
        if spec.value_type == "integer":
            result.append(int(round(float(value))))
        elif isinstance(value, (int, float)):
            result.append(float("{:.12g}".format(float(value))))
        else:
            result.append(value)
    if not result:
        raise ValueError("扫描值列表不能为空。")
    return result


def get_sweep_parameter_spec(path: str) -> Optional[SweepParameterSpec]:
    """Return a registered or user-named stackup sweep specification."""
    return SWEEP_PARAMETER_REGISTRY.get(path) or _dynamic_sweep_spec(path)


for _layer_name in (
    "DIEL_L1_L2", "DIEL_L2_L3", "DIEL_L3_L4", "DIEL_L4_L5",
    "DIEL_L5_L6", "DIEL_L6_L7", "DIEL_L7_L8", "DIEL_L8_L9",
    "DIEL_L9_L10", "DIEL_L10_L11", "DIEL_L11_L12",
    "DIEL_L12_L13", "DIEL_L13_L14",
):
    for _field_name in ("thickness", "dk", "df"):
        _path = "stackup.{}.{}".format(_layer_name, _field_name)
        SWEEP_PARAMETER_REGISTRY.setdefault(_path, _dynamic_stackup_spec(_path))


@dataclass(frozen=True)
class SweepPlan:
    axes: Tuple[Tuple[str, Tuple[Any, ...]], ...]
    case_count: int
    issues: Tuple[Tuple[str, str], ...]


def inspect_sweep_plan(config: Mapping[str, Any], max_cases: Optional[int] = None) -> SweepPlan:
    issues: List[Tuple[str, str]] = []
    axes: List[Tuple[str, Tuple[Any, ...]]] = []
    for path, sweep in config.get("sweeps", {}).items():
        if not isinstance(sweep, Mapping) or not sweep.get("enabled"):
            continue
        spec = SWEEP_PARAMETER_REGISTRY.get(path) or _dynamic_sweep_spec(path)
        if not spec:
            issues.append(("error", f"扫描参数 {path} 未在统一注册表中登记。"))
            continue
        for message in spec.dependency_issues(config):
            issues.append(("error", f"{spec.label}：{message}"))
        try:
            values = tuple(scan_values(path, sweep))
        except (KeyError, TypeError, ValueError) as exc:
            issues.append(("error", f"{spec.label}：{exc}"))
            continue
        axes.append((path, values))
    if config.get("baseline_comparison", {}).get("enabled") and config["baseline_comparison"].get("compare_gnd_vias"):
        axes.append(("gnd_vias.enabled", (False, True)))
    case_count = math.prod(len(values) for _, values in axes) if axes else 1
    limit = int(max_cases or config.get("sweep_execution", {}).get("max_cases", 200))
    if case_count > limit:
        issues.append(("error", f"全因子扫描将生成 {case_count} 个案例，超过安全上限 {limit}。"))
    if case_count > 100 and case_count <= limit:
        issues.append(("warning", f"案例数为 {case_count}，生成和求解工作量较大。"))
    return SweepPlan(tuple(axes), case_count, tuple(issues))


# Topology choices are deliberately registered separately from native numeric
# variables.  They are valid scan inputs, but must be materialized as Designs
# inside the same AEDT Project rather than inserted into Optimetrics.
SWEEP_PARAMETER_REGISTRY.update({
    "trace.trace_width": _spec("trace.trace_width", "走线宽度", "项目与走线"),
    "trace.trace_pitch": _spec("trace.trace_pitch", "差分走线中心距", "项目与走线"),
    "trace.transition_length": _spec("trace.transition_length", "S弯过渡长度", "项目与走线"),
    "capacitor.pad_rotation": _spec("capacitor.pad_rotation", "PAD旋转角", "电容与模型",
                                      "float", "angle", minimum=-89.0, maximum=89.0),
    "capacitor_model.capacitance_pf": _spec("capacitor_model.capacitance_pf", "电容值", "电容与模型", "float", "none", ("range", "values"), minimum=0.0),
    "capacitor_model.esr_ohm": _spec("capacitor_model.esr_ohm", "ESR", "电容与模型", "float", "none", ("range", "values"), minimum=0.0),
    "capacitor_model.esl_nh": _spec("capacitor_model.esl_nh", "ESL", "电容与模型", "float", "none", ("range", "values"), minimum=0.0),
    "gnd_vias.enabled": _spec("gnd_vias.enabled", "GND VIA 启用", "GND VIA", "choice", "none", ("values",), apply=lambda c, v: _nested_set(c, "gnd_vias.enabled", bool(v))),
    "gnd_vias.pattern": _spec("gnd_vias.pattern", "GND VIA 拓扑", "GND VIA", "choice", "none", ("values",)),
    "antipad.mode": _spec("antipad.mode", "反焊盘合并模式", "反焊盘与叠层", "choice", "none", ("values",)),
    "capacitor_model.mode": _spec("capacitor_model.mode", "电容模型模式", "电容与模型", "choice", "none", ("values",)),
    "project.solution_type": _spec("project.solution_type", "HFSS 求解类型", "仿真与扫描", "choice", "none", ("values",)),
    "scenario.type": _spec("scenario.type", "场景类型", "VIA 换层", "choice", "none", ("values",)),
    "via_transition.entry_layer": _spec("via_transition.entry_layer", "输入层", "VIA 换层", "choice", "none", ("values",)),
    "via_transition.exit_layer": _spec("via_transition.exit_layer", "输出层", "VIA 换层", "choice", "none", ("values",)),
    "via_transition.drill_start_layer": _spec("via_transition.drill_start_layer", "机械钻孔起始层", "VIA 换层", "choice", "none", ("values",)),
    "via_transition.drill_end_layer": _spec("via_transition.drill_end_layer", "机械钻孔终止层", "VIA 换层", "choice", "none", ("values",)),
    "via_transition.padstack.nonfunctional_pad_mode": _spec("via_transition.padstack.nonfunctional_pad_mode", "非功能焊盘模式", "VIA 换层", "choice", "none", ("values",)),
    "via_transition.backdrill.enabled": _spec("via_transition.backdrill.enabled", "背钻启用", "VIA 换层", "choice", "none", ("values",)),
    "via_transition.backdrill.from_side": _spec("via_transition.backdrill.from_side", "背钻方向", "VIA 换层", "choice", "none", ("values",)),
    "via_transition.return_vias.enabled": _spec("via_transition.return_vias.enabled", "回流孔启用", "VIA 换层", "choice", "none", ("values",)),
    "via_transition.return_vias.pattern": _spec("via_transition.return_vias.pattern", "回流孔布局", "VIA 换层", "choice", "none", ("values",)),
    "via_transition.return_vias.count": _spec("via_transition.return_vias.count", "回流孔数量", "VIA 换层", "integer", "count", ("values",), minimum=0),
    "via_transition.padstack.antipad.topology": _spec(
        "via_transition.padstack.antipad.topology", "P/N 开窗拓扑", "反焊盘与叠层",
        "choice", "none", ("values",), deps=(_need_via_transition,)),
    "via_transition.padstack.antipad.shape": _spec(
        "via_transition.padstack.antipad.shape", "信号孔反焊盘形状", "反焊盘与叠层",
        "choice", "none", ("values",), deps=(_need_via_transition,)),
})


@dataclass(frozen=True)
class SweepPlan:
    axes: Tuple[Tuple[str, Tuple[Any, ...]], ...]
    case_count: int
    issues: Tuple[Tuple[str, str], ...]
    strategy: str = "hybrid"
    variation_mode: str = "full_factorial"
    native_axes: Tuple[Tuple[str, Tuple[Any, ...]], ...] = ()
    topology_axes: Tuple[Tuple[str, Tuple[Any, ...]], ...] = ()
    design_count: int = 1
    optimetrics_setup_count: int = 0


def _combination_count(axes: Sequence[Tuple[str, Sequence[Any]]], mode: str,
                       custom: Optional[Sequence[Mapping[str, Any]]] = None) -> int:
    if not axes:
        return len(custom or ()) if mode == "custom" and custom else 1
    if mode == "synchronous":
        return len(axes[0][1])
    if mode == "one_at_a_time":
        return 1 + sum(max(0, len(values) - 1) for _, values in axes)
    if mode == "custom":
        return len(custom or ())
    return math.prod(len(values) for _, values in axes)


def inspect_sweep_plan(config: Mapping[str, Any], max_cases: Optional[int] = None) -> SweepPlan:
    issues: List[Tuple[str, str]] = []
    axes: List[Tuple[str, Tuple[Any, ...]]] = []
    native_axes: List[Tuple[str, Tuple[Any, ...]]] = []
    topology_axes: List[Tuple[str, Tuple[Any, ...]]] = []
    for path, sweep in config.get("sweeps", {}).items():
        if not isinstance(sweep, Mapping) or not sweep.get("enabled"):
            continue
        spec = SWEEP_PARAMETER_REGISTRY.get(path) or _dynamic_sweep_spec(path)
        if not spec:
            issues.append(("error", f"扫描参数 {path} 未在统一注册表中登记。"))
            continue
        for message in spec.dependency_issues(config):
            issues.append(("error", f"{spec.label}：{message}"))
        try:
            values = tuple(scan_values(path, sweep))
        except (KeyError, TypeError, ValueError) as exc:
            issues.append(("error", f"{spec.label}：{exc}"))
            continue
        axes.append((path, values))
        (topology_axes if is_topology_parameter(path) else native_axes).append((path, values))
    if config.get("baseline_comparison", {}).get("enabled") and config["baseline_comparison"].get("compare_gnd_vias"):
        axis = ("gnd_vias.enabled", (False, True))
        axes.append(axis)
        topology_axes.append(axis)

    strategy = normalize_execution_strategy(config)
    variation_mode = normalize_variation_mode(config)
    custom = config.get("sweep_execution", {}).get("custom_variations", [])
    native_count = _combination_count(native_axes, variation_mode, custom)
    design_count = math.prod(len(values) for _, values in topology_axes) if topology_axes else 1
    total = native_count * design_count
    limit = int(max_cases or config.get("sweep_execution", {}).get("max_cases", 200))
    if total > limit:
        issues.append(("error", f"扫描总 Variation 数为 {total}，超过安全上限 {limit}。"))
    if strategy == "native_optimetrics" and topology_axes:
        issues.append(("error", "当前范围包含拓扑参数；它不能放入原生 Optimetrics，请改用 hybrid/topology_variants。"))
    if strategy == "topology_variants" and not topology_axes:
        issues.append(("warning", "已选择 topology_variants，但当前没有拓扑参数；将只创建一个 Design。"))
    if topology_axes:
        issues.append(("warning", "扫描范围包含拓扑/逐层几何参数；将生成重新计算的"
                                   "独立 HFSS Design/case，不会伪装成单 Design 的 Optimetrics 轴。"))
    capacitor_mode = str(config.get("capacitor_model", {}).get("mode", "ideal_bridge"))
    moving_pad_paths = {
        "capacitor.cap_pitch", "capacitor.pad_length", "capacitor.pad_width",
        "capacitor.pad_gap", "capacitor.pad_rotation",
    }
    unsafe_model_axes = sorted(
        path for path, _values in native_axes
        if path in moving_pad_paths and capacitor_mode in {"lumped_rlc", "sparameter"}
    )
    if unsafe_model_axes:
        issues.append((
            "error",
            "非理想电容模式的扫描会移动PAD/RLC或网络Sheet端点，当前AEDT 2022 R2边界端点不能可靠随"
            "Optimetrics重绑定；请拆分为独立Design/案例后逐点Validate：" + ", ".join(unsafe_model_axes),
        ))
    cap_values = next((tuple(float(v) for v in values) for path, values in native_axes
                       if path == "capacitor.cap_pitch"),
                      (float(config.get("capacitor", {}).get("cap_pitch", 0.0)),))
    trace_values = next((tuple(float(v) for v in values) for path, values in native_axes
                         if path == "trace.trace_pitch"),
                        (float(config.get("trace", {}).get("trace_pitch", 0.0)),))
    # Equality is a first-class, legal geometry.  The shared path keeps a
    # fixed non-zero collinear route vertex so native expressions never emit
    # 0/0 normals while a sweep contracts, equals, or expands.
    if total > 100 and total <= limit:
        issues.append(("warning", f"扫描总 Variation 数为 {total}，预计求解工作量较大。"))
    return SweepPlan(
        tuple(axes), native_count, tuple(issues), strategy, variation_mode,
        tuple(native_axes), tuple(topology_axes), design_count,
        1 if native_axes and strategy != "topology_variants" else 0,
    )


def iter_variations(plan: SweepPlan, config: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Return preview variations without creating files or AEDT sessions."""

    axes = list(plan.native_axes)
    mode = plan.variation_mode
    if not axes:
        if mode == "custom":
            return [dict(item) for item in config.get("sweep_execution", {}).get("custom_variations", [])]
        return [dict()]
    if mode == "synchronous":
        lengths = {len(values) for _, values in axes}
        if len(lengths) != 1:
            raise ValueError("同步扫描要求所有参数具有相同的 Variation 数量。")
        return [dict((path, values[index]) for path, values in axes) for index in range(next(iter(lengths)))]
    if mode == "one_at_a_time":
        nominal = {path: values[0] for path, values in axes}
        result = [dict(nominal)]
        for path, values in axes:
            for value in values[1:]:
                item = dict(nominal)
                item[path] = value
                result.append(item)
        return result
    if mode == "custom":
        custom = config.get("sweep_execution", {}).get("custom_variations", [])
        return [dict(item) for item in custom]
    import itertools
    return [dict(zip((path for path, _ in axes), combination))
            for combination in itertools.product(*(values for _, values in axes))]


def iter_materialized_variations(plan: SweepPlan,
                                 config: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Expand native Variations together with every discrete geometry case."""

    native = iter_variations(plan, config)
    topology_axes = list(plan.topology_axes)
    if not topology_axes:
        return native
    import itertools
    topology = [
        dict(zip((path for path, _values in topology_axes), combination))
        for combination in itertools.product(*(values for _path, values in topology_axes))
    ]
    return [{**topology_case, **native_case}
            for topology_case in topology for native_case in native]


@dataclass(frozen=True)
class SweepPreflight:
    sampled_variations: int
    errors: Tuple[str, ...]
    warnings: Tuple[str, ...]
    topology_changes: Tuple[str, ...]


def preflight_sweep_geometry(config: Mapping[str, Any], sample_limit: int = 200) -> SweepPreflight:
    """Recalculate selected boundary samples and report geometry risks.

    This is intentionally a local preflight.  It does not claim that every
    AEDT Boolean or port mode will converge; those remain HFSS-side checks.
    """

    normalized, _ = __import__("config_manager").normalize_config(config)
    plan = inspect_sweep_plan(normalized, sample_limit)
    errors = [message for level, message in plan.issues if level == "error"]
    warnings = [message for level, message in plan.issues if level == "warning"]
    topology_changes: List[str] = []
    try:
        variations = iter_materialized_variations(plan, normalized)[:sample_limit]
    except ValueError as exc:
        return SweepPreflight(0, tuple(errors + [str(exc)]), tuple(warnings), tuple(topology_changes))
    from geometry_calculator import GeometryCalculator
    from config_manager import validate_config
    for variation in variations:
        candidate = copy.deepcopy(normalized)
        for path, value in variation.items():
            apply_sweep_value(candidate, path, value)
        candidate, validation = validate_config(
            candidate, check_output_directory=False
        )
        errors.extend(validation.errors)
        warnings.extend(validation.warnings)
        if not validation.is_valid:
            continue
        geometry = GeometryCalculator(candidate).calculate()
        errors.extend(geometry.errors)
        warnings.extend(geometry.warnings)
        if str(candidate.get("scenario", {}).get("type", "")) == "via_transition":
            from via_transition_semantics import validate_via_transition
            for _rule_id, level, message, _path, _suggestion in validate_via_transition(
                    candidate, geometry):
                (errors if level == "error" else warnings).append(message)
        mode = str(candidate.get("capacitor_model", {}).get("mode", "ideal_bridge"))
        if mode != "ideal_bridge" and geometry.bridge_shapes:
            errors.append(f"{mode} Variation 中检测到PAD间铜桥旁路。")
        if mode == "lumped_rlc":
            for signal, sheet in geometry.capacitor_model_shapes.items():
                start = sheet.metadata.get("integration_line_start", ())
                end = sheet.metadata.get("integration_line_end", ())
                if len(start) != 3 or len(end) != 3 or math.dist(start, end) <= 1e-12:
                    errors.append(f"{signal} RLC Sheet积分线退化或端点不完整。")
    if plan.topology_axes:
        topology_changes.append("扫描范围包含对象数量、合并方式或模型轮廓变化；"
                                "这些点必须生成重新计算的独立 HFSS Design/case。")
    return SweepPreflight(len(variations), tuple(dict.fromkeys(errors)), tuple(dict.fromkeys(warnings)), tuple(topology_changes))
