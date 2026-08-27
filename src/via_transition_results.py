"""via_transition 求解结果：混合模 S 参数、结果结构与导出（规格 §10）。

本模块只做纯数据处理，不依赖 AEDT/PyAEDT/tkinter，供求解编排与离线测试共用。
规格 §10.2：不得错误复用 ``validate_results.py`` 作为 S 参数解析器。
"""
from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from via_transition_tdr import (
    TdrResult,
    compute_tdr,
    default_tdr_settings,
    export_tdr_csv,
    export_tdr_json,
    resolve_tdr_settings,
)

DIFFERENTIAL_REFERENCE_IMPEDANCE = 100.0
SINGLE_ENDED_PORT_ORDER = ("PORT_IN_P", "PORT_IN_N", "PORT_OUT_P", "PORT_OUT_N")

# 混合模端口顺序：[IN_DIFF, OUT_DIFF, IN_COMM, OUT_COMM]。
# M（未归一化）：mixed = (1/√2) · M · single。
_M = (
    (1.0, -1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, -1.0),
    (1.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 1.0),
)
_INV_SQRT2 = 1.0 / math.sqrt(2.0)


def compute_mixed_mode_smatrix(se_matrix: Sequence[Sequence[complex]]) -> List[List[complex]]:
    """把 4x4 单端 S 矩阵（端口顺序 PORT_IN_P/N/OUT_P/N）转为混合模 S 矩阵。

    返回 4x4 复数矩阵，端口顺序 [IN_DIFF, OUT_DIFF, IN_COMM, OUT_COMM]。
    ``S_mm = M · S_se · Mᵀ``，其中 M 为混合模变换矩阵。
    """
    if len(se_matrix) != 4 or any(len(row) != 4 for row in se_matrix):
        raise ValueError("单端 S 矩阵必须是 4x4。")
    # M · S_se
    ms = [[sum(_M[i][k] * se_matrix[k][j] for k in range(4)) * _INV_SQRT2 for j in range(4)]
          for i in range(4)]
    # (M · S_se) · Mᵀ
    mm = [[sum(ms[i][k] * _M[j][k] for k in range(4)) * _INV_SQRT2 for j in range(4)]
          for i in range(4)]
    return mm


def mixed_mode_metrics(mm: Sequence[Sequence[complex]]) -> Dict[str, complex]:
    """从混合模矩阵提取 SDD11/SDD21/SDC21/SCD21（复数）。"""
    return {
        "SDD11": mm[0][0],
        "SDD21": mm[1][0],
        "SDC21": mm[1][2],
        "SCD21": mm[3][0],
    }


def _to_db(value: complex) -> float:
    magnitude = abs(value)
    if magnitude <= 0.0:
        return float("-inf")
    return 20.0 * math.log10(magnitude)


def default_port_mapping() -> List[Dict[str, str]]:
    """四个单端端口到差分对的映射。"""
    return [
        {"port": "PORT_IN_P", "pair": "IN", "polarity": "P", "mode": "differential"},
        {"port": "PORT_IN_N", "pair": "IN", "polarity": "N", "mode": "differential"},
        {"port": "PORT_OUT_P", "pair": "OUT", "polarity": "P", "mode": "differential"},
        {"port": "PORT_OUT_N", "pair": "OUT", "polarity": "N", "mode": "differential"},
    ]


@dataclass
class ViaResults:
    """规格 §10.2 的结构化 ``via_results.json``。"""

    schema_version: str = "via_results_v1"
    software_version: str = ""
    config_hash: str = ""
    geometry_hash: str = ""
    aedt_version: str = ""
    pyaedt_version: str = ""
    project: str = ""
    design: str = ""
    setup: str = ""
    sweep: str = ""
    variation: Dict[str, Any] = field(default_factory=dict)
    frequency_ghz: List[float] = field(default_factory=list)
    Sdd11_db: List[float] = field(default_factory=list)
    Sdd21_db: List[float] = field(default_factory=list)
    Sdc21_db: List[float] = field(default_factory=list)
    Scd21_db: List[float] = field(default_factory=list)
    Sdd11_re: List[float] = field(default_factory=list)
    Sdd11_im: List[float] = field(default_factory=list)
    solve_completed: bool = False
    converged: bool = False
    convergence_evidence: str = ""
    port_mapping: List[Dict[str, str]] = field(default_factory=default_port_mapping)
    launch_lengths: Dict[str, float] = field(default_factory=dict)
    deembed_settings: Dict[str, Any] = field(default_factory=dict)
    tdr_settings: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "software_version": self.software_version,
            "config_hash": self.config_hash,
            "geometry_hash": self.geometry_hash,
            "aedt_version": self.aedt_version,
            "pyaedt_version": self.pyaedt_version,
            "project": self.project,
            "design": self.design,
            "setup": self.setup,
            "sweep": self.sweep,
            "variation": dict(self.variation),
            "frequency_ghz": list(self.frequency_ghz),
            "Sdd11_db": list(self.Sdd11_db),
            "Sdd21_db": list(self.Sdd21_db),
            "Sdc21_db": list(self.Sdc21_db),
            "Scd21_db": list(self.Scd21_db),
            "Sdd11_re": list(self.Sdd11_re),
            "Sdd11_im": list(self.Sdd11_im),
            "solve_completed": self.solve_completed,
            "converged": self.converged,
            "convergence_evidence": self.convergence_evidence,
            "port_mapping": [dict(item) for item in self.port_mapping],
            "launch_lengths": dict(self.launch_lengths),
            "deembed_settings": dict(self.deembed_settings),
            "tdr_settings": dict(self.tdr_settings),
            "warnings": list(self.warnings),
        }


