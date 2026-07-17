from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path

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

# File intake uses NiceGUI's built-in ui.upload -- an in-page dropzone that
# accepts BOTH drag-and-drop and click-to-browse, served over HTTP inside the
# same webview. This replaced a fragile native-OS-drop scheme that monkeypatched
# an installed dependency file (any `uv sync` silently reverted it) and relied on
# a 4-hop WebView2->poller->subprocess->timer chain. See
# docs/run_dragdrop-uiupload_2026-07-17.md.
#
# A browser security boundary means a dropped file's real path is never exposed
# to the page, so upload delivers file BYTES. We write them to a managed temp
# working copy under the file's ORIGINAL name (so tokens, the report, and the
# output filename stay correct) and feed that path into the normal scan pipeline.
_upload_dir: Path | None = None
_work_dir_lock = threading.Lock()  # _persist_upload runs on worker threads; guard lazy init
_work_dir_swept = False  # sweep crash-leftovers at most ONCE per process

# Writing to any of these basenames (in ANY directory) hits a legacy Windows DOS
# device, not a file: NUL silently discards the bytes (-> a degenerate "clean"
# file), COM1/LPT1 can BLOCK on a serial/printer device. Reject such uploads.
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def _work_root() -> Path:
    # App-owned (%LOCALAPPDATA%\Anonymizer\work), NOT the system temp: working
    # copies hold RAW client PII, and %TEMP% is essentially never swept, so a
    # crash would leave unredacted documents there indefinitely.
    return config_mod.app_data_dir() / "work"


def _work_dir() -> Path:
    """Lazily-created dir holding working copies of uploaded files, under the app's
    own data dir (never the system temp). On first use it also sweeps any working
    dirs a previously crashed session left behind, so raw-PII copies never
    accumulate across runs.

    Thread-safe: _persist_upload runs on worker threads (run.io_bound), and a
    multi-file first-drop fires several concurrently. Without the lock two threads
    could both lazy-init, and one's `upload_*` sweep would delete the dir the other
    just created (and wrote PII into). The lock double-checks the dir, and the
    crash-leftover sweep runs at most once per process so it can never delete a
    concurrently-created sibling."""
    global _upload_dir, _work_dir_swept
    with _work_dir_lock:
        if _upload_dir is None or not _upload_dir.exists():
            root = _work_root()
            root.mkdir(parents=True, exist_ok=True)
            if not _work_dir_swept:
                for stale in root.glob("upload_*"):  # leftovers from a crash (no clean shutdown)
                    shutil.rmtree(stale, ignore_errors=True)
                _work_dir_swept = True
            _upload_dir = Path(tempfile.mkdtemp(prefix="upload_", dir=root))
        return _upload_dir


def _cleanup_work_dir() -> None:
    global _upload_dir
    if _upload_dir is not None:
        shutil.rmtree(_upload_dir, ignore_errors=True)
        _upload_dir = None


def _discard_working_copy(job: FileJob) -> None:
    """Remove a job's uploaded working copy (raw PII) as soon as it is terminal,
    rather than holding every upload's plaintext on disk until shutdown. Only
    touches files under the managed work dir, never a user's real source file."""
    try:
        p = Path(job.path)
        if _upload_dir is not None and _upload_dir in p.parents:
            p.unlink(missing_ok=True)
    except OSError:
        pass


def _persist_upload(name: str, data: bytes) -> Path | None:
    """Write uploaded bytes to a working copy under the ORIGINAL filename, or
    return None if the name is unusable (unsupported extension, empty, a reserved
    Windows device name) OR the write fails -- the caller surfaces None to the
    user, so a failed upload is never silent. Only the basename is used and Windows
    device/ADS hazards are stripped, so an uploaded name can neither escape the
    work dir nor hit a device. Uniquifies same-named uploads within a session."""
    safe = Path(name.replace("\\", "/")).name
    safe = safe.split(":", 1)[0].rstrip(". ")  # drop NTFS ADS ("f:evil") + trailing dots/spaces
    if not safe or Path(safe).suffix.lower() not in SUPPORTED_EXTENSIONS:
        return None
    stem, suffix = Path(safe).stem, Path(safe).suffix
    if stem.lower() in _WINDOWS_RESERVED:  # NUL/CON/COM1... would hit a device, not a file
        return None
    target = _work_dir() / safe
    n = 2
    while target.exists():
        target = _work_dir() / f"{stem} ({n}){suffix}"
        n += 1
    try:
        target.write_bytes(data)
    except OSError:
        return None
    return target


def _documents_dir() -> Path:
    """The user's REAL Documents folder. On managed/bank Windows with OneDrive
    Known-Folder redirection, that is under the OneDrive path, not ~/Documents --
    so a naive Path.home()/Documents would write where Explorer shows nothing."""
    if os.name == "nt":
        try:
            import winreg

            key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                val, _ = winreg.QueryValueEx(k, "Personal")
            resolved = Path(os.path.expandvars(val))
            if resolved.is_absolute():
                return resolved
        except OSError:
            pass
    return Path.home() / "Documents"


