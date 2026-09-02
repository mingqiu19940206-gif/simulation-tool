"""Font-aware helpers shared by the Tk canvas previews.

The helpers in this module are intentionally presentation-only.  They never
read or mutate engineering configuration or geometry data.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Any, Callable, Iterable, Optional, Sequence, Tuple


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
        force: bool = False,
    ) -> Optional[int]:
        chosen: Optional[Point] = None
        chosen_box: Optional[Box] = None
        candidate_list = list(candidates)
        for candidate in candidate_list:
            position, box = self._box(candidate, text, font, padding)
            if not any(boxes_intersect(box, old) for old in self.occupied):
                chosen, chosen_box = position, box
                break
        if chosen is None or chosen_box is None:
            if force and candidate_list:
                chosen, chosen_box = self._box(candidate_list[0], text, font, padding)
            else:
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


def mousewheel_steps(event: tk.Event) -> int:
    """Return canvas ``yview_scroll`` steps for Windows, macOS and X11 wheels."""

    number = getattr(event, "num", None)
    if number == 4:
        return -1
    if number == 5:
        return 1
    delta = int(getattr(event, "delta", 0) or 0)
    if delta == 0:
        return 0
    if abs(delta) >= 120:
        return int(-delta / 120)
    return -1 if delta > 0 else 1


def bind_wraplength(
    label: tk.Misc,
    *,
    padding: int = 24,
    source: Optional[tk.Misc] = None,
    min_width: int = 80,
) -> None:
    """Keep a Label ``wraplength`` aligned to the live pane/widget width."""

    def _apply() -> None:
        widget = source or label
        try:
            width = int(widget.winfo_width())
        except tk.TclError:
            return
        if width <= 2 and source is None:
            parent = getattr(label, "master", None)
            if parent is None:
                return
            try:
                width = int(parent.winfo_width())
            except tk.TclError:
                return
        wrap = max(min_width, width - padding)
        try:
            current = int(float(label.cget("wraplength") or 0))
        except (tk.TclError, TypeError, ValueError):
            return
        if abs(current - wrap) > 2:
            try:
                label.configure(wraplength=wrap)
            except tk.TclError:
                return

    def _on_configure(_event: Optional[tk.Event] = None) -> None:
        debounce_after(label, "_wraplength_job", _apply)

    target = source or getattr(label, "master", None) or label
    target.bind("<Configure>", _on_configure, add="+")
    if source is None and label is not target:
        label.bind("<Configure>", _on_configure, add="+")
    try:
        label.after_idle(_apply)
    except tk.TclError:
        _apply()


def bind_existing_wraplength_labels(root: tk.Misc, *, padding: int = 24) -> None:
    """Rebind every Label that already requested wrapping so it follows pane width."""

    def visit(widget: tk.Misc) -> None:
        try:
            class_name = str(widget.winfo_class())
        except tk.TclError:
            return
        if class_name in {"TLabel", "Label"}:
            try:
                wrap = int(float(widget.cget("wraplength") or 0))
            except (tk.TclError, TypeError, ValueError):
                wrap = 0
            if wrap > 0:
                bind_wraplength(widget, padding=padding)
        try:
            children = widget.winfo_children()
        except tk.TclError:
            return
        for child in children:
            visit(child)

    visit(root)


def raise_canvas_labels(canvas: tk.Canvas) -> None:
    """Keep screen-space labels above filled geometry."""

    for tag in ("label-background", "label-leader", "object-label",
                "dimension-label", "axis-label", "hud-label"):
        try:
            canvas.tag_raise(tag)
        except tk.TclError:
            continue


def attach_mousewheel(scroller: tk.Misc, canvas: tk.Canvas, *roots: tk.Misc) -> None:
    """Forward wheel events from ``roots`` and their descendants to ``canvas``."""

    setattr(scroller, "_forwards_mousewheel", True)
    if getattr(scroller, "canvas", None) is None:
        setattr(scroller, "canvas", canvas)

    def _on_wheel(event: tk.Event) -> str:
        steps = mousewheel_steps(event)
        if steps:
            canvas.yview_scroll(steps, "units")
        return "break"

    def _bind_tree(widget: tk.Misc) -> None:
        if getattr(widget, "_mousewheel_forward_bound", False):
            children = ()
            try:
                children = widget.winfo_children()
            except tk.TclError:
                return
            for child in children:
                _bind_tree(child)
            return
        setattr(widget, "_mousewheel_forward_bound", True)
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            widget.bind(sequence, _on_wheel, add="+")
        widget.bind("<Map>", lambda _event, current=widget: _bind_tree(current), add="+")
        try:
            children = widget.winfo_children()
        except tk.TclError:
            return
        for child in children:
            _bind_tree(child)

    for root in (canvas, *roots):
        _bind_tree(root)


class WrappingButtonBar(ttk.Frame):
    """Reflow action buttons into rows when the bar is narrower than one line."""

    def __init__(
        self,
        master: tk.Misc,
        actions: Sequence[Tuple[str, Any]],
        *,
        overflow_label: str = "更多",
    ) -> None:
        super().__init__(master)
        self._actions = list(actions)
        self._overflow_label = overflow_label
        self._buttons = [
            ttk.Button(self, text=text, command=command) for text, command in actions
        ]
        self._overflow: Optional[ttk.Menubutton] = None
        self._last_signature: Tuple[int, ...] = ()
        self.bind("<Configure>", lambda _event: debounce_after(self, "_wrap_job", self._reflow))
        self.after_idle(self._reflow)

    @property
    def buttons(self) -> Sequence[ttk.Button]:
        return tuple(self._buttons)

    def _reflow(self) -> None:
        try:
            width = int(self.winfo_width())
        except tk.TclError:
            return
        if width <= 1:
            return
        sizes = [max(32, int(button.winfo_reqwidth())) for button in self._buttons]
        gap = 6
        rows: list[list[int]] = [[]]
        row_width = 0
        for index, size in enumerate(sizes):
            needed = size if not rows[-1] else row_width + gap + size
            if rows[-1] and needed > width:
                rows.append([index])
                row_width = size
            else:
                rows[-1].append(index)
                row_width = needed
        use_menu = len(rows) > 3 and width < 640
        signature = (width, int(use_menu), *(index for row in rows for index in row))
        if signature == self._last_signature:
            return
        self._last_signature = signature
        if self._overflow is not None:
            self._overflow.destroy()
            self._overflow = None
        for button in self._buttons:
            button.grid_forget()
            button.pack_forget()
        if use_menu:
            used = 0
            visible: list[int] = []
            overflow_need = 84
            for index, size in enumerate(sizes):
                extra = size if not visible else used + gap + size
                if visible and extra + overflow_need > width:
                    break
                visible.append(index)
                used = extra
            visible_set = set(visible)
            for index in visible:
                self._buttons[index].pack(side="left", padx=2, pady=2)
            hidden = [index for index in range(len(self._buttons)) if index not in visible_set]
            if hidden:
                menu_button = ttk.Menubutton(self, text=self._overflow_label)
                menu = tk.Menu(menu_button, tearoff=False)
                for index in hidden:
                    text, command = self._actions[index]
                    menu.add_command(label=text, command=command)
                menu_button.configure(menu=menu)
                menu_button.pack(side="left", padx=2, pady=2)
                self._overflow = menu_button
            return
        for row_index, row in enumerate(rows):
            for column, index in enumerate(row):
                self._buttons[index].grid(
                    row=row_index, column=column, padx=2, pady=2, sticky="w",
                )
