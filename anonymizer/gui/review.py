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

from ..actions import token_label
from ..models import GroupedFinding, ScanResult
from . import theme

ACTIONS = ["skip", "pseudonymize", "anonymize", "summarize"]

# Compact labels + the Quasar brand colour each action lights up in. A segmented
# toggle (not a dropdown) keeps every row's decision visible at a glance and one
# click to change -- with a column of dropdowns you cannot scan what will happen
# to each value without opening them one by one. The no-op ("Skip") is always
# FIRST and always grey, in every toggle this screen renders -- one shared
# visual language for "do nothing" everywhere, rather than each action surface
# inventing its own word/position/colour for it.
# "anonymize" keeps its config VALUE (no migration churn) but displays as
# "Redact" -- the one-way bare-[LABEL] blackout. "summarize" is the new
# structural-placeholder mode (mainly for free-text/topical cells).
_ACTION_LABELS = {"skip": "Skip", "pseudonymize": "Pseudonym", "anonymize": "Redact", "summarize": "Summarize"}
_ACTION_QCOLOR = {"skip": "grey-7", "pseudonymize": "primary", "anonymize": "negative", "summarize": "info"}

# Whole-column policy (spreadsheets): "skip" leaves the column to per-value review;
# the others black out EVERY non-empty cell in the column (see xlsx_handler). Shares
# _ACTION_LABELS/_ACTION_QCOLOR's exact word/position/colour for the no-op case --
# column policy and per-value review used to render this as two different-looking
# groups ("Keep" first vs. "Skip" last, both a hardcoded "grey-7" duplicated in two
# places) despite meaning the same thing: "don't touch this via this control."
_COLUMN_LABELS = _ACTION_LABELS
_COLUMN_QCOLOR = _ACTION_QCOLOR

# Trust tiers, most-confident first, for the by-confidence bulk bands.
_TIER_BANDS = [("high", "High confidence"), ("medium", "Medium"), ("low", "Low")]

# Max per-value rows rendered per class before the rest is summarized. Each row is
# a live segmented toggle, so rendering thousands at once (a "database" workbook)
# stalls the screen. The overflow stays fully decidable (bulk) and expandable.
_REVIEW_CAP = 100


def _labeled_toggle(labels: dict[str, str], colors: dict[str, str], initial: str):
    """The one toggle-building helper every action surface on this screen uses
    (per-value rows, overflow rows, tier bands, column policies, ...) so they
    can never again drift into visually-inconsistent near-duplicates."""
    tog = ui.toggle(labels, value=initial).props("dense unelevated no-caps")

    def paint() -> None:
        tog.props(f"toggle-color={colors.get(tog.value, 'grey-7')}")

    paint()
    return tog, paint


def _action_toggle(initial: str):
    return _labeled_toggle(_ACTION_LABELS, _ACTION_QCOLOR, initial)


def _class_card(dcg, on_change: Callable, expanded: set, rerender: Callable) -> None:
    review_items = dcg.review_items
    auto_items = dcg.high_tier_items
    show_all = dcg.key in expanded
    cap = None if show_all else _REVIEW_CAP
    shown_review = review_items if cap is None else review_items[:cap]
    over_review = review_items[len(shown_review):]
    shown_auto = auto_items if cap is None else auto_items[:cap]
    over_auto = auto_items[len(shown_auto):]

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
            # Say exactly how many rows the bulk control touches: it rewrites EVERY
            # value in the class -- the auto-accepted ones and the summarized overflow
            # too -- and silently changing decisions the reviewer cannot see would
            # break the mental model.
            ui.label(f"Set all {len(dcg.items)}:").classes("az-muted text-xs").tooltip(
                "Applies to every value in this category, including hidden overflow and auto-accepted ones."
            )
            bulk, bulk_paint = _action_toggle(_dominant_action(dcg.items))

        selects: list = []

        def bulk_apply() -> None:
            bulk_paint()
            for g in dcg.items:  # EVERY value in the class, incl. capped-out overflow + auto
                g.action = bulk.value
            for g, tog, paint in selects:  # sync only the toggles actually on screen
                tog.set_value(bulk.value)
                paint()
            on_change()

        bulk.on_value_change(bulk_apply)

        # Review-tier items first (the ones that actually need attention), capped.
        if review_items:
            with ui.column().classes("w-full mt-2 gap-0"):
                for g in shown_review:
                    selects.extend(_capture_row(g, on_change))
            if over_review:
                _overflow_row(over_review, dcg.key, expanded, rerender, on_change)

        # High-confidence items collapsed out of the way, also capped.
        if auto_items:
            with ui.expansion(f"{len(auto_items)} auto-accepted (high confidence)").classes("w-full mt-2"):
                with ui.column().classes("w-full gap-0"):
                    for g in shown_auto:
                        selects.extend(_capture_row(g, on_change))
                    if over_auto:
                        ui.label(f"+ {len(over_auto)} more auto-accepted not shown").classes(
                            "az-muted text-xs px-1 py-1"
                        )


