"""Maps low-level entity types to human-meaningful *data classes* (sensitivity
categories) so the reviewer decides per category, not per detected value.

Each entity type belongs to exactly one data class. The data class carries a
sensitivity level (used for display/ordering and to sort riskier categories
first) and a stable ordering. Default *actions* stay per-entity-type in the
config -- the data class is the review/decision grouping, not the source of the
default action.
"""

from __future__ import annotations

from dataclasses import dataclass

# Trust tiers -- confidence bands that drive the 3-tier auto-accept model.
TIER_HIGH = "high"
TIER_MEDIUM = "medium"
TIER_LOW = "low"

# Entity type used for the completeness/unmatched-risk bucket (see core.py).
POSSIBLE_MISS = "POSSIBLE_MISS"

# Topical (non-personal, organizational) category entity types. Detected
# structurally (header->category) or by the auto-gazetteer, NOT by personal-
# entity NER. See docs/run_topical-sensitivity_2026-07-23.md.
TOPICAL_ENTITY_TYPES = ("TOOL", "DIVISION", "DEPARTMENT", "LICENSEE", "PROJECT", "DESCRIPTION")
# Topical categories whose VALUES are names worth propagating document-wide (a
# tool named in a Tools column is a tool elsewhere too). DESCRIPTION is excluded:
# free-text descriptions are unique and whole-cell-handled, never propagated.
PROPAGATING_TOPICAL_TYPES = ("TOOL", "DIVISION", "DEPARTMENT", "LICENSEE", "PROJECT")
# Informational bucket: capitalized/OOV tokens that look like they COULD be an
# internal term but weren't in a labelled column -- surfaced for promote-or-skip,
# never auto-redacted.
POSSIBLE_TOPICAL = "POSSIBLE_TOPICAL"


@dataclass(frozen=True)
class DataClass:
    key: str
    display: str
    sensitivity: str  # "high" | "medium" | "low"
    order: int  # lower = shown first (riskier categories float up)


# The canonical data classes, ordered most-sensitive first.
PEOPLE = DataClass("people", "People", "high", 0)
# GDPR Art. 9 special-category data (nationality / religious or political group).
# HIGH sensitivity and never bucketed with dates -- it must never fall into a
# profile's dates "skip". Ordered right after People.
SPECIAL_CATEGORY = DataClass("special_category", "Special category (GDPR Art. 9)", "high", 1)
GOVERNMENT_IDS = DataClass("government_ids", "Government IDs", "high", 2)
FINANCIAL_IDS = DataClass("financial_ids", "Financial IDs", "high", 3)
CONTACT = DataClass("contact", "Contact details", "medium", 4)
BANK_INTERNAL = DataClass("bank_internal", "Bank-internal refs", "medium", 5)
# Non-personal organizational sensitivity: internal tools, divisions,
# departments, licensees, confidential project descriptions. Grouped so the five
# topical categories share one review cluster (see docs/run_topical-*).
INTERNAL_TOPICAL = DataClass("internal_topical", "Internal / topical", "medium", 6)
ORG_PLACES = DataClass("org_places", "Organizations & places", "medium", 7)
# spaCy's German model tags entities it cannot classify as MISC -- and real
# names land there. They are not confidently people, so they get their own
# review bucket rather than being forced into People (wrong) or dropped (a
# silent leak, which is what happened before).
OTHER_ENTITIES = DataClass("other_entities", "Other named entities", "medium", 8)
DATES_OTHER = DataClass("dates_other", "Dates & other", "low", 9)
# Informational: possible internal/topical terms (promote-or-skip, not redacted).
TOPICAL_CANDIDATES = DataClass("topical_candidates", "Possible internal/topical terms", "low", 10)
UNMATCHED = DataClass("unmatched", "Possible misses (unmatched)", "low", 11)

DATA_CLASSES = [
    PEOPLE,
    SPECIAL_CATEGORY,
    GOVERNMENT_IDS,
    FINANCIAL_IDS,
    CONTACT,
    BANK_INTERNAL,
    INTERNAL_TOPICAL,
    ORG_PLACES,
    OTHER_ENTITIES,
    DATES_OTHER,
    TOPICAL_CANDIDATES,
    UNMATCHED,
]

# entity_type -> DataClass. Unknown/custom entity types fall back to DATES_OTHER
# (see data_class_for), so a colleague's newly-added recognizer still groups
# somewhere sensible without a code change.
_ENTITY_TO_CLASS: dict[str, DataClass] = {
    "PERSON": PEOPLE,
    "DE_STEUER_ID": GOVERNMENT_IDS,
    "DE_SV_NUMMER": GOVERNMENT_IDS,
    "IBAN_CODE": FINANCIAL_IDS,
    "CREDIT_CARD": FINANCIAL_IDS,
    "DE_KONTONUMMER": FINANCIAL_IDS,
    "DE_DEPOTNUMMER": FINANCIAL_IDS,
    "BIC_CODE": FINANCIAL_IDS,
    "EMAIL_ADDRESS": CONTACT,
    "PHONE_NUMBER": CONTACT,
    "DE_PHONE": CONTACT,
    "DE_ADDRESS": CONTACT,
    "BANK_INTERNAL_REF": BANK_INTERNAL,
    "DE_KUNDENNUMMER": BANK_INTERNAL,
    "DENY_LIST": BANK_INTERNAL,
    "ORGANIZATION": ORG_PLACES,
    "ORG": ORG_PLACES,
    "LOCATION": ORG_PLACES,
    "GPE": ORG_PLACES,
    "NER_MISC": OTHER_ENTITIES,
    "DATE_TIME": DATES_OTHER,
    "NRP": SPECIAL_CATEGORY,  # nationality / religion / political group -- GDPR Art. 9
    "TOOL": INTERNAL_TOPICAL,
    "DIVISION": INTERNAL_TOPICAL,
    "DEPARTMENT": INTERNAL_TOPICAL,
    "LICENSEE": INTERNAL_TOPICAL,
    "PROJECT": INTERNAL_TOPICAL,
    "DESCRIPTION": INTERNAL_TOPICAL,
    POSSIBLE_TOPICAL: TOPICAL_CANDIDATES,
    POSSIBLE_MISS: UNMATCHED,
}


def data_class_for(entity_type: str) -> DataClass:
    """The data class an entity type belongs to. An unknown/custom type falls back
    to OTHER_ENTITIES (medium, review-tier) -- NOT the low, profile-skippable
    DATES_OTHER, so a colleague's newly-added recognizer is never silently skipped."""
    return _ENTITY_TO_CLASS.get(entity_type, OTHER_ENTITIES)


def tier_for(score: float, high: float = 0.9, medium: float = 0.5) -> str:
    """Maps a confidence score to a trust tier. High-tier findings are eligible
    for auto-accept; medium/low are surfaced for active review."""
    if score >= high:
        return TIER_HIGH
    if score >= medium:
        return TIER_MEDIUM
    return TIER_LOW
