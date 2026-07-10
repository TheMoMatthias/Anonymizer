from __future__ import annotations

from nicegui import ui

from .. import config as config_mod

ACTIONS = ["pseudonymize", "anonymize", "skip"]


def build(state) -> None:
    cfg = config_mod.load_config()

    ui.label("Entity defaults").classes("text-lg font-bold")
    with ui.column().classes("w-full gap-2"):
        for entity_type, settings in cfg.get("entities", {}).items():
            with ui.row().classes("items-center gap-4 w-full"):
                ui.label(entity_type).classes("w-48")
                ui.select(ACTIONS, value=settings.get("default_action", "anonymize")).bind_value(
                    settings, "default_action"
                )
                ui.slider(min=0.0, max=1.0, step=0.05, value=settings.get("confidence_threshold", 0.5)).bind_value(
                    settings, "confidence_threshold"
                ).classes("w-64")
                ui.label().bind_text_from(settings, "confidence_threshold", lambda v: f"{v:.2f}")

    ui.separator()
    ui.label("Allow list (never flag these terms)").classes("text-lg font-bold")
    allow_text = "\n".join(cfg.get("allow_list", []))
    allow_area = ui.textarea(value=allow_text).classes("w-full")

    ui.label("Deny list (always flag these terms)").classes("text-lg font-bold")
    deny_text = "\n".join(cfg.get("deny_list", []))
    deny_area = ui.textarea(value=deny_text).classes("w-full")

    ui.separator()
    ui.label("Custom recognizers").classes("text-lg font-bold")
    recognizers_column = ui.column().classes("w-full gap-2")
    _render_recognizers(recognizers_column, cfg)

    def add_recognizer() -> None:
        cfg.setdefault("custom_recognizers", []).append(
            {"name": "NEW_ENTITY", "language": "de", "patterns": [{"regex": "", "score": 0.5}], "context": []}
        )
        recognizers_column.clear()
        _render_recognizers(recognizers_column, cfg)

    ui.button("Add custom recognizer", on_click=add_recognizer)

    def save() -> None:
        cfg["allow_list"] = [line.strip() for line in allow_area.value.splitlines() if line.strip()]
        cfg["deny_list"] = [line.strip() for line in deny_area.value.splitlines() if line.strip()]
        config_mod.save_config(cfg)
        ui.notify("Settings saved. Restart the scan to apply changes.", type="positive")

    ui.button("Save settings", on_click=save).classes("mt-4")


def _render_recognizers(column, cfg: dict) -> None:
    with column:
        for rec in cfg.get("custom_recognizers", []):
            with ui.card().classes("w-full"):
                with ui.row().classes("items-center gap-4 w-full"):
                    ui.input(label="Entity name", value=rec.get("name", "")).bind_value(rec, "name")
                    ui.select(["de", "en"], value=rec.get("language", "de")).bind_value(rec, "language")

                    def remove(rec=rec) -> None:
                        cfg["custom_recognizers"].remove(rec)
                        column.clear()
                        _render_recognizers(column, cfg)

                    ui.button(icon="delete", on_click=remove).props("flat")

                pattern = rec.get("patterns", [{"regex": "", "score": 0.5}])[0]
                with ui.row().classes("items-center gap-4 w-full"):
                    ui.input(label="Regex pattern", value=pattern.get("regex", "")).bind_value(
                        pattern, "regex"
                    ).classes("flex-grow")
                    ui.number(label="Score", min=0.0, max=1.0, step=0.05, value=pattern.get("score", 0.5)).bind_value(
                        pattern, "score"
                    )
                if "patterns" not in rec:
                    rec["patterns"] = [pattern]

                context_text = ", ".join(rec.get("context", []))
                context_input = ui.input(label="Context words (comma-separated)", value=context_text).classes(
                    "w-full"
                )

                def sync_context(rec=rec, context_input=context_input) -> None:
                    rec["context"] = [w.strip() for w in context_input.value.split(",") if w.strip()]

                context_input.on("blur", sync_context)
