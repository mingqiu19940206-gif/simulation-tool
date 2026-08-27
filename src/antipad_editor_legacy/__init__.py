"""反焊盘实时预览兼容包，并优化工作台初次选层。"""
from __future__ import annotations

import math
from typing import Any, List, Sequence

import antipad_editor_preview_base as _base
from antipad_editor_preview_base import *  # noqa: F401,F403


def _same_point(first: PointTuple, second: PointTuple, tolerance: float = 1.0e-8) -> bool:
    return math.dist(first, second) <= tolerance


def _merge_collinear_segments(segments: Sequence[SegmentTuple]) -> List[SegmentTuple]:
    """合并相邻共线边，消除布尔边界在交点处产生的显示接缝。"""
    merged = list(segments)
    changed = True
    while changed:
        changed = False
        for left in range(len(merged)):
            first_start, first_end = merged[left]
            first_vector = (first_end[0] - first_start[0], first_end[1] - first_start[1])
            first_length = math.hypot(*first_vector)
            for right in range(left + 1, len(merged)):
                second_start, second_end = merged[right]
                second_vector = (second_end[0] - second_start[0], second_end[1] - second_start[1])
                second_length = math.hypot(*second_vector)
                if first_length <= 1.0e-12 or second_length <= 1.0e-12:
                    continue
                cross = abs(first_vector[0] * second_vector[1] - first_vector[1] * second_vector[0])
                if cross > 1.0e-8 * first_length * second_length:
                    continue
                combinations = (
                    (first_start, second_start, first_end, second_end),
                    (first_start, second_end, first_end, second_start),
                    (first_end, second_start, first_start, second_end),
                    (first_end, second_end, first_start, second_start),
                )
                replacement = next(
                    ((outer_first, outer_second) for shared_first, shared_second, outer_first, outer_second
                     in combinations if _same_point(shared_first, shared_second)), None
                )
                if replacement is None:
                    continue
                merged[left] = replacement
                del merged[right]
                changed = True
                break
            if changed:
                break
    return merged


_raw_union_boundary_segments = _base.union_boundary_segments


def union_boundary_segments(polygons):
    return _merge_collinear_segments(_raw_union_boundary_segments(polygons))


_base.union_boundary_segments = union_boundary_segments


class AntipadWorkbench(_base.AntipadWorkbench):
    """首次进入页面时定位到首个已启用反焊盘的参考层。"""

    def __init__(self, parent: Any, app: Any) -> None:
        super().__init__(parent, app)
        layers = list(app.config.get("stackup", []))
        current = layers[self.selected_index] if layers else None
        if current is not None and current.get("enable_antipad"):
            return
        target = next(
            (
                index for index, layer in enumerate(layers)
                if layer.get("type") == "plane"
                and layer.get("is_reference_plane")
                and layer.get("enable_antipad")
            ),
            None,
        )
        if target is None:
            return
        self.selected_index = target
        self.preview.layer_var.set(str(layers[target]["name"]))
        self._sync_layer_panel()
