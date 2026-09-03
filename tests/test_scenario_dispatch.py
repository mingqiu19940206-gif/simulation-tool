"""Scenario dispatch: surface capacitor unchanged; VIA native axes are gated."""
from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config_manager import ConfigManager, validate_config
from geometry_calculator import GeometryCalculator
from scenario_semantics import (
    ScenarioNotImplementedError,
    require_sweep_supported_scenario,
)


class ScenarioDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = ConfigManager(ROOT)
        self.default = self.manager.default()

    def _via_cfg(self):
        cfg = copy.deepcopy(self.default)
        cfg["scenario"]["type"] = "via_transition"
        return cfg

    def test_01_surface_capacitor_validates_and_calculates(self) -> None:
        cfg = copy.deepcopy(self.default)
        _, result = validate_config(cfg, ROOT, check_output_directory=False)
        self.assertTrue(result.is_valid, result.errors)
        geometry = GeometryCalculator(cfg).calculate()
        self.assertFalse(geometry.errors)

    def test_02_surface_capacitor_preview_does_not_crash(self) -> None:
        from model_preview import build_model_scene
        cfg = copy.deepcopy(self.default)
        geometry = GeometryCalculator(cfg).calculate()
        scene = build_model_scene(cfg, geometry, None)
        self.assertGreater(len(scene.objects), 0)

    def test_03_via_transition_produces_via_geometry_not_capacitor(self) -> None:
        geometry = GeometryCalculator(self._via_cfg()).calculate()
        self.assertEqual(len(geometry.signal_vias), 2)
        self.assertEqual(len(geometry.return_vias), 2)
        self.assertEqual(len(geometry.ports), 4)
        self.assertFalse(hasattr(geometry, "capacitor_centers"))

    def test_04_via_transition_unbound_native_axis_fails_closed(self) -> None:
        # 首批孔径轴已完成原生绑定；pair_pitch 坐标表达式仍必须 fail closed。
        from sweep_manager import generate_sweep_cases
        cfg = self._via_cfg()
        cfg["sweeps"]["via_transition.pair_pitch"] = {
            "enabled": True, "start": 0.7, "stop": 0.9, "step": 0.1,
        }
        with self.assertRaises(ScenarioNotImplementedError) as ctx:
            generate_sweep_cases(cfg, ROOT / ".tmp" / "via_sweep")
        self.assertIn("via_transition.pair_pitch", str(ctx.exception))

    def test_04b_unbound_stub_and_return_via_axes_fail_closed(self) -> None:
        # pair_pitch is already covered above; residual_stub and Return VIA
        # coordinates are the other unbound safe axes that must not emit a
        # native Optimetrics setup whose HFSS geometry would stay frozen.
        for path in (
            "via_transition.backdrill.residual_stub",
            "via_transition.return_vias.offset_x",
            "via_transition.return_vias.offset_y",
            "via_transition.return_vias.center_to_signal",
            "via_transition.return_vias.pitch_x",
            "via_transition.return_vias.pitch_y",
        ):
            with self.subTest(path=path):
                cfg = self._via_cfg()
                cfg["sweeps"][path] = {
                    "enabled": True, "start": 0.2, "stop": 0.4, "step": 0.2,
                }
                with self.assertRaises(ScenarioNotImplementedError) as ctx:
                    require_sweep_supported_scenario(cfg)
                self.assertIn(path, str(ctx.exception))

    def test_05_surface_capacitor_still_generates_hfss_script(self) -> None:
        from hfss_script_generator import HfssScriptGenerator
        cfg = copy.deepcopy(self.default)
        cfg["project"]["overwrite_existing"] = True
        with tempfile.TemporaryDirectory() as directory:
            result = HfssScriptGenerator(cfg, ROOT).generate(Path(directory))
            self.assertTrue(result.script_path.is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