def _overflow_row(items: list, key: str, expanded: set, rerender: Callable, on_change: Callable) -> None:
    """Summary row for the review-tier values beyond the render cap: a bulk action
    for all of them at once, and a 'Show all' that renders the full class."""
    with ui.row().classes("az-row items-center gap-3 w-full py-1 px-1"):
        ui.label(f"+ {len(items)} more value(s) not shown").classes("az-muted text-xs flex-grow")
        ui.label("set these to:").classes("az-muted text-xs")
        tog, _paint = _action_toggle(_dominant_action(items))

        def set_overflow(_e=None, items=items, t=tog) -> None:
            for g in items:
                g.action = t.value
            on_change()

        tog.on_value_change(set_overflow)

        def show_all(k=key) -> None:
            expanded.add(k)
            rerender()

        ui.button("Show all", on_click=show_all).props("flat dense").classes("text-xs")


def _capture_row(g: GroupedFinding, on_change: Callable) -> list:
    """Renders a value row and returns [(g, toggle)] so bulk actions can update
    the visible per-value toggles."""
    captured: list = []
    with ui.row().classes("az-row items-center gap-3 w-full py-1 px-1"):
        with ui.column().classes("gap-0 flex-grow min-w-0"):
            # Full value + context on hover: a redaction decision rests on the
            # exact string and its surroundings, so the truncated row must never
            # be the only thing the reviewer can see.
            ui.label(g.value[:80]).classes("az-mono text-sm truncate").tooltip(g.value)
            ui.label(g.context).classes("az-muted text-xs truncate").tooltip(g.context)
        # The detected category (PERSON / TOOL / DEPT / IBAN / ...), so the
        # reviewer can see WHAT kind of thing each value is -- especially the new
        # topical categories (tools, divisions, projects) vs. personal entities.
        theme.chip(token_label(g.entity_type), theme.SECONDARY).tooltip(g.entity_type)
        if g.validated is True:
            theme.chip("✓ valid", theme.TIER_COLORS["high"])
        elif g.validated is False:
            theme.chip("unverified", theme.SENSITIVITY_COLORS["low"])
        ui.label(f"×{g.count}").classes("az-muted text-xs w-8 text-right")
        ui.label(f"{g.max_score:.2f}").classes("az-muted text-xs w-10 text-right").tooltip(
            "Detection confidence (1.00 = certain)"
        )
        tog, paint = _action_toggle(g.action)

        def changed(_e=None, g=g, tog=tog, paint=paint) -> None:
            g.action = tog.value
            paint()
            on_change()

        tog.on_value_change(changed)
        captured.append((g, tog, paint))
    return captured


def _dominant_action(items: list[GroupedFinding]) -> str:
    counts: dict[str, int] = {}
    for g in items:
        counts[g.action] = counts.get(g.action, 0) + 1
    return max(counts, key=counts.get) if counts else "pseudonymize"


