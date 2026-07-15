import zipfile

from pptx import Presentation

from anonymizer.formats import pptx_handler
from anonymizer.pipeline import apply_document, scan_document

_MODERN_COMMENT = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<p188:cmLst xmlns:p188="http://schemas.microsoft.com/office/powerpoint/2018/8/main" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
    "<p188:cm><p188:txBody><a:bodyPr/>"
    "<a:p><a:r><a:t>Bitte pruefen: Hans Mueller, IBAN DE89370400440532013000</a:t></a:r></a:p>"
    "</p188:txBody></p188:cm></p188:cmLst>"
)


def _add_modern_comment(path):
    with zipfile.ZipFile(path, "a") as zf:
        zf.writestr("ppt/comments/comment1.xml", _MODERN_COMMENT)


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


def test_scans_modern_threaded_comments(sample_pptx, analyzer, base_config):
    _add_modern_comment(sample_pptx)
    units = pptx_handler.extract_text_units(sample_pptx)
    assert any("Hans Mueller" in u.text for u in units), "modern threaded comment text must be scanned"
    grouped = scan_document(sample_pptx, analyzer, base_config).all_actionable()
    assert any(g.entity_type == "IBAN_CODE" for g in grouped)


def test_apply_redacts_modern_threaded_comments(sample_pptx, analyzer, base_config, mapping_db_path):
    _add_modern_comment(sample_pptx)
    grouped = scan_document(sample_pptx, analyzer, base_config).all_actionable()
    for g in grouped:
        g.action = "anonymize"
    out_path, _ = apply_document(sample_pptx, grouped, analyzer, base_config, mapping_db_path)

    # The safety property is that NO comment PII survives anywhere in the output
    # (whether the part was redacted or dropped). apply_document's output re-scan
    # -- which now also reads modern a:t comment text -- would fail loud on any
    # residual, so reaching here at all means it verified clean.
    with zipfile.ZipFile(out_path, "r") as zf:
        blob = b"".join(zf.read(n) for n in zf.namelist())
    assert b"Hans Mueller" not in blob
    assert b"DE89370400440532013000" not in blob
