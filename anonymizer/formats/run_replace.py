"""Run-level find/replace shared by docx and pptx handlers (and the raw-XML
adapter for footnotes/comments). Works on any object with a get/set `.text`."""

from __future__ import annotations

from ..actions import decisions_lookup, resolve_replacement


def runs_text_and_spans(runs: list) -> tuple[str, list[tuple[object, int, int]]]:
    full_text = ""
    spans = []
    pos = 0
    for run in runs:
        t = run.text or ""
        spans.append((run, pos, pos + len(t)))
        full_text += t
        pos += len(t)
    return full_text, spans


def apply_span_replacement(spans: list[tuple[object, int, int]], start: int, end: int, replacement: str) -> None:
    overlapping = [(r, rs, re) for (r, rs, re) in spans if re > start and rs < end]
    if not overlapping:
        return
    first = True
    for run, rs, re in overlapping:
        local_start = max(start, rs) - rs
        local_end = min(end, re) - rs
        text = run.text or ""
        if first:
            run.text = text[:local_start] + replacement + text[local_end:]
            first = False
        else:
            run.text = text[:local_start] + text[local_end:]


def apply_findings_to_runs(runs: list, findings: list, decisions: dict, mapping_store) -> None:
    """Applies findings to runs in descending start order so earlier offsets stay valid."""
    if not findings:
        return
    _, spans = runs_text_and_spans(runs)
    for f in sorted(findings, key=lambda f: -f.start):
        action = decisions_lookup(decisions, f.entity_type, f.value)
        replacement = resolve_replacement(f.entity_type, f.value, action, mapping_store)
        if replacement is None:
            continue
        apply_span_replacement(spans, f.start, f.end, replacement)


class XmlRunAdapter:
    """Adapts a single OOXML `<w:t>`/`<a:t>` element to the run-like `.text` interface."""

    def __init__(self, t_element):
        self._t = t_element

    @property
    def text(self) -> str:
        return self._t.text or ""

    @text.setter
    def text(self, value: str) -> None:
        self._t.text = value
