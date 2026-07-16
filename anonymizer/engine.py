from __future__ import annotations

import regex
from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_analyzer.nlp_engine.ner_model_configuration import MODEL_TO_PRESIDIO_ENTITY_MAPPING
from presidio_analyzer.predefined_recognizers import (
    CreditCardRecognizer,
    EmailRecognizer,
    IbanRecognizer,
    PhoneRecognizer,
)

SPACY_MODELS = {
    "de": "de_core_news_lg",
    "en": "en_core_web_md",
}

# The single source of truth for "which languages does this tool support".
# Order matters: the first entry is the fallback when a document's language
# cannot be determined (German, this being a German bank).
DEFAULT_LANGUAGES = ("de", "en")

# Built-in pattern recognizers we want available regardless of the scan
# language. Presidio registers these for English only by default; because a
# document is now scanned in a SINGLE detected language (to avoid cross-language
# NER noise), a German scan would otherwise miss IBANs/emails/cards/foreign
# phones. So we add a copy for every supported language. These are pure
# regex/checksum/library lookups -- language only affects context boosting -- so
# cross-registering is safe. PhoneRecognizer (phonenumbers-backed) catches
# international client numbers that the German-only DE_PHONE pattern rejects;
# overlap resolution in core.detect_unit dedupes it against DE_PHONE.
_PORTABLE_PATTERN_RECOGNIZERS = (IbanRecognizer, EmailRecognizer, CreditCardRecognizer, PhoneRecognizer)

# The German spaCy label set is PER/LOC/ORG/MISC, and the model puts real names
# it cannot confidently classify into MISC -- measured: "Frau Bauer zahlt." ->
# ('Frau Bauer', 'MISC'). Presidio's default mapping has NO MISC key, so those
# spans were silently DISCARDED and the name leaked with no trace. Route MISC to
# its own reviewable entity instead of dropping it: it is not confidently a
# person, so it must not be auto-accepted, but it must be SEEN.
_ENTITY_MAPPING = {**MODEL_TO_PRESIDIO_ENTITY_MAPPING, "MISC": "NER_MISC"}

# Presidio's PatternRecognizer defaults to regex.I|M|S -- IGNORECASE -- which
# silently defeats every [A-Z]-based pattern (the BIC regex matched the ordinary
# lowercase words "geehrter" and "ausgefuehrt"; harmless only until the
# sensitivity slider lowers the threshold under the base score). Case-sensitive
# recognizers must opt in via `case_sensitive: true` in the YAML.
_CASE_SENSITIVE_FLAGS = regex.MULTILINE | regex.DOTALL

# Names spaCy demonstrably misses. de_core_news_lg's NER is WikiNER-trained, so
# it keys off well-formed sentence context; a name in a form field, a table cell
# or a salutation gives it nothing. Measured misses include the single most
# common line in a German bank letter -- "Sehr geehrter Herr Müller," -- and
# every labelled field (Name:/Kunde:). These anchors key off explicit German
# business-letter structure instead, so they are high-precision.
#
# Two implementation details that are easy to get wrong:
#  * Presidio returns the FULL match span, not a capture group -- so the
#    honorific is excluded with a LOOKBEHIND (the `regex` module allows it to be
#    variable-width), otherwise the token would be "Herr Müller" and the
#    pseudonym would read [PERSON_1] for "Herr Müller".
#  * These must be case-sensitive, hence _CASE_SENSITIVE_FLAGS.
_NAME = r"[A-ZÄÖÜ][a-zäöüß]+(?:[-\s][A-ZÄÖÜ][a-zäöüß]+){0,2}"
_HONORIFICS = r"(?:Herr|Frau|Hr\.|Fr\.|Dr\.|Prof\.)"
_NAME_LABELS = (
    r"(?:Name|Kunde|Kundin|Kontoinhaber|Sachbearbeiter|Ansprechpartner|Empfänger|"
    r"Berater|Beraterin|Mitarbeiter|Antragsteller|Versicherungsnehmer|Vertragspartner)"
)
_ANCHORED_NAME_PATTERNS = [
    Pattern(name="honorific_name", regex=rf"(?<=\b{_HONORIFICS}\s+){_NAME}", score=0.75),
    Pattern(name="labelled_name", regex=rf"(?<=\b{_NAME_LABELS}\s*:\s*){_NAME}", score=0.70),
]


def build_analyzer(config: dict) -> AnalyzerEngine:
    """Builds the Presidio analyzer (spaCy NLP engine + recognizers). Detection
    logic itself lives in `core`; language *selection* per document lives in
    `pipeline`/`language`. This just assembles an engine that can run either
    supported language on demand."""
    languages = config.get("languages") or list(DEFAULT_LANGUAGES)
    unknown = [lang for lang in languages if lang not in SPACY_MODELS]
    if unknown:
        raise ValueError(
            f"No spaCy model configured for language(s) {unknown}. Supported: {sorted(SPACY_MODELS)}."
        )
    nlp_config = {
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": lang, "model_name": SPACY_MODELS[lang]} for lang in languages],
        "ner_model_configuration": {"model_to_presidio_entity_mapping": _ENTITY_MAPPING},
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
        kwargs = {}
        if rec_cfg.get("case_sensitive"):
            kwargs["global_regex_flags"] = _CASE_SENSITIVE_FLAGS
        for lang in languages:
            analyzer.registry.add_recognizer(
                PatternRecognizer(
                    supported_entity=rec_cfg["name"],
                    patterns=patterns,
                    context=rec_cfg.get("context", []),
                    supported_language=lang,
                    **kwargs,
                )
            )

    # Structure-anchored name detection, for every language (a German letter can
    # appear in an otherwise-English document). Emits PERSON so it merges into
    # the existing People group/tier/action with no taxonomy change; the 0.75
    # score keeps it under the 0.9 auto-accept bar -- fallible name detection
    # stays under human eyes, as the tier config intends.
    for lang in languages:
        analyzer.registry.add_recognizer(
            PatternRecognizer(
                supported_entity="PERSON",
                patterns=_ANCHORED_NAME_PATTERNS,
                supported_language=lang,
                global_regex_flags=_CASE_SENSITIVE_FLAGS,
            )
        )
    return analyzer
