"""Guards for the review screen -- the human-decision surface.

Here a control that silently does nothing is worse than no control at all: the
reviewer sets "Set all: pseudonymize", believes the whole category is covered,
and ships a document in which the minority rows were never touched. Every test
below therefore asserts on the DECISIONS a Save would act on, not on pixels.

The screen is driven headlessly: NiceGUI elements attach to the auto-index
client in a plain process, so `render_review` can be built and its event
listeners fired without a browser. (pytest-asyncio is deliberately not a
dependency of this project, so NiceGUI's async `User` fixture is out of reach --
and `Client.auto_index_client` does not exist in NiceGUI 3.14.)
"""

from __future__ import annotations

import pytest
from nicegui import context as ng_context
from nicegui import core as ng_core
from nicegui import ui

from anonymizer import audit as audit_mod
from anonymizer.gui import app as gui_app
from anonymizer.gui import review, settings_page
from anonymizer.mapping import MappingLockError, MappingRekeyError
from anonymizer.models import ColumnInfo, DataClassGroup, FileJob, GroupedFinding, ScanResult

# --- headless UI driving -----------------------------------------------------


@pytest.fixture(autouse=True)
def _fail_on_swallowed_handler_exception():
    """NiceGUI routes any exception raised inside an event handler to
    `app.handle_exception` and carries on, which makes a CRASHING bulk handler
    indistinguishable from one that silently did nothing -- the exact defect class
    this file exists to catch. Surface those exceptions as test failures."""
    caught: list[Exception] = []
    ng_core.app.on_exception(caught.append)
    try:
        yield
    finally:
        ng_core.app._exception_handlers.remove(caught.append)
    assert not caught, f"an event handler raised: {caught!r}"


@pytest.fixture
def dialogs():
    """Returns the dialogs THIS test opened.

    A `ui.dialog` does not attach to the slot it is written in -- NiceGUI parents
    every dialog on the client layout (elements/dialog.py) -- so a headless test
    cannot find it by walking the container it was declared in."""
    layout = ng_context.client.layout
    before = {id(d) for d in layout.descendants()}

    def opened():
        return [d for d in layout.descendants() if isinstance(d, ui.dialog) and id(d) not in before]

    return opened


@pytest.fixture
def notifications(monkeypatch):
    """Captures every `ui.notify(...)` as (message, type).

    A toast is the ONLY channel these screens have to the operator: this is an
    air-gapped kiosk, so a traceback in a server log reaches nobody. Tests here
    assert on what the toast actually says."""
    seen: list[tuple[str, str]] = []

    def fake_notify(message="", *args, **kwargs):
        seen.append((str(message), kwargs.get("type", "")))

    monkeypatch.setattr(ui, "notify", fake_notify)
    return seen


def _all(el):
    return [el, *el.descendants()]


def _texts(el) -> str:
    """Everything the element tree renders as text (labels, button labels, chips),
    flattened -- so a test can assert what the reviewer actually reads."""
    out: list[str] = []
    for d in _all(el):
        for candidate in (getattr(d, "_text", None), d._props.get("label"), d._props.get("innerHTML")):
            if isinstance(candidate, str) and candidate:
                out.append(candidate)
    return " | ".join(out)


def _click(button) -> None:
    """Fire a button exactly the way the browser's click message does."""
    button._handle_event({"listener_id": next(iter(button._event_listeners)), "args": None})


def _click_labeled(scope, label: str) -> None:
    for d in _all(scope):
        if isinstance(d, ui.button) and d._props.get("label") == label:
            _click(d)
            return
    raise AssertionError(f"no button labelled {label!r} was rendered")


def _row_of(scope, label_prefix: str, *, up: int = 1):
    """The element `up` levels above the label starting with `label_prefix` -- i.e.
    the control row that label introduces."""
    for d in _all(scope):
        if isinstance(d, ui.label) and (d._text or "").startswith(label_prefix):
            for _ in range(up):
                d = d.parent_slot.parent
            return d
    raise AssertionError(f"no label starting with {label_prefix!r} was rendered")


