"""OCR tests. The Tesseract binary isn't required: the pure mapping logic is
tested directly, and the scan/redaction integration is exercised with a
monkeypatched OCR that returns synthetic text + word boxes.
"""

import fitz

from anonymizer import ocr as ocr_mod
from anonymizer.formats import pdf_handler
from anonymizer.mapping import MappingStore
from anonymizer.ocr import WordBox, boxes_for_span, find_tesseract
from anonymizer.pipeline import scan_document


def test_boxes_for_span_overlap():
    boxes = [
        WordBox("Hans", 10, 10, 50, 30, 0, 4),
        WordBox("Mueller", 55, 10, 120, 30, 5, 12),
        WordBox("x", 0, 0, 5, 5, 13, 14),
    ]
    assert len(boxes_for_span(boxes, 0, 12)) == 2  # both name words
    assert len(boxes_for_span(boxes, 5, 12)) == 1  # only "Mueller"
    assert boxes_for_span(boxes, 100, 110) == []  # nothing overlaps


def test_find_tesseract_explicit(tmp_path, monkeypatch):
    monkeypatch.delenv("ANONYMIZER_TESSERACT", raising=False)
    fake = tmp_path / "tesseract.exe"
    fake.write_text("x")
    assert find_tesseract({"tesseract_path": str(fake)}) == str(fake)
    monkeypatch.setenv("ANONYMIZER_TESSERACT", str(fake))
    assert find_tesseract() == str(fake)


def _blank_pdf(path):
    doc = fitz.open()
    doc.new_page(width=200, height=100)
    doc.save(path)
    doc.close()


def test_ocr_scan_detects_pii(tmp_path, analyzer, base_config, monkeypatch):
    path = tmp_path / "scan.pdf"
    _blank_pdf(path)
    monkeypatch.setattr(ocr_mod, "ocr_available", lambda config=None: True)
    monkeypatch.setattr(
        ocr_mod,
        "ocr_page",
        lambda page, zoom=3.0: ("Kunde Hans Mueller IBAN DE89370400440532013000", []),
    )
    result = scan_document(path, analyzer, base_config)
    assert any(g.entity_type == "IBAN_CODE" for g in result.all_actionable())


def test_ocr_apply_redacts_boxes(tmp_path, analyzer, base_config, mapping_db_path, monkeypatch):
    path = tmp_path / "scan.pdf"
    _blank_pdf(path)
    text = "Hans Mueller"
    boxes = [WordBox("Hans", 10, 10, 50, 30, 0, 4), WordBox("Mueller", 55, 10, 120, 30, 5, 12)]
    monkeypatch.setattr(ocr_mod, "ocr_available", lambda config=None: True)
    monkeypatch.setattr(ocr_mod, "ocr_page", lambda page, zoom=3.0: (text, boxes))

    out = tmp_path / "out.pdf"
    decisions = {("PERSON", "hans mueller"): "anonymize"}
    with MappingStore(mapping_db_path) as ms:
        pdf_handler.apply(path, out, decisions, analyzer, base_config, ms)

    doc = fitz.open(out)
    pix = doc[0].get_pixmap()
    # A pixel inside the redacted "Hans" box must now be black.
    r, g, b = pix.pixel(30, 20)
    doc.close()
    assert r < 20 and g < 20 and b < 20, "redaction box should blacken the OCR word region"
