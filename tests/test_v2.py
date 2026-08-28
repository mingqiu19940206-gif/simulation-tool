from __future__ import annotations

import copy
import json
import os
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config_manager import (ConfigManager, convert_length, load_config, make_generation_snapshot,
                            normalize_aedt_version, normalize_config, save_config,
                            snapshot_from_config, to_external_config, validate_config)
from geometry_calculator import GeometryCalculator, calculate_gnd_via_positions
from hfss_script_generator import HfssScriptGenerator
from main_gui_legacy import runtime_base_directory


class V2AcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = ConfigManager(ROOT)
        self.default = self.manager.default()

    def test_01_v1_json_migrates_to_v3(self) -> None:
        v1 = json.loads((ROOT / "examples" / "v1_legacy.json").read_text(encoding="utf-8"))
        normalized, corrections = normalize_config(v1)
        self.assertEqual(normalized["schema_version"], 3)
        self.assertEqual(normalized["units"], "mm")
        self.assertFalse(normalized["gnd_vias"]["enabled"])
        self.assertEqual(normalized["gnd_vias"]["start_layer"], "TOP")
        self.assertEqual(normalized["gnd_vias"]["end_layer"], "GND2")
        self.assertEqual(normalized["gnd_vias"]["connect_layers"], ["GND1", "GND2"])
        self.assertEqual(normalized["simulation"]["port_reference_layer"], "GND1")
        self.assertEqual(normalized["capacitor_model"]["mode"], "ideal_bridge")
        self.assertTrue(any("schema_version=1" in item for item in corrections))

    def test_02_default_v2_keeps_v1_physical_geometry(self) -> None:
        geometry = GeometryCalculator(self.default).calculate()
        self.assertAlmostEqual(geometry.capacitor_centers["C_P"].y, 0.3)
        self.assertAlmostEqual(geometry.capacitor_centers["C_N"].y, -0.3)
        self.assertAlmostEqual(geometry.pad_centers["P_IN"].x, -0.4)
        self.assertAlmostEqual(geometry.pad_centers["P_OUT"].x, 0.4)
        self.assertEqual(len(geometry.gnd_vias), 0)
        self.assertEqual(self.default["capacitor_model"]["mode"], "ideal_bridge")
        self.assertFalse(geometry.errors)

    def test_03_point_one_mm_is_expected_mil(self) -> None:
        self.assertAlmostEqual(convert_length(0.1, "mm", "mil"), 3.937007874015748, places=12)

    def test_04_mm_mil_mm_round_trip_no_physical_drift(self) -> None:
        original = copy.deepcopy(self.default)
        mil_file = to_external_config(original, "mil")
        internal_mil, _ = normalize_config(mil_file)
        mm_file = to_external_config(internal_mil, "mm")
        round_trip, _ = normalize_config(mm_file)
        for section, key in (("trace", "trace_width"), ("capacitor", "cap_pitch"),
                             ("capacitor", "pad_pitch"), ("antipad", "global_expansion"),
                             ("gnd_vias", "drill_diameter"), ("simulation", "airbox_margin")):
            self.assertTrue(math.isclose(original[section][key], round_trip[section][key], abs_tol=1e-12))

    def test_05_mm_and_mil_hfss_boundaries_are_physically_equal(self) -> None:
        mm = copy.deepcopy(self.default)
        mil, _ = normalize_config(to_external_config(mm, "mil"))
        g_mm = GeometryCalculator(mm).calculate().to_dict()
        g_mil = GeometryCalculator(mil).calculate().to_dict()
        self.assertEqual(g_mm["plane_bounds"], g_mil["plane_bounds"])
        script = HfssScriptGenerator.build_script_text(mil, GeometryCalculator(mil).calculate())
        self.assertIn('UNITS = str(CONFIG.get("units", "mm"))', script)
        self.assertIn('float(value) / 0.0254', script)

    def test_06_cap_pitch_centers_are_plus_minus_half(self) -> None:
        for pitch in (0.5, 0.6, 0.9, 1.2):
            cfg = copy.deepcopy(self.default)
            cfg["capacitor"]["cap_pitch"] = pitch
            geometry = GeometryCalculator(cfg).calculate()
            self.assertAlmostEqual(geometry.capacitor_centers["C_P"].y, pitch / 2)
            self.assertAlmostEqual(geometry.capacitor_centers["C_N"].y, -pitch / 2)

    def test_07_pad_pitch_and_cap_pitch_are_not_mixed(self) -> None:
        cfg = copy.deepcopy(self.default)
        cfg["capacitor"]["pad_pitch_mode"] = "advanced_pitch"
        cfg["capacitor"]["pad_pitch"] = 1.0
        cfg["capacitor"]["cap_pitch"] = 0.7
        geometry = GeometryCalculator(cfg).calculate()
        self.assertAlmostEqual(abs(geometry.pad_centers["P_OUT"].x - geometry.pad_centers["P_IN"].x), 1.0)
        self.assertAlmostEqual(geometry.capacitor_centers["C_P"].y - geometry.capacitor_centers["C_N"].y, 0.7)

    def _via_config(self, pattern: str, count: int) -> dict:
        cfg = copy.deepcopy(self.default)
        cfg["gnd_vias"]["enabled"] = True
        cfg["gnd_vias"]["pattern"] = pattern
        cfg["gnd_vias"]["count"] = count
        return cfg

    def test_08_via_layout_counts(self) -> None:
        for pattern, count in (("four_corner", 4), ("side_pair", 4), ("rectangular_fence", 8)):
            geometry = GeometryCalculator(self._via_config(pattern, count)).calculate()
            self.assertEqual(len(geometry.gnd_vias), count, pattern)
            self.assertFalse(geometry.errors, pattern)

    def test_09_automatic_vias_are_pn_symmetric(self) -> None:
        for pattern, count in (("four_corner", 4), ("side_pair", 6), ("rectangular_fence", 12)):
            geometry = GeometryCalculator(self._via_config(pattern, count)).calculate()
            points = {(round(via.x, 9), round(via.y, 9)) for via in geometry.gnd_vias}
            for x, y in points:
                self.assertIn((x, -y), points)

    def test_10_via_intrusion_fails_config_validation(self) -> None:
        cfg = self._via_config("custom", 2)
        cfg["gnd_vias"]["custom_coordinates"] = [[0.0, 0.3], [0.0, -0.3]]
        _, result = validate_config(cfg, ROOT, check_output_directory=False)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("侵入" in error for error in result.errors))

    def test_11_disabled_vias_are_v1_compatible(self) -> None:
        cfg = copy.deepcopy(self.default)
        cfg["gnd_vias"]["enabled"] = False
        geometry = GeometryCalculator(cfg).calculate()
        self.assertEqual(geometry.gnd_vias, [])
        self.assertEqual(len(geometry.signal_shapes["P"]), 5)
        self.assertEqual(len(geometry.signal_shapes["N"]), 5)
        self.assertEqual(set(geometry.conductor_groups), {"SIGNAL_P", "SIGNAL_N"})

    def test_12_independent_script_validate_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cfg = copy.deepcopy(self.default)
            cfg["project"]["overwrite_existing"] = True
            result = HfssScriptGenerator(cfg, ROOT).generate(Path(directory))
            completed = subprocess.run([sys.executable, str(result.script_path), "--validate-only"],
                                       capture_output=True, text=True, timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_13_package_presets_are_available(self) -> None:
        from config_manager import apply_package_preset
        p0201 = apply_package_preset(self.default, "0201")
        p0402 = apply_package_preset(self.default, "0402")
        self.assertLess(p0201["capacitor"]["body_length"], p0402["capacitor"]["body_length"])
        self.assertEqual(p0402["capacitor"]["pad_pitch"], 0.8)

    def test_14_sparameter_failure_is_explicit(self) -> None:
        cfg = copy.deepcopy(self.default)
        cfg["capacitor_model"]["mode"] = "sparameter"
        cfg["capacitor_model"]["touchstone_file"] = "missing_vendor_model.s2p"
        _, result = validate_config(cfg, ROOT, check_output_directory=False)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("不存在" in error for error in result.errors))


    def test_15_aedt_2022_r2_and_2023_r2_aliases_are_canonical(self) -> None:
        aliases = {
            "2022 R2": "2022.2", "2022R2": "2022.2", "222": "2022.2",
            "2023 R2": "2023.2", "2023R2": "2023.2", "232": "2023.2",
        }
        for supplied, expected in aliases.items():
            self.assertEqual(normalize_aedt_version(supplied), expected)

    def test_16_both_supported_aedt_versions_generate_explicit_launch_version(self) -> None:
        for version in ("2022.2", "2023.2"):
            cfg = copy.deepcopy(self.default)
            cfg["project"]["aedt_version"] = version
            normalized, result = validate_config(cfg, ROOT, check_output_directory=False)
            self.assertTrue(result.is_valid, result.errors)
            self.assertEqual(normalized["project"]["aedt_version"], version)
            script = HfssScriptGenerator.build_script_text(
                normalized, GeometryCalculator(normalized).calculate()
            )
            self.assertIn('"version": AEDT_VERSION', script)
            self.assertIn('"specified_version": AEDT_VERSION', script)
            self.assertIn('SUPPORTED_AEDT_VERSIONS = {"2022.2", "2023.2"}', script)

    def test_17_unsupported_aedt_version_is_rejected(self) -> None:
        cfg = copy.deepcopy(self.default)
        cfg["project"]["aedt_version"] = "2024.1"
        _, result = validate_config(cfg, ROOT, check_output_directory=False)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("2022 R2" in error and "2023 R2" in error for error in result.errors))

    def test_18_generated_mil_snapshot_json_preserves_hashes(self) -> None:
        internal_mil, _ = normalize_config(to_external_config(self.default, "mil"))
        internal_mil["project"]["overwrite_existing"] = True
        snapshot = make_generation_snapshot(
            internal_mil,
            GeometryCalculator(internal_mil).calculate(),
        )
        with tempfile.TemporaryDirectory() as directory:
            result = HfssScriptGenerator(internal_mil, ROOT).generate(
                Path(directory), snapshot=snapshot
            )
            persisted = json.loads(result.config_path.read_text(encoding="utf-8"))
            restored = snapshot_from_config(persisted)
        self.assertIsNotNone(restored)
        self.assertEqual(restored["generation_id"], snapshot["generation_id"])
        self.assertEqual(restored["config_hash"], snapshot["config_hash"])
        self.assertEqual(restored["geometry_hash"], snapshot["geometry_hash"])
        self.assertEqual(restored["config_snapshot"], snapshot["config_snapshot"])

    @unittest.skipUnless(os.name == "nt", "frozen EXE paths are Windows-only; "
                         "on POSIX these strings are relative paths, so "
                         "Path.resolve() rebases them on the current directory")
    def test_19_frozen_relative_output_is_based_on_exe_not_meipass(self) -> None:
        executable = Path(r"C:\Tools\SurfaceCapAntipadTool.exe")
        module_file = Path(r"C:\Temp\_MEI123456\main_gui_legacy.py")
        self.assertEqual(
            runtime_base_directory(
                frozen=True, executable=executable, module_file=module_file
            ),
            executable.parent,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