def render_review(
    container, result: ScanResult, on_change: Callable,
    column_policies: dict | None = None, cell_policies: dict | None = None,
) -> None:
    """Two-pane clustered master/detail. LEFT: a fixed list of clusters (an
    Overview, each data class, Columns, Possible misses) with counts. RIGHT:
    only the selected cluster's detail. This replaces a single long scrolling
    column -- on a document with thousands of findings dominated by one noisy
    cluster, you pick a cluster and see only its (capped) items, instead of
    scrolling past everything to reach anything."""
    container.clear()
    with container:
        if not result.all_actionable() and not result.possible_misses:
            ui.label("No sensitive data detected in this document.").classes("az-muted")
            return

        # State that must survive the in-place re-render (mutate + re-render),
        # but reset when a new file is scanned into a fresh ScanResult.
        if not hasattr(result, "_expanded_classes"):
            result._expanded_classes = set()

        clusters = _build_clusters(result, column_policies, cell_policies)
        keys = [c["key"] for c in clusters]
        if getattr(result, "_selected_cluster", None) not in keys:
            result._selected_cluster = keys[0]

        def rerender() -> None:
            render_review(container, result, on_change, column_policies, cell_policies)

        with ui.row().classes("w-full gap-4 items-start flex-nowrap az-review-split"):
            with ui.column().classes("az-cluster-rail gap-1").style("flex:0 0 236px; max-width:236px;"):
                for c in clusters:
                    _cluster_nav_item(c, result, rerender)
            with ui.column().classes("flex-grow min-w-0 gap-3 az-scroll pr-1"):
                sel = next(c for c in clusters if c["key"] == result._selected_cluster)
                _render_cluster_detail(sel, result, on_change, column_policies, cell_policies, container, rerender)


def _build_clusters(result: ScanResult, column_policies: dict | None, cell_policies: dict | None) -> list[dict]:
    """The left-rail entries, in reading order: an always-present Overview
    (bulk + confidence + stats), one per data class, then Columns, Cells, and
    Possible misses when they apply."""
    clusters: list[dict] = [
        {"key": "overview", "label": "Overview", "count": len(result.all_actionable()), "kind": "overview"}
    ]
    for dcg in result.groups:
        clusters.append({"key": f"class:{dcg.key}", "label": dcg.display, "count": len(dcg.items), "kind": "class", "dcg": dcg})
    if result.columns and column_policies is not None:
        clusters.append({"key": "columns", "label": "Columns", "count": len(result.columns), "kind": "columns"})
    if result.cells and cell_policies is not None:
        clusters.append({"key": "cells", "label": "Cells", "count": len(result.cells), "kind": "cells"})
    if result.possible_misses:
        clusters.append({"key": "misses", "label": "Possible misses", "count": len(result.possible_misses), "kind": "misses"})
    return clusters


def _cluster_nav_item(c: dict, result: ScanResult, rerender: Callable) -> None:
    active = c["key"] == result._selected_cluster
    cls = "az-cluster-nav w-full items-center justify-between px-3 py-2" + (" az-cluster-nav--active" if active else "")
    row = ui.row().classes(cls)
    with row:
        ui.label(c["label"]).classes("text-sm truncate")
        ui.label(str(c["count"])).classes("az-muted text-xs")

    def select(_e=None, k=c["key"]) -> None:
        result._selected_cluster = k
        rerender()

    row.on("click", select)


def _render_cluster_detail(
    c: dict, result: ScanResult, on_change: Callable, column_policies: dict | None,
    cell_policies: dict | None, container, rerender: Callable,
) -> None:
    kind = c["kind"]
    if kind == "overview":
        _render_overview(result, on_change, column_policies, container)
    elif kind == "class":
        _class_card(c["dcg"], on_change, result._expanded_classes, rerender)
    elif kind == "columns":
        _columns_panel(result, column_policies, container, on_change)
    elif kind == "cells":
        _cells_panel(result, cell_policies, on_change)
    elif kind == "misses":
        _possible_misses_card(result.possible_misses)


def _render_overview(result: ScanResult, on_change: Callable, column_policies: dict | None, container) -> None:
    _stat_bar(result)
    with ui.element("div").classes("az-card w-full"):
        with ui.row().classes("items-center gap-2 w-full flex-wrap"):
            ui.label("Apply to everything:").classes("az-muted text-xs")
            for action in ACTIONS:
                ui.button(
                    action, on_click=lambda a=action: _set_all(result, a, container, on_change, column_policies)
                ).props("flat dense").classes("text-xs")
        # By-confidence bulk bands: accept high, review medium, glance-and-decide low.
        _tier_bands(result, container, on_change, column_policies)


