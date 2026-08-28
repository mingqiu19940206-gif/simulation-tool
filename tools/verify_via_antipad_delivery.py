"""Verify both checked-in VIA examples without launching AEDT."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from config_manager import ConfigManager  # noqa: E402
from hfss_script_generator import HfssScriptGenerator  # noqa: E402
from model_validator import ModelValidator  # noqa: E402


EXAMPLES = ("via_transition_through.json", "via_transition_backdrill.json")


def main() -> int:
    output_root = (Path(sys.argv[1]).expanduser().resolve() if len(sys.argv) > 1
                   else ROOT / "verification" / "via_antipad")
    output_root.mkdir(parents=True, exist_ok=True)
    manager = ConfigManager(ROOT)
    records = []
    passed = True
    for filename in EXAMPLES:
        source = ROOT / "examples" / filename
        config, corrections = manager.load(source)
        report, normalized, geometry = ModelValidator(manager).run_local(config)
        record = {
            "example": str(source),
            "migration_messages": corrections,
            "local_validate_state": report.state,
            "local_validate_counts": report.counts,
            "contour_count": len(getattr(geometry, "antipad_contours", [])) if geometry else 0,
            "shapes": sorted({item.shape for item in getattr(geometry, "antipad_contours", [])}),
        }
        if not report.can_continue or normalized is None or geometry is None:
            record["error"] = "local Validate blocked generation"
            passed = False
            records.append(record)
            continue
        normalized["project"]["overwrite_existing"] = True
        generated = HfssScriptGenerator(normalized, ROOT).generate(output_root / source.stem)
        completed = subprocess.run(
            [sys.executable, str(generated.script_path), "--validate-only"],
            cwd=generated.project_directory,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        record.update({
            "hfss_script": str(generated.script_path),
            "geometry_summary": str(generated.geometry_summary_path),
            "config_and_geometry_snapshot": str(generated.config_path),
            "local_validate_json": str(generated.local_validate_json_path),
            "validate_only_exit_code": completed.returncode,
            "validate_only_output_tail": completed.stdout[-1500:],
        })
        passed = passed and completed.returncode == 0
        records.append(record)
    payload = {
        "passed": passed,
        "example_count": len(records),
        "real_aedt_hfss_executed": False,
        "examples": records,
    }
    report_path = output_root / "via_antipad_acceptance.json"
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": passed, "report": str(report_path),
                      "example_count": len(records)}, ensure_ascii=False))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
