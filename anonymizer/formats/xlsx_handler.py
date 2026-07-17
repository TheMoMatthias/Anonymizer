from __future__ import annotations

import re
from pathlib import Path

import openpyxl

from ..actions import decisions_lookup, resolve_replacement
from ..core import _resolve_overlaps, detect_unit
from ..models import Finding, TextUnit

EXTENSIONS = (".xlsx", ".xlsm", ".xls")

# A column header that declares its cells are people. This is the one place a
# bare surname legitimately appears with no prose around it, and it is exactly
# where NER collapses: measured, de_core_news_lg finds only ~35% of ordinary
# German surnames (Müller, Weber, Bauer) in a bare cell, because its training
# gives it nothing to lean on without a sentence.
#
# The header is stronger evidence than any model: the spreadsheet's own author
# labelled the column. Note this does NOT work via Presidio's context boost --
# that only lifts PATTERN recognizers, and spaCy NER gets no boost from it at
# all, which is why "Kunde: Müller" in a cell still missed.
_NAME_COLUMN_HEADER = re.compile(
    r"(name|kunde|kundin|inhaber|empf(ä|ae)nger|sachbearbeiter|ansprechpartner|"
    r"berater|mitarbeiter|antragsteller|vertragspartner|beg(ü|ue)nstigter)",
    re.IGNORECASE,
)
# Cell contents that a name column can still hold but which are not names.
_NOT_A_NAME = re.compile(r"^[\W\d_]*$|^(unbekannt|n/?a|keine?|leer|-{1,3}|divers)$", re.IGNORECASE)
_NAME_COLUMN_SCORE = 0.8


def _column_headers(ws) -> dict[int, str]:
    headers = {}
    first_row = next(ws.iter_rows(min_row=1, max_row=1), [])
    for cell in first_row:
        if isinstance(cell.value, str) and cell.value.strip():
            headers[cell.column] = cell.value.strip()
    return headers


def _cell_scan_text(cell) -> str | None:
    """The text to scan for a cell, or None to skip. String cells pass through;
    NUMBERS are coerced to a plain string so account / tax / customer / phone
    numbers STORED AS NUMBERS (very common in bank spreadsheets) are not
    invisible to detection -- previously only string cells were scanned, so a
    numeric account number sailed through into a "verified" file. Short numbers
    (< 5 digits: counts, small amounts) and non-integer decimals (monetary
    amounts) are skipped as not-identifiers; dates/booleans/formulas/errors are
    left to structure."""
    v = cell.value
    if v is None:
        return None
    if cell.data_type == "s" and isinstance(v, str):
        return v if v.strip() else None
    if cell.data_type == "n" and isinstance(v, (int, float)) and not isinstance(v, bool):
        if isinstance(v, float):
            if not v.is_integer():
                return None
            v = int(v)
        s = str(v)
        return s if len(s) >= 5 else None
    return None


def _iter_cell_units(wb):
    """Yields (id, text, header) -- header is the column-1-row text for that
    column, given as context so recognizers relying on nearby German context
    words (Kontonummer, Depotnummer, ...) actually have something to match,
    since a bare cell value alone carries no context."""
    for ws in wb.worksheets:
        headers = _column_headers(ws)
        for row in ws.iter_rows():
            for cell in row:
                header = headers.get(cell.column) if cell.row != 1 else None
                text = _cell_scan_text(cell)
                if text is not None:
                    yield f"cell|{ws.title}|{cell.coordinate}", text, header
                if cell.comment is not None and cell.comment.text.strip():
                    yield f"comment|{ws.title}|{cell.coordinate}", cell.comment.text, header


def _iter_defined_name_units(wb):
    for name, defn in wb.defined_names.items():
        if isinstance(defn.value, str) and defn.value.strip():
            yield f"defined_name|{name}", defn.value


def extract_text_units(path: Path) -> list[TextUnit]:
    wb = openpyxl.load_workbook(path, data_only=False)
    units = [TextUnit(id=key, text=text) for key, text, _header in _iter_cell_units(wb)]
    units.extend(TextUnit(id=key, text=text) for key, text in _iter_defined_name_units(wb))
    return units


