import fitz
import pytest

from anonymizer.core import _resolve_overlaps, completeness_scan, detect_unit
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


_OV_TEXT = "x" * 64  # filler text for overlap tests that never trigger a re-slice


def test_resolve_overlaps_keeps_one_longer_span():
    """Regression: two recognizers claiming overlapping-but-different spans for
    one phone number used to both survive (exact-tuple dedup), and apply then
    spliced them and mangled the text ('...danke' -> '...ke'). Overlap
    resolution must keep exactly one -- the longer (more complete) span."""
    a = Finding("PHONE_NUMBER", "030 12345678", 0.85, "", "u1", 5, 17)
    b = Finding("DE_PHONE", "030 12345678x", 0.60, "", "u1", 5, 18)  # overlaps, longer
    kept = _resolve_overlaps([a, b], _OV_TEXT)
    assert len(kept) == 1
    assert kept[0].entity_type == "DE_PHONE"  # longer span wins -> more coverage


def test_resolve_overlaps_full_address_beats_contained_city():
    """A full DE_ADDRESS must win over spaCy's city-only LOCATION inside it, so
    the house number is redacted too -- even though LOCATION scores higher."""
    city = Finding("LOCATION", "Königsallee", 0.85, "", "u1", 10, 21)
    addr = Finding("DE_ADDRESS", "Königsallee 3", 0.60, "", "u1", 10, 23)
    kept = _resolve_overlaps([city, addr], _OV_TEXT)
    assert len(kept) == 1 and kept[0].entity_type == "DE_ADDRESS"


def test_resolve_overlaps_keeps_touching_and_disjoint():
    a = Finding("PERSON", "Anna", 0.9, "", "u1", 0, 4)
    b = Finding("PERSON", "Berlin", 0.9, "", "u1", 5, 11)
    c = Finding("IBAN_CODE", "DE00", 0.98, "", "u1", 11, 15)  # touches b at 11
    kept = _resolve_overlaps([a, b, c], _OV_TEXT)
    assert [k.start for k in kept] == [0, 5, 11]  # none overlap; sorted by start


def test_resolve_overlaps_denylist_wins():
    real = Finding("PERSON", "Musterbank", 0.85, "", "u1", 0, 10)
    deny = Finding("DENY_LIST", "Musterbank", 1.0, "", "u1", 0, 10)
    kept = _resolve_overlaps([real, deny], _OV_TEXT)
    assert len(kept) == 1 and kept[0].entity_type == "DENY_LIST"


def test_resolve_overlaps_crossing_spans_merge_not_leak():
    """Regression (LEAK): a PERSON anchor over-reaching into an address used to be
    dropped WHOLESALE by a longer, CROSSING DE_ADDRESS, leaving the customer name
    redacted by nothing. Crossing overlaps must MERGE to the union, not drop the
    loser's exclusive range."""
    text = "Kontoinhaber: Klaus Mueller Hauptstr 12, Musterstadt."
    person = Finding(
        "PERSON", "Klaus Mueller Hauptstr", 0.75, "", "u1",
        text.index("Klaus"), text.index("Hauptstr") + len("Hauptstr"),
    )
    addr = Finding(
        "DE_ADDRESS", "Hauptstr 12, Musterstadt", 0.60, "", "u1",
        text.index("Hauptstr"), text.index("Musterstadt") + len("Musterstadt"),
    )
    kept = _resolve_overlaps([person, addr], text)
    assert len(kept) == 1, "crossing spans must merge into one, not drop one"
    covered = kept[0]
    assert covered.start <= text.index("Klaus"), "merged span must cover the name"
    assert covered.end >= text.index("Musterstadt") + len("Musterstadt")
    assert "Klaus Mueller" in covered.value  # value re-sliced from text to cover both


def test_checksum_failed_steuer_id_still_surfaces(analyzer, base_config):
    """Regression (LEAK): a checksum-FAILED Steuer-ID was demoted to 0.4 then
    dropped by its 0.6 threshold, vanishing from the actionable set. A typo'd/OCR'd
    tax ID is still identifying and must be surfaced (flagged unverified)."""
    cfg = {**base_config, "languages": ["de"]}
    unit = TextUnit("u1", "Steuer-ID: 12345678901 des Kunden.")
    findings = detect_unit(analyzer, unit, cfg)
    st = [f for f in findings if f.entity_type == "DE_STEUER_ID"]
    assert st, f"checksum-failed Steuer-ID dropped: {[(f.entity_type, f.value) for f in findings]}"
    assert st[0].validated is False


def test_honorific_herrn_dative_is_anchored(analyzer, base_config):
    """'Herrn <Name>' (dative -- the postal address-block form) must anchor the name
    exactly like 'Herr <Name>'; the plain 'Herr' pattern silently missed 'Herrn'."""
    cfg = {**base_config, "languages": ["de"]}
    typed = {(f.entity_type, f.value) for f in detect_unit(analyzer, TextUnit("u1", "Herrn Klaus Mueller"), cfg)}
    assert any(et == "PERSON" and "Mueller" in v for et, v in typed), typed
