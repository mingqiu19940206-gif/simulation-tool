"""Run source-tree checks through the packaged Python runtime on Windows.

This helper is useful on clean review machines where the V2 executable is
available but a system Python installation is intentionally absent.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

for source_path in ROOT.rglob("*.py"):
    if any(part in {"generated", "generated_v2", "build", "dist", "dist_v2"}
           for part in source_path.parts):
        continue
    compile(source_path.read_text(encoding="utf-8"), str(source_path), "exec")

# The frozen launcher imports a few bundled copies before dispatching this
# file.  Remove them so unittest exercises the current workspace sources.
local_modules = {path.stem for path in ROOT.glob("*.py")}
for name in tuple(sys.modules):
    if name in local_modules:
        sys.modules.pop(name, None)

from config_manager import ConfigManager
from geometry_calculator import GeometryCalculator
from model_preview import build_model_scene

manager = ConfigManager(ROOT)
config = manager.default(); config["simulation"]["enabled"] = True
config, validation = manager.validate(config, check_output_directory=False)
geometry = GeometryCalculator(config).calculate()
scene = build_model_scene(config, geometry, validation)
ports = [item for item in scene.objects if item.category == "Port"]
assert {item.name for item in ports} == {
    "PORT_IN_P", "PORT_IN_N", "PORT_OUT_P", "PORT_OUT_N",
}
assert all(item.default_visible for item in ports)
assert all(item.details["参考导体"] for item in ports)
airbox = next(item for item in scene.objects if item.key == "airbox")
boundary = next(item for item in scene.objects if item.key == "open_boundary")
assert airbox.default_visible and airbox.object_type == "box_outline"
assert boundary.default_visible and boundary.category == "开放边界"

disabled = manager.default()
disabled["simulation"]["enabled"] = False
disabled["gnd_vias"]["enabled"] = False
disabled["capacitor"]["create_body_in_hfss"] = False
disabled, validation = manager.validate(disabled, check_output_directory=False)
disabled_scene = build_model_scene(disabled, GeometryCalculator(disabled).calculate(), validation)
disabled_simulation = [item for item in disabled_scene.objects
                       if item.category in {"Port", "空气盒", "开放边界"}]
assert len([item for item in disabled_simulation if item.category == "Port"]) == 4
assert len(disabled_simulation) == 6

from main_gui import SurfaceCapAntipadApp
app = SurfaceCapAntipadApp(); app.withdraw(); app.update_idletasks()
app.variables["simulation.enabled"].set(False)
app.refresh_visuals()
workbench = app.model_preview_workbench
workbench.update_pending()
port_children = workbench.tree.get_children("cat:Port")
assert len(port_children) == 4
assert all(workbench.objects[key].status == "未启用"
           for key in workbench.objects if workbench.objects[key].category == "Port")
assert "Setup：未创建" in workbench.summary_var.get()
workbench.show(config, geometry, validation); workbench.update_pending()
assert workbench.visibility["airbox"]
assert all(workbench.visibility[item.key] for item in ports)
workbench.quick_vars["Port"].set(False); workbench._quick_toggle("Port")
assert not any(workbench.visibility[key] for key, item in workbench.objects.items() if item.category == "Port")
workbench.tree.selection_set("cat:Port"); workbench.toggle_visibility()
assert workbench.quick_vars["Port"].get()
workbench.select_object("airbox"); workbench.clear_selection()
workbench.airbox_mode.set("仅开放边界"); workbench._airbox_mode_changed()
assert not workbench.visibility["airbox"] and workbench.visibility["open_boundary"]
workbench.restore_visibility()
workbench.fit_model(); workbench.fit_all(); app.destroy()

print("SOURCE_CHECKS_OK")
