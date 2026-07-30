"""Shared detection + review core.

This is the single detection code path used by BOTH scan and apply, so what the
reviewer approved is exactly what gets written (scan/apply parity by
construction -- no divergent per-handler detection logic). Format handlers are
thin adapters: they only turn a document into TextUnits and apply span
replacements; all the "what is sensitive and what tier is it" logic lives here.
"""

from __future__ import annotations

import functools
import re
from dataclasses import replace
from pathlib import Path

from . import taxonomy, validators
from .actions import token_label
from .engine import DEFAULT_LANGUAGES, HONORIFIC_PREFIX_RE
from .gliner_recognizer import GLINER_SOURCE
from .models import DataClassGroup, Finding, GroupedFinding, PreviewGroup, PreviewRow, ScanResult, TextUnit

CONTEXT_SNIPPET_RADIUS = 40

# Generic free-text NER labels (spaCy). On an exact span+score tie during
# overlap resolution, a specific pattern/checksum recognizer is preferred over
# these, so e.g. a full DE_ADDRESS wins over a bare LOCATION on the same span.
_NER_ENTITIES = frozenset({"PERSON", "LOCATION", "ORGANIZATION", "GPE", "NRP", "NER_MISC"})

# How many distinct possible-misses to surface before truncating (informational
# bucket -- a full list of every digit-run in a 200-page doc helps no one).
MAX_POSSIBLE_MISSES = 300

# Confidence assigned to a checksum-validated ID (forces the high/auto-accept
# tier) and to one whose checksum FAILED. A failing checksum no longer zeroes
# the finding: a typo'd / OCR'd IBAN or card number is still an identifying
# string that must not leak, so it is DEMOTED to a review-tier score (kept, and
# flagged with the "unverified" chip) instead of dropped -- the reviewer decides.
# A checksum-FAILED finding (validated is False) BYPASSES the score-threshold gate
# in detect_unit, so an ID whose threshold sits above this demoted score (e.g.
# Steuer-ID at 0.6) is still surfaced for review rather than silently filtered.
_VALIDATED_SCORE = 0.98
_INVALID_SCORE = 0.4

# Confidence given to a value propagated from elsewhere in the same document.
# Matches spaCy's flat PERSON score, so it lands in the review tier rather than
# auto-accept -- propagated hits are inference, not observation.
_PROPAGATED_SCORE = 0.85

# spaCy returns the honorific INSIDE the person span ("Herr Müller"). Trimming
# it keys the pseudonym on the name itself, so "Herr Müller" here and a bare
# "Müller" in a table cell become the SAME token rather than two people -- and
# it gives document-wide propagation the right seed to match on.
# `Herrn?` covers the dative "Herrn" that opens a German postal address block
# ("Herrn\n<Name>\n<Straße>") -- a plain "Herr" pattern silently misses it. Kept in
# sync with engine._HONORIFICS and pipeline's honorific stripper.
_HONORIFIC_PREFIX = HONORIFIC_PREFIX_RE

# Any leading character that isn't part of a word (a bullet "-", a stray "."
# from a glued file-extension-style token, a bracket, ...). spaCy's tokenizer
# can fuse such a character onto the following word when there's no space
# between them ("...proprietären .iboflow-Format..."), and NER then tags the
# fused token -- including the punctuation -- as an entity. Stripped
# regardless of root cause, from every free-text NER finding, as a safety net.
_LEADING_NOISE = re.compile(r"^[^\w]+", re.UNICODE)

# NER_MISC/ORGANIZATION/LOCATION are the three entity types with NO structural
# validation (no checksum, no anchoring pattern) backing them -- a raw spaCy
# span at spaCy's flat ~0.85 score, full stop. This is where free-text noise
# ("aber", "abdeckung") ends up. PERSON is deliberately excluded: it already
# has other scrutiny (honorific/labelled-name patterns, propagation) and a
# blanket case/stopword filter there would risk real lowercase surnames.
_PRECISION_GATED_ENTITIES = frozenset({"NER_MISC", "ORGANIZATION", "LOCATION"})

# Free-text NER types with NO structural validation behind them -- a raw model span at
# a flat score. In corroboration-only mode one of these is surfaced in the main list
# ONLY when something beyond a bare guess backs it (a pattern/anchor source, a checksum,
# a name-column or topical override, a given-name match, a GLiNER hit); otherwise it is
# DEMOTED to its own band, never dropped.
#
# PERSON joined this set on 2026-07-27 -- the core of the precision rework.
#
# Why: on a real workbook 84% of PERSON *values* and 91% of PERSON *occurrences* were not
# people. They were German common nouns, which NO part-of-speech filter can reject:
# German capitalizes every noun, and NOUN has to stay name-like because a surname like
# "Bauer" is tagged NOUN. Filtering the WORD was the wrong model; requiring EVIDENCE is
# the right one. Measured effect on the audit workbook: false positives 28/84 -> 4/84
# (86% cut) with recall holding at 293/293 and the apply round trip clean.
#
# ONLY SAFE ALONGSIDE THE CORROBORATION SOURCES. On the audited export EVERY real person
# had is_ner_guess=True, so this set without those sources would discard every name.
# They are: name-column headers (widened + boundary-matched), the given-name gazetteer
# (is_given_name), a GLiNER hit (already excluded from is_guess), propagation from any of
# those, and genitive inheritance (below).
#
# Two LEAKS had to be fixed first, both found by the fail-loud verify and both real --
# these values were previously redacted only because everything was:
#   * English honorifics were never stripped, so "Mr Amina Adeyemi" kept its title: the
#     gazetteer tested "Mr", found no given name, and demoted the whole name. Fixed by
#     deriving both strippers from engine._HONORIFICS (see HONORIFIC_PREFIX_RE).
#   * a German GENITIVE ("Kochs") formed its own uncorroborated group, so demoting it
#     left the surname legible while "Koch" was redacted everywhere else. Fixed by
#     genitive inheritance below.
#
# PERSON IS NOW IN THIS SET (2026-07-30), and the history of why it took so long is the
# useful part. It was blocked for days by an apparently inherent trade: adding it cut
# false positives 28/84 -> 4/84 but dropped per-occurrence recall on realistic letters
# from 98% to 80%, with six surnames missed ENTIRELY (Winkler, Habermehl, Osterkamp,
# Oeztuerk, Kowalczyk, Demir all 0/5). It was not inherent. It was two defects:
#
#   * Presidio's EntityRecognizer.remove_duplicates() runs INSIDE analyze() and drops a
#     result contained in a higher-scored result of the SAME entity type. spaCy reports
#     PERSON at a flat 0.85, so every anchored pattern below that score was deleted
#     before this module saw it -- taking its corroborating source with it. The anchors
#     only ever worked where spaCy did not also fire. Fixed by engine._ANCHOR_SCORE=0.86.
#   * corroboration did not cross a (type, value) group boundary, so the same name typed
#     PERSON here and ORGANIZATION there formed two groups and the second was demoted
#     while the identical characters were redacted elsewhere. Fixed by
#     corroborated_any_type + the reverse token direction, below.
#
# With both fixed: false positives 27/84 -> 4/84 AND every recall stratum equal or
# better (full letter and unanchored memo both 100%, spreadsheet cells 77% -> 83%).
#
# Note the instrument trap, still true: the audit workbook reads 293/293 whether PERSON
# is gated or not, because its recall matching is value-keyed and lenient. Only
# scripts/measure_recall.py, scored per OCCURRENCE, can see a regression here. Run BOTH
# before touching this line.
_CORROBORATION_ONLY_ENTITIES = frozenset({"NER_MISC", "ORGANIZATION", "LOCATION", "PERSON"})

# The sources whose _NER_ENTITIES hits are BARE guesses and must therefore clear
# the precision gate: spaCy's model itself, the document-wide propagation of one,
# GLiNER below its confidence override, and any hit that arrived with no
# attribution at all (the propagation call site passes no source -- an
# unattributed hit is assumed to be a guess, so this fails CLOSED).
#
# Anything else reaching the gate with an NER entity type came from an ANCHORED
# pattern recognizer, and is exempt -- see _rejected_by_precision.
# "oov_candidate" is PROPRIETARY_CANDIDATE_SOURCE, spelled literally because that
# constant is declared further down with the gazetteer it belongs to.
_GATED_NER_SOURCES = frozenset(
    {"SpacyRecognizer", "propagation", GLINER_SOURCE, "oov_candidate", ""}
)


# A PERSON candidate whose FIRST token is a known given name is corroborated: the
# value has positive evidence behind it, not just a model's say-so. This is one of the
# four corroboration sources the design calls for (name-column header, given-name
# gazetteer, GLiNER hit, column-level inference), and it is the only one that works on
# a name standing completely alone in prose with no header and no anchor.
GIVEN_NAME_SOURCE = "given_name"
_GIVEN_NAMES_PATH = Path(__file__).resolve().parent / "data" / "given_names.txt"


@functools.lru_cache(maxsize=1)
def _given_names() -> frozenset[str]:
    """The shipped given-name list, lowercased. Cached: read once per process.

    Missing or unreadable is NOT fatal -- it degrades to "this corroboration source
    contributes nothing", exactly like a machine without the ML pack. A detection
    input that can hard-fail a scan by being absent would be a worse bug than the
    recall it buys."""
    try:
        lines = _GIVEN_NAMES_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return frozenset()
    return frozenset(
        s for s in (line.strip().lower() for line in lines) if s and not s.startswith("#")
    )


PRODUCT_NAME_SOURCE = "product_gazetteer"
# The weakest signal in the tool -- see looks_like_proprietary_name. Given its own
# source so build_scan_result can hold such a group in the demoted band
# unconditionally, even when an inheritance rule would otherwise promote it.
PROPRIETARY_CANDIDATE_SOURCE = "oov_candidate"
_PRODUCT_NAMES_PATHS = (
    Path(__file__).resolve().parent / "data" / "product_names.txt",
    Path(__file__).resolve().parent / "data" / "project_names.txt",
)


@functools.lru_cache(maxsize=1)
def _product_names() -> frozenset[str]:
    """Shipped commercial product names PLUS the user's own project codenames.

    Two files, one lookup, because the distinction matters to the person editing
    them and not at all to the matcher: the shipped list covers products that are
    the same at every bank, the project list covers codenames only the user knows.
    Degrades to empty on any read error, for the same reason the given-name list
    does -- a detection input that can hard-fail a scan by being absent would be a
    worse bug than the recall it buys."""
    out: set[str] = set()
    for path in _PRODUCT_NAMES_PATHS:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        out |= {
            s for s in (line.strip().lower() for line in lines) if s and not s.startswith("#")
        }
    return frozenset(out)


