"""via_transition 差分 TDR（规格 §10.4 / §13 Phase 5）。

对导出的复数频域 SDD11 做离线时域反射计变换，输出差分阻抗随时间/距离曲线。
本模块只做纯数据处理，不依赖 numpy/tkinter/shapely/AEDT。

**算法（记录在 ``tdr_settings`` 中）**——明确禁止对 dB 曲线直接 IFFT：

1. 输入曲线来源固定为复数 SDD11（混合模 S[0][0]），不接受 dB 幅度。
2. DC 外推：由低频复数 SDD11 外推 ``Γ(0)``（``zero`` / ``constant`` / ``linear``）。
3. 频域抗振铃窗：对正频率 SDD11 施加窗函数（``none`` / ``hann`` / ``kaiser``）。
4. 上升时间：以 10–90% 上升时间对应的 Gaussian 低通滤波器建模入射阶跃的有限边沿。
5. Hermitian 对称：构造双边频谱（负频取共轭），逆变换得到实数冲激响应（隐式因果）。
6. 阶跃响应：冲激响应累积积分 → 反射系数阶跃 ``ρ(t)``。
7. 阻抗：``Z(t) = Zref · (1 + ρ(t)) / (1 − ρ(t))``。
8. 距离：可选由传播速度换算单程距离 ``d = v·t/2``；否则只输出时间轴。

频率网格假设为 DC 锚定的均匀网格 ``freq[k] = (k+1)·Δf``（``Δf = freq[0]``），
与常见离散 Sweep「f_min…f_max step Δf」且 ``f_min = Δf`` 一致；不满足时 fail closed。
"""
from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

# 差分参考阻抗默认值（规格 §10.4）。
DIFFERENTIAL_REFERENCE_IMPEDANCE = 100.0

# 允许的枚举值。
_DC_EXTRAPOLATIONS = ("zero", "constant", "linear")
_WINDOW_FUNCTIONS = ("none", "hann", "kaiser", "raised_cosine")


def default_tdr_settings() -> Dict[str, Any]:
    """规格 §10.4 要求的全部 TDR 设置及默认值。"""
    return {
        "algorithm": "offline_ift_sdd11",
        "input_curve": "SDD11",
        "differential_reference_impedance": DIFFERENTIAL_REFERENCE_IMPEDANCE,
        "rise_time_ps": 35.0,
        "dc_extrapolation": "linear",
        "window_function": "hann",
        "window_beta": 6.0,
        "causality": "hermitian_symmetry",
        "propagation_velocity_m_s": None,
        "distance_axis": "time",
    }


def resolve_tdr_settings(overrides: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """合并默认值与可选覆盖项，返回规范化后的设置；非法值抛出 ``ValueError``。"""
    settings = default_tdr_settings()
    if overrides:
        for key, value in overrides.items():
            if value is not None:
                settings[key] = value
    errors = validate_tdr_settings(settings)
    if errors:
        raise ValueError("TDR 设置非法：" + "; ".join(errors))
    settings["differential_reference_impedance"] = float(settings["differential_reference_impedance"])
    settings["rise_time_ps"] = float(settings["rise_time_ps"])
    settings["window_beta"] = float(settings["window_beta"])
    return settings


def validate_tdr_settings(settings: Mapping[str, Any]) -> List[str]:
    """校验 TDR 设置，返回错误列表。"""
    errors: List[str] = []
    if settings.get("input_curve") != "SDD11":
        errors.append("input_curve 必须为 'SDD11'（禁止对 dB 曲线直接 IFFT）。")
    zref = settings.get("differential_reference_impedance")
    if zref is None or float(zref) <= 0:
        errors.append("differential_reference_impedance 必须为正数。")
    tr = settings.get("rise_time_ps")
    if tr is not None and float(tr) < 0:
        errors.append("rise_time_ps 不得为负。")
    if settings.get("dc_extrapolation") not in _DC_EXTRAPOLATIONS:
        errors.append(f"dc_extrapolation 必须是 {_DC_EXTRAPOLATIONS} 之一。")
    if settings.get("window_function") not in _WINDOW_FUNCTIONS:
        errors.append(f"window_function 必须是 {_WINDOW_FUNCTIONS} 之一。")
    beta = settings.get("window_beta")
    if beta is not None and float(beta) < 0:
        errors.append("window_beta 不得为负。")
    return errors


@dataclass
class TdrResult:
    """差分 TDR 结果：差分阻抗随时间/距离曲线及完整设置。"""

    time_ns: List[float] = field(default_factory=list)
    distance_mm: List[float] = field(default_factory=list)
    reflection: List[float] = field(default_factory=list)
    impedance_ohm: List[float] = field(default_factory=list)
    settings: Dict[str, Any] = field(default_factory=default_tdr_settings)
    frequency_window_ghz: List[float] = field(default_factory=list)
    time_window_ns: float = 0.0
    time_resolution_ns: float = 0.0
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": "via_tdr_v1",
            "settings": dict(self.settings),
            "frequency_window_ghz": list(self.frequency_window_ghz),
            "time_window_ns": self.time_window_ns,
            "time_resolution_ns": self.time_resolution_ns,
            "time_ns": list(self.time_ns),
            "distance_mm": list(self.distance_mm),
            "reflection": list(self.reflection),
            "impedance_ohm": list(self.impedance_ohm),
            "warnings": list(self.warnings),
        }


