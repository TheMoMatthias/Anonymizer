from __future__ import annotations

from nicegui import ui

from .. import config as config_mod

ACTIONS = ["pseudonymize", "anonymize", "skip"]


def build() -> None:
    cfg = config_mod.load_config()

    with ui.card().classes("w-full"):
        ui.label("Entity defaults").classes("text-lg font-bold")
        ui.label("Action and confidence threshold applied to each entity type by default.").classes(
            "text-xs text-gray-500 mb-2"
        )
        with ui.column().classes("w-full gap-2"):
            for entity_type, settings in cfg.get("entities", {}).items():
                with ui.row().classes("items-center gap-4 w-full"):
                    ui.label(entity_type).classes("w-48 font-mono text-sm")
                    ui.select(ACTIONS, value=settings.get("default_action", "anonymize")).bind_value(
                        settings, "default_action"
                    ).props("dense outlined")
                    ui.slider(
                        min=0.0, max=1.0, step=0.05, value=settings.get("confidence_threshold", 0.5)
                    ).bind_value(settings, "confidence_threshold").classes("w-64")
                    ui.label().bind_text_from(settings, "confidence_threshold", lambda v: f"{v:.2f}").classes(
                        "w-10 text-xs"
                    )

    with ui.card().classes("w-full"):
        ui.label("Allow / deny lists").classes("text-lg font-bold")
        with ui.row().classes("w-full gap-4"):
            with ui.column().classes("flex-grow"):
                ui.label("Allow list (never flag these terms)").classes("text-sm font-bold")
                allow_area = ui.textarea(value="\n".join(cfg.get("allow_list", []))).classes("w-full")
            with ui.column().classes("flex-grow"):
                ui.label("Deny list (always flag these terms)").classes("text-sm font-bold")
                deny_area = ui.textarea(value="\n".join(cfg.get("deny_list", []))).classes("w-full")

    with ui.card().classes("w-full"):
        ui.label("Custom recognizers").classes("text-lg font-bold")
        ui.label("German bank-specific patterns Presidio doesn't ship with.").classes("text-xs text-gray-500 mb-2")
        recognizers_column = ui.column().classes("w-full gap-2")
        _render_recognizers(recognizers_column, cfg)

        def add_recognizer() -> None:
            cfg.setdefault("custom_recognizers", []).append(
                {"name": "NEW_ENTITY", "language": "de", "patterns": [{"regex": "", "score": 0.5}], "context": []}
            )
            recognizers_column.clear()
            _render_recognizers(recognizers_column, cfg)

        ui.button("Add custom recognizer", on_click=add_recognizer).props("outline")

    def save() -> None:
        cfg["allow_list"] = [line.strip() for line in allow_area.value.splitlines() if line.strip()]
        cfg["deny_list"] = [line.strip() for line in deny_area.value.splitlines() if line.strip()]
        config_mod.save_config(cfg)
        ui.notify("Settings saved. Restart the app to apply changes.", type="positive")

    ui.button("Save settings", on_click=save).props("color=primary")


def _render_recognizers(column, cfg: dict) -> None:
    with column:
        for rec in cfg.get("custom_recognizers", []):
            with ui.card().classes("w-full").props("flat bordered"):
                with ui.row().classes("items-center gap-4 w-full"):
                    ui.input(label="Entity name", value=rec.get("name", "")).bind_value(rec, "name")
                    ui.select(["de", "en"], value=rec.get("language", "de")).bind_value(rec, "language").classes(
                        "w-24"
                    )

                    def remove(rec=rec) -> None:
                        cfg["custom_recognizers"].remove(rec)
                        column.clear()
                        _render_recognizers(column, cfg)

                    ui.button(icon="delete", on_click=remove).props("flat")

                pattern = rec.get("patterns", [{"regex": "", "score": 0.5}])[0]
                if "patterns" not in rec:
                    rec["patterns"] = [pattern]
                with ui.row().classes("items-center gap-4 w-full"):
                    ui.input(label="Regex pattern", value=pattern.get("regex", "")).bind_value(
                        pattern, "regex"
                    ).classes("flex-grow")
                    ui.number(label="Score", min=0.0, max=1.0, step=0.05, value=pattern.get("score", 0.5)).bind_value(
                        pattern, "score"
                    ).classes("w-24")

                context_input = ui.input(
                    label="Context words (comma-separated)", value=", ".join(rec.get("context", []))
                ).classes("w-full")

                def sync_context(rec=rec, context_input=context_input) -> None:
                    rec["context"] = [w.strip() for w in context_input.value.split(",") if w.strip()]

                context_input.on("blur", sync_context)