def anonymized_dir() -> Path:
    """The fixed folder every anonymized copy is written to. Dropped/uploaded
    files have no origin folder, so a single predictable destination beats a copy
    landing next to a temp working file the user can never find."""
    return _documents_dir() / "Anonymized"


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


class PageState:
    def __init__(self) -> None:
        self.jobs: list[FileJob] = []
        self.selected: int | None = None
        self.profile: str = profiles_mod.PROFILE_NAMES[0]
        self.language: str = "auto"  # "auto" | "de" | "en"
        # Wired up by main_page()/_intake_panel so the nested render helpers can
        # reach them; declared here so PageState's real interface is visible.
        self.add_files: Callable[[list[str]], Awaitable[None]] | None = None
        self.refresh: Callable[[], None] | None = None
        self.save_all: Callable[[], Awaitable[None]] | None = None
        self.upload: object | None = None  # the ui.upload widget

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

    refresh_queue()


def _intake_panel(state: PageState) -> None:
    with ui.element("div").classes("az-card w-full"):
        ui.label("Add documents").classes("az-h2 mb-1")

        async def handle_upload(e) -> None:
            # ui.upload fires this once per file. `e.content` is a file-like of the
            # uploaded bytes. The read AND the disk-write can be large/slow (up to
            # the 100 MB cap), so both are kept off the event loop; ANY failure (bad
            # name, disk full, unreadable, a device-name write that blocks) surfaces
            # to the user -- an upload must never silently vanish.
            try:
                data = await run.io_bound(e.content.read)
                path = await run.io_bound(_persist_upload, e.name, data)
            except Exception as exc:  # noqa: BLE001
                ui.notify(f"Couldn't add {e.name}: {exc}", type="negative", timeout=8000)
                return
            if path is None:
                ui.notify(f"Couldn't add {e.name} — unsupported or invalid filename.", type="warning")
                return
            await state.add_files([str(path)])  # type: ignore[attr-defined]

        upload = (
            ui.upload(
                on_upload=handle_upload,
                multiple=True,
                auto_upload=True,
                max_file_size=100 * 1024 * 1024,  # 100 MB -- bank PDFs can be large
                label="Drag documents here — or click to browse",
            )
            .props('flat accept=".docx,.doc,.xlsx,.xlsm,.xls,.pptx,.ppt,.pdf"')
            .classes("az-dropzone w-full")
        )
        state.upload = upload  # type: ignore[attr-defined]
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
                with ui.row().classes("mt-2"):
                    ui.button(
                        "Open output folder",
                        icon="folder_open",
                        on_click=lambda p=job.out_path: _reveal_in_explorer(p),
                    ).props("flat dense").classes("text-xs")
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
    # Re-entrancy guard: flip status BEFORE the first await so a rapid double-click
    # (or Save + Save-all racing the same job) can't launch two apply_document runs
    # on one source -- which would race the same atomic .part temp write.
    if job.status != "review":
        return
    job.status = "saving"
    state.refresh()  # type: ignore[attr-defined]
    analyzer, base = await run.io_bound(_ensure_analyzer)
    config = job.config or base  # same config the file was scanned with (parity)
    grouped = job.scan.all_actionable()
    try:
        out_path, report_path = await run.io_bound(
            apply_document, Path(job.path), grouped, analyzer, config, None, anonymized_dir()
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
    if job.status == "done":
        _discard_working_copy(job)  # drop the raw-PII upload copy once safely saved
    state.refresh()  # type: ignore[attr-defined]


def _clear(state: PageState) -> None:
    for job in state.jobs:
        _discard_working_copy(job)  # remove staged raw-PII copies, not user originals
    state.jobs.clear()
    state.selected = None
    upload = getattr(state, "upload", None)
    if upload is not None:
        upload.reset()  # clear the uploader's own file list; the queue is authoritative
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


def _reveal_in_explorer(file_path: str) -> None:
    """Open the OS file manager with the anonymized file selected (Windows), or its
    folder. Best-effort -- a failure here must never crash the app."""
    p = Path(file_path)
    try:
        if os.name == "nt" and p.exists():
            # /select highlights the file itself, matching the docstring's promise.
            subprocess.run(["explorer", f"/select,{p}"], check=False)  # noqa: S603,S607
        elif p.parent.exists():
            if os.name == "nt":
                os.startfile(p.parent)  # type: ignore[attr-defined] # noqa: S606
            else:
                ui.notify(f"Saved in {p.parent}", type="info")
        else:
            ui.notify("That output folder no longer exists.", type="warning")
    except Exception as exc:  # noqa: BLE001
        ui.notify(f"Couldn't open the folder: {exc}", type="warning")


def main() -> None:
    warm_start()
    app.on_shutdown(_cleanup_work_dir)  # remove uploaded working copies on exit
    ui.run(title="Document Anonymizer", reload=False, native=True, window_size=(1400, 950))


if __name__ in {"__main__", "__mp_main__"}:
    main()
