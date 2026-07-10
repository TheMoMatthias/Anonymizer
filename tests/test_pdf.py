import fitz

from anonymizer.pipeline import apply_document, scan_document


def test_detects_person_and_iban(sample_pdf, analyzer, base_config):
    grouped = scan_document(sample_pdf, analyzer, base_config)
    entity_types = {g.entity_type for g in grouped}
    assert "IBAN_CODE" in entity_types
    assert any(g.entity_type == "PERSON" for g in grouped)


def test_apply_truly_removes_text_not_just_visually(sample_pdf, analyzer, base_config, mapping_db_path):
    grouped = scan_document(sample_pdf, analyzer, base_config)
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
