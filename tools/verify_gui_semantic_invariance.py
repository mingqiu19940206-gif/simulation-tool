from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


SEMANTIC_FILES = (
    "config/default_config.json",
    "src/config_manager.py",
    "src/geometry_calculator.py",
    "src/geometry_v1.py",
    "src/gnd_via_semantics.py",
    "src/hfss_script_generator.py",
    "src/model_validator.py",
    "src/simulation_sweep_semantics.py",
    "src/sweep_manager.py",
)

PROBE = r'''
import hashlib
import json
from pathlib import Path

import hfss_script_generator as generator_module
from config_manager import ConfigManager, make_generation_snapshot, validate_config
from geometry_calculator import GeometryCalculator

root = Path.cwd()
fixed_build = {
    "software_version": "SEMANTIC-PROBE",
    "build_time_utc": "2000-01-01T00:00:00Z",
    "source_hash": "0" * 64,
    "sweep_backend_version": "native-optimetrics-v3.2",
    "config_schema_version": 2,
    "source_files": [],
}
generator_module.get_build_info = lambda: dict(fixed_build)
result = {}
for unit in ("mm", "mil"):
    for enabled in (False, True):
        config = ConfigManager(root).default()
        config["units"] = unit
        config["gnd_vias"]["enabled"] = enabled
        config["simulation"]["enabled"] = True
        normalized, validation = validate_config(config, root, check_output_directory=False)
        if not validation.is_valid:
            raise RuntimeError(validation.errors)
        geometry = GeometryCalculator(normalized).calculate()
        script = generator_module.HfssScriptGenerator.build_script_text(normalized, geometry)
        snapshot = make_generation_snapshot(normalized, geometry, "semantic-probe")
        payload = {
            "config": normalized,
            "geometry": geometry.to_dict(),
            "snapshot": snapshot,
            "script_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest(),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        result[f"{unit}:via={enabled}"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
print(json.dumps(result, ensure_ascii=False, sort_keys=True))
'''


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def probe(root: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(root / "src")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", PROBE],
        cwd=root, env=environment, capture_output=True, text=True, timeout=120,
    )
    if completed.returncode:
        raise RuntimeError(completed.stdout + completed.stderr)
    return json.loads(completed.stdout.strip().splitlines()[-1])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline_root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    current_root = Path(__file__).resolve().parents[1]
    baseline_root = args.baseline_root.resolve()

    files: dict[str, Any] = {}
    for relative in SEMANTIC_FILES:
        baseline_hash = sha256(baseline_root / relative)
        current_hash = sha256(current_root / relative)
        files[relative] = {
            "baseline_sha256": baseline_hash,
            "current_sha256": current_hash,
            "equal": baseline_hash == current_hash,
        }

    baseline_probe = probe(baseline_root)
    current_probe = probe(current_root)
    default_stackup = json.loads((current_root / "config/default_config.json").read_text(encoding="utf-8-sig"))["stackup"]
    payload = {
        "passed": all(item["equal"] for item in files.values()) and baseline_probe == current_probe,
        "baseline_root": str(baseline_root),
        "current_root": str(current_root),
        "semantic_file_hashes": files,
        "runtime_cases": current_probe,
        "runtime_equal": baseline_probe == current_probe,
        "m6g_layer_count": len(default_stackup),
        "m6g_layer_names": [layer["name"] for layer in default_stackup],
        "verified_scope": [
            "configuration normalization and mm/mil conversion",
            "29-layer M6G geometry with GND VIA disabled/enabled",
            "ports, airbox and simulation geometry",
            "generation snapshot",
            "HFSS script text with normalized build identity",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": payload["passed"], "cases": len(current_probe),
                      "semantic_files": len(files)}, ensure_ascii=False))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