def is_known_product(value: str) -> bool:
    """Whether this value is a listed product or project name. Corroboration only:
    it never creates a finding, it confirms one that already exists."""
    return value.strip().lower() in _product_names()


# --- proprietary-name candidates (the weakest signal in the tool) --------------
# For a codename that is in no list and in no declaring column, the last resort is
# vocabulary: is this token German at all?
#
# CAPITALISATION IS USELESS HERE, and that is the whole difficulty. German
# capitalises every noun, so "Die Anbindung an Alteryx" gives a detector no way to
# tell the product from the ordinary word beside it. What does separate them is
# that "Anbindung" is German vocabulary and "Alteryx" is not.
#
# Plain out-of-vocabulary is not enough either, because German COMPOUNDS are
# absent from any word list while being entirely ordinary -- "Portfoliobeitrag",
# "Marktdatengrundlage", "Bearbeitungszeit". So a token counts as a candidate only
# when it is unknown AND does not decompose into known German words. Measured
# against the audit fixture's own decoys: 0/6 ordinary words flagged, 1/12 decoy
# compounds flagged, 7/11 proprietary names flagged.
#
# The residual limit, stated because it decides how much this can ever be trusted:
# codenames chosen from ordinary vocabulary ("Nordstern", "Seidenpfad", "Habicht",
# "Delphin") are UNREACHABLE by this or any other content heuristic -- they are
# indistinguishable from the same words used literally. Only project_names.txt or
# a declaring column finds those.
_COMPOUND_MIN_PART = 4
_COMPOUND_MAX_PARTS = 3
# German glues compounds with a linking -s- or -n-.
_COMPOUND_LINKERS = ("", "s", "n", "es", "en")
_PROPRIETARY_MIN_LEN = 5
_PROPRIETARY_CACHE: dict[str, bool] = {}
_PROPRIETARY_CACHE_MAX = 50_000


def _decomposes_into_vocabulary(word: str, known, depth: int = _COMPOUND_MAX_PARTS) -> bool:
    """Whether `word` splits into up to `depth` known German words. Recursive so a
    three-part stack (Markt+Daten+Grundlage) resolves -- two-part splitting alone
    left exactly those long compounds looking like proprietary names."""
    if known(word):
        return True
    if depth <= 1 or len(word) < 2 * _COMPOUND_MIN_PART:
        return False
    for i in range(_COMPOUND_MIN_PART, len(word) - _COMPOUND_MIN_PART + 1):
        head, tail = word[:i], word[i:]
        if not known(head):
            continue
        for linker in _COMPOUND_LINKERS:
            if linker and not tail.startswith(linker):
                continue
            rest = tail[len(linker):]
            if len(rest) >= _COMPOUND_MIN_PART and _decomposes_into_vocabulary(rest, known, depth - 1):
                return True
    return False


def looks_like_proprietary_name(value: str, known) -> bool:
    """A capitalised token that is neither German vocabulary nor a German compound.
    `known(word)` answers "is this lowercase word in the model's vocabulary"."""
    word = value.strip()
    if len(word) < _PROPRIETARY_MIN_LEN or not word[:1].isupper():
        return False
    if not word.isalpha():
        return False
    # ALL-CAPS is an acronym or a field code (CAPEX, OPEX, RAG), not a product
    # name shape. The commercial acronyms that ARE products (SAP, SWIFT) are in
    # product_names.txt, which is checked before this ever runs.
    if word.isupper():
        return False
    # A German NOMINALIZER suffix is German morphology, so the word is German
    # however absent it is from a vector table -- "Derivatefreiheit", "Effizienz",
    # "Reaktionszeiten". Reusing the same suffix list the precision filter uses
    # keeps the two from disagreeing about what counts as an ordinary noun.
    if _NOMINALIZER_SUFFIX.search(word):
        return False
    cached = _PROPRIETARY_CACHE.get(word)
    if cached is not None:
        return cached
    # Try the ASCII-transliterated spelling too. The model's vocabulary holds
    # "geschäftsbedingungen" with the umlaut, so a document that writes
    # "Geschaeftsbedingungen" -- which German business text does constantly, and
    # which every fixture here does -- looks out-of-vocabulary and would be read as
    # a proprietary name. Measured: it was the only ordinary word this flagged.
    lower = word.lower()
    variants = {lower}
    restored = lower.replace("ae", "ä").replace("oe", "ö").replace("ue", "ü")
    if restored != lower:
        variants.add(restored)
    result = not any(_decomposes_into_vocabulary(v, known) for v in variants)
    if len(_PROPRIETARY_CACHE) < _PROPRIETARY_CACHE_MAX:
        _PROPRIETARY_CACHE[word] = result
    return result


def is_given_name(value: str) -> bool:
    """True when the FIRST token of `value` is a known given name.

    First token only, and never the surname: the list holds given names ONLY, because
    German surnames collide massively with ordinary vocabulary (Stark, Gering, Koch,
    Bauer, Berg, Winter, Fuchs, Wolf, Jung, Klein are all attested surnames AND
    ordinary words). A surname list used as POSITIVE evidence would corroborate the
    very false positives this exists to remove."""
    first = value.strip().split(maxsplit=1)
    if not first:
        return False
    # Trim an honorific so "Herr Klaus Mueller" tests "klaus", not "herr".
    stripped = _HONORIFIC_PREFIX.sub("", value.strip()).strip()
    token = (stripped.split(maxsplit=1) or first)[0]
    return token.strip(".,;:!?()[]").lower() in _given_names()


def _is_single_lowercase_word(value: str) -> bool:
    """True for a single all-lowercase token ('aber', 'abdeckung'). German
    capitalizes every noun, so a lowercase single word tagged as an entity is
    almost never a genuine name/org/place -- a near-zero-risk precision filter
    that's independent of any stopword list (catches ordinary nouns a
    stopword list wouldn't, e.g. 'abdeckung')."""
    v = value.strip()
    return bool(v) and " " not in v and v.islower()


def _is_noise_entity(entity_type: str, value: str, analyzer, lang: str) -> bool:
    """Combines the lowercase-word filter with spaCy's own stopword list
    (conjunctions/articles/prepositions -- catches a stopword even when
    capitalized at a sentence's start, e.g. 'Aber', which the lowercase
    check alone would miss). Only applied to _PRECISION_GATED_ENTITIES."""
    if entity_type not in _PRECISION_GATED_ENTITIES:
        return False
    if _is_single_lowercase_word(value):
        return True
    stripped = value.strip()
    return bool(stripped) and analyzer.nlp_engine.is_stopword(stripped, lang)


# Short ASCII all-caps run: an acronym / ticker (CAPEX, OPEX, RAG, DORA, DMS,
# GRC, PPT, CSV, LTV, FTE) -- jargon, not personal data. Bounded at 5 chars so
# a longer ALL-CAPS surname in an all-caps form ("SCHMIDT") is not caught; a
# real short all-caps name ("MAIER") is a rare, low-value-PII edge the
# cut-false-positives mandate accepts. ASCII-only so umlaut-bearing all-caps
# ("MÜLLER") is spared. Digits are handled separately (_is_digit_bearing_code).
_ACRONYM_RE = re.compile(r"^[A-Z]{2,5}$")


def _is_structural_nonname(entity_type: str, value: str) -> bool:
    """Filters shapes that structurally CANNOT be natural-language PII, applied
    to every free-text NER entity INCLUDING PERSON -- unlike the
    lowercase/stopword filter, which must spare a real lowercase surname, these
    shapes never occur in a genuine name/org/place. Measured on the reported
    document, these three shapes dominated the residual German-model noise:
    single letters / 2-char fragments ('S', 'ch', 'PL'), snake_case field or
    template identifiers ('Feld_Name', 'Persona_Liste', 'UI_Label'), and short
    ALL-CAPS acronyms ('CAPEX', 'OPEX', 'RAG', 'DMS')."""
    if entity_type not in _NER_ENTITIES:
        return False
    v = value.strip()
    if len(v) <= 2:
        return True  # single letters / 2-char fragments are never a real name span
    if "_" in v:
        return True  # snake_case identifier, not natural language
    return bool(_ACRONYM_RE.match(v))


# Reported false positive: "Abgelehnt" ("Rejected") tagged PERSON at spaCy's
# flat NER score. PERSON is deliberately excluded from _is_noise_entity's
# lowercase/stopword checks (a real lowercase surname must stay reachable),
# but a POS-based check is safe there too: it doesn't look at case at all, so
# it can't reject a lowercase surname, and a common-noun-shaped surname like
# "Bauer" (tagged NOUN, not PROPN -- and still correctly kept, since NOUN
# qualifies) survives it just as well as a proper name does. Measured
# identically for German and English (spaCy tags "Rejected"/"Genehmigt" as
# VERB, "Smith"/"Bauer" as NOUN), so this applies to any supported language.
_NAME_LIKE_POS = frozenset({"NOUN", "PROPN", "X"})  # X: unclassified/foreign tokens


def _is_pos_implausible(entity_type: str, start: int, end: int, nlp_artifacts) -> bool:
    """True when NONE of the tokens spanning [start:end) are noun-class --
    i.e. spaCy's own tagger disagrees with its NER component that this could
    be a name/org/place at all (a verb, determiner, conjunction, ...). This
    does NOT catch a determiner+noun phrase like "Alle Zielwerte" (it DOES
    contain a noun token) -- that is a different failure mode, handled by
    xlsx_handler's whole-cell-override name-shape gate, not here."""
    if entity_type not in _NER_ENTITIES or nlp_artifacts is None or nlp_artifacts.tokens is None:
        return False
    span = nlp_artifacts.tokens.char_span(start, end, alignment_mode="expand")
    if span is None or len(span) == 0:
        return False
    return not any(tok.pos_ in _NAME_LIKE_POS for tok in span)


# German nominalizer suffixes -- productive noun-forming endings (Effizienz,
# Nutzung, Derivatefreiheit, Reaktionszeiten). A word built with one of these is
# a common noun, essentially never a surname. Kept as a layered check (see
# _is_german_nominalization) so a rare -ung/-heit SURNAME is still protected.
_NOMINALIZER_SUFFIX = re.compile(
    r"(?:ung|ungen|heit|heiten|keit|keiten|schaft|schaften|tion|tionen|sion|"
    r"ität|enz|anz|ismus|ierung|ierungen|zeiten|barkeit)$",
    re.IGNORECASE,
)


