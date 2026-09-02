"""Phase 3 背钻与残桩：几何派生 + 语义校验（纯 Python，无 tkinter/shapely/AEDT）。"""
from __future__ import annotations

import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from config_manager import (ConfigManager, make_generation_snapshot,
                            validate_generation_snapshot)
from geometry_calculator import GeometryCalculator
from hfss_script_generator import HfssScriptGenerator
from model_preview import build_via_transition_scene
from via_transition_geometry import derive_functional_barrel_span
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


def _reversed_cfg():
    """需求场景：L14 输入、L12 输出，只有物理顶部残桩。"""
    cfg = _phase3_cfg()
    vt = cfg["via_transition"]
    vt.update({"entry_layer": "L14", "exit_layer": "L12",
               "drill_start_layer": "L1", "drill_end_layer": "L14"})
    vt["padstack"].update({
        "drill_diameter": 8.0 * 0.0254,
        "finished_hole_diameter": 6.0 * 0.0254,
        "plating_thickness": 1.0 * 0.0254,
    })
    vt["backdrill"].update({
        "enabled": True,
        "from_side": "auto",
        "drill_diameter": 11.811 * 0.0254,
        "residual_stub": 9.8425 * 0.0254,
    })
    # 端口位于 L12/L14，物理内侧最近参考平面均为 L13。
    vt["return_vias"]["connect_layers"] = ["L13"]
    return cfg


def _bottom_reversed_cfg():
    """对称场景：L10 输入、L1 输出，机械钻孔 L1–L14，只有物理底部残桩。"""
    cfg = _phase3_cfg()
    vt = cfg["via_transition"]
    vt.update({"entry_layer": "L10", "exit_layer": "L1",
               "drill_start_layer": "L1", "drill_end_layer": "L14"})
    vt["backdrill"]["from_side"] = "auto"
    return cfg


