from __future__ import annotations

from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
from presidio_analyzer.nlp_engine import NlpEngineProvider

SPACY_MODELS = {
    "de": "de_core_news_md",
    "en": "en_core_web_md",
}


def build_analyzer(config: dict) -> AnalyzerEngine:
    """Builds the Presidio analyzer (spaCy NLP engine + custom recognizers).
    Detection logic itself lives in `core` -- this only assembles the engine."""
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