def _columns_panel(result: ScanResult, column_policies: dict, container, on_change: Callable) -> None:
    """One row per spreadsheet column with a whole-column policy toggle. A blackout
    policy redacts EVERY non-empty cell in the column -- the only way to cover a
    column that is sensitive by topic (a project description) rather than by a
    detectable entity, and the fastest way to decide a high-cardinality column
    (5000 unique customer numbers -> one decision)."""
    from itertools import groupby

    with ui.element("div").classes("az-card w-full"):
        with ui.row().classes("items-center gap-2"):
            ui.icon("view_column", size="1.2rem").classes("az-muted")
            ui.label("Columns").classes("az-h2")
        ui.label(
            "Redact or pseudonymize an ENTIRE column — every non-empty cell, even ones with no detected "
            "PII. Use for sensitive project/description columns, or to decide a whole ID column at once. "
            "Pseudonymize keeps a consistent token per value so lookups still work."
        ).classes("az-muted text-xs mb-2")
        for sheet, cols in groupby(result.columns, key=lambda c: c.sheet):
            ui.label(sheet).classes("az-kicker mt-1")
            for c in cols:
                with ui.row().classes("az-row items-center gap-3 w-full py-1 px-1"):
                    with ui.column().classes("gap-0 flex-grow min-w-0"):
                        title = f"{c.column} · {c.header}" if c.header else c.column
                        ui.label(title).classes("text-sm truncate").tooltip(c.sample or title)
                        if c.sample:
                            ui.label(f"e.g. {c.sample[:48]}").classes("az-muted text-xs truncate").tooltip(c.sample)
                    if c.name_override:
                        theme.chip("name override", theme.SECONDARY).tooltip(
                            "This header matched the people-column list, so every name-shaped cell in "
                            "this column is claimed as a person regardless of what detection found. If "
                            "this column isn't actually names, rename the header and re-scan."
                        )
                    if c.pii_count:
                        theme.chip(f"{c.pii_count} PII", theme.WARNING)
                    tog, paint = _labeled_toggle(_COLUMN_LABELS, _COLUMN_QCOLOR, column_policies.get(c.key, "skip"))

                    def changed(_e=None, key=c.key, t=tog, p=paint) -> None:
                        if t.value == "skip":
                            column_policies.pop(key, None)
                        else:
                            column_policies[key] = t.value
                        p()
                        on_change()

                    tog.on_value_change(changed)


_CELLS_CAP = 100


def _cells_panel(result: ScanResult, cell_policies: dict, on_change: Callable) -> None:
    """The per-cell EXCEPTION layer: one row per flagged cell (Sheet!Coord) with
    its own mode toggle, plus an add-by-reference input for a cell detection
    didn't flag. A cell policy wins over the column policy and per-value decision
    for that single cell -- the finest granularity, between whole-column and
    per-value. Capped for responsiveness."""
    with ui.element("div").classes("az-card w-full"):
        with ui.row().classes("items-center gap-2"):
            ui.icon("grid_on", size="1.2rem").classes("az-muted")
            ui.label("Cells").classes("az-h2")
        ui.label(
            "Override a SINGLE cell — redact, pseudonymize, or summarize just this cell, even if its "
            "column isn't covered. A cell decision wins over the column and per-value decisions. "
            "Add a cell that wasn't flagged by its reference (e.g. Tabelle1!C7)."
        ).classes("az-muted text-xs mb-2")

        # Add-by-reference: mark a cell detection never flagged.
        with ui.row().classes("items-center gap-2 w-full mb-2"):
            ref = ui.input(placeholder="Sheet!C7").props("dense outlined").classes("w-40")
            mode = ui.select(
                {"anonymize": "Redact", "pseudonymize": "Pseudonym", "summarize": "Summarize"}, value="anonymize"
            ).props("dense outlined")

            def add_ref(_e=None) -> None:
                key = (ref.value or "").strip()
                if "!" in key:
                    cell_policies[key] = mode.value
                    ref.value = ""
                    on_change()

            ui.button("Add cell", icon="add", on_click=add_ref).props("flat dense").classes("text-xs")

        shown = result.cells[:_CELLS_CAP]
        for c in shown:
            with ui.row().classes("az-row items-center gap-3 w-full py-1 px-1"):
                with ui.column().classes("gap-0 flex-grow min-w-0"):
                    ui.label(c.key).classes("az-mono text-sm truncate")
                    if c.sample:
                        ui.label(c.sample[:60]).classes("az-muted text-xs truncate").tooltip(c.sample)
                if c.entity_types:
                    theme.chip(", ".join(c.entity_types)[:24], theme.SECONDARY)
                tog, paint = _labeled_toggle(_COLUMN_LABELS, _COLUMN_QCOLOR, cell_policies.get(c.key, "skip"))

                def changed(_e=None, key=c.key, t=tog, p=paint) -> None:
                    if t.value == "skip":
                        cell_policies.pop(key, None)
                    else:
                        cell_policies[key] = t.value
                    p()
                    on_change()

                tog.on_value_change(changed)
        if len(result.cells) > _CELLS_CAP:
            ui.label(f"+ {len(result.cells) - _CELLS_CAP} more flagged cells not shown (add by reference above)").classes(
                "az-muted text-xs mt-1"
            )


