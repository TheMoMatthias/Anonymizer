from __future__ import annotations

import asyncio
import tkinter as tk
from pathlib import Path
from tkinter import filedialog

from nicegui import app, run, ui

from .. import config as config_mod
from ..engine import build_analyzer
from ..pipeline import SUPPORTED_EXTENSIONS, apply_document, scan_document
from . import settings_page

_analyzer = None
_config = None

ACTIONS = ["pseudonymize", "anonymize", "skip"]

# Native mode has exactly one window, so a single set of refs to the
# currently-rendered page's file-selection widgets is enough -- the native
# 'drop' event is a window-level event (not tied to a specific page/client),
# dispatched safely onto the main asyncio loop by NiceGUI itself. The real
# fix for getting a usable path out of the drop event lives in
# scripts/patch_nicegui_drop.py (patches the installed nicegui package
# in-place) -- a runtime monkeypatch here was verified NOT to reach the
# separate process NiceGUI spawns for the native window.
_active_refs: dict = {}


def _handle_native_drop(e) -> None:
    paths = [p for p in e.args.get("files", []) if p]
    if not paths:
        ui.notify("Drop received but no path could be resolved.", type="warning")
        return
    if len(paths) > 1:
        ui.notify("Only one file at a time is supported -- using the first one.", type="warning")
    handler = _active_refs.get("on_file_selected")
    if handler:
        asyncio.create_task(handler(paths[0]))


app.native.on("drop", _handle_native_drop)


def _ensure_analyzer():
    global _analyzer, _config
    if _analyzer is None:
        _config = config_mod.load_config()
        _analyzer = build_analyzer(_config)
    return _analyzer, _config


def _pick_file() -> str:
    # Browsers can't expose a dropped file's real folder path, and the _psd
    # output must land next to the source -- a native dialog is the only way
    # to get a real absolute path while staying in the browser-based UI.
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askopenfilename(
        title="Select a document",
        filetypes=[("Documents", "*.docx *.doc *.xlsx *.xlsm *.xls *.pptx *.ppt *.pdf"), ("All files", "*.*")],
    )
    root.destroy()
    return path


class PageState:
    def __init__(self) -> None:
        self.path: Path | None = None
        self.grouped: list = []
        self.min_score: float = 0.0


def _render_group_body(items: list) -> None:
    with ui.row().classes("items-center gap-4 w-full"):
        ui.label("Bulk action for this group:").classes("text-xs text-gray-500")

        def set_all(e, items=items) -> None:
            for g in items:
                g.action = e.value

        ui.select(ACTIONS, on_change=set_all).classes("w-40").props("dense outlined")

    for g in items:
        with ui.row().classes("items-center gap-4 w-full pl-2 border-t pt-1"):
            ui.label(g.value[:60]).classes("flex-grow text-sm font-mono")
            ui.label(g.context).classes("flex-grow text-xs text-gray-500")
            ui.label(f"x{g.count}").classes("w-10 text-xs")
            ui.label(f"{g.max_score:.2f}").classes("w-14 text-xs")
            ui.select(ACTIONS, value=g.action).bind_value(g, "action").classes("w-36").props("dense outlined")


def _render_review(container, state: PageState) -> None:
    container.clear()
    with container:
        if not state.grouped:
            ui.label("No entities detected yet -- scan a document above.").classes("text-gray-500")
            return

        visible = [g for g in state.grouped if g.max_score >= state.min_score]
        ui.label(f"{len(visible)} of {len(state.grouped)} distinct findings shown, grouped by type:").classes(
            "text-sm text-gray-500"
        )

        groups: dict[str, list] = {}
        for g in visible:
            groups.setdefault(g.entity_type, []).append(g)

        for entity_type in sorted(groups, key=lambda t: -len(groups[t])):
            items = groups[entity_type]
            with ui.expansion(f"{entity_type}  ({len(items)})").classes("w-full border rounded"):
                _render_group_body(items)


