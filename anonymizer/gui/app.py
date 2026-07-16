from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog

from nicegui import app, run, ui

from .. import audit as audit_mod
from .. import config as config_mod
from .. import profiles as profiles_mod
from ..actions import reidentify_text
from ..core import build_preview
from ..engine import build_analyzer
from ..mapping import MappingStore
from ..models import FileJob
from ..pipeline import SUPPORTED_EXTENSIONS, ProcessingError, apply_document, scan_document, sniff_language
from . import review, settings_page, theme

_analyzer = None
_config = None
_analyzer_lock = threading.Lock()

# Native drop plumbing. The native 'drop' event fires OUTSIDE any client/UI
# context, so touching UI there (notify, adding rows) silently fails to reach
# the window -- the likely cause of "drag-drop does nothing". Instead the event
# just parks paths in a thread-safe buffer, and a ui.timer running INSIDE the
# page's client context drains it and updates the UI reliably.
_drop_lock = threading.Lock()
_pending_drops: list[str] = []
_drop_stats = {"events": 0, "empty_events": 0, "last_seen_empty": 0}


def _handle_native_drop(e) -> None:
    paths = [p for p in (e.args.get("files", []) if hasattr(e, "args") else []) if p]
    with _drop_lock:
        _drop_stats["events"] += 1
        if paths:
            _pending_drops.extend(paths)
        else:
            _drop_stats["empty_events"] += 1


app.native.on("drop", _handle_native_drop)


def _take_pending_drops() -> tuple[list[str], bool]:
    """Returns (new paths since last poll, whether an empty-path drop occurred)."""
    with _drop_lock:
        paths = _pending_drops[:]
        _pending_drops.clear()
        empty = _drop_stats["empty_events"] > _drop_stats["last_seen_empty"]
        _drop_stats["last_seen_empty"] = _drop_stats["empty_events"]
    return paths, empty


def drop_patch_status() -> bool:
    """True if the nicegui native-drop patch (which delivers real file paths) is
    applied. When False, native drag-drop cannot yield a path and the UI should
    say so and steer to Browse."""
    try:
        import nicegui.native.native_mode as native_mode

        return "_poll_dnd_state" in Path(native_mode.__file__).read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return False


def _ensure_analyzer():
    global _analyzer, _config
    with _analyzer_lock:
        if _analyzer is None:
            _config = config_mod.load_config()
            _analyzer = build_analyzer(_config)
    return _analyzer, _config


def warm_start() -> None:
    """Loads the models in the background at launch so the first scan doesn't
    stall (~10-20s of spaCy load happens while the user reads the UI)."""
    threading.Thread(target=_ensure_analyzer, daemon=True).start()


def _pick_files() -> list[str]:
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    paths = filedialog.askopenfilenames(
        title="Select document(s)",
        filetypes=[
            ("Documents", "*.docx *.doc *.xlsx *.xlsm *.xls *.pptx *.ppt *.pdf"),
            ("All files", "*.*"),
        ],
    )
    root.destroy()
    return list(paths)


class PageState:
    def __init__(self) -> None:
        self.jobs: list[FileJob] = []
        self.selected: int | None = None
        self.profile: str = profiles_mod.PROFILE_NAMES[0]
        self.language: str = "auto"  # "auto" | "de" | "en"

    @property
    def current(self) -> FileJob | None:
        if self.selected is None or self.selected >= len(self.jobs):
            return None
        return self.jobs[self.selected]


_STATUS_COLORS = {
    "pending": theme.SECONDARY,
    "scanning": theme.INFO,
    "review": theme.WARNING,
    "saving": theme.INFO,
    "done": theme.POSITIVE,
    "failed": theme.NEGATIVE,
}


