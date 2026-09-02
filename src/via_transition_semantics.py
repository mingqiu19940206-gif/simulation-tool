"""via_transition 本地语义校验规则（规格 §8）。"""
from __future__ import annotations

import math
from typing import Any, List, Mapping, Optional, Tuple

from config_manager import format_length
from via_transition_geometry import (ViaTransitionGeometryResult,
                                     derive_functional_barrel_span)
from via_antipad import (DIRECTION_MODES, SHAPES, SIZE_MODES, TOPOLOGIES,
                         canonical_antipad, contains_circle,
                         minimum_merged_circle_diameter)


def validate_via_transition(
    config: Mapping[str, Any],
    geometry: Optional[ViaTransitionGeometryResult],
) -> List[Tuple[str, str, str, str, str]]:
    """返回 ``(rule_id, level, message, parameter_path, suggestion)`` 列表。"""
    issues: List[Tuple[str, str, str, str, str]] = []
    display_unit = str(config.get("units", "mm"))
    def shown_length(mm_value: float) -> str:
        return f"{format_length(mm_value, display_unit, 9)} {display_unit}"
    stackup = list(config.get("stackup", []))
    names = [str(layer.get("name", "")) for layer in stackup]
    types = {str(layer.get("name", "")): str(layer.get("type", "")) for layer in stackup}
    planes = {name for name, kind in types.items()
              if kind == "plane" and bool(next(
                  (layer.get("is_reference_plane", False) for layer in stackup
                   if layer.get("name") == name), False))}

    vt = config.get("via_transition", {})
    route_axis = str(vt.get("route_axis", "x"))
    if route_axis not in {"x", "y"}:
        issues.append(("VIA-TRN-008", "error", "route_axis 仅支持 x/y。",
                       "via_transition.route_axis", "选择 X 或 Y 走线方向。"))
    entry = str(vt.get("entry_layer", ""))
    exit_layer = str(vt.get("exit_layer", ""))
    drill_start = str(vt.get("drill_start_layer", ""))
    drill_end = str(vt.get("drill_end_layer", ""))

    for path, value in (("via_transition.entry_layer", entry),
                        ("via_transition.exit_layer", exit_layer)):
        if value not in names:
            issues.append(("VIA-TRN-001", "error", f"{path} {value!r} 不存在于叠层。",
                           path, "选择叠层中存在的 signal 层。"))
        elif types.get(value) != "signal":
            issues.append(("VIA-TRN-002", "error", f"{path} {value!r} 不是 signal 层。",
                           path, "entry/exit 必须是 signal 层。"))
    if entry == exit_layer and entry:
        issues.append(("VIA-TRN-003", "error", "entry_layer 与 exit_layer 不得相同。",
                       "via_transition.exit_layer", "为 exit_layer 选择不同的 signal 层。"))
    for path, value in (("via_transition.drill_start_layer", drill_start),
                        ("via_transition.drill_end_layer", drill_end)):
        if value not in names:
            issues.append(("VIA-TRN-004", "error", f"{path} {value!r} 不存在于叠层。",
                           path, "选择叠层中存在的层。"))
    if drill_start in names and drill_end in names:
        if names.index(drill_start) >= names.index(drill_end):
            issues.append(("VIA-TRN-005", "error",
                           "drill_start_layer 必须位于 drill_end_layer 上方。",
                           "via_transition.drill_end_layer", "调整机械钻孔跨层顺序。"))
        span = names[names.index(drill_start):names.index(drill_end) + 1]
        if entry and entry not in span:
            issues.append(("VIA-TRN-006", "error",
                           "entry_layer 不在机械钻孔跨层内。",
                           "via_transition.entry_layer", "entry 必须位于钻孔跨层范围。"))
        if exit_layer and exit_layer not in span:
            issues.append(("VIA-TRN-007", "error",
                           "exit_layer 不在机械钻孔跨层内。",
                           "via_transition.exit_layer", "exit 必须位于钻孔跨层范围。"))

    # Phase 3 背钻与残桩语义校验（规格 §6.5、§8.1、§8.2）。
    backdrill = vt.get("backdrill", {})
    backdrill_enabled = bool(backdrill.get("enabled"))
    from_side = str(backdrill.get("from_side", "auto"))
    bd_diameter = float(backdrill.get("drill_diameter", 0.0))
    residual_stub = float(backdrill.get("residual_stub", 0.0))
    drill_for_bd = float(vt.get("padstack", {}).get("drill_diameter", 0.0))

    functional_span = derive_functional_barrel_span(
        names, entry, exit_layer, drill_start, drill_end)
    top_stub = bool(functional_span and functional_span.top_stub)
    bottom_stub = bool(functional_span and functional_span.bottom_stub)

    if residual_stub < 0:
        issues.append(("VIA-TRN-043", "error",
                       "backdrill.residual_stub 必须 >= 0。",
                       "via_transition.backdrill.residual_stub", "残桩长度不能为负。"))
    if from_side not in ("auto", "top", "bottom"):
        issues.append(("VIA-TRN-044", "error",
                       f"backdrill.from_side={from_side!r} 非法；仅支持 auto/top/bottom。",
                       "via_transition.backdrill.from_side", "选择 auto/top/bottom。"))
    if backdrill_enabled:
        if bd_diameter <= drill_for_bd:
            issues.append(("VIA-TRN-045", "error",
                           "backdrill.drill_diameter 必须大于原始 drill_diameter。",
                           "via_transition.backdrill.drill_diameter", "增大背钻孔径。"))
        if functional_span is not None and from_side == "auto" and top_stub and bottom_stub:
            issues.append(("VIA-TRN-046", "error",
                           "backdrill.from_side=auto 无法唯一推导方向：两侧都有残桩。",
                           "via_transition.backdrill.from_side", "显式指定 top 或 bottom。"))
        elif functional_span is not None and from_side == "top" and not top_stub:
            issues.append(("VIA-TRN-047", "error",
                           "backdrill.from_side=top 但不存在顶部残桩。",
                           "via_transition.backdrill.from_side", "检查上方功能连接层与机械钻孔顶部。"))
        elif functional_span is not None and from_side == "bottom" and not bottom_stub:
            issues.append(("VIA-TRN-048", "error",
                           "backdrill.from_side=bottom 但不存在底部残桩。",
                           "via_transition.backdrill.from_side", "检查下方功能连接层与机械钻孔底部。"))
        elif functional_span is not None and not top_stub and not bottom_stub:
            issues.append(("VIA-TRN-049", "error",
                           "backdrill.enabled=true 但不存在残桩（外层到外层贯通孔无需背钻）。",
                           "via_transition.backdrill.enabled", "外层到外层贯通孔应禁用背钻。"))
    elif functional_span is not None and (top_stub or bottom_stub):
        issues.append(("VIA-TRN-050", "error",
                       "存在未背钻残桩：内层退出必须启用 backdrill。",
                       "via_transition.backdrill.enabled", "启用 backdrill 或改为外层到外层贯通孔。"))

    padstack = vt.get("padstack", {})
    drill = float(padstack.get("drill_diameter", 0.0))
    finished = float(padstack.get("finished_hole_diameter", 0.0))
    plating = float(padstack.get("plating_thickness", 0.0))
    connected = float(padstack.get("connected_pad_diameter", 0.0))
    if finished >= drill:
        issues.append(("VIA-TRN-010", "error",
                       "finished_hole_diameter 必须小于 drill_diameter。",
                       "via_transition.padstack.finished_hole_diameter", "成品孔必须小于钻孔。"))
    if connected <= drill:
        issues.append(("VIA-TRN-011", "error",
                       "connected_pad_diameter 必须大于 drill_diameter。",
                       "via_transition.padstack.connected_pad_diameter", "功能焊盘必须大于钻孔。"))
    if abs(plating - (drill - finished) / 2.0) > 1e-9:
        issues.append(("VIA-TRN-012", "error",
                       "plating_thickness 必须等于钻孔与成品孔的半径差。",
                       "via_transition.padstack.plating_thickness", "修正镀铜厚度。"))

    # Canonical signal-antipad contract.  These rules deliberately use stable
    # VIA-TRN identifiers and point to the exact normalized parameter path.
    try:
        antipad = canonical_antipad(padstack.get("antipad", {}))
    except (TypeError, ValueError, OverflowError) as exc:
        issues.append(("VIA-TRN-080", "error", f"反焊盘配置无法解析：{exc}",
                       "via_transition.padstack.antipad", "所有尺寸必须是有限实数。"))
        antipad = None
    if antipad is not None:
        if antipad["topology"] not in TOPOLOGIES:
            issues.append(("VIA-TRN-081", "error", "antipad.topology 仅支持 independent/merged_pair。",
                           "via_transition.padstack.antipad.topology", "选择受支持的拓扑。"))
        if antipad["shape"] not in SHAPES:
            issues.append(("VIA-TRN-082", "error", "antipad.shape 仅支持 circle/ellipse/capsule/rounded_rectangle。",
                           "via_transition.padstack.antipad.shape", "选择受支持的形状。"))
        if antipad["direction_mode"] not in DIRECTION_MODES:
            issues.append(("VIA-TRN-093", "error", "antipad.direction_mode 仅支持 auto_pn/manual。",
                           "via_transition.padstack.antipad.direction_mode", "选择自动沿 P/N 轴或手动旋转。"))
        if antipad["size_mode"] not in SIZE_MODES:
            issues.append(("VIA-TRN-094", "error", "antipad.size_mode 仅支持 auto_fit/auto_plus_increment/manual。",
                           "via_transition.padstack.antipad.size_mode", "选择自动适配、自动适配＋用户增量或完全手动。"))

        def validate_shape(spec: Mapping[str, Any], prefix: str) -> None:
            shape = str(spec.get("shape", antipad["shape"]))
            topology = str(spec.get("topology", antipad["topology"]))
            if topology not in TOPOLOGIES:
                issues.append(("VIA-TRN-081", "error", f"{prefix}.topology={topology!r} 非法。",
                               prefix + ".topology", "选择 independent 或 merged_pair。"))
            if shape not in SHAPES:
                issues.append(("VIA-TRN-082", "error", f"{prefix}.shape={shape!r} 非法。",
                               prefix + ".shape", "选择受支持的形状。"))
                return
            required = ("diameter",) if shape == "circle" else ("length", "width")
            for field in required:
                value = spec.get(field, antipad.get(field))
                if not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) <= 0:
                    issues.append(("VIA-TRN-083", "error", f"{prefix}.{field} 必须为有限正数。",
                                   prefix + "." + field, "输入大于零的有限尺寸。"))
            rotation = spec.get("rotation_deg", antipad["rotation_deg"])
            if not isinstance(rotation, (int, float)) or not math.isfinite(float(rotation)):
                issues.append(("VIA-TRN-084", "error", f"{prefix}.rotation_deg 必须为有限实数。",
                               prefix + ".rotation_deg", "输入有限角度。"))
            direction_mode = str(spec.get("direction_mode", antipad["direction_mode"]))
            size_mode = str(spec.get("size_mode", antipad["size_mode"]))
            if direction_mode not in DIRECTION_MODES:
                issues.append(("VIA-TRN-093", "error", f"{prefix}.direction_mode={direction_mode!r} 非法。",
                               prefix + ".direction_mode", "选择 auto_pn 或 manual。"))
            if size_mode not in SIZE_MODES:
                issues.append(("VIA-TRN-094", "error", f"{prefix}.size_mode={size_mode!r} 非法。",
                               prefix + ".size_mode", "选择受支持的尺寸控制模式。"))
            for field in ("clearance", "end_clearance", "side_clearance",
                          "length_increment", "width_increment"):
                value = float(spec.get(field, antipad.get(field, 0.0)))
                if not math.isfinite(value) or value < 0:
                    issues.append(("VIA-TRN-092", "error", f"{prefix}.{field} 必须为有限非负数。",
                                   prefix + "." + field, "输入有限非负尺寸。"))
            if shape == "capsule" and float(spec.get("length", antipad["length"])) + 1e-12 < float(spec.get("width", antipad["width"])):
                issues.append(("VIA-TRN-085", "error", "capsule.length 必须不小于 width。",
                               prefix + ".length", "增大端到端长度或减小总宽度。"))
            if shape == "rounded_rectangle":
                length = float(spec.get("length", antipad["length"]))
                width = float(spec.get("width", antipad["width"]))
                radius = float(spec.get("corner_radius", antipad["corner_radius"]))
                if not math.isfinite(radius) or radius < 0 or radius > min(length, width) / 2.0 + 1e-12:
                    issues.append(("VIA-TRN-086", "error", "圆角半径必须满足 0 <= radius <= min(length,width)/2。",
                                   prefix + ".corner_radius", "修正圆角半径。"))

        validate_shape(antipad, "via_transition.padstack.antipad")
        if not math.isfinite(float(antipad.get("clearance", 0.0))) or float(antipad.get("clearance", 0.0)) < 0:
            issues.append(("VIA-TRN-092", "error", "antipad.clearance 必须为有限非负制造净距。",
                           "via_transition.padstack.antipad.clearance", "输入明确的最小铜边净距。"))
        per_layer = antipad.get("per_layer", {})
        for layer_name, override in per_layer.items():
            prefix = f"via_transition.padstack.antipad.per_layer.{layer_name}"
            if layer_name not in names:
                issues.append(("VIA-TRN-087", "error", f"逐层反焊盘层 {layer_name!r} 不存在。",
                               prefix, "选择叠层中存在的铜层。"))
                continue
            if drill_start in names and drill_end in names:
                span_names = names[names.index(drill_start):names.index(drill_end) + 1]
                if layer_name not in span_names or types.get(layer_name) not in {"signal", "plane"}:
                    issues.append(("VIA-TRN-088", "error", f"逐层反焊盘 {layer_name!r} 不在有效钻孔铜层跨度内。",
                                   prefix, "移除该覆盖或调整钻孔跨度。"))
            if not bool(override.get("inherit_global", False)):
                merged_spec = dict(antipad); merged_spec.update(override)
                override_shape = str(merged_spec.get("shape", "circle"))
                allowed_dimensions = {
                    "circle": {"diameter", "rotation_deg", "clearance", "end_clearance", "side_clearance",
                               "length_increment", "width_increment"},
                    "ellipse": {"length", "width", "rotation_deg", "clearance", "end_clearance", "side_clearance",
                                "length_increment", "width_increment"},
                    "capsule": {"length", "width", "rotation_deg", "clearance", "end_clearance", "side_clearance",
                                "length_increment", "width_increment"},
                    "rounded_rectangle": {"length", "width", "corner_radius", "rotation_deg", "clearance",
                                          "end_clearance", "side_clearance", "length_increment", "width_increment"},
                }.get(override_shape, set())
                incompatible = sorted(
                    key for key in override
                    if key in {"diameter", "length", "width", "corner_radius", "rotation_deg",
                               "clearance", "end_clearance", "side_clearance",
                               "length_increment", "width_increment"}
                    and key not in allowed_dimensions
                )
                if incompatible:
                    issues.append(("VIA-TRN-089", "error",
                                   f"逐层反焊盘 {layer_name!r} 含有与 {override_shape} 不匹配的字段："
                                   + ", ".join(incompatible) + "。",
                                   prefix, "删除无关字段或选择匹配的形状。"))
                validate_shape(merged_spec, prefix)
                layer_clearance = float(merged_spec.get("clearance", antipad.get("clearance", 0.0)))
                if not math.isfinite(layer_clearance) or layer_clearance < 0:
                    issues.append(("VIA-TRN-092", "error",
                                   f"逐层反焊盘 {layer_name!r} 的 clearance 必须为有限非负数。",
                                   prefix + ".clearance", "输入明确的最小铜边净距。"))

    # 信号孔中心距独立；输入/输出走线中心距分别由 width + GAP 派生。
    legacy_pair_pitch = float(vt.get("pair_pitch", 0.0))
    signal_via_pitch = float(vt.get("signal_via_pitch", legacy_pair_pitch))
    if signal_via_pitch <= 0:
        issues.append(("VIA-TRN-070", "error",
                       "signal_via_pitch 必须大于 0。",
                       "via_transition.signal_via_pitch", "信号孔中心距不能为非正。"))
    elif signal_via_pitch < connected:
        issues.append(("VIA-TRN-070", "error",
                       "signal_via_pitch 必须不小于功能焊盘直径，否则 P/N 信号孔功能焊盘重叠。",
                       "via_transition.signal_via_pitch", "增大信号孔中心距或减小功能焊盘直径。"))
    fanout = vt.get("fanout") if isinstance(vt.get("fanout"), Mapping) else {}
    fanout_style = str(fanout.get("style", "deg45"))
    fanout_direction = str(fanout.get("direction", "auto"))
    straight_length = float(fanout.get("straight_length", 1.0))
    if fanout_style not in ("deg45", "arc"):
        issues.append(("VIA-TRN-073", "error",
                       f"fanout.style={fanout_style!r} 非法；仅支持 deg45/arc。",
                       "via_transition.fanout.style", "选择 deg45 或 arc。"))
    if fanout_direction not in ("auto", "expand", "contract"):
        issues.append(("VIA-TRN-074", "error",
                       f"fanout.direction={fanout_direction!r} 非法；仅支持 auto/expand/contract。",
                       "via_transition.fanout.direction", "选择 auto/expand/contract。"))
    if straight_length < 0:
        issues.append(("VIA-TRN-071", "error",
                       "fanout.straight_length 必须 >= 0。",
                       "via_transition.fanout.straight_length", "耦合直线段长不能为负。"))
    side_geometry = []
    for side, label in (("entry", "输入"), ("exit", "输出")):
        trace = vt.get(f"{side}_trace", {})
        width = float(trace.get("width", 0.0))
        gap = float(trace.get("gap", legacy_pair_pitch - width))
        length = float(trace.get("length", 0.0))
        if gap <= 0:
            issues.append(("VIA-TRN-075", "error",
                           f"{label}走线 P/N 铜边间距 GAP 必须大于 0。",
                           f"via_transition.{side}_trace.gap", "输入大于 0 的铜边净距。"))
        side_geometry.append((side, label, length, width + gap))

    for side, label, length, trace_pitch in side_geometry:
        delta = (signal_via_pitch - trace_pitch) / 2.0
        if abs(delta) <= 1e-12 or straight_length < 0:
            continue
        fanout_x = abs(delta)
        if length + 1e-9 < fanout_x + straight_length:
            issues.append(("VIA-TRN-071", "error",
                           f"{label}走线长度 {shown_length(length)} 不足以容纳扇出"
                           f"（X 投影 {shown_length(fanout_x)}）与耦合直线段"
                           f"（{shown_length(straight_length)}）。",
                           f"via_transition.{side}_trace.length",
                           "增大走线长度、减小孔间距与走线中心距之差，或减小耦合直线段长。"))
        derived = "expand" if delta > 0 else "contract"
        if fanout_direction != "auto" and fanout_direction != derived:
            issues.append(("VIA-TRN-072", "warning",
                           f"旧配置的扇出方向为 {fanout_direction!r}，但{label}侧按实际间距差"
                           f"推导应为 {derived!r}；"
                           "几何仍按实际间距差生成。",
                           "via_transition.fanout.direction",
                           "保存时保留 auto，方向由输入/输出各侧间距差自动确定。"))

    rv = vt.get("return_vias", {})
    if rv.get("enabled"):
        layout_mode = str(rv.get("layout_mode", "advanced"))
        if layout_mode not in {"standard", "advanced"}:
            issues.append(("VIA-TRN-093", "error",
                           f"return_vias.layout_mode={layout_mode!r} 非法。",
                           "via_transition.return_vias.layout_mode",
                           "选择 standard 或 advanced。"))
        pattern = str(rv.get("pattern", ""))
        if pattern not in {"side_pair", "four_corner", "custom"}:
            issues.append(("VIA-TRN-023", "error", f"return_vias.pattern={pattern!r} 非法。",
                           "via_transition.return_vias.pattern", "选择 side_pair/four_corner/custom。"))
        count = int(rv.get("count", 0))
        coordinates = list(rv.get("custom_coordinates", []))
        if layout_mode == "standard":
            along = float(rv.get("relative_offset_along_route", 0.0))
            outward = float(rv.get("relative_offset_outward", 0.0))
            if not math.isfinite(along):
                issues.append(("VIA-TRN-094", "error",
                               "回流孔沿走线方向相对偏移必须为有限数。",
                               "via_transition.return_vias.relative_offset_along_route",
                               "输入有限长度值。"))
            if not math.isfinite(outward):
                issues.append(("VIA-TRN-095", "error",
                               "回流孔向外相对偏移必须为有限数。",
                               "via_transition.return_vias.relative_offset_outward",
                               "输入有限长度值；正值向外、负值向内。"))
            if pattern != "side_pair" or count != 2:
                issues.append(("VIA-TRN-096", "error",
                               "标准回流孔布局必须派生为 side_pair/count=2。",
                               "via_transition.return_vias.layout_mode",
                               "重新加载配置以恢复标准双孔派生值。"))
        if pattern == "side_pair" and (count <= 0 or count % 2):
            issues.append(("VIA-TRN-024", "error", "side_pair 的 count 必须为正偶数，且实际生成数等于配置数。",
                           "via_transition.return_vias.count", "输入 2、4、6…颗。"))
        if pattern == "four_corner" and count != 4:
            issues.append(("VIA-TRN-025", "error", "four_corner 固定生成 4 颗；count 是派生只读值。",
                           "via_transition.return_vias.count", "重新加载配置以派生 count=4。"))
        if pattern == "custom" and count != len(coordinates):
            issues.append(("VIA-TRN-026", "error", "custom 的 count 必须由坐标行数派生。",
                           "via_transition.return_vias.count", "编辑坐标矩阵，不要直接编辑 count。"))
        rv_drill = float(rv.get("drill_diameter", 0.0))
        rv_finished = float(rv.get("finished_hole_diameter", 0.0))
        rv_plating = float(rv.get("plating_thickness", 0.0))
        rv_antipad = float(rv.get("antipad_diameter", 0.0))
        rv_pad = float(rv.get("pad_diameter", 0.0))
        if rv_finished <= 0 or rv_drill <= rv_finished:
            issues.append(("VIA-TRN-027", "error", "Return VIA 必须满足 drill > finished_hole > 0。",
                           "via_transition.return_vias.drill_diameter", "增大钻孔或修正镀铜厚度。"))
        if abs(rv_finished - (rv_drill - 2.0 * rv_plating)) > 1e-9:
            issues.append(("VIA-TRN-028", "error", "Return VIA 成品孔必须由 drill - 2×plating 派生。",
                           "via_transition.return_vias.finished_hole_diameter", "重新加载以修正旧值。"))
        if rv_pad <= rv_drill:
            issues.append(("VIA-TRN-029", "error", "return_vias.pad_diameter 必须大于 drill_diameter。",
                           "via_transition.return_vias.pad_diameter", "增大回流孔焊盘。"))
        if rv_antipad <= rv_pad:
            issues.append(("VIA-TRN-020", "error",
                           "return_vias.antipad_diameter 必须大于 pad_diameter。",
                           "via_transition.return_vias.antipad_diameter", "增大回流孔避空。"))
        connects = [str(x) for x in rv.get("connect_layers", [])]
        rv_start, rv_end = str(rv.get("start_layer", "")), str(rv.get("end_layer", ""))
        rv_span: set[str] = set()
        if rv_start not in names or rv_end not in names or names.index(rv_start) >= names.index(rv_end):
            issues.append(("VIA-TRN-033", "error", "Return VIA 起止层必须存在且自上而下有序。",
                           "via_transition.return_vias.start_layer", "从当前叠层重新选择起止层。"))
        else:
            rv_span = set(names[names.index(rv_start):names.index(rv_end) + 1])
        if not connects:
            issues.append(("VIA-TRN-021", "error",
                           "return_vias.connect_layers 至少需要一个 GND 参考平面。",
                           "via_transition.return_vias.connect_layers", "指定回流孔连接层。"))
        for name in connects:
            if name not in planes:
                issues.append(("VIA-TRN-022", "error",
                               f"return_vias.connect_layers 中的 {name!r} 不是有效 GND 参考平面。",
                               "via_transition.return_vias.connect_layers", "选择参考平面。"))
            elif name not in rv_span:
                issues.append(("VIA-TRN-034", "error",
                               f"return_vias.connect_layers 中的 {name!r} 不在钻孔跨度内。",
                               "via_transition.return_vias.connect_layers", "只选择起止层之间的参考平面。"))

    if geometry is not None:
        if len(geometry.ports) != 4:
            issues.append(("VIA-TRN-030", "error",
                           f"期望4个差分端口，实际 {len(geometry.ports)} 个。",
                           "simulation.create_differential_ports", "检查端口生成。"))
        port_names = [port.name for port in geometry.ports]
        if len(set(port_names)) != len(port_names):
            issues.append(("VIA-TRN-031", "error", "端口名称重复。",
                           "simulation.create_differential_ports", "端口名称必须唯一。"))
        if len(geometry.signal_vias) != 2:
            issues.append(("VIA-TRN-032", "error",
                           f"期望2个信号孔（P/N），实际 {len(geometry.signal_vias)} 个。",
                           "via_transition.signal_via_pitch", "检查差分信号孔生成。"))
        for via in geometry.signal_vias:
            if via.backdrilled and abs(via.actual_residual_stub - via.target_residual_stub) > 1e-9:
                issues.append(("VIA-TRN-051", "error",
                               f"{via.name} 实际残桩与目标残桩不一致。",
                               "via_transition.backdrill.residual_stub", "检查背钻深度派生。"))
        contours = list(getattr(geometry, "antipad_contours", []))
        contour_by_layer: dict[str, list[Any]] = {}
        for contour in contours:
            contour_by_layer.setdefault(contour.layer, []).append(contour)
            b = contour.bounds
            pb = geometry.plane_bounds
            if b[0] < pb.x_min - 1e-9 or b[1] > pb.x_max + 1e-9 or b[2] < pb.y_min - 1e-9 or b[3] > pb.y_max + 1e-9:
                issues.append(("VIA-TRN-091", "error", f"{contour.layer} 旋转后反焊盘超出 plane 建模范围。",
                               "via_transition.padstack.antipad", "增大 plane 余量或减小反焊盘。"))
        for via in geometry.signal_vias:
            for layer_name in via.antipad_layers:
                candidates = [c for c in contour_by_layer.get(layer_name, [])
                              if c.owner in {via.signal, "merged_pair"}]
                clearance = max((float(c.clearance) for c in candidates), default=0.0)
                effective_hole = via.drill_diameter
                if via.backdrilled and via.per_layer_states.get(layer_name) == "backdrilled_removed":
                    effective_hole = max(effective_hole, via.backdrill_diameter)
                required_radius = effective_hole / 2.0 + clearance
                if not candidates or not any(contains_circle(c, (via.x, via.y), required_radius) for c in candidates):
                    detail = ""
                    merged_circle = next((c for c in candidates
                                          if c.owner == "merged_pair" and c.shape == "circle"), None)
                    if merged_circle is not None:
                        pitch = math.hypot(
                            geometry.signal_vias[0].x - geometry.signal_vias[1].x,
                            geometry.signal_vias[0].y - geometry.signal_vias[1].y,
                        )
                        minimum = minimum_merged_circle_diameter(pitch, effective_hole, clearance)
                        detail = (f" 当前直径 {shown_length(merged_circle.diameter)}，"
                                  f"最小直径 {shown_length(minimum)}，"
                                  f"不足 {shown_length(max(0.0, minimum - merged_circle.diameter))}。")
                    merged_shape = next((c for c in candidates if c.owner == "merged_pair"), None)
                    if merged_shape is not None and merged_shape.shape != "circle":
                        length_short = max(0.0, merged_shape.required_length - merged_shape.length)
                        width_short = max(0.0, merged_shape.required_width - merged_shape.width)
                        p_short = max(0.0, clearance - merged_shape.p_clearance)
                        n_short = max(0.0, clearance - merged_shape.n_clearance)
                        detail = (
                            f" 当前长度/宽度 {shown_length(merged_shape.length)}/"
                            f"{shown_length(merged_shape.width)}，所需最小长度/宽度 "
                            f"{shown_length(merged_shape.required_length)}/{shown_length(merged_shape.required_width)}；"
                            f"P/N 侧实际净距 {shown_length(merged_shape.p_clearance)}/"
                            f"{shown_length(merged_shape.n_clearance)}，长度/宽度不足 "
                            f"{shown_length(length_short)}/{shown_length(width_short)}，"
                            f"P/N 净距不足 {shown_length(p_short)}/{shown_length(n_short)}。"
                        )
                        if merged_shape.shape == "capsule":
                            detail += (
                                f" 当前合并胶囊未完整包含 P/N 孔及要求净距：长度至少需要"
                                f"{shown_length(merged_shape.required_length)}，"
                                f"当前{shown_length(merged_shape.length)}。"
                            )
                    issues.append(("VIA-TRN-090", "error",
                                   f"{layer_name} 反焊盘未完整包含 {via.name} 孔桶/背钻投影并保留 "
                                   f"{shown_length(clearance)} 最小铜边净距。{detail}",
                                   "via_transition.padstack.antipad", "增大有效直径/长宽或调整旋转与拓扑。"))
        # 端口与差分对（规格 §7、§8.4）。
        if len(geometry.differential_pairs) != 2:
            issues.append(("VIA-TRN-060", "error",
                           f"期望2个差分对（IN/OUT），实际 {len(geometry.differential_pairs)} 个。",
                           "simulation.create_differential_ports", "检查差分对定义。"))
        for port in geometry.ports:
            if not port.reference_layer or not port.reference_conductor:
                issues.append(("VIA-TRN-061", "error",
                               f"端口 {port.name} 缺少参考导体/参考层。",
                               "simulation.create_differential_ports", "为端口指定有效 GND 参考平面。"))
            elif port.reference_layer not in planes:
                issues.append(("VIA-TRN-062", "error",
                               f"端口 {port.name} 的参考层 {port.reference_layer!r} 不是有效参考平面。",
                               "simulation.create_differential_ports", "选择有效 GND 参考平面。"))
            if abs(float(port.integration_line_start[2]) - float(port.integration_line_end[2])) < 1e-12:
                issues.append(("VIA-TRN-063", "error",
                               f"端口 {port.name} 积分线起止点 Z 相同，未指向参考导体。",
                               "simulation.create_differential_ports", "积分线应从信号指向参考导体。"))
        if rv.get("enabled"):
            connected_refs = {str(name) for name in rv.get("connect_layers", [])}
            required_refs = {str(port.reference_layer) for port in geometry.ports if port.reference_layer}
            missing_refs = sorted(required_refs.difference(connected_refs))
            if missing_refs:
                issues.append(("VIA-TRN-064", "error",
                               "回流孔未连接端口使用的参考平面：" + ", ".join(missing_refs) + "。",
                               "via_transition.return_vias.connect_layers",
                               "把输入/输出端口参考层都加入回流孔连接层，保证截断模型内存在连续回流路径。"))
    return issues
