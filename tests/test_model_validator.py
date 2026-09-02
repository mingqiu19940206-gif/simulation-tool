from __future__ import annotations

import json
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

from config_manager import ConfigManager
from geometry_calculator import GeometryCalculator
from hfss_script_generator import HfssScriptGenerator, ScriptGenerationError
from main_gui import SurfaceCapAntipadApp
from model_validator import (
    ModelValidator, ValidationItem, ValidationReport, config_revision,
    export_report, load_report, preview_auto_fix,
)


ROOT = Path(__file__).resolve().parents[1]


class ModelValidatorLogicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = ConfigManager(ROOT)
        self.config = self.manager.default()

    def test_default_quick_validate_passes_with_stable_source_tagged_rules(self) -> None:
        report, normalized, geometry = ModelValidator(self.manager).run_local(self.config)
        self.assertIn(report.state, {"passed", "passed_with_warnings"})
        self.assertEqual(report.counts["errors"], 0)
        self.assertIsNotNone(normalized)
        self.assertIsNotNone(geometry)
        self.assertIn("HFSS-000", {item.rule_id for item in report.items})
        self.assertTrue(all(item.source == "local" for item in report.items))
        self.assertTrue(all(item.config_revision == report.config_revision for item in report.items))
        self.assertIn("PAR-002", {item.rule_id for item in report.items})
        self.assertTrue(any("临时仿真值，待材料数据表确认" in item.message
                            for item in report.items))

    def test_geometry_error_is_structured_and_blocks_script_generation(self) -> None:
        self.config["trace"]["trace_pitch"] = self.config["trace"]["trace_width"]
        report, _normalized, _geometry = ModelValidator(self.manager).run_local(self.config)
        self.assertEqual(report.state, "failed")
        self.assertTrue(any(item.level == "error" and item.rule_id.startswith("GEO-") for item in report.items))
        with self.assertRaises(ScriptGenerationError):
            HfssScriptGenerator(self.config, ROOT).generate()

    def test_report_json_and_txt_roundtrip(self) -> None:
        report, _normalized, _geometry = ModelValidator(self.manager).run_local(self.config)
        with tempfile.TemporaryDirectory() as directory:
            json_path = Path(directory) / "validate.json"
            txt_path = Path(directory) / "validate.txt"
            export_report(report, json_path); export_report(report, txt_path)
            restored = load_report(json_path)
            self.assertEqual(restored.config_revision, report.config_revision)
            self.assertEqual(restored.counts, report.counts)
            self.assertIn("不保证收敛", txt_path.read_text(encoding="utf-8"))

    def test_safe_auto_fix_previews_before_and_after_values(self) -> None:
        self.config["simulation"]["start_frequency_ghz"] = 1.0
        self.config["simulation"]["stop_frequency_ghz"] = 8.0
        self.config["simulation"]["adaptive_frequency_ghz"] = 16.0
        fixed, changes = preview_auto_fix(self.config, "FIX-ADAPTIVE")
        self.assertEqual(fixed["simulation"]["adaptive_frequency_ghz"], 8.0)
        self.assertEqual(changes[0][0], "simulation.adaptive_frequency_ghz")

    def test_hfss_report_merge_preserves_sources(self) -> None:
        local, _normalized, _geometry = ModelValidator(self.manager).run_local(self.config)
        item = ValidationItem(
            "HFSS-090", "info", "HFSS实际工程", "原生Validate通过。",
            source="hfss", timestamp=local.timestamp, config_revision=local.config_revision,
        )
        hfss = ValidationReport("HFSS工程检查", local.config_revision, local.timestamp, [item])
        merged = local.merge(hfss)
        self.assertEqual({entry.source for entry in merged.items}, {"local", "hfss"})

    def test_generated_script_contains_native_validate_and_structured_reports(self) -> None:
        self.config["project"]["overwrite_existing"] = True
        with tempfile.TemporaryDirectory() as directory:
            generated = HfssScriptGenerator(self.config, ROOT).generate(Path(directory))
            script = generated.script_path.read_text(encoding="utf-8")
            self.assertIn("sweep_along_vector", script)
            self.assertNotIn("app.modeler.thicken_sheet", script)
            self.assertIn("ValidateDesign", script)
            self.assertNotIn("app.validate_full_design(", script)
            self.assertIn("configure_hfss_design_settings(app)", script)
            self.assertIn("change_automatically_use_causal_materials", script)
            self.assertIn('"Calculate Lossy Dielectrics:=", True', script)
            self.assertIn(
                "create_hfss_design(app)\n        configure_hfss_design_settings(app)", script
            )
            self.assertLess(
                script.index("configure_hfss_design_settings(app)"),
                script.index("create_simulation_setup(app, state)"),
            )
            self.assertIn("write_hfss_validation_report", script)
            self.assertIn('"HFSS-010"', script)
            self.assertIn('"HFSS-011"', script)
            self.assertIn('groups = GEOMETRY.get("conductor_groups", {})', script)
            self.assertIn('if mode != "ideal_bridge":', script)
            self.assertTrue(generated.local_validate_json_path.is_file())
            payload = json.loads(generated.local_validate_json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["mode"], "本地快速预检")

    def test_port_validate_reports_missing_reference_with_specific_rule(self) -> None:
        self.config["simulation"]["enabled"] = True
        for layer in self.config["stackup"]:
            layer["is_reference_plane"] = False
            layer["enable_antipad"] = False
        report, _normalized, _geometry = ModelValidator(self.manager).run_local(self.config)
        self.assertEqual(report.state, "failed")
        self.assertIn("PORT-003", {item.rule_id for item in report.items if item.level == "error"})

    def test_hfss_script_reuses_explicit_port_sheets_single_reference_and_point_line(self) -> None:
        self.config["simulation"]["enabled"] = True
        normalized, validation = self.manager.validate(self.config, check_output_directory=False)
        self.assertTrue(validation.is_valid)
        geometry = GeometryCalculator(normalized).calculate()
        script = HfssScriptGenerator.build_script_text(normalized, geometry)
        self.assertIn('sheet_name = str(port_geometry["sheet_name"])', script)
        self.assertIn("create_port_sheet=False, create_pec_cap=True", script)
        self.assertIn("integration_line=integration_line", script)
        self.assertIn('reference = state["plane_objects"].get', script)
        self.assertIn('assignment=state["port_terminals"]["PORT_IN_P"]', script)
        self.assertIn('reference=state["port_terminals"]["PORT_OUT_N"]', script)
        self.assertNotIn('references = list(state["plane_objects"].values())', script)
        self.assertIn('"PORT-011"', script)
        self.assertIn('"PORT-012"', script)
        self.assertIn('"PORT-013"', script)


class ValidateGuiTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.app = SurfaceCapAntipadApp(); self.app.withdraw(); self.app.update_idletasks()
        except tk.TclError as exc:
            self.skipTest(f"Tk GUI unavailable: {exc}")

    def tearDown(self) -> None:
        if hasattr(self, "app"):
            self.app.destroy()

    def test_validate_results_page_replaces_legacy_log_and_runs_quick_check(self) -> None:
        self.assertIn("检查结果", self.app.tabs)
        self.assertNotIn("日志与检查", self.app.tabs)
        self.assertTrue(self.app.validate_model(show_dialog=False))
        report = self.app.validate_workbench.report
        self.assertIsNotNone(report)
        self.assertEqual(report.mode, "本地快速预检")
        self.assertEqual(self.app.notebook.select(), str(self.app.tabs["检查结果"]))

    def test_parameter_change_marks_previous_result_stale(self) -> None:
        self.assertTrue(self.app.validate_model(show_dialog=False))
        self.assertFalse(self.app.validate_workbench.report.stale)
        self.app.variables["trace.trace_width"].set("0.11")
        self.assertTrue(self.app.validate_workbench.report.stale)

    def test_validate_item_can_locate_3d_object(self) -> None:
        self.app.validate_model(show_dialog=False)
        item = ValidationItem(
            "GEO-TEST", "error", "信号几何", "测试定位",
            object_keys=(next(key for key in self.app.model_preview_workbench.objects if key.startswith("pad:")),),
            parameter_path="capacitor.pad_width", suggestion="测试建议", source="local",
        )
        self.app.locate_validate_item(item)
        self.app.update()
        self.assertEqual(self.app.notebook.select(), str(self.app.tabs["模型预览"]))
        self.assertTrue(self.app.model_preview_workbench.validation_marker_actors)
        hfss_item = ValidationItem(
            "HFSS-TEST", "error", "HFSS实际工程", "实际对象定位",
            object_keys=("SIGNAL_P",), source="hfss",
        )
        self.app.model_preview_workbench.locate_validation_item(hfss_item)
        self.assertTrue(self.app.model_preview_workbench.selected_key)
        self.assertIn(
            self.app.model_preview_workbench.objects[self.app.model_preview_workbench.selected_key].details.get("信号"),
            {"P", None},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