def _is_german_nominalization(entity_type: str, value: str, start: int, end: int, nlp_artifacts) -> bool:
    """A German common noun formed with a productive nominalizer suffix
    ("Effizienz", "Derivatefreiheit", "Nutzung", "Reaktionszeiten") mis-tagged
    as an entity. Layered to keep the near-zero false-drop rate the tool needs:
    requires ALL of (1) a nominalizer suffix, (2) length >= 8 (spares short
    surnames like "Jung"/"Lang"), (3) spaCy POS NOUN with NO proper-noun token
    (spares a rarer -ung surname the tagger reads as PROPN in context). Ordinary
    surnames (Müller/Weber/Bauer/Metzler) have no such suffix and are untouched."""
    if entity_type not in _NER_ENTITIES:
        return False
    v = value.strip()
    if len(v) < 8 or not _NOMINALIZER_SUFFIX.search(v):
        return False
    if nlp_artifacts is None or nlp_artifacts.tokens is None:
        return False
    span = nlp_artifacts.tokens.char_span(start, end, alignment_mode="expand")
    if span is None or len(span) == 0:
        return False
    return (not any(t.pos_ == "PROPN" for t in span)) and any(t.pos_ == "NOUN" for t in span)


def _is_digit_bearing_code(entity_type: str, value: str) -> bool:
    """A genuine PERSON/LOCATION/ORGANIZATION/MISC name essentially never
    contains a digit -- a value that does ("BP-002", a project/ticket ID) is
    a structured code, not a name, regardless of NER's score. This is exactly
    the residual noise the POS check (above) cannot catch: spaCy tags
    "BP-002" as NOUN/PROPN, indistinguishable by part-of-speech alone from a
    real proper noun. Structured identifiers that ARE PII have their own
    dedicated pattern recognizers (IBAN, Kontonummer, ...); this only guards
    the free-text NER entity types.

    MULTI-LINE values are exempt. The premise above -- "this whole value is one
    name-shaped span, and names have no digits" -- only holds for a single-line
    span. A multi-line span is a BLOCK spaCy fused together, and the classic one
    is a German address block ("Hans Mueller\\nHauptstrasse 5"): rejecting it for
    the house number throws away the person's name sitting on the line above,
    which then never seeds propagation and leaks wherever it recurs bare. The
    digit-bearing lines of such a block are covered on their own by DE_ADDRESS
    and the other anchored recognizers."""
    if entity_type not in _NER_ENTITIES or "\n" in value:
        return False
    return any(ch.isdigit() for ch in value)


def _rejected_by_precision(
    entity_type, value, start, end, analyzer, lang, nlp_artifacts, *, source="", score=0.0, trust_override=1.0
) -> bool:
    """The single precision gate every candidate must clear -- applied
    IDENTICALLY to raw NER findings and to document-wide propagated matches, so
    propagation can no longer spread a value past the filters that would reject
    it on the direct path. Combines: lowercase/stopword noise, structural
    non-names (fragments, snake_case ids, acronyms), part-of-speech
    implausibility (verb/determiner tagged as an entity), and digit-bearing
    codes.

    Confidence override: a GLiNER hit scoring at/above `trust_override` bypasses
    the gate entirely. The gate exists to filter spaCy's flat-score NER noise
    (verbs/determiners/common German nouns mis-tagged as entities); a model that
    scored THIS span highly has already made that call, so re-filtering it by POS
    would re-drop exactly the German tool/project/org names GLiNER was added to
    catch (e.g. a project literally named "Derivatefreiheit"). Low-confidence
    GLiNER hits still run the full gate. Default trust_override=1.0 means callers
    that don't pass it keep byte-identical behaviour."""
    if source == GLINER_SOURCE and score >= trust_override:
        return False
    # A snake_case identifier is not a DATE. Every filter below is keyed on
    # _NER_ENTITIES, which deliberately excludes DATE_TIME -- so a spaCy DATE guess
    # had NO gate at all. Measured on a real workbook: the ENGLISH model (reached
    # via per-sheet language routing) tagged "MDX_PROXY_20", "MDX_LEAD_51" and
    # "PROJEKT_ID_37" as DATE_TIME at its flat 0.85, above the 0.5 threshold.
    #
    # Restricted to the shape and the sources where it is certain: no real date
    # contains an underscore, and the anchored DateRecognizer (not in
    # _GATED_NER_SOURCES) is untouched, so a genuine "24.03.2026" still lands.
    if entity_type == "DATE_TIME" and source in _GATED_NER_SOURCES and "_" in value:
        return True
    # An ANCHORED PATTERN hit is not a bare NER guess, even when its entity type
    # happens to be one spaCy also produces. Every filter below exists to clean up
    # spaCy's flat-score noise; a config pattern that demanded a literal
    # "Staatsangehörigkeit:" immediately before the value has corroborated it far
    # more strongly than a part-of-speech tag ever could.
    #
    # Without this the German Art. 9 racial/ethnic-origin recognizer -- which
    # deliberately emits the EXISTING NRP type, because NRP is already mapped to
    # the special-category class -- had every hit silently dropped: nationality
    # values are ADJECTIVES ("tuerkisch", "syrisch", "tunesisch") and no adjective
    # is noun-class, so _is_pos_implausible rejected all of them. The entity type
    # was only ever a proxy for "came from the NER model"; it stopped being one the
    # moment a pattern recognizer started emitting an NER type.
    if entity_type in _NER_ENTITIES and source not in _GATED_NER_SOURCES:
        return False
    return (
        _is_noise_entity(entity_type, value, analyzer, lang)
        or _is_structural_nonname(entity_type, value)
        or _is_pos_implausible(entity_type, start, end, nlp_artifacts)
        or _is_digit_bearing_code(entity_type, value)
        or _is_german_nominalization(entity_type, value, start, end, nlp_artifacts)
    )


_STRUCTURAL_MARKER = re.compile(r"(?m)^([-*+#]+)(?=\S)")

# Excel stores a character it cannot represent literally as an `_xHHHH_` escape,
# and openpyxl hands that escape through VERBATIM as the cell's text. The common
# one by far is `_x001E_` (U+001E RECORD SEPARATOR), which Excel writes between
# the parts of a multi-value cell -- so a cell holding only empty parts reads as
# `_x001E__x001E__x001E_`, and a real one as
# `Kein Beitrag_x001E_Reiner Backoffice-Prozess`.
#
# Left alone this breaks detection in BOTH directions, measured on the reported
# workbook: 7 entirely EMPTY cells were flagged DESCRIPTION at the auto-accept
# tier (the emptiness guard `^[\W\d_]*$` cannot match, since `x` and `E` are word
# characters), and any name fused to such an escape is rejected outright by
# _is_structural_nonname's `"_" in v` rule -- a silent false NEGATIVE.
#
# Safe to treat as never-content: a genuine literal `_x001E_` typed into a cell is
# itself escaped by Excel (as `_x005F_x001E_`), so an unescaped one in the value
# is always a real control character. Replaced by the SAME NUMBER of spaces, for
# the same reason the bullet markers are -- every later character keeps its index,
# so a finding's start/end stays a correct offset into the real, untouched text.
_XML_CHAR_ESCAPE = re.compile(r"_x[0-9A-Fa-f]{4}_")


def neutralize_structural_noise(text: str) -> str:
    """Same-length normalization used ONLY to decide what to feed the NLP
    pipeline -- never for the redacted OUTPUT, which always splices from the
    real, untouched text. Neutralizes a markdown-ish bullet ("-"/"*"/"+") or
    heading ("#") marker that sits at the start of a line with NO space before
    the next word (a bullet list rendered as plain text: "...Format.\\n-
    Erstellung..." with the space missing). spaCy's tokenizer otherwise fuses
    such a marker onto the following word, and NER then tags the fused token
    -- punctuation included -- as an entity. Each marker run is replaced by
    the SAME NUMBER of spaces, so every later character's index is identical
    to the original: a finding's start/end computed against this cleaned copy
    remains a correct offset into the real text (see detect_unit).

    This is deliberately narrow (line-start bullet/heading fusion only) --
    punctuation fused onto a word MID-sentence (e.g. a file-extension-style
    ".iboflow") is a different shape and is instead handled generically,
    post-detection, by _LEADING_NOISE trimming any free-text finding's span.

    Also neutralizes Excel's `_xHHHH_` character escapes -- see
    _XML_CHAR_ESCAPE for why those are never content and why same-length
    replacement is what keeps offsets (and therefore parity) intact."""
    cleaned = _STRUCTURAL_MARKER.sub(lambda m: " " * len(m.group(1)), text)
    return _XML_CHAR_ESCAPE.sub(lambda m: " " * len(m.group()), cleaned)


# --- OCR skeletons ------------------------------------------------------------
# Scanned correspondence, faxes and legacy exports do not contain clean text, and
# a name the OCR mangled is a name the tool redacts nowhere -- "Sehr geehrter Herr
# Mul1er," and "Müller" are different strings to every mechanism here.
#
# The skeleton folds only the substitutions OCR ACTUALLY MAKES, and it is used
# ONLY to match against names the document has already established. That
# restriction is what makes an aggressive fold safe: collapsing i/l/1 would be
# reckless as a detector (it merges unrelated words) and is harmless as a
# comparison against a known name, because the worst case is recognising a second
# spelling of somebody the tool is already redacting.
#
# Deliberately NOT folded: b/h. It would make "Bauer" and "Hauer" the same
# skeleton, and both are real German surnames -- the one collision in this table
# that could redact the wrong person's name.
_OCR_DIGRAPHS = (
    ("ii", "u"),    # the classic OCR umlaut: Miiller -> Müller
    ("rn", "m"),    # Kretschrnar -> Kretschmar
    ("vv", "w"),
    ("cl", "d"),
    ("ss", "s"),    # folds ß, which is expanded first
)
_OCR_CHARS = str.maketrans({
    "0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "6": "g", "7": "t", "8": "b", "9": "g",
    "|": "l", "!": "l", "i": "l",  # i/l/1/|/! are one class in most scans
    "ä": "a", "ö": "o", "ü": "u", "à": "a", "á": "a", "â": "a", "é": "e", "è": "e",
    "ç": "c", "ñ": "n", "ı": "i", "ş": "s", "ğ": "g", "å": "a", "ø": "o",
})


def ocr_skeleton(value: str) -> str:
    """A spelling-insensitive key for OCR-damaged text. Equal skeletons mean the
    two strings are plausibly the same word as read by a scanner."""
    s = value.lower().replace("ß", "ss")
    for a, b in (("ae", "a"), ("oe", "o"), ("ue", "u")):
        s = s.replace(a, b)
    for a, b in _OCR_DIGRAPHS:
        s = s.replace(a, b)
    s = s.translate(_OCR_CHARS)
    # Re-apply the digraph folds: a translation can CREATE one ("Mul1er" -> "muller").
    for a, b in _OCR_DIGRAPHS:
        s = s.replace(a, b)
    return "".join(ch for ch in s if ch.isalnum())


