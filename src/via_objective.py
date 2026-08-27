"""via_transition 目标函数与结果评分（规格 §11.4）。

把四条混合模 dB 曲线按频段规格掩膜转成无量纲惩罚，再按权重求和为单一目标，
输出加权排名与至少一个帕累托视图；几何/制造约束失败的 Variation 不参与排名。
纯 Python，不依赖 AEDT/tkinter/shapely。
"""
from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

DIFFERENTIAL_REFERENCE_IMPEDANCE = 100.0


@dataclass(frozen=True)
class SpecMask:
    """规格掩膜（无量纲惩罚的频段阈值）。"""
    frequency_start_ghz: float = 0.01
    frequency_stop_ghz: float = 64.0
    return_loss_limit_db: float = -15.0        # SDD11 上限（更负更好）
    insertion_loss_limit_db: float = -1.0      # SDD21 下限（越接近 0 越好）
    mode_conversion_limit_db: float = -20.0    # SDC21/SCD21 上限
    tdr_target_ohm: float = DIFFERENTIAL_REFERENCE_IMPEDANCE
    tdr_tolerance_ohm: float = 10.0
    normalization_db: float = 1.0              # dB 惩罚归一化尺度

    def to_dict(self) -> Dict[str, Any]:
        return {
            "frequency_start_ghz": self.frequency_start_ghz,
            "frequency_stop_ghz": self.frequency_stop_ghz,
            "return_loss_limit_db": self.return_loss_limit_db,
            "insertion_loss_limit_db": self.insertion_loss_limit_db,
            "mode_conversion_limit_db": self.mode_conversion_limit_db,
            "tdr_target_ohm": self.tdr_target_ohm,
            "tdr_tolerance_ohm": self.tdr_tolerance_ohm,
            "normalization_db": self.normalization_db,
        }


@dataclass(frozen=True)
class ObjectiveWeights:
    w_rl: float = 1.0
    w_il: float = 1.0
    w_dc: float = 1.0
    w_cd: float = 1.0
    w_tdr: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {"w_rl": self.w_rl, "w_il": self.w_il, "w_dc": self.w_dc,
                "w_cd": self.w_cd, "w_tdr": self.w_tdr}


def default_spec_mask() -> SpecMask:
    return SpecMask()


def default_objective_weights() -> ObjectiveWeights:
    return ObjectiveWeights()


@dataclass(frozen=True)
class Penalty:
    """单个目标的最坏频段惩罚（保存最坏频率与原始指标值）。"""
    name: str
    worst_metric_db: float
    worst_frequency_ghz: float
    normalized: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "worst_metric_db": self.worst_metric_db,
            "worst_frequency_ghz": self.worst_frequency_ghz,
            "normalized": self.normalized,
        }


@dataclass
class VariationScore:
    variation: Dict[str, Any]
    penalties: Dict[str, Penalty]
    objective: float
    constraint_failed: bool = False
    constraint_reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "variation": dict(self.variation),
            "penalties": {key: value.to_dict() for key, value in self.penalties.items()},
            "objective": self.objective,
            "constraint_failed": self.constraint_failed,
            "constraint_reasons": list(self.constraint_reasons),
        }


def _in_band(frequency_ghz: float, mask: SpecMask) -> bool:
    return mask.frequency_start_ghz <= frequency_ghz <= mask.frequency_stop_ghz


def _worst_violation(
    frequency_ghz: Sequence[float],
    metric_db: Sequence[float],
    mask: SpecMask,
    violation_fn: "object",
) -> Tuple[float, float]:
    """返回频段内最坏违规值及其频率；无违规时返回 (0.0, 0.0)。"""
    worst = 0.0
    worst_freq = 0.0
    for freq, value in zip(frequency_ghz, metric_db):
        if not _in_band(float(freq), mask):
            continue
        if not math.isfinite(float(value)):
            continue
        violation = max(0.0, float(violation_fn(float(value))))
        if violation > worst:
            worst = violation
            worst_freq = float(freq)
    return worst, worst_freq


