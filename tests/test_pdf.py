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


def test_pdf_widget_redaction_leaves_no_orphan_appearance_stream(tmp_path, analyzer, base_config, mapping_db_path):
    """Regression (LEAK): redacting a form field via w.field_value + w.update() then
    a plain doc.save left the OLD appearance stream (rendering the ORIGINAL value)
    orphaned but recoverable in the bytes. The handler must save garbage-collected.
    Tested against the handler directly (the pipeline's later metadata re-save would
    otherwise mask it)."""
    from anonymizer.formats import pdf_handler
    from anonymizer.mapping import MappingStore

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Formular.")
    w = fitz.Widget()
    w.field_name = "holder"
    w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    w.rect = fitz.Rect(50, 100, 320, 120)
    w.field_value = _IBAN
    page.add_widget(w)
    path = tmp_path / "w.pdf"
    doc.save(path)
    doc.close()

    cfg = {**base_config, "languages": ["de"]}
    grouped = scan_document(path, analyzer, cfg).all_actionable()
    for g in grouped:
        g.action = "anonymize"
    decisions = {(g.entity_type, g.value.strip().lower()): g.action for g in grouped}
    out = tmp_path / "w_out.pdf"
    with MappingStore(mapping_db_path) as ms:
        pdf_handler.apply(path, out, decisions, analyzer, cfg, ms)

    assert _IBAN.encode() not in out.read_bytes(), "original widget value recoverable in output bytes"


def test_pdf_text_page_with_many_images_uses_text_layer(tmp_path, analyzer, base_config, monkeypatch):
    """Regression: total-image-coverage must NOT override a page that has a healthy
    digital text layer. A real letter with a logo + signature + footer graphic
    (summing >50% of the page) must be processed from its text, not refused/re-OCR'd.
    OCR is forced OFF so a misclassification would raise -- the test then proves it
    doesn't."""
    from anonymizer import ocr as ocr_mod

    monkeypatch.setattr(ocr_mod, "ocr_available", lambda config=None: False)
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((60, 60), f"Sehr geehrter Herr Mueller, betreffend Ihr Konto {_IBAN} im Anhang.")
    for top in (0, 300, 560):  # three big images summing well over 50% of the page
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 600, 240))
        pix.clear_with(205)
        page.insert_image(fitz.Rect(0, top, 600, top + 240), pixmap=pix)
    path = tmp_path / "letter_with_images.pdf"
    doc.save(path)
    doc.close()

    grouped = scan_document(path, analyzer, base_config).all_actionable()  # must NOT raise
    assert any(_IBAN in g.value for g in grouped), "text-layer IBAN missed on an image-heavy page"