# Below this length a skeleton match is not evidence -- "Berg"/"Berq"/"8erg" fold
# together, but so do too many unrelated short tokens for the match to mean
# anything on its own.
_OCR_MIN_LEN = 5


_OCR_TOKEN = re.compile(r"[^\W_]{%d,}" % _OCR_MIN_LEN)
# Alphabetic runs only: a proprietary-name candidate must be a WORD, so anything
# carrying a digit or an underscore is an identifier and handled elsewhere.
_OOV_TOKEN = re.compile(r"[^\W\d_]{%d,}" % _PROPRIETARY_MIN_LEN)


def _vocab_checker(analyzer, language: str):
    """A `known(lowercase_word) -> bool` closure over the model's vocabulary, or
    None when no vocabulary is reachable (in which case the proprietary-name
    candidate pass simply does not run, rather than flagging every token)."""
    try:
        vocab = analyzer.nlp_engine.nlp[language].vocab
    except Exception:  # noqa: BLE001 -- a missing vocab disables the pass, never fails a scan
        return None

    def known(word: str) -> bool:
        try:
            return vocab[word].has_vector
        except Exception:  # noqa: BLE001
            return False

    return known


@functools.lru_cache(maxsize=8)
def _ocr_skeleton_index(propagate: tuple[tuple[str, str], ...]) -> dict[str, tuple[str, str]]:
    """{skeleton: (entity_type, canonical value)} for every propagated value long
    enough for a skeleton match to mean something.

    A skeleton shared by two DIFFERENT propagated values is dropped outright: if
    the document itself contains two names that fold together, the fold cannot
    tell which one a damaged token was, and guessing would redact somebody under
    the wrong pseudonym."""
    index: dict[str, tuple[str, str]] = {}
    ambiguous: set[str] = set()
    for entity_type, value in propagate:
        token = value.strip()
        if len(token) < _OCR_MIN_LEN or " " in token:
            continue
        key = ocr_skeleton(token)
        if len(key) < _OCR_MIN_LEN:
            continue
        if key in index and index[key][1].lower() != token.lower():
            ambiguous.add(key)
        index.setdefault(key, (entity_type, token))
    for key in ambiguous:
        index.pop(key, None)
    return index


def _ocr_variant_matches(text: str, propagate: tuple[tuple[str, str], ...]):
    """(start, end, entity_type, matched_text) for tokens that are an OCR-damaged
    spelling of a propagated value but are NOT the value itself."""
    index = _ocr_skeleton_index(propagate)
    if not index:
        return
    for m in _OCR_TOKEN.finditer(text):
        token = m.group()
        hit = index.get(ocr_skeleton(token))
        if hit and token.lower() != hit[1].lower():
            yield m.start(), m.end(), hit[0], token


@functools.lru_cache(maxsize=8)
def _compiled_propagate_patterns(propagate: tuple[tuple[str, str], ...]):
    """Compiles every propagated value's match pattern ONCE per distinct
    propagate list, instead of re-building (and re-compiling) it from an f-string
    on every call. A "database" spreadsheet can propagate hundreds of confirmed
    names, and detect_unit runs once per distinct cell -- calling
    re.finditer(f-string, ...) that many times blows past Python's regex-compile
    cache (512 entries) and forces near-constant recompilation from scratch:
    measured as >90% of total scan time on a document with ~800 propagated
    names. lru_cache keeps this to one compile pass per scan (the propagate list
    is fixed for its duration), matching the pattern already used for
    xlsx_handler's _name_header_re.

    The boundary is `[^\\W_]` (alphanumerics only), NOT `\\w`: an underscore is a
    word character, so a `\\w` boundary could never see a name inside the
    identifiers a bank's systems generate -- "AKTE_Winkler_2024",
    "Vertrag_Winkler_final_v2.pdf". Measured: the hyphen and backslash forms
    ("K-Winkler-2024", a UNC path) already propagated because those separators are
    non-word, while the underscore forms silently did not. Treating `_` as a
    boundary makes the behaviour consistent across separators; the alphanumeric
    part of the guard is unchanged, so "Berg" still refuses to match inside
    "Bergstraße"."""
    return [
        (entity_type, value, re.compile(rf"(?<![^\W_]){re.escape(value)}(?![^\W_])"))
        for entity_type, value in propagate
    ]


def _snippet(text: str, start: int, end: int) -> str:
    lo = max(0, start - CONTEXT_SNIPPET_RADIUS)
    hi = min(len(text), end + CONTEXT_SNIPPET_RADIUS)
    prefix = "..." if lo > 0 else ""
    suffix = "..." if hi < len(text) else ""
    return f"{prefix}{text[lo:start]}[{text[start:end]}]{text[end:hi]}{suffix}"


def _deny_list_findings(text: str, deny_list: list[str]) -> list[tuple[int, int, str]]:
    hits = []
    for term in deny_list:
        if not term:
            continue
        for m in re.finditer(re.escape(term), text, flags=re.IGNORECASE):
            hits.append((m.start(), m.end(), "DENY_LIST"))
    return hits


def _refine(finding: Finding) -> Finding:
    """Applies checksum validation. A validated structured ID is promoted to the
    auto-accept tier; a checksum-FAILING one is DEMOTED to a review-tier score --
    never dropped. A typo'd or OCR'd IBAN, card or tax ID is still an identifying
    string, so it stays in the actionable set with validated=False, which the
    review UI shows as an "unverified" chip and which makes detect_unit skip the
    score-threshold gate for it."""
    verdict = validators.validate(finding.entity_type, finding.value)
    finding.validated = verdict
    if verdict is True:
        finding.score = max(finding.score, _VALIDATED_SCORE)
    elif verdict is False:
        # Demote, don't drop -- a checksum-failing IBAN/card is still identifying.
        finding.score = min(finding.score, _INVALID_SCORE)
    return finding


def _survives_special_category(f: Finding) -> bool:
    """True for a finding strong enough to survive INSIDE a one-way Art. 9 span
    and cut it (see _split_special_category_spans).

    Deliberately narrow. A PERSON, because a customer name is the value whose
    reversibility is worth most operationally; and any id whose CHECKSUM PASSED,
    because that is the only claim here backed by arithmetic rather than by a
    model. A checksum-FAILED id is excluded on purpose -- it may not be an id at
    all, and every wrong survivor cuts a hole in health data."""
    return f.entity_type == "PERSON" or f.validated is True


def _lettered_gaps(parent: Finding, chosen: list[Finding], text: str) -> list[Finding]:
    """The parts of `parent` that `chosen` does not cover, as Art. 9 findings.

    Only gaps that still contain a LETTER survive: health text on either side of
    a name is still health text, but a gap of pure punctuation (", ", " - ") has
    nothing to redact, and emitting a `[DE_HEALTH_DATA]` token where only a comma
    stood would corrupt the line for no privacy gain. Surrounding whitespace is
    trimmed off each gap for the same reason."""
    gaps: list[Finding] = []
    cursor = parent.start
    for boundary in [*(s.start for s in chosen), parent.end]:
        if boundary > cursor:
            raw = text[cursor:boundary]
            lead = len(raw) - len(raw.lstrip())
            start, end = cursor + lead, cursor + len(raw.rstrip())
            value = text[start:end]
            if end > start and any(ch.isalpha() for ch in value):
                gaps.append(
                    replace(parent, start=start, end=end, value=value, context=_snippet(text, start, end))
                )
        nxt = next((s for s in chosen if s.start == boundary), None)
        if nxt is not None:
            cursor = max(cursor, nxt.end)
    return gaps


def _split_special_category_spans(
    kept: list[Finding], carved: dict[int, list[Finding]], text: str
) -> list[Finding]:
    """Cut a one-way Art. 9 span around the high-specificity findings it swallowed.

    An Art. 9 recognizer anchors on a LABEL and claims the rest of the line
    ("Diagnose: ..."), which is correct and must stay that way: a German diagnosis
    legitimately contains commas ("Diabetes mellitus Typ 2, insulinpflichtig"), so
    terminating the value at the first one would leave health data in a file the
    tool calls verified. But that same line also carries the customer's name and
    IBAN -- and the Art. 9 action is ONE-WAY, so those were destroyed outright
    instead of pseudonymized, with no way to ever restore them.

    So the survivors are re-emitted as their own findings and the Art. 9 span is
    split around them, with every lettered fragment keeping the identical one-way
    action. A survivor covering the parent ENTIRELY does not split it: no fragment
    would remain, the Art. 9 finding would vanish, and the value would silently
    become reversible -- trading the special-category protection for the very
    reversibility this is meant to preserve *inside* it. That case keeps the old
    behaviour and the Art. 9 span wins whole.

    The non-overlap invariant apply relies on holds by construction: fragments are
    the gaps BETWEEN survivors, and the survivors are de-overlapped against each
    other first -- they were each compared only against the parent on the way in,
    never against one another, so two of them genuinely can overlap."""
    if not carved:
        return kept
    out: list[Finding] = []
    for parent in kept:
        survivors = carved.get(id(parent))
        if not survivors:
            out.append(parent)
            continue
        chosen: list[Finding] = []
        for s in survivors:  # already in _resolve_overlaps' priority order
            if s.start < parent.start or s.end > parent.end:
                continue  # a crossing merge moved the parent; s is no longer inside
            if any(s.start < c.end and c.start < s.end for c in chosen):
                continue
            chosen.append(s)
        chosen.sort(key=lambda f: f.start)
        fragments = _lettered_gaps(parent, chosen, text) if chosen else []
        if not fragments:
            out.append(parent)
            continue
        out.extend(fragments)
        out.extend(chosen)
    return out


