"""Phase 3 背钻与残桩：几何派生 + 语义校验（纯 Python，无 tkinter/shapely/AEDT）。"""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from config_manager import ConfigManager
from geometry_calculator import GeometryCalculator
from via_transition_semantics import validate_via_transition


def _phase3_cfg():
    manager = ConfigManager(ROOT)
    cfg = copy.deepcopy(manager.default())
    cfg["scenario"]["type"] = "via_transition"
    # Phase 3 范围：L1 entry、L10 exit、L1–L14 机械贯通、bottom 背钻。
    cfg["via_transition"]["exit_layer"] = "L10"
    cfg["via_transition"]["backdrill"]["enabled"] = True
    cfg["via_transition"]["backdrill"]["from_side"] = "auto"
    return cfg


class ViaTransitionBackdrillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = ConfigManager(ROOT)
        self.cfg = _phase3_cfg()

    def _layer(self, geometry, name):
        for layer in geometry.stackup:
            if layer.name == name:
                return layer
        raise KeyError(name)

    def test_01_backdrill_derives_bottom_side(self) -> None:
        geometry = GeometryCalculator(self.cfg).calculate()
        self.assertFalse(geometry.errors, geometry.errors)
        for via in geometry.signal_vias:
            self.assertTrue(via.backdrilled)
            self.assertEqual(via.backdrill_from_side, "bottom")

    def test_02_effective_barrel_removes_stub_below_exit(self) -> None:
        geometry = GeometryCalculator(self.cfg).calculate()
        exit_layer = self._layer(geometry, "L10")
        for via in geometry.signal_vias:
            expected = exit_layer.z_bottom - via.target_residual_stub
            self.assertAlmostEqual(via.effective_barrel_z_bottom, expected, places=6)
            # L10 以下不应保留的铜桶被去除：有效孔桶底部高于原始孔桶底部。
            self.assertGreater(via.effective_barrel_z_bottom, via.z_bottom)
            self.assertAlmostEqual(via.effective_barrel_z_top, via.z_top, places=6)

    def test_03_target_and_actual_residual_stub_match(self) -> None:
        geometry = GeometryCalculator(self.cfg).calculate()
        for via in geometry.signal_vias:
            self.assertAlmostEqual(via.actual_residual_stub, via.target_residual_stub, places=9)
            self.assertAlmostEqual(via.target_residual_stub, 0.25, places=9)

    def test_04_semantics_passes_valid_backdrill(self) -> None:
        geometry = GeometryCalculator(self.cfg).calculate()
        issues = validate_via_transition(self.cfg, geometry)
        errors = [i for i in issues if i[1] == "error"]
        self.assertFalse(errors, errors)

    def test_05_backdrill_diameter_must_exceed_drill(self) -> None:
        cfg = copy.deepcopy(self.cfg)
        cfg["via_transition"]["backdrill"]["drill_diameter"] = 0.15
        geometry = GeometryCalculator(cfg).calculate()
        self.assertTrue(any("必须大于" in e for e in geometry.errors), geometry.errors)
        issues = validate_via_transition(cfg, geometry)
        self.assertTrue(any(i[0] == "VIA-TRN-045" for i in issues))

    def test_06_internal_exit_without_backdrill_rejected(self) -> None:
        cfg = copy.deepcopy(self.cfg)
        cfg["via_transition"]["backdrill"]["enabled"] = False
        geometry = GeometryCalculator(cfg).calculate()
        self.assertTrue(any("残桩" in e for e in geometry.errors), geometry.errors)
        issues = validate_via_transition(cfg, geometry)
        self.assertTrue(any(i[0] == "VIA-TRN-050" for i in issues))

    def test_07_auto_ambiguous_when_both_stubs(self) -> None:
        cfg = copy.deepcopy(self.cfg)
        cfg["via_transition"]["entry_layer"] = "L3"  # 顶部也产生残桩
        geometry = GeometryCalculator(cfg).calculate()
        self.assertTrue(any("无法唯一" in e for e in geometry.errors), geometry.errors)
        issues = validate_via_transition(cfg, geometry)
        self.assertTrue(any(i[0] == "VIA-TRN-046" for i in issues))


if __name__ == "__main__":
    unittest.main(verbosity=2)