def _activate(row, action: str) -> None:
    """Pick `action` on whatever control this bulk row exposes, reproducing the
    browser's behaviour faithfully -- the test must not paper over the bug it is
    hunting.

    Two layers suppress a same-value selection, and BOTH are reproduced here:
      * Quasar's QBtnToggle emits `update:modelValue` ONLY when the clicked value
        DIFFERS from the one displayed (`set2()` in quasar.umd.js; a non-clearable
        toggle swallows a click on the already-selected segment entirely).
      * NiceGUI's `BindableProperty.__set__` returns before invoking any change
        handler when the assigned value is unchanged (binding.py).
    Calling `set_value()` directly instead would hide both.
    """
    labels = {review._ACTION_LABELS.get(action, action), action}
    for d in _all(row):
        if isinstance(d, ui.button) and d._props.get("label") in labels:
            _click(d)
            return
        if isinstance(d, ui.toggle) and action in d._values:
            if d.value == action:
                return  # Quasar sends nothing at all -- a no-op, faithfully
            d._handle_event(
                {"listener_id": next(iter(d._event_listeners)), "args": d._values.index(action)}
            )
            return
    raise AssertionError(f"this row exposes no control for {action!r}")


def _stat_tiles(scope) -> dict[str, int]:
    """The header's stat tiles as {label: number}, read back off the rendered DOM."""
    tiles: dict[str, int] = {}
    for d in _all(scope):
        if "az-stat" in getattr(d, "_classes", []):
            labels = [c for c in d.descendants() if isinstance(c, ui.label)]
            if len(labels) == 2 and (labels[0]._text or "").isdigit():
                tiles[labels[1]._text or ""] = int(labels[0]._text)
    return tiles


# --- fixtures ----------------------------------------------------------------


def _finding(value: str, action: str = "pseudonymize", tier: str = "medium", score: float = 0.85):
    return GroupedFinding(
        entity_type="PERSON",
        value=value,
        count=1,
        max_score=score,
        context=f"...{value}...",
        action=action,
        tier=tier,
    )


def _result(items, **kwargs) -> ScanResult:
    items = list(items)
    dcg = DataClassGroup(key="people", display="People", sensitivity="high", items=items)
    high = sum(1 for g in items if g.tier == "high")
    stats = {
        "units_scanned": 1,
        "distinct_findings": len(items),
        "total_occurrences": len(items),
        "auto_accept": high,
        "needs_review": len(items) - high,
        "possible_misses": 0,
    }
    return ScanResult(groups=[dcg], stats=stats, **kwargs)


def _render(result, column_policies=None):
    box = ui.column()
    review.render_review(box, result, lambda: None, column_policies if column_policies is not None else {})
    return box


# --- D1: bulk controls must never silently no-op -----------------------------


def test_class_bulk_applies_when_the_action_is_already_the_dominant_one():
    """D1: the per-class "Set all" must apply on EVERY pick, including the value it
    is already showing.

    It used to be a segmented toggle initialised to the class's dominant action, so
    clicking that action was swallowed by Quasar and the handler never ran. The
    reviewer saw "Pseudonym" highlighted, clicked it to push it across the class,
    and every row that DIFFERED silently kept its old action -- values believed to
    be pseudonymized were saved as `skip`, i.e. shipped in the clear.
    """
    items = [_finding("Klaus"), _finding("Anna"), _finding("Bernd"), _finding("Erika", action="skip")]
    box = _render(_result(items))

    _activate(_row_of(box, "Set all"), "pseudonymize")

    assert [g.action for g in items] == ["pseudonymize"] * 4


def test_tier_band_applies_when_the_action_is_already_the_dominant_one():
    """D1: same defect shape on the by-confidence bulk bands. A band initialised to
    its own dominant action cannot be used to enforce that action on the band."""
    items = [_finding("Klaus"), _finding("Anna"), _finding("Bernd"), _finding("Erika", action="skip")]
    box = _render(_result(items))

    _activate(_row_of(box, "Medium"), "pseudonymize")

    assert [g.action for g in items] == ["pseudonymize"] * 4


def test_overflow_bulk_applies_when_the_action_is_already_the_dominant_one():
    """D1: same defect shape on the capped-overflow "set these to" row -- and this
    one is the only control that can reach those values at all, because they are not
    rendered. A no-op here leaves values the reviewer can neither see nor fix."""
    items = [_finding(f"Name {i}") for i in range(review._REVIEW_CAP + 20)]
    items[-1].action = "skip"  # beyond the render cap: only the summary row reaches it
    box = _render(_result(items))

    _activate(_row_of(box, "set these to"), "pseudonymize")

    assert items[-1].action == "pseudonymize"
    assert {g.action for g in items} == {"pseudonymize"}


