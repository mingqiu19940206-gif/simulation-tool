"""Phase 4 端口与差分对：四端口几何、差分对定义与 §8.4 校验（纯 Python）。"""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from config_manager import ConfigManager, normalize_config
from geometry_calculator import GeometryCalculator
from via_transition_semantics import validate_via_transition


def _via_cfg():
    manager = ConfigManager(ROOT)
    cfg = copy.deepcopy(manager.default())
    cfg["scenario"]["type"] = "via_transition"
    cfg["via_transition"]["exit_layer"] = "L14"
    cfg["via_transition"]["backdrill"]["enabled"] = False
    cfg["via_transition"]["return_vias"]["connect_layers"] = ["L2", "L13"]
    return cfg


class ViaTransitionPortsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = ConfigManager(ROOT)
        self.cfg = _via_cfg()

    def test_01_four_ports_with_signal_and_reference(self) -> None:
        geometry = GeometryCalculator(self.cfg).calculate()
        names = {port.name for port in geometry.ports}
        self.assertEqual(names, {"PORT_IN_P", "PORT_IN_N", "PORT_OUT_P", "PORT_OUT_N"})
        for port in geometry.ports:
            self.assertTrue(port.signal_layer)
            self.assertTrue(port.reference_layer, port.name)
            self.assertTrue(port.reference_conductor, port.name)

    def test_02_two_differential_pairs(self) -> None:
        geometry = GeometryCalculator(self.cfg).calculate()
        self.assertEqual(len(geometry.differential_pairs), 2)
        names = {pair["name"] for pair in geometry.differential_pairs}
        self.assertEqual(names, {"IN", "OUT"})
        for pair in geometry.differential_pairs:
            self.assertTrue(pair["p_port"].startswith("PORT_"))
            self.assertTrue(pair["n_port"].startswith("PORT_"))
            self.assertEqual(pair["reference_impedance"], 100.0)

    def test_03_port_validation_passes_default(self) -> None:
        geometry = GeometryCalculator(self.cfg).calculate()
        issues = validate_via_transition(self.cfg, geometry)
        errors = [i for i in issues if i[1] == "error"]
        self.assertFalse(errors, errors)

    def test_04_integration_line_points_from_signal_to_reference(self) -> None:
        geometry = GeometryCalculator(self.cfg).calculate()
        for port in geometry.ports:
            start_z = float(port.integration_line_start[2])
            end_z = float(port.integration_line_end[2])
            self.assertNotEqual(start_z, end_z, port.name)

    def test_05_each_side_uses_its_own_derived_trace_pitch(self) -> None:
        vt = self.cfg["via_transition"]
        vt["entry_trace"].update({"width": 0.1, "gap": 0.5})
        vt["exit_trace"].update({"width": 0.2, "gap": 0.8})
        geometry = GeometryCalculator(self.cfg).calculate()
        centers = {
            port.name: port.integration_line_start[1]
            for port in geometry.ports
        }
        self.assertAlmostEqual(centers["PORT_IN_P"], 0.3)
        self.assertAlmostEqual(centers["PORT_IN_N"], -0.3)
        self.assertAlmostEqual(centers["PORT_OUT_P"], 0.5)
        self.assertAlmostEqual(centers["PORT_OUT_N"], -0.5)

    def test_06_forward_l10_to_l14_uses_inward_reference_planes(self) -> None:
        cfg = copy.deepcopy(self.cfg)
        cfg["via_transition"].update({"entry_layer": "L10", "exit_layer": "L14"})
        cfg["via_transition"]["backdrill"].update({"enabled": True, "from_side": "auto"})
        cfg = normalize_config(cfg)[0]
        geometry = GeometryCalculator(cfg).calculate()
        self.assertFalse(geometry.errors, geometry.errors)
        ports = {port.name: port for port in geometry.ports}
        self.assertEqual(ports["PORT_IN_P"].reference_layer, "L11")
        self.assertEqual(ports["PORT_IN_N"].reference_layer, "L11")
        self.assertEqual(ports["PORT_OUT_P"].reference_layer, "L13")
        self.assertEqual(ports["PORT_OUT_N"].reference_layer, "L13")
        self.assertGreater(ports["PORT_IN_P"].integration_line_start[2],
                           ports["PORT_IN_P"].integration_line_end[2])
        self.assertLess(ports["PORT_OUT_P"].integration_line_start[2],
                        ports["PORT_OUT_P"].integration_line_end[2])

    def test_07_reverse_l14_to_l10_swaps_signal_flow_not_physical_refs(self) -> None:
        cfg = copy.deepcopy(self.cfg)
        cfg["via_transition"].update({"entry_layer": "L14", "exit_layer": "L10"})
        cfg["via_transition"]["backdrill"].update({"enabled": True, "from_side": "auto"})
        cfg = normalize_config(cfg)[0]
        geometry = GeometryCalculator(cfg).calculate()
        self.assertFalse(geometry.errors, geometry.errors)
        ports = {port.name: port for port in geometry.ports}
        for name in ("PORT_IN_P", "PORT_IN_N"):
            self.assertEqual(ports[name].signal_layer, "L14")
            self.assertEqual(ports[name].reference_layer, "L13")
            self.assertNotEqual(ports[name].integration_line_start[2],
                                ports[name].integration_line_end[2])
        for name in ("PORT_OUT_P", "PORT_OUT_N"):
            self.assertEqual(ports[name].signal_layer, "L10")
            self.assertEqual(ports[name].reference_layer, "L11")
            self.assertNotEqual(ports[name].integration_line_start[2],
                                ports[name].integration_line_end[2])
        self.assertEqual(cfg["via_transition"]["return_vias"]["connect_layers"],
                         ["L13", "L11"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
