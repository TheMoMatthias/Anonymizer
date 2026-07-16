"""Phase-4 fail-loud coverage: numeric-cell detection (#4), metadata scrubbing
(#3), and the recognizer-independent literal-residual backstop (#5)."""

import openpyxl
from docx import Document
from lxml import etree

from anonymizer.formats import docx_handler
from anonymizer.pipeline import _literal_residual, _output_text_blob, apply_document, scan_document

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
IBAN = "DE89370400440532013000"


def _append_xml(doc, xml: str) -> None:
    doc.element.body.append(etree.fromstring(xml))


def _add_textbox(doc, text: str) -> None:
    """python-docx has no text-box API, so inject a minimal VML text box."""
    _append_xml(
        doc,
        f'<w:p xmlns:w="{W_NS}" xmlns:v="urn:schemas-microsoft-com:vml"><w:r><w:pict><v:shape>'
        f"<v:textbox><w:txbxContent><w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
        f"</w:txbxContent></v:textbox></v:shape></w:pict></w:r></w:p>",
    )


def _add_hyperlink_paragraph(doc, text: str) -> None:
    _append_xml(
        doc,
        f'<w:p xmlns:w="{W_NS}"><w:hyperlink><w:r><w:t>{text}</w:t></w:r></w:hyperlink></w:p>',
    )


def test_numeric_account_cell_detected_and_redacted(tmp_path, analyzer, base_config, mapping_db_path):
    """An account number stored as a NUMBER (not text) used to be invisible to
    scan and verify -> emitted in a 'verified' file. It must now be detected via
    the column header context and redacted."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "Kontonummer"  # header -> supplies context for the bare number
    ws["A2"] = 1234567890  # stored as an int, data_type == "n"
    path = tmp_path / "accounts.xlsx"
    wb.save(path)

    grouped = scan_document(path, analyzer, base_config).all_actionable()
    assert any("1234567890" in g.value for g in grouped), "numeric account number must be detected"

    for g in grouped:
        g.action = "anonymize"
    out_path, _ = apply_document(path, grouped, analyzer, base_config, mapping_db_path)
    assert "1234567890" not in _output_text_blob(out_path)
    assert _literal_residual(out_path, ["1234567890"]) == []


def test_document_metadata_is_scrubbed(tmp_path, analyzer, base_config, mapping_db_path):
    """The author / last-modified-by carry the real name but are never in body
    text; they must be blanked, and the literal backstop must confirm the name
    is gone from the whole file."""
    doc = Document()
    doc.add_paragraph("Kunde: Hans Mueller")
    doc.core_properties.author = "Hans Mueller"
    doc.core_properties.last_modified_by = "Hans Mueller"
    path = tmp_path / "letter.docx"
    doc.save(path)

    grouped = scan_document(path, analyzer, base_config).all_actionable()
    for g in grouped:
        g.action = "anonymize"
    out_path, _ = apply_document(path, grouped, analyzer, base_config, mapping_db_path)

    out_doc = Document(out_path)
    assert out_doc.core_properties.author == ""
    assert out_doc.core_properties.last_modified_by == ""
    assert _literal_residual(out_path, ["Hans Mueller"]) == []


def test_textbox_text_is_scanned_and_redacted(tmp_path, analyzer, base_config, mapping_db_path):
    """PII inside a Word text box (w:txbxContent) was invisible to scan AND to
    the output re-scan -- a false-clean leak in letterhead/form templates."""
    doc = Document()
    doc.add_paragraph("Vertrag mit der Musterbank.")
    _add_textbox(doc, f"Zahlungen an IBAN {IBAN}")
    path = tmp_path / "textbox.docx"
    doc.save(path)

    units = docx_handler.extract_text_units(path)
    assert any(IBAN in u.text for u in units), "text-box text must be extracted"

    grouped = scan_document(path, analyzer, base_config).all_actionable()
    assert any(IBAN in g.value.replace(" ", "") for g in grouped)
    for g in grouped:
        g.action = "anonymize"
    out_path, _ = apply_document(path, grouped, analyzer, base_config, mapping_db_path)
    assert IBAN not in _output_text_blob(out_path).replace(" ", "")


def test_hyperlink_text_is_scanned_and_redacted(tmp_path, analyzer, base_config, mapping_db_path):
    """python-docx's p.runs skips runs nested in w:hyperlink, so PII in link
    display text was never scanned or replaced."""
    doc = Document()
    _add_hyperlink_paragraph(doc, f"Konto {IBAN} ansehen")
    path = tmp_path / "link.docx"
    doc.save(path)

    units = docx_handler.extract_text_units(path)
    assert any(IBAN in u.text for u in units), "hyperlink text must be extracted"

    grouped = scan_document(path, analyzer, base_config).all_actionable()
    for g in grouped:
        g.action = "anonymize"
    out_path, _ = apply_document(path, grouped, analyzer, base_config, mapping_db_path)
    assert IBAN not in _output_text_blob(out_path).replace(" ", "")
