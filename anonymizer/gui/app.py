from __future__ import annotations

from pathlib import Path

from nicegui import ui

from .. import config as config_mod
from ..engine import build_analyzer
from ..pipeline import SUPPORTED_EXTENSIONS, apply_document, scan_document
from . import settings_page

_analyzer = None
_config = None


def _ensure_analyzer():
    global _analyzer, _config
    if _analyzer is None:
        _config = config_mod.load_config()
        _analyzer = build_analyzer(_config)
    return _analyzer, _config


class PageState:
    def __init__(self) -> None:
        self.path: Path | None = None
        self.grouped: list = []


def _render_review(container, state: PageState) -> None:
    container.clear()
    with container:
        if not state.grouped:
            ui.label("No entities detected.")
            return
        ui.label(f"{len(state.grouped)} distinct findings — review before saving:").classes("font-bold")
        for g in state.grouped:
            with ui.row().classes("items-center gap-4 w-full border-b pb-1"):
                ui.label(g.entity_type).classes("w-40 font-mono text-xs")
                ui.label(g.context).classes("flex-grow text-sm")
                ui.label(f"x{g.count}").classes("w-10")
                ui.label(f"{g.max_score:.2f}").classes("w-14")
                ui.select(["pseudonymize", "anonymize", "skip"], value=g.action).bind_value(g, "action").classes(
                    "w-40"
                )


@ui.page("/")
def main_page() -> None:
    state = PageState()

    ui.label("Document Anonymizer").classes("text-2xl font-bold")
    ui.link("Settings", "/settings")

    path_input = ui.input(label="File path", placeholder=r"C:\path\to\document.docx").classes("w-full")
    review_container = ui.column().classes("w-full gap-2")
    result_label = ui.label()

    def do_scan() -> None:
        result_label.text = ""
        raw_path = path_input.value.strip().strip('"')
        if not raw_path:
            ui.notify("Enter a file path", type="warning")
            return
        path = Path(raw_path)
        if not path.exists():
            ui.notify("File not found", type="negative")
            return
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            ui.notify(f"Unsupported file type: {path.suffix}", type="negative")
            return
        analyzer, config = _ensure_analyzer()
        ui.notify("Scanning...", type="info")
        state.path = path
        state.grouped = scan_document(path, analyzer, config)
        _render_review(review_container, state)

    def do_save() -> None:
        if not state.path or not state.grouped:
            ui.notify("Scan a file first", type="warning")
            return
        analyzer, config = _ensure_analyzer()
        out_path, report_path = apply_document(state.path, state.grouped, analyzer, config)
        result_label.text = f"Saved: {out_path}   |   Report: {report_path}"
        ui.notify("Saved anonymized copy", type="positive")

    with ui.row():
        ui.button("Scan", on_click=do_scan)
        ui.button("Save _psd", on_click=do_save)


@ui.page("/settings")
def settings_page_route() -> None:
    ui.label("Settings").classes("text-2xl font-bold")
    ui.link("Back", "/")
    settings_page.build(None)


def main() -> None:
    ui.run(title="Document Anonymizer", reload=False, show=True)


if __name__ in {"__main__", "__mp_main__"}:
    main()