@ui.page("/")
def main_page() -> None:
    theme.install()
    dark = ui.dark_mode(value=True)
    state = PageState()

    with ui.element("div").classes("az-header w-full"):
        with ui.row().classes("items-center justify-between px-6 py-3 w-full max-w-7xl mx-auto"):
            with ui.row().classes("items-center gap-3"):
                ui.icon("shield_lock", size="1.6rem").style(f"color:{theme.PRIMARY}")
                with ui.column().classes("gap-0"):
                    ui.label("Document Anonymizer").classes("az-h1")
                    ui.label("Local · offline · bank-grade PII redaction").classes("az-kicker")
            with ui.row().classes("items-center gap-1"):
                ui.button(icon="dark_mode", on_click=lambda: dark.toggle()).props("flat round dense")
                ui.button("Re-identify", icon="lock_open", on_click=lambda: ui.navigate.to("/reidentify")).props(
                    "flat dense"
                )
                ui.button("Settings", icon="settings", on_click=lambda: ui.navigate.to("/settings")).props(
                    "flat dense"
                )

    if not drop_patch_status():
        with ui.row().classes("w-full max-w-7xl mx-auto px-6 pt-3 -mb-1"):
            with ui.element("div").classes("az-card w-full").style(
                f"border-left:3px solid {theme.WARNING}; padding:12px 16px;"
            ):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("info", size="1.2rem").style(f"color:{theme.WARNING}")
                    ui.label(
                        "Drag-and-drop isn't active on this install (the NiceGUI drop patch isn't applied). "
                        "Use 'click to browse' to select files. Re-run setup to enable drag-and-drop."
                    ).classes("text-sm")

    with ui.row().classes("az-main w-full max-w-7xl mx-auto gap-4 px-6 py-4 items-start flex-nowrap"):
        # -- Left: intake + queue -------------------------------------------
        with ui.column().classes("az-rail gap-4").style("flex: 0 0 340px; max-width: 340px;"):
            _intake_panel(state)
            queue_container = ui.column().classes("w-full gap-2")

        # -- Right: review + save -------------------------------------------
        work_container = ui.column().classes("flex-grow gap-4 min-w-0")

    def refresh_queue() -> None:
        _render_queue(queue_container, state, select_job)
        _render_work(work_container, state)

    async def select_job(idx: int) -> None:
        state.selected = idx
        refresh_queue()

    async def add_files(paths: list[str]) -> None:
        added = 0
        for p in paths:
            path = Path(p)
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                ui.notify(f"Skipped unsupported file: {path.name}", type="warning")
                continue
            state.jobs.append(FileJob(path=str(path)))
            added += 1
        if added and state.selected is None:
            state.selected = len(state.jobs) - added
        refresh_queue()
        if added:
            await scan_all(state, refresh_queue)

    async def save_all() -> None:
        pending_review = [j for j in state.jobs if j.status == "review"]
        if not pending_review:
            ui.notify("No reviewed files ready to save.", type="info")
            return
        for job in pending_review:
            await _save_job(state, job)
        done = sum(1 for j in state.jobs if j.status == "done")
        failed = sum(1 for j in state.jobs if j.status == "failed")
        ui.notify(f"Save all finished: {done} saved, {failed} failed.", type="positive" if not failed else "warning")

    # store on state so intake buttons can reach them
    state.add_files = add_files  # type: ignore[attr-defined]
    state.refresh = refresh_queue  # type: ignore[attr-defined]
    state.save_all = save_all  # type: ignore[attr-defined]

    async def drain_drops() -> None:
        paths, empty = _take_pending_drops()
        if empty and not paths:
            ui.notify(
                "A file was dropped but Windows didn't hand over its path. Use 'click to browse' instead.",
                type="warning",
                timeout=6000,
            )
        if paths:
            await add_files(paths)

    # Native drop events land outside any client context; this timer runs INSIDE
    # the client, so draining here is what makes dropped files actually appear.
    ui.timer(0.3, drain_drops)

    refresh_queue()


def _intake_panel(state: PageState) -> None:
    with ui.element("div").classes("az-card w-full"):
        ui.label("Add documents").classes("az-h2 mb-1")

        with ui.column().classes("az-dropzone w-full items-center justify-center gap-1 p-6") as dz:
            ui.icon("cloud_upload", size="2rem").classes("az-muted")
            ui.label("Drag files here").classes("text-sm font-medium")
            ui.label("or click to browse").classes("az-muted text-xs")
            ui.label(".docx .doc .xlsx .xlsm .xls .pptx .ppt .pdf").classes("az-muted text-xs mt-1")

        with ui.row().classes("items-center gap-2 w-full mt-3"):
            ui.label("Profile").classes("az-muted text-xs w-16")
            prof = ui.select(profiles_mod.PROFILE_NAMES, value=state.profile).props("dense outlined").classes(
                "flex-grow"
            )
            prof.on_value_change(lambda e: setattr(state, "profile", e.value))
        with ui.row().classes("items-center gap-2 w-full mt-1"):
            ui.label("Language").classes("az-muted text-xs w-16")
            lang_labels = {"Auto-detect": "auto", "German": "de", "English": "en"}
            lang_sel = ui.select(list(lang_labels), value="Auto-detect").props("dense outlined").classes("flex-grow")
            lang_sel.on_value_change(lambda e: setattr(state, "language", lang_labels[e.value]))
        ui.label(
            "Auto-detect scans in the document's language (asks if unsure). Presets apply to files added next."
        ).classes("az-muted text-xs")

        async def browse() -> None:
            picked = await run.io_bound(_pick_files)
            if picked:
                await state.add_files(picked)  # type: ignore[attr-defined]

        def on_enter() -> None:
            dz.classes(add="az-drag")

        def on_leave() -> None:
            dz.classes(remove="az-drag")

        dz.on("click", browse)
        dz.on("dragenter.prevent", on_enter)
        dz.on("dragover.prevent", lambda: None)
        dz.on("dragleave.prevent", on_leave)
        dz.on("drop.prevent", on_leave)

        with ui.expansion("Enter a path manually").classes("w-full mt-2"):
            manual = ui.input(placeholder=r"C:\path\to\document.docx").props("dense outlined").classes("w-full")

            async def add_manual() -> None:
                val = (manual.value or "").strip().strip('"')
                if val:
                    manual.value = ""
                    await state.add_files([val])  # type: ignore[attr-defined]

            manual.on("keydown.enter", add_manual)
            ui.button("Add", on_click=add_manual).props("flat dense").classes("mt-1")


