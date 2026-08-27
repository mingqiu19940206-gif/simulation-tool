"""Font-aware helpers shared by the Tk canvas previews.

The helpers in this module are intentionally presentation-only.  They never
read or mutate engineering configuration or geometry data.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from typing import Callable, Iterable, Optional, Sequence, Tuple


Box = Tuple[float, float, float, float]
Point = Tuple[float, float]


def canvas_viewport(canvas: tk.Canvas) -> Optional[Tuple[int, int]]:
    """Return the real drawable viewport, or ``None`` before Tk lays it out."""

    width, height = int(canvas.winfo_width()), int(canvas.winfo_height())
    if width <= 2 or height <= 2:
        # Unit tests and startup may update an intentionally withdrawn window.
        # Drawing into its requested (not visible) size preserves widget state;
        # the first mapped Configure always redraws with the real viewport.
        if not bool(canvas.winfo_ismapped()):
            requested_width = int(canvas.winfo_reqwidth())
            requested_height = int(canvas.winfo_reqheight())
            if requested_width > 2 and requested_height > 2:
                return requested_width, requested_height
        return None
    return width, height


def resolve_font(widget: tk.Misc, spec: object) -> tkfont.Font:
    if isinstance(spec, str):
        try:
            return tkfont.nametofont(spec, root=widget)
        except tk.TclError:
            pass
    return tkfont.Font(root=widget, font=spec)


def font_linespace(widget: tk.Misc, spec: object) -> int:
    return max(1, int(resolve_font(widget, spec).metrics("linespace")))


def text_size(widget: tk.Misc, text: str, spec: object) -> Tuple[int, int]:
    font = resolve_font(widget, spec)
    lines = str(text).splitlines() or [""]
    return max((int(font.measure(line)) for line in lines), default=0), max(
        1, len(lines) * int(font.metrics("linespace"))
    )


def ellipsize(widget: tk.Misc, text: str, spec: object, maximum: int) -> str:
    """Fit one line to ``maximum`` pixels without changing the font."""

    maximum = max(1, int(maximum))
    font = resolve_font(widget, spec)
    if font.measure(text) <= maximum:
        return text
    suffix = "…"
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if font.measure(text[:middle] + suffix) <= maximum:
            low = middle
        else:
            high = middle - 1
    return text[:low] + suffix


def responsive_grid_columns(
    width: int,
    item_count: int,
    minimum_item_width: int,
    *,
    choices: Sequence[int],
    gap: int = 6,
) -> int:
    """Choose a supported grid width from the widget's real pixel width.

    ``minimum_item_width`` is expected to come from font measurements plus the
    icon and padding tracks.  Keeping the policy here lets card selectors share
    the same behavior without tying their breakpoints to screen resolution.
    """

    available = max(1, int(width))
    count = max(1, int(item_count))
    minimum = max(1, int(minimum_item_width))
    spacing = max(0, int(gap))
    supported = sorted(
        {max(1, min(count, int(value))) for value in choices if int(value) > 0},
        reverse=True,
    )
    if 1 not in supported:
        supported.append(1)
    for columns in supported:
        required = columns * minimum + max(0, columns - 1) * spacing
        if available >= required:
            return columns
    return 1


def debounce_after(
    widget: tk.Misc,
    attribute: str,
    callback: Callable[[], None],
    *,
    delay_ms: int = 24,
) -> None:
    """Schedule one layout callback and replace an older pending request."""

    scheduler = widget.winfo_toplevel()
    pending = getattr(widget, attribute, None)
    if pending:
        try:
            scheduler.after_cancel(pending)
        except tk.TclError:
            pass

    def run() -> None:
        setattr(widget, attribute, None)
        try:
            callback()
        except tk.TclError:
            # A queued Configure can outlive a page during application teardown.
            return

    # Register the Tcl command on the toplevel.  Application teardown commonly
    # cancels all ``after`` jobs through that object; keeping command ownership
    # there avoids child widgets later trying to delete an already-cancelled
    # callback command.
    setattr(widget, attribute, scheduler.after(max(0, int(delay_ms)), run))


def boxes_intersect(first: Box, second: Box, padding: float = 2.0) -> bool:
    return not (
        first[2] + padding <= second[0]
        or first[0] >= second[2] + padding
        or first[3] + padding <= second[1]
        or first[1] >= second[3] + padding
    )


class CanvasLabelPlacer:
    """Place screen-space labels inside a viewport without text collisions."""

    def __init__(self, canvas: tk.Canvas, width: int, height: int, *, safe: int = 8) -> None:
        self.canvas = canvas
        self.width = int(width)
        self.height = int(height)
        self.safe = int(safe)
        self.occupied: list[Box] = []
        self.hidden: list[str] = []

    def reserve(self, box: Optional[Sequence[float]]) -> None:
        if box and len(box) == 4:
            self.occupied.append(tuple(float(value) for value in box))  # type: ignore[arg-type]

    def _box(self, center: Point, text: str, font: object,
             padding: Tuple[int, int]) -> Tuple[Point, Box]:
        text_width, text_height = text_size(self.canvas, text, font)
        half_width = text_width / 2.0 + padding[0]
        half_height = text_height / 2.0 + padding[1]
        cx = min(self.width - self.safe - half_width,
                 max(self.safe + half_width, float(center[0])))
        cy = min(self.height - self.safe - half_height,
                 max(self.safe + half_height, float(center[1])))
        return (cx, cy), (cx-half_width, cy-half_height, cx+half_width, cy+half_height)

    def place(
        self,
        text: str,
        candidates: Iterable[Point],
        *,
        font: object = "TkDefaultFont",
        fill: str = "#172033",
        background: str = "",
        outline: str = "",
        tags: Sequence[str] = (),
        leader: Optional[Point] = None,
        leader_color: Optional[str] = None,
        padding: Tuple[int, int] = (4, 2),
        priority: int = 1,
    ) -> Optional[int]:
        chosen: Optional[Point] = None
        chosen_box: Optional[Box] = None
        for candidate in candidates:
            position, box = self._box(candidate, text, font, padding)
            if not any(boxes_intersect(box, old) for old in self.occupied):
                chosen, chosen_box = position, box
                break
        if chosen is None or chosen_box is None:
            # No label is allowed to overwrite another one.  Hidden secondary
            # details remain available through selection and hover text.
            self.hidden.append(text)
            return None
        self.occupied.append(chosen_box)
        item_tags = tuple(tags)
        if leader is not None and (abs(leader[0]-chosen[0]) > 5 or abs(leader[1]-chosen[1]) > 5):
            leader_tags = tuple(tag for tag in item_tags if not str(tag).startswith("scene_object_"))
            self.canvas.create_line(
                leader[0], leader[1], chosen[0], chosen[1],
                fill=leader_color or fill, width=1, dash=(3, 3),
                tags=leader_tags + ("label-leader",),
            )
        text_id = self.canvas.create_text(
            chosen[0], chosen[1], text=text, justify="center", fill=fill,
            font=font, tags=item_tags,
        )
        if background:
            bbox = self.canvas.bbox(text_id)
            if bbox:
                background_id = self.canvas.create_rectangle(
                    bbox[0]-padding[0], bbox[1]-padding[1],
                    bbox[2]+padding[0], bbox[3]+padding[1],
                    fill=background, outline=outline, width=1,
                    tags=item_tags + ("label-background",),
                )
                self.canvas.tag_lower(background_id, text_id)
        return text_id
