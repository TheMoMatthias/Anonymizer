from __future__ import annotations

from pathlib import Path

import openpyxl

from ..actions import decisions_lookup, resolve_replacement
from ..engine import analyze_unit
from ..models import TextUnit

EXTENSIONS = (".xlsx", ".xlsm", ".xls")


def _column_headers(ws) -> dict[int, str]:
    headers = {}
    first_row = next(ws.iter_rows(min_row=1, max_row=1), [])
    for cell in first_row:
        if isinstance(cell.value, str) and cell.value.strip():
            headers[cell.column] = cell.value.strip()
    return headers


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
                if cell.data_type == "s" and isinstance(cell.value, str) and cell.value.strip():
                    yield f"cell|{ws.title}|{cell.coordinate}", cell.value, header
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


def _analyze_cell_text(text: str, header: str | None, analyzer, config) -> list:
    prefix = f"{header}: " if header else ""
    combined = prefix + text
    unit = TextUnit(id="tmp", text=combined)
    findings = analyze_unit(analyzer, unit, config)
    offset = len(prefix)
    result = []
    for f in findings:
        if f.start < offset:
            continue  # matched inside the header context, not the actual value
        f.start -= offset
        f.end -= offset
        result.append(f)
    return result


def scan(path: Path, analyzer, config) -> list:
    wb = openpyxl.load_workbook(path, data_only=False)
    findings = []
    for _key, text, header in _iter_cell_units(wb):
        findings.extend(_analyze_cell_text(text, header, analyzer, config))
    for _key, text in _iter_defined_name_units(wb):
        findings.extend(_analyze_cell_text(text, None, analyzer, config))
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
                if cell.data_type == "s" and isinstance(cell.value, str) and cell.value.strip():
                    new_value = _apply_findings_to_text(cell.value, header, analyzer, config, decisions, mapping_store)
                    if new_value != cell.value:
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