# --- bulk controls must reach the values that are NOT on screen ---------------


def test_class_bulk_reaches_capped_overflow_and_auto_accepted_items():
    """The class bulk promises "every value in this category, including hidden
    overflow and auto-accepted ones". Only the first _REVIEW_CAP rows are rendered,
    so a bulk that walked the rendered toggles instead of the model would leave the
    invisible majority untouched."""
    review_items = [_finding(f"Name {i}") for i in range(review._REVIEW_CAP + 50)]
    auto_items = [_finding(f"Auto {i}", tier="high", score=0.99) for i in range(5)]
    items = review_items + auto_items
    box = _render(_result(items))

    _activate(_row_of(box, "Set all"), "anonymize")

    assert {g.action for g in items} == {"anonymize"}


def test_tier_band_reaches_items_beyond_the_render_cap():
    """A tier band claims the whole tier; the render cap must not shrink it."""
    items = [_finding(f"Name {i}") for i in range(review._REVIEW_CAP + 200)]
    box = _render(_result(items))

    _activate(_row_of(box, "Medium"), "anonymize")

    assert {g.action for g in items} == {"anonymize"}


def test_overflow_bulk_touches_only_the_hidden_values():
    """"Set these to X" means the summarized overflow, not the whole class -- the
    rows the reviewer already decided on screen must survive it."""
    items = [_finding(f"Name {i}") for i in range(review._REVIEW_CAP + 200)]
    box = _render(_result(items))

    _activate(_row_of(box, "set these to"), "anonymize")

    assert {g.action for g in items[: review._REVIEW_CAP]} == {"pseudonymize"}
    assert {g.action for g in items[review._REVIEW_CAP :]} == {"anonymize"}


def test_column_policy_toggle_keeps_its_value_and_records_every_change():
    """The Columns panel deliberately keeps a stateful toggle: unlike a bulk
    control, it displays ONE column's real policy, so a click on the value already
    shown is a genuine "it is already that" and dropping it loses no instruction.
    What must hold is that every CHANGE lands in the dict the xlsx handler reads --
    a policy that never reaches `config["column_policies"]` is a column the
    reviewer believes is blacked out and which ships in the clear."""
    result = _result(
        [_finding("Klaus")],
        columns=[
            ColumnInfo(sheet="Kunden", column="B", header="Kundenname", sample="Klaus", pii_count=1),
            ColumnInfo(sheet="Kunden", column="C", header="Notiz", sample="privat", pii_count=0),
        ],
    )
    policies: dict = {}
    box = _render(result, column_policies=policies)

    _activate(_row_of(box, "C · Notiz", up=2), "anonymize")
    assert policies == {"Kunden!C": "anonymize"}

    _activate(_row_of(box, "C · Notiz", up=2), "none")
    assert policies == {}, "clearing a column policy must remove it, not leave a stale blackout"


# --- D4: the header must not contradict the decisions ------------------------


def test_stat_bar_is_recomputed_after_a_class_bulk_action():
    """D4: the header is the last thing the reviewer reads before pressing Save.

    "auto-accepted" is a DECISION word, but it was read once from the frozen scan
    stats dict -- so after the reviewer bulk-skipped the class the header still
    advertised those values as accepted. The per-class bulk deliberately does NOT
    re-render (10k live toggles), so the header has to be refreshed on its own.
    """
    items = [
        _finding("Auto 1", tier="high", score=0.99),
        _finding("Auto 2", tier="high", score=0.99),
        _finding("Klaus"),
        _finding("Anna"),
    ]
    box = _render(_result(items))
    assert _stat_tiles(box)["auto-accepted"] == 2

    _activate(_row_of(box, "Set all"), "skip")

    assert {g.action for g in items} == {"skip"}
    assert _stat_tiles(box)["auto-accepted"] == 0


def test_stat_bar_is_recomputed_after_a_global_bulk_action():
    """D4: same for "Apply to everything", which does re-render."""
    items = [
        _finding("Auto 1", tier="high", score=0.99),
        _finding("Klaus"),
    ]
    box = _render(_result(items))
    assert _stat_tiles(box)["auto-accepted"] == 1

    _activate(_row_of(box, "Apply to everything"), "skip")

    assert _stat_tiles(box)["auto-accepted"] == 0


# --- D3: Clear must be confirmed ---------------------------------------------


