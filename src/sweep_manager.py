"""Manage one-project HFSS sweeps and the explicit legacy backend."""
from __future__ import annotations
import copy
import json
import math
import re
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from build_info import get_build_info
from config_manager import normalize_config
from scenario_semantics import require_sweep_supported_scenario
from hfss_script_generator import GenerationResult, HfssScriptGenerator, ScriptGenerationError
from simulation_sweep_semantics import (
    apply_sweep_value,
    inspect_sweep_plan,
    normalize_execution_strategy,
    iter_variations,
    iter_materialized_variations,
    preflight_sweep_geometry,
)


@dataclass(frozen=True)
class SweepGenerationResult:
    root: Path
    manifest_path: Path
    cases: Tuple[GenerationResult, ...]
    variations: Tuple[Mapping[str, Any], ...] = ()

    @property
    def project(self) -> GenerationResult:
        """Return the only generated project for a native Optimetrics sweep."""

        if len(self.cases) != 1:
            raise ScriptGenerationError(
                "Native Optimetrics 必须且只能生成一个 Project/Design 脚本。"
            )
        return self.cases[0]


def _values(start: float, stop: float, step: float, integer: bool = False) -> List[float | int]:
    count = int(math.floor((stop - start) / step + 1e-9)) + 1
    values = [start + index * step for index in range(max(0, count))]
    if values and values[-1] < stop - 1e-9:
        values.append(stop)
    return [int(round(value)) for value in values] if integer else values


def expand_sweep_cases(config: Mapping[str, Any], max_cases: int = 200) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    normalized, _ = normalize_config(config)
    plan = inspect_sweep_plan(normalized, max_cases)
    errors = [message for level, message in plan.issues if level == "error"]
    if errors:
        raise ScriptGenerationError("设计参数扫描预检失败：\n" + "\n".join("  [错误] " + item for item in errors))
    try:
        variations = iter_materialized_variations(plan, normalized)
    except ValueError as exc:
        raise ScriptGenerationError(f"设计参数扫描展开失败：{exc}") from exc
    total = len(variations)
    if total > max_cases:
        raise ScriptGenerationError(f"扫描将生成 {total} 个案例，超过安全上限 {max_cases}。请缩小范围。")
    cases = []
    for variation in variations:
        case = copy.deepcopy(normalized)
        for name, value in variation.items():
            apply_sweep_value(case, name, value)
        for sweep in case["sweeps"].values():
            if isinstance(sweep, dict):
                sweep["enabled"] = False
        case["baseline_comparison"]["enabled"] = False
        cases.append((normalize_config(case)[0], variation))
    return cases


def generate_sweep_cases(config: Mapping[str, Any], output_root: Path, max_cases: int = 200) -> SweepGenerationResult:
    root = Path(output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    base, _ = normalize_config(config)
    plan = inspect_sweep_plan(base, max_cases)
    preflight = preflight_sweep_geometry(base, max_cases)
    if preflight.errors:
        raise ScriptGenerationError(
            "离散 Design/case 几何预检失败：\n"
            + "\n".join("  [错误] " + item for item in preflight.errors)
        )
    generated: List[GenerationResult] = []
    manifest_cases = []
    for index, (case, variation) in enumerate(expand_sweep_cases(base, max_cases), 1):
        suffix = "_".join(re.sub(r"[^A-Za-z0-9]+", "_", f"{key}_{value}").strip("_") for key, value in variation.items())
        case["project"]["project_name"] = (base["project"]["project_name"] + f"_CASE_{index:03d}" + ("_" + suffix if suffix else ""))[:180]
        case["project"]["overwrite_existing"] = True
        result = HfssScriptGenerator(case, root).generate(root)
        generated.append(result)
        manifest_cases.append({"index": index, "variation": variation, "project": case["project"]["project_name"],
                               "script": str(result.script_path.relative_to(root)), "status": "generated",
                               "return_code": None, "message": "", "result_files": []})
    sim = base.get("simulation", {})
    selected_metrics = [name for name, enabled in sim.get("report_outputs", {}).items()
                        if enabled and sim.get("enabled") and sim.get("generate_reports") and sim.get("create_differential_ports")]
    if sim.get("enabled") and sim.get("generate_tdr"):
        selected_metrics.append("differential_TDR_review_request")
    build = get_build_info()
    manifest = {
        "schema_version": 3,
        "strategy": "discrete_recalculated_full_factorial",
        "requested_strategy": plan.strategy,
        "backend": "discrete_design_cases",
        "execution_ready": True,
        "execution_blocked_reason": "",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "simulation_enabled": bool(sim.get("enabled")),
        "metrics": selected_metrics,
        "project_count": len(generated),
        "design_count": len(generated),
        "optimetrics_setup_count": 0,
        "design_variation_count": len(manifest_cases),
        "software_version": build["software_version"],
        "source_hash": build["source_hash"],
        "topology_axes": [path for path, _values in plan.topology_axes],
        "native_axes_materialized_discretely": [path for path, _values in plan.native_axes],
        "preflight": {
            "sampled_variations": preflight.sampled_variations,
            "errors": list(preflight.errors),
            "warnings": list(preflight.warnings),
            "topology_changes": list(preflight.topology_changes),
        },
        "cases": manifest_cases,
        "variations": manifest_cases,
        "execution": {"status": "generated", "return_code": None, "message": ""},
    }
    path = root / "sweep_manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return SweepGenerationResult(root, path, tuple(generated), tuple(
        item["variation"] for item in manifest_cases
    ))