def compute_penalties(
    frequency_ghz: Sequence[float],
    sdd11_db: Sequence[float],
    sdd21_db: Sequence[float],
    sdc21_db: Sequence[float],
    scd21_db: Sequence[float],
    mask: Optional[SpecMask] = None,
    weights: Optional[ObjectiveWeights] = None,
    tdr_impedance_ohm: Optional[Sequence[float]] = None,
) -> Dict[str, Penalty]:
    """按规格掩膜把四条混合模 dB 曲线转为无量纲惩罚（规格 §11.4）。"""
    mask = mask or default_spec_mask()
    weights = weights or default_objective_weights()
    norm = float(mask.normalization_db) if mask.normalization_db > 0 else 1.0

    rl_worst, rl_freq = _worst_violation(
        frequency_ghz, sdd11_db, mask, lambda v: v - mask.return_loss_limit_db)
    il_worst, il_freq = _worst_violation(
        frequency_ghz, sdd21_db, mask, lambda v: mask.insertion_loss_limit_db - v)
    dc_worst, dc_freq = _worst_violation(
        frequency_ghz, sdc21_db, mask, lambda v: v - mask.mode_conversion_limit_db)
    cd_worst, cd_freq = _worst_violation(
        frequency_ghz, scd21_db, mask, lambda v: v - mask.mode_conversion_limit_db)

    penalties: Dict[str, Penalty] = {
        "P_RL": Penalty("P_RL", rl_worst, rl_freq, rl_worst / norm),
        "P_IL": Penalty("P_IL", il_worst, il_freq, il_worst / norm),
        "P_DC": Penalty("P_DC", dc_worst, dc_freq, dc_worst / norm),
        "P_CD": Penalty("P_CD", cd_worst, cd_freq, cd_worst / norm),
    }

    if weights.w_tdr > 0 and tdr_impedance_ohm:
        worst_dev = 0.0
        worst_t = 0.0
        for index, value in enumerate(tdr_impedance_ohm):
            deviation = abs(float(value) - float(mask.tdr_target_ohm))
            if deviation > worst_dev:
                worst_dev = deviation
                worst_t = index
        tol = float(mask.tdr_tolerance_ohm) if mask.tdr_tolerance_ohm > 0 else 1.0
        normalized_tdr = max(0.0, worst_dev - tol) / tol
        penalties["P_TDR"] = Penalty("P_TDR", worst_dev, float(worst_t), normalized_tdr)
    return penalties


def objective_value(penalties: Mapping[str, Penalty], weights: ObjectiveWeights) -> float:
    return (
        weights.w_rl * penalties.get("P_RL", _ZERO_PENALTY).normalized
        + weights.w_il * penalties.get("P_IL", _ZERO_PENALTY).normalized
        + weights.w_dc * penalties.get("P_DC", _ZERO_PENALTY).normalized
        + weights.w_cd * penalties.get("P_CD", _ZERO_PENALTY).normalized
        + weights.w_tdr * penalties.get("P_TDR", _ZERO_PENALTY).normalized
    )


_ZERO_PENALTY = Penalty("", 0.0, 0.0, 0.0)


def score_variation(
    frequency_ghz: Sequence[float],
    sdd11_db: Sequence[float],
    sdd21_db: Sequence[float],
    sdc21_db: Sequence[float],
    scd21_db: Sequence[float],
    mask: Optional[SpecMask] = None,
    weights: Optional[ObjectiveWeights] = None,
    variation: Optional[Mapping[str, Any]] = None,
    tdr_impedance_ohm: Optional[Sequence[float]] = None,
    constraint_failed: bool = False,
    constraint_reasons: Optional[Sequence[str]] = None,
) -> VariationScore:
    """对单个 Variation 评分。"""
    mask = mask or default_spec_mask()
    weights = weights or default_objective_weights()
    penalties = compute_penalties(
        frequency_ghz, sdd11_db, sdd21_db, sdc21_db, scd21_db,
        mask, weights, tdr_impedance_ohm,
    )
    return VariationScore(
        variation=dict(variation or {}),
        penalties=penalties,
        objective=objective_value(penalties, weights),
        constraint_failed=bool(constraint_failed),
        constraint_reasons=list(constraint_reasons or []),
    )


