import fitz
import pytest

from anonymizer.core import completeness_scan
from anonymizer.models import Finding, TextUnit
from anonymizer.pipeline import ProcessingError, apply_document, scan_document, verify_output


def _finding(unit_id, text, value):
    start = text.index(value)
    return Finding("X", value, 0.9, "", unit_id, start, start + len(value))


def test_completeness_flags_unmatched_numbers_and_emails():
    text = "Vertrag 998877 fuer a@bank.de, gedeckt 555001 vorhanden."
    units = [TextUnit("u1", text)]
    covered = [_finding("u1", text, "555001")]
    misses = completeness_scan(units, covered)
    values = {m.value for m in misses}
    assert any("998877" in v for v in values)  # unmatched number surfaced
    assert any("@" in v for v in values)  # email-shaped surfaced
    assert all("555001" != v for v in values)  # already-covered span skipped


def test_scan_apply_parity_no_residual(sample_docx, analyzer, base_config, mapping_db_path):
    grouped = scan_document(sample_docx, analyzer, base_config).all_actionable()
    for g in grouped:
        g.action = "anonymize"
    out_path, _ = apply_document(sample_docx, grouped, analyzer, base_config, mapping_db_path)

    decisions = {(g.entity_type, g.value.strip().lower()): g.action for g in grouped}
    assert verify_output(out_path, decisions, analyzer, base_config) == []


def test_image_pdf_is_refused(tmp_path, analyzer, base_config):
    doc = fitz.open()
    doc.new_page()  # a page with no text layer (simulates a scan)
    path = tmp_path / "scanned.pdf"
    doc.save(path)
    doc.close()
    with pytest.raises(ProcessingError, match="no extractable text"):
        scan_document(path, analyzer, base_config)


def test_corrupt_file_fails_loud(tmp_path, analyzer, base_config):
    path = tmp_path / "broken.docx"
    path.write_bytes(b"this is not a real docx")
    with pytest.raises(ProcessingError):
        scan_document(path, analyzer, base_config)


def test_stats_report_tiers(sample_docx, analyzer, base_config):
    result = scan_document(sample_docx, analyzer, base_config)
    assert result.stats["units_scanned"] > 0
    assert result.stats["distinct_findings"] == len(result.all_actionable())
    assert "auto_accept" in result.stats and "needs_review" in result.stats
