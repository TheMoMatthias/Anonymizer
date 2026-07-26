"""Source-based Medium-tier split (Pass 2 of the 2026-07-23 detection/GUI
investigation, see docs/run_detection-precision_2026-07-23.md): a raw spaCy
NER guess with nothing else corroborating it is a different kind of "medium"
than a pattern/checksum-anchored hit that merely sits under the high-tier bar.
GroupedFinding.is_ner_guess drives review.py's split of the Medium band.
"""

from anonymizer import core
from anonymizer.models import Finding, TextUnit

CONFIG = {
    "entities": {
        "NER_MISC": {"default_action": "pseudonymize"},
        "PERSON": {"default_action": "pseudonymize"},
        "IBAN_CODE": {"default_action": "pseudonymize"},
    },
    "tiers": {"high": 0.9, "medium": 0.5},
    # These tests exercise the is_ner_guess FLAG in isolation; the separate
    # corroboration-only DROP policy (which would remove bare ORG/LOC/MISC
    # guesses) is off here so the flag itself can be observed.
    "corroboration_only": False,
}


def _scan(findings):
    units = [TextUnit("u1", "irrelevant for this test")]
    return core.build_scan_result(findings, units, CONFIG)


def test_raw_ner_hit_is_flagged_as_a_guess():
    result = _scan([Finding("NER_MISC", "Migration", 0.85, "ctx", "u1", 0, 9, source="SpacyRecognizer")])
    g = result.all_actionable()[0]
    assert g.is_ner_guess


def test_pattern_backed_hit_is_not_a_guess():
    result = _scan([Finding("PERSON", "Müller", 0.75, "ctx", "u1", 0, 6, source="PatternRecognizer")])
    g = result.all_actionable()[0]
    assert not g.is_ner_guess


def test_one_corroborating_occurrence_clears_the_guess_flag():
    """The same value seen once as a bare NER guess and once via a pattern
    match (e.g. propagation, or the honorific-anchored pattern elsewhere in
    the document) is corroborated -- it must not read as "just a guess"."""
    result = _scan(
        [
            Finding("PERSON", "Müller", 0.85, "ctx", "u1", 0, 6, source="SpacyRecognizer"),
            Finding("PERSON", "Müller", 0.75, "ctx", "u2", 10, 16, source="PatternRecognizer"),
        ]
    )
    g = result.all_actionable()[0]
    assert g.count == 2
    assert not g.is_ner_guess


def test_checksum_backed_entity_is_never_a_guess_regardless_of_source():
    """is_ner_guess only applies to the free-text NER entity types -- an
    IBAN is never 'a guess' even if source were somehow mislabeled."""
    result = _scan(
        [Finding("IBAN_CODE", "DE89370400440532013000", 0.98, "ctx", "u1", 0, 22, validated=True, source="SpacyRecognizer")]
    )
    g = result.all_actionable()[0]
    assert not g.is_ner_guess


def test_overlap_resolution_preserves_corroborating_source():
    """Regression: a raw NER candidate and a same-span, differently-sourced
    candidate (e.g. a whole-cell override) on the IDENTICAL span used to have
    the NER candidate silently win the tie-break, discarding the
    corroboration entirely -- making a value that TWO mechanisms agreed on
    read as 'just a guess'. _resolve_overlaps must retain the corroborating
    source whichever candidate wins the span/score tie-break."""
    text = "Klaus von Bergen"
    ner_hit = Finding("PERSON", text, 0.85, "ctx", "u1", 0, len(text), source="SpacyRecognizer")
    override_hit = Finding("PERSON", text, 0.8, "ctx", "u1", 0, len(text), source="whole_cell_override")

    kept = core._resolve_overlaps([ner_hit, override_hit], text)
    assert len(kept) == 1
    assert kept[0].source == "whole_cell_override"

    result = _scan(kept)
    assert not result.all_actionable()[0].is_ner_guess
