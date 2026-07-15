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
from .models import DataClassGroup, Finding, GroupedFinding, PreviewGroup, PreviewRow, ScanResult, TextUnit

CONTEXT_SNIPPET_RADIUS = 40

# How many distinct possible-misses to surface before truncating (informational
# bucket -- a full list of every digit-run in a 200-page doc helps no one).
MAX_POSSIBLE_MISSES = 300

# Confidence assigned to a checksum-validated ID (forces the high/auto-accept
# tier) and to one whose checksum FAILED (0.0 -> filtered out of the actionable
# set; it re-surfaces via the completeness scan as a possible-miss, so it is
# never silently lost).
_VALIDATED_SCORE = 0.98
_INVALID_SCORE = 0.0


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
        finding.score = _INVALID_SCORE
    return finding


def detect_unit(analyzer, unit: TextUnit, config: dict) -> list[Finding]:
    """THE detection primitive -- one span-deduped list of findings for a unit.
    Used identically by scan and apply."""
    languages = config.get("languages", ["en"])
    allow_list = config.get("allow_list", [])
    deny_list = config.get("deny_list", [])
    entities_cfg = config.get("entities", {})
    # Global recall/precision offset (sensitivity slider). Positive lowers every
    # threshold (more recall); default 0 keeps shipped behaviour.
    sensitivity = float(config.get("sensitivity", 0.0))
    wanted_entities = list(entities_cfg.keys())

    seen_spans: dict[tuple[int, int], Finding] = {}

    for lang in languages:
        results = analyzer.analyze(text=unit.text, language=lang, entities=wanted_entities, allow_list=allow_list)
        for r in results:
            finding = Finding(
                entity_type=r.entity_type,
                value=unit.text[r.start : r.end],
                score=r.score,
                context=_snippet(unit.text, r.start, r.end),
                unit_id=unit.id,
                start=r.start,
                end=r.end,
            )
            _refine(finding)
            threshold = entities_cfg.get(r.entity_type, {}).get("confidence_threshold", 0.5)
            if finding.score < max(0.0, threshold - sensitivity):
                continue
            span = (r.start, r.end)
            existing = seen_spans.get(span)
            if existing is None or finding.score > existing.score:
                seen_spans[span] = finding

    for start, end, entity_type in _deny_list_findings(unit.text, deny_list):
        span = (start, end)
        if span not in seen_spans:
            seen_spans[span] = Finding(
                entity_type=entity_type,
                value=unit.text[start:end],
                score=1.0,
                context=_snippet(unit.text, start, end),
                unit_id=unit.id,
                start=start,
                end=end,
            )

    return list(seen_spans.values())


def detect_all(analyzer, units: list[TextUnit], config: dict) -> list[Finding]:
    findings: list[Finding] = []
    for unit in units:
        findings.extend(detect_unit(analyzer, unit, config))
    return findings


# --- completeness / unmatched-risk scan -------------------------------------

_MISS_PATTERNS = [
    re.compile(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}"),  # IBAN-shaped
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),  # email-shaped
    re.compile(r"\d[\d ./-]{3,}\d"),  # 5+ char digit-ish runs (phones, ids, ...)
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
                if sum(c.isdigit() for c in value) < 4 and "@" not in value:
                    continue  # too few digits and not an email -> not risky enough
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