def _resolve_overlaps(findings: list[Finding], text: str) -> list[Finding]:
    """Keeps a non-overlapping set. Apply replaces spans by splicing text and
    ASSUMES they never overlap (see the format handlers' run/cell replacement);
    two recognizers claiming overlapping-but-not-identical spans for the same
    text (e.g. the built-in PHONE_NUMBER and the custom DE_PHONE on one number,
    or spaCy's city-only LOCATION inside a full DE_ADDRESS) would otherwise
    corrupt the output or silently drop a redaction.

    Priority order (highest first):
      1. GDPR Art. 9 SPECIAL-CATEGORY types, ahead of span length. Everything else
         being equal, covering MORE text is safer -- but not when it costs the
         value its data CLASS: spaCy claims "Krankenkasse Barmer" / "Gewerkschaft
         ver.di" as one long ORGANIZATION containing the Art. 9 hit, so
         length-first dropped the Art. 9 finding as "fully contained" and filed a
         health insurer / union membership under Organizations & places, whose
         action is PSEUDONYMIZE -- a reversible, mapping-backed [ORG_n]. Winning
         costs no coverage: a crossing/containing generic span is still MERGED
         into the winner below, so the union is redacted either way, one-way.
      2. The LONGER span: the full address wins over the bare city, the complete
         phone over a fragment.
      3. A CHECKSUM-TESTED id (validated is not None) over an untested one. This
         only ever decides an equal-length contest, where there is no coverage
         argument either way: a checksum-FAILED IBAN demoted to 0.4 used to lose
         its IDENTICAL span to spaCy's NER_MISC at its flat 0.85, and a typo'd or
         OCR'd IBAN lost both its Financial-IDs class and its "unverified" chip.
      4. The higher score, then the specific pattern recognizer over the generic
         NER label (e.g. DE_ADDRESS vs spaCy LOCATION on one PLZ+city), then the
         entity type, so the result is deterministic.
    Touching spans (end == next start) do not overlap.

    Overlap handling is UNION-MERGE, not drop-the-loser: a finding fully CONTAINED
    by a kept span adds nothing and is dropped, but a CROSSING (partial) overlap is
    merged -- the kept span is extended to cover the union of every span it crosses,
    and its value re-sliced from `text`. Dropping the loser outright (the old
    behaviour) leaked any character range covered ONLY by the loser: e.g. an
    over-reaching PERSON anchor "Klaus Mueller Hauptstr" crossing a longer
    DE_ADDRESS "Hauptstr 12, Musterstadt" dropped the PERSON entirely, leaving the
    customer name "Klaus Mueller" redacted by nothing. Merging over-redacts the
    crossing region (safe) instead of leaking it; the merged span keeps the
    highest-priority overlapper's entity type.
    """
    ordered = sorted(
        findings,
        key=lambda f: (
            not taxonomy.is_special_category(f.entity_type),  # Art. 9 first
            -(f.end - f.start),
            f.validated is None,  # a checksum-tested id wins an equal-length tie
            -f.score,
            f.entity_type in _NER_ENTITIES,  # specific pattern recognizers win ties
            f.entity_type,
            f.start,
        ),
    )
    kept: list[Finding] = []
    # Contained findings strong enough to CUT the one-way Art. 9 span holding them.
    # Resolved after the loop, keyed on the parent's identity because a crossing
    # merge below can still move that parent's bounds after we record it.
    carved: dict[int, list[Finding]] = {}
    for f in ordered:
        overlappers = [k for k in kept if f.start < k.end and k.start < f.end]
        if not overlappers:
            kept.append(f)
            continue
        contained_in = next((k for k in overlappers if k.start <= f.start and f.end <= k.end), None)
        if contained_in is not None:
            _absorb_corroborating_source(contained_in, f)
            if taxonomy.is_special_category(contained_in.entity_type) and _survives_special_category(f):
                carved.setdefault(id(contained_in), []).append(f)
            continue  # fully contained by a kept span -> its PII is already covered
        # Crossing overlap: extend the highest-priority overlapper to the union of
        # f and EVERY span it crosses (f may bridge two adjacent kept spans), so no
        # detected PII char is left uncovered and the kept set stays non-overlapping.
        new_start = min(f.start, *(k.start for k in overlappers))
        new_end = max(f.end, *(k.end for k in overlappers))
        winner = overlappers[0]  # earliest-inserted == highest priority in sort order
        for loser in overlappers[1:]:
            kept.remove(loser)
        winner.start, winner.end = new_start, new_end
        winner.value = text[new_start:new_end]
        winner.context = _snippet(text, new_start, new_end)
        # The merged span is a NEW string that was never itself checksum-tested, so
        # the old validated verdict no longer applies -- clear it (re-tier on score)
        # rather than show a stale "verified" chip for a value never validated.
        winner.validated = None
        _absorb_corroborating_source(winner, f)
    return sorted(_split_special_category_spans(kept, carved, text), key=lambda f: f.start)


def _absorb_corroborating_source(kept: Finding, dropped: Finding) -> None:
    """A raw spaCy NER candidate often wins the span/score tie-break over a
    same-span pattern/checksum/whole-cell-override candidate on the SAME
    document location (e.g. spaCy independently tags a name a header-matched
    whole-cell override ALSO claimed) -- without this, that corroboration is
    silently lost, and the surviving finding reads as "just a guess"
    (is_ner_guess) even though something else independently confirmed it."""
    # "propagation" is NOT independent corroboration -- propagated matches are
    # DERIVED from NER guesses (a common word like "Sparen" that NER tags PERSON
    # in one cell seeds propagation, which must not then vouch for an ORG hit on
    # the same word elsewhere and defeat corroboration-only). Only a genuinely
    # authoritative source (a pattern/checksum recognizer, a whole-cell or
    # topical-header override, the deny-list) counts as corroboration.
    # "propagation" is included on the KEPT side, not just spaCy. Propagation scores
    # _PROPAGATED_SCORE (0.85) while the anchored name patterns score 0.70-0.75, so a
    # propagated occurrence WINS the overlap on the very span an anchor corroborated --
    # and with only "SpacyRecognizer" here, that anchor was silently destroyed.
    #
    # Measured: "Sehr geehrter Herr Müller," seeds propagation, propagation then wins
    # back the same span, every occurrence ends up source="propagation", the group reads
    # as a bare guess, and under corroboration-only the name is DEMOTED AND LEAKS.
    # Propagation must not CREATE corroboration (see below) but it must not erase it.
    if kept.source in ("SpacyRecognizer", "propagation") and dropped.source not in (
        "SpacyRecognizer",
        "propagation",
        "",
    ):
        kept.source = dropped.source


def precompute_nlp_artifacts(analyzer, texts, language: str, batch_size: int = 128):
    """Batch-runs the shared spaCy pipeline (via `nlp.pipe`) over every DISTINCT
    string in `texts` ONCE, instead of the one-`analyze()`-call-per-text pattern
    `detect_unit` uses on its own. Measured ~5x faster than sequential calls for
    the many short, highly repetitive values a spreadsheet scan produces (a
    single Python-level `nlp(text)` call pays fixed per-call overhead that
    `nlp.pipe()` amortizes across the batch).

    Returns {text: NlpArtifacts}. An artifact is valid for a given (text,
    language) regardless of `entities`/`allow_list`/`config` -- Presidio's
    AnalyzerEngine.analyze() only uses a passed-in `nlp_artifacts` to skip its
    own internal NLP call; every other analyze() parameter is applied
    downstream of it. So callers may safely reuse these across differing
    per-call `entities`/`allow_list` values, as long as the language matches.
    """
    unique_texts = list(dict.fromkeys(texts))
    if not unique_texts:
        return {}
    pairs = analyzer.nlp_engine.process_batch(texts=unique_texts, language=language, batch_size=batch_size)
    return dict(pairs)