def _dc_extrapolate(freq: Sequence[float], gamma: Sequence[complex], method: str) -> float:
    """由低频复数 SDD11 外推实数 ``Γ(0)``。

    DC 反射系数在物理上为实数；各方法均返回实数：
    - ``zero``：Γ(0) = 0。
    - ``constant``：Γ(0) = Re(Γ(f_min))。
    - ``linear``：由最低两个频点的实部线性外推到 f=0。
    """
    if method == "zero":
        return 0.0
    if method == "constant":
        return gamma[0].real
    if method == "linear":
        f0, f1 = float(freq[0]), float(freq[1])
        y0, y1 = gamma[0].real, gamma[1].real
        if abs(f1 - f0) < 1e-15:
            return y0
        return y0 - (y1 - y0) * f0 / (f1 - f0)
    raise ValueError(f"未知 dc_extrapolation：{method}")


def _i0(x: float) -> float:
    """零阶修正 Bessel 函数 I0(x) 的级数近似（用于 Kaiser 窗）。"""
    total = 1.0
    term = 1.0
    k = 1
    half = x / 2.0
    while True:
        term *= half * half / (k * k)
        total += term
        if term < 1e-15 * total:
            break
        k += 1
        if k > 1000:
            break
    return total


def _kaiser(u: float, beta: float) -> float:
    if beta <= 0:
        return 1.0
    return _i0(beta * math.sqrt(max(0.0, 1.0 - u * u))) / _i0(beta)


def _window_value(f_ghz: float, f_max_ghz: float, kind: str, beta: float) -> float:
    """频域抗振铃窗：在 f=0 处为 1，向 f_max 衰减。"""
    if kind in ("none", None) or f_max_ghz <= 0:
        return 1.0
    u = min(max(f_ghz / f_max_ghz, 0.0), 1.0)
    if kind in ("hann", "raised_cosine"):
        return 0.5 * (1.0 + math.cos(math.pi * u))
    if kind == "kaiser":
        return _kaiser(u, beta)
    raise ValueError(f"未知 window_function：{kind}")


def _rise_time_filter(f_ghz: float, rise_time_ps: float) -> float:
    """入射阶跃的有限上升时间对应的 Gaussian 低通滤波器（10–90% 上升时间）。"""
    if not rise_time_ps or rise_time_ps <= 0:
        return 1.0
    sigma_s = rise_time_ps * 1e-12 / 2.563
    f_hz = f_ghz * 1e9
    return math.exp(-2.0 * (math.pi * sigma_s * f_hz) ** 2)


def _idft_unnormalized(spectrum: Sequence[complex]) -> List[complex]:
    """未归一化逆 DFT：H[m] = Σ_k X[k]·e^{j·2π·k·m/N}。

    纯 Python 实现（O(N²)），避免依赖 numpy；对离散 Sweep 的数百个频点足够。
    """
    n = len(spectrum)
    result: List[complex] = []
    for m in range(n):
        real = 0.0
        imag = 0.0
        for k in range(n):
            value = spectrum[k]
            angle = 2.0 * math.pi * k * m / n
            cos_a = math.cos(angle)
            sin_a = math.sin(angle)
            real += value.real * cos_a - value.imag * sin_a
            imag += value.real * sin_a + value.imag * cos_a
        result.append(complex(real, imag))
    return result