def _render_queue(container, state: PageState, select_job) -> None:
    container.clear()
    if not state.jobs:
        return
    with container:
        with ui.element("div").classes("az-card w-full"):
            with ui.row().classes("items-center justify-between w-full mb-1"):
                ui.label(f"Queue ({len(state.jobs)})").classes("az-h2")
                with ui.row().classes("items-center gap-1"):
                    review_ready = sum(1 for j in state.jobs if j.status == "review")
                    if review_ready:
                        ui.button(
                            f"Save all ({review_ready})",
                            icon="save",
                            on_click=lambda: state.save_all(),  # type: ignore[attr-defined]
                        ).props("dense color=primary").classes("text-xs")
                    ui.button("Clear", icon="clear_all", on_click=lambda: _clear(state)).props("flat dense").classes(
                        "text-xs"
                    )
            for i, job in enumerate(state.jobs):
                selected = i == state.selected
                border = f"border-left:3px solid {theme.PRIMARY};" if selected else "border-left:3px solid transparent;"
                with ui.row().classes("az-row items-center gap-2 w-full py-2 px-2 cursor-pointer").style(
                    border
                ).on("click", lambda i=i: select_job(i)):
                    ui.icon("description", size="1.1rem").classes("az-muted")
                    with ui.column().classes("gap-0 flex-grow min-w-0"):
                        ui.label(job.name).classes("text-sm truncate")
                        if job.status == "failed" and job.error:
                            ui.label(job.error).classes("text-xs truncate").style(f"color:{theme.NEGATIVE}")
                        elif job.status == "done":
                            ui.label("saved").classes("az-muted text-xs")
                    theme.chip(job.status, _STATUS_COLORS.get(job.status, theme.SECONDARY))