def detect_unit(analyzer, unit: TextUnit, config: dict, nlp_artifacts=None) -> list[Finding]:
    """THE detection primitive -- one overlap-resolved list of findings for a
    unit. Used identically by scan and apply.

    `nlp_artifacts`, when given, is a precomputed artifact for `unit.text`
    (see `precompute_nlp_artifacts`) that lets `analyzer.analyze()` skip its own
    NLP call. Only applied when the config is single-language: an artifact is
    tied to one language, so passing it through under a multi-language config
    would silently reuse the wrong language's tokenization for the others."""
    # A narrowed config always pins exactly one language; the fallback stays
    # SINGLE-language on purpose (running every model over one document is the
    # cross-language noise this design exists to prevent).
    languages = config.get("languages") or [DEFAULT_LANGUAGES[0]]
    allow_list = config.get("allow_list", [])
    deny_list = config.get("deny_list", [])
    entities_cfg = config.get("entities", {})
    # Global recall/precision offset (sensitivity slider). Positive lowers every
    # threshold (more recall); default 0 keeps shipped behaviour.
    sensitivity = float(config.get("sensitivity", 0.0))
    # Topical categories are detected structurally (header->category / gazetteer),
    # NOT by any Presidio recognizer -- so they carry a default_action in
    # entities_cfg (for grouping/apply) but must NOT be requested from
    # analyzer.analyze (Presidio warns per call for an entity with no recognizer).
    _structural = set(taxonomy.TOPICAL_ENTITY_TYPES) | {taxonomy.POSSIBLE_TOPICAL}
    # When GLiNER is enabled it BECOMES the recognizer for the propagating topical
    # types, so they may now be requested from analyze(). DESCRIPTION stays
    # structural (handled whole-cell at the format layer, not as a prose span) and
    # POSSIBLE_TOPICAL is never model-emitted.
    gliner_cfg = config.get("gliner") or {}
    gliner_on = bool(gliner_cfg.get("enabled"))
    gliner_override = float(gliner_cfg.get("confidence_override", 0.85))
    if gliner_on:
        _structural -= set(taxonomy.PROPAGATING_TOPICAL_TYPES)
    wanted_entities = [e for e in entities_cfg if e not in _structural]

    candidates: list[Finding] = []
    # Same-length cleanup (see neutralize_structural_noise) -- feeds the NLP
    # pipeline a version with line-start bullet/heading fusion neutralized,
    # while every Finding.value/context below still slices from unit.text
    # (the real original), since positions match exactly either way. A caller
    # passing precomputed nlp_artifacts (xlsx's batched path) MUST have built
    # them from this same cleaned text -- see xlsx_handler._precompute_cell_artifacts.
    scan_text = neutralize_structural_noise(unit.text)
    # Ensure an nlp_artifacts is always available for the POS-plausibility
    # check below, even for callers (docx/pptx/pdf, or any xlsx cell that
    # skipped batching) that never precomputed one -- computed once here and
    # reused for analyzer.analyze() too, so this is not a redundant NLP call.
    if nlp_artifacts is None and len(languages) == 1:
        nlp_artifacts = analyzer.nlp_engine.process_text(scan_text, languages[0])

    for lang in languages:
        results = analyzer.analyze(
            text=scan_text,
            language=lang,
            entities=wanted_entities,
            allow_list=allow_list,
            nlp_artifacts=nlp_artifacts if len(languages) == 1 else None,
        )
        for r in results:
            start, end = r.start, r.end
            value = unit.text[start:end]
            if r.entity_type in _NER_ENTITIES:
                noise = _LEADING_NOISE.match(value)
                if noise:
                    start += noise.end()
                    value = value[noise.end() :]
                if not value:
                    continue  # the whole span was punctuation -- nothing left to flag
            # spaCy's German model routes many real names into MISC, not PERSON, so
            # trim the honorific there too -- otherwise "Frau Bauer" (MISC) keys as a
            # different entity than a bare "Bauer" elsewhere.
            if r.entity_type in ("PERSON", "NER_MISC"):
                trimmed = _HONORIFIC_PREFIX.match(value)
                if trimmed:
                    start += trimmed.end()
                    value = value[trimmed.end() :]
            r_source = (r.recognition_metadata or {}).get("recognizer_name", "")
            if _rejected_by_precision(
                r.entity_type, value, start, end, analyzer, lang, nlp_artifacts,
                source=r_source, score=r.score, trust_override=gliner_override,
            ):
                continue
            # CORROBORATION, not detection: this never creates a finding, it only
            # records that an existing PERSON candidate has positive evidence behind
            # it (its first token is a known given name). Re-sourcing is how the rest
            # of this codebase expresses corroboration -- see
            # _absorb_corroborating_source -- and it is what will let PERSON become
            # corroboration-only without discarding real names.
            #
            # PERSON only. A given name is evidence about a PERSON; it says nothing
            # about whether an ORGANIZATION or LOCATION guess is real.
            if r.entity_type == "PERSON" and r_source in _GATED_NER_SOURCES and is_given_name(value):
                r_source = GIVEN_NAME_SOURCE
            # Same mechanism for PRODUCT and PROJECT names. Unlike a given name this
            # applies across the NER types, because the model has no idea what kind
            # of thing a proprietary name is and guesses inconsistently: measured on
            # the audit fixture, "Alteryx" and "OpenClaw" were both typed LOCATION,
            # while other tools in the same file came back ORGANIZATION or MISC.
            # Whatever it guessed, a listed product name is a confirmed entity.
            elif r.entity_type in _NER_ENTITIES and r_source in _GATED_NER_SOURCES and is_known_product(value):
                r_source = PRODUCT_NAME_SOURCE
            finding = Finding(
                entity_type=r.entity_type,
                value=value,
                score=r.score,
                context=_snippet(unit.text, start, end),
                unit_id=unit.id,
                start=start,
                end=end,
                source=r_source,
            )
            _refine(finding)
            threshold = entities_cfg.get(r.entity_type, {}).get("confidence_threshold", 0.5)
            # A checksum-FAILED ID (validated is False) is demoted to _INVALID_SCORE
            # but MUST still be surfaced for review -- a typo'd/OCR'd Steuer-ID is
            # identifying, and its 0.6 threshold would otherwise silently drop the
            # 0.4-demoted finding. Only findings that did NOT fail a checksum obey the
            # score gate.
            if finding.validated is not False and finding.score < max(0.0, threshold - sensitivity):
                continue
            candidates.append(finding)

    # Document-wide propagation. A value confirmed as an entity ANYWHERE in this
    # document is very likely the same entity here too -- even in the units where
    # NER missed it, which is the measured failure: de_core_news_lg finds
    # "Müller" in "Herr Müller hat das Konto eröffnet." but not in a bare table
    # cell, a labelled field, or an oblique clause. The caller derives this list
    # from the same units in BOTH scan and apply, so it stays deterministic and
    # in parity. (Published technique: Dehghan et al., i2b2 2014 -- +9.2% recall
    # AND +5.1% precision, precision rising because only filtered values spread.)
    for entity_type, value, pattern in _compiled_propagate_patterns(tuple(config.get("propagate", ()))):
        for m in pattern.finditer(unit.text):
            # Propagation used to append UNFILTERED -- so a value seeded once
            # (a snake_case field id "Aktueller_Status", an acronym, or a
            # common-word-that's-also-a-surname used as an ordinary word here)
            # re-appeared across the whole document as PII, bypassing every
            # precision gate and swamping the review. Re-validate each
            # occurrence IN ITS LOCAL CONTEXT with the same filters the direct
            # NER path uses: a propagated "Gering" lands only where it is
            # actually name-shaped, not where it means "low".
            if _rejected_by_precision(entity_type, m.group(), m.start(), m.end(), analyzer, languages[0], nlp_artifacts):
                continue
            candidates.append(
                Finding(
                    entity_type=entity_type,
                    value=m.group(),
                    score=_PROPAGATED_SCORE,
                    context=_snippet(unit.text, m.start(), m.end()),
                    unit_id=unit.id,
                    start=m.start(),
                    end=m.end(),
                    source="propagation",
                )
            )

    # OCR-DAMAGED occurrences of a name the document already established. Scanned
    # correspondence corrupts SOME occurrences, not all -- the salutation reads
    # cleanly and the body says "Mul1er" -- and to every mechanism above those are
    # unrelated strings, so the corrupted ones are redacted nowhere.
    #
    # Matched on the OCR skeleton, and ONLY against values already propagating.
    # That is the whole safety argument: this can never invent an entity, it can
    # only recognise a second spelling of somebody the tool is already redacting.
    # Findings are tagged "propagation" like their clean twins, so they inherit the
    # same corroboration treatment rather than smuggling in a new trusted source.
    for start, end, entity_type, matched in _ocr_variant_matches(
        unit.text, tuple(config.get("propagate", ()))
    ):
        # _rejected_by_precision is deliberately NOT applied here. It is a shape
        # filter written for clean text, and running it on deliberately damaged
        # text rejects exactly the evidence this path exists to use: measured, the
        # two damaged spellings that contain DIGITS ("Mul1er", "0sterkamp") were
        # thrown out as number-like and scored 0/3, while every digit-free
        # corruption passed. The identity question the filter would answer has
        # already been answered more strongly -- an exact skeleton match against a
        # name this document established, with ambiguous skeletons dropped.
        if any(f.start < end and f.end > start for f in candidates):
            continue  # already claimed by a literal match or by NER
        candidates.append(
            Finding(
                entity_type=entity_type,
                value=matched,
                score=_PROPAGATED_SCORE,
                context=_snippet(unit.text, start, end),
                unit_id=unit.id,
                start=start,
                end=end,
                source="propagation",
            )
        )

    # PROPRIETARY-NAME CANDIDATES. The last resort for a codename that is in no
    # list and has no declaring column: a capitalised token that is neither German
    # vocabulary nor a German compound.
    #
    # Emitted as an UNCORROBORATED NER_MISC on purpose -- an empty source is in
    # _GATED_NER_SOURCES, so corroboration-only routes these straight into the
    # DEMOTED band. They appear in the separate section the reviewer scans, never
    # in the actionable list, and are never applied. That is the correct weight for
    # the weakest signal in the tool: it cannot silently redact a word, and it
    # cannot inflate the decoy false-positive count, but it also cannot stay
    # silent about an unrecognised name. If something else independently confirms
    # the same value, the normal corroboration path promotes it.
    if config.get("proprietary_name_candidates", True):
        vocab = _vocab_checker(analyzer, languages[0])
        if vocab is not None:
            for m in _OOV_TOKEN.finditer(unit.text):
                token = m.group()
                if any(f.start < m.end() and f.end > m.start() for f in candidates):
                    continue
                if is_known_product(token) or is_given_name(token):
                    continue  # already covered by a real corroboration source
                if not looks_like_proprietary_name(token, vocab):
                    continue
                candidates.append(
                    Finding(
                        entity_type="NER_MISC",
                        value=token,
                        score=_PROPAGATED_SCORE,
                        context=_snippet(unit.text, m.start(), m.end()),
                        unit_id=unit.id,
                        start=m.start(),
                        end=m.end(),
                        source=PROPRIETARY_CANDIDATE_SOURCE,
                    )
                )

    # Deny-list terms are explicit user intent -> score 1.0 so they win any span
    # contest during overlap resolution.
    for start, end, entity_type in _deny_list_findings(unit.text, deny_list):
        candidates.append(
            Finding(
                entity_type=entity_type,
                value=unit.text[start:end],
                score=1.0,
                context=_snippet(unit.text, start, end),
                unit_id=unit.id,
                start=start,
                end=end,
                source="deny_list",
            )
        )

    return _resolve_overlaps(candidates, unit.text)


# --- completeness / unmatched-risk scan -------------------------------------

_MISS_PATTERNS = [
    re.compile(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}"),  # IBAN-shaped
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),  # email-shaped
    # 5+ char digit-ish runs (phones, ids, ...). The optional LETTERS- prefix makes
    # the reported value the whole structured code: without it a "BP-26-001" project
    # id was reported as the bare tail "26-001", which reads as meaningless noise
    # and gives the reviewer nothing to act on.
    re.compile(r"(?:\b[A-Za-z]{2,12}-)?\d[\d ./-]{3,}\d"),
    re.compile(r"\b[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b"),  # BIC/SWIFT-shaped
    # Links, as a backstop only: URL is a configured entity with a real recognizer,
    # so a matched link is a normal finding and the overlap check below suppresses
    # it here. This fires when the URL entity is switched off or its data class is
    # skipped -- i.e. exactly when a leak would otherwise be invisible.
    re.compile(r"(?:https?://|www\.)\S{4,}"),
]

# Shapes that are NUMBERS, not identifiers. The digit-run pattern above is
# deliberately broad, and on a financial workbook that made this bucket almost
# pure noise: measured, 100 of 100 rows were money amounts ("10000", "750000",
# "36.000", "178.4") and it surfaced NONE of the values that actually leaked. A
# bucket that is entirely noise trains the reviewer to ignore it, which is worse
# than not having one -- so these are subtracted by SHAPE, and only shapes that
# have been characterised. Nothing that could be an identifier is removed.
_GROUPED_NUMBER = re.compile(r"^\d{1,3}(?:[.\s]\d{3})+(?:,\d+)?$")  # 36.000 / 1 250,50
_DECIMAL_NUMBER = re.compile(r"^\d+[.,]\d+$")  # 178.4
_PLAIN_INTEGER = re.compile(r"^\d+$")
# A bare integer is identifier-SHAPED once it reaches the length of the real ID
# classes (Kontonummer 8-10, Steuer-ID 11, SV-Nummer 12, card 16, IBAN 22).
_MIN_BARE_DIGITS = 8


def _is_just_a_number(value: str, whole_unit: bool) -> bool:
    """True for a value whose shape is a quantity rather than an identifier.

    `whole_unit` -- the value IS the entire text unit, i.e. a lone number in its
    own spreadsheet cell. That is what separates the two cases a length threshold
    alone cannot: a 6-digit number sitting by itself in a `..._Kosten_EUR` column
    is an amount, while the same 6 digits inside "Vertrag 998877 fuer ..." is a
    reference number worth surfacing. An earlier attempt used a flat 8-digit floor
    and silently broke the second case -- see test_core.py's completeness test.

    The grouped/decimal shapes are quantities in ANY position, so they are
    excluded regardless: German thousands grouping ("36.000", "1.100") and
    decimals ("178.4") are never identifiers."""
    if _GROUPED_NUMBER.match(value) or _DECIMAL_NUMBER.match(value):
        return True
    if not _PLAIN_INTEGER.match(value):
        return False
    return whole_unit and len(value) < _MIN_BARE_DIGITS