def test_clear_asks_before_discarding_the_queue(dialogs):
    """D3: "Clear" sits one flat button away from "Save all" and used to wipe the
    queue instantly -- discarding an hour of triage decisions with no undo and no
    warning. It must ask first, and the decisions must survive the ask."""
    state = gui_app.PageState()
    state.jobs = [FileJob(path="C:/nowhere/a.docx", status="review"), FileJob(path="C:/nowhere/b.docx", status="review")]
    state.refresh = lambda: None
    state.save_all = lambda: None
    queue = ui.column()
    gui_app._render_queue(queue, state, lambda i: None)

    _click_labeled(queue, "Clear")

    assert len(state.jobs) == 2, "Clear discarded the queue without asking"
    assert dialogs(), "Clear neither asked nor cleared -- the button does nothing"

    for dlg in dialogs():  # the confirmation
        _click_labeled(dlg, "Clear queue")

    assert state.jobs == []


# --- D2: the preview must not be blind to whole-column policies --------------


def _xlsx_job(policy: str | None, value_action: str = "skip") -> FileJob:
    scan = ScanResult(
        groups=[
            DataClassGroup(
                key="people",
                display="People",
                sensitivity="high",
                items=[_finding("Klaus Mueller", action=value_action)],
            )
        ],
        stats={"distinct_findings": 1, "auto_accept": 0, "needs_review": 1, "possible_misses": 0},
        columns=[
            ColumnInfo(sheet="Kunden", column="B", header="Kundenname", sample="Klaus Mueller", pii_count=3),
            ColumnInfo(sheet="Kunden", column="C", header="Notiz", sample="privat", pii_count=0),
        ],
    )
    job = FileJob(path="C:/nowhere/db.xlsx", status="review", scan=scan)
    job.config = {"column_policies": ({"Kunden!B": policy} if policy else {})}
    return job


def _preview_text(job, dialogs) -> str:
    gui_app._preview_dialog(job)
    shown = " | ".join(_texts(d) for d in dialogs())
    assert shown, "the preview dialog rendered nothing at all"
    return shown


def test_preview_shows_whole_column_blackout_policies(dialogs):
    """D2: a whole-column policy blacks out EVERY non-empty cell in the column at
    apply time (xlsx_handler), including cells no recognizer flagged, and it
    overrides the per-value decisions. It is the single highest-impact decision on
    the screen -- and the preview showed nothing of it, so the reviewer committed to
    it blind."""
    shown = _preview_text(_xlsx_job("anonymize"), dialogs)

    assert "Kunden!B" in shown, "the preview does not name the column being blacked out"
    assert "Kundenname" in shown, "the preview does not show the column's header"
    assert "Kunden!C" not in shown, "a column with no policy must not be listed as changing"


def test_preview_does_not_claim_nothing_changes_when_a_column_is_blacked_out(dialogs):
    """D2, the dangerous half: with every per-value decision set to skip the preview
    said "Nothing selected for redaction" -- while the save was about to black out a
    whole column. A preview that contradicts the save is worse than no preview."""
    shown = _preview_text(_xlsx_job("anonymize", value_action="skip"), dialogs)

    assert "Nothing selected for redaction" not in shown


def test_preview_shows_column_policies_alongside_the_per_value_changes(dialogs):
    """Both halves of the save plan have to appear together -- the column blackout
    OVERRIDES the per-value decisions inside that column, so showing only one of
    them misrepresents what the file will look like."""
    shown = _preview_text(_xlsx_job("pseudonymize", value_action="anonymize"), dialogs)

    assert "Kunden!B" in shown
    assert "Klaus Mueller" in shown


def test_preview_still_reports_an_empty_plan_when_nothing_is_selected(dialogs):
    """The counterpart: with no column policy and everything skipped, the preview
    must still say so rather than imply changes."""
    shown = _preview_text(_xlsx_job(None, value_action="skip"), dialogs)

    assert "Nothing selected for redaction" in shown


def test_preview_ignores_a_column_policy_of_none(dialogs):
    """D2 residual: only ("pseudonymize", "anonymize") black a column out
    (`xlsx_handler._COLUMN_BLACKOUT_ACTIONS`); "none" means "leave this column to
    per-value review". Rendering a "none" entry made the preview positively assert
    that every non-empty cell of that column is replaced, for a column Save does
    not touch -- the preview over-promising redaction, which is the direction that
    gets a document shipped un-checked."""
    shown = _preview_text(_xlsx_job("none", value_action="skip"), dialogs)

    assert "Kunden!B" not in shown, "a 'none' policy must not be previewed as a blackout"
    assert "Nothing selected for redaction" in shown