def compute_tdr(
    frequency_ghz: Sequence[float],
    sdd11: Sequence[complex],
    settings: Optional[Mapping[str, Any]] = None,
) -> TdrResult:
    """由复数 SDD11 计算差分阻抗 TDR 曲线（离线 IFT，规格 §10.4）。

    阶跃响应在频域直接构造：反射波谱 ``Γ(f)·G(f)·W(f)/(j·2πf)``（``1/(j2πf)``
    是单位阶跃激励的积分器），再逆变换并叠加 DC 稳态项 ``Γ(0)/2``，保证因果性。
    仅输出正时间半段（t ≥ 0）。
    """
    cfg = resolve_tdr_settings(settings)
    freq = [float(f) for f in frequency_ghz]
    gamma = [complex(v) for v in sdd11]
    warnings: List[str] = []

    if len(freq) < 2:
        raise ValueError("TDR 至少需要 2 个频点。")
    if len(freq) != len(gamma):
        raise ValueError("frequency_ghz 与 sdd11 长度不一致。")

    df = freq[0]
    if df <= 0:
        raise ValueError("频率网格必须从正频率开始。")
    for k, f in enumerate(freq):
        expected = (k + 1) * df
        if abs(f - expected) > 1e-6 * max(1.0, abs(expected)):
            raise ValueError(
                "频率必须是 DC 锚定的均匀网格 freq[k]=(k+1)·Δf（Δf=freq[0]）；"
                f"freq[{k}]={f} 与 {(k + 1) * df} 不一致。"
            )

    m = len(freq)
    n = 2 * m + 1
    f_max = freq[-1]

    gamma0 = _dc_extrapolate(freq, gamma, cfg["dc_extrapolation"])
    win_kind = cfg["window_function"]
    beta = float(cfg["window_beta"])
    tr_ps = float(cfg["rise_time_ps"])

    # 加窗/上升时间滤波后的反射谱（Hermitian，DC = Γ(0)）。
    spectrum: List[complex] = [0j] * n
    spectrum[0] = complex(gamma0, 0.0)
    for k in range(1, m + 1):
        f = k * df
        w = _window_value(f, f_max, win_kind, beta)
        g = _rise_time_filter(f, tr_ps)
        spectrum[k] = gamma[k - 1] * w * g
    for k in range(1, m + 1):
        spectrum[n - k] = spectrum[k].conjugate()

    h = _idft_unnormalized(spectrum)

    # 因果化：零相位窗会把部分冲激能量放到负时间（n>m），将其折叠回 t=0，
    # 保证总积分 = Γ(0) 且阶跃响应只存在于正时间半段（隐式因果）。
    folded = h[0] + sum((h[i] for i in range(m + 1, n)), 0j)
    causal = [0j] * n
    causal[0] = folded
    for i in range(1, m + 1):
        causal[i] = h[i]

    # 阶跃响应：ρ[n] = (1/N)·Σ_{i<=n} H[i]；ρ[M] = Γ(0)。
    half = m + 1
    accumulator = 0.0
    reflection: List[float] = []
    for i in range(half):
        accumulator += causal[i].real
        reflection.append(accumulator / n)

    dt_ns = 1.0 / (n * df)
    time_ns = [i * dt_ns for i in range(half)]

    zref = float(cfg["differential_reference_impedance"])
    impedance: List[float] = []
    for rho in reflection:
        # 被动网络 |ρ| ≤ 1；数值过冲时夹紧，避免开路侧出现符号错误的发散阻抗。
        clamped = min(max(rho, -1.0 + 1e-9), 1.0 - 1e-9)
        impedance.append(zref * (1.0 + clamped) / (1.0 - clamped))

    velocity = cfg.get("propagation_velocity_m_s")
    distance_mm: List[float] = []
    if velocity:
        velocity = float(velocity)
        for t in time_ns:
            distance_mm.append(velocity * t * 1e-9 / 2.0 * 1e3)
        cfg["distance_axis"] = "distance"
    else:
        cfg["distance_axis"] = "time"

    if abs(gamma0) > 1.0 + 1e-9:
        warnings.append("DC 外推得到 |Γ(0)| > 1，可能来自非因果输入。")
    if tr_ps > 0 and f_max > 0 and _rise_time_filter(f_max, tr_ps) > 0.5:
        warnings.append("上升时间相对带宽过小，Gaussian 滤波器未能充分截止最高频。")

    return TdrResult(
        time_ns=time_ns,
        distance_mm=distance_mm,
        reflection=reflection,
        impedance_ohm=impedance,
        settings=cfg,
        frequency_window_ghz=[freq[0], f_max],
        time_window_ns=m * dt_ns,
        time_resolution_ns=dt_ns,
        warnings=warnings,
    )


def known_network_sdd11(
    kind: str,
    n_points: int = 128,
    f_max_ghz: float = 64.0,
) -> tuple[List[float], List[complex]]:
    """生成已知网络的理想复数 SDD11（均匀 DC 锚定网格），用于数值测试。

    - ``matched_load``：Γ(f) = 0。
    - ``open``：Γ(f) = +1。
    - ``short``：Γ(f) = -1。
    """
    if n_points < 2:
        raise ValueError("n_points 至少为 2。")
    df = f_max_ghz / n_points
    freq = [(k + 1) * df for k in range(n_points)]
    if kind in ("matched_load", "load"):
        gamma = [0.0 + 0.0j] * n_points
    elif kind == "open":
        gamma = [1.0 + 0.0j] * n_points
    elif kind == "short":
        gamma = [-1.0 + 0.0j] * n_points
    else:
        raise ValueError(f"未知已知网络：{kind}")
    return freq, gamma


def export_tdr_json(result: TdrResult, path: Any) -> None:
    """写出 ``via_tdr.json``。"""
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(result.to_dict(), stream, ensure_ascii=False, indent=2)


def export_tdr_csv(result: TdrResult, path: Any) -> None:
    """写出便于人工核对的 TDR CSV（每行一个时间采样）。"""
    with open(path, "w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        header = ["time_ns"]
        if result.distance_mm:
            header.append("distance_mm")
        header += ["reflection", "impedance_ohm"]
        writer.writerow(header)
        for index, t in enumerate(result.time_ns):
            row = [f"{t:.9g}"]
            if result.distance_mm:
                row.append(f"{result.distance_mm[index]:.9g}")
            row.append(f"{result.reflection[index]:.9g}")
            row.append(f"{result.impedance_ohm[index]:.9g}")
            writer.writerow(row)
