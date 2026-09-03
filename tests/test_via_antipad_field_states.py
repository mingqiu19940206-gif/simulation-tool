"""Signal VIA antipad field visibility/help (GUI contract) and geometry lock.

Acceptance D/E are geometry-only and must not depend on Tk.  Visibility maps
are the same helper the global box and VIA layer aliases consume.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from antipad_editor_core import (
    VIA_PARAMETER_HELP,
    VIA_SIZE_CLUSTER_KEYS,
    via_antipad_field_states,
    via_field_hint_sentences,
    via_parameter_help_text,
)
from config_manager import ConfigManager
from via_antipad import build_layer_contours


INDEPENDENT_CIRCLE_POINTS_SHA256 = (
    "67fe85e77f2ab0a656a9bfee0a19fdea01325aeab322f8d7d10e12be113be79b"
)
_PAIR = ((0.0, -0.4), (0.0, 0.4))


def _contour_geometry_payload(contours) -> list[dict]:
    return [
        {
            "owner": contour.owner,
            "topology": contour.topology,
            "shape": contour.shape,
            "center": [contour.center_x, contour.center_y],
            "diameter": contour.diameter,
            "length": contour.length,
            "width": contour.width,
            "rotation_deg": contour.rotation_deg,
            "clearance": contour.clearance,
            "points": [list(point) for point in contour.points],
            "bounds": list(contour.bounds),
            "area": contour.area,
        }
        for contour in contours
    ]


def _visible_keys(states: dict) -> set[str]:
    return {key for key, shown in states["visible"].items() if shown}


class ViaAntipadFieldStateTests(unittest.TestCase):
    def test_a_independent_circle_hides_merged_only_fields(self) -> None:
        states = via_antipad_field_states(
            "independent", "circle", "auto_pn", "auto_fit")
        self.assertTrue(states["manual_size"])
        self.assertFalse(states["auto_size"])
        self.assertEqual(
            _visible_keys(states),
            {"topology", "shape", "diameter"},
        )
        self.assertNotIn("clearance", VIA_SIZE_CLUSTER_KEYS)
        self.assertFalse(states["visible"]["clearance"])
        self.assertTrue(states["enabled"]["clearance"])
        hint = " ".join(via_field_hint_sentences(
            "independent", "circle", "auto_pn", "auto_fit"))
        self.assertEqual(hint, "")
        self.assertNotIn("方向/自动尺寸", hint)
        self.assertNotIn("尺寸增量", hint)

    def test_b_independent_ellipse_shows_length_width_still_hides_direction(self) -> None:
        states = via_antipad_field_states(
            "independent", "ellipse", "auto_pn", "auto_fit")
        visible = _visible_keys(states)
        self.assertIn("length", visible)
        self.assertIn("width", visible)
        self.assertIn("rotation_deg", visible)
        self.assertNotIn("direction_mode", visible)
        self.assertNotIn("size_mode", visible)
        self.assertNotIn("end_clearance", visible)
        self.assertNotIn("clearance", visible)
        self.assertTrue(states["manual_size"])

    def test_c_merged_auto_fit_shows_direction_size_and_end_side(self) -> None:
        states = via_antipad_field_states(
            "merged_pair", "circle", "auto_pn", "auto_fit")
        visible = _visible_keys(states)
        self.assertIn("direction_mode", visible)
        self.assertIn("size_mode", visible)
        self.assertIn("end_clearance", visible)
        self.assertIn("side_clearance", visible)
        self.assertNotIn("clearance", visible)
        self.assertNotIn("diameter", visible)
        self.assertNotIn("length", visible)
        self.assertTrue(states["enabled"]["direction_mode"])
        self.assertTrue(states["enabled"]["size_mode"])
        hint = " ".join(via_field_hint_sentences(
            "merged_pair", "circle", "auto_pn", "auto_fit"))
        self.assertIn("孔距", hint)
        self.assertNotIn("用户增量", hint)
        self.assertNotIn("方向/自动尺寸仅作用于", hint)

        increment = via_antipad_field_states(
            "merged_pair", "capsule", "auto_pn", "auto_plus_increment")
        self.assertTrue(increment["visible"]["length_increment"])
        increment_hint = " ".join(via_field_hint_sentences(
            "merged_pair", "capsule", "auto_pn", "auto_plus_increment"))
        self.assertIn("用户增量", increment_hint)

    def test_circle_help_does_not_mention_other_shapes(self) -> None:
        text = via_parameter_help_text(
            "shape", topology="independent", shape="circle",
            size_mode="auto_fit")
        self.assertIn("圆形", text)
        self.assertIn("直径", text)
        self.assertNotIn("其余形状", text)
        self.assertNotIn("局部 X", text)
        diameter = via_parameter_help_text(
            "diameter", topology="independent", shape="circle")
        self.assertIn("各一个圆", diameter)
        clearance = via_parameter_help_text(
            "clearance", topology="independent", shape="circle",
            size_mode="auto_fit")
        self.assertIn("制造预检", clearance)
        self.assertIn("不增大开窗", clearance)
        self.assertNotIn("端部", clearance)
        self.assertNotIn("完全手动", clearance)
        merged = via_parameter_help_text(
            "clearance", topology="merged_pair", shape="capsule",
            size_mode="auto_fit")
        self.assertIn("端部/侧向净距增大开窗", merged)
        self.assertNotIn("完全手动", merged)
        merged_manual = via_parameter_help_text(
            "clearance", topology="merged_pair", shape="circle",
            size_mode="manual")
        self.assertIn("完全手动", merged_manual)
        self.assertNotIn("独立开窗", merged_manual)

    def test_help_copy_omits_modes_that_are_not_selected(self) -> None:
        independent_clearance = via_parameter_help_text(
            "clearance", topology="independent", shape="circle",
            size_mode="auto_fit")
        self.assertNotIn("完全手动", independent_clearance)
        self.assertIn("独立开窗", independent_clearance)
        autofit_hint = " ".join(via_field_hint_sentences(
            "merged_pair", "circle", "auto_pn", "auto_fit"))
        self.assertIn("孔距", autofit_hint)
        self.assertNotIn("用户增量", autofit_hint)
        increment_hint = " ".join(via_field_hint_sentences(
            "merged_pair", "circle", "auto_pn", "auto_plus_increment"))
        self.assertIn("用户增量", increment_hint)
        circle_help = via_parameter_help_text(
            "shape", topology="independent", shape="circle")
        self.assertIn("圆形", circle_help)
        self.assertNotEqual(VIA_PARAMETER_HELP["shape"], circle_help)
        self.assertNotIn("各生成一个圆形", VIA_PARAMETER_HELP["shape"])

    def test_d_independent_circle_contours_are_bit_identical(self) -> None:
        cfg = copy.deepcopy(ConfigManager(ROOT).default())
        anti = cfg["via_transition"]["padstack"]["antipad"]
        self.assertEqual(anti["topology"], "independent")
        self.assertEqual(anti["shape"], "circle")
        self.assertEqual(anti["size_mode"], "auto_fit")
        contours = build_layer_contours(anti, "L2", _PAIR[0], _PAIR[1], 0.2)
        payload = _contour_geometry_payload(contours)
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        self.assertEqual(digest, INDEPENDENT_CIRCLE_POINTS_SHA256)
        self.assertEqual([item["owner"] for item in payload], ["P", "N"])
        self.assertAlmostEqual(payload[0]["diameter"], 0.6)
        self.assertAlmostEqual(payload[1]["diameter"], 0.6)

    def test_e_old_json_independent_auto_fit_still_uses_manual_diameter(self) -> None:
        cfg = copy.deepcopy(ConfigManager(ROOT).default())
        anti = dict(cfg["via_transition"]["padstack"]["antipad"])
        self.assertEqual(anti["topology"], "independent")
        self.assertEqual(anti["size_mode"], "auto_fit")
        auto = build_layer_contours(anti, "L2", _PAIR[0], _PAIR[1], 0.2)
        manual_spec = dict(anti)
        manual_spec["size_mode"] = "manual"
        manual = build_layer_contours(manual_spec, "L2", _PAIR[0], _PAIR[1], 0.2)
        self.assertEqual(
            _contour_geometry_payload(auto),
            _contour_geometry_payload(manual),
        )
        self.assertAlmostEqual(auto[0].diameter, float(anti["diameter"]))
        self.assertNotAlmostEqual(auto[0].diameter, 0.2 + 2.0 * float(anti["clearance"]) + 0.8)

    def test_default_config_keeps_size_mode_auto_fit_and_clearance_key(self) -> None:
        anti = ConfigManager(ROOT).default()["via_transition"]["padstack"]["antipad"]
        self.assertEqual(anti["size_mode"], "auto_fit")
        self.assertIn("clearance", anti)
        self.assertAlmostEqual(float(anti["clearance"]), 0.1)
        self.assertNotIn("min_copper_clearance", anti)

    def test_merged_manual_and_autofit_keep_clearance_out_of_size_cluster(self) -> None:
        manual = via_antipad_field_states(
            "merged_pair", "circle", "auto_pn", "manual")
        self.assertTrue(manual["manual_size"])
        self.assertFalse(manual["visible"]["clearance"])
        self.assertTrue(manual["enabled"]["clearance"])
        auto = via_antipad_field_states(
            "merged_pair", "circle", "auto_pn", "auto_fit")
        self.assertTrue(auto["visible"]["end_clearance"])
        self.assertTrue(auto["visible"]["side_clearance"])
        self.assertFalse(auto["visible"]["clearance"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
