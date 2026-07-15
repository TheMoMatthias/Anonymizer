from __future__ import annotations

from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_analyzer.predefined_recognizers import (
    CreditCardRecognizer,
    EmailRecognizer,
    IbanRecognizer,
)

SPACY_MODELS = {
    "de": "de_core_news_lg",
    "en": "en_core_web_md",
}

# Built-in pattern recognizers we want available regardless of the scan
# language. Presidio registers these for English only by default; because a
# document is now scanned in a SINGLE detected language (to avoid cross-language
# NER noise), a German scan would otherwise miss IBANs/emails/cards. So we add a
# copy for every supported language. These are pure regex/checksum -- language
# only affects context boosting -- so cross-registering is safe.
_PORTABLE_PATTERN_RECOGNIZERS = (IbanRecognizer, EmailRecognizer, CreditCardRecognizer)


def build_analyzer(config: dict) -> AnalyzerEngine:
    """Builds the Presidio analyzer (spaCy NLP engine + recognizers). Detection
    logic itself lives in `core`; language *selection* per document lives in
    `pipeline`/`language`. This just assembles an engine that can run either
    supported language on demand."""
    languages = config.get("languages", ["de", "en"])
    nlp_config = {
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": lang, "model_name": SPACY_MODELS[lang]} for lang in languages],
    }
    provider = NlpEngineProvider(nlp_configuration=nlp_config)
    nlp_engine = provider.create_engine()
    analyzer = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=languages)

    # Cross-register the built-in pattern recognizers to non-default languages.
    for lang in languages:
        if lang == "en":
            continue  # already registered for English by default
        for cls in _PORTABLE_PATTERN_RECOGNIZERS:
            analyzer.registry.add_recognizer(cls(supported_language=lang))

    # Custom recognizers: register for EVERY supported language so a scan in any
    # single language still catches the German bank identifiers (a German ID can
    # appear in an otherwise-English document).
    for rec_cfg in config.get("custom_recognizers", []):
        patterns = [Pattern(name=rec_cfg["name"], regex=p["regex"], score=p["score"]) for p in rec_cfg["patterns"]]
        for lang in languages:
            analyzer.registry.add_recognizer(
                PatternRecognizer(
                    supported_entity=rec_cfg["name"],
                    patterns=patterns,
                    context=rec_cfg.get("context", []),
                    supported_language=lang,
                )
            )
    return analyzer
