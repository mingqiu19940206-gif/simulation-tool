"""End-to-end local verification that does not launch AEDT."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
os.chdir(ROOT)

from build_info import get_build_info
from config_manager import ConfigManager, normalize_config, to_external_config
from geometry_calculator import GeometryCalculator
from hfss_script_generator import HfssScriptGenerator
from model_preview import build_model_scene
from model_validator import ModelValidator
from sweep_manager import assert_native_manifest_safe, generate_sweep_cases


def assert_true(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def configs_roundtrip_equivalent(expected: object, actual: object) -> bool:
    """Compare configs at the documented persistence boundary.

    Internal engineering lengths are normalized to mm.  Saving converts them
    to the selected external unit and loading converts them back, which can
    change the last binary digit.  Converting both configs back to their
    external JSON form removes only that defined unit-conversion noise; the
    resulting structures and values are still compared strictly.
    """
    return to_external_config(expected) == to_external_config(actual)


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "verification" / "source_verification.json"
    if not target.is_absolute():
        target = ROOT / target
    checks: dict[str, object] = {}
    try:
        manager = ConfigManager(ROOT)
        config = manager.default()
        checks["default_config_loaded"] = True

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            roundtrip = temporary / "roundtrip.json"
            manager.save(config, roundtrip)
            loaded, corrections = manager.load(roundtrip)
            roundtrip_equivalent = configs_roundtrip_equivalent(config, loaded)
            checks["config_roundtrip"] = roundtrip_equivalent
            checks["config_roundtrip_corrections"] = corrections
            assert_true(roundtrip_equivalent, "default configuration did not round-trip")

            report, normalized, geometry = ModelValidator(manager).run_local(config)
            checks["validate_state"] = report.state
            checks["validate_counts"] = report.counts
            assert_true(report.can_continue and normalized is not None and geometry is not None,
                        "local Validate did not pass")

            scene = build_model_scene(normalized, geometry, report)
            categories = sorted({item.category for item in scene.objects})
            ports = [item for item in scene.objects if item.category == "Port"]
            keys = {item.key for item in scene.objects}
            checks["scene_object_count"] = len(scene.objects)
            checks["scene_categories"] = categories
            checks["port_count"] = len(ports)
            checks["airbox_present"] = "airbox" in keys
            checks["open_boundary_present"] = "open_boundary" in keys
            assert_true(len(ports) == 4 and {"airbox", "open_boundary"} <= keys,
                        "3D scene lacks four ports, airbox, or open boundary")

            result = HfssScriptGenerator(normalized, ROOT).generate(temporary / "hfss")
            generated_script = result.script_path.read_text(encoding="utf-8")
            validate_only = subprocess.run(
                [sys.executable, str(result.script_path), "--validate-only"],
                cwd=result.project_directory,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            tokens = {
                "ports": "PORT_IN_P" in generated_script and "PORT_OUT_N" in generated_script,
                "airbox": "AIRBOX" in generated_script,
                "antipad": "antipad" in generated_script.lower(),
                "gnd_via": "gnd_via" in generated_script.lower(),
                "stackup": "stackup" in generated_script.lower(),
                "optimetrics": "create_optimetrics_parametric_setup" in generated_script and "OptiParametric" in generated_script,
                "hfss_variables": "design_variables" in generated_script,
                "direct_45": "direct_45" in generated_script,
                "smooth_s_bend": "smooth_s_bend" in generated_script,
            }
            checks["hfss_script_tokens"] = tokens
            checks["hfss_validate_only_exit_code"] = validate_only.returncode
            checks["hfss_validate_only_output_tail"] = validate_only.stdout[-1500:]
            assert_true(all(tokens.values()), "generated HFSS script lacks required object/variable paths")
            assert_true(validate_only.returncode == 0, "generated HFSS script --validate-only failed")

            smooth = copy.deepcopy(config)
            smooth["trace"]["pad_escape_mode"] = "smooth_s_bend"
            smooth["trace"]["transition_length"] = 0.8
            smooth, smooth_validation = manager.validate(smooth, check_output_directory=False)
            smooth_geometry = GeometryCalculator(smooth).calculate()
            assert_true(smooth_validation.is_valid and not smooth_geometry.errors,
                        "smooth S-bend geometry failed")
            checks["smooth_s_bend_geometry"] = True

            direct = copy.deepcopy(config)
            direct["trace"]["pad_escape_mode"] = "direct_45"
            direct, direct_validation = manager.validate(direct, check_output_directory=False)
            direct_geometry = GeometryCalculator(direct).calculate()
            assert_true(direct_validation.is_valid and not direct_geometry.errors,
                        "direct 45-degree geometry failed")
            checks["direct_45_geometry"] = True

            native = copy.deepcopy(config)
            native["project"]["overwrite_existing"] = True
            native["antipad"]["mode"] = "merged_all"
            native["sweep_execution"]["strategy"] = "native_optimetrics"
            for node in native["sweeps"].values():
                node["enabled"] = False
            native["sweeps"]["antipad.global_expansion"].update(
                enabled=True, start=0.1, stop=0.4, step=0.05
            )
            native_result = generate_sweep_cases(native, temporary / "native")
            assert_native_manifest_safe(native_result.manifest_path)
            native_manifest = json.loads(native_result.manifest_path.read_text(encoding="utf-8"))
            checks["native_optimetrics"] = {
                key: native_manifest.get(key) for key in (
                    "backend", "project_count", "design_count", "optimetrics_setup_count",
                    "design_variation_count",
                )
            }
            assert_true(native_manifest.get("backend") == "native_optimetrics", "wrong sweep backend")
            assert_true(native_manifest.get("project_count") == 1, "native sweep is not one project")
            assert_true(native_manifest.get("design_count") == 1, "native sweep is not one design")
            assert_true(native_manifest.get("optimetrics_setup_count") == 1, "missing Optimetrics setup")

        payload = {
            "passed": True,
            "software_build": get_build_info(),
            "checks": checks,
            "real_aedt_hfss_executed": False,
        }
        exit_code = 0
    except Exception as exc:
        import traceback
        payload = {
            "passed": False,
            "software_build": get_build_info(),
            "checks": checks,
            "real_aedt_hfss_executed": False,
            "error": repr(exc),
            "traceback": traceback.format_exc(),
        }
        exit_code = 2
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