# --- D4: the header must not read as a to-do count ---------------------------


def test_header_does_not_advertise_decided_values_as_outstanding_work():
    """D4 residual: the "to review" tile was purely tier-derived, so after the
    reviewer had bulk-decided every uncertain value the header still advertised
    them as outstanding workload -- a header contradicting the list below it.
    Nothing in the model records "reviewed", so the count must not be WORDED as a
    to-do list."""
    items = [_finding("Klaus"), _finding("Anna")]
    box = _render(_result(items))

    _activate(_row_of(box, "Apply to everything"), "anonymize")

    assert {g.action for g in items} == {"anonymize"}
    assert "to review" not in _texts(box), (
        "every value is decided, yet the screen still calls them 'to review'"
    )


# --- D3: per-click dialogs must not accumulate -------------------------------


def test_confirm_clear_dialog_is_disposed_when_it_closes(dialogs):
    """D3 residual: NiceGUI parents every dialog on `client.layout`, not on the
    slot it was written in, so a dialog built per click lives for the whole
    session. The native webview stays open for a full working day of triage, so
    "Clear" + "Preview" leak an element tree per click."""
    state = gui_app.PageState()
    state.jobs = [FileJob(path="C:/nowhere/a.docx", status="review")]
    state.refresh = lambda: None
    state.save_all = lambda: None
    queue = ui.column()
    gui_app._render_queue(queue, state, lambda i: None)

    for _ in range(3):
        _click_labeled(queue, "Clear")
        for dlg in dialogs():
            _click_labeled(dlg, "Cancel")

    assert dialogs() == [], "closed confirmation dialogs are never removed from the client layout"
    assert len(state.jobs) == 1


def test_preview_dialog_is_disposed_when_it_closes(dialogs):
    """D3 residual, same defect on the preview dialog -- the one a reviewer opens
    most often."""
    job = _xlsx_job("anonymize")
    for _ in range(3):
        gui_app._preview_dialog(job)
        for dlg in dialogs():
            _click_labeled(dlg, "Close")

    assert dialogs() == [], "closed preview dialogs are never removed from the client layout"


# --- D1: one vocabulary for every bulk control -------------------------------


def test_global_bulk_row_uses_the_same_action_labels_as_every_other_bulk_row():
    """D1 residual: "Apply to everything" labelled its buttons with the raw action
    names while the class / tier / overflow rows use `_ACTION_LABELS`. Four bulk
    surfaces, two vocabularies, on the one screen whose entire point is that the
    reviewer must not misread a control."""
    box = _render(_result([_finding("Klaus")]))
    row = _row_of(box, "Apply to everything")

    labels = {d._props.get("label") for d in _all(row) if isinstance(d, ui.button)}

    assert labels == set(review._ACTION_LABELS.values())


# --- A-P6-UI: a restore that reversed nothing is not a success ---------------


def test_reidentify_notice_warns_when_some_tokens_could_not_be_restored():
    """A-P6-UI: the re-identify screen reported the result as
    `positive if n else info` -- so a document whose tokens could NOT be reversed
    produced a friendly "Restored 0 value(s)." and no hint that anything failed.
    Unrestorable tokens are a real, observed case (a token minted from an umlaut
    column label). Silent data loss dressed as success."""
    text = "[PERSON_1] and [PERSON_2] met [IBAN_3]."

    message, kind = gui_app._reidentify_notice(text, restored=1)

    assert kind == "warning"
    assert "2" in message, "the warning must name how many tokens were left unrestored"


def test_reidentify_notice_warns_when_nothing_at_all_was_restored():
    """The exact case round 1 reproduced: tokens present, none reversible."""
    message, kind = gui_app._reidentify_notice("Kunde [PERSON_1] hat Konto [IBAN_2].", restored=0)

    assert kind == "warning"
    assert "0 value" not in message.lower() or "not" in message.lower()


def test_reidentify_notice_reports_a_complete_restore_as_success():
    message, kind = gui_app._reidentify_notice("[PERSON_1] und [PERSON_2]", restored=2)

    assert kind == "positive"
    assert "2" in message