def update_manifest_case_status(manifest_path: Path, index: int, status: str,
                                return_code: Optional[int] = None, message: str = "") -> None:
    path = Path(manifest_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("backend") == "native_optimetrics":
        execution = manifest.setdefault("execution", {})
        execution.update({
            "status": status,
            "return_code": return_code,
            "message": message,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        for variation in manifest.get("variations", []):
            variation["status"] = status
            variation["message"] = message
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return
    entries = manifest.get("cases", manifest.get("variations", []))
    for case in entries:
        if int(case.get("index", -1)) == int(index):
            case["status"] = status
            case["return_code"] = return_code
            case["message"] = message
            case["updated_at"] = datetime.now(timezone.utc).isoformat()
            break
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def summarize_sweep_results(manifest_path: Path) -> Path:
    """Create a durable status/result-file index without pretending to parse absent data."""
    path = Path(manifest_path).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    counts: Dict[str, int] = {}
    entries = manifest.get("cases", manifest.get("variations", []))
    for case in entries:
        status = str(case.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
        script = case.get("script")
        if not script and manifest.get("projects"):
            script = manifest["projects"][0].get("script")
        case_dir = path.parent / Path(script).parent if script else path.parent
        files = sorted(str(item.relative_to(path.parent)) for item in case_dir.glob("*.csv"))
        case["result_files"] = files
    summary = {
        "schema_version": 3,
        "source_manifest": path.name,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "project_count": manifest.get("project_count", 0),
        "design_count": manifest.get("design_count", 0),
        "optimetrics_setup_count": manifest.get("optimetrics_setup_count", 0),
        "design_variation_count": manifest.get("design_variation_count", len(manifest.get("cases", []))),
        "execution_ready": manifest.get("execution_ready", True),
        "execution_blocked_reason": manifest.get("execution_blocked_reason", ""),
        "frequency_sweep": manifest.get("frequency_sweep", {}),
        "counts": counts,
        "metrics": manifest.get("metrics", []),
        "variations": manifest.get("variations", []),
        "cases": manifest.get("cases", []),
    }
    target = path.parent / "sweep_results_summary.json"
    target.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


# Keep the former generator available only as an explicit compatibility path.
generate_legacy_discrete_cases = generate_sweep_cases


def generate_native_optimetrics_sweep(config: Mapping[str, Any], output_root: Path,
                                      max_cases: int = 200) -> SweepGenerationResult:
    """Generate one AEDT project containing a native Optimetrics plan.

    The generated project contains one primary HFSS Design and one frequency
    sweep.  Design-parameter combinations are represented by Optimetrics
    Variations; no ``*_CASE_*`` project, JSON or script is emitted.
    """

    base, _ = normalize_config(config)
    plan = inspect_sweep_plan(base, max_cases)
    hard_errors = [message for level, message in plan.issues if level == "error"]
    if hard_errors:
        raise ScriptGenerationError("扫描计划预检失败：\n" + "\n".join("  [错误] " + item for item in hard_errors))
    preflight = preflight_sweep_geometry(base, max_cases)
    # Keep the validated nominal Design reproducible even when one sampled
    # Variation is geometrically invalid.  The error is persisted in the
    # manifest and shown by the workbench; it is never converted into a silent
    # fallback or an independent CASE project.

    root = Path(output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    # The generator itself creates ``root/project_name``.  Keep the project
    # name unchanged so the AEDT file remains stable when the scan changes.
    result = HfssScriptGenerator(base, root).generate(root)
    variations = iter_variations(plan, base)
    sim = base.get("simulation", {})
    selected_metrics = [name for name, enabled in sim.get("report_outputs", {}).items()
                        if enabled and sim.get("enabled") and sim.get("generate_reports") and sim.get("create_differential_ports")]
    if sim.get("enabled") and sim.get("generate_tdr"):
        selected_metrics.append("differential_TDR_review_request")
    project_name = base["project"]["project_name"]
    relative_script = str(result.script_path.relative_to(root))
    topology_blocked = bool(plan.topology_axes)
    topology_reason = (
        "拓扑扫描已单独分类，但当前生成器尚未在同一 AEDT 工程中生成多个 HFSS Design。"
        "因此本计划不会执行原生数值 Optimetrics，也不会使用冻结拓扑回退。"
        if topology_blocked else ""
    )
    variation_rows = []
    for index, variation in enumerate(variations, 1):
        variation_rows.append({
            "index": index,
            "variation": variation,
            "status": "blocked" if topology_blocked else "generated",
            "message": topology_reason,
        })
    build = get_build_info()
    manifest = {
        "schema_version": 3,
        "strategy": plan.strategy,
        "variation_mode": plan.variation_mode,
        "backend": "native_optimetrics",
        "execution_ready": not topology_blocked,
        "execution_blocked_reason": topology_reason,
        "simulation_enabled": bool(sim.get("enabled")),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_count": 1,
        "design_count": plan.design_count,
        "optimetrics_setup_count": plan.optimetrics_setup_count,
        "design_variation_count": plan.case_count,
        "software_version": build["software_version"],
        "build_time_utc": build["build_time_utc"],
        "source_hash": build["source_hash"],
        "sweep_backend_version": build["sweep_backend_version"],
        "config_schema_version": build["config_schema_version"],
        "geometry_binding_verified": False,
        "frequency_sweep": {
            "setup": sim.get("setup_name", "Setup1"),
            "name": sim.get("sweep_name", "Sweep1"),
            "enabled": bool(sim.get("enabled")),
            "start_ghz": sim.get("start_frequency_ghz"),
            "stop_ghz": sim.get("stop_frequency_ghz"),
            "mode": sim.get("sweep_type", "Interpolating"),
        },
        "metrics": selected_metrics,
        "preflight": {
            "sampled_variations": preflight.sampled_variations,
            "errors": list(preflight.errors),
            "warnings": list(preflight.warnings),
            "topology_changes": list(preflight.topology_changes),
        },
        "projects": [{
            "project": project_name,
            "design": base["project"].get("design_name", "Surface_Cap_Antipad"),
            "script": relative_script,
            "aedt": str(result.aedt_project_path.relative_to(root)),
        }],
        "variations": variation_rows,
        "execution": {
            "status": "blocked" if topology_blocked else "generated",
            "return_code": None,
            "message": topology_reason,
            "script": relative_script,
        },
    }
    path = root / "sweep_manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _assert_native_manifest_safe(path)
    # Native output owns one generated artifact.  Variations have their own
    # explicit preview collection and are never represented as fake CASEs.
    return SweepGenerationResult(root, path, (result,), tuple(variations))


def _assert_native_manifest_safe(manifest_path: Path) -> None:
    """Fail closed if a stale discrete-CASE manifest reached the native path."""

    path = Path(manifest_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    serialized = json.dumps(manifest, ensure_ascii=False)
    errors = []
    if manifest.get("schema_version") != 3:
        errors.append("schema_version != 3")
    if manifest.get("backend") != "native_optimetrics":
        errors.append("backend != native_optimetrics")
    if manifest.get("strategy") == "discrete_recalculated_full_factorial":
        errors.append("检测到旧 discrete_recalculated_full_factorial")
    if "CASE" in serialized.upper():
        errors.append("manifest 中检测到 CASE 标记")
    if int(manifest.get("project_count", 0)) != 1:
        errors.append("project_count != 1")
    if errors:
        raise ScriptGenerationError(
            "检测到旧版/离散 CASE 生成契约，已禁止提示生成成功：" + "；".join(errors)
        )


assert_native_manifest_safe = _assert_native_manifest_safe


def generate_sweep_cases(config: Mapping[str, Any], output_root: Path,
                         max_cases: int = 200) -> SweepGenerationResult:
    """Generate native Optimetrics or explicit recalculated geometry cases.

    A pure numeric plan stays in one native HFSS Design.  A hybrid/topology
    plan is materialized as auditable discrete Design cases so no topology,
    clearance, or per-layer polygon is frozen inside a fake continuous axis.
    """

    require_sweep_supported_scenario(config)
    normalized, _ = normalize_config(config)
    strategy = normalize_execution_strategy(config)
    plan = inspect_sweep_plan(normalized, max_cases)
    if strategy == "legacy_discrete" or (
            plan.topology_axes and strategy in {"hybrid", "topology_variants"}):
        return generate_legacy_discrete_cases(config, output_root, max_cases)
    return generate_native_optimetrics_sweep(config, output_root, max_cases)
