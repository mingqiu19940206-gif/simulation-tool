"""Phase 3 门禁离线验证（纯 Python，不依赖 tkinter/shapely/AEDT）。

覆盖 Phase 2 回归 + Phase 3 背钻/残桩的几何派生、语义校验、fail-closed、
快照往返、示例往返与 surface_capacitor 回归 sanity。
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from config_manager import (
    ConfigManager, make_generation_snapshot, validate_generation_snapshot,
)
from geometry_calculator import GeometryCalculator
from via_transition_semantics import validate_via_transition
from scenario_semantics import (
    VIA_TRANSITION, scenario_type, require_sweep_supported_scenario,
    ScenarioNotImplementedError,
)

manager = ConfigManager(ROOT)
results = []
failures = []


def check(name, condition, detail=""):
    results.append({"name": name, "ok": bool(condition), "detail": detail})
    if not condition:
        failures.append(name)
    print(("PASS" if condition else "FAIL"), "-", name, ("| " + detail if detail else ""))


def cfg_phase2():
    cfg = copy.deepcopy(manager.default())
    cfg["scenario"]["type"] = "via_transition"
    cfg["via_transition"]["exit_layer"] = "L14"
    cfg["via_transition"]["backdrill"]["enabled"] = False
    return cfg


def cfg_phase3():
    cfg = copy.deepcopy(manager.default())
    cfg["scenario"]["type"] = "via_transition"
    cfg["via_transition"]["exit_layer"] = "L10"
    cfg["via_transition"]["backdrill"]["enabled"] = True
    cfg["via_transition"]["backdrill"]["from_side"] = "auto"
    return cfg


def layer(geometry, name):
    for l in geometry.stackup:
        if l.name == name:
            return l
    raise KeyError(name)


# ---- Phase 2 回归（外层到外层、无背钻）----
p2 = cfg_phase2()
g2 = GeometryCalculator(p2).calculate()
i2 = validate_via_transition(p2, g2)
check("phase2_regression_no_errors", not g2.errors and not [x for x in i2 if x[1] == "error"],
      f"geo={g2.errors[:1]} sem={[x[0] for x in i2 if x[1]=='error']}")
check("phase2_signal_vias_2_ports_4", len(g2.signal_vias) == 2 and len(g2.ports) == 4)

# ---- Phase 3：背钻几何派生 ----
p3 = cfg_phase3()
g3 = GeometryCalculator(p3).calculate()
i3 = validate_via_transition(p3, g3)
check("phase3_valid_no_errors", not g3.errors and not [x for x in i3 if x[1] == "error"],
      f"geo={g3.errors[:1]} sem={[x[0] for x in i3 if x[1]=='error']}")
exit_l10 = layer(g3, "L10")
all_backdrilled_bottom = all(
    v.backdrilled and v.backdrill_from_side == "bottom" for v in g3.signal_vias)
check("phase3_backdrill_bottom", all_backdrilled_bottom)
check("phase3_effective_barrel_removes_stub", all(
    abs(v.effective_barrel_z_bottom - (exit_l10.z_bottom - v.target_residual_stub)) < 1e-6
    and v.effective_barrel_z_bottom > v.z_bottom
    for v in g3.signal_vias))
check("phase3_target_equals_actual_stub", all(
    abs(v.actual_residual_stub - v.target_residual_stub) < 1e-9 for v in g3.signal_vias))
check("phase3_backdrill_diameter_gt_drill", all(
    v.backdrill_diameter > v.drill_diameter for v in g3.signal_vias))

# ---- Phase 3 fail-closed ----
bad_bd = cfg_phase3()
bad_bd["via_transition"]["backdrill"]["drill_diameter"] = 0.15
gb = GeometryCalculator(bad_bd).calculate()
ib = validate_via_transition(bad_bd, gb)
check("phase3_bd_diameter_rejected", any("必须大于" in e for e in gb.errors)
      and any(x[0] == "VIA-TRN-045" for x in ib))

no_bd = cfg_phase3()
no_bd["via_transition"]["backdrill"]["enabled"] = False
gn = GeometryCalculator(no_bd).calculate()
inn = validate_via_transition(no_bd, gn)
check("phase3_internal_exit_without_backdrill_rejected", any("残桩" in e for e in gn.errors)
      and any(x[0] == "VIA-TRN-050" for x in inn))

both = cfg_phase3()
both["via_transition"]["entry_layer"] = "L3"
gboth = GeometryCalculator(both).calculate()
iboth = validate_via_transition(both, gboth)
check("phase3_auto_ambiguous_both_stubs", any("无法唯一" in e for e in gboth.errors)
      and any(x[0] == "VIA-TRN-046" for x in iboth))

# ---- 扫描 fail closed（Phase 6）----
try:
    require_sweep_supported_scenario(p3)
    check("sweep_fail_closed", False, "未抛出异常")
except ScenarioNotImplementedError as exc:
    check("sweep_fail_closed", "Phase 6" in str(exc), str(exc)[:50])

# ---- 快照往返（Phase 3 背钻信息保留）----
snap = make_generation_snapshot(p3, g3)
validate_generation_snapshot(snap)
sv = snap["geometry_snapshot"]["signal_vias"][0]
check("phase3_snapshot_round_trip_has_backdrill",
      sv.get("backdrilled") is True and abs(sv["effective_barrel_z_bottom"]
      - (exit_l10.z_bottom - sv["target_residual_stub"])) < 1e-6)

# ---- 示例往返 ----
example = ROOT / "examples" / "via_transition_backdrill.json"
if example.is_file():
    loaded, _ = manager.load(example)
    check("example_backdrill_exists", example.is_file())
    check("example_backdrill_scenario", loaded.get("scenario", {}).get("type") == "via_transition")
    check("example_backdrill_exit_l10", loaded.get("via_transition", {}).get("exit_layer") == "L10")
    check("example_backdrill_enabled", loaded.get("via_transition", {}).get("backdrill", {}).get("enabled") is True)
else:
    check("example_backdrill_exists", False, "文件尚未生成")

# ---- surface_capacitor 回归 ----
sc = copy.deepcopy(manager.default())
sc_geo = GeometryCalculator(sc).calculate()
check("surface_capacitor_dispatch_ok", scenario_type(sc) == "surface_capacitor" and sc_geo is not None)
check("surface_capacitor_not_via", not getattr(sc_geo, "signal_vias", None))

summary = {"passed": len(results) - len(failures), "failed": len(failures),
           "total": len(results), "failures": failures, "checks": results}
out = ROOT / "verification" / "phase3_gate_result.json"
out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n=== 汇总：{summary['passed']}/{summary['total']} 通过 ===")
print("WROTE", out)
sys.exit(1 if failures else 0)