def test_reidentify_notice_does_not_warn_when_the_text_holds_no_tokens():
    """No pseudonym-shaped token in the text means nothing was expected to be
    restored -- that is genuinely neutral, not a failure."""
    message, kind = gui_app._reidentify_notice("Plain text with [NAME] and no numbers.", restored=0)

    assert kind == "info"
    assert "warn" not in message.lower()


@pytest.mark.parametrize(
    "source,restored",
    [
        ("Kunde [PRÜFUNG_1] und [GRÖSSE_2] blieben stehen.", 0),  # nothing restored at all
        ("[PRÜFUNG_1] und [PERSON_2]", 1),  # partially restored -- must NOT read as success
    ],
)
def test_reidentify_notice_counts_tokens_with_non_ascii_labels(source, restored):
    """SILENT-DATA-LOSS regression: the counter used actions.TOKEN_RE, whose label
    class is [A-Z0-9_] and therefore cannot match a token minted from a German
    column header ('[PRÜFUNG_1]'). That made the ONE shape which actually produces
    an unreversible document invisible to the warning -- a text of nothing but such
    tokens reported 'nothing to restore', and a mixed text reported a full green
    success over a restore that left live unreversible tokens in the document.
    Counting must use a SUPERSET of TOKEN_RE, because re-identification serves
    documents that were ALREADY archived before minting them was blocked."""
    message, kind = gui_app._reidentify_notice(source, restored=restored)

    assert kind == "warning", f"unreversible token reported as {kind!r}: {message}"
    assert "could NOT be restored" in message


def test_reidentify_notice_does_not_deny_a_one_way_token_exists():
    """A one-way token is deliberately not reversible, so 'nothing to restore' is
    the right outcome -- but the message must not tell the user there is no
    placeholder in a text whose brackets they can see on screen."""
    message, kind = gui_app._reidentify_notice("[NOTIZEN_2_REDACTED]", restored=0)

    assert kind == "info"
    assert "No reversible placeholder" in message


# --- A-P2/A-P4: a mapping failure must reach a human -------------------------


class _LockedStore:
    """Stands in for a MappingStore whose exclusive lock is held by an in-flight
    apply (minutes long on a multi-sheet workbook, same process via run.io_bound)."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        raise MappingLockError("The pseudonym mapping is in use by another operation.")

    def __exit__(self, *exc) -> bool:
        return False


class _FailingStore:
    """Opens fine, then fails on the action itself (e.g. a key rotation that could
    not re-key the encrypted allow/deny lists)."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def entry_count(self) -> int:
        return 7

    def reset(self, restart_numbering: bool = False) -> None:
        raise MappingLockError("The pseudonym mapping is in use by another operation.")

    def rotate_key(self) -> None:
        raise MappingRekeyError("Key rotated, but the encrypted deny list could not be re-keyed.")


def test_settings_page_survives_a_locked_mapping_store(notifications, monkeypatch):
    """A-P2: the Settings page opened the mapping store at BUILD time with a bare
    `with MappingStore()`. Opening Settings while an apply holds the lock waited
    ~10s and then raised mid-page-render: an unhandled exception, a half-drawn
    page, and no message at all for the operator."""
    monkeypatch.setattr(settings_page, "MappingStore", _LockedStore)
    box = ui.column()

    with box:
        settings_page._mapping_admin()  # must not raise

    assert any(kind == "negative" for _, kind in notifications), (
        "a locked mapping store produced no visible error"
    )
    assert "Reset all mappings" in _texts(box), "the page render was aborted by the failure"


def test_mapping_action_failure_is_surfaced_to_the_user(notifications, dialogs, monkeypatch):
    """A-P4: a failed key rotation showed the user NOTHING -- just a missing
    success toast, with the traceback going to a server log nobody reads on an
    air-gapped kiosk. A loud failure that reaches no human is not fail-loud."""
    monkeypatch.setattr(settings_page, "MappingStore", _FailingStore)
    monkeypatch.setattr(audit_mod, "log_event", lambda *a, **kw: None)
    box = ui.column()
    with box:
        settings_page._mapping_admin()
    notifications.clear()

    _click_labeled(box, "Rotate key")
    for dlg in dialogs():
        _click_labeled(dlg, "Confirm")

    assert any(kind == "negative" for _, kind in notifications), "a failed key rotation is invisible"
    assert any("re-key" in msg for msg, _ in notifications), "the error text never reaches the user"


