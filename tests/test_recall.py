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


def test_bic_valid_country_gate():
    assert bic_valid("COBADEFFXXX")  # Commerzbank Frankfurt, DE
    assert bic_valid("DEUTDEFF")  # 8-char BIC, DE
    assert not bic_valid("TRANSFER")  # 'SF' not an ISO country
    assert not bic_valid("HELLO")  # wrong shape
