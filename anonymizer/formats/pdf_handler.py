from __future__ import annotations

from pathlib import Path

import fitz

from ..actions import decisions_lookup
from ..engine import analyze_unit
from ..models import TextUnit

EXTENSIONS = (".pdf",)


def extract_text_units(path: Path) -> list[TextUnit]:
    doc = fitz.open(path)
    units = []
    for i, page in enumerate(doc):
        text = page.get_text()
        if text.strip():
            units.append(TextUnit(id=f"page{i}", text=text))
    doc.close()
    return units


def scan(path: Path, analyzer, config) -> list:
    findings = []
    for unit in extract_text_units(path):
        findings.extend(analyze_unit(analyzer, unit, config))
    return findings


def apply(path: Path, out_path: Path, decisions: dict, analyzer, config, mapping_store) -> None:
    # PDF text can't be edited in place -- every non-skipped match is fully
    # redacted (content removed) regardless of pseudonymize/anonymize choice.
    # There is no reversible mode for PDFs.
    doc = fitz.open(path)
    for page in doc:
        text = page.get_text()
        if not text.strip():
            continue
        unit = TextUnit(id="tmp", text=text)
        findings = analyze_unit(analyzer, unit, config)
        for f in findings:
            if decisions_lookup(decisions, f.entity_type, f.value) == "skip":
                continue
            for rect in page.search_for(f.value):
                page.add_redact_annot(rect, fill=(0, 0, 0))
        page.apply_redactions()
    doc.save(str(out_path))
    doc.close()
