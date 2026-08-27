"""Phase 6 目标函数与结果评分（规格 §11.4）：纯 Python 数值测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from via_objective import (
    ObjectiveWeights,
    SpecMask,
    build_scoring_report,
    build_sweep_summary,
    compute_penalties,
    default_objective_weights,
    default_spec_mask,
    dominates,
    objective_value,
    pareto_front,
    rank_scores,
    score_sweep_results,
    score_variation,
)


def _mask() -> SpecMask:
    return SpecMask(frequency_start_ghz=1.0, frequency_stop_ghz=10.0,
                    return_loss_limit_db=-15.0, insertion_loss_limit_db=-1.0,
                    mode_conversion_limit_db=-20.0, normalization_db=1.0)


class ViaObjectiveTests(unittest.TestCase):
    def test_01_ideal_through_zero_penalty(self) -> None:
        freq = [1.0, 2.0, 3.0]
        score = score_variation(freq, [-40.0] * 3, [-0.1] * 3, [-60.0] * 3, [-60.0] * 3, _mask())
        self.assertEqual(score.objective, 0.0)
        self.assertTrue(all(p.normalized == 0.0 for p in score.penalties.values()))

    def test_02_hand_computed_penalties(self) -> None:
        freq = [1.0, 2.0, 3.0]
        penalties = compute_penalties(freq, [-10.0] * 3, [-3.0] * 3, [-10.0] * 3, [-10.0] * 3, _mask())
        self.assertEqual(penalties["P_RL"].normalized, 5.0)
        self.assertEqual(penalties["P_IL"].normalized, 2.0)
        self.assertEqual(penalties["P_DC"].normalized, 10.0)
        self.assertEqual(penalties["P_CD"].normalized, 10.0)
        self.assertEqual(objective_value(penalties, default_objective_weights()), 27.0)

    def test_03_weights_scale_objective(self) -> None:
        freq = [2.0]
        score = score_variation(freq, [-10.0], [-0.1], [-60.0], [-60.0], _mask(),
                                weights=ObjectiveWeights(w_rl=2.0, w_il=0.0, w_dc=0.0, w_cd=0.0))
        self.assertAlmostEqual(score.objective, 10.0, places=9)

    def test_04_out_of_band_ignored(self) -> None:
        freq = [0.1, 2.0, 100.0]  # 0.1 与 100 在掩膜频段外
        penalties = compute_penalties(freq, [-10.0, -10.0, -10.0], [-0.1, -0.1, -0.1],
                                      [-60.0] * 3, [-60.0] * 3, _mask())
        # 只有 2.0 GHz 在带内 → P_RL = 5
        self.assertEqual(penalties["P_RL"].normalized, 5.0)
        self.assertEqual(penalties["P_RL"].worst_frequency_ghz, 2.0)

    def test_05_ranking_excludes_constraint_failed(self) -> None:
        freq = [2.0]
        results = [
            {"variation": {"a": 1}, "frequency_ghz": freq, "Sdd11_db": [-40.0], "Sdd21_db": [-0.1],
             "Sdc21_db": [-60.0], "Scd21_db": [-60.0]},
            {"variation": {"a": 2}, "frequency_ghz": freq, "Sdd11_db": [-10.0], "Sdd21_db": [-0.1],
             "Sdc21_db": [-60.0], "Scd21_db": [-60.0]},
            {"variation": {"a": 3}, "frequency_ghz": freq, "Sdd11_db": [-10.0], "Sdd21_db": [-0.1],
             "Sdc21_db": [-60.0], "Scd21_db": [-60.0], "constraint_failed": True},
        ]
        scores = score_sweep_results(results, _mask())
        ranking = rank_scores(scores)
        self.assertEqual([s.variation["a"] for s in ranking], [1, 2])

    def test_06_pareto_front(self) -> None:
        freq = [2.0]
        def make(rl, il, key):
            return {"variation": {"k": key}, "frequency_ghz": freq,
                    "Sdd11_db": [rl], "Sdd21_db": [il], "Sdc21_db": [-60.0], "Scd21_db": [-60.0]}
        results = [make(-15.0, -6.0, 1), make(-10.0, -1.0, 2),
                   make(-12.0, -4.0, 3), make(-10.0, -6.0, 4)]
        scores = score_sweep_results(results, _mask())
        front = pareto_front(scores)
        self.assertEqual(sorted(s.variation["k"] for s in front), [1, 2, 3])

    def test_07_dominance_is_strict(self) -> None:
        freq = [2.0]
        a = score_variation(freq, [-15.0], [-6.0], [-60.0], [-60.0], _mask(), variation={"k": "a"})
        b = score_variation(freq, [-10.0], [-6.0], [-60.0], [-60.0], _mask(), variation={"k": "b"})
        self.assertTrue(dominates(a, b))
        self.assertFalse(dominates(b, a))
        self.assertFalse(dominates(a, a))

    def test_08_tdr_penalty_optional(self) -> None:
        freq = [2.0]
        mask = _mask()
        weights = ObjectiveWeights(w_rl=0.0, w_il=0.0, w_dc=0.0, w_cd=0.0, w_tdr=1.0)
        score = score_variation(freq, [-40.0], [-0.1], [-60.0], [-60.0], mask, weights,
                                tdr_impedance_ohm=[100.0, 115.0])
        # 最大偏差 15，容差 10 → (15-10)/10 = 0.5
        self.assertAlmostEqual(score.penalties["P_TDR"].normalized, 0.5, places=9)
        self.assertAlmostEqual(score.objective, 0.5, places=9)

    def test_09_sweep_summary_references_parsed_curves(self) -> None:
        freq = [1.0, 2.0, 3.0]
        results = []
        for index in range(9):
            results.append({
                "variation": {"via_transition.pair_pitch": 0.6 + 0.2 * (index // 3),
                              "via_transition.padstack.antipad_diameter": 0.6 + 0.1 * (index % 3)},
                "config_hash": f"cfg{index}", "geometry_hash": f"geo{index}",
                "frequency_ghz": freq,
                "Sdd11_db": [-40.0 + index] * 3, "Sdd21_db": [-0.1] * 3,
                "Sdc21_db": [-60.0] * 3, "Scd21_db": [-60.0] * 3,
            })
        summary = build_sweep_summary(results, _mask())
        self.assertEqual(len(summary["variations"]), 9)
        self.assertEqual(len(summary["variations"][0]["frequency_ghz"]), 3)
        self.assertTrue(summary["ranked_count"] >= 1)
        self.assertTrue(all("rank" in v and "objective" in v for v in summary["variations"]))
        self.assertTrue(any(v["rank"] == 1 for v in summary["variations"]))

    def test_10_report_and_summary_are_json_serializable(self) -> None:
        import json
        freq = [2.0]
        results = [{"variation": {"a": 1}, "frequency_ghz": freq, "Sdd11_db": [-10.0],
                    "Sdd21_db": [-0.1], "Sdc21_db": [-60.0], "Scd21_db": [-60.0]}]
        report = build_scoring_report(results, _mask())
        summary = build_sweep_summary(results, _mask())
        json.dumps(report.to_dict())
        json.dumps(summary)
        self.assertTrue(report.to_dict()["mask"]["return_loss_limit_db"] == -15.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