def _tier_bands(result: ScanResult, container, on_change: Callable, column_policies: dict | None) -> None:
    """Bulk-set every finding of a confidence tier at once. Low is offered but NEVER
    auto-applied -- a failed-checksum ID is demoted to low yet still identifying, so
    the reviewer must glance before skipping it.

    Medium is further split by SOURCE: a pattern/checksum-anchored hit that
    merely sits under the high-tier bar is a different kind of uncertain than
    a bare spaCy NER guess with nothing else corroborating it -- one band
    hid that distinction, making "how much of this Medium bucket is just the
    model guessing" impossible to see at a glance."""
    items = result.all_actionable()
    by_tier = {tier: [g for g in items if g.tier == tier] for tier, _ in _TIER_BANDS}
    if not any(by_tier.values()):
        return
    with ui.row().classes("items-center gap-4 w-full flex-wrap"):
        ui.label("By confidence:").classes("az-muted text-xs")
        for tier, label in _TIER_BANDS:
            gs = by_tier[tier]
            if not gs:
                continue
            if tier == "medium":
                pattern_backed = [g for g in gs if not g.is_ner_guess]
                ner_guess = [g for g in gs if g.is_ner_guess]
                if pattern_backed:
                    _tier_band(f"{label} ({len(pattern_backed)})", pattern_backed, result, container, on_change, column_policies)
                if ner_guess:
                    _tier_band(
                        f"{label} · NER guess ({len(ner_guess)})", ner_guess, result, container, on_change,
                        column_policies,
                    )
                continue
            _tier_band(f"{label} ({len(gs)})", gs, result, container, on_change, column_policies)


def _tier_band(
    label: str, gs: list[GroupedFinding], result: ScanResult, container, on_change: Callable, column_policies: dict | None
) -> None:
    with ui.row().classes("items-center gap-1"):
        ui.label(label).classes("az-muted text-xs")
        tog, _paint = _action_toggle(_dominant_action(gs))

        def apply(_e=None, gs=gs, t=tog) -> None:
            _set_all_items(gs, t.value, result, container, on_change, column_policies)

        tog.on_value_change(apply)


def _set_all(result: ScanResult, action: str, container, on_change: Callable, column_policies: dict | None = None) -> None:
    _set_all_items(result.all_actionable(), action, result, container, on_change, column_policies)


def _set_all_items(
    items: list[GroupedFinding],
    action: str,
    result: ScanResult,
    container,
    on_change: Callable,
    column_policies: dict | None,
) -> None:
    for g in items:
        g.action = action
    render_review(container, result, on_change, column_policies)
    on_change()


def _stat_bar(result: ScanResult) -> None:
    """"To review" is the reviewer's actual workload, so it is the hero. The
    likely-PII vs model-guess split is the triage signal: it tells the reviewer
    how much of the workload is trustworthy findings vs. bare NER guesses they
    can clear in bulk via the "NER guess" confidence band."""
    s = result.stats
    with ui.row().classes("az-card w-full items-center justify-around gap-4"):
        _stat(s.get("needs_review", 0), "to review", theme.WARNING, hero=True)
        _stat(s.get("likely_pii", 0), "likely PII", theme.PRIMARY)
        _stat(s.get("model_guess", 0), "model guesses", theme.SECONDARY)
        _stat(s.get("auto_accept", 0), "auto-accepted")
        _stat(s.get("possible_misses", 0), "possible misses", theme.SECONDARY)


def _stat(n: int, label: str, color: str | None = None, *, hero: bool = False) -> None:
    with ui.element("div").classes("az-stat" + (" az-stat-hero" if hero else "")):
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
