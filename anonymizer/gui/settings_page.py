from __future__ import annotations

from pathlib import Path

from nicegui import run, ui

from .. import audit as audit_mod
from .. import config as config_mod
from .. import ocr as ocr_mod
from ..mapping import MappingStore
from ..pipeline import scan_document
from . import theme

ACTIONS = ["pseudonymize", "anonymize", "skip"]


def build() -> None:
    cfg = config_mod.load_config()

    flush_hooks: list = []
    _detection_section(cfg)
    _gliner_section(cfg)
    _ocr_section(cfg)
    _lists_and_recognizers(cfg, flush_hooks)
    _mapping_admin()

    def save() -> None:
        # Flush any pending textarea edits into cfg BEFORE saving -- the lists sync
        # on blur, but clicking Save without the textarea losing focus first could
        # otherwise persist a stale (missing a just-typed deny term = a leak next scan).
        for hook in flush_hooks:
            hook()
        config_mod.save_config(cfg)
        ui.notify("Settings saved. Restart the app to apply detection changes.", type="positive")

    ui.button("Save settings", icon="save", on_click=save).props("color=primary")


def _detection_section(cfg: dict) -> None:
    with ui.element("div").classes("az-card w-full"):
        ui.label("Detection sensitivity").classes("az-h2")
        ui.label(
            "Global recall vs precision. Higher = catch more (more to review); lower = fewer, higher-confidence hits."
        ).classes("az-muted text-xs mb-2")
        with ui.row().classes("items-center gap-4 w-full"):
            ui.label("precision").classes("az-muted text-xs")
            slider = ui.slider(min=0.0, max=0.3, step=0.01, value=float(cfg.get("sensitivity", 0.0))).classes(
                "flex-grow"
            )
            slider.bind_value(cfg, "sensitivity")
            ui.label("recall").classes("az-muted text-xs")
            ui.label().bind_text_from(cfg, "sensitivity", lambda v: f"+{float(v or 0):.2f}").classes(
                "az-mono text-xs w-12"
            )
        _sensitivity_preview(cfg, slider)


def _gliner_status(gliner: dict) -> tuple[bool, str]:
    """(ok, human_status) for the AI-detection model WITHOUT loading it (loading
    is heavy). Reports whether the ONNX runtime is importable and whether the
    model file exists + its size -- exactly what a user needs to see before a
    scan hard-fails on an enabled-but-missing model."""
    import importlib.util

    from ..gliner_recognizer import resolve_model_path

    runtime_ok = (
        importlib.util.find_spec("gliner") is not None and importlib.util.find_spec("onnxruntime") is not None
    )
    try:
        mp = resolve_model_path(gliner)
        model_ok = mp.exists()
        if model_ok:
            size_mb = sum(f.stat().st_size for f in mp.rglob("*") if f.is_file()) / 1e6 if mp.is_dir() else mp.stat().st_size / 1e6
            model_str = f"{mp.name} ({size_mb:.0f} MB)"
        else:
            model_str = f"not found at {mp}"
    except Exception as exc:  # noqa: BLE001
        model_ok, model_str = False, f"unresolved ({exc})"
    ok = runtime_ok and model_ok
    return ok, f"runtime: {'installed' if runtime_ok else 'MISSING'} · model: {model_str}"


def _gliner_section(cfg: dict) -> None:
    """Toggle + read-only status for the GLiNER second-pass ML detector. The
    status line tells the user, before scanning, whether an enabled model will
    actually load (an enabled-but-missing model hard-fails the scan by design --
    this is the escape hatch that lets them turn it off)."""
    gliner = cfg.get("gliner")
    if gliner is None:
        return
    with ui.element("div").classes("az-card w-full"):
        with ui.row().classes("items-center gap-2"):
            ui.label("AI detection (GLiNER)").classes("az-h2")
            en = ui.switch(value=bool(gliner.get("enabled", False)))
            en.bind_value(gliner, "enabled")
        ui.label(
            "Offline zero-shot NER that recovers names, organizations and internal tools/projects the "
            "rule-based pass misses, and catches English terms inside German documents. Requires the "
            "bundled model; when enabled but the model is missing, scanning stops with an error so you can "
            "turn it back off here."
        ).classes("az-muted text-xs mb-2")
        ok, status = _gliner_status(gliner)
        ui.label(status).classes(f"text-xs az-mono {'text-positive' if ok else 'text-warning'}")


# Debounce: re-scanning is real work (seconds, not instant), so a preview
# fires only after the slider has been still for this long, not on every
# intermediate drag tick.
_PREVIEW_DEBOUNCE_S = 0.6


