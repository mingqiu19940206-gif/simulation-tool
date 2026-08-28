"""Phase 4 门禁离线验证（纯 Python，不依赖 tkinter/shapely/AEDT）。

覆盖：四端口几何与差分对、混合模 S 参数转换、结果构造/校验/JSON+CSV 导出、
快照携带差分对，以及 surface_capacitor 回归 sanity。
"""
from __future__ import annotations

import copy
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from config_manager import ConfigManager, make_generation_snapshot
from geometry_calculator import GeometryCalculator
from via_transition_semantics import validate_via_transition
from via_transition_results import (
    compute_mixed_mode_smatrix, mixed_mode_metrics,
    build_via_results_from_single_ended, validate_via_results,
    export_via_results_json, export_via_results_csv,
)

manager = ConfigManager(ROOT)
results = []
failures = []


def check(name, condition, detail=""):
    results.append({"name": name, "ok": bool(condition), "detail": detail})
    if not condition:
        failures.append(name)
    print(("PASS" if condition else "FAIL"), "-", name, ("| " + detail if detail else ""))


def via_cfg():
    cfg = copy.deepcopy(manager.default())
    cfg["scenario"]["type"] = "via_transition"
    cfg["via_transition"]["exit_layer"] = "L14"
    cfg["via_transition"]["backdrill"]["enabled"] = False
    return cfg


# ---- 端口与差分对 ----
cfg = via_cfg()
geo = GeometryCalculator(cfg).calculate()
issues = validate_via_transition(cfg, geo)
check("phase4_four_ports", len(geo.ports) == 4,
      f"ports={len(geo.ports)}")
check("phase4_port_reference_present", all(p.reference_layer and p.reference_conductor for p in geo.ports))
check("phase4_two_differential_pairs",
      len(geo.differential_pairs) == 2
      and {p["name"] for p in geo.differential_pairs} == {"IN", "OUT"})
check("phase4_port_validation_no_errors", not [i for i in issues if i[1] == "error"],
      str([i[0] for i in issues if i[1] == "error"]))

# ---- 混合模 S 参数 ----
t = 0.8 + 0.0j
se = [[0j, 0j, 0j, 0j], [0j, 0j, 0j, 0j], [t, 0j, 0j, 0j], [0j, t, 0j, 0j]]
mm = compute_mixed_mode_smatrix(se)
m = mixed_mode_metrics(mm)
check("phase4_mixed_mode_sdd21", abs(m["SDD21"] - t) < 1e-9, f"SDD21={m['SDD21']}")
check("phase4_mixed_mode_no_conversion",
      abs(m["SDC21"]) < 1e-9 and abs(m["SCD21"]) < 1e-9 and abs(m["SDD11"]) < 1e-9)

# ---- 结果构造与校验 ----
result = build_via_results_from_single_ended(
    frequency_ghz=[10.0, 20.0], se_matrices=[se, se],
    config_hash="cfg", geometry_hash="geo", software_version="2.4.0",
)
expected_db = 20.0 * math.log10(0.8)
check("phase4_build_results_db", abs(result.Sdd21_db[0] - expected_db) < 1e-6
      and abs(result.Sdd21_db[1] - expected_db) < 1e-6)
check("phase4_results_hash_present", result.config_hash == "cfg" and result.geometry_hash == "geo")
check("phase4_validate_results_ok", not validate_via_results(result.to_dict()))

# ---- JSON/CSV 导出（写到工作区临时目录，避开系统 temp）----
out_dir = ROOT / "verification" / "phase4_tmp"
out_dir.mkdir(parents=True, exist_ok=True)
json_path = out_dir / "via_results.json"
csv_path = out_dir / "via_results.csv"
export_via_results_json(result, json_path)
export_via_results_csv(result, csv_path)
loaded = json.loads(json_path.read_text(encoding="utf-8"))
check("phase4_json_round_trip", loaded["Sdd21_db"][0] == result.Sdd21_db[0]
      and loaded["config_hash"] == "cfg")
check("phase4_csv_written", csv_path.is_file() and "frequency_ghz" in csv_path.read_text(encoding="utf-8"))

# ---- 快照携带差分对 ----
snap = make_generation_snapshot(cfg, geo)
check("phase4_snapshot_has_differential_pairs",
      len(snap["geometry_snapshot"].get("differential_pairs", [])) == 2)

# ---- surface_capacitor 回归 ----
sc = copy.deepcopy(manager.default())
sc_geo = GeometryCalculator(sc).calculate()
check("phase4_surface_capacitor_ok", sc_geo is not None and not getattr(sc_geo, "signal_vias", None))

summary = {"passed": len(results) - len(failures), "failed": len(failures),
           "total": len(results), "failures": failures, "checks": results}
out = ROOT / "verification" / "phase4_gate_result.json"
out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n=== 汇总：{summary['passed']}/{summary['total']} 通过 ===")
print("WROTE", out)
sys.exit(1 if failures else 0)
