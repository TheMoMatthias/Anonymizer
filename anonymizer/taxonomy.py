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


@dataclass(frozen=True)
class DataClass:
    key: str
    display: str
    sensitivity: str  # "high" | "medium" | "low"
    order: int  # lower = shown first (riskier categories float up)


# The canonical data classes, ordered most-sensitive first.
PEOPLE = DataClass("people", "People", "high", 0)
GOVERNMENT_IDS = DataClass("government_ids", "Government IDs", "high", 1)
FINANCIAL_IDS = DataClass("financial_ids", "Financial IDs", "high", 2)
CONTACT = DataClass("contact", "Contact details", "medium", 3)
BANK_INTERNAL = DataClass("bank_internal", "Bank-internal refs", "medium", 4)
ORG_PLACES = DataClass("org_places", "Organizations & places", "medium", 5)
DATES_OTHER = DataClass("dates_other", "Dates & other", "low", 6)
UNMATCHED = DataClass("unmatched", "Possible misses (unmatched)", "low", 7)

DATA_CLASSES = [
    PEOPLE,
    GOVERNMENT_IDS,
    FINANCIAL_IDS,
    CONTACT,
    BANK_INTERNAL,
    ORG_PLACES,
    DATES_OTHER,
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
    "EMAIL_ADDRESS": CONTACT,
    "PHONE_NUMBER": CONTACT,
    "BANK_INTERNAL_REF": BANK_INTERNAL,
    "DENY_LIST": BANK_INTERNAL,
    "ORGANIZATION": ORG_PLACES,
    "ORG": ORG_PLACES,
    "LOCATION": ORG_PLACES,
    "GPE": ORG_PLACES,
    "DATE_TIME": DATES_OTHER,
    "NRP": DATES_OTHER,
    POSSIBLE_MISS: UNMATCHED,
}


def data_class_for(entity_type: str) -> DataClass:
    """The data class an entity type belongs to; unknown types -> DATES_OTHER."""
    return _ENTITY_TO_CLASS.get(entity_type, DATES_OTHER)


def tier_for(score: float, high: float = 0.9, medium: float = 0.5) -> str:
    """Maps a confidence score to a trust tier. High-tier findings are eligible
    for auto-accept; medium/low are surfaced for active review."""
    if score >= high:
        return TIER_HIGH
    if score >= medium:
        return TIER_MEDIUM
    return TIER_LOW
