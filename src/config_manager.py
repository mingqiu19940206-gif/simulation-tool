"""V2 configuration management: V1 migration, unit boundaries and validation."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from scenario_semantics import SUPPORTED_SCENARIO_TYPES
from via_antipad import canonical_antipad

def _bundled_config_path(filename: str) -> Path:
    """Locate immutable project configuration in source and frozen builds."""

    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / "config" / filename
    return Path(__file__).resolve().parents[1] / "config" / filename


DEFAULT_CONFIG_PATH = _bundled_config_path("default_config.json")
SUPPORTED_UNITS = {"mm", "mil"}
INTERNAL_LENGTH_UNIT = "mm"
DEFAULT_DISPLAY_UNIT = "mil"
SUPPORTED_LAYER_TYPES = {"signal", "plane", "dielectric", "soldermask"}
SUPPORTED_ANTIPAD_MODES = {"independent_pad", "merged_per_capacitor", "merged_all"}
SUPPORTED_TRANSITION_DIRECTIONS = {"auto", "expand", "contract"}
SUPPORTED_PAD_ESCAPE_MODES = {"direct_45", "smooth_s_bend", "lead_then_45"}
SUPPORTED_CONDUCTOR_MODES = {"copper", "pec", "custom"}
SUPPORTED_PAD_PITCH_MODES = {"auto_from_gap", "advanced_pitch"}
SUPPORTED_VIA_PATTERNS = {"four_corner", "side_pair", "rectangular_fence", "custom"}
SUPPORTED_CAP_MODELS = {"ideal_bridge", "lumped_rlc", "sparameter", "open"}
SUPPORTED_MOUNT_SIDES = {"top", "bottom"}
SUPPORTED_TRACE_JOINS = {"miter", "limited_miter", "bevel"}
SUPPORTED_PACKAGES = {"0201", "0402", "custom"}
SUPPORTED_AEDT_VERSIONS = {"2022.2", "2023.2"}
AEDT_VERSION_LABELS = {"2022.2": "2022 R2", "2023.2": "2023 R2"}
MM_PER_MIL = 0.0254
_EPS = 1.0e-9
_AEDT_SAFE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
PROVISIONAL_MATERIAL_TOKEN = "PROVISIONAL"
PROVISIONAL_MATERIAL_NOTICE = "临时仿真值，待材料数据表确认"

# Snapshot fields are deliberately kept outside the engineering configuration
# hash.  This prevents a hash from changing merely because it is written into
# the configuration that carries it, while still making the generated files
# self-describing and auditable.
SNAPSHOT_METADATA_KEY = "_generation"
SNAPSHOT_GEOMETRY_KEY = "_geometry_snapshot"
SNAPSHOT_SCHEMA_VERSION = 1


def _canonical_payload(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def _config_hash_payload(config: Mapping[str, Any]) -> Dict[str, Any]:
    payload = copy.deepcopy(dict(config))
    payload.pop(SNAPSHOT_METADATA_KEY, None)
    payload.pop(SNAPSHOT_GEOMETRY_KEY, None)
    payload.pop("_internal_units", None)
    return payload


def config_hash(config: Mapping[str, Any]) -> str:
    """Return the stable SHA-256 of one normalized engineering config."""

    return hashlib.sha256(_canonical_payload(_config_hash_payload(config)).encode("utf-8")).hexdigest()


def geometry_hash(geometry: Any) -> str:
    """Return the stable SHA-256 of the exact geometry manifest used by HFSS."""

    payload = geometry.to_dict() if hasattr(geometry, "to_dict") else geometry
    return hashlib.sha256(_canonical_payload(payload).encode("utf-8")).hexdigest()


def make_generation_snapshot(config: Mapping[str, Any], geometry: Any,
                            generation_id: Optional[str] = None) -> Dict[str, Any]:
    """Create the single immutable data contract shared by preview and HFSS.

    ``geometry`` is copied immediately.  Callers may additionally keep the
    live GeometryResult beside this dictionary for GUI rendering, but the
    serialized manifest is the only geometry input accepted by the generated
    script.
    """

    normalized, _ = normalize_config(config)
    normalized.pop(SNAPSHOT_METADATA_KEY, None)
    normalized.pop(SNAPSHOT_GEOMETRY_KEY, None)
    geometry_snapshot = copy.deepcopy(geometry.to_dict() if hasattr(geometry, "to_dict") else geometry)
    if isinstance(geometry_snapshot, dict) and "units" in geometry_snapshot:
        # Serialized coordinates are the internal geometry contract, never a
        # display/HFSS expression.  Keep the metadata aligned with the values.
        geometry_snapshot["units"] = INTERNAL_LENGTH_UNIT
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "generation_id": str(generation_id or uuid.uuid4().hex),
        "config_hash": config_hash(normalized),
        "geometry_hash": geometry_hash(geometry_snapshot),
        "config_snapshot": normalized,
        "geometry_snapshot": geometry_snapshot,
    }


def snapshot_metadata(snapshot: Mapping[str, Any]) -> Dict[str, str]:
    return {
        "generation_id": str(snapshot.get("generation_id", "")),
        "config_hash": str(snapshot.get("config_hash", "")),
        "geometry_hash": str(snapshot.get("geometry_hash", "")),
    }


def validate_generation_snapshot(snapshot: Mapping[str, Any]) -> None:
    """Fail closed when a snapshot was edited or its two hashes disagree."""

    config_snapshot = snapshot.get("config_snapshot")
    geometry_snapshot = snapshot.get("geometry_snapshot")
    metadata = snapshot_metadata(snapshot)
    if not metadata["generation_id"] or not metadata["config_hash"] or not metadata["geometry_hash"]:
        raise ConfigError("generation snapshot 缺少 generation_id/config_hash/geometry_hash")
    if not isinstance(config_snapshot, Mapping) or not isinstance(geometry_snapshot, Mapping):
        raise ConfigError("generation snapshot 缺少 config_snapshot 或 geometry_snapshot")
    actual_config = config_hash(config_snapshot)
    actual_geometry = geometry_hash(geometry_snapshot)
    if actual_config != metadata["config_hash"]:
        raise ConfigError(f"config_hash 不一致：快照={metadata['config_hash']} 实际={actual_config}")
    if actual_geometry != metadata["geometry_hash"]:
        raise ConfigError(f"geometry_hash 不一致：快照={metadata['geometry_hash']} 实际={actual_geometry}")


def materialize_snapshot_config(snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a JSON-safe config carrying the snapshot audit metadata."""

    validate_generation_snapshot(snapshot)
    config = copy.deepcopy(dict(snapshot["config_snapshot"]))
    config[SNAPSHOT_METADATA_KEY] = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        **snapshot_metadata(snapshot),
    }
    config[SNAPSHOT_GEOMETRY_KEY] = copy.deepcopy(snapshot["geometry_snapshot"])
    return config


