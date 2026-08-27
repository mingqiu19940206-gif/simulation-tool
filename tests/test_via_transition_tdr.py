"""Phase 5 差分 TDR：离线 IFT 算法、已知网络数值测试与设置校验（纯 Python）。

覆盖规格 §10.4 / §13 Phase 5：
- 不对 dB 曲线直接 IFFT（复数相位敏感）。
- 理想负载/开路/短路的方向与阻抗结果正确。
- TDR 设置完整且可校验。
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from via_transition_tdr import (
    TdrResult,
    compute_tdr,
    default_tdr_settings,
    known_network_sdd11,
    resolve_tdr_settings,
    validate_tdr_settings,
)
from via_transition_results import (
    build_via_results_from_single_ended,
    run_tdr_from_result,
)

ZREF = 100.0


class ViaTransitionTdrTests(unittest.TestCase):
    def _compute(self, kind, **settings):
        freq, gamma = known_network_sdd11(kind, n_points=128, f_max_ghz=64.0)
        return compute_tdr(freq, gamma, settings if settings else None)

    def test_01_matched_load_impedance_is_zref(self) -> None:
        result = self._compute("matched_load")
        self.assertGreater(len(result.time_ns), 0)
        self.assertLess(max(abs(z - ZREF) for z in result.impedance_ohm), 1e-6)
        self.assertLess(max(abs(r) for r in result.reflection), 1e-9)

    def test_02_open_direction_high_impedance(self) -> None:
        result = self._compute("open")
        self.assertGreater(result.reflection[-1], 0.99)
        self.assertGreater(result.impedance_ohm[-1], 10.0 * ZREF)

    def test_03_short_direction_low_impedance(self) -> None:
        result = self._compute("short")
        self.assertLess(result.reflection[-1], -0.99)
        self.assertLess(abs(result.impedance_ohm[-1]), 0.01 * ZREF)

    def test_04_not_direct_ifft_of_db(self) -> None:
        # 相同 |Γ|=0.5（dB 曲线一致）但相位不同 → TDR 曲线必须不同。
        m = 128
        f_max = 64.0
        df = f_max / m
        freq = [(k + 1) * df for k in range(m)]
        tau = 0.05  # ns，延迟
        g_a = [0.5 + 0.0j] * m
        g_b = [0.5 * complex(math.cos(-2 * math.pi * f * tau),
                             math.sin(-2 * math.pi * f * tau)) for f in freq]
        self.assertTrue(all(abs(abs(a) - abs(b)) < 1e-12 for a, b in zip(g_a, g_b)))
        r_a = compute_tdr(freq, g_a, {"window_function": "none", "rise_time_ps": 0})
        r_b = compute_tdr(freq, g_b, {"window_function": "none", "rise_time_ps": 0})
        self.assertTrue(any(abs(a - b) > 1e-6 for a, b in zip(r_a.impedance_ohm, r_b.impedance_ohm)))

    def test_05_constant_reflection_reaches_zref_ratio(self) -> None:
        # Γ = 0.5 常数 → Z = Zref·(1+0.5)/(1-0.5) = 3·Zref = 300 Ω。
        m = 64
        freq = [k + 1.0 for k in range(m)]
        gamma = [0.5 + 0.0j] * m
        result = compute_tdr(freq, gamma, {"window_function": "none", "rise_time_ps": 0})
        self.assertAlmostEqual(result.reflection[-1], 0.5, places=6)
        self.assertAlmostEqual(result.impedance_ohm[-1], 3.0 * ZREF, places=3)

    def test_06_dc_extrapolation_methods(self) -> None:
        m = 64
        freq = [k + 1.0 for k in range(m)]
        # 实部随频率线性下降：Γ(f) = 0.9 - 0.1·f（在 f=1..64）。
        gamma = [(0.9 - 0.1 * f) + 0.0j for f in freq]
        r_linear = compute_tdr(freq, gamma, {"window_function": "none", "rise_time_ps": 0,
                                             "dc_extrapolation": "linear"})
        r_constant = compute_tdr(freq, gamma, {"window_function": "none", "rise_time_ps": 0,
                                               "dc_extrapolation": "constant"})
        # linear 外推应回到 Γ(0)=0.9；constant 取 Γ(f_min)=0.8。
        self.assertAlmostEqual(r_linear.reflection[-1], 0.9, places=6)
        self.assertAlmostEqual(r_constant.reflection[-1], 0.8, places=6)

    def test_07_distance_axis_with_velocity(self) -> None:
        result = self._compute("open", propagation_velocity_m_s=1.5e8)
        self.assertTrue(result.distance_mm)
        self.assertEqual(len(result.distance_mm), len(result.time_ns))
        self.assertEqual(result.settings["distance_axis"], "distance")
        # 单程距离 d = v·t/2；末采样 t≈1ns → d≈1.5e8·1e-9/2·1e3 = 75mm。
        self.assertAlmostEqual(result.distance_mm[-1], 75.0, delta=1.0)

    def test_08_settings_defaults_complete(self) -> None:
        settings = default_tdr_settings()
        for key in ("input_curve", "differential_reference_impedance", "rise_time_ps",
                    "dc_extrapolation", "window_function", "causality",
                    "propagation_velocity_m_s", "distance_axis"):
            self.assertIn(key, settings)
        self.assertEqual(settings["input_curve"], "SDD11")
        self.assertEqual(settings["differential_reference_impedance"], ZREF)

    def test_09_settings_validation_rejects_bad_values(self) -> None:
        self.assertTrue(validate_tdr_settings({"input_curve": "Sdd11_db"}))
        self.assertTrue(validate_tdr_settings({"dc_extrapolation": "bogus"}))
        self.assertTrue(validate_tdr_settings({"window_function": "bogus"}))
        self.assertFalse(validate_tdr_settings(default_tdr_settings()))
        with self.assertRaises(ValueError):
            resolve_tdr_settings({"dc_extrapolation": "bogus"})

    def test_10_nonuniform_grid_fail_closed(self) -> None:
        freq = [1.0, 2.0, 5.0]
        gamma = [0.1 + 0j] * 3
        with self.assertRaises(ValueError):
            compute_tdr(freq, gamma)

    def test_11_run_tdr_from_result(self) -> None:
        t = 0.5 + 0.0j
        se = [[0j, 0j, 0j, 0j], [0j, 0j, 0j, 0j], [t, 0j, 0j, 0j], [0j, t, 0j, 0j]]
        result = build_via_results_from_single_ended(
            frequency_ghz=[10.0, 20.0], se_matrices=[se, se],
            config_hash="cfg", geometry_hash="geo",
        )
        self.assertTrue(result.Sdd11_re and result.Sdd11_im)
        tdr = run_tdr_from_result(result)
        self.assertIsInstance(tdr, TdrResult)
        self.assertTrue(tdr.time_ns)
        # SDD11 = 0（差分直通无反射）→ TDR 阻抗恒为 Zref。
        self.assertLess(max(abs(z - ZREF) for z in tdr.impedance_ohm), 1e-6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
