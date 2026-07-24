"""Turns a decided (entity_type, value, action) into the concrete replacement
text, and back again for re-identification.

Placeholder style: readable, bracketed, typed tokens -- `[PERSON_1]`, `[IBAN_3]`
for pseudonymized values (consistent across documents via the mapping), and a
bare `[PERSON]` / `[IBAN]` for one-way anonymized values. Brackets make it
obvious to a downstream AI that the token is a placeholder, and let us find and
reverse pseudonyms unambiguously.
"""

from __future__ import annotations

import re

from .mapping import MappingStore

# entity_type -> short, human-readable token label. Keeps tokens compact
# ([IBAN_1] rather than [IBAN_CODE_1]) while staying unambiguous.
TOKEN_LABELS = {
    "PERSON": "PERSON",
    "ORGANIZATION": "ORG",
    "ORG": "ORG",
    "LOCATION": "LOCATION",
    "GPE": "LOCATION",
    "EMAIL_ADDRESS": "EMAIL",
    "PHONE_NUMBER": "PHONE",
    "DE_PHONE": "PHONE",
    "IBAN_CODE": "IBAN",
    "CREDIT_CARD": "CARD",
    "DE_STEUER_ID": "STEUER_ID",
    "DE_SV_NUMMER": "SV_NUMMER",
    "DE_KONTONUMMER": "KONTO",
    "DE_DEPOTNUMMER": "DEPOT",
    "BIC_CODE": "BIC",
    "DE_ADDRESS": "ADDRESS",
    "DE_KUNDENNUMMER": "KUNDENNR",
    "BANK_INTERNAL_REF": "REF",
    "DATE_TIME": "DATE",
    "NER_MISC": "ENTITY",
    "DENY_LIST": "REDACTED",
    # Topical (non-personal) categories.
    "TOOL": "TOOL",
    "DIVISION": "DIVISION",
    "DEPARTMENT": "DEPT",
    "LICENSEE": "LICENSEE",
    "PROJECT": "PROJEKT",
    "DESCRIPTION": "TEXT",
}

# Matches a rendered token like [PERSON_1] or [IBAN] for re-identification.
# Digits are allowed in the label: a custom entity type with a digit in its name
# (e.g. a second BIC recognizer, DE_BIC2) renders [DE_BIC2_1], which a
# digit-less label class would silently fail to match -- leaving the placeholder
# unrestored with no error.
TOKEN_RE = re.compile(r"\[([A-Z0-9_]+?)(?:_(\d+))?\]")


def token_label(entity_type: str) -> str:
    return TOKEN_LABELS.get(entity_type, entity_type)


_SENTENCE_SPLIT = re.compile(r"[.!?]+")


def _structural_summary(value: str) -> str:
    """A zero-content SHAPE descriptor of a value: sentence/line count and
    approximate size, NO original characters. Feeds the 'summarize' mode so a
    downstream LLM learns the cell's format/size without its confidential
    content -- and so the fail-loud verify (which forbids any retained original)
    passes by construction. Deterministic (pure function of the value), so
    scan/apply stay in parity and the per-cell caches stay valid."""
    v = value.strip()
    lines = [ln for ln in re.split(r"[\r\n]+", v) if ln.strip()]
    if len(lines) >= 2:
        return f"Liste, {len(lines)} Einträge"
    n = max(1, len([s for s in _SENTENCE_SPLIT.split(v) if s.strip()]))
    return f"{n} {'Satz' if n == 1 else 'Sätze'}, ~{len(v)} Zeichen"


def resolve_replacement(entity_type: str, value: str, action: str, mapping_store: MappingStore) -> str | None:
    """Returns the replacement text for a decided action, or None if the match
    should be left untouched (skip).

    Modes: skip -> leave; pseudonymize -> consistent reversible `[PERSON_1]`
    token; redact/anonymize -> one-way `[PERSON]`; summarize -> a zero-content
    structural placeholder `[PROJEKT: 3 Sätze, ~140 Zeichen]` (never reversible,
    never contains original text)."""
    if action == "skip":
        return None
    if action == "pseudonymize":
        placeholder = mapping_store.get_or_create(entity_type, value, label=token_label(entity_type))
        return f"[{placeholder}]"
    if action == "summarize":
        return f"[{token_label(entity_type)}: {_structural_summary(value)}]"
    # "redact" (and legacy "anonymize"): one-way bare label.
    return f"[{token_label(entity_type)}]"


def decisions_lookup(decisions: dict[tuple[str, str], str], entity_type: str, value: str) -> str:
    return decisions.get((entity_type, value.strip().lower()), "skip")


def reidentify_text(text: str, mapping_store: MappingStore) -> tuple[str, int]:
    """Replaces pseudonym tokens (`[PERSON_1]`, ...) in `text` with their
    original values via the mapping. Returns (restored_text, tokens_replaced).
    Unknown tokens (unmapped or one-way anonymized) are left untouched."""
    count = 0

    def repl(match: re.Match) -> str:
        nonlocal count
        placeholder = match.group(0)[1:-1]  # strip the surrounding [ ]
        original = mapping_store.reverse(placeholder)
        if original is None:
            return match.group(0)
        count += 1
        return original

    return TOKEN_RE.sub(repl, text), count