def _covered_ranges(findings: list[Finding]) -> dict[str, list[tuple[int, int]]]:
    ranges: dict[str, list[tuple[int, int]]] = {}
    for f in findings:
        ranges.setdefault(f.unit_id, []).append((f.start, f.end))
    return ranges


def completeness_scan(units: list[TextUnit], kept: list[Finding]) -> list[GroupedFinding]:
    """Flags sensitive-looking strings that no recognizer matched (or that were
    dropped as checksum-invalid), so the reviewer can catch false negatives.
    Informational only -- these are never auto-applied."""
    covered = _covered_ranges(kept)
    groups: dict[str, GroupedFinding] = {}
    for unit in units:
        unit_covered = covered.get(unit.id, [])
        for pattern in _MISS_PATTERNS:
            for m in pattern.finditer(unit.text):
                start, end = m.start(), m.end()
                value = m.group().strip()
                if (
                    sum(c.isdigit() for c in value) < 4
                    and "@" not in value
                    and "//" not in value
                    and not value.lower().startswith("www.")
                    and not validators.bic_valid(value)
                ):
                    continue  # too few digits, not an email/link, not a BIC -> not risky
                if _is_just_a_number(value, whole_unit=value == unit.text.strip()):
                    continue  # an amount or a count, not an identifier
                if any(cs < end and ce > start for cs, ce in unit_covered):
                    continue  # overlaps a real finding -> already handled
                key = value.lower()
                if key in groups:
                    groups[key].count += 1
                else:
                    groups[key] = GroupedFinding(
                        entity_type=taxonomy.POSSIBLE_MISS,
                        value=value,
                        count=1,
                        max_score=0.0,
                        context=_snippet(unit.text, start, end),
                        action="skip",
                        tier=taxonomy.TIER_LOW,
                    )
    ordered = sorted(groups.values(), key=lambda g: -g.count)
    return ordered[:MAX_POSSIBLE_MISSES]


# --- grouping / review model -------------------------------------------------


def build_scan_result(findings: list[Finding], units: list[TextUnit], config: dict) -> ScanResult:
    """Groups raw findings into per-data-class review groups with trust tiers,
    plus the informational possible-miss bucket and coverage stats."""
    entities_cfg = config.get("entities", {})
    tiers_cfg = config.get("tiers", {})
    high = float(tiers_cfg.get("high", 0.9))
    medium = float(tiers_cfg.get("medium", 0.5))

    grouped: dict[tuple[str, str], GroupedFinding] = {}
    # True only while EVERY occurrence seen so far for this key was a raw
    # spaCy NER hit with no pattern/checksum corroboration anywhere -- one
    # corroborating occurrence (a pattern/anchor match, a whole-cell/topical
    # override, a checksum, ...) is enough to call the whole group "not just a
    # guess." A "propagation"-sourced occurrence does NOT count as corroboration
    # (it is DERIVED from an NER guess), so a bare NER value that merely
    # propagated stays a guess.
    all_ner_guess: dict[tuple[str, str], bool] = {}
    # ANY occurrence from the ML second pass marks the group AI-detected (see
    # GroupedFinding.is_ai_detected for why "any" rather than "every").
    any_ai: dict[tuple[str, str], bool] = {}
    # EVERY occurrence from the proprietary-name candidate pass -- see
    # GroupedFinding.is_oov_candidate.
    all_oov: dict[tuple[str, str], bool] = {}
    for f in findings:
        key = (f.entity_type, f.value.strip().lower())
        default_action = entities_cfg.get(f.entity_type, {}).get("default_action", "anonymize")
        g = grouped.get(key)
        if g is None:
            grouped[key] = g = GroupedFinding(
                entity_type=f.entity_type,
                value=f.value,
                count=0,
                max_score=f.score,
                context=f.context,
                action=default_action,
                validated=f.validated,
            )
        g.count += 1
        g.max_score = max(g.max_score, f.score)
        if f.validated is not None:
            g.validated = f.validated
        is_guess = f.entity_type in _NER_ENTITIES and f.source in (
            "SpacyRecognizer", "propagation", PROPRIETARY_CANDIDATE_SOURCE,
        )
        all_ner_guess[key] = all_ner_guess.get(key, True) and is_guess
        any_ai[key] = any_ai.get(key, False) or f.source == GLINER_SOURCE
        all_oov[key] = all_oov.get(key, True) and f.source == PROPRIETARY_CANDIDATE_SOURCE
    for key, g in grouped.items():
        g.tier = taxonomy.tier_for(g.max_score, high, medium)
        g.is_ner_guess = all_ner_guess.get(key, False)
        g.is_ai_detected = any_ai.get(key, False)
        g.is_oov_candidate = all_oov.get(key, False)
        # An ML-sourced GDPR Art. 9 finding is NEVER auto-accepted, whatever it
        # scored. Art. 9 types carry a one-way `anonymize` default, so an
        # auto-applied zero-shot false positive destroys legitimate content with
        # no way back -- and zero-shot confidence is not calibrated evidence that
        # a sentence really discloses someone's health, religion or sexuality.
        # Demoting to the review tier keeps the recall (the finding is still
        # surfaced, still defaulted to anonymize) while putting a human in front
        # of every irreversible act. Anchored Art. 9 recognizers are untouched:
        # they demanded a literal label like "Diagnose:" before matching.
        # ...and the same protection for a bare NER guess, for the same reason.
        # Measured: spaCy's English model claims "The Great Depression" as NRP,
        # which is a special category and therefore a ONE-WAY action -- an
        # auto-accepted guess there destroys ordinary business prose with no way
        # back. Both the model and the zero-shot pass are opinions, not evidence;
        # an ANCHORED Art. 9 recognizer that demanded a literal "Diagnose:" is
        # evidence and keeps whatever tier it earns. Costs no recall: the finding
        # is still surfaced and still defaults to anonymize, it just cannot apply
        # itself without a human looking at it.
        unproven = g.is_ai_detected or g.is_ner_guess
        if unproven and taxonomy.is_special_category(g.entity_type) and g.tier == taxonomy.TIER_HIGH:
            g.tier = taxonomy.TIER_MEDIUM

    # Corroboration-only: drop bare ORG/LOCATION/MISC NER guesses (nothing but a
    # flat spaCy hit backs them) -- on business prose these are almost entirely
    # product names / jargon / common nouns, not PII. A corroborated one
    # (propagated, anchored, validated, or name-column) has is_ner_guess False
    # and survives; PERSON and structured IDs are never dropped here. Toggleable
    # so a recall-first deployment can turn it off.
    demoted: list[GroupedFinding] = []
    if config.get("corroboration_only", True):
        kept: dict[tuple[str, str], GroupedFinding] = {}

        def _is_uncorroborated(g: GroupedFinding) -> bool:
            return (
                g.entity_type in _CORROBORATION_ONLY_ENTITIES
                and g.is_ner_guess
                and g.validated is not True
            )

        # A German GENITIVE inherits its base name's corroboration. "Kochs Team" is the
        # same person as "Koch", but NER reports it as its own value, so it formed its
        # own uncorroborated group -- and demoting it left "Kochs" in the clear while
        # "Koch" was redacted everywhere else. That is a LEAK, not a cosmetic gap: the
        # surname is still legible, and the fail-loud verify caught it as a residual
        # (the removed "Koch" still present inside the surviving "Kochs").
        #
        # Narrow on purpose -- value + "s" only, and only when the STEM is itself a
        # corroborated group of the same entity type. "Hans" does not inherit from a
        # "Han" that does not exist.
        corroborated_values = {
            g.value.strip().lower()
            for g in grouped.values()
            if g.entity_type in _CORROBORATION_ONLY_ENTITIES and not _is_uncorroborated(g)
        }

        # The SAME value, corroborated under ANY entity type. Groups are keyed by
        # (type, value), so one name detected as PERSON in one sentence and as
        # ORGANIZATION in another becomes two groups -- and the second one is
        # demoted even though the identical characters are being redacted
        # elsewhere in the document. Measured: "Verteiler: Rechtsabteilung,
        # Winkler, Innenrevision" types Winkler ORGANIZATION, which cost exactly
        # one occurrence on nearly every name in the harness (the uniform "4/5").
        #
        # Matched on the EXACT string, deliberately: identical characters are
        # unarguably the same disclosure, whereas token-overlap across types would
        # let an unrelated "Berg Consulting GmbH" ride in on a person named Berg.
        corroborated_any_type = {
            g.value.strip().lower() for g in grouped.values() if not _is_uncorroborated(g)
        }

        # OCR skeletons of the corroborated values. A scanner mangles SOME
        # occurrences of a name and not others, so "Mul1er" forms its own group
        # next to a corroborated "Müller" and is demoted -- leaving the surname
        # legible in exactly the documents where it was hardest to read. Same
        # argument as the genitive rule above: it is the same person, so it
        # inherits.
        #
        # An ambiguous skeleton is dropped rather than guessed: if two DIFFERENT
        # corroborated values fold together, nothing here can tell which one a
        # damaged token was.
        corroborated_skeletons: dict[str, str] = {}
        for g in grouped.values():
            v = g.value.strip()
            if _is_uncorroborated(g) or " " in v or len(v) < _OCR_MIN_LEN:
                continue
            key = ocr_skeleton(v)
            if len(key) >= _OCR_MIN_LEN:
                if corroborated_skeletons.get(key, v.lower()) != v.lower():
                    corroborated_skeletons[key] = ""  # ambiguous -> never inherit
                else:
                    corroborated_skeletons.setdefault(key, v.lower())

        # A SURNAME inherits from a corroborated FULL NAME containing it. This is the
        # fourth corroboration source, and without it the flip is unshippable: measured,
        # per-OCCURRENCE recall on realistic letters fell from 98% to 57% (foreign names
        # 100% -> 22%) because a bare "Müller"/"Okonkwo" forms its OWN group, and
        # propagation is deliberately not corroboration. Yet it is plainly the same
        # person as the corroborated "Klaus Müller" two lines above.
        #
        # Note the audit workbook could NOT see this -- its recall matching is
        # value-keyed and lenient, so it still read 293/293. Only the per-occurrence
        # harness caught it, which is why both instruments are run.
        corroborated_name_parts = {
            part
            for g in grouped.values()
            if g.entity_type == "PERSON" and not _is_uncorroborated(g)
            # Split on HYPHENS as well as spaces: a double-barrelled name is one
            # group whose halves also appear alone, and the harness scores a
            # half-caught "Schmidt-Rottluff" as a full miss because leaving
            # "Rottluff" legible discloses the person.
            for part in re.split(r"[\s\-]+", g.value.strip().lower())
            if len(part) >= 3
        }

        def _inherits_from_base_name(g: GroupedFinding) -> bool:
            v = g.value.strip().lower()
            # German genitive: "Kochs" is the same person as "Koch".
            if len(v) >= 4 and v.endswith("s") and (
                v[:-1] in corroborated_values or v.rstrip("s").rstrip("'") in corroborated_values
            ):
                return True
            # The identical value, corroborated under some other entity type.
            if v in corroborated_any_type:
                return True
            # An OCR-damaged spelling of a corroborated name.
            if " " not in v and len(v) >= _OCR_MIN_LEN:
                canonical = corroborated_skeletons.get(ocr_skeleton(v))
                if canonical and canonical != v:
                    return True
            # A PERSON name sharing a token with a corroborated PERSON name. BOTH
            # directions are needed and only one used to be: a bare "Winkler"
            # inheriting from "Ayşe Winkler" was handled, but the signature line
            # "Mit freundlichen Grüßen Ayşe Winkler" forms its own group and had to
            # inherit from the corroborated bare "Winkler" -- the other way round.
            # Restricted to PERSON, since a shared token across TYPES is the
            # over-reach the exact-value rule above deliberately avoids.
            if g.entity_type != "PERSON":
                return False
            tokens = [t for t in re.split(r"[\s\-]+", v) if len(t) >= 3]
            return bool(tokens) and any(t in corroborated_name_parts for t in tokens)

        for key, g in grouped.items():
            # A proprietary-name CANDIDATE is never promoted by an inheritance rule.
            # It is the weakest evidence the tool produces, and the entire point of
            # emitting it is that it stays in the band the reviewer scans rather than
            # the list the tool acts on. Measured without this: the exact-value rule
            # promoted them and they cost 3 decoy false positives.
            uncorroborated = _is_uncorroborated(g) and (
                g.is_oov_candidate or not _inherits_from_base_name(g)
            )
            if not uncorroborated:
                kept[key] = g
                continue
            # DEMOTE, never drop. Previously these were discarded outright. For a GDPR
            # redaction tool that is the wrong direction of error: an over-flag costs
            # review time, a miss is a disclosure. So they leave the list the reviewer
            # reads (and the set apply redacts) but stay visible in their own band.
            g.tier = taxonomy.TIER_LOW
            demoted.append(g)
        grouped = kept

    # Bucket the grouped findings into data classes, ordered most-sensitive first.
    class_map: dict[str, DataClassGroup] = {}
    for g in grouped.values():
        dc = taxonomy.data_class_for(g.entity_type)
        dcg = class_map.get(dc.key)
        if dcg is None:
            class_map[dc.key] = dcg = DataClassGroup(key=dc.key, display=dc.display, sensitivity=dc.sensitivity)
        dcg.items.append(g)
    for dcg in class_map.values():
        dcg.items.sort(key=lambda g: (-g.max_score, g.entity_type, g.value.lower()))
    order = {dc.key: dc.order for dc in taxonomy.DATA_CLASSES}
    groups = sorted(class_map.values(), key=lambda d: order.get(d.key, 99))

    possible_misses = completeness_scan(units, findings)

    high_count = sum(1 for g in grouped.values() if g.tier == taxonomy.TIER_HIGH)
    # Triage split: a bare NER guess (no anchor/pattern/validation behind it) is
    # far likelier to be a non-PII common noun / product term than a corroborated
    # finding. Surfaced as counts so the reviewer can bulk-skip the guess bucket
    # (via the "NER guess" confidence band) and focus on the likely-PII majority.
    model_guess = sum(1 for g in grouped.values() if g.is_ner_guess)
    stats = {
        "units_scanned": len(units),
        "distinct_findings": len(grouped),
        "total_occurrences": sum(g.count for g in grouped.values()),
        "auto_accept": high_count,
        "needs_review": len(grouped) - high_count,
        "possible_misses": len(possible_misses),
        "model_guess": model_guess,
        "likely_pii": len(grouped) - model_guess,
        # Reported so the demoted band is never invisible: a reviewer who sees a
        # suspiciously short list can tell at a glance that something was set aside.
        "demoted": len(demoted),
    }
    demoted.sort(key=lambda g: (-g.count, g.entity_type, g.value.lower()))
    return ScanResult(
        groups=groups, possible_misses=possible_misses, stats=stats, demoted=demoted
    )


