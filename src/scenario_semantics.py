"""Schema v3 scenario typing and fail-closed dispatch helpers.

This module is intentionally dependency-free (standard library only) so every
entry point -- config, geometry, preview, ModelValidator, HFSS, sweep, GUI --
can import it without creating an import cycle.
"""
from __future__ import annotations

from typing import Any, Mapping

SURFACE_CAPACITOR = "surface_capacitor"
VIA_TRANSITION = "via_transition"
SUPPORTED_SCENARIO_TYPES = (SURFACE_CAPACITOR, VIA_TRANSITION)

VIA_TRANSITION_SWEEP_NOT_IMPLEMENTED = (
    "请先把仿真场景切换为差分 VIA 换层。"
    "扫描只接受已登记的原生几何轴，"
    "或可通过独立 HFSS Design/case 重新计算的拓扑/逐层轴。"
)


class ScenarioNotImplementedError(RuntimeError):
    """Raised by a dispatch point when the requested scenario has no implementation yet."""


def scenario_type(config: Mapping[str, Any]) -> str:
    """Return the declared scenario type, defaulting to ``surface_capacitor``.

    A legacy payload has no ``scenario`` node, which always means the existing
    AC-coupled capacitor scenario after migration.
    """

    scenario = config.get("scenario") if isinstance(config, Mapping) else None
    if isinstance(scenario, Mapping):
        value = scenario.get("type")
        if value:
            return str(value)
    return SURFACE_CAPACITOR


def require_known_scenario(config: Mapping[str, Any]) -> None:
    """Fail closed only for unknown scenario types.

    Both ``surface_capacitor`` and ``via_transition`` are recognized scenarios;
    individual dispatch points decide which capabilities are available.
    """

    kind = scenario_type(config)
    if kind not in SUPPORTED_SCENARIO_TYPES:
        raise ScenarioNotImplementedError(
            f"未知仿真场景 scenario.type={kind!r}；仅支持 surface_capacitor 与 via_transition。"
        )


def require_sweep_supported_scenario(config: Mapping[str, Any]) -> None:
    """放行已绑定的原生轴和可重算的离散轴；未实现轴 fail closed。"""

    if scenario_type(config) == VIA_TRANSITION:
        from via_transition_sweeps import VIA_NATIVE_SWEEP_PATHS, VIA_TOPOLOGY_SWEEP_PATHS

        enabled = [
            str(path) for path, sweep in config.get("sweeps", {}).items()
            if isinstance(sweep, Mapping) and sweep.get("enabled")
        ]
        supported = set(VIA_NATIVE_SWEEP_PATHS) | set(VIA_TOPOLOGY_SWEEP_PATHS)
        per_layer_prefix = "via_transition.padstack.antipad.per_layer."
        per_layer_fields = {
            "diameter", "length", "width", "corner_radius", "rotation_deg", "clearance",
        }
        unsupported = [
            path for path in enabled
            if path not in supported and not (
                path.startswith(per_layer_prefix)
                and len(path.split(".")) == 6
                and path.rsplit(".", 1)[-1] in per_layer_fields
            )
        ]
        if unsupported:
            raise ScenarioNotImplementedError(
                VIA_TRANSITION_SWEEP_NOT_IMPLEMENTED
                + " 当前请求未完成原生绑定或离散重算实现："
                + ", ".join(sorted(unsupported))
            )