def _l14_to_l3_cfg():
    """L14→L3：层名排序与叠层 Z/index 不一致的顶部残桩案例。"""
    cfg = _reversed_cfg()
    vt = cfg["via_transition"]
    vt.update({"entry_layer": "L14", "exit_layer": "L3"})
    # L3 距板顶不足 0.25 mm，必须使用更短残桩才能合法背钻。
    vt["backdrill"]["residual_stub"] = 0.05
    # 端口位于 L3/L14，朝功能跨度内侧的最近参考平面为 L4 与 L13。
    vt["return_vias"]["connect_layers"] = ["L4", "L13"]
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

    def test_08_reversed_layer_order_auto_derives_top(self) -> None:
        cfg = _reversed_cfg()
        geometry = GeometryCalculator(cfg).calculate()
        self.assertFalse(geometry.errors, geometry.errors)
        issues = validate_via_transition(cfg, geometry)
        self.assertFalse(any(item[0] == "VIA-TRN-046" for item in issues), issues)
        self.assertFalse([item for item in issues if item[1] == "error"], issues)
        for via in geometry.signal_vias:
            self.assertTrue(via.backdrilled)
            self.assertEqual(via.backdrill_from_side, "top")
            self.assertEqual(via.upper_functional_layer, "L12")
            self.assertEqual(via.lower_functional_layer, "L14")
            self.assertTrue(via.top_stub_present)
            self.assertFalse(via.bottom_stub_present)
            self.assertEqual(via.per_layer_states["L12"], "exit_connected")
            self.assertEqual(via.per_layer_states["L14"], "entry_connected")

    def test_09_swapping_entry_exit_preserves_backdrill_geometry(self) -> None:
        reversed_cfg = _reversed_cfg()
        forward_cfg = copy.deepcopy(reversed_cfg)
        forward_cfg["via_transition"].update(
            {"entry_layer": "L12", "exit_layer": "L14"})
        reversed_geometry = GeometryCalculator(reversed_cfg).calculate()
        forward_geometry = GeometryCalculator(forward_cfg).calculate()
        for reversed_via, forward_via in zip(
                reversed_geometry.signal_vias, forward_geometry.signal_vias):
            self.assertEqual(reversed_via.backdrill_from_side,
                             forward_via.backdrill_from_side)
            for field in ("backdrill_z_bottom", "backdrill_z_top",
                          "effective_barrel_z_bottom", "effective_barrel_z_top",
                          "actual_residual_stub"):
                self.assertAlmostEqual(getattr(reversed_via, field),
                                       getattr(forward_via, field), places=12)
        self.assertEqual({trace["layer"] for trace in reversed_geometry.entry_traces}, {"L14"})
        self.assertEqual({trace["layer"] for trace in forward_geometry.entry_traces}, {"L12"})
        self.assertEqual({port.signal_layer for port in reversed_geometry.ports if port.side == "IN"},
                         {"L14"})
        self.assertEqual({port.signal_layer for port in forward_geometry.ports if port.side == "IN"},
                         {"L12"})

    def test_10_explicit_top_matches_reversed_auto(self) -> None:
        auto_cfg = _reversed_cfg()
        top_cfg = copy.deepcopy(auto_cfg)
        top_cfg["via_transition"]["backdrill"]["from_side"] = "top"
        auto_vias = GeometryCalculator(auto_cfg).calculate().signal_vias
        top_vias = GeometryCalculator(top_cfg).calculate().signal_vias
        self.assertEqual([via.to_dict() for via in auto_vias],
                         [via.to_dict() for via in top_vias])

    def test_11_top_stub_uses_l12_top_copper_boundary(self) -> None:
        geometry = GeometryCalculator(_reversed_cfg()).calculate()
        l12 = self._layer(geometry, "L12")
        l14 = self._layer(geometry, "L14")
        for via in geometry.signal_vias:
            expected_endpoint = l12.z_top + via.target_residual_stub
            self.assertAlmostEqual(via.backdrill_z_bottom, expected_endpoint, places=12)
            self.assertAlmostEqual(via.effective_barrel_z_top, expected_endpoint, places=12)
            self.assertAlmostEqual(via.actual_residual_stub,
                                   via.target_residual_stub, places=12)
            self.assertNotAlmostEqual(via.backdrill_z_bottom,
                                      l14.z_top + via.target_residual_stub, places=6)
            self.assertLessEqual(via.effective_barrel_z_bottom, l14.z_bottom + 1e-12)
            self.assertGreaterEqual(via.effective_barrel_z_top, l12.z_top - 1e-12)

    def test_12_zero_stub_ends_at_allowed_functional_boundary(self) -> None:
        cfg = _reversed_cfg()
        cfg["via_transition"]["backdrill"]["residual_stub"] = 0.0
        geometry = GeometryCalculator(cfg).calculate()
        self.assertFalse(geometry.errors, geometry.errors)
        l12 = self._layer(geometry, "L12")
        for via in geometry.signal_vias:
            self.assertAlmostEqual(via.backdrill_z_bottom, l12.z_top, places=12)
            self.assertAlmostEqual(via.effective_barrel_z_top, l12.z_top, places=12)
            self.assertEqual(via.per_layer_states["L12"], "exit_connected")
            self.assertIn("L12", via.functional_pad_layers)

    def test_13_excessive_stub_rejected_without_fake_geometry(self) -> None:
        cfg = _reversed_cfg()
        baseline = GeometryCalculator(cfg).calculate()
        l1 = self._layer(baseline, "L1")
        l12 = self._layer(baseline, "L12")
        cfg["via_transition"]["backdrill"]["residual_stub"] = (
            l1.z_top - l12.z_top + 0.001)
        geometry = GeometryCalculator(cfg).calculate()
        self.assertTrue(any("过大" in error for error in geometry.errors), geometry.errors)
        self.assertTrue(all(not via.backdrilled for via in geometry.signal_vias))
        self.assertTrue(all(via.backdrill_z_bottom == 0.0 and via.backdrill_z_top == 0.0
                            for via in geometry.signal_vias))

    def test_14_two_sided_stub_auto_errors_explicit_side_is_single_sided(self) -> None:
        cfg = _reversed_cfg()
        cfg["via_transition"].update({"entry_layer": "L12", "exit_layer": "L3"})
        cfg["via_transition"]["backdrill"]["residual_stub"] = 0.05
        auto_geometry = GeometryCalculator(cfg).calculate()
        self.assertTrue(any("无法唯一" in error for error in auto_geometry.errors),
                        auto_geometry.errors)
        auto_issues = validate_via_transition(cfg, auto_geometry)
        self.assertTrue(any(item[0] == "VIA-TRN-046" for item in auto_issues))

        for side in ("top", "bottom"):
            with self.subTest(side=side):
                explicit = copy.deepcopy(cfg)
                explicit["via_transition"]["backdrill"]["from_side"] = side
                geometry = GeometryCalculator(explicit).calculate()
                self.assertFalse(geometry.errors, geometry.errors)
                for via in geometry.signal_vias:
                    self.assertEqual(via.backdrill_from_side, side)
                    if side == "top":
                        self.assertAlmostEqual(via.effective_barrel_z_bottom,
                                               via.z_bottom, places=12)
                    else:
                        self.assertAlmostEqual(via.effective_barrel_z_top,
                                               via.z_top, places=12)

    def test_15_no_stub_still_reports_unneeded_backdrill(self) -> None:
        cfg = _reversed_cfg()
        cfg["via_transition"].update({
            "entry_layer": "L14", "exit_layer": "L12",
            "drill_start_layer": "L12", "drill_end_layer": "L14",
        })
        geometry = GeometryCalculator(cfg).calculate()
        self.assertTrue(any("不存在残桩" in error for error in geometry.errors), geometry.errors)
        issues = validate_via_transition(cfg, geometry)
        self.assertTrue(any(item[0] == "VIA-TRN-049" for item in issues), issues)

    def test_16_reversed_preview_contains_two_authoritative_cavities(self) -> None:
        cfg = _reversed_cfg()
        geometry = GeometryCalculator(cfg).calculate()
        scene = build_via_transition_scene(cfg, geometry)
        cavities = [item for item in scene.objects if item.category == "背钻腔体"]
        self.assertEqual(len(cavities), 2)
        by_name = {item.name: item for item in cavities}
        for via in geometry.signal_vias:
            cavity = by_name[f"{via.name}_BACKDRILL_CAVITY"]
            self.assertAlmostEqual(cavity.z_bottom, via.backdrill_z_bottom, places=12)
            self.assertAlmostEqual(cavity.z_top, via.backdrill_z_top, places=12)

    def test_17_reversed_hfss_snapshot_fields_and_validate_only(self) -> None:
        cfg = _reversed_cfg()
        geometry = GeometryCalculator(cfg).calculate()
        snapshot = make_generation_snapshot(cfg, geometry)
        validate_generation_snapshot(snapshot)
        snapshot_vias = snapshot["geometry_snapshot"]["signal_vias"]
        for via in snapshot_vias:
            self.assertTrue(via["backdrilled"])
            self.assertEqual(via["backdrill_from_side"], "top")
            self.assertEqual(via["upper_functional_layer"], "L12")
            self.assertEqual(via["lower_functional_layer"], "L14")
            for field in ("backdrill_z_bottom", "backdrill_z_top",
                          "effective_barrel_z_bottom", "effective_barrel_z_top",
                          "target_residual_stub", "actual_residual_stub"):
                self.assertIn(field, via)
        with tempfile.TemporaryDirectory() as directory:
            result = HfssScriptGenerator(cfg, ROOT).generate(
                Path(directory), snapshot=snapshot)
            completed = subprocess.run(
                [sys.executable, str(result.script_path), "--validate-only"],
                capture_output=True, text=True, timeout=90,
            )
        self.assertEqual(completed.returncode, 0,
                         completed.stdout + completed.stderr)

    def test_18_bottom_stub_reversed_auto_derives_bottom(self) -> None:
        # 对称于 L14→L12 推导 top：entry 在内层、exit 在板顶时 auto 必须推导 bottom。
        cfg = _bottom_reversed_cfg()
        geometry = GeometryCalculator(cfg).calculate()
        self.assertFalse(geometry.errors, geometry.errors)
        issues = validate_via_transition(cfg, geometry)
        self.assertFalse(any(item[0] == "VIA-TRN-046" for item in issues), issues)
        self.assertFalse([item for item in issues if item[1] == "error"], issues)
        for via in geometry.signal_vias:
            self.assertTrue(via.backdrilled)
            self.assertEqual(via.backdrill_from_side, "bottom")
            self.assertEqual(via.upper_functional_layer, "L1")
            self.assertEqual(via.lower_functional_layer, "L10")
            self.assertFalse(via.top_stub_present)
            self.assertTrue(via.bottom_stub_present)
            self.assertEqual(via.per_layer_states["L1"], "exit_connected")
            self.assertEqual(via.per_layer_states["L10"], "entry_connected")

    def test_19_l14_to_l3_uses_stackup_index_not_layer_name_order(self) -> None:
        # 层名排序会把 L14 放在 L3 之前（"L14" < "L3"），但叠层 Z/index 中 L3 在上、L14 在下。
        self.assertLess("L14", "L3")
        cfg = _l14_to_l3_cfg()
        geometry = GeometryCalculator(cfg).calculate()
        self.assertFalse(geometry.errors, geometry.errors)
        issues = validate_via_transition(cfg, geometry)
        self.assertFalse(any(item[0] == "VIA-TRN-046" for item in issues), issues)
        self.assertFalse([item for item in issues if item[1] == "error"], issues)
        names = [layer.name for layer in geometry.stackup]
        self.assertLess(names.index("L3"), names.index("L14"))
        for via in geometry.signal_vias:
            self.assertTrue(via.backdrilled)
            self.assertEqual(via.backdrill_from_side, "top")
            self.assertEqual(via.upper_functional_layer, "L3")
            self.assertEqual(via.lower_functional_layer, "L14")
            self.assertTrue(via.top_stub_present)
            self.assertFalse(via.bottom_stub_present)
            self.assertNotEqual(via.upper_functional_layer, min("L14", "L3"))
            self.assertEqual(via.per_layer_states["L3"], "exit_connected")
            self.assertEqual(via.per_layer_states["L14"], "entry_connected")

        # 数字层名与 Z 相反：L14 在板顶、L3 在其下。按 int(name[1:]) 会误把 L3 当 upper。
        inverted_names = ["L14", "DIEL", "L3", "DIEL2", "AAA"]
        span = derive_functional_barrel_span(
            inverted_names, "L14", "L3", "L14", "AAA")
        self.assertIsNotNone(span)
        self.assertEqual(span.upper_functional_layer, "L14")
        self.assertEqual(span.lower_functional_layer, "L3")
        self.assertFalse(span.top_stub)
        self.assertTrue(span.bottom_stub)
        numeric_upper = min(("L14", "L3"), key=lambda name: int(name[1:]))
        self.assertNotEqual(span.upper_functional_layer, numeric_upper)

    def test_20_explicit_bottom_on_reversed_top_stub_fails_closed(self) -> None:
        # L14→L12 物理上只有顶部残桩：auto 与显式 top 合法，显式 bottom 必须 VIA-TRN-048。
        auto_cfg = _reversed_cfg()
        auto_geometry = GeometryCalculator(auto_cfg).calculate()
        self.assertFalse(auto_geometry.errors, auto_geometry.errors)
        auto_issues = validate_via_transition(auto_cfg, auto_geometry)
        self.assertFalse([item for item in auto_issues if item[1] == "error"], auto_issues)
        for via in auto_geometry.signal_vias:
            self.assertEqual(via.backdrill_from_side, "top")
            self.assertTrue(via.top_stub_present)
            self.assertFalse(via.bottom_stub_present)

        top_cfg = copy.deepcopy(auto_cfg)
        top_cfg["via_transition"]["backdrill"]["from_side"] = "top"
        top_geometry = GeometryCalculator(top_cfg).calculate()
        self.assertFalse(top_geometry.errors, top_geometry.errors)
        top_issues = validate_via_transition(top_cfg, top_geometry)
        self.assertFalse([item for item in top_issues if item[1] == "error"], top_issues)
        self.assertFalse(any(item[0] == "VIA-TRN-048" for item in top_issues), top_issues)

        bottom_cfg = copy.deepcopy(auto_cfg)
        bottom_cfg["via_transition"]["backdrill"]["from_side"] = "bottom"
        bottom_geometry = GeometryCalculator(bottom_cfg).calculate()
        self.assertTrue(
            any("不存在底部残桩" in error for error in bottom_geometry.errors),
            bottom_geometry.errors)
        self.assertTrue(all(not via.backdrilled for via in bottom_geometry.signal_vias))
        bottom_issues = validate_via_transition(bottom_cfg, bottom_geometry)
        self.assertTrue(any(item[0] == "VIA-TRN-048" for item in bottom_issues), bottom_issues)
        self.assertFalse(any(item[0] == "VIA-TRN-046" for item in bottom_issues), bottom_issues)


if __name__ == "__main__":
    unittest.main(verbosity=2)
