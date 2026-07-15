from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog

from nicegui import run, ui

from .. import config as config_mod
from ..engine import build_analyzer
from ..pipeline import SUPPORTED_EXTENSIONS, apply_document, scan_document
from . import settings_page

_analyzer = None
_config = None

ACTIONS = ["pseudonymize", "anonymize", "skip"]


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
            with ui.row().classes("items-center gap-2 w-full"):
                path_input = ui.input(label="File path", placeholder=r"C:\path\to\document.docx").classes(
                    "flex-grow"
                )
                browse_button = ui.button("Browse...")
                scan_button = ui.button("Scan").props("color=primary")

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

    async def browse() -> None:
        picked = await run.io_bound(_pick_file)
        if picked:
            path_input.value = picked

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

    browse_button.on_click(browse)
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
