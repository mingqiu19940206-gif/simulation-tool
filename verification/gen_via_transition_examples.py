"""Generate via_transition example configs (schema v3) from the default config:
- examples/via_transition_through.json   (Phase 2: L1->L14, no backdrill)
- examples/via_transition_backdrill.json (Phase 3: L1->L10, bottom backdrill)
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from config_manager import ConfigManager

manager = ConfigManager(ROOT)

through = copy.deepcopy(manager.default())
through["scenario"]["type"] = "via_transition"
through["via_transition"]["exit_layer"] = "L14"
through["via_transition"]["backdrill"]["enabled"] = False
manager.save(through, ROOT / "examples" / "via_transition_through.json")

backdrill = copy.deepcopy(manager.default())
backdrill["scenario"]["type"] = "via_transition"
backdrill["via_transition"]["exit_layer"] = "L10"
backdrill["via_transition"]["backdrill"]["enabled"] = True
backdrill["via_transition"]["backdrill"]["from_side"] = "auto"
manager.save(backdrill, ROOT / "examples" / "via_transition_backdrill.json")

for name in ("via_transition_through", "via_transition_backdrill"):
    loaded, _ = manager.load(ROOT / "examples" / f"{name}.json")
    vt = loaded["via_transition"]
    print(name, "-> scenario", loaded["scenario"]["type"], "entry", vt["entry_layer"],
          "exit", vt["exit_layer"], "backdrill", vt["backdrill"]["enabled"])
print("DONE")
