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


def test_resolve_overlaps_art9_outranks_a_longer_generic_span():
    """LEAK: spaCy claims 'Krankenkasse Barmer' as one ORGANIZATION -- a LONGER
    span CONTAINING the Art. 9 health hit. Sorting by span length first dropped
    the Art. 9 finding as "fully contained", so union membership / a health
    insurer was filed under Organizations & places and PSEUDONYMIZED into a
    reversible [ORG_n]. Special-category sensitivity must outrank raw span
    length -- while still merging to the union, so nothing is left uncovered."""
    text = "Krankenkasse Barmer zahlt."
    org = Finding("ORGANIZATION", "Krankenkasse Barmer", 0.85, "", "u1", 0, 19)
    health = Finding("DE_HEALTH_DATA", "Barmer", 0.86, "", "u1", 13, 19)
    kept = _resolve_overlaps([org, health], text)
    assert len(kept) == 1
    assert kept[0].entity_type == "DE_HEALTH_DATA", "Art.9 lost its class to a longer ORG span"
    assert (kept[0].start, kept[0].end) == (0, 19), "the generic span's extra range must stay covered"


def test_resolve_overlaps_checksum_tested_id_beats_ner_on_an_identical_span():
    """LEAK: a checksum-FAILED IBAN (demoted to 0.4) lost the IDENTICAL span to
    spaCy's NER_MISC at its flat 0.85, so a typo'd/OCR'd IBAN lost its Financial
    IDs class and its "unverified" chip. On an identical span there is no
    coverage argument either way, so the recognizer that actually ran a checksum
    on the string wins."""
    text = "DE89370400440532013001"
    misc = Finding("NER_MISC", text, 0.85, "", "u1", 0, 22)
    iban = Finding("IBAN_CODE", text, 0.4, "", "u1", 0, 22, validated=False)
    kept = _resolve_overlaps([misc, iban], text)
    assert len(kept) == 1 and kept[0].entity_type == "IBAN_CODE"


# --- Art. 9 span splitting ----------------------------------------------------
#
# An Art. 9 recognizer anchors on a label and claims the REST OF THE LINE, which
# is deliberate: a German diagnosis legitimately contains commas ("Diabetes
# mellitus Typ 2, insulinpflichtig"), so terminating at the first one would leave
# health data in a file the tool calls verified. The cost was that a customer name
# and IBAN sharing that line were destroyed ONE-WAY instead of pseudonymized.

_DIAG = "Diagnose: Diabetes mellitus Typ 2, Herr Klaus Mueller, IBAN DE89370400440532013000"


def _diag_findings(*, iban_validated=True, person_span=None):
    """The Art. 9 line above as (art9, person, iban) findings. The Art. 9 span
    covers everything after the 'Diagnose:' anchor, exactly as the recognizer
    emits it."""
    a_start = _DIAG.index("Diabetes")
    art9 = Finding("DE_HEALTH_DATA", _DIAG[a_start:], 0.86, "", "u1", a_start, len(_DIAG))
    p_start, p_end = person_span or (_DIAG.index("Klaus"), _DIAG.index("Mueller") + len("Mueller"))
    person = Finding("PERSON", _DIAG[p_start:p_end], 0.85, "", "u1", p_start, p_end)
    i_start = _DIAG.index("DE89")
    iban = Finding(
        "IBAN_CODE", _DIAG[i_start:], 0.98, "", "u1", i_start, len(_DIAG), validated=iban_validated
    )
    return art9, person, iban


def test_art9_span_splits_around_a_contained_person_and_validated_id():
    """The fix: a name and a checksum-validated IBAN buried in a `Diagnose:` line
    survive as their own findings (so they stay reversibly pseudonymized) and the
    one-way Art. 9 span is cut around them instead of destroying them."""
    kept = _resolve_overlaps(list(_diag_findings()), _DIAG)
    by_type = {f.entity_type for f in kept}
    assert "PERSON" in by_type, "the customer name was destroyed by the Art.9 span"
    assert "IBAN_CODE" in by_type, "the IBAN was destroyed by the Art.9 span"
    assert "DE_HEALTH_DATA" in by_type, "the health text must still be covered"
    person = next(f for f in kept if f.entity_type == "PERSON")
    assert person.value == "Klaus Mueller"


