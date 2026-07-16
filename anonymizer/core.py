"""Shared detection + review core.

This is the single detection code path used by BOTH scan and apply, so what the
reviewer approved is exactly what gets written (scan/apply parity by
construction -- no divergent per-handler detection logic). Format handlers are
thin adapters: they only turn a document into TextUnits and apply span
replacements; all the "what is sensitive and what tier is it" logic lives here.
"""

from __future__ import annotations

import re

from . import taxonomy, validators
from .actions import token_label
from .engine import DEFAULT_LANGUAGES
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
# flagged with the "unverified" chip) instead of dropped -- the reviewer
# decides. IDs whose threshold sits above this score (e.g. Steuer-ID at 0.6)
# still fall out of the actionable set and re-surface via the completeness scan,
# so nothing is silently lost.
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
_HONORIFIC_PREFIX = re.compile(r"^(?:Herr|Frau|Hr\.|Fr\.|Dr\.|Prof\.)\s+")


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


def _resolve_overlaps(findings: list[Finding]) -> list[Finding]:
    """Keeps a non-overlapping set. Apply replaces spans by splicing text and
    ASSUMES they never overlap (see the format handlers' run/cell replacement);
    two recognizers claiming overlapping-but-not-identical spans for the same
    text (e.g. the built-in PHONE_NUMBER and the custom DE_PHONE on one number,
    or spaCy's city-only LOCATION inside a full DE_ADDRESS) would otherwise
    corrupt the output or silently drop a redaction.

    We keep the LONGER span first, then the higher score: for a redaction tool,
    covering MORE of a value is always safer than covering less, so the full
    address wins over the bare city and the complete phone wins over a fragment.
    A kept superset also covers any contained finding's PII. On an exact tie
    (same span and score, e.g. DE_ADDRESS vs spaCy LOCATION on one PLZ+city) we
    prefer the specific pattern recognizer over the generic NER label and then
    break ties by entity type, so the result is deterministic. Touching spans
    (end == next start) do not overlap.
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
        if any(f.start < k.end and k.start < f.end for k in kept):
            continue
        kept.append(f)
    return sorted(kept, key=lambda f: f.start)


def detect_unit(analyzer, unit: TextUnit, config: dict) -> list[Finding]:
    """THE detection primitive -- one overlap-resolved list of findings for a
    unit. Used identically by scan and apply."""
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
    wanted_entities = list(entities_cfg.keys())

    candidates: list[Finding] = []

    for lang in languages:
        results = analyzer.analyze(text=unit.text, language=lang, entities=wanted_entities, allow_list=allow_list)
        for r in results:
            start, end = r.start, r.end
            value = unit.text[start:end]
            if r.entity_type == "PERSON":
                trimmed = _HONORIFIC_PREFIX.match(value)
                if trimmed:
                    start += trimmed.end()
                    value = value[trimmed.end() :]
            finding = Finding(
                entity_type=r.entity_type,
                value=value,
                score=r.score,
                context=_snippet(unit.text, start, end),
                unit_id=unit.id,
                start=start,
                end=end,
            )
            _refine(finding)
            threshold = entities_cfg.get(r.entity_type, {}).get("confidence_threshold", 0.5)
            if finding.score < max(0.0, threshold - sensitivity):
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
    for entity_type, value in config.get("propagate", []):
        for m in re.finditer(rf"(?<!\w){re.escape(value)}(?!\w)", unit.text):
            candidates.append(
                Finding(
                    entity_type=entity_type,
                    value=m.group(),
                    score=_PROPAGATED_SCORE,
                    context=_snippet(unit.text, m.start(), m.end()),
                    unit_id=unit.id,
                    start=m.start(),
                    end=m.end(),
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
            )
        )

    return _resolve_overlaps(candidates)


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
    for g in grouped.values():
        g.tier = taxonomy.tier_for(g.max_score, high, medium)

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
    stats = {
        "units_scanned": len(units),
        "distinct_findings": len(grouped),
        "total_occurrences": sum(g.count for g in grouped.values()),
        "auto_accept": high_count,
        "needs_review": len(grouped) - high_count,
        "possible_misses": len(possible_misses),
    }
    return ScanResult(groups=groups, possible_misses=possible_misses, stats=stats)


def build_preview(groups: list[DataClassGroup]) -> list[PreviewGroup]:
    """Text-level before->after preview of what a Save will change, per data
    class. Skipped values are omitted. Pseudonym tokens are shown as a template
    ([PERSON_#]) because the exact number is assigned at apply time; the '#'
    signals a stable, consistent token."""
    preview: list[PreviewGroup] = []
    for dcg in groups:
        rows: list[PreviewRow] = []
        for g in dcg.items:
            if g.action == "skip":
                continue
            label = token_label(g.entity_type)
            token = f"[{label}_#]" if g.action == "pseudonymize" else f"[{label}]"
            rows.append(PreviewRow(entity_type=g.entity_type, value=g.value, action=g.action, token=token))
        if rows:
            preview.append(PreviewGroup(display=dcg.display, rows=rows))
    return preview
