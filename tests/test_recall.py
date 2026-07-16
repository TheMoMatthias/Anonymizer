"""Recall coverage for the German-banking recognizers added in the Phase-4
audit: BIC/SWIFT, dates/DOB, postal addresses, bare customer numbers, and the
country-code-gated BIC validator used by the completeness backstop.

All scans force a single German language (`languages: ["de"]`) to mirror the
real per-document routing the pipeline applies.
"""

from anonymizer.core import detect_unit
from anonymizer.models import TextUnit
from anonymizer.validators import bic_valid


def _types(analyzer, base_config, text):
    cfg = {**base_config, "languages": ["de"]}
    findings = detect_unit(analyzer, TextUnit("u1", text), cfg)
    return {(f.entity_type, f.value) for f in findings}


def test_bic_does_not_leak(analyzer, base_config):
    """A labeled BIC must be caught. It may classify as BIC_CODE or, when spaCy
    also tags the token as ORGANIZATION (which can outrank the context-gated
    BIC), under that category -- either way it is redacted, which is the property
    that matters. (Unlabeled BICs are covered by the completeness backstop.)"""
    typed = _types(analyzer, base_config, "Bitte überweisen an BIC: COBADEFFXXX zeitnah.")
    assert any("COBADEFF" in v for _et, v in typed)


def test_plain_uppercase_word_not_flagged_as_bic(analyzer, base_config):
    # No bic/swift context -> below threshold; must not flag an 8-letter word.
    typed = _types(analyzer, base_config, "Bitte das DOKUMENT prüfen und danach ABSENDEN.")
    assert not any(et == "BIC_CODE" for et, _v in typed)


def test_german_date_detected(analyzer, base_config):
    typed = _types(analyzer, base_config, "Geburtsdatum: 15.03.1980 des Kunden.")
    assert any(et == "DATE_TIME" and "15.03.1980" in v for et, v in typed)


def test_address_street_detected(analyzer, base_config):
    typed = _types(analyzer, base_config, "Anschrift: Musterstraße 12a in der Akte.")
    assert any(et == "DE_ADDRESS" and "Musterstraße" in v for et, v in typed)


def test_plz_city_detected(analyzer, base_config):
    typed = _types(analyzer, base_config, "Wohnort ist 50667 Köln laut Unterlagen.")
    assert any(et == "DE_ADDRESS" and "50667" in v for et, v in typed)


def test_kundennummer_detected_with_context(analyzer, base_config):
    typed = _types(analyzer, base_config, "Die Kundennummer 4830123 ist im System.")
    assert any(et == "DE_KUNDENNUMMER" and "4830123" in v for et, v in typed)


def test_anchored_name_detected_without_sentence_context(analyzer, base_config):
    """de_core_news_lg is WikiNER-trained, so it misses a name with no sentence
    context -- including the most common line in a German bank letter. The
    structural anchors must catch these, and must NOT swallow the honorific."""
    for text in ("Sehr geehrter Herr Müller,", "Name: Müller", "Kunde: Müller"):
        typed = _types(analyzer, base_config, text)
        assert ("PERSON", "Müller") in typed, f"missed the name in {text!r}: {typed}"


def test_honorific_is_not_part_of_the_name(analyzer, base_config):
    """'Herr Müller' and a bare 'Müller' must be ONE person, not two tokens."""
    typed = _types(analyzer, base_config, "Herr Müller hat das Konto eröffnet.")
    assert ("PERSON", "Müller") in typed


def test_misc_entities_surface_instead_of_being_dropped(analyzer, base_config):
    """Regression: spaCy tags 'Frau Bauer' as MISC; Presidio's mapping had no
    MISC key so the span was silently DISCARDED and the name leaked."""
    typed = _types(analyzer, base_config, "Frau Bauer zahlt.")
    assert any("Bauer" in v for _et, v in typed), f"MISC entity dropped: {typed}"


def test_lowercase_word_never_matches_case_sensitive_pattern(analyzer, base_config):
    """Regression: Presidio defaults to IGNORECASE, so the [A-Z] BIC pattern
    matched ordinary German words. At sensitivity 0.15 that redacted them."""
    cfg = {**base_config, "languages": ["de"], "sensitivity": 0.15}
    findings = detect_unit(analyzer, TextUnit("u1", "Sehr geehrter Herr, wie ausgefuehrt."), cfg)
    assert not any(f.entity_type == "BIC_CODE" for f in findings)


def test_name_propagates_across_the_document(tmp_path, analyzer, base_config):
    """The anchored salutation seeds the name; propagation must then catch the
    bare occurrences NER cannot see."""
    from docx import Document

    from anonymizer.pipeline import scan_document

    doc = Document()
    doc.add_paragraph("Sehr geehrter Herr Müller,")
    doc.add_paragraph("Die Unterlagen wurden von Müller geprüft.")
    doc.add_paragraph("Müller")
    path = tmp_path / "letter.docx"
    doc.save(path)

    persons = [g for g in scan_document(path, analyzer, base_config).all_actionable() if g.entity_type == "PERSON"]
    match = [g for g in persons if g.value == "Müller"]
    assert match, f"name not detected at all: {[(g.entity_type, g.value) for g in persons]}"
    assert match[0].count >= 3, f"propagation missed bare occurrences (count={match[0].count})"


def test_name_column_header_catches_bare_surnames(tmp_path, analyzer, base_config):
    """A spreadsheet column headed 'Name' is the one place a bare surname
    legitimately appears with no prose. NER only finds ~35% of ordinary German
    surnames there; the header is stronger evidence than the model."""
    import openpyxl

    from anonymizer.pipeline import scan_document

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "Name"
    for row, surname in enumerate(["Müller", "Weber", "Bauer", "Koch"], start=2):
        ws[f"A{row}"] = surname
    path = tmp_path / "kunden.xlsx"
    wb.save(path)

    found = {g.value for g in scan_document(path, analyzer, base_config).all_actionable()}
    for surname in ("Müller", "Weber", "Bauer", "Koch"):
        assert surname in found, f"{surname} leaked from a 'Name' column: {found}"


def test_name_column_override_is_header_gated(tmp_path, analyzer, base_config):
    """The override must key off the HEADER, not blanket-flag every column. Uses
    a value spaCy ignores on its own ("Vorsorge"), so the header is the only
    variable: flagged under 'Name', untouched under 'Produktgruppe'."""
    import openpyxl

    from anonymizer.pipeline import scan_document

    def _scan(header: str) -> set[str]:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws["A1"] = header
        ws["A2"] = "Vorsorge"
        path = tmp_path / f"{header}.xlsx"
        wb.save(path)
        return {g.value for g in scan_document(path, analyzer, base_config).all_actionable()}

    assert "Vorsorge" not in _scan("Produktgruppe"), "override fired without a name header"
    assert "Vorsorge" in _scan("Name"), "override did not fire under a name header"


def test_bic_valid_country_gate():
    assert bic_valid("COBADEFFXXX")  # Commerzbank Frankfurt, DE
    assert bic_valid("DEUTDEFF")  # 8-char BIC, DE
    assert not bic_valid("TRANSFER")  # 'SF' not an ISO country
    assert not bic_valid("HELLO")  # wrong shape
