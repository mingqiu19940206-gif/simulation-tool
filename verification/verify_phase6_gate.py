"""Phase 6 门禁离线验证（纯 Python，不依赖 tkinter/shapely/AEDT）。

覆盖：VIA 安全数值轴/拓扑护栏/残桩跨界/预算、目标评分、排名与帕累托、
9 个 Variation 的 3×3 评分演示，以及 surface_capacitor 回归 sanity。
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from config_manager import ConfigManager
from via_transition_sweeps import (
    VIA_SAFE_SWEEP_PATHS,
    VIA_TOPOLOGY_SWEEP_PATHS,
    inspect_via_sweep_plan,
    iter_via_variations,
)
from via_objective import (
    SpecMask,
    build_scoring_report,
    build_sweep_summary,
    export_scoring_csv,
    export_scoring_json,
    export_sweep_summary,
    rank_scores,
    score_sweep_results,
)

manager = ConfigManager(ROOT)
results = []
failures = []


def check(name, condition, detail=""):
    results.append({"name": name, "ok": bool(condition), "detail": detail})
    if not condition:
        failures.append(name)
    print(("PASS" if condition else "FAIL"), "-", name, ("| " + detail if detail else ""))


MASK = SpecMask(frequency_start_ghz=1.0, frequency_stop_ghz=64.0,
                return_loss_limit_db=-15.0, insertion_loss_limit_db=-1.0,
                mode_conversion_limit_db=-20.0, normalization_db=1.0)


def via_cfg():
    cfg = copy.deepcopy(manager.default())
    cfg["scenario"]["type"] = "via_transition"
    return cfg


# ---- 轴注册（规格 §11.1/§11.2）----
check("phase6_safe_axes_9", len(VIA_SAFE_SWEEP_PATHS) == 9)
check("phase6_topology_axes_11", len(VIA_TOPOLOGY_SWEEP_PATHS) == 11)

# ---- 3×3 计划与 Variation ----
cfg = via_cfg()
cfg["sweeps"]["via_transition.pair_pitch"] = {"enabled": True, "start": 0.6, "stop": 1.0, "step": 0.2}
cfg["sweeps"]["via_transition.padstack.antipad_diameter"] = {"enabled": True, "start": 0.6, "stop": 0.8, "step": 0.1}
plan = inspect_via_sweep_plan(cfg)
check("phase6_plan_9_variations", plan.case_count == 9 and not plan.issues)
variations = iter_via_variations(plan)
check("phase6_variations_carry_params", len(variations) == 9
      and all({"via_transition.pair_pitch", "via_transition.padstack.antipad_diameter"} <= set(v.keys())
              for v in variations))

# ---- 拓扑护栏 ----
topo = via_cfg()
topo["sweeps"]["via_transition.exit_layer"] = {"enabled": True, "values": ["L10", "L12"]}
topo_plan = inspect_via_sweep_plan(topo)
check("phase6_topology_blocked", topo_plan.topology_axes
      and any(level == "error" and "拓扑" in m for level, m in topo_plan.issues))

# ---- 预算 ----
budget = via_cfg()
budget["sweeps"]["via_transition.pair_pitch"] = {"enabled": True, "start": 0.2, "stop": 2.0, "step": 0.2}
budget["sweeps"]["via_transition.padstack.antipad_diameter"] = {"enabled": True, "start": 0.2, "stop": 2.0, "step": 0.2}
budget_plan = inspect_via_sweep_plan(budget, max_cases=50)
check("phase6_budget_blocked", any(level == "error" and "上限" in m for level, m in budget_plan.issues))

# ---- 残桩跨界 ----
stub = via_cfg()
stub["sweeps"]["via_transition.backdrill.residual_stub"] = {"enabled": True, "start": 0.1, "stop": 3.0, "step": 1.0}
stub_plan = inspect_via_sweep_plan(stub)
check("phase6_residual_stub_guard", any(level == "error" and "铜层" in m for level, m in stub_plan.issues))

# ---- 目标评分：手工可复核 ----
freq = [2.0, 4.0, 8.0]
bad = score_sweep_results([{
    "variation": {"k": "bad"}, "frequency_ghz": freq,
    "Sdd11_db": [-10.0] * 3, "Sdd21_db": [-3.0] * 3,
    "Sdc21_db": [-10.0] * 3, "Scd21_db": [-10.0] * 3,
}], MASK)[0]
check("phase6_hand_penalties", bad.penalties["P_RL"].normalized == 5.0
      and bad.penalties["P_IL"].normalized == 2.0
      and bad.penalties["P_DC"].normalized == 10.0
      and bad.penalties["P_CD"].normalized == 10.0
      and abs(bad.objective - 27.0) < 1e-9)

# ---- 排名与帕累托 ----
def make_result(key, rl, il):
    return {"variation": {"k": key}, "frequency_ghz": freq,
            "Sdd11_db": [rl] * 3, "Sdd21_db": [il] * 3,
            "Sdc21_db": [-60.0] * 3, "Scd21_db": [-60.0] * 3}

scored = score_sweep_results([
    make_result(1, -15.0, -6.0), make_result(2, -10.0, -1.0),
    make_result(3, -12.0, -4.0), make_result(4, -10.0, -6.0),
], MASK)
ranking = rank_scores(scored)
check("phase6_ranking_matches_hand", [s.variation["k"] for s in ranking] == [1, 2, 3, 4]
      and ranking[0].objective <= ranking[1].objective)
report = build_scoring_report([
    make_result(1, -15.0, -6.0), make_result(2, -10.0, -1.0),
    make_result(3, -12.0, -4.0), make_result(4, -10.0, -6.0),
], MASK)
check("phase6_pareto_nonempty", sorted(s.variation["k"] for s in report.pareto) == [1, 2, 3])

# ---- 9 个 Variation 均有参数、曲线与评分 ----
sweep_results = []
for index, variation in enumerate(variations):
    sweep_results.append({
        "variation": variation,
        "config_hash": f"cfg{index}", "geometry_hash": f"geo{index}",
        "frequency_ghz": freq,
        "Sdd11_db": [-40.0 + index] * 3, "Sdd21_db": [-0.1] * 3,
        "Sdc21_db": [-60.0] * 3, "Scd21_db": [-60.0] * 3,
    })
summary = build_sweep_summary(sweep_results, MASK)
check("phase6_9_scored", len(summary["variations"]) == 9
      and all(v["frequency_ghz"] and "objective" in v and "rank" in v for v in summary["variations"]))
check("phase6_summary_embeds_curves", all(len(v["Sdd11_db"]) == 3 for v in summary["variations"]))

# ---- 导出 ----
out_dir = ROOT / "verification" / "phase6_tmp"
out_dir.mkdir(parents=True, exist_ok=True)
export_sweep_summary(summary, out_dir / "sweep_results_summary.json")
export_scoring_json(report, out_dir / "scoring.json")
export_scoring_csv(report, out_dir / "scoring.csv")
loaded = json.loads((out_dir / "sweep_results_summary.json").read_text(encoding="utf-8"))
check("phase6_summary_json_round_trip", loaded["variations"][0]["variation"]
      and loaded["mask"]["return_loss_limit_db"] == -15.0)
check("phase6_scoring_csv_written", (out_dir / "scoring.csv").is_file())

# ---- surface_capacitor 回归 sanity ----
from simulation_sweep_semantics import inspect_sweep_plan  # noqa: E402
sc = copy.deepcopy(manager.default())
sc["sweeps"]["capacitor.cap_pitch"] = {"enabled": True, "start": 0.5, "stop": 0.7, "step": 0.1}
sc_plan = inspect_sweep_plan(sc)
check("phase6_surface_capacitor_sweep_ok", sc_plan.case_count == 3 and not sc_plan.topology_axes)

summary_out = {"passed": len(results) - len(failures), "failed": len(failures),
               "total": len(results), "failures": failures, "checks": results}
out = ROOT / "verification" / "phase6_gate_result.json"
out.write_text(json.dumps(summary_out, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n=== 汇总：{summary_out['passed']}/{summary_out['total']} 通过 ===")
print("WROTE", out)
sys.exit(1 if failures else 0)