def _sensitivity_preview(cfg: dict, slider) -> None:
    """Live 'N findings at this setting' readout against whatever document is
    currently open on the main page. This is a REAL re-scan (not a cheap
    re-bucketing -- the sensitivity offset gates which candidates become
    findings at all, during detection itself, so nothing short of re-running
    detection reflects a changed value), debounced so dragging the slider
    doesn't fire one per tick. Settings is a separate NiceGUI page/route with
    no other link to the main page's job state, hence the lazy app import and
    the current_review_job() accessor."""
    from . import app as app_module  # local: app.py imports this module at its own top level

    label = ui.label().classes("az-muted text-xs mt-1")
    pending = {"timer": None}

    def show(text: str) -> None:
        label.text = text

    async def run_preview() -> None:
        job = app_module.current_review_job()
        if job is None:
            show("Open and scan a document to preview this slider's live effect on it.")
            return
        show(f"Re-scanning “{job.name}” at this setting…")
        analyzer, _base_cfg = await run.io_bound(app_module._ensure_analyzer)
        trial_cfg = {**(job.config or {}), "sensitivity": float(cfg.get("sensitivity", 0.0))}
        try:
            result = await run.io_bound(scan_document, Path(job.path), analyzer, trial_cfg)
        except Exception as exc:  # noqa: BLE001 -- a preview failure must not block Settings
            show(f"Preview failed: {exc}")
            return
        show(f"At this setting: {len(result.all_actionable())} finding(s) on “{job.name}” (open now).")

    def debounced(_e=None) -> None:
        if pending["timer"] is not None:
            pending["timer"].deactivate()
        pending["timer"] = ui.timer(_PREVIEW_DEBOUNCE_S, run_preview, once=True)

    show(
        "Open and scan a document to preview this slider's live effect on it."
        if app_module.current_review_job() is None
        else "Move the slider to preview its effect on the currently open document."
    )
    slider.on_value_change(debounced)

    with ui.element("div").classes("az-card w-full"):
        ui.label("Entity defaults").classes("az-h2")
        ui.label("Default action and confidence threshold per entity type.").classes("az-muted text-xs mb-2")
        for entity_type, settings in cfg.get("entities", {}).items():
            with ui.row().classes("items-center gap-4 w-full az-row py-1"):
                ui.label(entity_type).classes("az-mono text-sm w-44")
                ui.select(ACTIONS, value=settings.get("default_action", "anonymize")).bind_value(
                    settings, "default_action"
                ).props("dense outlined")
                ui.slider(min=0.0, max=1.0, step=0.05, value=settings.get("confidence_threshold", 0.5)).bind_value(
                    settings, "confidence_threshold"
                ).classes("flex-grow")
                ui.label().bind_text_from(settings, "confidence_threshold", lambda v: f"{v:.2f}").classes(
                    "az-mono text-xs w-10"
                )


def _ocr_section(cfg: dict) -> None:
    with ui.element("div").classes("az-card w-full"):
        ui.label("OCR — scanned PDFs").classes("az-h2")
        ocr_mod.reset_resolution()
        available = ocr_mod.ocr_available(cfg)
        with ui.row().classes("items-center gap-2 mb-1"):
            theme.chip("available" if available else "not found", theme.POSITIVE if available else theme.WARNING,
                       filled=available)
            ui.label(
                "Reads scanned/image PDFs via a portable Tesseract. Without it, scanned PDFs are refused "
                "(never silently passed)."
            ).classes("az-muted text-xs")
        ui.input(label="Tesseract path (optional)", value=cfg.get("tesseract_path", "")).bind_value(
            cfg, "tesseract_path"
        ).props("dense outlined").classes("w-full")
        ui.label(
            "Leave blank to auto-detect a `tesseract` folder in the app bundle or on PATH. Save and reopen "
            "this page to re-check."
        ).classes("az-muted text-xs")


