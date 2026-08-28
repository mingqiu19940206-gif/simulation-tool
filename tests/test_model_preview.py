from __future__ import annotations

import copy
import math
import sys
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config_manager import ConfigManager
from geometry_calculator import GeometryCalculator, PortGeometry
from model_preview import (COLORS, NATIVE_VTK_EMBED_ENABLED, VTK_AVAILABLE,
                           ModelPreviewWorkbench, build_model_scene, mesh_for_object)


class ModelPreviewGeometryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = ConfigManager(ROOT)
        self.config = self.manager.default()
        self.config["gnd_vias"]["enabled"] = True
        self.config["gnd_vias"]["create_top_ground_copper"] = True
        self.config["simulation"]["enabled"] = True
        self.config, self.validation = self.manager.validate(
            self.config, check_output_directory=False
        )
        self.geometry = GeometryCalculator(self.config).calculate()
        self.scene = build_model_scene(self.config, self.geometry, self.validation)

    def test_hfss_auxiliary_geometry_is_authoritative(self) -> None:
        self.assertEqual(len(self.geometry.capacitor_bodies), 2)
        self.assertEqual(len(self.geometry.ports), 4)
        self.assertIsNotNone(self.geometry.airbox)
        self.assertIsNotNone(self.geometry.top_ground)
        expected_keepouts = sum(len(self.geometry.signal_shapes[signal]) for signal in ("P", "N"))
        self.assertEqual(len(self.geometry.top_ground["keepouts"]), expected_keepouts)
        for port in self.geometry.ports:
            signal = port["signal"]
            category = "trace_in" if port["side"] == "IN" else "trace_out"
            shape = next(item for item in self.geometry.signal_shapes[signal]
                         if item.category == category)
            expected_x = shape.bounds.x_min if port["side"] == "IN" else shape.bounds.x_max
            self.assertAlmostEqual(port["x"], expected_x)

    def test_port_geometry_reaches_nearest_reference_and_defines_integration_line(self) -> None:
        layers = {layer.name: layer for layer in self.geometry.stackup}
        self.assertTrue(all(isinstance(port, PortGeometry) for port in self.geometry.ports))
        for port in self.geometry.ports:
            self.assertEqual(port["reference_layer"], "L2")
            self.assertEqual(port["reference_conductor"], "PLANE_L2")
            self.assertEqual(port["reference_conductors"], ["PLANE_L2"])
            self.assertAlmostEqual(port["z_min"], layers["L2"].z_bottom)
            self.assertAlmostEqual(port["z_max"], self.geometry.trace_z_top)
            self.assertAlmostEqual(port["integration_line_start"][2],
                                   (self.geometry.trace_z_bottom + self.geometry.trace_z_top) / 2.0)
            self.assertAlmostEqual(port["integration_line_end"][2], layers["L2"].z_top)
            self.assertLess(port["integration_line_end"][2], port["integration_line_start"][2])
            self.assertEqual(port["validation_status"], "local_estimate_not_hfss_verified")
            self.assertEqual(port["size_mode"], "auto")
            self.assertGreater(port["lateral_margin"], 0)
            self.assertIn("trace_width", port["size_basis"])
            self.assertFalse(port["clipped_by_board"])

    def test_manual_port_margin_is_authoritative_and_reported(self) -> None:
        config = self.manager.default(); config["simulation"]["enabled"] = True
        config["simulation"]["port_size_mode"] = "manual"
        config["simulation"]["port_lateral_margin"] = .18
        config, _validation = self.manager.validate(config, check_output_directory=False)
        geometry = GeometryCalculator(config).calculate()
        for port in geometry.ports:
            self.assertEqual(port["size_mode"], "manual")
            self.assertAlmostEqual(port["lateral_margin"], .18)
            self.assertIn("manual", port["size_basis"])

    def test_partial_hfss_messages_cannot_promote_ports_to_verified(self) -> None:
        port_names = tuple(port["name"] for port in self.geometry.ports)
        partial = SimpleNamespace(
            stale=False, incomplete=False,
            items=[SimpleNamespace(rule_id="PORT-011", level="info", source="hfss",
                                   object_keys=port_names)],
        )
        scene = build_model_scene(self.config, self.geometry, partial)
        ports = [item for item in scene.objects if item.category == "Port"]
        self.assertTrue(all(item.status == "本地预估" for item in ports))
        self.assertTrue(all("未完成" in item.validate_result for item in ports))

    def test_manual_reference_and_stackup_changes_recalculate_same_port_geometry(self) -> None:
        config = self.manager.default(); config["simulation"]["enabled"] = True
        config["simulation"]["port_reference_mode"] = "manual"
        config["simulation"]["port_reference_layer"] = "L4"
        next(layer for layer in config["stackup"] if layer["name"] == "DIEL_L1_L2")["thickness"] = 0.25
        config, _validation = self.manager.validate(config, check_output_directory=False)
        geometry = GeometryCalculator(config).calculate()
        layers = {layer.name: layer for layer in geometry.stackup}
        for port in geometry.ports:
            self.assertEqual(port["reference_layer"], "L4")
            self.assertAlmostEqual(port["z_min"], layers["L4"].z_bottom)
            self.assertAlmostEqual(port["integration_line_end"][2], layers["L4"].z_top)

    def test_every_scene_category_preserves_authoritative_xyz(self) -> None:
        layers = {layer.name: layer for layer in self.geometry.stackup}
        for item in (obj for obj in self.scene.objects if obj.category == "PCB叠层"):
            self.assertAlmostEqual(item.z_bottom, layers[item.layer].z_bottom)
            self.assertAlmostEqual(item.z_top, layers[item.layer].z_top)
        signal_shapes = {shape.name: shape for signal in ("P", "N")
                         for shape in self.geometry.signal_shapes[signal]}
        for item in (obj for obj in self.scene.objects if obj.category == "信号走线"):
            shape = signal_shapes[item.name]
            self.assertEqual(item.points_xy, tuple((point.x, point.y) for point in shape.points))
            self.assertAlmostEqual(item.z_bottom, shape.z_bottom)
            self.assertAlmostEqual(item.z_top, shape.z_bottom + shape.thickness)
        vias = {via.name: via for via in self.geometry.gnd_vias}
        for item in (obj for obj in self.scene.objects
                     if obj.category == "GND VIA" and obj.key.endswith(":barrel")):
            via = vias[item.key.split(":")[1]]
            self.assertEqual(item.center_xy, (via.x, via.y))
            self.assertAlmostEqual(item.z_bottom, via.z_bottom)
            self.assertAlmostEqual(item.z_top, via.z_top)
        ports = {port["name"]: port for port in self.geometry.ports}
        for item in (obj for obj in self.scene.objects if obj.category == "Port"):
            port = ports[item.name]
            self.assertEqual(item.bounds_xy, (port["x"], port["x"], port["y_min"], port["y_max"]))
            self.assertAlmostEqual(item.z_bottom, port["z_bottom"])
            self.assertAlmostEqual(item.z_top, port["z_top"])

    def test_scene_keeps_antipads_and_vias_on_real_layers(self) -> None:
        layer_map = {layer.name: layer for layer in self.geometry.stackup}
        antipads = [item for item in self.scene.objects if item.category == "反焊盘"]
        self.assertGreater(len(antipads), 0)
        for item in antipads:
            self.assertAlmostEqual(item.z_bottom, layer_map[item.layer].z_bottom)
            self.assertAlmostEqual(item.z_top, layer_map[item.layer].z_top)
        barrels = [item for item in self.scene.objects
                   if item.category == "GND VIA" and item.details.get("结构") in {"镀铜桶壁", "实心圆柱"}]
        self.assertEqual(len(barrels), len(self.geometry.gnd_vias))
        self.assertTrue(all(item.z_top > item.z_bottom for item in barrels))
        self.assertTrue(next(item for item in self.scene.objects if item.key == "airbox").default_visible)
        self.assertEqual(next(item for item in self.scene.objects if item.key == "airbox").object_type,
                         "box_outline")
        self.assertTrue(all(item.default_visible for item in self.scene.objects
                            if item.category in {"Port", "开放边界"}))
        self.assertEqual(len([item for item in self.scene.objects if item.category == "Port"]), 4)
        self.assertEqual(next(item for item in self.scene.objects
                              if item.category == "开放边界").name, "OPEN_RADIATION")

    def test_disabled_simulation_uses_explicit_non_hfss_placeholders(self) -> None:
        config = self.manager.default()
        config["simulation"]["enabled"] = False
        config, validation = self.manager.validate(config, check_output_directory=False)
        geometry = GeometryCalculator(config).calculate()
        scene = build_model_scene(config, geometry, validation)
        placeholders = [item for item in scene.objects
                        if item.category in {"Port", "空气盒", "开放边界"}]
        self.assertEqual(len([item for item in placeholders if item.category == "Port"]), 4)
        self.assertTrue(all(item.status == "未启用" for item in placeholders))
        self.assertTrue(all("不会生成到HFSS" in item.validate_result for item in placeholders))
        self.assertTrue(all(item.color_key == "port" for item in placeholders
                            if item.category == "Port"))

    @unittest.skipUnless(VTK_AVAILABLE, "VTK unavailable")
    def test_reference_plane_mesh_has_real_openings(self) -> None:
        plane = next(item for item in self.scene.objects
                     if item.object_type == "plane_with_holes" and item.category == "PCB叠层")
        mesh = mesh_for_object(plane)
        self.assertGreater(mesh.GetNumberOfCells(), 10)
        self.assertAlmostEqual(mesh.GetBounds()[4], plane.z_bottom, places=6)
        self.assertAlmostEqual(mesh.GetBounds()[5], plane.z_top, places=6)

    @unittest.skipUnless(VTK_AVAILABLE, "VTK unavailable")
    def test_airbox_wireframe_has_only_twelve_physical_edges(self) -> None:
        airbox = next(item for item in self.scene.objects if item.key == "airbox")
        mesh = mesh_for_object(airbox)
        self.assertEqual(mesh.GetNumberOfLines(), 12)
        self.assertEqual(mesh.GetNumberOfPolys(), 0)

    @unittest.skipUnless(VTK_AVAILABLE, "VTK unavailable")
    def test_vtk_port_is_exact_zero_thickness_sheet_without_visual_padding(self) -> None:
        port = next(item for item in self.scene.objects if item.category == "Port")
        mesh = mesh_for_object(port)
        self.assertEqual(mesh.GetNumberOfPolys(), 1)
        self.assertAlmostEqual(mesh.GetBounds()[0], port.bounds_xy[0], places=6)
        self.assertAlmostEqual(mesh.GetBounds()[1], port.bounds_xy[0], places=6)
        self.assertAlmostEqual(mesh.GetBounds()[4], port.z_bottom, places=6)
        self.assertAlmostEqual(mesh.GetBounds()[5], port.z_top, places=6)


class ModelPreviewGuiTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from main_gui import SurfaceCapAntipadApp
            self.app = SurfaceCapAntipadApp()
            self.app.withdraw()
            self.app.update_idletasks()
            self.app.refresh_visuals()
            self.app.model_preview_workbench.update_pending()
        except tk.TclError as exc:
            self.skipTest(f"Tk GUI unavailable: {exc}")

    def tearDown(self) -> None:
        if hasattr(self, "app"):
            self.app.destroy()

    def test_tab_defaults_to_real_3d_workbench(self) -> None:
        self.assertIn("模型预览", self.app.tabs)
        self.assertNotIn("二维预览", self.app.tabs)
        self.assertFalse(hasattr(self.app, "preview_canvas"))
        workbench = self.app.model_preview_workbench
        self.assertIsNotNone(workbench.scene)
        self.assertFalse(NATIVE_VTK_EMBED_ENABLED)
        self.assertFalse(workbench.vtk_ready)
        self.assertTrue(workbench.software_ready)
        self.assertGreater(len(workbench.objects), 20)
        self.assertEqual(workbench.z_display_mode.get(), "真实比例")
        self.assertEqual(workbench.z_scale_var.get(), "1×")
        self.assertEqual(workbench._z_scale(), 1.0)
        for view in ("等轴测", "顶视图", "底视图", "前视图", "后视图",
                     "左视图", "右视图", "叠层剖视图"):
            workbench.set_view(view)

    def test_switching_model_preview_tab_uses_stable_canvas_and_draws_ports(self) -> None:
        workbench = self.app.model_preview_workbench
        self.app.variables["simulation.enabled"].set(True)
        self.app.notebook.select(self.app.tabs["项目与走线"])
        self.app.update()
        self.app.notebook.select(self.app.tabs["模型预览"])
        self.app.refresh_visuals(); workbench.update_pending(); self.app.update()
        port_keys = [key for key, item in workbench.objects.items() if item.category == "Port"]
        self.assertEqual(len(port_keys), 4)
        self.assertTrue(all(workbench.visibility[key] for key in port_keys))
        self.assertGreater(len(workbench.software_canvas.find_all()), 20)
        self.assertTrue(any(key in port_keys for key in workbench._canvas_tag_keys.values()))
        labels = [item for item in workbench.software_canvas.find_withtag("port_label")
                  if workbench.software_canvas.type(item) == "text"]
        self.assertEqual(len(labels), 4)
        boxes = [workbench.software_canvas.bbox(item) for item in labels]
        for index, first in enumerate(boxes):
            for second in boxes[index + 1:]:
                self.assertTrue(first[2] < second[0] or first[0] > second[2]
                                or first[3] < second[1] or first[1] > second[3])

    def test_ordinary_trace_has_no_default_per_vertex_vertical_lines(self) -> None:
        workbench = self.app.model_preview_workbench
        trace_key = next(key for key, item in workbench.objects.items()
                         if item.category == "信号走线")
        for key in workbench.visibility:
            workbench.visibility[key] = key == trace_key
        workbench.selected_key = None; workbench._software_redraw()
        trace_tags = [tag for tag, key in workbench._canvas_tag_keys.items() if key == trace_key]
        self.assertEqual(len(trace_tags), 1)
        default_lines = [canvas_id for canvas_id in workbench.software_canvas.find_withtag(trace_tags[0])
                         if workbench.software_canvas.type(canvas_id) == "line"]
        self.assertEqual(default_lines, [])
        workbench.selected_key = trace_key; workbench._software_redraw()
        selected_lines = [canvas_id for canvas_id in workbench.software_canvas.find_withtag(trace_tags[0])
                          if workbench.software_canvas.type(canvas_id) == "line"]
        self.assertGreater(len(selected_lines), 0)
        self.assertLessEqual(len(selected_lines), 4)

    def test_orbit_pan_zoom_and_standard_view_axes(self) -> None:
        workbench = self.app.model_preview_workbench
        for sequence in ("<ButtonPress-1>", "<B1-Motion>", "<ButtonRelease-1>",
                         "<Double-Button-1>", "<ButtonPress-2>", "<B2-Motion>",
                         "<MouseWheel>", "<Button-3>"):
            self.assertTrue(workbench.software_canvas.bind(sequence), sequence)
        workbench.fit_model()
        original_target = tuple(workbench._camera_target)
        original_yaw = workbench._camera_yaw
        workbench._software_left_press(SimpleNamespace(x=120, y=120))
        workbench._software_left_drag(SimpleNamespace(x=165, y=142))
        workbench._software_left_release(SimpleNamespace(x=165, y=142))
        self.assertNotEqual(workbench._camera_yaw, original_yaw)
        self.assertEqual(tuple(workbench._camera_target), original_target)
        pan_start = tuple(workbench._camera_target)
        workbench._software_middle_press(SimpleNamespace(x=180, y=160))
        workbench._software_middle_drag(SimpleNamespace(x=210, y=185))
        workbench._software_pointer_release()
        self.assertNotEqual(tuple(workbench._camera_target), pan_start)
        scale_start = workbench._camera_scale
        workbench._software_mouse_wheel(SimpleNamespace(x=250, y=220, delta=120))
        self.assertGreater(workbench._camera_scale, scale_start)
        workbench.set_view("顶视图", fit=False)
        origin = workbench._software_raw_projection((0.0, 0.0, 0.0))
        x_point = workbench._software_raw_projection((1.0, 0.0, 0.0))
        y_point = workbench._software_raw_projection((0.0, 1.0, 0.0))
        self.assertGreater(x_point[0], origin[0])
        self.assertGreater(y_point[1], origin[1])

    def test_fit_model_ignores_airbox_and_refresh_preserves_camera(self) -> None:
        workbench = self.app.model_preview_workbench
        config = self.app.manager.default(); config["simulation"]["enabled"] = True
        config, validation = self.app.manager.validate(config, check_output_directory=False)
        geometry = GeometryCalculator(config).calculate()
        workbench.show(config, geometry, validation); workbench.update_pending()
        workbench.set_view("等轴测"); workbench.fit_model()
        model_scale = workbench._camera_scale
        model_target = tuple(workbench._camera_target)
        workbench.visibility["airbox"] = False; workbench.visibility["open_boundary"] = False
        workbench.fit_model()
        self.assertAlmostEqual(workbench._camera_scale, model_scale)
        self.assertEqual(tuple(workbench._camera_target), model_target)
        workbench.visibility["airbox"] = True; workbench.visibility["open_boundary"] = True
        workbench.fit_all()
        self.assertLessEqual(workbench._camera_scale, model_scale)
        workbench.fit_model()
        workbench._camera_yaw += .2
        workbench._camera_target[0] += .15
        preserved = (workbench._camera_yaw, tuple(workbench._camera_target), workbench._camera_scale)
        object_count = len(workbench.objects)
        workbench.show(config, geometry, validation); workbench.update_pending()
        self.assertEqual((workbench._camera_yaw, tuple(workbench._camera_target), workbench._camera_scale), preserved)
        self.assertEqual(len(workbench.objects), object_count)
        self.assertEqual(len(workbench.objects), len(set(workbench.objects)))

    def test_z_scale_and_explosion_are_visual_only_and_not_accumulated(self) -> None:
        workbench = self.app.model_preview_workbench
        item = next(obj for obj in workbench.objects.values()
                    if obj.category == "PCB叠层" and obj.layer_index is not None)
        source_z = (item.z_bottom, item.z_top)
        workbench.z_scale_var.set("1×"); workbench.exploded.set(False); workbench._apply_transforms()
        visual = workbench._software_visual_point((0.0, 0.0, item.z_top), item)
        self.assertAlmostEqual(visual[2], item.z_top)
        workbench.z_scale_var.set("5×"); workbench.exploded.set(True); workbench._apply_transforms()
        expected = (item.z_top + workbench._layer_offset(item)) * 5.0
        self.assertAlmostEqual(workbench._software_visual_point((0.0, 0.0, item.z_top), item)[2], expected)
        workbench._apply_transforms(); workbench.set_view("前视图", fit=False); workbench.set_view("等轴测", fit=False)
        self.assertEqual((item.z_bottom, item.z_top), source_z)
        self.assertAlmostEqual(workbench._software_visual_point((0.0, 0.0, item.z_top), item)[2], expected)

    def test_enhanced_stackup_does_not_stretch_capacitor_body(self) -> None:
        workbench = self.app.model_preview_workbench
        body = next(item for item in workbench.objects.values()
                    if item.details.get("结构") == "电容本体")
        layer = next(item for item in workbench.objects.values()
                     if item.category == "PCB叠层" and item.z_top > item.z_bottom)
        source = (body.z_bottom, body.z_top, layer.z_bottom, layer.z_top)
        workbench.z_display_mode.set("增强叠层"); workbench._apply_transforms()
        body_visual_height = (workbench._map_visual_z(body.z_top)
                              - workbench._map_visual_z(body.z_bottom))
        layer_visual_height = (workbench._map_visual_z(layer.z_top)
                               - workbench._map_visual_z(layer.z_bottom))
        self.assertAlmostEqual(body_visual_height, body.z_top - body.z_bottom)
        self.assertAlmostEqual(layer_visual_height, (layer.z_top - layer.z_bottom) * 3.0)
        self.assertEqual((body.z_bottom, body.z_top, layer.z_bottom, layer.z_top), source)
        self.assertIn("不代表真实比例", workbench.zoom_notice.cget("text"))

    def test_two_capacitor_bodies_are_distinct_complete_ceramic_solids(self) -> None:
        workbench = self.app.model_preview_workbench
        bodies = [item for item in workbench.objects.values()
                  if item.details.get("结构") == "电容本体"]
        self.assertEqual({item.key for item in bodies}, {"body:C_P", "body:C_N"})
        self.assertTrue(all(item.object_type == "polygon_prism" and len(item.points_xy) == 4
                            for item in bodies))
        self.assertNotEqual(bodies[0].points_xy, bodies[1].points_xy)
        self.assertEqual(COLORS["body"], (0.82, 0.78, 0.66))
        self.assertTrue(all(item.opacity < 1 for item in bodies))

    def test_port_quad_validation_rejects_collapsed_or_self_crossing_projection(self) -> None:
        self.assertTrue(ModelPreviewWorkbench._projected_quad_is_valid(
            [(0, 0), (20, 0), (20, 10), (0, 10)]))
        self.assertFalse(ModelPreviewWorkbench._projected_quad_is_valid(
            [(0, 0), (20, 10), (0, 10), (20, 0)]))
        self.assertFalse(ModelPreviewWorkbench._projected_quad_is_valid(
            [(0, 0), (20, 0), (20, 0), (0, 0)]))

    def test_port_front_view_and_screen_labels_are_non_overlapping(self) -> None:
        workbench = self.app.model_preview_workbench
        config = self.app.manager.default(); config["simulation"]["enabled"] = True
        config, validation = self.app.manager.validate(config, check_output_directory=False)
        geometry = GeometryCalculator(config).calculate()
        workbench.show(config, geometry, validation); workbench.update_pending()
        key = next(key for key, item in workbench.objects.items() if item.name == "PORT_IN_P")
        workbench.select_object(key); workbench.port_front_view(); workbench._software_redraw()
        self.assertEqual(workbench.software_view, "右视图")
        tag = next(tag for tag, object_key in workbench._canvas_tag_keys.items() if object_key == key)
        polygons = [item for item in workbench.software_canvas.find_withtag(tag)
                    if workbench.software_canvas.type(item) == "polygon"]
        self.assertTrue(polygons)
        port_text = "\n".join(workbench.software_canvas.itemcget(item, "text")
                              for item in workbench.software_canvas.find_withtag(tag)
                              if workbench.software_canvas.type(item) == "text")
        self.assertIn("Reference: L2", port_text)
        label_texts = [item for item in workbench.software_canvas.find_withtag("port_label")
                       if workbench.software_canvas.type(item) == "text"]
        boxes = [workbench.software_canvas.bbox(item) for item in label_texts]
        for index, first in enumerate(boxes):
            for second in boxes[index + 1:]:
                self.assertTrue(first[2] < second[0] or first[0] > second[2]
                                or first[3] < second[1] or first[1] > second[3])

    def test_airbox_and_boundary_do_not_draw_duplicate_twelve_edge_boxes(self) -> None:
        workbench = self.app.model_preview_workbench
        config = self.app.manager.default(); config["simulation"]["enabled"] = True
        config, validation = self.app.manager.validate(config, check_output_directory=False)
        geometry = GeometryCalculator(config).calculate()
        workbench.show(config, geometry, validation); workbench.update_pending()
        for key in workbench.visibility:
            workbench.visibility[key] = key in {"airbox", "open_boundary"}
        workbench._software_redraw()
        by_key = {key: tag for tag, key in workbench._canvas_tag_keys.items()
                  if key in {"airbox", "open_boundary"}}
        air_lines = [item for item in workbench.software_canvas.find_withtag(by_key["airbox"])
                     if workbench.software_canvas.type(item) == "line"]
        boundary_lines = [item for item in workbench.software_canvas.find_withtag(by_key["open_boundary"])
                          if workbench.software_canvas.type(item) == "line"]
        self.assertEqual(len(air_lines), 12)
        self.assertEqual(len(boundary_lines), 16)  # eight independent surface corner markers

    def test_diagnostics_is_default_off_and_reports_camera_and_selection(self) -> None:
        workbench = self.app.model_preview_workbench
        self.assertFalse(workbench.diagnostics_enabled.get())
        workbench.select_object(next(iter(workbench.objects)))
        workbench.diagnostics_enabled.set(True); workbench._software_redraw()
        diagnostics = workbench.software_canvas.find_withtag("diagnostics")
        self.assertTrue(diagnostics)
        text = "\n".join(str(workbench.software_canvas.itemcget(item, "text"))
                         for item in diagnostics if workbench.software_canvas.type(item) == "text")
        self.assertIn("相机位置", text)
        self.assertIn("旋转中心", text)
        self.assertIn("Actor/对象数", text)

    def test_categories_render_in_required_incremental_validation_order(self) -> None:
        workbench = self.app.model_preview_workbench
        config = self.app.manager.default()
        config["gnd_vias"]["enabled"] = True
        config["gnd_vias"]["create_top_ground_copper"] = True
        config["simulation"]["enabled"] = True
        config, validation = self.app.manager.validate(config, check_output_directory=False)
        geometry = GeometryCalculator(config).calculate()
        workbench.show(config, geometry, validation); workbench.update_pending()
        order = ("PCB叠层", "信号走线", "电容与焊盘", "电气模型", "反焊盘",
                 "GND VIA", "安装面GND铜皮", "Port", "空气盒", "开放边界")
        enabled = set()
        for category in order:
            enabled.add(category)
            for key, item in workbench.objects.items():
                workbench.visibility[key] = item.category in enabled
            workbench._software_redraw()
            rendered_keys = set(workbench._canvas_tag_keys.values())
            expected = {key for key, item in workbench.objects.items()
                        if item.category in enabled and workbench._software_world_points(item)}
            self.assertEqual(rendered_keys, expected, category)

    def test_quick_controls_sync_with_tree_and_fit_modes(self) -> None:
        workbench = self.app.model_preview_workbench
        config = self.app.manager.default(); config["simulation"]["enabled"] = True
        config, validation = self.app.manager.validate(config, check_output_directory=False)
        geometry = GeometryCalculator(config).calculate()
        workbench.show(config, geometry, validation); workbench.update_pending()
        workbench.quick_vars["Port"].set(False)
        workbench._quick_toggle("Port")
        self.assertFalse(any(workbench.visibility[key] for key, item in workbench.objects.items()
                             if item.category == "Port"))
        port_parent = "cat:Port"
        workbench.tree.selection_set(port_parent)
        workbench.toggle_visibility()
        self.assertTrue(workbench.quick_vars["Port"].get())
        workbench.airbox_mode.set("仅线框")
        workbench._airbox_mode_changed()
        self.assertTrue(workbench.visibility["airbox"])
        workbench.fit_model(); workbench.fit_all()

    def test_invalid_parameters_preserve_last_valid_scene(self) -> None:
        workbench = self.app.model_preview_workbench
        scene = workbench.scene
        object_count = len(workbench.objects)
        workbench.show_error("pad_length 不是有效数字")
        self.assertIs(workbench.scene, scene)
        self.assertEqual(len(workbench.objects), object_count)
        self.assertIn("不是最新状态", workbench.stale_var.get())

    def test_selection_layer_isolation_and_navigation(self) -> None:
        workbench = self.app.model_preview_workbench
        pad_key = next(key for key, item in workbench.objects.items() if key.startswith("pad:"))
        workbench.select_object(pad_key)
        self.assertIn("参数来源", workbench.detail_var.get())
        workbench.goto_parameter()
        self.app.update_idletasks()
        self.assertEqual(self.app.notebook.select(), str(self.app.tabs["电容与模型"]))
        layer_key = next(key for key, item in workbench.objects.items()
                          if item.category == "PCB叠层" and item.layer == "L2")
        workbench.tree.selection_set(f"obj:{layer_key}")
        workbench.show_selected_layer()
        self.assertTrue(workbench.visibility[layer_key])
        self.assertFalse(any(workbench.visibility[key] for key, item in workbench.objects.items()
                             if item.layer and item.layer != "L2"))

    def test_3d_selection_does_not_reenter_tree_selection(self) -> None:
        workbench = self.app.model_preview_workbench
        key = next(key for key, item in workbench.objects.items()
                   if item.category == "电容与焊盘")
        workbench.select_object(key)
        # Process the virtual TreeviewSelect event generated by selection_set.
        # Before the guard this event selected the same object indefinitely.
        self.app.update()
        self.assertEqual(workbench.selected_key, key)
        self.assertEqual(workbench.tree.selection(), (f"obj:{key}",))
        self.assertIsNone(workbench._selection_job)


if __name__ == "__main__":
    unittest.main(verbosity=2)
