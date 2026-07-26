"""Turns a decided (entity_type, value, action) into the concrete replacement
text, and back again for re-identification.

Placeholder style: readable, bracketed, typed tokens -- `[PERSON_1]`, `[IBAN_3]`
for pseudonymized values (consistent across documents via the mapping), and a
bare `[PERSON]` / `[IBAN]` for one-way anonymized values. Brackets make it
obvious to a downstream AI that the token is a placeholder, and let us find and
reverse pseudonyms unambiguously.

The two forms share one namespace, so they must never be confusable: a pseudonym
is exactly `[<LABEL>_<digits>]`, and a one-way token is never allowed to take that
shape (see `one_way_token`).
"""

from __future__ import annotations

import re

from .mapping import MappingStore, UnreversibleTokenError

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
# The label class is ASCII on purpose, and this pattern is the SINGLE authority on
# what a pseudonym may look like: _assert_pseudonym_round_trips tests every
# candidate against TOKEN_RE itself before it is minted, so the mint rule can never
# drift from what re-identification actually accepts.
TOKEN_RE = re.compile(r"\[([A-Z0-9_]+?)(?:_(\d+))?\]")

# A label that already ends in _<digits> is indistinguishable from "some other
# label plus a pseudonym number". Column policies derive the label from the
# spreadsheet HEADER (xlsx_handler._column_entity_type: "Notizen 2" -> NOTIZEN_2,
# "Person 1" -> PERSON_1), so a bare one-way token [NOTIZEN_2] collided with the
# pseudonym NOTIZEN_2 minted for a pseudonymized "Notizen" column: re-identifying
# that document replaced a ONE-WAY redacted cell with another row's REAL value.
# One-way tokens for such labels therefore carry an explicit terminator, which can
# never be parsed as label+number. Unambiguous labels (the overwhelming majority --
# PERSON, IBAN, PROJEKT) keep the plain, readable [LABEL] form.
_NUMBERED_TAIL_RE = re.compile(r"_\d+$")
ONE_WAY_SUFFIX = "_REDACTED"


def token_label(entity_type: str) -> str:
    return TOKEN_LABELS.get(entity_type, entity_type)


def _assert_pseudonym_round_trips(placeholder: str, entity_type: str) -> None:
    """Refuses to render a PSEUDONYM that reidentify_text could never find again.

    Labels are not ours to choose: they come from config-defined entity names and
    from spreadsheet HEADERS (xlsx_handler._column_entity_type). A header like
    'Prüfung' has to reach us transliterated (PRUEFUNG) to be usable at all: left
    as-is it minted PRÜFUNG_1, wrote [PRÜFUNG_1] into the document, and TOKEN_RE --
    whose label class is [A-Z0-9_] -- could never match it again. The document was
    permanently unreversible and the interface reported
    "Restored 0 value(s)." as a cheerful *info* toast. This is the backstop under
    that fix: the check is performed with TOKEN_RE ITSELF, so it can never drift
    from what re-identification actually accepts, and it fires at MINT time --
    while the operator can still fix the header -- instead of years later when
    someone needs the original value back.

    The test is deliberately made against `[<label>_1]`: if that round-trips, so
    does every `[<label>_<n>]`, and validating BEFORE the mint keeps an unusable
    row out of the mapping entirely.

    Widening TOKEN_RE to accept umlauts was rejected. It would fix this one path
    while leaving the class of bug open: an ASCII-only token is the only shape
    that survives the round trip through everything a document passes through --
    Unicode normalisation (a composed 'Ü' can come back decomposed as 'U'+U+0308
    and stop matching), Python's own case mapping ('ß'.upper() is 'SS', so the
    label is not even stable under the .upper() that produces it), and non-UTF-8
    exports. It would also make the token pattern match arbitrary non-ASCII words
    in bracketed prose, giving re-identification new false positives on documents
    it must not touch. The right answer is: keep tokens ASCII, and make anything
    else impossible to emit rather than merely unlikely.
    """
    token = f"[{placeholder}]"
    match = TOKEN_RE.fullmatch(token)
    if match is not None and match.group(2) is not None:
        return
    raise UnreversibleTokenError(
        f"Refusing to write the pseudonym token '{token}' (entity type "
        f"'{entity_type}'): re-identification could never match it again, so the "
        "output document would be permanently unreversible. Placeholder labels "
        "must be upper-case ASCII letters, digits and underscores only -- rename "
        "the source column/entity type (umlauts included: use UE, OE, AE, SS) and "
        "run again. No file was written."
    )


def one_way_token(entity_type: str) -> str:
    """The rendered token for a ONE-WAY anonymized value -- guaranteed never to
    match a pseudonym placeholder, so re-identification can never turn it back
    into anyone's data.

    Deliberately NOT subject to the round-trip check that guards pseudonyms: a
    one-way token is not supposed to be reversible, so a label TOKEN_RE cannot
    match (e.g. an umlaut one) fails safe here -- the token is simply left alone
    by re-identification, which is precisely the required behaviour. Rejecting it
    would refuse a document that is in fact perfectly correct."""
    label = token_label(entity_type)
    if _NUMBERED_TAIL_RE.search(label):
        return f"[{label}{ONE_WAY_SUFFIX}]"
    return f"[{label}]"


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
        label = token_label(entity_type)
        # Before the mint, so a label that can never round-trip leaves no row
        # behind; and again after it, because get_or_create may return a LEGACY
        # placeholder stored under a different (possibly unreversible) label.
        _assert_pseudonym_round_trips(f"{label}_1", entity_type)
        placeholder = mapping_store.get_or_create(entity_type, value, label=label)
        _assert_pseudonym_round_trips(placeholder, entity_type)
        return f"[{placeholder}]"
    if action == "summarize":
        return f"[{token_label(entity_type)}: {_structural_summary(value)}]"
    # "redact" (and legacy "anonymize"): one-way bare label -- rendered by
    # one_way_token, not inline, so a NUMBERED label (a "Projekt_2" spreadsheet
    # header) still gets its explicit _REDACTED terminator and can never be
    # re-parsed as a reversible [LABEL_n] pseudonym pointing at another row.
    return one_way_token(entity_type)


def decisions_lookup(decisions: dict[tuple[str, str], str], entity_type: str, value: str) -> str:
    return decisions.get((entity_type, value.strip().lower()), "skip")


def reidentify_text(text: str, mapping_store: MappingStore) -> tuple[str, int]:
    """Replaces pseudonym tokens (`[PERSON_1]`, ...) in `text` with their
    original values via the mapping. Returns (restored_text, tokens_replaced).
    Unknown tokens (unmapped or one-way anonymized) are left untouched."""
    count = 0

    def repl(match: re.Match) -> str:
        nonlocal count
        # Only a token SHAPED like a pseudonym ([LABEL_<digits>]) is ever looked
        # up. A one-way token can carry no number, so it can never be turned back
        # into data even if some future label happened to equal a placeholder.
        if match.group(2) is None:
            return match.group(0)
        placeholder = match.group(0)[1:-1]  # strip the surrounding [ ]
        original = mapping_store.reverse(placeholder)
        if original is None:
            return match.group(0)
        count += 1
        return original

    return TOKEN_RE.sub(repl, text), count
