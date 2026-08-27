"""Phase 5 门禁离线验证（纯 Python，不依赖 tkinter/shapely/AEDT）。

覆盖：差分 TDR 离线 IFT 算法、理想负载/开路/短路的方向与阻抗、非 dB IFFT、
DC 外推、距离轴、设置校验、TDR 导出往返，以及 Phase 4 回归 sanity。
"""
from __future__ import annotations

import copy
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from config_manager import ConfigManager
from geometry_calculator import GeometryCalculator
from via_transition_tdr import (
    compute_tdr,
    default_tdr_settings,
    export_tdr_csv,
    export_tdr_json,
    known_network_sdd11,
    validate_tdr_settings,
)
from via_transition_results import (
    build_via_results_from_single_ended,
    run_tdr_from_result,
)

manager = ConfigManager(ROOT)
results = []
failures = []


def check(name, condition, detail=""):
    results.append({"name": name, "ok": bool(condition), "detail": detail})
    if not condition:
        failures.append(name)
    print(("PASS" if condition else "FAIL"), "-", name, ("| " + detail if detail else ""))


ZREF = 100.0


def tdr_of(kind, **settings):
    freq, gamma = known_network_sdd11(kind, n_points=128, f_max_ghz=64.0)
    return compute_tdr(freq, gamma, settings if settings else None)


# ---- TDR 设置完整 ----
settings = default_tdr_settings()
required = ("input_curve", "differential_reference_impedance", "rise_time_ps",
            "dc_extrapolation", "window_function", "causality",
            "propagation_velocity_m_s", "distance_axis")
check("phase5_settings_complete", all(k in settings for k in required), str(sorted(settings)))
check("phase5_settings_valid", not validate_tdr_settings(settings))

# ---- 已知网络：负载 / 开路 / 短路 ----
load = tdr_of("matched_load")
check("phase5_load_zref", max(abs(z - ZREF) for z in load.impedance_ohm) < 1e-6)
open_r = tdr_of("open")
check("phase5_open_direction", open_r.reflection[-1] > 0.99
      and open_r.impedance_ohm[-1] > 10.0 * ZREF,
      f"rho={open_r.reflection[-1]:.4f} Z={open_r.impedance_ohm[-1]:.1f}")
short = tdr_of("short")
check("phase5_short_direction", short.reflection[-1] < -0.99
      and abs(short.impedance_ohm[-1]) < 0.01 * ZREF,
      f"rho={short.reflection[-1]:.4f} Z={short.impedance_ohm[-1]:.3f}")

# ---- 非 dB IFFT（复数相位敏感）----
m = 128
f_max = 64.0
df = f_max / m
freq = [(k + 1) * df for k in range(m)]
tau = 0.05
g_a = [0.5 + 0j] * m
g_b = [0.5 * complex(math.cos(-2 * math.pi * f * tau), math.sin(-2 * math.pi * f * tau))
       for f in freq]
same_mag = all(abs(abs(a) - abs(b)) < 1e-12 for a, b in zip(g_a, g_b))
r_a = compute_tdr(freq, g_a, {"window_function": "none", "rise_time_ps": 0})
r_b = compute_tdr(freq, g_b, {"window_function": "none", "rise_time_ps": 0})
check("phase5_not_db_ifft", same_mag
      and any(abs(a - b) > 1e-6 for a, b in zip(r_a.impedance_ohm, r_b.impedance_ohm)))

# ---- 常数反射 → Zref 比例 ----
const_freq = [k + 1.0 for k in range(64)]
const_gamma = [0.5 + 0j] * 64
const_tdr = compute_tdr(const_freq, const_gamma, {"window_function": "none", "rise_time_ps": 0})
check("phase5_constant_ratio", abs(const_tdr.reflection[-1] - 0.5) < 1e-6
      and abs(const_tdr.impedance_ohm[-1] - 3.0 * ZREF) < 1e-2)

# ---- DC 外推 ----
ramp = [(0.9 - 0.1 * f) + 0j for f in const_freq]
r_lin = compute_tdr(const_freq, ramp, {"window_function": "none", "rise_time_ps": 0,
                                       "dc_extrapolation": "linear"})
r_con = compute_tdr(const_freq, ramp, {"window_function": "none", "rise_time_ps": 0,
                                       "dc_extrapolation": "constant"})
check("phase5_dc_extrapolation", abs(r_lin.reflection[-1] - 0.9) < 1e-6
      and abs(r_con.reflection[-1] - 0.8) < 1e-6)

# ---- 距离轴 ----
dist = tdr_of("open", propagation_velocity_m_s=1.5e8)
check("phase5_distance_axis", bool(dist.distance_mm)
      and dist.settings["distance_axis"] == "distance"
      and abs(dist.distance_mm[-1] - 75.0) < 1.0)

# ---- fail closed：非均匀网格 ----
try:
    compute_tdr([1.0, 2.0, 5.0], [0.1 + 0j] * 3)
    check("phase5_nonuniform_fail_closed", False, "未抛异常")
except ValueError:
    check("phase5_nonuniform_fail_closed", True)

# ---- 结果集成 + TDR 导出往返 ----
t = 0.5 + 0j
se = [[0j, 0j, 0j, 0j], [0j, 0j, 0j, 0j], [t, 0j, 0j, 0j], [0j, t, 0j, 0j]]
vresult = build_via_results_from_single_ended(
    frequency_ghz=[10.0, 20.0], se_matrices=[se, se],
    config_hash="cfg", geometry_hash="geo",
)
check("phase5_result_carries_complex_sdd11", bool(vresult.Sdd11_re) and bool(vresult.Sdd11_im))
tdr = run_tdr_from_result(vresult)
check("phase5_run_tdr_from_result", max(abs(z - ZREF) for z in tdr.impedance_ohm) < 1e-6)
out_dir = ROOT / "verification" / "phase5_tmp"
out_dir.mkdir(parents=True, exist_ok=True)
tdr_json = out_dir / "via_tdr.json"
tdr_csv = out_dir / "via_tdr.csv"
export_tdr_json(tdr, tdr_json)
export_tdr_csv(tdr, tdr_csv)
loaded = json.loads(tdr_json.read_text(encoding="utf-8"))
check("phase5_tdr_json_round_trip", loaded["settings"]["input_curve"] == "SDD11"
      and len(loaded["time_ns"]) == len(loaded["impedance_ohm"]))
check("phase5_tdr_csv_written", tdr_csv.is_file() and "impedance_ohm" in tdr_csv.read_text(encoding="utf-8"))

# ---- surface_capacitor 回归 sanity ----
sc = copy.deepcopy(manager.default())
sc_geo = GeometryCalculator(sc).calculate()
check("phase5_surface_capacitor_ok", sc_geo is not None and not getattr(sc_geo, "signal_vias", None))

summary = {"passed": len(results) - len(failures), "failed": len(failures),
           "total": len(results), "failures": failures, "checks": results}
out = ROOT / "verification" / "phase5_gate_result.json"
out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n=== 汇总：{summary['passed']}/{summary['total']} 通过 ===")
print("WROTE", out)
sys.exit(1 if failures else 0)
