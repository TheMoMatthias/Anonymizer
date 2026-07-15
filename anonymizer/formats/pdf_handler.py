from __future__ import annotations

from pathlib import Path

import fitz

from .. import ocr as ocr_mod
from ..actions import decisions_lookup
from ..core import detect_unit
from ..models import TextUnit

EXTENSIONS = (".pdf",)

_BLACK = (0, 0, 0)


def _page_text_or_ocr(page):
    """Returns (text, ocr_boxes|None). Falls back to OCR for pages with no text
    layer when a portable Tesseract is available; otherwise (text, None)."""
    text = page.get_text()
    if text.strip():
        return text, None
    if ocr_mod.ocr_available():
        ocr_text, boxes = ocr_mod.ocr_page(page)
        if ocr_text.strip():
            return ocr_text, boxes
    return text, None


def extract_text_units(path: Path) -> list[TextUnit]:
    doc = fitz.open(path)
    units = []
    for i, page in enumerate(doc):
        text, _boxes = _page_text_or_ocr(page)
        if text.strip():
            units.append(TextUnit(id=f"page{i}", text=text))
    doc.close()
    return units


def scan(path: Path, analyzer, config) -> list:
    findings = []
    for unit in extract_text_units(path):
        findings.extend(detect_unit(analyzer, unit, config))
    return findings


def apply(path: Path, out_path: Path, decisions: dict, analyzer, config, mapping_store) -> None:
    # PDF text can't be edited in place -- every non-skipped match is fully
    # redacted (content physically removed) regardless of pseudonymize/anonymize.
    # Text-layer pages locate matches via search_for; OCR'd image pages place
    # redaction boxes over the OCR word boxes for each matched span.
    doc = fitz.open(path)
    for page in doc:
        text, boxes = _page_text_or_ocr(page)
        if not text.strip():
            continue
        unit = TextUnit(id="tmp", text=text)
        findings = detect_unit(analyzer, unit, config)
        for f in findings:
            if decisions_lookup(decisions, f.entity_type, f.value) == "skip":
                continue
            if boxes is None:
                rects = list(page.search_for(f.value))
            else:
                rects = [fitz.Rect(*r) for r in ocr_mod.boxes_for_span(boxes, f.start, f.end)]
            for rect in rects:
                page.add_redact_annot(rect, fill=_BLACK)
        page.apply_redactions()
    doc.save(str(out_path))
    doc.close()
