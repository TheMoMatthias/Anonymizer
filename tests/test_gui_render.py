"""Headless render smoke tests for the review screen (2026-07-23 clustered
master/detail redesign). These do NOT check appearance -- only that every code
path BUILDS inside a real NiceGUI client without raising (a blank-screen /
crash guard). NiceGUI has no browser here (playwright absent), so this uses the
synchronous Client-context harness rather than the async `user` fixture.
"""

import pytest
from nicegui import Client, ui
from nicegui.testing.general import nicegui_reset_globals, prepare_simulation

from anonymizer import core
from anonymizer.models import CellInfo, ColumnInfo, Finding, GroupedFinding, TextUnit

CONFIG = {
    "entities": {
        "NER_MISC": {"default_action": "pseudonymize"},
        "PERSON": {"default_action": "pseudonymize"},
        "IBAN_CODE": {"default_action": "pseudonymize"},
    },
    "tiers": {"high": 0.9, "medium": 0.5},
}


def _result(with_columns=True, with_misses=True):
    findings = [
        Finding("PERSON", "Klaus Müller", 0.85, "...[Klaus Müller]...", "u1", 0, 12, source="SpacyRecognizer"),
        Finding("IBAN_CODE", "DE89370400440532013000", 0.98, "ctx", "u3", 0, 22, validated=True, source="IbanRecognizer"),
        Finding("NER_MISC", "Migration", 0.85, "...[Migration]...", "u4", 0, 9, source="SpacyRecognizer"),
    ]
    result = core.build_scan_result(findings, [TextUnit("u1", "x")], CONFIG)
    if with_columns:
        result.columns = [ColumnInfo("Sheet", "A", "Verantwortlich", "Klaus Müller", 5, True)]
        result.cells = [CellInfo("Sheet", "A2", "Verantwortlich", "Klaus Müller", ("PERSON",))]
    if with_misses:
        result.possible_misses = [GroupedFinding("POSSIBLE_MISS", "AB1234567", 3, 0.0, "ctx", "skip", tier="low")]
    return result


@pytest.fixture
def render_ctx():
    """A NiceGUI page/client context so ui.* element creation is legal."""
    prepare_simulation()
    with nicegui_reset_globals():

        @ui.page("/probe")
        def probe():
            pass

        client = Client(probe)
        with client:
            yield


def test_render_review_builds_every_cluster(render_ctx):
    from anonymizer.gui import review

    result = _result()
    keys = [c["key"] for c in review._build_clusters(result, {}, {})]
    assert keys[0] == "overview"
    assert "columns" in keys and "cells" in keys and "misses" in keys
    assert any(k.startswith("class:") for k in keys)

    # Selecting each cluster must render its detail without raising.
    for key in keys:
        result._selected_cluster = key
        review.render_review(ui.column(), result, lambda: None, {}, {})


def test_render_review_no_columns_no_misses(render_ctx):
    """A non-spreadsheet doc with no possible-misses still renders (only the
    Overview + data-class clusters exist)."""
    from anonymizer.gui import review

    result = _result(with_columns=False, with_misses=False)
    review.render_review(ui.column(), result, lambda: None, None)


def test_render_review_empty_result(render_ctx):
    from anonymizer.gui import review

    empty = core.build_scan_result([], [TextUnit("u1", "x")], CONFIG)
    review.render_review(ui.column(), empty, lambda: None, {})


def test_gliner_settings_section_renders(render_ctx):
    """The AI-detection toggle + status line builds without raising, and the
    toggle is two-way bound to cfg['gliner']['enabled']."""
    import yaml

    from anonymizer.config import DEFAULT_CONFIG_PATH
    from anonymizer.gui import settings_page

    cfg = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    settings_page._gliner_section(cfg)  # must not raise
    ok, status = settings_page._gliner_status(cfg["gliner"])
    assert isinstance(ok, bool) and "runtime:" in status and "model:" in status


def test_gliner_section_absent_block_is_safe(render_ctx):
    """No gliner block in config -> section is a no-op, never raises."""
    from anonymizer.gui import settings_page

    settings_page._gliner_section({})


def test_detection_control_bar_builds(render_ctx):
    from anonymizer.gui import app as gui_app
    from anonymizer.models import FileJob

    state = gui_app.PageState()
    job = FileJob(path=r"C:\docs\report.xlsx", status="review", scan=_result(), config={})
    gui_app._detection_control_bar(state, job)
    # The bar initializes the live sensitivity from the job's config.
    assert state.sensitivity == 0.0


def _bar_text(state, job) -> str:
    from nicegui import ui

    from anonymizer.gui import app as gui_app

    box = ui.column()
    with box:
        gui_app._detection_control_bar(state, job)
    out = []
    for d in [box, *box.descendants()]:
        for c in (getattr(d, "_text", None), d._props.get("label")):
            if isinstance(c, str) and c:
                out.append(c)
    return " | ".join(out)


def test_switching_to_a_profile_that_detects_differently_offers_a_rescan(render_ctx):
    """Profiles used to carry only default ACTIONS, so switching one could be
    applied to findings already on screen. They now also carry DETECTION settings,
    which cannot be applied retroactively -- the findings came from a scan that
    used the old values. Left silent, the control bar asserts a profile that
    demonstrably did not produce the list underneath it."""
    from anonymizer.gui import app as gui_app
    from anonymizer.models import FileJob

    state = gui_app.PageState()
    state.profile = "HR documents"
    job = FileJob(
        path=r"C:\docs\report.xlsx", status="review", scan=_result(), config={},
        scanned_profile="Client statements",
    )
    assert "Re-scan to apply" in _bar_text(state, job)


def test_no_rescan_notice_when_detection_is_unchanged(render_ctx):
    """The notice must not cry wolf: re-showing it for a profile whose detection
    settings are identical would train the reviewer to ignore it."""
    from anonymizer.gui import app as gui_app
    from anonymizer.models import FileJob

    state = gui_app.PageState()
    state.profile = "HR documents"
    job = FileJob(
        path=r"C:\docs\report.xlsx", status="review", scan=_result(), config={},
        scanned_profile="HR documents",
    )
    assert "Re-scan to apply" not in _bar_text(state, job)
