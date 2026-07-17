import fitz
import pytest

from anonymizer.pipeline import (
    ProcessingError,
    _output_text_blob,
    _scrub_metadata,
    apply_document,
    scan_document,
)

_IBAN = "DE89370400440532013000"


def test_pdf_form_field_is_scanned_and_redacted(tmp_path, analyzer, base_config, mapping_db_path):
    """An AcroForm text field VALUE (where a bank form holds the IBAN/name) must be
    scanned and redacted -- get_text() never sees it, so it used to ship clear."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Antragsformular fuer ein Konto.")
    w = fitz.Widget()
    w.field_name = "holder"
    w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    w.rect = fitz.Rect(50, 100, 320, 120)
    w.field_value = _IBAN
    page.add_widget(w)
    path = tmp_path / "form.pdf"
    doc.save(path)
    doc.close()

    grouped = scan_document(path, analyzer, base_config).all_actionable()
    assert any(_IBAN in g.value for g in grouped), "form-field IBAN was not scanned"
    for g in grouped:
        g.action = "anonymize"
    out_path, _ = apply_document(path, grouped, analyzer, base_config, mapping_db_path)

    out = fitz.open(out_path)
    field_values = [wg.field_value for pg in out for wg in list(pg.widgets() or [])]
    out.close()
    assert not any(_IBAN in (v or "") for v in field_values), f"IBAN survived in a form field: {field_values}"


def test_pdf_annotation_content_is_scanned(tmp_path, analyzer, base_config):
    """Annotation (sticky-note / comment) text must be scanned too."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Dokument mit Anmerkung.")
    page.add_text_annot(fitz.Point(120, 120), f"Konto {_IBAN} pruefen")
    path = tmp_path / "annot.pdf"
    doc.save(path)
    doc.close()

    grouped = scan_document(path, analyzer, base_config).all_actionable()
    assert any(_IBAN in g.value for g in grouped), "annotation IBAN was not scanned"


def test_pdf_mixed_image_page_refused_without_ocr(tmp_path, analyzer, base_config, monkeypatch):
    """A PDF with a text page AND a scanned (large-image, no-text) page must fail
    loud when OCR is unavailable, not scan only the text page and ship the image
    page unredacted as 'verified clean'."""
    from anonymizer import ocr as ocr_mod

    monkeypatch.setattr(ocr_mod, "ocr_available", lambda config=None: False)
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Seite eins mit ausreichend Text und Inhalt.")
    p2 = doc.new_page(width=600, height=800)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 600, 800))
    pix.clear_with(210)
    p2.insert_image(p2.rect, pixmap=pix)  # full-page image, no text
    path = tmp_path / "mixed.pdf"
    doc.save(path)
    doc.close()

    with pytest.raises(ProcessingError):
        scan_document(path, analyzer, base_config)


def test_pdf_metadata_fully_scrubbed_and_not_in_blob(tmp_path):
    """/Info author name must be cleared AND absent from the verification blob (the
    literal backstop reads metadata now), not just left recoverable via saveIncr."""
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Body text here for the page.")
    doc.set_metadata({"author": "Klaus Mueller", "title": "Vertraulich"})
    path = tmp_path / "meta.pdf"
    doc.save(path)
    doc.close()

    _scrub_metadata(path)
    out = fitz.open(path)
    meta = out.metadata or {}
    out.close()
    assert not (meta.get("author") or ""), "author metadata not cleared"
    assert "Klaus Mueller" not in _output_text_blob(path), "author name still recoverable in output"


def test_detects_person_and_iban(sample_pdf, analyzer, base_config):
    grouped = scan_document(sample_pdf, analyzer, base_config).all_actionable()
    entity_types = {g.entity_type for g in grouped}
    assert "IBAN_CODE" in entity_types
    assert any(g.entity_type == "PERSON" for g in grouped)


def test_apply_truly_removes_text_not_just_visually(sample_pdf, analyzer, base_config, mapping_db_path):
    grouped = scan_document(sample_pdf, analyzer, base_config).all_actionable()
    for g in grouped:
        g.action = "anonymize"
    out_path, report_path = apply_document(sample_pdf, grouped, analyzer, base_config, mapping_db_path)

    assert out_path.exists()
    assert report_path.exists()
    doc = fitz.open(out_path)
    extracted = doc[0].get_text()
    doc.close()
    assert "Hans Mueller" not in extracted
    assert "DE89370400440532013000" not in extracted