def test_art9_split_still_covers_every_letter_of_the_health_text():
    """The whole point of claiming the rest of the line is that no health text
    escapes. Splitting must not create a hole: every alphabetic character of the
    original Art. 9 span is still covered by SOME finding."""
    art9, person, iban = _diag_findings()
    kept = _resolve_overlaps([art9, person, iban], _DIAG)
    covered = set()
    for f in kept:
        covered.update(range(f.start, f.end))
    uncovered = [i for i in range(art9.start, art9.end) if i not in covered and _DIAG[i].isalpha()]
    assert not uncovered, f"letters left uncovered by the split: {[_DIAG[i] for i in uncovered]}"


def test_art9_split_preserves_the_non_overlap_invariant():
    """Apply splices spans and ASSUMES they never overlap. A split that produced
    overlapping fragments would garble the written document."""
    kept = _resolve_overlaps(list(_diag_findings()), _DIAG)
    spans = sorted((f.start, f.end) for f in kept)
    assert all(a_end <= b_start for (_a, a_end), (b_start, _b) in zip(spans, spans[1:])), spans


def test_art9_split_emits_no_token_for_a_letter_free_gap():
    """A gap of pure punctuation between two survivors has nothing to redact --
    emitting a [DE_HEALTH_DATA] where only ', ' stood would corrupt the line for
    no privacy gain."""
    text = "Diagnose: Asthma, Klaus Mueller, Anna Weber"
    a_start = text.index("Asthma")
    art9 = Finding("DE_HEALTH_DATA", text[a_start:], 0.86, "", "u1", a_start, len(text))
    p1 = Finding("PERSON", "Klaus Mueller", 0.85, "", "u1", text.index("Klaus"), text.index("Mueller") + 7)
    p2 = Finding("PERSON", "Anna Weber", 0.85, "", "u1", text.index("Anna"), len(text))
    kept = _resolve_overlaps([art9, p1, p2], text)
    health = [f for f in kept if f.entity_type == "DE_HEALTH_DATA"]
    assert all(any(ch.isalpha() for ch in f.value) for f in health), (
        f"a letter-free fragment became a finding: {[f.value for f in health]}"
    )
    assert any("Asthma" in f.value for f in health), "the actual health word must stay covered"


def test_art9_span_is_not_split_by_a_checksum_failed_id():
    """Only arithmetic-backed survivors cut health data. A checksum-FAILED id may
    not be an id at all, and a wrong survivor punches a hole in the Art. 9 span."""
    art9, _person, iban = _diag_findings(iban_validated=False)
    kept = _resolve_overlaps([art9, iban], _DIAG)
    assert len(kept) == 1 and kept[0].entity_type == "DE_HEALTH_DATA"


def test_art9_span_wins_whole_when_a_survivor_covers_it_entirely():
    """If the survivor covers the Art. 9 span exactly, splitting would leave NO
    Art. 9 finding at all -- the value would silently become reversible and lose
    its special-category protection. That case keeps the old behaviour."""
    a_start = _DIAG.index("Diabetes")
    art9, _p, _i = _diag_findings()
    person = Finding("PERSON", _DIAG[a_start:], 0.85, "", "u1", a_start, len(_DIAG))
    kept = _resolve_overlaps([art9, person], _DIAG)
    assert len(kept) == 1 and kept[0].entity_type == "DE_HEALTH_DATA"


def test_art9_diagnosis_containing_commas_is_never_truncated():
    """Pins the REJECTED fix: terminating an Art. 9 value at the first comma looks
    obvious and leaks -- a German diagnosis legitimately contains commas, so the
    tail ('insulinpflichtig') would ship in the clear."""
    text = "Diagnose: Diabetes mellitus Typ 2, insulinpflichtig"
    a_start = text.index("Diabetes")
    art9 = Finding("DE_HEALTH_DATA", text[a_start:], 0.86, "", "u1", a_start, len(text))
    kept = _resolve_overlaps([art9], text)
    assert len(kept) == 1
    assert "insulinpflichtig" in kept[0].value, "health data past the comma was left in the document"


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