def snapshot_from_config(config: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Rehydrate an embedded snapshot from a generated/configuration JSON."""

    metadata = config.get(SNAPSHOT_METADATA_KEY)
    geometry_snapshot = config.get(SNAPSHOT_GEOMETRY_KEY)
    if not isinstance(metadata, Mapping) or not isinstance(geometry_snapshot, Mapping):
        return None
    config_snapshot = copy.deepcopy(dict(config))
    config_snapshot.pop(SNAPSHOT_METADATA_KEY, None)
    config_snapshot.pop(SNAPSHOT_GEOMETRY_KEY, None)
    payload = {
        "schema_version": metadata.get("schema_version", SNAPSHOT_SCHEMA_VERSION),
        "generation_id": metadata.get("generation_id", ""),
        "config_hash": metadata.get("config_hash", ""),
        "geometry_hash": metadata.get("geometry_hash", ""),
        # A generated JSON is the persisted preview contract.  Its values are
        # already normalized internal values and must not pass through the
        # user-facing unit conversion path a second time.
        "config_snapshot": config_snapshot,
        "geometry_snapshot": copy.deepcopy(geometry_snapshot),
    }
    validate_generation_snapshot(payload)
    return payload

PACKAGE_PRESETS: Dict[str, Dict[str, float | str]] = {
    "0201": {"package_metric": "0603", "body_length": 0.6, "body_width": 0.3,
             "body_height": 0.3, "pad_length": 0.25, "pad_width": 0.3,
             "pad_gap": 0.2, "pad_pitch": 0.45},
    "0402": {"package_metric": "1005", "body_length": 1.0, "body_width": 0.5,
             "body_height": 0.5, "pad_length": 0.4, "pad_width": 0.5,
             "pad_gap": 0.4, "pad_pitch": 0.8},
}

_SECTION_LENGTH_FIELDS: Dict[str, set[str]] = {
    "trace": {"trace_width", "trace_pitch", "trace_gap", "copper_thickness",
              "input_straight_length", "output_straight_length",
              "pad_lead_in_length", "pad_lead_out_length", "transition_length"},
    "capacitor": {"body_length", "body_width", "body_height", "pad_length", "pad_width",
                  "pad_gap", "pad_pitch", "cap_pitch", "cap_center_x", "cap_center_x_offset",
                  "bridge_width", "bridge_length"},
    "antipad": {"global_expansion"},
    "plane": {"margin_input", "margin_output", "margin_y"},
    "geometry": {"overlap"},
    "capacitor_model": {"sheet_width"},
    "gnd_vias": {"clearance_x", "clearance_y", "pitch_x", "pitch_y", "drill_diameter",
                 "finished_hole_diameter", "plating_thickness", "pad_diameter",
                 "antipad_diameter", "top_ground_clearance"},
    "simulation": {"airbox_margin", "port_lateral_margin"},
    "via_transition": {
        "pair_pitch", "signal_via_pitch", "center_x", "center_y",
        "fanout.straight_length",
        "entry_trace.length", "entry_trace.width", "entry_trace.gap",
        "exit_trace.length", "exit_trace.width", "exit_trace.gap",
        "padstack.drill_diameter", "padstack.finished_hole_diameter",
        "padstack.plating_thickness", "padstack.connected_pad_diameter",
        "padstack.nonfunctional_pad_diameter", "padstack.antipad_diameter",
        "padstack.antipad.diameter", "padstack.antipad.length",
        "padstack.antipad.width", "padstack.antipad.corner_radius",
        "padstack.antipad.clearance", "padstack.antipad.end_clearance",
        "padstack.antipad.side_clearance", "padstack.antipad.length_increment",
        "padstack.antipad.width_increment",
        "backdrill.drill_diameter", "backdrill.residual_stub",
        "return_vias.offset_x", "return_vias.offset_y",
        "return_vias.center_to_signal",
        "return_vias.pitch_x", "return_vias.pitch_y",
        "return_vias.drill_diameter", "return_vias.finished_hole_diameter",
        "return_vias.plating_thickness", "return_vias.pad_diameter",
        "return_vias.antipad_diameter",
    },
}
_LENGTH_SWEEPS = {
    "trace.trace_width", "trace.trace_pitch", "trace.transition_length",
    "capacitor.cap_pitch", "capacitor.pad_length", "capacitor.pad_width",
    "capacitor.pad_gap", "antipad.global_expansion", "antipad.first_reference_expansion",
    "gnd_vias.clearance_x", "gnd_vias.clearance_y", "gnd_vias.drill_diameter",
    "gnd_vias.pad_diameter", "gnd_vias.antipad_diameter",
    "via_transition.padstack.antipad.diameter",
    "via_transition.padstack.antipad.length",
    "via_transition.padstack.antipad.width",
    "via_transition.padstack.antipad.corner_radius",
    "via_transition.padstack.antipad.clearance",
    "via_transition.padstack.antipad.end_clearance",
    "via_transition.padstack.antipad.side_clearance",
    "via_transition.padstack.antipad.length_increment",
    "via_transition.padstack.antipad.width_increment",
    "via_transition.padstack.antipad_diameter",
}
# VIA padstack drill/pad, pair pitch, residual stub and Return VIA coordinates
# are length fields.  They must follow the file ``units`` on save/load the
# same way capacitor/GND and antipad diameter sweeps already do.  Nested
# via_transition length keys live in ``_SECTION_LENGTH_FIELDS``.
_LENGTH_SWEEPS.update(
    "via_transition." + field
    for field in _SECTION_LENGTH_FIELDS["via_transition"]
)


class ConfigError(ValueError):
    pass


@dataclass
class ValidationResult:
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    corrections: List[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.errors

    def add_error(self, message: str) -> None:
        if message not in self.errors:
            self.errors.append(message)

    def add_warning(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def add_correction(self, message: str) -> None:
        if message not in self.corrections:
            self.corrections.append(message)

    def extend_corrections(self, messages: Iterable[str]) -> None:
        for message in messages:
            self.add_correction(message)


def get_default_config() -> Dict[str, Any]:
    try:
        with DEFAULT_CONFIG_PATH.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"无法读取默认配置 {DEFAULT_CONFIG_PATH}：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("默认配置根节点必须是 JSON 对象。")
    return copy.deepcopy(data)


def _layer_value(layer: Any, name: str, default: Any = None) -> Any:
    """Read one stackup field from either a config mapping or geometry layer."""

    if isinstance(layer, Mapping):
        if name == "type":
            return layer.get("type", layer.get("layer_type", default))
        return layer.get(name, default)
    if name == "type":
        return getattr(layer, "layer_type", getattr(layer, "type", default))
    return getattr(layer, name, default)


def find_surface_signal_layer(stackup: Sequence[Any], mount_side: str) -> Optional[Any]:
    """Return the exterior signal copper selected by ``mount_side``.

    Stackup order is always top-to-bottom.  This helper deliberately accepts
    both JSON layer mappings and ``LayerZPosition`` instances so config, GUI,
    geometry and validation cannot drift onto separate selection algorithms.
    """

    if mount_side not in SUPPORTED_MOUNT_SIDES:
        return None
    signals = [layer for layer in stackup if _layer_value(layer, "type") == "signal"]
    if not signals:
        return None
    return signals[0] if mount_side == "top" else signals[-1]


def find_nearest_reference_plane(stackup: Sequence[Any], mount_side: str) -> Optional[Any]:
    """Find the nearest valid reference plane inward from the mounted surface."""

    layers = list(stackup)
    surface = find_surface_signal_layer(layers, mount_side)
    if surface is None:
        return None
    surface_index = next((index for index, layer in enumerate(layers) if layer is surface), None)
    if surface_index is None:
        return None
    indices = (range(surface_index + 1, len(layers)) if mount_side == "top"
               else range(surface_index - 1, -1, -1))
    for index in indices:
        layer = layers[index]
        if (_layer_value(layer, "type") == "plane"
                and bool(_layer_value(layer, "is_reference_plane", False))):
            return layer
    return None


def synchronize_antipad_mount_side(
    config: Dict[str, Any], *, migrate_legacy: bool = False,
) -> List[str]:
    """Apply provenance-aware antipad following and return migration messages.

    ``auto_follow_layer`` records only the layer owned by the automatic
    linkage.  On a side change that one old layer is disabled; every other
    enabled reference layer is treated as a manual user choice and retained.
    """

    messages: List[str] = []
    antipad = config.setdefault("antipad", {})
    if not bool(antipad.get("follow_mount_side", True)):
        if str(antipad.get("auto_follow_layer", "")).strip():
            antipad["auto_follow_layer"] = ""
            messages.append("反焊盘安装面自动跟随已关闭；手工 enable_antipad 设置保持不变。")
        return messages

    stackup = config.get("stackup", [])
    mount_side = str(config.get("capacitor", {}).get("mount_side", "top"))
    target = find_nearest_reference_plane(stackup, mount_side)
    previous_name = str(antipad.get("auto_follow_layer", "")).strip()
    target_name = str(_layer_value(target, "name", "")) if target is not None else ""

    enabled = [layer for layer in stackup if bool(_layer_value(layer, "enable_antipad", False))]
    if migrate_legacy and target_name:
        # Old V2 files had no ownership marker.  A lone top-nearest layer in a
        # bottom configuration is the known stale default and can be migrated
        # safely.  Multi-layer selections are preserved verbatim, then the new
        # target is added, so an old manual selection is never silently lost.
        top_reference = find_nearest_reference_plane(stackup, "top")
        top_name = str(_layer_value(top_reference, "name", "")) if top_reference is not None else ""
        if (mount_side == "bottom" and len(enabled) == 1
                and str(_layer_value(enabled[0], "name", "")) == top_name
                and top_name != target_name):
            enabled[0]["enable_antipad"] = False
            messages.append(
                f"旧 Bottom 配置中仅启用的顶侧默认反焊盘层 {top_name} 已迁移为 {target_name}。"
            )
    elif previous_name and previous_name != target_name:
        previous = next(
            (layer for layer in stackup
             if str(_layer_value(layer, "name", "")) == previous_name),
            None,
        )
        if (previous is not None and _layer_value(previous, "type") == "plane"
                and bool(_layer_value(previous, "is_reference_plane", False))
                and bool(_layer_value(previous, "enable_antipad", False))):
            previous["enable_antipad"] = False
            messages.append(f"已关闭旧安装面自动选择的反焊盘层 {previous_name}。")

    if target is None:
        antipad["auto_follow_layer"] = ""
        return messages
    if not bool(_layer_value(target, "enable_antipad", False)):
        target["enable_antipad"] = True
        messages.append(f"已启用{mount_side}安装面最近参考层 {target_name} 的反焊盘。")
    antipad["auto_follow_layer"] = target_name
    return messages


def convert_length(value: float, from_unit: str, to_unit: str) -> float:
    if from_unit not in SUPPORTED_UNITS or to_unit not in SUPPORTED_UNITS:
        raise ConfigError(f"不支持的长度单位：{from_unit} -> {to_unit}")
    mm = float(value) if from_unit == "mm" else float(value) * MM_PER_MIL
    return mm if to_unit == "mm" else mm / MM_PER_MIL


def format_number(value: float, max_decimals: int) -> str:
    """Format an editor value compactly without changing stored precision."""
    number = float(value)
    text = f"{number:.{max_decimals}f}".rstrip("0").rstrip(".")
    if text in {"", "-0"}:
        return "0"
    if number and float(text) == 0.0:
        return f"{number:.{max_decimals}g}"
    return text


def format_length(mm_value: float, unit: str, significant: Optional[int] = None) -> str:
    """Format a physical length for the GUI; internal/JSON values stay full precision."""
    value = convert_length(mm_value, "mm", unit)
    if significant is not None:
        return f"{value:.{significant}g}"
    # Sidebar, preview labels and derived length copy use 1–2 decimals so
    # mil conversions do not show artifacts like 31.4961 / 27.5591.
    return format_number(value, 2)


def normalize_aedt_version(value: Any) -> str:
    """Return the canonical PyAEDT release string for supported AEDT aliases."""
    raw = "" if value is None else str(value).strip()
    if not raw:
        return "2022.2"
    key = re.sub(r"[\s._-]+", "", raw).lower()
    aliases = {
        "20222": "2022.2", "2022r2": "2022.2", "22r2": "2022.2", "222": "2022.2",
        "20232": "2023.2", "2023r2": "2023.2", "23r2": "2023.2", "232": "2023.2",
    }
    return aliases.get(key, raw)


def derive_capacitor_values(capacitor: Mapping[str, Any], overlap: float) -> Dict[str, float]:
    """Return the authoritative automatically-derived capacitor geometry values in mm."""
    if capacitor.get("pad_pitch_mode") == "advanced_pitch":
        pad_gap = float(capacitor["pad_pitch"]) - float(capacitor["pad_length"])
        pad_pitch = float(capacitor["pad_pitch"])
    else:
        pad_gap = float(capacitor["pad_gap"])
        pad_pitch = float(capacitor["pad_length"]) + pad_gap
    bridge_length = (pad_gap + 2.0 * float(overlap)
                     if capacitor.get("bridge_length_mode") == "auto"
                     else float(capacitor["bridge_length"]))
    return {"pad_gap": pad_gap, "pad_pitch": pad_pitch, "bridge_length": bridge_length}


def via_trace_center_pitch(trace: Mapping[str, Any], pair_pitch: float) -> float:
    """VIA 走线中心距 = width + GAP；缺 GAP 时回退到旧 pair_pitch。"""
    width = float(trace.get("width", 0.0) or 0.0)
    if "gap" in trace and trace["gap"] not in (None, ""):
        return width + float(trace["gap"])
    return float(pair_pitch)


def apply_pair_pitch_to_trace_gaps(via_transition: Dict[str, Any], pair_pitch: float) -> None:
    """Legacy both-end pitch setter: pair_pitch becomes width+GAP on both traces."""
    pitch = float(pair_pitch)
    via_transition["pair_pitch"] = pitch
    for side in ("entry_trace", "exit_trace"):
        trace = via_transition.setdefault(side, {})
        width = float(trace.get("width", 0.0) or 0.0)
        trace["gap"] = pitch - width


def is_side_pair_count2(return_vias: Mapping[str, Any]) -> bool:
    """MVP Return VIA editor: default 两侧成对 with exactly two holes."""
    try:
        count = int(return_vias.get("count", 0) or 0)
    except (TypeError, ValueError):
        count = 0
    return str(return_vias.get("pattern", "side_pair")) == "side_pair" and count == 2


def side_pair_return_offset_y(signal_via_pitch: float, center_to_signal: float) -> float:
    """side_pair count=2: Y offset from pair center = 孔间距/2 + 相对信号孔中心距."""
    return abs(float(signal_via_pitch)) / 2.0 + float(center_to_signal)


def side_pair_center_to_signal(offset_y: float, signal_via_pitch: float) -> float:
    """Inverse of side_pair_return_offset_y: |Y偏移| − 孔间距/2."""
    return float(offset_y) - abs(float(signal_via_pitch)) / 2.0


def sync_side_pair_return_via_y(via_transition: Dict[str, Any]) -> None:
    """Keep offset_y derived from center_to_signal for side_pair count=2."""
    rv = via_transition.get("return_vias")
    if not isinstance(rv, dict) or not is_side_pair_count2(rv):
        return
    if rv.get("center_to_signal") in (None, ""):
        return
    pitch = float(via_transition.get("signal_via_pitch", 0.0) or 0.0)
    rv["offset_y"] = side_pair_return_offset_y(pitch, rv["center_to_signal"])


def _deep_merge(defaults: Any, supplied: Any) -> Any:
    if isinstance(defaults, Mapping) and isinstance(supplied, Mapping):
        merged = copy.deepcopy(dict(defaults))
        for key, value in supplied.items():
            merged[key] = _deep_merge(merged[key], value) if key in merged else copy.deepcopy(value)
        return merged
    return copy.deepcopy(supplied)


def _number(value: Any, name: str, integer: bool = False) -> float | int:
    if isinstance(value, bool):
        raise ConfigError(f"字段 {name} 需要数值。")
    try:
        result = float(str(value).strip()) if isinstance(value, str) else float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"字段 {name} 需要数值，当前为 {value!r}。") from exc
    if not math.isfinite(result):
        raise ConfigError(f"字段 {name} 必须为有限数值。")
    if integer:
        if not result.is_integer():
            raise ConfigError(f"字段 {name} 需要整数。")
        return int(result)
    return result


def _boolean(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on", "是"}:
            return True
        if lowered in {"false", "0", "no", "off", "否"}:
            return False
    raise ConfigError(f"字段 {name} 需要布尔值。")


def _coerce(value: Any, template: Any, name: str) -> Any:
    if template is None:
        return None if value in (None, "") else copy.deepcopy(value)
    if isinstance(template, bool):
        return _boolean(value, name)
    if isinstance(template, int) and not isinstance(template, bool):
        return _number(value, name, True)
    if isinstance(template, float):
        return _number(value, name)
    if isinstance(template, str):
        return "" if value is None else str(value).strip()
    if isinstance(template, Mapping):
        if not isinstance(value, Mapping):
            raise ConfigError(f"字段 {name} 必须是 JSON 对象。")
        return {key: _coerce(child, template[key], f"{name}.{key}" if name else key)
                if key in template else copy.deepcopy(child) for key, child in value.items()}
    if isinstance(template, list):
        if not isinstance(value, list):
            raise ConfigError(f"字段 {name} 必须是 JSON 数组。")
        if not template:
            return copy.deepcopy(value)
        return [_coerce(item, template[0], f"{name}[{index}]") for index, item in enumerate(value)]
    return copy.deepcopy(value)


def _scale_nested_length(node: Any, field_path: str, factor: float) -> None:
    """Scale one length field, where the path may use dots for nested sections."""
    parts = field_path.split(".")
    target = node
    for part in parts[:-1]:
        if not isinstance(target, dict):
            return
        target = target.get(part)
        if not isinstance(target, dict):
            return
    if not isinstance(target, dict):
        return
    last = parts[-1]
    if target.get(last) not in (None, ""):
        target[last] = float(target[last]) * factor


def _parse_legacy_coordinate_string(value: str, path: str) -> List[List[float]]:
    """Accept the pre-table ``x,y; x,y`` GUI representation on load only."""

    points: List[List[float]] = []
    for index, item in enumerate(str(value).split(";")):
        if not item.strip():
            continue
        parts = [part.strip() for part in item.split(",")]
        if len(parts) != 2:
            raise ConfigError(f"{path} 第 {index + 1} 行必须包含 x、y 两个数。")
        try:
            points.append([float(parts[0]), float(parts[1])])
        except ValueError as exc:
            raise ConfigError(f"{path} 第 {index + 1} 行包含非数字坐标。") from exc
    return points


def _scale_explicit_lengths(data: Mapping[str, Any], factor: float) -> Dict[str, Any]:
    result = copy.deepcopy(dict(data))
    for section, fields in _SECTION_LENGTH_FIELDS.items():
        node = result.get(section)
        if isinstance(node, dict):
            for field_name in fields:
                _scale_nested_length(node, field_name, factor)
    for layer in result.get("stackup", []) if isinstance(result.get("stackup"), list) else []:
        for key in ("thickness", "antipad_expansion_override"):
            if layer.get(key) not in (None, ""):
                layer[key] = float(layer[key]) * factor
    vias = result.get("gnd_vias")
    if isinstance(vias, dict) and isinstance(vias.get("custom_coordinates"), list):
        vias["custom_coordinates"] = [[float(point[0]) * factor, float(point[1]) * factor]
                                      for point in vias["custom_coordinates"]]
    via_transition = result.get("via_transition")
    if isinstance(via_transition, dict):
        padstack = via_transition.get("padstack")
        if isinstance(padstack, dict) and isinstance(padstack.get("antipad_per_layer"), dict):
            padstack["antipad_per_layer"] = {
                key: (float(value) * factor if value not in (None, "") else value)
                for key, value in padstack["antipad_per_layer"].items()
            }
        canonical = padstack.get("antipad") if isinstance(padstack, dict) else None
        if isinstance(canonical, dict) and isinstance(canonical.get("per_layer"), dict):
            for override in canonical["per_layer"].values():
                if not isinstance(override, dict):
                    continue
                for key in ("diameter", "length", "width", "corner_radius", "clearance",
                            "end_clearance", "side_clearance", "length_increment",
                            "width_increment"):
                    if override.get(key) not in (None, ""):
                        override[key] = float(override[key]) * factor
        return_vias = via_transition.get("return_vias")
        if isinstance(return_vias, dict) and isinstance(return_vias.get("custom_coordinates"), list):
            return_vias["custom_coordinates"] = [
                [float(point[0]) * factor, float(point[1]) * factor]
                for point in return_vias["custom_coordinates"]
            ]
    sweeps = result.get("sweeps")
    if isinstance(sweeps, dict):
        for name, sweep in sweeps.items():
            parts = str(name).split(".")
            per_layer_length = (
                len(parts) == 6
                and parts[:4] == ["via_transition", "padstack", "antipad", "per_layer"]
                and parts[5] in {"diameter", "length", "width", "corner_radius", "clearance",
                                 "end_clearance", "side_clearance", "length_increment",
                                 "width_increment"}
            )
            stackup_thickness = (
                len(parts) == 3 and parts[0] == "stackup" and parts[2] == "thickness"
            )
            if name not in _LENGTH_SWEEPS and not per_layer_length and not stackup_thickness:
                continue
            if isinstance(sweep, dict):
                for key in ("start", "stop", "step"):
                    if sweep.get(key) not in (None, ""):
                        sweep[key] = float(sweep[key]) * factor
                if isinstance(sweep.get("values"), list):
                    sweep["values"] = [float(value) * factor for value in sweep["values"]]
    return result


def migrate_v1_config(raw: Mapping[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    migrated = copy.deepcopy(dict(raw))
    messages: List[str] = []
    try:
        version = int(migrated.get("schema_version", 1))
    except (TypeError, ValueError):
        version = 1
    unit = str(migrated.get("units", "mm")).lower()
    if unit not in SUPPORTED_UNITS:
        migrated["units"] = "mm"
        messages.append("旧配置缺少有效 units，已按 mm 解释。")
    if version < 2:
        migrated["schema_version"] = 2
        messages.append("配置已从 schema_version=1 迁移到 2；新增项使用 V2 安全默认值。")
    return migrated, messages


def migrate_v2_to_v3(migrated: Dict[str, Any], messages: List[str]) -> Dict[str, Any]:
    """Introduce ``scenario.type`` without changing any physical dimension.

    v2 payloads have no scenario concept, so the migrated value is always the
    existing AC-coupled capacitor scenario.  The ``via_transition`` block is
    filled from the v3 defaults during normalization, never invented here.
    """

    scenario = migrated.get("scenario")
    if not isinstance(scenario, Mapping):
        scenario = {}
        migrated["scenario"] = scenario
    if not scenario.get("type"):
        scenario["type"] = "surface_capacitor"
    migrated["schema_version"] = 3
    messages.append(
        "配置已从 schema v2 迁移到 v3：scenario.type 已补为 surface_capacitor，"
        "via_transition 使用安全默认值；原有电容、GND VIA、端口、仿真与扫描值不变。"
    )
    return migrated


def normalize_config(raw_config: Mapping[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    if not isinstance(raw_config, Mapping):
        raise ConfigError("配置根节点必须是 JSON 对象。")
    defaults = get_default_config()
    try:
        source_schema_version = int(raw_config.get("schema_version", 1))
    except (TypeError, ValueError):
        source_schema_version = 1
    migrated, corrections = migrate_v1_config(raw_config)
    if int(migrated.get("schema_version", 1)) < 3:
        migrated = migrate_v2_to_v3(migrated, corrections)
    raw_capacitor = migrated.get("capacitor") if isinstance(migrated.get("capacitor"), Mapping) else {}
    if "mount_side" not in raw_capacitor:
        corrections.append("旧配置缺少 capacitor.mount_side，已按 top（顶层安装）处理。")
    raw_trace = migrated.get("trace") if isinstance(migrated.get("trace"), Mapping) else {}
    if "pad_escape_mode" not in raw_trace:
        migrated.setdefault("trace", {})["pad_escape_mode"] = "direct_45"
        corrections.append(
            "旧配置缺少 trace.pad_escape_mode，已迁移为 direct_45；"
            "原 pad_lead_in_length/pad_lead_out_length 仅在 lead_then_45 中生效。"
        )
    # ``transition_mode=strict_45deg`` was the old, unconditional route
    # selector.  A single source of truth avoids an apparently editable field
    # being silently ignored by preview or HFSS.
    migrated.setdefault("trace", {}).pop("transition_mode", None)
    raw_via_transition = migrated.get("via_transition")
    if isinstance(raw_via_transition, dict):
        raw_return_vias = raw_via_transition.get("return_vias")
        if (isinstance(raw_return_vias, dict)
                and isinstance(raw_return_vias.get("custom_coordinates"), str)):
            raw_return_vias["custom_coordinates"] = _parse_legacy_coordinate_string(
                raw_return_vias["custom_coordinates"],
                "via_transition.return_vias.custom_coordinates",
            )
            corrections.append(
                "旧版 Return VIA 坐标字符串已转换为 custom_coordinates 数组；保存字段语义不变。"
            )
    units = str(migrated.get("units", "mm")).lower()
    def shown_length(mm_value: float) -> str:
        return f"{format_length(mm_value, units)} {units}"
    already_mm = migrated.get("_internal_units") == "mm"
    explicit_mm = copy.deepcopy(migrated) if already_mm else _scale_explicit_lengths(
        migrated, 1.0 if units == "mm" else MM_PER_MIL
    )
    # schema v3 is intentionally extensible at nested engineering objects.  The
    # canonical VIA antipad object is therefore an in-schema migration: old
    # diameter aliases are accepted only on input and immediately converted to
    # the one authoritative topology/shape mapping.
    explicit_vt = (explicit_mm.get("via_transition")
                   if isinstance(explicit_mm.get("via_transition"), Mapping) else {})
    explicit_padstack = (explicit_vt.get("padstack")
                         if isinstance(explicit_vt.get("padstack"), Mapping) else {})
    if explicit_padstack:
        if isinstance(explicit_padstack.get("antipad"), Mapping):
            raw_direction_mode = explicit_padstack["antipad"].get("direction_mode")
            explicit_padstack["antipad"] = canonical_antipad(explicit_padstack["antipad"])
            if raw_direction_mode in (None, ""):
                corrections.append(
                    "旧配置缺少 antipad.direction_mode，已迁移为 auto_pn（自动沿实际 P/N 中心轴）；"
                    "原 rotation_deg 仍保留，切换 manual 后继续使用。"
                )
            # A legacy-only layer key cannot conflict with the canonical map;
            # import it once so configs assembled by older API clients remain
            # lossless.  When both representations name the same layer, the
            # canonical value wins.
            for layer_name, diameter in explicit_padstack.get("antipad_per_layer", {}).items():
                if str(layer_name) not in explicit_padstack["antipad"]["per_layer"]:
                    explicit_padstack["antipad"]["per_layer"][str(layer_name)] = {
                        "enabled": True, "inherit_global": False,
                        "topology": "independent", "shape": "circle",
                        "diameter": float(diameter),
                    }
        elif "antipad_diameter" in explicit_padstack or "antipad_per_layer" in explicit_padstack:
            migrated_antipad = canonical_antipad({
                "topology": "independent", "shape": "circle",
                "diameter": explicit_padstack.get("antipad_diameter", 0.6),
            })
            for layer_name, diameter in explicit_padstack.get("antipad_per_layer", {}).items():
                migrated_antipad["per_layer"][str(layer_name)] = {
                    "enabled": True, "inherit_global": False,
                    "topology": "independent", "shape": "circle",
                    "diameter": float(diameter),
                }
            explicit_padstack["antipad"] = migrated_antipad
            corrections.append(
                "旧 via_transition.padstack.antipad_diameter/antipad_per_layer 已无损迁移为"
                "规范 antipad.topology/shape/per_layer；旧字段仅作为单向兼容别名输出。"
            )
    supplied_antipad = (explicit_mm.get("antipad")
                        if isinstance(explicit_mm.get("antipad"), Mapping) else {})
    legacy_antipad_follow = "follow_mount_side" not in supplied_antipad
    supplied_vias = (explicit_mm.get("gnd_vias")
                     if isinstance(explicit_mm.get("gnd_vias"), Mapping) else {})
    supplied_simulation = (explicit_mm.get("simulation")
                           if isinstance(explicit_mm.get("simulation"), Mapping) else {})
    merged = _deep_merge(defaults, explicit_mm)
    # A stackup is an owned ordered collection.  Preserve a saved custom
    # stackup verbatim instead of combining it with newer application defaults.
    stackup = copy.deepcopy(explicit_mm.get("stackup", defaults["stackup"]))
    merged.pop("stackup")
    converted = _coerce(merged, {key: value for key, value in defaults.items() if key != "stackup"}, "")
    layer_template = copy.deepcopy(defaults["stackup"][0])
    for key in ("dk", "df", "conductivity", "antipad_expansion_override"):
        layer_template[key] = None
    converted["stackup"] = []
    if not isinstance(stackup, list):
        raise ConfigError("stackup 必须是数组。")
    for index, layer in enumerate(stackup):
        merged_layer = _deep_merge(layer_template, layer)
        item = _coerce(merged_layer, layer_template, f"stackup[{index}]")
        for key in ("dk", "df", "conductivity", "antipad_expansion_override"):
            item[key] = None if item.get(key) in (None, "") else _number(item[key], f"stackup[{index}].{key}")
        converted["stackup"].append(item)

    # Defaults may evolve to a completely different layer naming scheme. If
    # an older/custom stackup did not save linked layer fields, derive those
    # links from that same stackup. Explicit user layer names are deliberately
    # preserved so validation reports stale references instead of redirecting
    # a via or port silently.
    conductive_layers = [layer for layer in converted["stackup"]
                         if layer.get("type") in {"signal", "plane"}]
    signal_layers = [layer for layer in converted["stackup"]
                     if layer.get("type") == "signal"]
    reference_layers = [layer for layer in converted["stackup"]
                        if layer.get("type") == "plane" and layer.get("is_reference_plane")]
    derived_start = str((signal_layers or conductive_layers or [{"name": ""}])[0]["name"])
    derived_end = str((conductive_layers or [{"name": ""}])[-1]["name"])
    derived_references = [str(layer["name"]) for layer in reference_layers]
    if "start_layer" not in supplied_vias:
        converted["gnd_vias"]["start_layer"] = derived_start
    if "end_layer" not in supplied_vias:
        converted["gnd_vias"]["end_layer"] = derived_end
    if "connect_layers" not in supplied_vias:
        converted["gnd_vias"]["connect_layers"] = derived_references
    if "port_reference_layer" not in supplied_simulation:
        nearest_reference = find_nearest_reference_plane(
            converted["stackup"], str(converted["capacitor"].get("mount_side", "top"))
        )
        converted["simulation"]["port_reference_layer"] = (
            str(_layer_value(nearest_reference, "name", ""))
            if nearest_reference is not None else ""
        )
    if source_schema_version < 2 and ("gnd_vias" not in raw_config or
                                      "simulation" not in raw_config):
        corrections.append(
            "旧配置未保存的 VIA/端口层引用已按其自定义叠层重新派生；"
            "显式保存的层名不会被静默改写。"
        )
    converted["schema_version"] = 3
    converted["units"] = units
    converted["_internal_units"] = "mm"
    # V2.1 used ``full_factorial`` as the execution backend.  Keep loading
    # those files, but migrate the backend to the hybrid native workflow while
    # preserving full-factorial as the Variation combination mode.
    execution = converted.setdefault("sweep_execution", {})
    if execution.get("strategy") == "full_factorial":
        execution["strategy"] = "hybrid"
    execution.setdefault("variation_mode", "full_factorial")
    execution.setdefault("optimetrics_setup_name", "ParametricSetup1")
    execution.setdefault("custom_variations", [])
    execution.setdefault("resume", True)
    execution.setdefault("retry_failed", True)
    raw_aedt_version = converted["project"].get("aedt_version", "")
    canonical_aedt_version = normalize_aedt_version(raw_aedt_version)
    converted["project"]["aedt_version"] = canonical_aedt_version
    if not str(raw_aedt_version).strip():
        corrections.append("未指定 AEDT 版本，已按当前环境设为 2022 R2。")
    elif canonical_aedt_version != str(raw_aedt_version).strip():
        corrections.append(
            f"AEDT 版本 {raw_aedt_version!r} 已规范化为 {canonical_aedt_version}。"
        )
    custom = converted["gnd_vias"].get("custom_coordinates", [])
    if not isinstance(custom, list):
        raise ConfigError("gnd_vias.custom_coordinates 必须是 [[x,y], ...]。")
    parsed_custom = []
    for index, point in enumerate(custom):
        if not isinstance(point, Sequence) or isinstance(point, str) or len(point) != 2:
            raise ConfigError(f"gnd_vias.custom_coordinates[{index}] 必须包含 x、y 两个数。")
        parsed_custom.append([float(_number(point[0], f"custom[{index}].x")),
                              float(_number(point[1], f"custom[{index}].y"))])
    converted["gnd_vias"]["custom_coordinates"] = parsed_custom
    # V2.1 以“钻孔直径 + 镀铜厚度”为权威输入，成品孔始终由两者派生。
    # 旧 JSON 若同时保存了不一致的三个值，保留明确迁移记录而不是静默采用
    # 一个与 HFSS 空心桶几何不同的成品孔。
    vias = converted["gnd_vias"]
    expected_hole = float(vias["drill_diameter"]) - 2.0 * float(vias["plating_thickness"])
    if not math.isclose(float(vias["finished_hole_diameter"]), expected_hole, abs_tol=_EPS):
        old_hole = float(vias["finished_hole_diameter"])
        vias["finished_hole_diameter"] = expected_hole
        corrections.append(
            "GND VIA 旧配置的成品孔直径与钻孔/镀铜厚度不一致："
            f"{shown_length(old_hole)} 已按 drill_diameter - 2×plating_thickness 更新为 "
            f"{shown_length(expected_hole)}。"
        )
    if vias.get("pattern") == "four_corner" and int(vias.get("count", 0)) != 4:
        vias["count"] = 4
        corrections.append("四角 GND VIA 布局的 count 已固定为 4。")
    if vias.get("pattern") == "custom" and int(vias.get("count", 0)) != len(parsed_custom):
        vias["count"] = len(parsed_custom)
        corrections.append("自定义 GND VIA 的 count 已按坐标行数自动更新。")
    capacitor = converted["capacitor"]
    derived = derive_capacitor_values(capacitor, converted["geometry"]["overlap"])
    if not math.isclose(capacitor["pad_gap"], derived["pad_gap"], abs_tol=_EPS):
        capacitor["pad_gap"] = derived["pad_gap"]
        corrections.append("高级中心距模式：pad_gap 已按 pad_pitch - pad_length 更新。")
    if not math.isclose(capacitor["pad_pitch"], derived["pad_pitch"], abs_tol=_EPS):
        capacitor["pad_pitch"] = derived["pad_pitch"]
        corrections.append("普通焊盘模式：pad_pitch 已按 pad_length + pad_gap 更新。")
    if not math.isclose(capacitor["bridge_length"], derived["bridge_length"], abs_tol=_EPS):
        capacitor["bridge_length"] = derived["bridge_length"]
        corrections.append("自动桥接：bridge_length 已按 pad_gap + 2×overlap 更新。")
    trace = converted["trace"]
    derived_trace_gap = float(trace["trace_pitch"]) - float(trace["trace_width"])
    if not math.isclose(float(trace.get("trace_gap", derived_trace_gap)), derived_trace_gap,
                        abs_tol=_EPS):
        corrections.append(
            "trace_gap 已按 trace_pitch - trace_width 重新派生；内部以中心距 trace_pitch 为权威。"
        )
    trace["trace_gap"] = derived_trace_gap
    # 信号孔中心距（孔间距）为 V2.4 新增的独立字段；旧配置未保存时回填为
    # pair_pitch，保证孔与走线同中心距、无扇出，与旧版本几何完全一致。
    supplied_via_transition = (explicit_mm.get("via_transition")
                               if isinstance(explicit_mm.get("via_transition"), Mapping) else {})
    if "signal_via_pitch" not in supplied_via_transition:
        converted["via_transition"]["signal_via_pitch"] = float(converted["via_transition"]["pair_pitch"])
        corrections.append(
            "旧配置缺少 via_transition.signal_via_pitch，已回填为 pair_pitch（信号孔与走线同中心距，无扇出）。"
        )
    # Independent entry/exit GAP is the source of truth for walk-line pitch.
    # Legacy files with only pair_pitch get both gaps = pair_pitch − width so
    # geometry still matches the old same-pitch-both-ends contract.  Defaults
    # merge would otherwise inject the production GAP (0.7 mm) and silently
    # change a file whose pair_pitch was not 0.8 mm.
    pair_pitch = float(converted["via_transition"].get("pair_pitch", 0.0))
    backfilled_gap_sides: List[str] = []
    for side in ("entry_trace", "exit_trace"):
        supplied_side = (supplied_via_transition.get(side)
                         if isinstance(supplied_via_transition.get(side), Mapping) else {})
        trace = converted["via_transition"].setdefault(side, {})
        width = float(trace.get("width", 0.0) or 0.0)
        if "gap" not in supplied_side:
            trace["gap"] = pair_pitch - width
            backfilled_gap_sides.append(side)
        else:
            trace["gap"] = float(trace.get("gap", pair_pitch - width))
    if backfilled_gap_sides:
        corrections.append(
            "旧配置缺少 via_transition."
            + "/".join(f"{side}.gap" for side in backfilled_gap_sides)
            + "，已按 pair_pitch − width 回填（两端走线中心距与旧 pair_pitch 一致）。"
        )
    # Re-normalize arbitrary per-layer keys retained by the extensible schema,
    # then derive legacy read-only aliases in one direction.
    padstack = converted["via_transition"]["padstack"]
    padstack["antipad"] = canonical_antipad(padstack.get("antipad"))
    padstack["antipad_diameter"] = float(padstack["antipad"]["diameter"])
    padstack["antipad_per_layer"] = {
        str(name): float(item.get("diameter", padstack["antipad"]["diameter"]))
        for name, item in padstack["antipad"]["per_layer"].items()
        if (not bool(item.get("inherit_global", False))
            and str(item.get("shape", padstack["antipad"]["shape"])) == "circle")
    }
    # Return VIA uses the same finished-hole contract as every other plated
    # through hole.  Derived-pattern counts are read-only canonical values;
    # side_pair remains user-authoritative and odd values are rejected later.
    return_vias = converted["via_transition"]["return_vias"]
    expected_return_hole = (float(return_vias["drill_diameter"])
                            - 2.0 * float(return_vias["plating_thickness"]))
    if not math.isclose(float(return_vias["finished_hole_diameter"]),
                        expected_return_hole, abs_tol=_EPS):
        old_hole = float(return_vias["finished_hole_diameter"])
        return_vias["finished_hole_diameter"] = expected_return_hole
        corrections.append(
            "Return VIA 旧配置的成品孔直径与钻孔/镀铜厚度不一致："
            f"{shown_length(old_hole)} 已按 drill_diameter - 2×plating_thickness 更新为 "
            f"{shown_length(expected_return_hole)}。")
    if return_vias.get("pattern") == "four_corner" and int(return_vias.get("count", 0)) != 4:
        return_vias["count"] = 4
        corrections.append("four_corner Return VIA 的 count 已按固定拓扑派生为 4（只读）。")
    if return_vias.get("pattern") == "custom":
        derived_count = len(return_vias.get("custom_coordinates", []))
        if int(return_vias.get("count", 0)) != derived_count:
            return_vias["count"] = derived_count
            corrections.append("custom Return VIA 的 count 已按坐标行数派生（只读）。")
    # side_pair count=2: center_to_signal is the editable source; offset_y is
    # derived.  Legacy files with only offset_y backfill the clearance so
    # geometry stays bit-identical to current main (1.1 − 0.8/2 = 0.7 mm).
    supplied_return = (supplied_via_transition.get("return_vias")
                       if isinstance(supplied_via_transition.get("return_vias"), Mapping)
                       else {})
    signal_pitch = float(converted["via_transition"].get("signal_via_pitch", 0.0) or 0.0)
    offset_y = float(return_vias.get("offset_y", 0.0) or 0.0)
    if "center_to_signal" not in supplied_return:
        return_vias["center_to_signal"] = side_pair_center_to_signal(offset_y, signal_pitch)
        corrections.append(
            "旧配置缺少 via_transition.return_vias.center_to_signal，"
            "已按 offset_y − signal_via_pitch/2 回填（两侧成对几何与旧 Y 向偏移一致）。"
        )
    else:
        return_vias["center_to_signal"] = float(
            return_vias.get("center_to_signal", side_pair_center_to_signal(offset_y, signal_pitch))
        )
    if is_side_pair_count2(return_vias):
        derived_offset_y = side_pair_return_offset_y(
            signal_pitch, return_vias["center_to_signal"])
        if not math.isclose(float(return_vias.get("offset_y", 0.0) or 0.0),
                            derived_offset_y, abs_tol=_EPS):
            return_vias["offset_y"] = derived_offset_y
            if "center_to_signal" in supplied_return:
                corrections.append(
                    "side_pair count=2 的 offset_y 已按 signal_via_pitch/2 + "
                    "center_to_signal 重新派生。"
                )
        else:
            return_vias["offset_y"] = derived_offset_y
    mount_side = str(converted.get("capacitor", {}).get("mount_side", "top"))
    if "auto_follow_layer" not in supplied_antipad:
        converted["antipad"]["auto_follow_layer"] = ""
    corrections.extend(synchronize_antipad_mount_side(
        converted, migrate_legacy=legacy_antipad_follow
    ))
    if legacy_antipad_follow:
        corrections.append(
            "旧配置缺少 antipad.follow_mount_side/auto_follow_layer，已启用推荐的安装面跟随；"
            "原有多层 enable_antipad 选择均保留，系统自动层已单独记录。"
        )
    surface_signal = find_surface_signal_layer(converted["stackup"], mount_side)
    if (surface_signal is not None and "stackup" in explicit_mm and
            "copper_thickness" not in raw_trace):
        converted["trace"]["copper_thickness"] = float(surface_signal["thickness"])
        corrections.append(
            "自定义叠层未单独保存 trace.copper_thickness，已从当前安装面外层 signal 层派生，"
            "未套用新版默认铜厚。"
        )
    elif surface_signal and not math.isclose(surface_signal["thickness"], converted["trace"]["copper_thickness"], abs_tol=_EPS):
        surface_signal["thickness"] = converted["trace"]["copper_thickness"]
        corrections.append("当前安装面信号铜厚已与 trace.copper_thickness 同步。")
    materials = converted["materials"]
    if materials.get("use_ideal_conductor"):
        materials["conductor_mode"] = "pec"
    elif materials.get("conductor_mode") == "pec":
        materials["use_ideal_conductor"] = True
    return converted, corrections


def to_external_config(config: Mapping[str, Any], unit: Optional[str] = None) -> Dict[str, Any]:
    normalized, _ = normalize_config(config)
    target = str(unit or normalized.get("units", "mm")).lower()
    if target not in SUPPORTED_UNITS:
        raise ConfigError(f"units 只能为 mm 或 mil，当前为 {target!r}。")
    result = _scale_explicit_lengths(normalized, 1.0 if target == "mm" else 1.0 / MM_PER_MIL)
    result.pop("_internal_units", None)
    result["units"] = target
    result["schema_version"] = 3
    return result


def apply_package_preset(config: Mapping[str, Any], package: str) -> Dict[str, Any]:
    normalized, _ = normalize_config(config)
    package = str(package).lower()
    if package not in SUPPORTED_PACKAGES:
        raise ConfigError("封装只支持 0201、0402 或 custom。")
    normalized["capacitor"]["package"] = package
    if package in PACKAGE_PRESETS:
        normalized["capacitor"].update(copy.deepcopy(PACKAGE_PRESETS[package]))
    return normalize_config(normalized)[0]


def load_config(path: Path) -> Tuple[Dict[str, Any], List[str]]:
    source = Path(path).expanduser().resolve()
    try:
        with source.open("r", encoding="utf-8") as stream:
            raw = json.load(stream)
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在：{source}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"JSON 格式错误，第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}") from exc
    except OSError as exc:
        raise ConfigError(f"无法读取配置：{exc}") from exc
    return normalize_config(raw)


def save_config(config: Mapping[str, Any], path: Path) -> List[str]:
    normalized, corrections = normalize_config(config)
    external = to_external_config(normalized)
    destination = Path(path).expanduser().resolve()
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(external, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(destination)
    except OSError as exc:
        raise ConfigError(f"无法保存配置 {destination}：{exc}") from exc
    return corrections


def resolve_output_directory(config: Mapping[str, Any], base_directory: Optional[Path] = None) -> Path:
    raw = str(config.get("project", {}).get("output_directory", "")).strip()
    if not raw:
        raise ConfigError("输出目录不能为空。")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (Path(base_directory) if base_directory else Path.cwd()) / path
    return path.resolve()


def _positive(result: ValidationResult, value: Any, name: str, allow_zero: bool = False) -> Optional[float]:
    try:
        number = float(value)
        invalid = (not math.isfinite(number)) or (number < 0 if allow_zero else number <= 0)
        if invalid:
            raise ValueError
        return number
    except (TypeError, ValueError):
        result.add_error(f"{name} 必须为{'非负' if allow_zero else '正'}数。")
        return None


def _finite(result: ValidationResult, value: Any, name: str) -> Optional[float]:
    try:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError
        return number
    except (TypeError, ValueError):
        result.add_error(f"{name} 必须为有限实数。")
        return None


def _validate_scenario(config: Mapping[str, Any], result: ValidationResult, *,
                       raw_scenario: Any = None, source_schema_version: int = 1) -> None:
    scenario = config.get("scenario") if isinstance(config.get("scenario"), Mapping) else {}
    scenario_type_value = scenario.get("type")
    # A v3 config must declare its scenario explicitly; a missing type is an error.
    if source_schema_version >= 3 and not (
        isinstance(raw_scenario, Mapping) and raw_scenario.get("type")
    ):
        result.add_error(
            "scenario.type 缺失：schema v3 配置必须显式声明 surface_capacitor 或 via_transition。"
        )
        return
    if scenario_type_value not in SUPPORTED_SCENARIO_TYPES:
        result.add_error(
            "scenario.type 只能为 surface_capacitor 或 via_transition；当前值为 {!r}。".format(
                scenario_type_value
            )
        )
        return


def _validate_project(config: Mapping[str, Any], result: ValidationResult,
                      base_directory: Optional[Path], check_output_directory: bool) -> None:
    project = config["project"]
    for key in ("project_name", "design_name"):
        if not _AEDT_SAFE_NAME.fullmatch(str(project.get(key, ""))):
            result.add_error(f"project.{key} 必须以字母开头且只含字母、数字、下划线。")
    if project.get("solution_type") not in {"Modal", "Terminal"}:
        result.add_error("solution_type 只能为 Modal 或 Terminal。")
    if project.get("aedt_version") not in SUPPORTED_AEDT_VERSIONS:
        result.add_error("aedt_version 仅支持 2022 R2（2022.2）或 2023 R2（2023.2）。")
    if check_output_directory:
        try:
            target = resolve_output_directory(config, base_directory)
            parent = target
            while not parent.exists() and parent != parent.parent:
                parent = parent.parent
            if not parent.is_dir() or not os.access(parent, os.W_OK):
                result.add_error(f"输出目录不可写：{target}")
        except ConfigError as exc:
            result.add_error(str(exc))


def _validate_trace_cap(config: Mapping[str, Any], result: ValidationResult) -> None:
    trace, cap = config["trace"], config["capacitor"]
    unit = str(config.get("units", DEFAULT_DISPLAY_UNIT))
    def shown_length(mm_value: float) -> str:
        return f"{format_length(mm_value, unit)} {unit}"
    if cap.get("mount_side") not in SUPPORTED_MOUNT_SIDES:
        result.add_error(
            "capacitor.mount_side 只能为 top 或 bottom；当前值为 {!r}。".format(
                cap.get("mount_side")
            )
        )
    zero_ok = {"cap_center_x", "cap_center_x_offset", "pad_lead_in_length",
               "pad_lead_out_length"}
    for section in ("trace", "capacitor", "geometry"):
        for key in _SECTION_LENGTH_FIELDS[section]:
            _positive(result, config[section].get(key), f"{section}.{key}", key in zero_ok)
    if result.errors:
        return
    if trace["trace_pitch"] <= trace["trace_width"]:
        result.add_error("trace_pitch 必须大于 trace_width，避免差分线短路。")
    expected_gap = float(trace["trace_pitch"]) - float(trace["trace_width"])
    if not math.isclose(float(trace["trace_gap"]), expected_gap, abs_tol=_EPS):
        result.add_error("trace_gap 必须等于 trace_pitch - trace_width。")
    mode = str(config.get("capacitor_model", {}).get("mode", "ideal_bridge"))
    transverse = float(cap["bridge_width"]) if mode == "ideal_bridge" else float(trace["trace_width"])
    if cap["cap_pitch"] <= max(trace["trace_width"], transverse):
        result.add_error("P/N 两颗电容中心距过小，会使信号导体短路。")
    if cap["pad_pitch"] <= cap["pad_length"]:
        result.add_error("单颗电容两个焊盘中心距必须大于焊盘长度。")
    if not math.isclose(cap["pad_pitch"], cap["pad_length"] + cap["pad_gap"], abs_tol=_EPS):
        result.add_error("pad_pitch 与 pad_length + pad_gap 不一致。")
    if cap.get("package") not in SUPPORTED_PACKAGES:
        result.add_error("capacitor.package 只能为 0201、0402 或 custom。")
    if cap.get("pad_pitch_mode") not in SUPPORTED_PAD_PITCH_MODES:
        result.add_error("pad_pitch_mode 无效。")
    escape_mode = str(trace.get("pad_escape_mode", ""))
    if escape_mode not in SUPPORTED_PAD_ESCAPE_MODES:
        result.add_error("pad_escape_mode 只能为 direct_45、smooth_s_bend 或 lead_then_45。")
    if trace.get("transition_direction") not in SUPPORTED_TRANSITION_DIRECTIONS:
        result.add_error("transition_direction 无效。")
    requested_direction = str(trace.get("transition_direction", "auto"))
    rotation = math.radians(float(cap.get("pad_rotation", 0.0)))
    if abs(math.cos(rotation)) <= 1.0e-6:
        result.add_error("pad_rotation 使焊盘局部出线方向不再朝向输入/输出远端。")
    if escape_mode == "direct_45" and abs(float(cap.get("pad_rotation", 0.0))) > 1.0e-7:
        result.add_error(
            "direct_45 要求 pad_rotation=0°，否则严格45°段无法同时沿旋转PAD的局部出线切线；"
            "请改用 smooth_s_bend 或 lead_then_45。"
        )
    outer_distance = (float(cap["pad_pitch"]) + float(cap["pad_length"])) / 2.0
    side_pitches = {
        "输入侧": float(cap["cap_pitch"]) - 2.0 * outer_distance * math.sin(rotation),
        "输出侧": float(cap["cap_pitch"]) + 2.0 * outer_distance * math.sin(rotation),
    }
    for side_label, actual_pad_pitch in side_pitches.items():
        delta = actual_pad_pitch - float(trace["trace_pitch"])
        actual_direction = "contract" if delta > _EPS else "expand" if delta < -_EPS else "none"
        if requested_direction != "auto" and actual_direction not in {"none", requested_direction}:
            result.add_error(
                f"transition_direction={requested_direction} 与旋转后{side_label}尺寸矛盾："
                f"PAD连接点中心距={shown_length(actual_pad_pitch)}，"
                f"目标 trace_pitch={shown_length(float(trace['trace_pitch']))}，"
                f"实际需要 {actual_direction}。"
            )
    if escape_mode == "smooth_s_bend" and not all(
        (actual - float(trace["trace_pitch"])) == 0.0
        or (actual - float(trace["trace_pitch"])) * (
            side_pitches["输入侧"] - float(trace["trace_pitch"])
        ) >= 0.0
        for actual in side_pitches.values()
    ):
        result.add_error(
            "当前 pad_rotation 使输入/输出两侧分别需要收缩和展开；"
            "这会破坏关于Y=0的等长镜像约束，请减小旋转角或调整目标 trace_pitch。"
        )
    if escape_mode == "smooth_s_bend":
        minimum = max(
            2.0 * float(trace["trace_width"]),
            max(abs(actual - float(trace["trace_pitch"])) / 2.0
                for actual in side_pitches.values()),
        )
        if float(trace["transition_length"]) + _EPS < minimum:
            result.add_error(
                f"smooth_s_bend 的 transition_length={shown_length(float(trace['transition_length']))} 空间不足；"
                f"当前旋转和间距至少需要 {shown_length(minimum)}。"
            )
    if not cap.get("symmetric_about_y", True):
        result.add_error("V2 要求 P/N 关于 Y=0 镜像。")
    geometry = config.get("geometry", {})
    if geometry.get("trace_join") not in SUPPORTED_TRACE_JOINS:
        result.add_error("geometry.trace_join 只能为 miter、limited_miter 或 bevel。")
    _positive(result, geometry.get("miter_limit"), "geometry.miter_limit")


def _validate_stackup(config: Mapping[str, Any], result: ValidationResult) -> None:
    stackup = config.get("stackup")
    if not isinstance(stackup, list) or not stackup:
        result.add_error("PCB 叠层不能为空。")
        return
    names: List[str] = []
    for index, layer in enumerate(stackup):
        prefix = f"stackup[{index}]"
        name, kind = str(layer.get("name", "")), str(layer.get("type", ""))
        names.append(name)
        if not _AEDT_SAFE_NAME.fullmatch(name):
            result.add_error(f"{prefix}.name 不是安全的 AEDT 名称。")
        if kind not in SUPPORTED_LAYER_TYPES:
            result.add_error(f"{prefix}.type 无效。")
        _positive(result, layer.get("thickness"), f"{prefix}.thickness")
        if kind in {"dielectric", "soldermask"}:
            _positive(result, layer.get("dk"), f"{prefix}.dk")
            _positive(result, layer.get("df"), f"{prefix}.df", True)
        if layer.get("enable_antipad") and (kind != "plane" or not layer.get("is_reference_plane")):
            result.add_error(f"{prefix} 只有参考平面可启用反焊盘。")
    if len(set(names)) != len(names):
        result.add_error("叠层名称不能重复。")
    signal_indices = [index for index, layer in enumerate(stackup) if layer.get("type") == "signal"]
    if not signal_indices:
        result.add_error("叠层至少需要一个 signal 层。")
    elif any(layer.get("type") != "soldermask" for layer in stackup[:signal_indices[0]]):
        result.add_error("首个 signal 层上方只能存在 soldermask。")
    mount_side = str(config.get("capacitor", {}).get("mount_side", "top"))
    if signal_indices and mount_side == "bottom" and any(
            layer.get("type") != "soldermask" for layer in stackup[signal_indices[-1] + 1:]):
        result.add_error(
            "底层安装需要实际的底层外层 signal 铜；当前最后一个 signal 层下方仍有非阻焊叠层。"
            "请在叠层底部提供外层信号铜（其下仅允许 soldermask），或切换为顶层安装。"
        )
    if not any(layer.get("type") == "dielectric" for layer in stackup):
        result.add_error("叠层至少需要一层介质。")
    if not any(layer.get("type") == "plane" and layer.get("is_reference_plane") for layer in stackup):
        result.add_error("叠层至少需要一层参考平面。")
    if signal_indices and mount_side in SUPPORTED_MOUNT_SIDES:
        surface = find_surface_signal_layer(stackup, mount_side)
        nearest = find_nearest_reference_plane(stackup, mount_side)
        if surface is not None and nearest is None:
            side_label = "Top" if mount_side == "top" else "Bottom"
            result.add_error(
                f"{side_label} 安装面从外层信号铜 {surface['name']} 向 PCB 内部找不到有效参考 plane；"
                "不能使用另一侧参考层。"
            )
    provisional = sorted({str(layer.get("material", "")) for layer in stackup
                          if PROVISIONAL_MATERIAL_TOKEN in str(layer.get("material", "")).upper()})
    if provisional:
        result.add_warning(
            "材料参数包含 PROVISIONAL 临时值（{}）：{}。".format(
                PROVISIONAL_MATERIAL_NOTICE, ", ".join(provisional)
            )
        )
    if signal_indices:
        unrouted_signal_layers = [str(stackup[index].get("name", ""))
                                  for index in signal_indices[1:]]
        if unrouted_signal_layers:
            result.add_warning(
                "内层/背面 signal 层不会被误建为整面铜，但当前模型没有其布线与铜密度数据，"
                "因此不生成这些层的导体图形：{}。需要真实内层影响时请提供布局/网络数据。".format(
                    ", ".join(unrouted_signal_layers)
                )
            )


def _validate_vias(config: Mapping[str, Any], result: ValidationResult) -> None:
    vias = config["gnd_vias"]
    if vias.get("pattern") not in SUPPORTED_VIA_PATTERNS:
        result.add_error("gnd_vias.pattern 无效。")
    if vias.get("barrel_model") not in {"hollow", "solid"}:
        result.add_error("gnd_vias.barrel_model 只能为 hollow 或 solid。")
    # Layer links remain meaningful saved state even while VIA generation is
    # disabled. Validate them first so a renamed/custom stackup cannot retain
    # a dangling reference that fails only after re-enabling the feature.
    names = [layer["name"] for layer in config["stackup"]]
    start, end = vias.get("start_layer"), vias.get("end_layer")
    valid_span = start in names and end in names
    if not valid_span:
        result.add_error("VIA start_layer/end_layer 必须存在；叠层可能已更名，请重新选择。")
    elif names.index(start) > names.index(end):
        result.add_error("VIA start_layer 必须位于 end_layer 上方。")
        valid_span = False
    valid_ground = {layer["name"] for layer in config["stackup"]
                    if layer.get("type") == "plane" and layer.get("is_reference_plane")}
    connections = vias.get("connect_layers", [])
    if not isinstance(connections, list):
        result.add_error("gnd_vias.connect_layers 必须是层名数组。")
        connections = []
    span_names = (set(names[names.index(start):names.index(end) + 1])
                  if valid_span else set())
    for name in connections:
        if name not in valid_ground:
            result.add_error(f"VIA connect_layers 中的 {name!r} 不是有效 GND/参考平面。")
        elif name not in span_names:
            result.add_error(f"VIA connect_layers 中的 {name!r} 不在起始层和终止层之间。")
    # 关闭时保留并检查层引用，但不让未使用的布局/制造值阻断无 VIA 基线生成。
    if not vias.get("enabled"):
        return
    count = _positive(result, vias.get("count"), "gnd_vias.count")
    if count is not None and not float(count).is_integer():
        result.add_error("gnd_vias.count 必须为整数。")
    signed_offsets = {"clearance_x", "clearance_y"}
    for key in _SECTION_LENGTH_FIELDS["gnd_vias"]:
        if key in signed_offsets:
            _finite(result, vias.get(key), f"gnd_vias.{key}")
        else:
            _positive(result, vias.get(key), f"gnd_vias.{key}")
    drill, hole, plating, pad = (float(vias[key]) for key in
                                  ("drill_diameter", "finished_hole_diameter", "plating_thickness", "pad_diameter"))
    if hole >= drill:
        result.add_error("finished_hole_diameter 必须小于 drill_diameter。")
    if pad <= drill:
        result.add_error("VIA pad_diameter 必须大于 drill_diameter。")
    if not math.isclose(plating, (drill - hole) / 2.0, abs_tol=_EPS):
        result.add_error("plating_thickness 必须等于钻孔与成品孔的半径差。")
    if float(vias.get("antipad_diameter", 0.0)) <= pad:
        result.add_error("VIA antipad_diameter 必须大于 pad_diameter。")
    if not isinstance(connections, list) or not connections:
        result.add_error("gnd_vias.connect_layers 至少需要一个参考平面。")
    if vias.get("enabled") and vias.get("pattern") == "custom" and not vias.get("custom_coordinates"):
        result.add_error("custom VIA 模式需要 custom_coordinates。")
    if vias.get("enabled") and vias.get("pattern") == "custom":
        coordinates = [tuple(float(value) for value in point)
                       for point in vias.get("custom_coordinates", [])]
        if len(coordinates) != len(set(coordinates)):
            result.add_error("custom VIA 坐标不能重复。")
        if int(vias.get("count", 0)) != len(coordinates):
            result.add_error("custom VIA 的 count 必须等于坐标数量。")
    if vias.get("enabled") and vias.get("pattern") == "side_pair" and int(vias.get("count", 0)) % 2:
        result.add_error("side_pair 的 VIA 数量必须为偶数。")
    if vias.get("enabled") and vias.get("pattern") == "rectangular_fence" and int(vias.get("count", 0)) % 4:
        result.add_error("rectangular_fence 的 VIA 数量必须为 4 的倍数。")


def _validate_model_sim_sweeps(config: Mapping[str, Any], result: ValidationResult,
                               base_directory: Optional[Path]) -> None:
    model = config["capacitor_model"]
    mode = model.get("mode")
    if mode not in SUPPORTED_CAP_MODELS:
        result.add_error("capacitor_model.mode 无效。")
    elif mode == "lumped_rlc":
        _positive(result, model.get("sheet_width"), "capacitor_model.sheet_width")
        for key in ("capacitance_pf", "esr_ohm", "esl_nh"):
            _positive(result, model.get(key), f"capacitor_model.{key}", key != "capacitance_pf")
    elif mode == "sparameter":
        _positive(result, model.get("sheet_width"), "capacitor_model.sheet_width")
        common = str(model.get("touchstone_file", "")).strip()
        p = str(model.get("touchstone_file_p", "")).strip() or common
        n = str(model.get("touchstone_file_n", "")).strip() or common
        if not p or not n:
            result.add_error("S 参数模式必须提供 P/N Touchstone 文件。")
        else:
            resolved = []
            for label, raw in (("P", p), ("N", n)):
                path = Path(raw).expanduser()
                if not path.is_absolute() and base_directory:
                    path = Path(base_directory) / path
                path = path.resolve()
                resolved.append(path)
                if path.suffix.lower() not in {".s2p", ".s4p", ".ts"}:
                    result.add_error(f"{label} 电容模型不是支持的 Touchstone 文件：{path}")
                elif not path.is_file():
                    result.add_error(f"{label} 电容模型文件不存在：{path}")
            if len(resolved) == 2 and resolved[0] != resolved[1]:
                result.add_error("P/N 两颗电容必须使用同一个 S 参数模型。")
    simulation = config["simulation"]
    if simulation.get("port_reference_mode", "auto_nearest") not in {"auto_nearest", "manual"}:
        result.add_error("port_reference_mode 只能为 auto_nearest 或 manual。")
    if simulation.get("port_size_mode", "auto") not in {"auto", "manual"}:
        result.add_error("port_size_mode 只能为 auto 或 manual。")
    reference_names = {layer["name"] for layer in config["stackup"]
                       if layer.get("type") == "plane" and layer.get("is_reference_plane")}
    requested_reference = str(simulation.get("port_reference_layer", "")).strip()
    if requested_reference and requested_reference not in reference_names:
        result.add_error(
            f"simulation.port_reference_layer={requested_reference!r} 不存在或不是有效参考平面；"
            "叠层可能已更名，请重新选择。"
        )
    _positive(result, simulation.get("port_lateral_margin"), "simulation.port_lateral_margin")
    if simulation.get("enabled"):
        from simulation_sweep_semantics import inspect_frequency_plan
        for level, message in inspect_frequency_plan(simulation).issues:
            (result.add_error if level == "error" else result.add_warning)(message)
        if simulation.get("create_airbox"):
            _positive(result, simulation.get("airbox_margin"), "simulation.airbox_margin")
            if simulation.get("open_boundary") not in {"Radiation", "PML"}:
                result.add_error("open_boundary 只能为 Radiation 或 PML。")
        elif simulation.get("open_boundary"):
            result.add_warning("未创建空气盒，开放边界设置不会应用。")
        _positive(result, simulation.get("max_passes"), "simulation.max_passes")
        delta_s = float(simulation.get("max_delta_s", 0))
        if not 0 < delta_s < 1:
            result.add_error("simulation.max_delta_s 必须位于 0 与 1 之间。")
        for key in ("setup_name", "sweep_name"):
            if not _AEDT_SAFE_NAME.fullmatch(str(simulation.get(key, ""))):
                result.add_error(f"simulation.{key} 必须以字母开头且只包含字母、数字和下划线。")
        if simulation.get("generate_reports") and not simulation.get("create_differential_ports"):
            result.add_warning("未创建差分端口，混合模 S 参数报告将自动跳过。")
        if simulation.get("create_differential_ports") and str(config.get("project", {}).get("solution_type")) != "Terminal":
            result.add_error("自动差分端口及混合模命名需要 project.solution_type=Terminal；旧 Modal 配置仍可用于仅生成几何。")
        outputs = simulation.get("report_outputs", {})
        if not isinstance(outputs, Mapping):
            result.add_error("simulation.report_outputs 必须是对象。")
        elif simulation.get("generate_reports") and not any(bool(outputs.get(name)) for name in ("Sdd11", "Sdd21", "Sdc21", "Scd21")):
            result.add_warning("已启用报告生成，但未选择任何 S 参数报告。")
        if simulation.get("generate_tdr"):
            result.add_warning("差分 TDR 当前仅记录复核请求；脚本不会自动创建完整 TDR 报告。")
    try:
        from simulation_sweep_semantics import inspect_sweep_plan
        for level, message in inspect_sweep_plan(config).issues:
            (result.add_error if level == "error" else result.add_warning)(message)
    except (ImportError, ValueError):
        pass
    execution = config.get("sweep_execution", {})
    if execution.get("strategy") not in {"hybrid", "native_optimetrics", "topology_variants", "legacy_discrete", "full_factorial"}:
        result.add_error("当前设计参数扫描策略只支持 full_factorial。")
    if execution.get("variation_mode", "full_factorial") not in {"full_factorial", "synchronous", "one_at_a_time", "custom"}:
        result.add_error("sweep_execution.variation_mode 无效。")
    if int(execution.get("max_cases", 0)) < 1:
        result.add_error("sweep_execution.max_cases 必须至少为 1。")
    if int(execution.get("parallel_jobs", 0)) < 1:
        result.add_error("当前批量队列 parallel_jobs 固定为 1；尚未启用并行 AEDT 会话。")
    for name, sweep in config.get("sweeps", {}).items():
        if not isinstance(sweep, Mapping) or not sweep.get("enabled") or "values" in sweep:
            continue
        scan_mode = str(sweep.get("scan_mode", sweep.get("mode", "LinearStep")))
        if scan_mode in {"LinearCount", "Log"}:
            try:
                start, stop = float(sweep["start"]), float(sweep["stop"])
                count = int(sweep.get("count", sweep.get("step", 0)))
                if stop < start or count < 2 or (scan_mode == "Log" and (start <= 0 or stop <= 0)):
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                result.add_error(f"扫描 {name} 的 {scan_mode} 参数无效。")
            continue
        if scan_mode == "Explicit Values":
            result.add_error(f"扫描 {name} 使用 Explicit Values 时必须提供 values。")
            continue
        try:
            start, stop, step = float(sweep["start"]), float(sweep["stop"]), float(sweep["step"])
            if step <= 0 or stop < start:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            result.add_error(f"扫描 {name} 必须满足 stop >= start 且 step > 0。")


def validate_config(config: Mapping[str, Any], base_directory: Optional[Path] = None,
                    check_output_directory: bool = True, normalize: bool = True) -> Tuple[Dict[str, Any], ValidationResult]:
    raw_is_mapping = isinstance(config, Mapping)
    source_schema_version = int(config.get("schema_version", 1)) if raw_is_mapping else 1
    raw_scenario = config.get("scenario") if raw_is_mapping else None
    normalized, corrections = normalize_config(config) if normalize else (copy.deepcopy(dict(config)), [])
    result = ValidationResult(corrections=corrections)
    if normalized.get("units") not in SUPPORTED_UNITS:
        result.add_error("units 只能为 mm 或 mil。")
    _validate_scenario(normalized, result, raw_scenario=raw_scenario,
                       source_schema_version=source_schema_version)
    _validate_project(normalized, result, base_directory, check_output_directory)
    _validate_trace_cap(normalized, result)
    if normalized["antipad"].get("mode") not in SUPPORTED_ANTIPAD_MODES:
        result.add_error("antipad.mode 无效。")
    _positive(result, normalized["antipad"].get("global_expansion"), "antipad.global_expansion")
    if normalized["antipad"].get("follow_mount_side"):
        nearest = find_nearest_reference_plane(
            normalized["stackup"], str(normalized["capacitor"].get("mount_side", "top"))
        )
        nearest_name = str(_layer_value(nearest, "name", "")) if nearest is not None else ""
        tracked_name = str(normalized["antipad"].get("auto_follow_layer", ""))
        if nearest_name and tracked_name != nearest_name:
            result.add_error(
                "antipad.auto_follow_layer 与当前安装面最近参考层不一致："
                f"当前={tracked_name or '空'}，应为={nearest_name}。"
            )
    for key in ("margin_input", "margin_output", "margin_y"):
        _positive(result, normalized["plane"].get(key), f"plane.{key}")
    if normalized["materials"].get("conductor_mode") not in SUPPORTED_CONDUCTOR_MODES:
        result.add_error("materials.conductor_mode 无效。")
    _validate_stackup(normalized, result)
    _validate_vias(normalized, result)
    _validate_model_sim_sweeps(normalized, result, base_directory)
    if normalized["gnd_vias"].get("barrel_model") == "solid":
        result.add_warning("实心 VIA 圆柱会高估铜体积和低估孔内场；无制造数据时仅用于趋势比较。")
    if normalized["simulation"].get("pcie_generation_preset") != "custom":
        result.add_warning("PCIe 代际预设仅用于频率范围初始化，不代表标准合规结论。")
    if result.is_valid and normalized.get("gnd_vias", {}).get("enabled"):
        try:
            from geometry_calculator import GeometryCalculator
            geometry = GeometryCalculator(normalized).calculate()
            for error in geometry.errors:
                result.add_error(error)
            for warning in geometry.warnings:
                result.add_warning(warning)
        except (ImportError, AttributeError):
            pass
    return normalized, result


def format_validation_report(result: ValidationResult) -> str:
    lines = ["Validate基础参数结果：" + ("通过" if result.is_valid else "失败"), "",
             f"严重错误（{len(result.errors)}）："]
    lines.extend([f"  [错误] {item}" for item in result.errors] or ["  无"])
    lines.extend(["", f"警告（{len(result.warnings)}）："])
    lines.extend([f"  [警告] {item}" for item in result.warnings] or ["  无"])
    lines.extend(["", f"自动修正/迁移（{len(result.corrections)}）："])
    lines.extend([f"  [修正] {item}" for item in result.corrections] or ["  无"])
    return "\n".join(lines)


class ConfigManager:
    def __init__(self, base_directory: Optional[Path] = None) -> None:
        self.base_directory = Path(base_directory).resolve() if base_directory else Path(__file__).resolve().parent

    def default(self) -> Dict[str, Any]:
        # The bundled JSON remains an internal-mm engineering baseline.  Only
        # after normalization do new/reset configurations select the default
        # GUI/save/HFSS expression unit, so no default number is reinterpreted
        # as mil on input.
        config = normalize_config(get_default_config())[0]
        config["units"] = DEFAULT_DISPLAY_UNIT
        config["_internal_units"] = INTERNAL_LENGTH_UNIT
        return config

    def load(self, path: Path) -> Tuple[Dict[str, Any], List[str]]:
        return load_config(path)

    def save(self, config: Mapping[str, Any], path: Path) -> List[str]:
        return save_config(config, path)

    def validate(self, config: Mapping[str, Any], check_output_directory: bool = True) -> Tuple[Dict[str, Any], ValidationResult]:
        return validate_config(config, self.base_directory, check_output_directory, True)

    def output_directory(self, config: Mapping[str, Any]) -> Path:
        return resolve_output_directory(config, self.base_directory)
