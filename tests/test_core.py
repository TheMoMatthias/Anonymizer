import fitz
import pytest

from anonymizer.core import completeness_scan, detect_unit
from anonymizer.models import Finding, TextUnit
from anonymizer.pipeline import ProcessingError, apply_document, scan_document, verify_output


def test_german_phone_is_contact_not_date(analyzer, base_config):
    unit = TextUnit("u1", "Telefon: 0170 1234567. IBAN DE89370400440532013000. Steuer-ID 86095742719.")
    findings = detect_unit(analyzer, unit, base_config)
    typed = {(f.value, f.entity_type) for f in findings}
    assert any(et == "DE_PHONE" and "0170" in v for v, et in typed), "phone should be DE_PHONE"
    # the phone regex must not carve a fake phone out of the IBAN or Steuer-ID digits
    assert not any(et == "DE_PHONE" and v.startswith("0400") for v, et in typed)
    assert not any(et == "DE_PHONE" and v.startswith("095") for v, et in typed)


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


def test_image_pdf_is_refused_when_ocr_unavailable(tmp_path, analyzer, base_config, monkeypatch):
    from anonymizer import ocr as ocr_mod

    monkeypatch.setattr(ocr_mod, "ocr_available", lambda config=None: False)
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
