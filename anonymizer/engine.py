from __future__ import annotations

import re

from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
from presidio_analyzer.nlp_engine import NlpEngineProvider

from .models import Finding, GroupedFinding, TextUnit

SPACY_MODELS = {
    "de": "de_core_news_md",
    "en": "en_core_web_md",
}

CONTEXT_SNIPPET_RADIUS = 40


def build_analyzer(config: dict) -> AnalyzerEngine:
    languages = config.get("languages", ["en"])
    nlp_config = {
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": lang, "model_name": SPACY_MODELS[lang]} for lang in languages],
    }
    provider = NlpEngineProvider(nlp_configuration=nlp_config)
    nlp_engine = provider.create_engine()
    analyzer = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=languages)

    for rec_cfg in config.get("custom_recognizers", []):
        patterns = [Pattern(name=rec_cfg["name"], regex=p["regex"], score=p["score"]) for p in rec_cfg["patterns"]]
        recognizer = PatternRecognizer(
            supported_entity=rec_cfg["name"],
            patterns=patterns,
            context=rec_cfg.get("context", []),
            supported_language=rec_cfg.get("language", languages[0]),
        )
        analyzer.registry.add_recognizer(recognizer)
    return analyzer


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


def analyze_unit(analyzer: AnalyzerEngine, unit: TextUnit, config: dict) -> list[Finding]:
    languages = config.get("languages", ["en"])
    allow_list = config.get("allow_list", [])
    deny_list = config.get("deny_list", [])
    entities_cfg = config.get("entities", {})

    seen_spans: dict[tuple[int, int], Finding] = {}

    for lang in languages:
        results = analyzer.analyze(text=unit.text, language=lang, allow_list=allow_list)
        for r in results:
            threshold = entities_cfg.get(r.entity_type, {}).get("confidence_threshold", 0.5)
            if r.score < threshold:
                continue
            span = (r.start, r.end)
            existing = seen_spans.get(span)
            if existing is None or r.score > existing.score:
                seen_spans[span] = Finding(
                    entity_type=r.entity_type,
                    value=unit.text[r.start:r.end],
                    score=r.score,
                    context=_snippet(unit.text, r.start, r.end),
                    unit_id=unit.id,
                    start=r.start,
                    end=r.end,
                )

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


def group_findings(findings: list[Finding], config: dict) -> list[GroupedFinding]:
    entities_cfg = config.get("entities", {})
    groups: dict[tuple[str, str], GroupedFinding] = {}
    for f in findings:
        key = (f.entity_type, f.value.strip().lower())
        default_action = entities_cfg.get(f.entity_type, {}).get("default_action", "anonymize")
        if key not in groups:
            groups[key] = GroupedFinding(
                entity_type=f.entity_type,
                value=f.value,
                count=0,
                max_score=f.score,
                context=f.context,
                action=default_action,
            )
        g = groups[key]
        g.count += 1
        g.max_score = max(g.max_score, f.score)
    return sorted(groups.values(), key=lambda g: (-g.max_score, g.entity_type))