def score_sweep_results(
    results: Sequence[Mapping[str, Any]],
    mask: Optional[SpecMask] = None,
    weights: Optional[ObjectiveWeights] = None,
) -> List[VariationScore]:
    """对逐 Variation 的解析结果评分（规格 §10.3 / §11.4）。

    每个 ``results`` 项至少包含 ``frequency_ghz``、``Sdd11_db``、``Sdd21_db``、
    ``Sdc21_db``、``Scd21_db``；可选 ``variation``、``tdr_impedance_ohm``、
    ``constraint_failed``、``constraint_reasons``。
    """
    scores: List[VariationScore] = []
    for item in results:
        scores.append(score_variation(
            frequency_ghz=item["frequency_ghz"],
            sdd11_db=item["Sdd11_db"],
            sdd21_db=item["Sdd21_db"],
            sdc21_db=item["Sdc21_db"],
            scd21_db=item["Scd21_db"],
            mask=mask, weights=weights,
            variation=item.get("variation"),
            tdr_impedance_ohm=item.get("tdr_impedance_ohm"),
            constraint_failed=bool(item.get("constraint_failed")),
            constraint_reasons=item.get("constraint_reasons") or [],
        ))
    return scores


def rank_scores(scores: Sequence[VariationScore]) -> List[VariationScore]:
    """约束失败的排除后按目标值升序排名。"""
    eligible = [score for score in scores if not score.constraint_failed]
    return sorted(eligible, key=lambda s: (s.objective, json.dumps(s.variation, sort_keys=True, default=str)))


def pareto_front(scores: Sequence[VariationScore]) -> List[VariationScore]:
    """返回帕累托前沿（按 P_RL/P_IL/P_DC/P_CD 各维最小化，含可选 P_TDR）。"""
    eligible = [score for score in scores if not score.constraint_failed]
    front: List[VariationScore] = []
    for candidate in eligible:
        dominated = False
        for other in eligible:
            if other is candidate:
                continue
            if dominates(other, candidate):
                dominated = True
                break
        if not dominated:
            front.append(candidate)
    return front


def dominates(a: VariationScore, b: VariationScore) -> bool:
    """a 在所有惩罚维度不劣于 b，且至少一维严格更优。"""
    a_names = set(a.penalties)
    b_names = set(b.penalties)
    dims = sorted(a_names | b_names)
    at_least_as_good = True
    strictly_better = False
    for name in dims:
        a_val = a.penalties[name].normalized if name in a.penalties else 0.0
        b_val = b.penalties[name].normalized if name in b.penalties else 0.0
        if a_val > b_val + 1e-12:
            at_least_as_good = False
            break
        if a_val < b_val - 1e-12:
            strictly_better = True
    return at_least_as_good and strictly_better


@dataclass
class ScoringReport:
    mask: SpecMask
    weights: ObjectiveWeights
    scores: List[VariationScore]
    ranking: List[VariationScore]
    pareto: List[VariationScore]

    def to_dict(self) -> Dict[str, Any]:
        ranking_index = {id(score): index for index, score in enumerate(self.ranking, 1)}
        pareto_ids = {id(score) for score in self.pareto}
        rows = []
        for score in self.scores:
            entry = score.to_dict()
            entry["rank"] = ranking_index.get(id(score))
            entry["pareto"] = id(score) in pareto_ids
            rows.append(entry)
        return {
            "schema_version": "via_scoring_v1",
            "mask": self.mask.to_dict(),
            "weights": self.weights.to_dict(),
            "ranked_count": len(self.ranking),
            "pareto_count": len(self.pareto),
            "variations": rows,
        }


