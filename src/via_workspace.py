"""v3.1 VIA-transition workspace chrome: collapsible groups, rail toggles, navigation.

Presentation-only helpers.  Engineering values stay on the existing
``variables`` / ``widgets`` bindings owned by ``SurfaceCapAntipadApp``.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Iterable, Mapping, Optional, Sequence, Tuple


VIA_GROUP_KEYS: Tuple[str, ...] = ("path", "signal", "backdrill", "return", "traces")

VIA_GROUP_TITLES: Mapping[str, str] = {
    "path": "① 换层路径",
    "signal": "② 信号孔",
    "backdrill": "③ 背钻",
    "return": "④ 回流孔",
    "traces": "⑤ 进出走线",
}

SCENARIO_LABELS: Mapping[str, str] = {
    "surface_capacitor": "AC 耦合电容",
    "via_transition": "差分 VIA 换层",
}

# Destinations for `_navigate_to_parameter`.  Contract: select scenario rail,
# then open the matching middle group OR stackup drawer OR footer sheet.
_DRAWER_PREFIXES: Tuple[str, ...] = (
    "stackup.",
    "antipad.",
    "plane.",
    "via_transition.padstack.antipad.",
    "via_transition.padstack.antipad_per_layer",
)
_SHEET_PREFIXES: Tuple[str, ...] = (
    "simulation.",
    "sweeps.",
    "sweep_execution.",
)
_PATH_GROUP_SUFFIXES: Tuple[str, ...] = (
    "mode", "route_axis", "entry_layer", "exit_layer",
    "drill_start_layer", "drill_end_layer",
    "pair_pitch", "signal_via_pitch", "center_x", "center_y",
)
_SIGNAL_PREFIXES: Tuple[str, ...] = (
    "via_transition.padstack.drill_diameter",
    "via_transition.padstack.connected_pad_diameter",
    "via_transition.padstack.antipad_diameter",
    "via_transition.padstack.finished_hole_diameter",
    "via_transition.padstack.plating_thickness",
    "via_transition.padstack.nonfunctional_pad_mode",
    "via_transition.padstack.nonfunctional_pad_diameter",
    "via_transition.padstack.barrel_model",
    "via_transition.padstack.material",
)
_BACKDRILL_PREFIX = "via_transition.backdrill."
_RETURN_PREFIX = "via_transition.return_vias."
_TRACE_PREFIXES: Tuple[str, ...] = (
    "via_transition.entry_trace.",
    "via_transition.exit_trace.",
    "via_transition.fanout.",
)


def navigation_target(path: str) -> Tuple[str, str]:
    """Map a parameter path to ``(kind, key)``.

    ``kind`` is one of ``group``, ``drawer``, ``sheet``, ``project``,
    ``capacitor``, ``gnd``, ``preview``, ``validate``.
    """
    if not path:
        return "validate", "overlay"
    if any(path.startswith(prefix) for prefix in _DRAWER_PREFIXES):
        return "drawer", "stackup"
    if any(path.startswith(prefix) for prefix in _SHEET_PREFIXES):
        return "sheet", "sweep"
    if path.startswith("project.") or path.startswith("trace."):
        return "project", "项目与走线"
    if path.startswith(("capacitor.", "capacitor_model.", "geometry.")):
        return "capacitor", "电容与模型"
    if path.startswith("gnd_vias."):
        return "gnd", "GND VIA"
    if path.startswith("via_transition.backdrill"):
        return "group", "backdrill"
    if path.startswith(_RETURN_PREFIX):
        return "group", "return"
    if any(path.startswith(prefix) for prefix in _TRACE_PREFIXES):
        return "group", "traces"
    if any(path == prefix or path.startswith(prefix) for prefix in _SIGNAL_PREFIXES):
        return "group", "signal"
    if path.startswith("via_transition."):
        leaf = path.split(".", 1)[-1]
        if leaf in _PATH_GROUP_SUFFIXES or any(leaf.startswith(item) for item in _PATH_GROUP_SUFFIXES):
            return "group", "path"
        return "group", "path"
    return "validate", "overlay"


def preview_click_path(source_path: str, tags: Iterable[str] = ()) -> str:
    """Resolve a preview click to the authoritative editor path."""
    tag_text = " ".join(str(item) for item in tags)
    if "pair_pitch" in tag_text or source_path.endswith("pair_pitch"):
        return "via_transition.pair_pitch"
    if "signal_via_pitch" in tag_text or source_path.endswith("signal_via_pitch"):
        return "via_transition.signal_via_pitch"
    if "via:antipad" in tag_text or "via-antipad" in tag_text:
        return "via_transition.padstack.antipad.diameter"
    if source_path.startswith("via_transition.padstack.antipad"):
        return "via_transition.padstack.antipad.diameter"
    if "backdrill" in source_path or "via:" in tag_text and "backdrill" in tag_text:
        return "via_transition.backdrill.residual_stub"
    return source_path


class SegmentedToggle(ttk.Frame):
    """Compact exclusive-choice control bound to an existing Tk variable."""

    def __init__(
        self,
        master: tk.Misc,
        variable: tk.Variable,
        choices: Sequence[Tuple[str, str]],
        *,
        command: Optional[Callable[[], None]] = None,
        width: int = 6,
    ) -> None:
        super().__init__(master)
        self.variable = variable
        self.choices = tuple(choices)
        self.command = command
        self.buttons: dict[str, ttk.Button] = {}
        for value, label in self.choices:
            button = ttk.Button(
                self, text=label, width=width,
                command=lambda backend=value: self._select(backend),
            )
            button.pack(side="left", padx=1)
            self.buttons[value] = button
        self._trace = variable.trace_add("write", lambda *_: self._sync())
        self._sync()

    def _select(self, value: str) -> None:
        current = str(self.variable.get())
        if current != str(value):
            self.variable.set(value)
        if self.command is not None:
            self.command()

    def _sync(self) -> None:
        current = str(self.variable.get())
        for value, button in self.buttons.items():
            try:
                button.state(["pressed"] if current == value else ["!pressed"])
            except tk.TclError:
                pass

    def destroy(self) -> None:
        try:
            self.variable.trace_remove("write", self._trace)
        except (tk.TclError, AttributeError, ValueError):
            pass
        super().destroy()


class CollapsibleGroup(ttk.LabelFrame):
    """One numbered middle-column group with expand/collapse chrome."""

    def __init__(
        self,
        master: tk.Misc,
        key: str,
        title: str,
        *,
        expanded: bool = True,
        on_toggle: Optional[Callable[["CollapsibleGroup"], None]] = None,
    ) -> None:
        super().__init__(master, text=title, padding=6)
        self.key = key
        self.title_text = title
        self.on_toggle = on_toggle
        self._expanded = bool(expanded)
        self.summary_var = tk.StringVar(value="")
        header = ttk.Frame(self)
        header.pack(fill="x")
        self.toggle_button = ttk.Button(
            header, text="▾" if self._expanded else "▸", width=3,
            command=self.toggle,
        )
        self.toggle_button.pack(side="left")
        ttk.Label(header, text=title, font="ViaCardTitleFont").pack(side="left", padx=(4, 8))
        self.summary_label = ttk.Label(
            header, textvariable=self.summary_var, foreground="#64748b")
        self.summary_label.pack(side="left", fill="x", expand=True)
        self.body = ttk.Frame(self)
        self.body.columnconfigure(1, weight=1)
        if self._expanded:
            self.body.pack(fill="x", pady=(4, 0))
        else:
            self._set_collapsed_summary()

    @property
    def expanded(self) -> bool:
        return self._expanded

    def expand(self) -> None:
        if self._expanded:
            return
        self._expanded = True
        self.toggle_button.configure(text="▾")
        self.summary_var.set("")
        self.body.pack(fill="x", pady=(4, 0))
        if self.on_toggle is not None:
            self.on_toggle(self)

    def collapse(self, summary: str = "未展开") -> None:
        if not self._expanded:
            self.summary_var.set(summary)
            return
        self._expanded = False
        self.toggle_button.configure(text="▸")
        self.body.pack_forget()
        self.summary_var.set(summary)
        if self.on_toggle is not None:
            self.on_toggle(self)

    def toggle(self) -> None:
        if self._expanded:
            self.collapse(self.summary_var.get() or "未展开")
        else:
            self.expand()

    def _set_collapsed_summary(self) -> None:
        if not self.summary_var.get():
            self.summary_var.set("未展开")


class NestedMoreSection(ttk.Frame):
    """Collapsed '更多' block inside the signal-via group."""

    def __init__(self, master: tk.Misc, title: str = "更多") -> None:
        super().__init__(master)
        self._expanded = False
        self.header = ttk.Frame(self)
        self.header.pack(fill="x")
        self.toggle_button = ttk.Button(
            self.header, text="▸ " + title, command=self.toggle)
        self.toggle_button.pack(side="left")
        self.body = ttk.Frame(self)
        self.body.columnconfigure(1, weight=1)

    @property
    def expanded(self) -> bool:
        return self._expanded

    def expand(self) -> None:
        if self._expanded:
            return
        self._expanded = True
        self.toggle_button.configure(text="▾ 更多")
        self.body.pack(fill="x", pady=(2, 0))

    def collapse(self) -> None:
        if not self._expanded:
            return
        self._expanded = False
        self.toggle_button.configure(text="▸ 更多")
        self.body.pack_forget()

    def toggle(self) -> None:
        if self._expanded:
            self.collapse()
        else:
            self.expand()


class PreviewViewBar(ttk.Frame):
    """俯视 / 等轴 / 截面(后续) / 后续 tabs plus the dirty badge."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        on_select: Callable[[str], None],
        dirty_var: tk.StringVar,
    ) -> None:
        super().__init__(master)
        self.on_select = on_select
        self.dirty_var = dirty_var
        self.active = "俯视"
        self.buttons: dict[str, ttk.Button] = {}
        specs = (
            ("俯视", True),
            ("等轴", True),
            ("截面", False),
            ("后续", False),
        )
        for name, enabled in specs:
            button = ttk.Button(
                self, text=name if enabled else f"{name} · 后续",
                command=(lambda value=name: self.select(value)) if enabled else None,
                state="normal" if enabled else "disabled",
            )
            button.pack(side="left", padx=2)
            self.buttons[name] = button
        ttk.Label(
            self, textvariable=self.dirty_var, foreground="#b45309",
            font="ViaSmallFont",
        ).pack(side="right", padx=6)
        self._sync()

    def select(self, name: str) -> None:
        if name not in self.buttons:
            return
        if str(self.buttons[name].cget("state")) == "disabled":
            return
        self.active = name
        self._sync()
        self.on_select(name)

    def _sync(self) -> None:
        for name, button in self.buttons.items():
            if str(button.cget("state")) == "disabled":
                continue
            try:
                button.state(["pressed"] if name == self.active else ["!pressed"])
            except tk.TclError:
                pass
