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

# Compact labels + the Quasar brand colour each action lights up in. A segmented
# toggle (not a dropdown) keeps every row's decision visible at a glance and one
# click to change -- with a column of dropdowns you cannot scan what will happen
# to each value without opening them one by one.
_ACTION_LABELS = {"pseudonymize": "Pseudonym", "anonymize": "Anonymize", "skip": "Skip"}
_ACTION_QCOLOR = {"pseudonymize": "primary", "anonymize": "negative", "skip": "grey-7"}

# Whole-column policy (spreadsheets): "none" leaves the column to per-value review;
# the others black out EVERY non-empty cell in the column (see xlsx_handler).
_COLUMN_LABELS = {"none": "Keep", "pseudonymize": "Pseudonym", "anonymize": "Anonymize"}
_COLUMN_QCOLOR = {"none": "grey-7", "pseudonymize": "primary", "anonymize": "negative"}

# Trust tiers, most-confident first, for the by-confidence bulk bands.
_TIER_BANDS = [("high", "High confidence"), ("medium", "Medium"), ("low", "Low")]

# Max per-value rows rendered per class before the rest is summarized. Each row is
# a live segmented toggle, so rendering thousands at once (a "database" workbook)
# stalls the screen. The overflow stays fully decidable (bulk) and expandable.
_REVIEW_CAP = 100


def _action_toggle(initial: str):
    tog = ui.toggle(_ACTION_LABELS, value=initial).props("dense unelevated no-caps")

    def paint() -> None:
        tog.props(f"toggle-color={_ACTION_QCOLOR.get(tog.value, 'primary')}")

    paint()
    return tog, paint


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
        tog = ui.toggle(_ACTION_LABELS, value=_dominant_action(items)).props("dense unelevated no-caps")

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


def render_review(container, result: ScanResult, on_change: Callable, column_policies: dict | None = None) -> None:
    container.clear()
    with container:
        if not result.all_actionable() and not result.possible_misses:
            ui.label("No sensitive data detected in this document.").classes("az-muted")
            return

        _stat_bar(result)

        # Whole-column policies (spreadsheets) -- the fastest lever at scale: one
        # decision per column instead of thousands per value.
        if result.columns and column_policies is not None:
            _columns_panel(result, column_policies, container, on_change)

        # Global bulk actions.
        with ui.row().classes("items-center gap-2 w-full"):
            ui.label("Apply to everything:").classes("az-muted text-xs")
            for action in ACTIONS:
                ui.button(
                    action, on_click=lambda a=action: _set_all(result, a, container, on_change, column_policies)
                ).props("flat dense").classes("text-xs")

        # By-confidence bulk bands: accept high, review medium, glance-and-decide low.
        _tier_bands(result, container, on_change, column_policies)

        # Per-class rows are capped for responsiveness; a class expands on demand.
        # Expanded state rides on the ScanResult so it survives the in-place
        # re-render (decisions mutate + re-render) but resets when a new file is
        # scanned into a fresh ScanResult.
        if not hasattr(result, "_expanded_classes"):
            result._expanded_classes = set()

        def rerender() -> None:
            render_review(container, result, on_change, column_policies)

        with ui.column().classes("w-full gap-3 az-scroll pr-1"):
            for dcg in result.groups:
                _class_card(dcg, on_change, result._expanded_classes, rerender)

            if result.possible_misses:
                _possible_misses_card(result.possible_misses)


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
                    if c.pii_count:
                        theme.chip(f"{c.pii_count} PII", theme.WARNING)
                    tog = ui.toggle(_COLUMN_LABELS, value=column_policies.get(c.key, "none")).props(
                        "dense unelevated no-caps"
                    )

                    def paint(t=tog) -> None:
                        t.props(f"toggle-color={_COLUMN_QCOLOR.get(t.value, 'grey-7')}")

                    paint()

                    def changed(_e=None, key=c.key, t=tog, p=paint) -> None:
                        if t.value == "none":
                            column_policies.pop(key, None)
                        else:
                            column_policies[key] = t.value
                        p()
                        on_change()

                    tog.on_value_change(changed)


def _tier_bands(result: ScanResult, container, on_change: Callable, column_policies: dict | None) -> None:
    """Bulk-set every finding of a confidence tier at once. Low is offered but NEVER
    auto-applied -- a failed-checksum ID is demoted to low yet still identifying, so
    the reviewer must glance before skipping it."""
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
            with ui.row().classes("items-center gap-1"):
                ui.label(f"{label} ({len(gs)})").classes("az-muted text-xs")
                tog = ui.toggle(_ACTION_LABELS, value=_dominant_action(gs)).props("dense unelevated no-caps")

                def apply(_e=None, tier=tier, t=tog) -> None:
                    _set_all_tier(result, tier, t.value, container, on_change, column_policies)

                tog.on_value_change(apply)


def _set_all(result: ScanResult, action: str, container, on_change: Callable, column_policies: dict | None = None) -> None:
    for g in result.all_actionable():
        g.action = action
    render_review(container, result, on_change, column_policies)
    on_change()


def _set_all_tier(
    result: ScanResult, tier: str, action: str, container, on_change: Callable, column_policies: dict | None
) -> None:
    for g in result.all_actionable():
        if g.tier == tier:
            g.action = action
    render_review(container, result, on_change, column_policies)
    on_change()


def _stat_bar(result: ScanResult) -> None:
    """"To review" is the reviewer's actual workload, so it is the hero; the
    other three are context and are demoted."""
    s = result.stats
    with ui.row().classes("az-card w-full items-center justify-around gap-4"):
        _stat(s.get("needs_review", 0), "to review", theme.WARNING, hero=True)
        _stat(s.get("auto_accept", 0), "auto-accepted", theme.PRIMARY)
        _stat(s.get("distinct_findings", 0), "distinct findings")
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
