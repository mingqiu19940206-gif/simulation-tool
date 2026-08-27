"""Phase 4 结果解析：混合模 S 参数转换、结果结构与校验（纯 Python）。"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from via_transition_results import (
    compute_mixed_mode_smatrix,
    mixed_mode_metrics,
    build_via_results_from_single_ended,
    validate_via_results,
)


def _assert_complex_close(testcase, actual, expected, places=6):
    testcase.assertAlmostEqual(actual.real, expected.real, places=places)
    testcase.assertAlmostEqual(actual.imag, expected.imag, places=places)


class ViaResultsParserTests(unittest.TestCase):
    def test_01_differential_through(self) -> None:
        # 理想差分直通：P_in->P_out、N_in->N_out，透射系数 0.8。
        t = 0.8 + 0.0j
        se = [
            [0j, 0j, 0j, 0j],
            [0j, 0j, 0j, 0j],
            [t, 0j, 0j, 0j],
            [0j, t, 0j, 0j],
        ]
        mm = compute_mixed_mode_smatrix(se)
        metrics = mixed_mode_metrics(mm)
        _assert_complex_close(self, metrics["SDD11"], 0j)
        _assert_complex_close(self, metrics["SDD21"], t)
        _assert_complex_close(self, metrics["SDC21"], 0j)
        _assert_complex_close(self, metrics["SCD21"], 0j)

    def test_02_round_trip_reconstructs_single_ended(self) -> None:
        # S_mm = M·S_se·Mᵀ，且 M 正交，故 Mᵀ·S_mm·M 应还原 S_se。
        se = [
            [0.1 + 0.2j, 0.05 + 0j, 0.8 + 0.1j, 0.02 + 0.03j],
            [0.05 + 0j, 0.1 - 0.2j, 0.02 - 0.03j, 0.8 - 0.1j],
            [0.8 + 0.1j, 0.02 - 0.03j, 0.1 + 0.2j, 0.05 + 0j],
            [0.02 + 0.03j, 0.8 - 0.1j, 0.05 + 0j, 0.1 - 0.2j],
        ]
        mm = compute_mixed_mode_smatrix(se)
        inv_sqrt2 = 1.0 / math.sqrt(2.0)
        m = ((1.0, -1.0, 0.0, 0.0), (0.0, 0.0, 1.0, -1.0),
             (1.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 1.0))
        # S_se' = Mᵀ·S_mm·M
        mt_smm = [[sum(m[k][i] * mm[k][j] for k in range(4)) * inv_sqrt2 for j in range(4)]
                  for i in range(4)]
        reconstructed = [[sum(mt_smm[i][k] * m[k][j] for k in range(4)) * inv_sqrt2
                         for j in range(4)] for i in range(4)]
        for i in range(4):
            for j in range(4):
                _assert_complex_close(self, reconstructed[i][j], se[i][j], places=9)

    def test_03_build_results_computes_db_curves(self) -> None:
        t = 0.8 + 0.0j
        se = [
            [0j, 0j, 0j, 0j],
            [0j, 0j, 0j, 0j],
            [t, 0j, 0j, 0j],
            [0j, t, 0j, 0j],
        ]
        result = build_via_results_from_single_ended(
            frequency_ghz=[10.0, 20.0],
            se_matrices=[se, se],
            config_hash="cfg", geometry_hash="geo",
        )
        self.assertEqual(len(result.Sdd21_db), 2)
        expected_db = 20.0 * math.log10(0.8)
        self.assertAlmostEqual(result.Sdd21_db[0], expected_db, places=6)
        self.assertEqual(result.frequency_ghz, [10.0, 20.0])
        self.assertEqual(len(result.port_mapping), 4)

    def test_04_validate_results_detects_missing_field(self) -> None:
        result = {
            "schema_version": "via_results_v1",
            "config_hash": "cfg", "geometry_hash": "geo",
            "frequency_ghz": [1.0], "Sdd11_db": [0.0], "Sdd21_db": [0.0],
            "Sdc21_db": [0.0], "Scd21_db": [0.0], "converged": True,
            "port_mapping": [{}] * 4,
        }
        errors = validate_via_results(result)
        self.assertFalse(errors, errors)

    def test_05_validate_results_rejects_missing_hash(self) -> None:
        result = {
            "schema_version": "via_results_v1",
            "frequency_ghz": [1.0], "Sdd11_db": [0.0], "Sdd21_db": [0.0],
            "Sdc21_db": [0.0], "Scd21_db": [0.0], "converged": True,
            "port_mapping": [{}] * 4,
        }
        errors = validate_via_results(result)
        self.assertTrue(any("config_hash" in e or "缺少字段" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main(verbosity=2)
