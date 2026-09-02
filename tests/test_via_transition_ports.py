"""Phase 4 端口与差分对：四端口几何、差分对定义与 §8.4 校验（纯 Python）。"""
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

    def test_05_in_and_out_use_local_nearest_reference_not_global_field(self) -> None:
        """Each VIA end has its own nearest plane; capacitor's single layer must not win.

        L1 entry → L2, L14 exit → L13.  A leftover ``port_reference_layer=L4``
        is the surface-capacitor identity and must not be copied onto both ends.
        """
        self.cfg["simulation"]["port_reference_mode"] = "manual"
        self.cfg["simulation"]["port_reference_layer"] = "L4"
        geometry = GeometryCalculator(self.cfg).calculate()
        by_name = {port.name: port for port in geometry.ports}
        for name in ("PORT_IN_P", "PORT_IN_N"):
            self.assertEqual(by_name[name].signal_layer, "L1", name)
            self.assertEqual(by_name[name].reference_layer, "L2", name)
            self.assertEqual(by_name[name].reference_conductor, "PLANE_L2", name)
        for name in ("PORT_OUT_P", "PORT_OUT_N"):
            self.assertEqual(by_name[name].signal_layer, "L14", name)
            self.assertEqual(by_name[name].reference_layer, "L13", name)
            self.assertEqual(by_name[name].reference_conductor, "PLANE_L13", name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
