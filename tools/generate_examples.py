"""Regenerate the checked-in V2 example configurations."""
from __future__ import annotations
import copy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config_manager import ConfigManager, apply_package_preset, save_config


def main() -> int:
    examples = ROOT / "examples"
    examples.mkdir(exist_ok=True)
    base = ConfigManager(ROOT).default()
    base["project"]["overwrite_existing"] = True
    save_config(base, examples / "default_mm.json")
    mil = copy.deepcopy(base)
    mil["units"] = "mil"
    save_config(mil, examples / "equivalent_mil.json")
    no_via = copy.deepcopy(base)
    no_via["project"]["project_name"] = "Surface_Cap_Antipad_V2_NoVia"
    no_via["gnd_vias"]["enabled"] = False
    save_config(no_via, examples / "no_via.json")
    four = copy.deepcopy(base)
    four["project"]["project_name"] = "Surface_Cap_Antipad_V2_FourCorner"
    four["gnd_vias"]["enabled"] = True
    four["gnd_vias"]["pattern"] = "four_corner"
    four["gnd_vias"]["count"] = 4
    save_config(four, examples / "four_corner_vias.json")
    fence = copy.deepcopy(base)
    fence["project"]["project_name"] = "Surface_Cap_Antipad_V2_RectFence"
    fence["gnd_vias"]["enabled"] = True
    fence["gnd_vias"]["pattern"] = "rectangular_fence"
    fence["gnd_vias"]["count"] = 8
    save_config(fence, examples / "rectangular_fence_vias.json")
    p0201 = apply_package_preset(base, "0201")
    p0201["project"]["project_name"] = "Surface_Cap_Antipad_V2_0201"
    save_config(p0201, examples / "package_0201.json")
    p0402 = apply_package_preset(base, "0402")
    p0402["project"]["project_name"] = "Surface_Cap_Antipad_V2_0402"
    save_config(p0402, examples / "package_0402.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