@ui.page("/")
def main_page() -> None:
    state = PageState()

    with ui.header().classes("items-center justify-between px-4"):
        ui.label("Document Anonymizer").classes("text-xl font-bold")
        ui.link("Settings", "/settings").classes("text-white")

    with ui.column().classes("w-full max-w-4xl mx-auto gap-4 p-4"):
        with ui.card().classes("w-full"):
            ui.label("1. Choose a document").classes("font-bold")

            with ui.column().classes(
                "w-full items-center justify-center gap-1 border-2 border-dashed border-gray-300 rounded-lg p-8 "
                "cursor-pointer hover:bg-gray-100 transition-colors"
            ) as drop_zone:
                ui.icon("upload_file").classes("text-4xl text-gray-400")
                selected_label = ui.label("Drag a file here, or click to select a document").classes(
                    "text-gray-700 font-medium"
                )
                ui.label(".docx  .doc  .xlsx  .xlsm  .xls  .pptx  .ppt  .pdf").classes("text-xs text-gray-400")

            selection_progress = ui.linear_progress().props("indeterminate").classes("w-full")
            selection_progress.visible = False

            with ui.expansion("Enter a path manually instead").classes("w-full"):
                path_input = ui.input(label="File path", placeholder=r"C:\path\to\document.docx").classes("w-full")

            scan_button = ui.button("Scan").props("color=primary").classes("w-full mt-2")

        with ui.card().classes("w-full") as review_card:
            ui.label("2. Review detected entities").classes("font-bold")
            with ui.row().classes("items-center gap-4"):
                score_filter = ui.slider(min=0.0, max=1.0, step=0.05, value=0.0).classes("w-64")
                filter_label = ui.label("min confidence: 0.00").classes("text-xs text-gray-500")
            review_container = ui.column().classes("w-full gap-1")
            ui.label("Nothing scanned yet.").classes("text-gray-500")

        with ui.card().classes("w-full"):
            ui.label("3. Save").classes("font-bold")
            save_button = ui.button("Save _psd").props("color=primary")
            result_label = ui.label()

    async def on_file_selected(raw_path: str) -> None:
        path_input.value = raw_path
        selected_label.text = raw_path
        selection_progress.visible = True
        await asyncio.sleep(0.4)  # brief, deliberate flash so selection feels acknowledged
        selection_progress.visible = False

    _active_refs["on_file_selected"] = on_file_selected

    async def browse() -> None:
        picked = await run.io_bound(_pick_file)
        if picked:
            await on_file_selected(picked)

    def on_manual_path_change(e) -> None:
        selected_label.text = e.value or "Drag a file here, or click to select a document"

    def on_drag_enter() -> None:
        drop_zone.classes(remove="border-gray-300", add="border-blue-400 bg-blue-50")

    def on_drag_leave() -> None:
        drop_zone.classes(remove="border-blue-400 bg-blue-50", add="border-gray-300")

    async def do_scan() -> None:
        result_label.text = ""
        raw_path = path_input.value.strip().strip('"')
        if not raw_path:
            ui.notify("Enter or browse to a file path", type="warning")
            return
        path = Path(raw_path)
        if not path.exists():
            ui.notify("File not found", type="negative")
            return
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            ui.notify(f"Unsupported file type: {path.suffix}", type="negative")
            return

        first_load = _analyzer is None
        scan_button.disable()
        if first_load:
            ui.notify("Loading language models (first run only, ~10-20s)...", type="info", timeout=0)
        else:
            ui.notify("Scanning...", type="info")
        try:
            analyzer, config = await run.io_bound(_ensure_analyzer)
            state.path = path
            state.min_score = 0.0
            score_filter.value = 0.0
            state.grouped = await run.io_bound(scan_document, path, analyzer, config)
            _render_review(review_container, state)
        finally:
            scan_button.enable()

    def on_filter_change(e) -> None:
        state.min_score = e.value
        filter_label.text = f"min confidence: {e.value:.2f}"
        _render_review(review_container, state)

    def do_save() -> None:
        if not state.path or not state.grouped:
            ui.notify("Scan a file first", type="warning")
            return
        analyzer, config = _ensure_analyzer()
        out_path, report_path = apply_document(state.path, state.grouped, analyzer, config)
        result_label.text = f"Saved: {out_path}   |   Report: {report_path}"
        ui.notify("Saved anonymized copy", type="positive")

    drop_zone.on("click", browse)
    drop_zone.on("dragenter.prevent", on_drag_enter)
    drop_zone.on("dragover.prevent", lambda: None)
    drop_zone.on("dragleave.prevent", on_drag_leave)
    drop_zone.on("drop.prevent", on_drag_leave)
    path_input.on_value_change(on_manual_path_change)
    scan_button.on_click(do_scan)
    score_filter.on_value_change(on_filter_change)
    save_button.on_click(do_save)


@ui.page("/settings")
def settings_page_route() -> None:
    with ui.header().classes("items-center justify-between px-4"):
        ui.label("Settings").classes("text-xl font-bold")
        ui.link("Back", "/").classes("text-white")
    with ui.column().classes("w-full max-w-4xl mx-auto gap-4 p-4"):
        settings_page.build()


def main() -> None:
    ui.run(title="Document Anonymizer", reload=False, native=True, window_size=(1300, 900))


if __name__ in {"__main__", "__mp_main__"}:
    main()