def _lists_and_recognizers(cfg: dict, flush_hooks: list | None = None) -> None:
    with ui.element("div").classes("az-card w-full"):
        ui.label("Allow / deny lists").classes("az-h2 mb-2")
        with ui.row().classes("w-full gap-4"):
            with ui.column().classes("flex-grow"):
                ui.label("Allow list — never flag these terms").classes("text-sm font-medium")
                allow_area = ui.textarea(value="\n".join(cfg.get("allow_list", []))).props("outlined").classes(
                    "w-full"
                )
            with ui.column().classes("flex-grow"):
                ui.label("Deny list — always flag these terms").classes("text-sm font-medium")
                deny_area = ui.textarea(value="\n".join(cfg.get("deny_list", []))).props("outlined").classes(
                    "w-full"
                )

        def sync_lists() -> None:
            cfg["allow_list"] = [ln.strip() for ln in allow_area.value.splitlines() if ln.strip()]
            cfg["deny_list"] = [ln.strip() for ln in deny_area.value.splitlines() if ln.strip()]

        allow_area.on("blur", sync_lists)
        deny_area.on("blur", sync_lists)
        if flush_hooks is not None:
            flush_hooks.append(sync_lists)  # save() flushes even if blur never fired

    with ui.element("div").classes("az-card w-full"):
        ui.label("Name columns — spreadsheets").classes("az-h2 mb-1")
        ui.label(
            "Extra column-header words that mark a column as people, so every cell in it is "
            "treated as a name (catches bare surnames the model misses in a table cell). One "
            "per line, matched case-insensitively. Built-ins already cover Name, Kunde, "
            "Sachbearbeiter, Ansprechpartner, Projektleiter, Betreuer, Verantwortlich, and more."
        ).classes("az-muted text-xs mb-2")
        nch_area = ui.textarea(value="\n".join(cfg.get("name_column_headers", []))).props("outlined").classes(
            "w-full"
        )

        def sync_name_headers() -> None:
            cfg["name_column_headers"] = [ln.strip() for ln in nch_area.value.splitlines() if ln.strip()]

        nch_area.on("blur", sync_name_headers)
        if flush_hooks is not None:
            flush_hooks.append(sync_name_headers)

    _topical_section(cfg, flush_hooks)

    with ui.element("div").classes("az-card w-full"):
        ui.label("Custom recognizers").classes("az-h2")
        ui.label("German bank-specific patterns Presidio doesn't ship with.").classes("az-muted text-xs mb-2")
        column = ui.column().classes("w-full gap-2")
        _render_recognizers(column, cfg)

        def add_recognizer() -> None:
            cfg.setdefault("custom_recognizers", []).append(
                {"name": "NEW_ENTITY", "language": "de", "patterns": [{"regex": "", "score": 0.5}], "context": []}
            )
            column.clear()
            _render_recognizers(column, cfg)

        def check_for_new() -> None:
            added = config_mod.merge_new_recognizers(cfg)
            column.clear()
            _render_recognizers(column, cfg)
            ui.notify(
                f"Added {added} new item(s)." if added else "Already up to date.",
                type="positive" if added else "info",
            )

        with ui.row().classes("gap-2"):
            ui.button("Add recognizer", icon="add", on_click=add_recognizer).props("flat")
            ui.button("Check for new recognizers", icon="sync", on_click=check_for_new).props("flat")


_TOPICAL_LABELS = {
    "TOOL": "Tools / systems / software",
    "DIVISION": "Divisions / business areas",
    "DEPARTMENT": "Departments / teams",
    "LICENSEE": "Licensees / vendors",
    "PROJECT": "Project names",
    "DESCRIPTION": "Free-text description columns",
}


def _topical_section(cfg: dict, flush_hooks: list | None = None) -> None:
    """Editor for the topical (non-personal) categories: per-category MANUAL
    terms (a name the model can't detect in prose -- 'DeepL Pro', 'Claudius' --
    added here is redacted document-wide via propagation) and the column HEADER
    words that assign a whole column to a category. Both take effect on the next
    scan."""
    topical = cfg.get("topical") or {}
    cats = topical.get("categories") or {}
    if not cats:
        return
    with ui.element("div").classes("az-card w-full"):
        with ui.row().classes("items-center gap-2"):
            ui.label("Internal / topical categories").classes("az-h2")
            en = ui.switch(value=bool(topical.get("enabled", True)))
            en.on_value_change(lambda e: topical.__setitem__("enabled", bool(e.value)))
        ui.label(
            "Non-personal sensitivity: tools, divisions, departments, licensees, projects, and free-text "
            "descriptions. TERMS = names to redact everywhere (add ones the model misses in prose, e.g. an "
            "internal tool). HEADER WORDS = column headers that put a whole column in this category. "
            "One per line; effective on the next scan."
        ).classes("az-muted text-xs mb-2")
        for cat, spec in cats.items():
            with ui.expansion(_TOPICAL_LABELS.get(cat, cat)).classes("w-full"):
                with ui.row().classes("w-full gap-4"):
                    with ui.column().classes("flex-grow"):
                        ui.label("Terms (redacted document-wide)").classes("text-xs font-medium")
                        terms_area = ui.textarea(value="\n".join(spec.get("terms", []) or [])).props(
                            "outlined dense"
                        ).classes("w-full")
                    with ui.column().classes("flex-grow"):
                        ui.label("Header words (whole-column)").classes("text-xs font-medium")
                        head_area = ui.textarea(value="\n".join(spec.get("header_terms", []) or [])).props(
                            "outlined dense"
                        ).classes("w-full")

                def sync(_e=None, spec=spec, ta=terms_area, ha=head_area) -> None:
                    spec["terms"] = [ln.strip() for ln in ta.value.splitlines() if ln.strip()]
                    spec["header_terms"] = [ln.strip() for ln in ha.value.splitlines() if ln.strip()]

                terms_area.on("blur", sync)
                head_area.on("blur", sync)
                if flush_hooks is not None:
                    flush_hooks.append(sync)