def build_preview(groups: list[DataClassGroup]) -> list[PreviewGroup]:
    """Text-level before->after preview of what a Save will change, per data
    class. Skipped values are omitted. Pseudonym tokens are shown as a template
    ([PERSON_#]) because the exact number is assigned at apply time; the '#'
    signals a stable, consistent token. A summarize row shows the exact
    zero-content structural placeholder the cell will become."""
    from .actions import _structural_summary

    preview: list[PreviewGroup] = []
    for dcg in groups:
        rows: list[PreviewRow] = []
        for g in dcg.items:
            if g.action == "skip":
                continue
            label = token_label(g.entity_type)
            if g.action == "pseudonymize":
                token = f"[{label}_#]"
            elif g.action == "summarize":
                token = f"[{label}: {_structural_summary(g.value)}]"
            else:  # redact / anonymize
                token = f"[{label}]"
            rows.append(
                PreviewRow(entity_type=g.entity_type, value=g.value, action=g.action, token=token, context=g.context)
            )
        if rows:
            preview.append(PreviewGroup(display=dcg.display, rows=rows))
    return preview


# --- diagnostic export ------------------------------------------------------
# UNLIKE report.py (which deliberately records NO original values, so it is safe
# to keep beside the anonymized document), this export DUMPS the raw flagged
# values and their surrounding context. That is the whole point -- it exists so
# a human (or an assistant) can see exactly what got flagged and why, to tune
# precision. It therefore contains original, potentially-sensitive data and must
# be treated as such (see the GUI's warning on the export button).

_EXPORT_COLUMNS = [
    "bucket",  # "flagged" (will be acted on) | "possible_miss" (informational)
    "data_class",
    "entity_type",
    "value",
    "count",
    "max_score",
    "tier",
    "is_ner_guess",  # True = a raw spaCy NER guess with nothing corroborating it
    "validated",  # checksum verdict: True/False/None
    "default_action",
    "context",
]


def findings_export_rows(result: ScanResult) -> list[dict]:
    """One dict per distinct finding (actionable + possible-miss), with the raw
    value and context, for the diagnostic CSV. Ordered most-sensitive class
    first, then by descending occurrence count -- so the noisiest items a
    reviewer would want to understand first sit at the top."""
    rows: list[dict] = []
    for dcg in result.groups:
        for g in sorted(dcg.items, key=lambda g: -g.count):
            rows.append(
                {
                    "bucket": "flagged",
                    "data_class": dcg.display,
                    "entity_type": g.entity_type,
                    "value": g.value,
                    "count": g.count,
                    "max_score": round(g.max_score, 3),
                    "tier": g.tier,
                    "is_ner_guess": g.is_ner_guess,
                    "validated": g.validated,
                    "default_action": g.action,
                    "context": g.context,
                }
            )
    # The DEMOTED band, in its own bucket -- the shape the export already uses for
    # possible_miss. Nothing is hidden: the export stays a complete record of what the
    # tool saw, while the "flagged" section a reviewer actually reads stays short.
    for g in result.demoted:
        rows.append(
            {
                "bucket": "demoted",
                "data_class": "(demoted — nothing corroborated this)",
                "entity_type": g.entity_type,
                "value": g.value,
                "count": g.count,
                "max_score": round(g.max_score, 3),
                "tier": g.tier,
                "is_ner_guess": g.is_ner_guess,
                "validated": g.validated,
                "default_action": "skip",
                "context": g.context,
            }
        )
    for g in result.possible_misses:
        rows.append(
            {
                "bucket": "possible_miss",
                "data_class": "(possible miss — no recognizer matched)",
                "entity_type": g.entity_type,
                "value": g.value,
                "count": g.count,
                "max_score": round(g.max_score, 3),
                "tier": g.tier,
                "is_ner_guess": g.is_ner_guess,
                "validated": g.validated,
                "default_action": g.action,
                "context": g.context,
            }
        )
    return rows


def findings_summary(result: ScanResult) -> dict:
    """Compact aggregate breakdown of a scan -- the fastest read on WHY a count
    is high (which entity type / data class / tier dominates, and how much of
    it is raw NER guessing). Cheap; safe to compute for a notification."""
    actionable = result.all_actionable()
    by_entity: dict[str, int] = {}
    by_class: dict[str, int] = {}
    by_tier: dict[str, int] = {}
    ner_guess = 0
    for g in actionable:
        by_entity[g.entity_type] = by_entity.get(g.entity_type, 0) + 1
        by_tier[g.tier] = by_tier.get(g.tier, 0) + 1
        if g.is_ner_guess:
            ner_guess += 1
    for dcg in result.groups:
        by_class[dcg.display] = by_class.get(dcg.display, 0) + len(dcg.items)
    return {
        "distinct_findings": len(actionable),
        "total_occurrences": sum(g.count for g in actionable),
        "ner_guess_findings": ner_guess,
        "possible_misses": len(result.possible_misses),
        "by_entity_type": dict(sorted(by_entity.items(), key=lambda kv: -kv[1])),
        "by_data_class": dict(sorted(by_class.items(), key=lambda kv: -kv[1])),
        "by_tier": by_tier,
    }


def write_findings_csv(result: ScanResult, csv_path) -> int:
    """Writes the diagnostic export to `csv_path` (utf-8-sig so Excel opens the
    umlauts correctly). Returns the row count. Column-policy metadata, when
    present (spreadsheets), is appended as trailing comment-style rows so a
    single file carries the whole picture."""
    import csv
    from pathlib import Path

    rows = findings_export_rows(result)
    path = Path(csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_EXPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
        if result.columns:
            fh.write("\n")
            col_writer = csv.writer(fh)
            col_writer.writerow(["# columns", "sheet", "column", "header", "pii_count", "name_override", "sample"])
            for c in result.columns:
                col_writer.writerow(["", c.sheet, c.column, c.header, c.pii_count, c.name_override, c.sample])
    return len(rows)
