from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from PIL import Image, ImageStat


ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT_ROOT = ROOT / "verification" / "screenshots"
EXPECTED = {
    "1120x720": (1120, 720, "normal"),
    "1900x1050": (1900, 1050, "large"),
    "2560x1400": (2560, 1400, "ultra"),
}
PAGES = (
    "gnd_via.png",
    "antipad_stackup.png",
    "capacitor_model.png",
    "model_preview.png",
)


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "verification" / "visual_acceptance.json"
    if not target.is_absolute():
        target = ROOT / target
    evidence = []
    errors = []
    for folder, (width, height, tier) in EXPECTED.items():
        for filename in PAGES:
            path = SCREENSHOT_ROOT / folder / filename
            if not path.is_file():
                errors.append(f"missing screenshot: {path.relative_to(ROOT)}")
                continue
            with Image.open(path) as image:
                actual_size = tuple(image.size)
                rgb = image.convert("RGB")
                variance = tuple(round(value, 3) for value in ImageStat.Stat(rgb).var)
            if actual_size != (width, height):
                errors.append(f"wrong size for {path.relative_to(ROOT)}: {actual_size}")
            if max(variance, default=0.0) < 25.0:
                errors.append(f"screenshot appears blank: {path.relative_to(ROOT)}")
            evidence.append({
                "path": path.relative_to(ROOT).as_posix(),
                "target_size": [width, height],
                "actual_size": list(actual_size),
                "font_tier": tier,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "rgb_variance": list(variance),
            })
    payload = {
        "passed": not errors and len(evidence) == 12,
        "screenshot_count": len(evidence),
        "default_stackup": "M6G / 29 layers",
        "capture_method": "Pillow ImageGrab(window=Tk HWND) / Windows PrintWindow",
        # These values are NOT measured by this script.  It only checks that
        # each screenshot exists, has the target size and is not blank; it
        # records a sha256 but compares it against no baseline.  The entries
        # below are what a person reported after looking at the screenshots, so
        # they are labelled as a declaration rather than presented as an
        # automated result.
        "manual_review_declared": {
            "measured_by_this_script": False,
            "declared_by": "operator reviewing the captured screenshots",
            "text_overlap_or_clipping_found": False,
            "gnd_legend_items_visible": 8,
            "last_stackup_layer_visible": True,
            "scrollbars_visible_where_required": True,
            "capacitor_core_labels_visible": ["P", "N", "X", "Y", "pad_pitch", "cap_pitch"],
            "model_ports_visible": 4,
            "airbox_boundary_label_combined": True,
        },
        "screenshots": evidence,
        "errors": errors,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "passed": payload["passed"],
        "screenshot_count": len(evidence),
        "output": str(target),
        "errors": errors,
    }, ensure_ascii=False))
    return 0 if payload["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
