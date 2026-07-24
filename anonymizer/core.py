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

from . import taxonomy, validators
from .actions import token_label
from .engine import DEFAULT_LANGUAGES
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
_HONORIFIC_PREFIX = re.compile(r"^(?:Herrn?|Frau|Hr\.|Fr\.|Dr\.|Prof\.)\s+")

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

# ORGANIZATION/LOCATION/NER_MISC are "medium sensitivity" free-text NER guesses,
# and on a business document they are overwhelmingly non-PII (product names,
# jargon, common nouns). In corroboration-only mode a finding of these types is
# surfaced ONLY when something beyond a bare spaCy guess backs it: a
# pattern/anchor/propagation source, a checksum verdict, or a name-column
# override -- i.e. NOT is_ner_guess. PERSON and all structured-ID entities are
# never gated this way (they are the core PII targets).
_CORROBORATION_ONLY_ENTITIES = frozenset({"NER_MISC", "ORGANIZATION", "LOCATION"})


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
    the free-text NER entity types."""
    return entity_type in _NER_ENTITIES and any(ch.isdigit() for ch in value)


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
    return (
        _is_noise_entity(entity_type, value, analyzer, lang)
        or _is_structural_nonname(entity_type, value)
        or _is_pos_implausible(entity_type, start, end, nlp_artifacts)
        or _is_digit_bearing_code(entity_type, value)
        or _is_german_nominalization(entity_type, value, start, end, nlp_artifacts)
    )


_STRUCTURAL_MARKER = re.compile(r"(?m)^([-*+#]+)(?=\S)")


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
    post-detection, by _LEADING_NOISE trimming any free-text finding's span."""
    return _STRUCTURAL_MARKER.sub(lambda m: " " * len(m.group(1)), text)


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
    xlsx_handler's _name_header_re."""
    return [(entity_type, value, re.compile(rf"(?<!\w){re.escape(value)}(?!\w)")) for entity_type, value in propagate]


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
    """Applies checksum validation: a validated structured ID is promoted to the
    auto-accept tier; a checksum-failing one is zeroed so the threshold filter
    drops it from the actionable set (it re-surfaces as a possible-miss)."""
    verdict = validators.validate(finding.entity_type, finding.value)
    finding.validated = verdict
    if verdict is True:
        finding.score = max(finding.score, _VALIDATED_SCORE)
    elif verdict is False:
        # Demote, don't drop -- a checksum-failing IBAN/card is still identifying.
        finding.score = min(finding.score, _INVALID_SCORE)
    return finding


def _resolve_overlaps(findings: list[Finding], text: str) -> list[Finding]:
    """Keeps a non-overlapping set. Apply replaces spans by splicing text and
    ASSUMES they never overlap (see the format handlers' run/cell replacement);
    two recognizers claiming overlapping-but-not-identical spans for the same
    text (e.g. the built-in PHONE_NUMBER and the custom DE_PHONE on one number,
    or spaCy's city-only LOCATION inside a full DE_ADDRESS) would otherwise
    corrupt the output or silently drop a redaction.

    We keep the LONGER span first, then the higher score: for a redaction tool,
    covering MORE of a value is always safer than covering less, so the full
    address wins over the bare city and the complete phone wins over a fragment.
    On an exact tie (same span and score, e.g. DE_ADDRESS vs spaCy LOCATION on one
    PLZ+city) we prefer the specific pattern recognizer over the generic NER label
    and then break ties by entity type, so the result is deterministic. Touching
    spans (end == next start) do not overlap.

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
            -(f.end - f.start),
            -f.score,
            f.entity_type in _NER_ENTITIES,  # specific pattern recognizers win ties
            f.entity_type,
            f.start,
        ),
    )
    kept: list[Finding] = []
    for f in ordered:
        overlappers = [k for k in kept if f.start < k.end and k.start < f.end]
        if not overlappers:
            kept.append(f)
            continue
        contained_in = next((k for k in overlappers if k.start <= f.start and f.end <= k.end), None)
        if contained_in is not None:
            _absorb_corroborating_source(contained_in, f)
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
    return sorted(kept, key=lambda f: f.start)


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
    if kept.source == "SpacyRecognizer" and dropped.source not in ("SpacyRecognizer", "propagation", ""):
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
    re.compile(r"\d[\d ./-]{3,}\d"),  # 5+ char digit-ish runs (phones, ids, ...)
    re.compile(r"\b[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b"),  # BIC/SWIFT-shaped
]


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
                    and not validators.bic_valid(value)
                ):
                    continue  # too few digits, not an email, not a BIC -> not risky enough
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
        is_guess = f.entity_type in _NER_ENTITIES and f.source in ("SpacyRecognizer", "propagation")
        all_ner_guess[key] = all_ner_guess.get(key, True) and is_guess
    for key, g in grouped.items():
        g.tier = taxonomy.tier_for(g.max_score, high, medium)
        g.is_ner_guess = all_ner_guess.get(key, False)

    # Corroboration-only: drop bare ORG/LOCATION/MISC NER guesses (nothing but a
    # flat spaCy hit backs them) -- on business prose these are almost entirely
    # product names / jargon / common nouns, not PII. A corroborated one
    # (propagated, anchored, validated, or name-column) has is_ner_guess False
    # and survives; PERSON and structured IDs are never dropped here. Toggleable
    # so a recall-first deployment can turn it off.
    if config.get("corroboration_only", True):
        grouped = {
            key: g
            for key, g in grouped.items()
            if not (g.entity_type in _CORROBORATION_ONLY_ENTITIES and g.is_ner_guess and g.validated is not True)
        }

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
    }
    return ScanResult(groups=groups, possible_misses=possible_misses, stats=stats)


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
