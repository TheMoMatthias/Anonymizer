from pptx import Presentation

from anonymizer.pipeline import apply_document, scan_document


def test_detects_person_and_iban(sample_pptx, analyzer, base_config):
    grouped = scan_document(sample_pptx, analyzer, base_config).all_actionable()
    entity_types = {g.entity_type for g in grouped}
    assert "IBAN_CODE" in entity_types
    assert any(g.entity_type == "PERSON" for g in grouped)


def test_apply_replaces_slide_text(sample_pptx, analyzer, base_config, mapping_db_path):
    grouped = scan_document(sample_pptx, analyzer, base_config).all_actionable()
    for g in grouped:
        g.action = "pseudonymize"
    out_path, report_path = apply_document(sample_pptx, grouped, analyzer, base_config, mapping_db_path)

    assert out_path.exists()
    assert report_path.exists()
    prs = Presentation(out_path)
    title_text = prs.slides[0].shapes.title.text
    assert "Hans Mueller" not in title_text