def _render_recognizers(column, cfg: dict) -> None:
    with column:
        for rec in cfg.get("custom_recognizers", []):
            with ui.element("div").classes("az-card w-full").style("padding:12px"):
                with ui.row().classes("items-center gap-3 w-full"):
                    ui.input(label="Entity name", value=rec.get("name", "")).bind_value(rec, "name").props("dense")
                    ui.select(["de", "en"], value=rec.get("language", "de")).bind_value(rec, "language").props(
                        "dense outlined"
                    ).classes("w-24")

                    def remove(rec=rec) -> None:
                        cfg["custom_recognizers"].remove(rec)
                        column.clear()
                        _render_recognizers(column, cfg)

                    ui.button(icon="delete", on_click=remove).props("flat dense")

                pattern = rec.get("patterns", [{"regex": "", "score": 0.5}])[0]
                if "patterns" not in rec:
                    rec["patterns"] = [pattern]
                with ui.row().classes("items-center gap-3 w-full"):
                    ui.input(label="Regex", value=pattern.get("regex", "")).bind_value(pattern, "regex").props(
                        "dense"
                    ).classes("flex-grow")
                    ui.number(label="Score", min=0.0, max=1.0, step=0.05, value=pattern.get("score", 0.5)).bind_value(
                        pattern, "score"
                    ).props("dense").classes("w-24")

                context_input = ui.input(
                    label="Context words (comma-separated)", value=", ".join(rec.get("context", []))
                ).props("dense").classes("w-full")

                def sync_context(rec=rec, ci=context_input) -> None:
                    rec["context"] = [w.strip() for w in ci.value.split(",") if w.strip()]

                context_input.on("blur", sync_context)


def _mapping_admin() -> None:
    with ui.element("div").classes("az-card w-full"):
        with ui.row().classes("items-center gap-2"):
            ui.icon("key", size="1.2rem").style(f"color:{theme.WARNING}")
            ui.label("Pseudonym mapping (sensitive)").classes("az-h2")
        with MappingStore() as store:
            count = store.entry_count()
        ui.label(
            f"{count} stored mapping(s). This is the reversible re-identification store — handle with care. "
            "All actions here are written to the audit log."
        ).classes("az-muted text-xs mb-2")

        erase_input = ui.input(label="Placeholder to erase (e.g. PERSON_3)").props("dense outlined").classes("w-72")

        def do_erase() -> None:
            token = (erase_input.value or "").strip().strip("[]")
            if not token:
                return
            with MappingStore() as store:
                ok = store.erase(token)
            audit_mod.log_event("mapping.erase", token if ok else f"{token} (not found)")
            ui.notify(f"Erased {token}." if ok else f"No mapping for {token}.", type="positive" if ok else "warning")
            erase_input.value = ""

        def confirm(title: str, body: str, on_yes) -> None:
            with ui.dialog() as dlg, ui.element("div").classes("az-card").style("max-width:460px"):
                ui.label(title).classes("az-h2")
                ui.label(body).classes("az-muted text-sm my-2")
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Cancel", on_click=dlg.close).props("flat")

                    def go() -> None:
                        dlg.close()
                        on_yes()

                    ui.button("Confirm", on_click=go).props("color=negative")
            dlg.open()

        def do_reset() -> None:
            with MappingStore() as store:
                store.reset()
            audit_mod.log_event("mapping.reset", f"{count} entries wiped")
            ui.notify("Mapping reset. New tokens will restart from 1.", type="positive")

        def do_rotate() -> None:
            with MappingStore() as store:
                store.rotate_key()
            audit_mod.log_event("mapping.rotate_key")
            ui.notify("Encryption key rotated.", type="positive")

        with ui.row().classes("items-center gap-2 flex-wrap"):
            ui.button("Erase", icon="delete_forever", on_click=do_erase).props("flat")
            ui.button(
                "Reset all mappings",
                icon="restart_alt",
                on_click=lambda: confirm(
                    "Reset all mappings?",
                    "Every pseudonym is deleted. Already-anonymized documents can no longer be re-identified. "
                    "This cannot be undone.",
                    do_reset,
                ),
            ).props("flat")
            ui.button(
                "Rotate key",
                icon="autorenew",
                on_click=lambda: confirm(
                    "Rotate encryption key?",
                    "A fresh key is generated. Any old copies of the mapping file become undecryptable.",
                    do_rotate,
                ),
            ).props("flat")
            ui.button("Open re-identify tool", icon="lock_open", on_click=lambda: ui.navigate.to("/reidentify")).props(
                "flat"
            )
