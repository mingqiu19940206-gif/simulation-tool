"""载入完整反焊盘测试集，并使用精确的 mil 输入常量。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from antipad_editor import layer_from_form


_source = Path(__file__).with_name("_antipad_editor_tests.py")
_spec = importlib.util.spec_from_file_location("antipad_editor_test_suite", _source)
assert _spec is not None and _spec.loader is not None
_suite = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_suite)


def _test_structured_layer_form_converts_units_and_enforces_interlocks(self) -> None:
    values = {
        "name": "GND_TEST", "type": "plane", "thickness": "1.377952755906",
        "material": "copper", "dk": "", "df": "", "conductivity": "58000000",
        "is_reference_plane": True, "enable_antipad": True, "use_global": False,
        "override": "7.874015748031",
    }
    layer = layer_from_form(values, "mil")
    self.assertAlmostEqual(layer["thickness"], 0.035, places=10)
    self.assertAlmostEqual(layer["antipad_expansion_override"], 0.2, places=10)
    self.assertTrue(layer["is_reference_plane"])
    self.assertTrue(layer["enable_antipad"])
    values["type"] = "dielectric"
    values.update({"dk": "3.8", "df": "0.015"})
    layer = layer_from_form(values, "mil")
    self.assertFalse(layer["is_reference_plane"])
    self.assertFalse(layer["enable_antipad"])
    self.assertIsNone(layer["conductivity"])


_suite.AntipadEditorLogicTests.test_structured_layer_form_converts_units_and_enforces_interlocks = (
    _test_structured_layer_form_converts_units_and_enforces_interlocks
)

AntipadEditorLogicTests = _suite.AntipadEditorLogicTests
AntipadEditorGuiTests = _suite.AntipadEditorGuiTests

