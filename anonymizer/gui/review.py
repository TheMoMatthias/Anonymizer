"""Category-first review screen.

The core fix for the "630 fields to check" problem: the reviewer decides one
action PER DATA CLASS (People, Financial IDs, ...), not per value. High-
confidence, checksum-validated findings are pre-decided and tucked into a
collapsed "auto-accepted" strip; only the uncertain minority is surfaced up
front. Per-value overrides are one expand away for the exceptions.
"""

from __future__ import annotations

from collections.abc import Callable

from nicegui import ui

from ..models import GroupedFinding, ScanResult
from . import theme

ACTIONS = ["pseudonymize", "anonymize", "skip"]


def _class_card(dcg, on_change: Callable) -> None:
    review_items = dcg.review_items
    auto_items = dcg.high_tier_items
    with ui.element("div").classes("az-card w-full"):
        # Header: category + bulk action for the whole class.
        with ui.row().classes("items-center gap-3 w-full"):
            with ui.column().classes("gap-1 flex-grow min-w-0"):
                with ui.row().classes("items-center gap-2"):
                    ui.label(dcg.display).classes("az-h2")
                    theme.sensitivity_chip(dcg.sensitivity)
                caption = f"{dcg.count} occurrence(s)"
                if review_items:
                    caption += f" · {len(review_items)} to review"
                if auto_items:
                    caption += f" · {len(auto_items)} auto-accepted"
                ui.label(caption).classes("az-muted text-xs")
            ui.label("Set whole category:").classes("az-muted text-xs")
            bulk = ui.select(ACTIONS, value=_dominant_action(dcg.items)).props("dense outlined").classes("w-40")

        selects: list = []

        def bulk_apply() -> None:
            for g, sel in selects:
                g.action = bulk.value
                sel.set_value(bulk.value)
            on_change()

        bulk.on_value_change(bulk_apply)

        # Review-tier items first (the ones that actually need attention).
        if review_items:
            with ui.column().classes("w-full mt-2 gap-0"):
                for g in review_items:
                    row_selects = _capture_row(g, on_change)
                    selects.extend(row_selects)

        # High-confidence items collapsed out of the way.
        if auto_items:
            with ui.expansion(f"{len(auto_items)} auto-accepted (high confidence)").classes("w-full mt-2"):
                with ui.column().classes("w-full gap-0"):
                    for g in auto_items:
                        selects.extend(_capture_row(g, on_change))


def _capture_row(g: GroupedFinding, on_change: Callable) -> list:
    """Renders a value row and returns [(g, select)] so bulk actions can update
    the visible per-value selects."""
    captured: list = []
    with ui.row().classes("az-row items-center gap-3 w-full py-1 px-1"):
        with ui.column().classes("gap-0 flex-grow min-w-0"):
            ui.label(g.value[:80]).classes("az-mono text-sm truncate")
            ui.label(g.context).classes("az-muted text-xs truncate")
        if g.validated is True:
            theme.chip("✓ valid", theme.TIER_COLORS["high"])
        elif g.validated is False:
            theme.chip("⚠ unverified", theme.SENSITIVITY_COLORS["medium"])
        ui.label(f"×{g.count}").classes("az-muted text-xs w-8 text-right")
        ui.label(f"{g.max_score:.2f}").classes("az-muted text-xs w-10 text-right")
        sel = ui.select(ACTIONS, value=g.action).props("dense outlined").classes("w-36")
        sel.bind_value(g, "action")
        sel.on_value_change(lambda: on_change())
        captured.append((g, sel))
    return captured


def _dominant_action(items: list[GroupedFinding]) -> str:
    counts: dict[str, int] = {}
    for g in items:
        counts[g.action] = counts.get(g.action, 0) + 1
    return max(counts, key=counts.get) if counts else "pseudonymize"


def render_review(container, result: ScanResult, on_change: Callable) -> None:
    container.clear()
    with container:
        if not result.all_actionable() and not result.possible_misses:
            ui.label("No sensitive data detected in this document.").classes("az-muted")
            return

        _stat_bar(result)

        # Global bulk actions.
        with ui.row().classes("items-center gap-2 w-full"):
            ui.label("Apply to everything:").classes("az-muted text-xs")
            for action in ACTIONS:
                ui.button(action, on_click=lambda a=action: _set_all(result, a, container, on_change)).props(
                    "flat dense"
                ).classes("text-xs")

        with ui.column().classes("w-full gap-3 az-scroll pr-1"):
            for dcg in result.groups:
                _class_card(dcg, on_change)

            if result.possible_misses:
                _possible_misses_card(result.possible_misses)


def _set_all(result: ScanResult, action: str, container, on_change: Callable) -> None:
    for g in result.all_actionable():
        g.action = action
    render_review(container, result, on_change)
    on_change()


def _stat_bar(result: ScanResult) -> None:
    s = result.stats
    with ui.row().classes("az-card w-full items-center justify-around gap-4"):
        _stat(s.get("distinct_findings", 0), "distinct findings")
        _stat(s.get("needs_review", 0), "to review", theme.WARNING)
        _stat(s.get("auto_accept", 0), "auto-accepted", theme.PRIMARY)
        _stat(s.get("possible_misses", 0), "possible misses", theme.SECONDARY)


def _stat(n: int, label: str, color: str | None = None) -> None:
    with ui.element("div").classes("az-stat"):
        ui.label(str(n)).classes("n").style(f"color:{color}" if color else "")
        ui.label(label).classes("l")


def _possible_misses_card(misses: list[GroupedFinding]) -> None:
    with ui.expansion(f"Possible misses — {len(misses)} sensitive-looking string(s) no recognizer matched").classes(
        "w-full"
    ):
        ui.label(
            "Informational only. These are not redacted. If any is sensitive, add it to the deny list "
            "in Settings and re-scan."
        ).classes("az-muted text-xs mb-2")
        with ui.column().classes("w-full gap-0"):
            for g in misses:
                with ui.row().classes("az-row items-center gap-3 w-full py-1"):
                    ui.label(g.value[:80]).classes("az-mono text-sm flex-grow truncate")
                    ui.label(g.context).classes("az-muted text-xs flex-grow truncate")
                    ui.label(f"×{g.count}").classes("az-muted text-xs w-8 text-right")
