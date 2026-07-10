from __future__ import annotations

from pathlib import Path

import openpyxl

from ..actions import decisions_lookup, resolve_replacement
from ..engine import analyze_unit
from ..models import TextUnit

EXTENSIONS = (".xlsx", ".xlsm", ".xls")


def _iter_cell_units(wb):
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if cell.data_type == "s" and isinstance(cell.value, str) and cell.value.strip():
                    yield f"cell|{ws.title}|{cell.coordinate}", cell.value
                if cell.comment is not None and cell.comment.text.strip():
                    yield f"comment|{ws.title}|{cell.coordinate}", cell.comment.text


def _iter_defined_name_units(wb):
    for name, defn in wb.defined_names.items():
        if isinstance(defn.value, str) and defn.value.strip():
            yield f"defined_name|{name}", defn.value


def extract_text_units(path: Path) -> list[TextUnit]:
    wb = openpyxl.load_workbook(path, data_only=False)
    units = [TextUnit(id=key, text=text) for key, text in _iter_cell_units(wb)]
    units.extend(TextUnit(id=key, text=text) for key, text in _iter_defined_name_units(wb))
    return units


def scan(path: Path, analyzer, config) -> list:
    findings = []
    for unit in extract_text_units(path):
        findings.extend(analyze_unit(analyzer, unit, config))
    return findings


def _apply_to_text(text: str, analyzer, config, decisions: dict, mapping_store) -> str:
    unit = TextUnit(id="tmp", text=text)
    findings = analyze_unit(analyzer, unit, config)
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
        for row in ws.iter_rows():
            for cell in row:
                if cell.data_type == "s" and isinstance(cell.value, str) and cell.value.strip():
                    new_value = _apply_to_text(cell.value, analyzer, config, decisions, mapping_store)
                    if new_value != cell.value:
                        cell.value = new_value
                if cell.comment is not None and cell.comment.text.strip():
                    new_text = _apply_to_text(cell.comment.text, analyzer, config, decisions, mapping_store)
                    if new_text != cell.comment.text:
                        cell.comment.text = new_text
    for name, defn in wb.defined_names.items():
        if isinstance(defn.value, str) and defn.value.strip():
            new_value = _apply_to_text(defn.value, analyzer, config, decisions, mapping_store)
            if new_value != defn.value:
                defn.value = new_value
    wb.save(out_path)