def build_scoring_report(
    results: Sequence[Mapping[str, Any]],
    mask: Optional[SpecMask] = None,
    weights: Optional[ObjectiveWeights] = None,
) -> ScoringReport:
    mask = mask or default_spec_mask()
    weights = weights or default_objective_weights()
    scores = score_sweep_results(results, mask, weights)
    ranking = rank_scores(scores)
    pareto = pareto_front(scores)
    return ScoringReport(mask, weights, scores, ranking, pareto)


def build_sweep_summary(
    results: Sequence[Mapping[str, Any]],
    mask: Optional[SpecMask] = None,
    weights: Optional[ObjectiveWeights] = None,
) -> Dict[str, Any]:
    """把逐 Variation 的解析结果与评分合并为 ``sweep_results_summary``（规格 §10.3/§11.4）。

    每个 ``results`` 项除曲线外可选 ``variation``、``config_hash``、``geometry_hash``、
    ``constraint_failed``、``constraint_reasons``、``tdr_impedance_ohm``。摘要内嵌真实
    解析曲线与评分，而不是只索引 CSV 文件路径。
    """
    mask = mask or default_spec_mask()
    weights = weights or default_objective_weights()
    report = build_scoring_report(results, mask, weights)
    ranking_index = {id(score): index for index, score in enumerate(report.ranking, 1)}
    pareto_ids = {id(score) for score in report.pareto}
    rows = []
    for index, (item, score) in enumerate(zip(results, report.scores), 1):
        rows.append({
            "index": index,
            "variation": dict(score.variation),
            "config_hash": item.get("config_hash", ""),
            "geometry_hash": item.get("geometry_hash", ""),
            "frequency_ghz": list(item.get("frequency_ghz", [])),
            "Sdd11_db": list(item.get("Sdd11_db", [])),
            "Sdd21_db": list(item.get("Sdd21_db", [])),
            "Sdc21_db": list(item.get("Sdc21_db", [])),
            "Scd21_db": list(item.get("Scd21_db", [])),
            "constraint_failed": score.constraint_failed,
            "constraint_reasons": list(score.constraint_reasons),
            "penalties": {key: value.to_dict() for key, value in score.penalties.items()},
            "objective": score.objective,
            "rank": ranking_index.get(id(score)),
            "pareto": id(score) in pareto_ids,
        })
    return {
        "schema_version": "via_sweep_summary_v1",
        "mask": mask.to_dict(),
        "weights": weights.to_dict(),
        "ranked_count": len(report.ranking),
        "pareto_count": len(report.pareto),
        "variations": rows,
    }


def export_sweep_summary(summary: Mapping[str, Any], path: Any) -> None:
    """写出 ``sweep_results_summary.json``。"""
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(summary, stream, ensure_ascii=False, indent=2)


def export_scoring_json(report: ScoringReport, path: Any) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(report.to_dict(), stream, ensure_ascii=False, indent=2)


def export_scoring_csv(report: ScoringReport, path: Any) -> None:
    with open(path, "w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["rank", "pareto", "objective", "variation",
                         "P_RL", "P_IL", "P_DC", "P_CD", "P_TDR", "constraint_failed"])
        ranking_index = {id(score): index for index, score in enumerate(report.ranking, 1)}
        pareto_ids = {id(score) for score in report.pareto}
        for score in sorted(report.scores, key=lambda s: (ranking_index.get(id(s), 10**9),
                                                          json.dumps(s.variation, sort_keys=True, default=str))):
            p = score.penalties
            writer.writerow([
                ranking_index.get(id(score), ""),
                "yes" if id(score) in pareto_ids else "",
                f"{score.objective:.9g}",
                json.dumps(score.variation, sort_keys=True, default=str),
                f"{p.get('P_RL', _ZERO_PENALTY).normalized:.9g}",
                f"{p.get('P_IL', _ZERO_PENALTY).normalized:.9g}",
                f"{p.get('P_DC', _ZERO_PENALTY).normalized:.9g}",
                f"{p.get('P_CD', _ZERO_PENALTY).normalized:.9g}",
                f"{p.get('P_TDR', _ZERO_PENALTY).normalized:.9g}",
                "yes" if score.constraint_failed else "",
            ])