def test_mapping_reset_failure_is_surfaced_to_the_user(notifications, dialogs, monkeypatch):
    """A-P4, same for the reset button: the operator must never be left believing
    a wipe happened when it did not."""
    monkeypatch.setattr(settings_page, "MappingStore", _FailingStore)
    monkeypatch.setattr(audit_mod, "log_event", lambda *a, **kw: None)
    box = ui.column()
    with box:
        settings_page._mapping_admin()
    notifications.clear()

    _click_labeled(box, "Reset all mappings")
    for dlg in dialogs():
        _click_labeled(dlg, "Confirm")

    assert any(kind == "negative" for _, kind in notifications)
    assert not any(kind == "positive" for _, kind in notifications), (
        "a failed reset still reported success"
    )


def test_failure_to_save_settings_is_surfaced_to_the_user(notifications, monkeypatch):
    """Same class as A-P4, one function over: `config_mod.save_config` writes the
    ENCRYPTED allow/deny sidecar, so it can fail on the credential store. NiceGUI
    routes a handler exception to `app.handle_exception` and carries on, so the
    operator saw no toast at all and walked away believing a just-added deny term
    was persisted -- a leak on the next scan."""
    monkeypatch.setattr(settings_page, "MappingStore", _OkStore)
    monkeypatch.setattr(settings_page.config_mod, "load_config", lambda: {"entities": {}, "sensitivity": 0.0})
    monkeypatch.setattr(settings_page.ocr_mod, "ocr_available", lambda cfg: False)
    monkeypatch.setattr(
        settings_page.config_mod,
        "save_config",
        lambda cfg: (_ for _ in ()).throw(OSError("credential store unavailable")),
    )
    box = ui.column()
    with box:
        settings_page.build()
    notifications.clear()

    _click_labeled(box, "Save settings")

    assert any(kind == "negative" for _, kind in notifications), "a failed settings save is invisible"
    assert not any(kind == "positive" for _, kind in notifications), "a failed save reported success"


# --- A-P3: the reset copy must describe what reset actually does -------------


class _OkStore(_FailingStore):
    def reset(self, restart_numbering: bool = False) -> None:
        assert restart_numbering is False, "the default reset must never restart numbering"

    def rotate_key(self) -> None:
        pass


def test_reset_never_claims_that_numbering_restarts(notifications, dialogs, monkeypatch):
    """A-P3: `reset()` deliberately does NOT restart numbering -- so that a number
    already printed in an archived document is never re-issued to a DIFFERENT
    person. The confirmation and the success toast both told the GDPR officer the
    exact opposite ("New tokens will restart from 1.")."""
    monkeypatch.setattr(settings_page, "MappingStore", _OkStore)
    monkeypatch.setattr(audit_mod, "log_event", lambda *a, **kw: None)
    box = ui.column()
    with box:
        settings_page._mapping_admin()
    notifications.clear()

    _click_labeled(box, "Reset all mappings")
    dialog_text = " | ".join(_texts(d) for d in dialogs()).lower()
    for dlg in dialogs():
        _click_labeled(dlg, "Confirm")

    toast = " | ".join(msg for msg, _ in notifications).lower()
    assert "restart" not in toast and "from 1" not in toast, (
        "the success toast still promises numbering restarts from 1"
    )
    assert "continue" in toast, "the toast must say numbering deliberately continues"
    assert "restart" not in dialog_text, "the confirmation body still promises a restart"


# --- build smoke -------------------------------------------------------------


def test_render_review_builds_for_a_large_multi_class_result():
    """Build-smoke: the screen must construct without error for the shape the bank
    actually has (thousands of values, several classes, spreadsheet columns)."""
    people = [_finding(f"Name {i}") for i in range(review._REVIEW_CAP + 300)]
    ids = [_finding(f"DE{i:020d}", tier="high", score=0.99) for i in range(20)]
    result = ScanResult(
        groups=[
            DataClassGroup(key="people", display="People", sensitivity="high", items=people),
            DataClassGroup(key="financial", display="Financial IDs", sensitivity="high", items=ids),
        ],
        possible_misses=[_finding("0049 170 1234567", action="skip", tier="low", score=0.0)],
        stats={"distinct_findings": len(people) + len(ids), "auto_accept": 20, "needs_review": len(people),
               "possible_misses": 1},
        columns=[ColumnInfo(sheet="Kunden", column="B", header="Kundenname", sample="Klaus", pii_count=3)],
    )
    box = _render(result, column_policies={})

    assert "People" in _texts(box)
    assert "Financial IDs" in _texts(box)