def _analyze_cell_text(text: str, header: str | None, analyzer, config, unit_id: str = "tmp") -> list:
    prefix = f"{header}: " if header else ""
    combined = prefix + text
    unit = TextUnit(id=unit_id, text=combined)
    findings = detect_unit(analyzer, unit, config)
    offset = len(prefix)
    result = []
    for f in findings:
        if f.end <= offset:
            continue  # entirely inside the header context -- not the cell value
        if f.start < offset:
            # Span STRADDLES the header/value boundary: clip to the value side and
            # re-slice its value, rather than dropping it wholesale (which leaked the
            # in-value portion -- e.g. a deny term that included the header text).
            f.start = offset
            f.value = combined[f.start : f.end]
        f.start -= offset
        f.end -= offset
        result.append(f)

    # The column header declares this cell is a person. Trust it over the model,
    # but only where the whole cell isn't already claimed end-to-end.
    value = text.strip()
    if (
        _NAME_COLUMN_HEADER.search(header or "")
        and value
        and not _NOT_A_NAME.match(value)
        and not any(f.start == 0 and f.end >= len(value) for f in result)
    ):
        start = text.index(value)
        result.append(
            Finding(
                entity_type="PERSON",
                value=value,
                score=_NAME_COLUMN_SCORE,
                context=f"{header}: {value}",
                unit_id=unit_id,
                start=start,
                end=start + len(value),
            )
        )
    # The whole-cell override can PARTIALLY overlap a finding NER did make (just the
    # surname, or a KONTO number in the same cell). Appending it raw left overlapping
    # spans, which the cell splicer assumes never happens -> garbled tokens. Re-resolve
    # the combined set so the no-overlap invariant holds (the override merges to cover
    # the cell rather than corrupting it).
    return _resolve_overlaps(result, text)


def scan(path: Path, analyzer, config) -> list:
    wb = openpyxl.load_workbook(path, data_only=False)
    findings = []
    for key, text, header in _iter_cell_units(wb):
        findings.extend(_analyze_cell_text(text, header, analyzer, config, unit_id=key))
    for key, text in _iter_defined_name_units(wb):
        findings.extend(_analyze_cell_text(text, None, analyzer, config, unit_id=key))
    return findings


def _apply_findings_to_text(text: str, header: str | None, analyzer, config, decisions: dict, mapping_store) -> str:
    findings = _analyze_cell_text(text, header, analyzer, config)
    if not findings:
        return text
    result = text
    for f in sorted(findings, key=lambda f: -f.start):
        action = decisions_lookup(decisions, f.entity_type, f.value)
        replacement = resolve_replacement(f.entity_type, f.value, action, mapping_store)
        if replacement is None:
            continue
        result = result[: f.start] + replacement + result[f.end :]
    return result


def apply(path: Path, out_path: Path, decisions: dict, analyzer, config, mapping_store) -> None:
    # keep_vba=False (the default) strips any macro project from the output,
    # which is intentional: anonymized copies are never macro-enabled.
    wb = openpyxl.load_workbook(path, data_only=False, keep_vba=False)
    for ws in wb.worksheets:
        headers = _column_headers(ws)
        for row in ws.iter_rows():
            for cell in row:
                header = headers.get(cell.column) if cell.row != 1 else None
                text = _cell_scan_text(cell)
                if text is not None:
                    new_value = _apply_findings_to_text(text, header, analyzer, config, decisions, mapping_store)
                    if new_value != text:
                        # A redacted numeric cell must become a string cell so
                        # the token ("[KONTO_1]") can be stored at all.
                        cell.value = new_value
                if cell.comment is not None and cell.comment.text.strip():
                    new_text = _apply_findings_to_text(
                        cell.comment.text, header, analyzer, config, decisions, mapping_store
                    )
                    if new_text != cell.comment.text:
                        cell.comment.text = new_text
    for name, defn in wb.defined_names.items():
        if isinstance(defn.value, str) and defn.value.strip():
            new_value = _apply_findings_to_text(defn.value, None, analyzer, config, decisions, mapping_store)
            if new_value != defn.value:
                defn.value = new_value
    wb.save(out_path)