def build_via_results_from_single_ended(
    *,
    frequency_ghz: Sequence[float],
    se_matrices: Sequence[Sequence[Sequence[complex]]],
    config_hash: str = "",
    geometry_hash: str = "",
    software_version: str = "",
    aedt_version: str = "",
    pyaedt_version: str = "",
    project: str = "",
    design: str = "",
    setup: str = "",
    sweep: str = "",
    variation: Optional[Mapping[str, Any]] = None,
    solve_completed: bool = False,
    converged: bool = False,
    convergence_evidence: str = "",
    port_mapping: Optional[Sequence[Mapping[str, str]]] = None,
    launch_lengths: Optional[Mapping[str, float]] = None,
    deembed_settings: Optional[Mapping[str, Any]] = None,
    tdr_settings: Optional[Mapping[str, Any]] = None,
    warnings: Optional[Sequence[str]] = None,
) -> ViaResults:
    """由逐频点 4x4 单端 S 矩阵构造结构化结果，计算混合模 dB 曲线。"""
    if len(frequency_ghz) != len(se_matrices):
        raise ValueError("frequency_ghz 与 se_matrices 长度不一致。")

    sdd11: List[float] = []
    sdd21: List[float] = []
    sdc21: List[float] = []
    scd21: List[float] = []
    sdd11_re: List[float] = []
    sdd11_im: List[float] = []
    for se in se_matrices:
        metrics = mixed_mode_metrics(compute_mixed_mode_smatrix(se))
        sdd11.append(_to_db(metrics["SDD11"]))
        sdd21.append(_to_db(metrics["SDD21"]))
        sdc21.append(_to_db(metrics["SDC21"]))
        scd21.append(_to_db(metrics["SCD21"]))
        sdd11_re.append(metrics["SDD11"].real)
        sdd11_im.append(metrics["SDD11"].imag)

    return ViaResults(
        software_version=software_version,
        config_hash=config_hash,
        geometry_hash=geometry_hash,
        aedt_version=aedt_version,
        pyaedt_version=pyaedt_version,
        project=project,
        design=design,
        setup=setup,
        sweep=sweep,
        variation=dict(variation or {}),
        frequency_ghz=list(frequency_ghz),
        Sdd11_db=sdd11,
        Sdd21_db=sdd21,
        Sdc21_db=sdc21,
        Scd21_db=scd21,
        Sdd11_re=sdd11_re,
        Sdd11_im=sdd11_im,
        solve_completed=solve_completed,
        converged=converged,
        convergence_evidence=convergence_evidence,
        port_mapping=[dict(item) for item in (port_mapping or default_port_mapping())],
        launch_lengths=dict(launch_lengths or {}),
        deembed_settings=dict(deembed_settings or {}),
        tdr_settings=dict(tdr_settings or default_tdr_settings()),
        warnings=list(warnings or []),
    )


def validate_via_results(result: Mapping[str, Any]) -> List[str]:
    """对 ``via_results.json`` 做结构校验，返回错误列表。"""
    errors: List[str] = []
    required = ("schema_version", "config_hash", "geometry_hash", "frequency_ghz",
                "Sdd11_db", "Sdd21_db", "Sdc21_db", "Scd21_db", "converged", "port_mapping")
    for key in required:
        if key not in result:
            errors.append(f"缺少字段 {key}。")
    if not result.get("config_hash") or not result.get("geometry_hash"):
        errors.append("config_hash / geometry_hash 不得为空。")
    freq = result.get("frequency_ghz", [])
    for key in ("Sdd11_db", "Sdd21_db", "Sdc21_db", "Scd21_db"):
        if len(result.get(key, [])) != len(freq):
            errors.append(f"{key} 长度与 frequency_ghz 不一致。")
    mapping = result.get("port_mapping", [])
    if len(mapping) != 4:
        errors.append("port_mapping 必须包含 4 个端口。")
    return errors


def export_via_results_json(result: ViaResults, path: Any) -> None:
    """写出 ``via_results.json``。"""
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(result.to_dict(), stream, ensure_ascii=False, indent=2)


def export_via_results_csv(result: ViaResults, path: Any) -> None:
    """写出便于人工核对的 CSV（每行一个频点）。"""
    with open(path, "w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["frequency_ghz", "Sdd11_db", "Sdd21_db", "Sdc21_db", "Scd21_db"])
        for index, freq in enumerate(result.frequency_ghz):
            writer.writerow([
                f"{freq:.9g}",
                f"{result.Sdd11_db[index]:.9g}",
                f"{result.Sdd21_db[index]:.9g}",
                f"{result.Sdc21_db[index]:.9g}",
                f"{result.Scd21_db[index]:.9g}",
            ])


def run_tdr_from_result(
    result: ViaResults,
    tdr_settings: Optional[Mapping[str, Any]] = None,
) -> TdrResult:
    """由结构化结果中的复数 SDD11 与 ``tdr_settings`` 计算差分 TDR。

    要求结果携带复数 SDD11（``Sdd11_re``/``Sdd11_im``）；缺失时抛出 ``ValueError``，
    绝不回退到对 dB 曲线做 IFFT。
    """
    if not result.Sdd11_re and not result.Sdd11_im:
        raise ValueError("结果缺少复数 SDD11（Sdd11_re/Sdd11_im），无法计算 TDR。")
    if len(result.Sdd11_re) != len(result.frequency_ghz):
        raise ValueError("Sdd11_re 长度与 frequency_ghz 不一致。")
    sdd11 = [complex(re, im) for re, im in zip(result.Sdd11_re, result.Sdd11_im)]
    settings = resolve_tdr_settings(tdr_settings if tdr_settings else result.tdr_settings)
    return compute_tdr(result.frequency_ghz, sdd11, settings)
