from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from main_gui import SurfaceCapAntipadApp  # noqa: E402


PAGES = {
    "gnd_via": "GND VIA",
    "via_transition": "VIA 换层",
    "antipad": "反焊盘与叠层",
    "capacitor": "电容与模型",
    "model": "模型预览",
}


def _align_widget_bottom(scroller, widget) -> None:
    """Place a tall target's bottom at the bottom of its real outer viewport."""
    scroller.update_idletasks()
    viewport = max(1, scroller.canvas.winfo_height())
    content_height = max(1, scroller.content.winfo_reqheight())
    target_bottom = (
        widget.winfo_rooty() - scroller.content.winfo_rooty()
        + max(widget.winfo_height(), widget.winfo_reqheight())
    )
    scroller.canvas.yview_moveto(max(0.0, min(1.0, (target_bottom - viewport + 4) / content_height)))


def _parse_size(value: str) -> tuple[int, int]:
    try:
        width, height = (int(part) for part in value.lower().split("x", 1))
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("size must be WIDTHxHEIGHT") from exc
    if width < 640 or height < 480:
        raise argparse.ArgumentTypeError("acceptance viewport must be at least 640x480")
    return width, height


def main() -> int:
    parser = argparse.ArgumentParser(description="Open a deterministic GUI visual-acceptance scene.")
    parser.add_argument("--size", type=_parse_size, required=True)
    parser.add_argument("--page", choices=tuple(PAGES), required=True)
    parser.add_argument("--scenario", choices=("surface", "via"), default="surface")
    parser.add_argument("--tier", choices=("auto", "normal", "large", "ultra"), default="auto")
    parser.add_argument("--capture", type=Path, help="Capture the real Tk client area as a PNG, then exit.")
    args = parser.parse_args()

    width, height = args.size
    app = SurfaceCapAntipadApp()
    app.minsize(1, 1)
    app.geometry(f"{width}x{height}+0+0")
    app.title(f"V2.4.0 VISUAL ACCEPTANCE {width}x{height} {args.page} PREPARING")

    def prepare() -> None:
        if args.scenario == "via":
            app.variables["scenario.type"].set("via_transition")
            app.update_idletasks()
            app.update()
        app.variables["simulation.enabled"].set(True)
        app.variables["gnd_vias.enabled"].set(True)
        app.refresh_visuals()
        app.notebook.select(app.tabs[PAGES[args.page]])
        if args.tier == "auto":
            app.set_responsive_font_tier(None)
        else:
            app.set_responsive_font_tier(args.tier)
        app.update_idletasks()

        if args.page == "gnd_via":
            app.gnd_via_workbench.preview.redraw()
            app.gnd_via_workbench.cross_section.redraw()
            app.update_idletasks()
            _align_widget_bottom(
                app.gnd_via_workbench.left_scroll,
                app.gnd_via_workbench.preview,
            )
            _align_widget_bottom(
                app.gnd_via_workbench.right_scroll,
                app.gnd_via_workbench.cross_section,
            )
        elif args.page == "antipad":
            app.antipad_workbench.select_layer(3 if args.scenario == "via" else 28)
            app.antipad_workbench.cross_section.canvas.yview_moveto(1.0)
        elif args.page == "via_transition":
            app.via_page_scroller.canvas.yview_moveto(0.0)
            app._redraw_via_diagrams()
        elif args.page == "capacitor":
            app.cap_page_scroller.canvas.yview_moveto(0.0)
            app.cap_geometry_canvas._redraw_last()
            app.cap_geometry_canvas.yview_moveto(0.0)
        elif args.page == "model":
            app.model_preview_workbench.set_view("等轴测")
            app.model_preview_workbench.fit_model()
            app.model_preview_workbench._software_redraw()

        app.update_idletasks()
        actual = f"{app.winfo_width()}x{app.winfo_height()}"
        tier = app._responsive_font_tier
        app.title(f"V2.4.0 VISUAL ACCEPTANCE {width}x{height} {args.scenario}/{args.page} READY actual={actual} tier={tier}")
        app.lift()
        app.focus_force()

        if args.capture:
            def capture() -> None:
                from PIL import ImageGrab

                if args.page == "gnd_via":
                    _align_widget_bottom(app.gnd_via_workbench.left_scroll, app.gnd_via_workbench.preview)
                    _align_widget_bottom(app.gnd_via_workbench.right_scroll, app.gnd_via_workbench.cross_section)
                elif args.page == "antipad":
                    app.antipad_workbench.cross_section.canvas.yview_moveto(1.0)
                elif args.page == "via_transition":
                    app.via_page_scroller.canvas.yview_moveto(0.0)
                    app._redraw_via_diagrams()
                elif args.page == "capacitor":
                    app.cap_geometry_canvas.yview_moveto(0.0)
                app.update_idletasks()
                app.update()
                app.update_idletasks()
                actual_width, actual_height = app.winfo_width(), app.winfo_height()
                target = args.capture.expanduser().resolve()
                target.parent.mkdir(parents=True, exist_ok=True)
                # Passing the HWND asks Pillow to capture this window directly
                # (PrintWindow), so evidence is deterministic even when the
                # verification process does not own the foreground desktop.
                ImageGrab.grab(window=app.winfo_id()).save(target, "PNG")
                capture_details = {}
                if args.page == "gnd_via":
                    scroller = app.gnd_via_workbench.right_scroll
                    widget = app.gnd_via_workbench.cross_section
                    capture_details["right_scroll"] = {
                        "yview": list(scroller.canvas.yview()),
                        "viewport_height": scroller.canvas.winfo_height(),
                        "content_height": scroller.content.winfo_reqheight(),
                        "cross_y": widget.winfo_rooty() - scroller.content.winfo_rooty(),
                        "cross_height": widget.winfo_height(),
                        "cross_request_height": widget.winfo_reqheight(),
                    }
                elif args.page == "capacitor":
                    canvas = app.cap_geometry_canvas
                    capture_details["semantic_labels"] = [
                        {
                            "text": canvas.itemcget(item, "text"),
                            "tags": list(canvas.gettags(item)),
                            "bbox": list(canvas.bbox(item) or ()),
                        }
                        for item in canvas.find_all()
                        if canvas.type(item) == "text"
                        and {"dimension-label", "axis-label", "object-label"}.intersection(canvas.gettags(item))
                    ]
                print(json.dumps({
                    "path": str(target),
                    "scenario": args.scenario,
                    "target_size": [width, height],
                    "actual_size": [actual_width, actual_height],
                    "page": args.page,
                    "font_tier": tier,
                    **capture_details,
                }, ensure_ascii=False), flush=True)
                app.destroy()

            app.after(350, capture)

    app.after(600, prepare)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