def _render_work(container, state: PageState) -> None:
    container.clear()
    job = state.current
    with container:
        if job is None:
            with ui.element("div").classes("az-card w-full items-center justify-center py-16"):
                ui.icon("policy", size="2.5rem").classes("az-muted")
                ui.label("Add a document to begin").classes("az-muted mt-2")
            return

        if job.status in ("pending", "scanning"):
            outstanding = sum(1 for j in state.jobs if j.status in ("pending", "scanning"))
            with ui.element("div").classes("az-card w-full"):
                ui.label(job.name).classes("az-h2")
                msg = "Scanning…" if job.status == "scanning" else "Waiting to scan…"
                if len(state.jobs) > 1:
                    msg += f"  ({outstanding} of {len(state.jobs)} files still to scan)"
                ui.label(msg).classes("az-muted text-sm")
                ui.linear_progress().props("indeterminate")
                ui.label("The first scan loads the language model — this can take 10–20 seconds.").classes(
                    "az-muted text-xs mt-1"
                )
            return

        if job.status == "failed":
            with ui.element("div").classes("az-card w-full"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("error", size="1.5rem").style(f"color:{theme.NEGATIVE}")
                    ui.label("Could not process this file").classes("az-h2")
                ui.label(job.error).classes("text-sm mt-1").style(f"color:{theme.NEGATIVE}")
                ui.label("No output was written — better no file than a falsely-clean one.").classes(
                    "az-muted text-xs mt-1"
                )
            return

        if job.status == "saving":
            with ui.element("div").classes("az-card w-full"):
                with ui.row().classes("items-center gap-2"):
                    ui.spinner(size="1.4rem")
                    ui.label("Applying redactions & verifying…").classes("az-h2")
                ui.label(
                    "Re-scanning the output to guarantee no residual PII before the file is written."
                ).classes("az-muted text-sm mt-1")
            return

        if job.status == "done":
            with ui.element("div").classes("az-card w-full"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("verified", size="1.5rem").style(f"color:{theme.POSITIVE}")
                    ui.label("Anonymized & verified").classes("az-h2")
                ui.label(f"Saved: {job.out_path}").classes("az-mono text-xs mt-1 truncate")
                ui.label(f"Audit: {job.report_path}").classes("az-mono text-xs truncate")
                ui.label("Output re-scanned — no residual PII of removed categories.").classes(
                    "az-muted text-xs mt-1"
                )
            return

        # status == review
        review_box = ui.column().classes("w-full gap-3")

        def on_change() -> None:
            pass  # decisions mutate in place; preview reads them live

        review.render_review(review_box, job.scan, on_change)

        with ui.row().classes("w-full justify-end gap-2 mt-1"):
            ui.button("Preview changes", icon="visibility", on_click=lambda: _preview_dialog(job)).props("flat")
            ui.button("Save anonymized copy", icon="save", on_click=lambda: _save_job(state, job)).props(
                "color=primary"
            )


def _preview_dialog(job: FileJob) -> None:
    groups = build_preview(job.scan.groups)
    with ui.dialog() as dialog, ui.element("div").classes("az-card").style("max-width:720px;width:92vw"):
        ui.label("Preview — what Save will change").classes("az-h2")
        if not groups:
            ui.label("Nothing selected for redaction (all set to skip).").classes("az-muted text-sm mt-2")
        else:
            with ui.column().classes("w-full gap-3 az-scroll mt-2"):
                for pg in groups:
                    ui.label(pg.display).classes("az-kicker mt-1")
                    for r in pg.rows:
                        with ui.row().classes("az-row items-center gap-2 w-full py-1"):
                            ui.label(r.value[:60]).classes("az-mono text-sm flex-grow truncate")
                            ui.icon("arrow_forward", size="1rem").classes("az-muted")
                            theme.chip(r.token, theme.ACTION_COLORS.get(r.action, theme.SECONDARY))
        with ui.row().classes("w-full justify-end mt-3"):
            ui.button("Close", on_click=dialog.close).props("flat")
    dialog.open()


async def _ask_language(n: int) -> str:
    with ui.dialog() as dlg, ui.element("div").classes("az-card").style("max-width:420px"):
        ui.label("Which language?").classes("az-h2")
        ui.label(
            f"Couldn't confidently detect the language of {n} file(s). Scan them with which model?"
        ).classes("az-muted text-sm my-2")
        with ui.row().classes("gap-2 justify-end w-full"):
            ui.button("English", on_click=lambda: dlg.submit("en")).props("flat")
            ui.button("German", on_click=lambda: dlg.submit("de")).props("color=primary")
    result = await dlg
    return result or "de"


async def scan_all(state: PageState, refresh) -> None:
    analyzer, config = await run.io_bound(_ensure_analyzer)
    effective = profiles_mod.apply_profile(config, state.profile)
    pending = [j for j in state.jobs if j.status == "pending"]

    # Resolve the scan language per file first (single-language scan is the fix
    # for ordinary German words being flagged as names).
    uncertain: list[FileJob] = []
    for job in pending:
        if state.language in ("de", "en"):
            lang = state.language
        else:
            lang, confident = await run.io_bound(sniff_language, Path(job.path), effective)
            if not confident:
                uncertain.append(job)
                continue
        job.config = {**effective, "languages": [lang]}
    if uncertain:
        chosen = await _ask_language(len(uncertain))
        for job in uncertain:
            job.config = {**effective, "languages": [chosen]}

    for job in pending:
        job.status = "scanning"
        refresh()
        try:
            job.scan = await run.io_bound(scan_document, Path(job.path), analyzer, job.config)
            job.status = "review"
        except ProcessingError as exc:
            job.status = "failed"
            job.error = str(exc)
        except Exception as exc:  # noqa: BLE001
            job.status = "failed"
            job.error = f"Unexpected error: {exc}"
        refresh()


async def _save_job(state: PageState, job: FileJob) -> None:
    analyzer, base = await run.io_bound(_ensure_analyzer)
    config = job.config or base  # same config the file was scanned with (parity)
    grouped = job.scan.all_actionable()
    job.status = "saving"
    state.refresh()  # type: ignore[attr-defined]
    try:
        out_path, report_path = await run.io_bound(
            apply_document, Path(job.path), grouped, analyzer, config, None
        )
        job.out_path, job.report_path = str(out_path), str(report_path)
        job.status = "done"
        ui.notify(f"Saved & verified: {out_path.name}", type="positive")
    except ProcessingError as exc:
        job.status = "failed"
        job.error = str(exc)
        ui.notify(str(exc), type="negative", timeout=8000)
    except Exception as exc:  # noqa: BLE001
        job.status = "failed"
        job.error = f"Unexpected error: {exc}"
        ui.notify(job.error, type="negative", timeout=8000)
    state.refresh()  # type: ignore[attr-defined]


def _clear(state: PageState) -> None:
    state.jobs.clear()
    state.selected = None
    state.refresh()  # type: ignore[attr-defined]


@ui.page("/settings")
def settings_page_route() -> None:
    theme.install()
    ui.dark_mode(value=True)
    with ui.element("div").classes("az-header w-full"):
        with ui.row().classes("items-center justify-between px-6 py-3 w-full max-w-5xl mx-auto"):
            ui.label("Settings").classes("az-h1")
            ui.button("Back", icon="arrow_back", on_click=lambda: ui.navigate.to("/")).props("flat dense")
    with ui.column().classes("w-full max-w-5xl mx-auto gap-4 p-4"):
        settings_page.build()


@ui.page("/reidentify")
def reidentify_route() -> None:
    theme.install()
    ui.dark_mode(value=True)
    with ui.element("div").classes("az-header w-full"):
        with ui.row().classes("items-center justify-between px-6 py-3 w-full max-w-4xl mx-auto"):
            with ui.column().classes("gap-0"):
                ui.label("Re-identify").classes("az-h1")
                ui.label("Restore original values in AI output — audit-logged").classes("az-kicker")
            ui.button("Back", icon="arrow_back", on_click=lambda: ui.navigate.to("/")).props("flat dense")

    with ui.column().classes("w-full max-w-4xl mx-auto gap-4 p-4"):
        with ui.element("div").classes("az-card w-full"):
            ui.label("Paste text containing placeholder tokens").classes("az-h2")
            ui.label(
                "Tokens like [PERSON_1] or [IBAN_3] are mapped back to their originals. One-way anonymized "
                "tokens and unknown tokens are left as-is. This reverses anonymization — every un-mask is "
                "written to the audit log."
            ).classes("az-muted text-xs mb-2")
            source = ui.textarea(placeholder="e.g. The advisor spoke with [PERSON_1] about account [IBAN_2].").props(
                "outlined"
            ).classes("w-full az-mono")
            result_box = ui.textarea(label="Restored text").props("outlined readonly").classes("w-full az-mono mt-2")
            result_box.visible = False

            def do_reidentify() -> None:
                text = source.value or ""
                if not text.strip():
                    return

                def run_it() -> None:
                    dlg.close()
                    with MappingStore() as store:
                        restored, n = reidentify_text(text, store)
                    audit_mod.log_event("reidentify", f"{n} token(s) un-masked")
                    result_box.value = restored
                    result_box.visible = True
                    ui.notify(f"Restored {n} value(s).", type="positive" if n else "info")

                with ui.dialog() as dlg, ui.element("div").classes("az-card").style("max-width:460px"):
                    ui.label("Re-identify this text?").classes("az-h2")
                    ui.label(
                        "This reveals real personal data behind the placeholders and records the action in the "
                        "audit log."
                    ).classes("az-muted text-sm my-2")
                    with ui.row().classes("w-full justify-end gap-2"):
                        ui.button("Cancel", on_click=dlg.close).props("flat")
                        ui.button("Reveal", on_click=run_it).props("color=primary")
                dlg.open()

            ui.button("Re-identify", icon="lock_open", on_click=do_reidentify).props("color=primary").classes("mt-2")

        with ui.expansion("Recent audit log").classes("w-full"):
            entries = audit_mod.read_recent(30)
            if not entries:
                ui.label("No audited actions yet.").classes("az-muted text-xs")
            for line in entries:
                ui.label(line).classes("az-mono text-xs")


def main() -> None:
    warm_start()
    ui.run(title="Document Anonymizer", reload=False, native=True, window_size=(1400, 950))


if __name__ in {"__main__", "__mp_main__"}:
    main()
