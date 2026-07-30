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

# MEASURED 2026-07-26, do not "optimize" this to the _sm models without redoing
# the measurement. Downgrading de->sm / en->sm saves ~500 MB of bundle but broke
# tests/test_fail_loud.py::test_docx_field_code_hyperlink_is_surfaced_and_redacted:
# a Word mail-merge FIELD CODE document could no longer be saved AT ALL -- the
# fail-loud verify found a removed value surviving verbatim and blocked the write.
# Note the recorded gate for this trial (test_precision.py + test_language.py) went
# GREEN: it watched precision and never watched recall, so the gate was too narrow.
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
# Unicode properties, NOT [A-ZÄÖÜ][a-zäöüß]: an ASCII+umlaut class silently
# fails on the international names a German bank actually holds -- measured, it
# missed "Yılmaz" (Turkish dotless ı). \p{Lu}\p{L}+ covers Turkish, Polish,
# Romanian, Vietnamese and ALL-CAPS forms. (The `regex` module supports \p{}.)
#
# Nobiliary/patronymic PARTICLES are lowercase, and requiring every token to
# start with \p{Lu} meant a particle name matched NOTHING here -- not even
# partially. Measured on the hardened recall harness: "Sehr geehrter Herr von
# Bergen," scored 38% for the particle stratum where every other stratum scored
# 100%, and "zu Guttenberg" was missed in EVERY context (0/5 even in a full
# letter). The particle has to be allowed both at the start of the name and
# BETWEEN its tokens, because German stacks them ("von der Leyen", "van den
# Broek", "de la Cruz").
_NAME_PARTICLE = (
    r"(?:von|van|de|der|den|del|della|di|da|dos|du|zu|zur|zum|la|le|el|al|bin|ibn|ter|ten|of)"
)
_NAME = (
    rf"(?:{_NAME_PARTICLE}\s+)*\p{{Lu}}\p{{L}}+"
    rf"(?:[-\s](?:{_NAME_PARTICLE}\s+)*\p{{Lu}}\p{{L}}+){{0,2}}"
)
# `Herrn?` also matches the dative "Herrn" that opens a German postal address
# block ("Herrn\n<Name>\n<Straße>") -- the single most common place a customer name
# appears in a bank letter, and exactly the sparse-context spot spaCy misses.
# ENGLISH honorifics/labels are included too, and these patterns are registered
# for EVERY scan language (below), so an English person's name in a
# German-dominant document ("Mr Smith", "Client: John Baker") is still caught
# even though only the German NER model runs -- the "layered" mixed-language
# strategy: dominant-language NER + language-independent anchors, rather than
# running a second full NER model (which re-tags ordinary German words as noise).
_HONORIFICS = r"(?:Herrn?|Frau|Hr\.|Fr\.|Dr\.|Prof\.|Mr\.?|Mrs\.?|Ms\.?|Miss|Sir|Madam)"

# The STRIPPER, derived from the same alternation so the two can never drift apart.
#
# They had drifted: this list has recognised English honorifics all along, but both
# strippers (core and pipeline) were German-only, so an English name kept its title
# INSIDE the finding value -- "Ms Priya Whitfield", "Mr Amina Adeyemi". Two measured
# consequences, both real:
#   * the pseudonym is keyed on the title, so the same person is [PERSON_1] as
#     "Mr Amina Adeyemi" and [PERSON_2] as a bare "Adeyemi" elsewhere;
#   * the given-name gazetteer tests the FIRST token, which was "Mr" -- so the name
#     could not be corroborated, and under corroboration-only it would be demoted and
#     LEAK. That is how this was found.
HONORIFIC_PREFIX_RE = regex.compile(rf"^{_HONORIFICS}\s+")
_NAME_LABELS = (
    r"(?:Name|Kunde|Kundin|Kontoinhaber|Sachbearbeiter|Ansprechpartner|Empfänger|"
    r"Berater|Beraterin|Mitarbeiter|Antragsteller|Versicherungsnehmer|Vertragspartner|"
    # English labels for a mixed-language document:
    r"Customer|Client|Contact|Beneficiary|Applicant|Representative|Attn)"
)
# ROLE NOUNS that introduce a person directly, with no colon and no honorific
# ("Der Einreicher Winkler aus Frankfurt bestätigte den Sachverhalt"). This is
# the single worst-measured shape in the whole harness: before these patterns
# existed, `role_reference` scored 0% for German common-noun surnames, 12% for
# rare German ones and 12-50% for foreign ones -- i.e. naming somebody by their
# ROLE rather than their title defeated detection almost completely, across
# every stratum, in text that is otherwise perfectly ordinary German.
#
# Deliberately scored into the REVIEW tier rather than auto-accept: "der Berater
# Deutsche Bank" is a construction this will claim. Per the tool's posture a
# reviewed over-flag costs minutes and a miss is a disclosure.
_ROLE_NOUNS = (
    r"(?:Einreicher|Antragsteller|Kontoinhaber|Mandant|Bearbeiter|Sachbearbeiter|"
    r"Berater|Beraterin|Mitarbeiter|Mitarbeiterin|Kollege|Kollegin|Bürge|Erbe|Erblasser|"
    r"Vollmachtnehmer|Bevollmächtigte[rn]?|Geschäftsführer|Prokurist|Gesellschafter|"
    r"Treuhänder|Begünstigte[rn]?|Versicherungsnehmer|Vertragspartner|Zeuge|Gutachter|"
    r"Notar|Rechtsanwalt|Anwalt|Steuerberater|Prüfer|Revisor|Teilnehmer|Absender|"
    r"Zeichnungsberechtigte[rn]?|Verfasser|Unterzeichner|Auftraggeber|Zahlungsempfänger|"
    # English equivalents, registered for every scan language like the labels above.
    r"Applicant|Signatory|Trustee|Guarantor|Witness|Auditor|Attorney|Sender)"
)
# Markers that introduce a BIRTH/former name. German records state a maiden name
# with a bare abbreviation and no other cue ("Die Kundin, geb. Winkler, ...");
# measured at 20% for the common-noun stratum without this.
_BIRTH_NAME_MARKERS = r"(?:geb\.|geborene[rn]?|verw\.|verwitwete[rn]?|verh\.|verheiratete[rn]?|née|nee)"

_ANCHORED_NAME_PATTERNS = [
    Pattern(name="honorific_name", regex=rf"(?<=\b{_HONORIFICS}\s+){_NAME}", score=0.75),
    Pattern(name="labelled_name", regex=rf"(?<=\b{_NAME_LABELS}\s*:\s*){_NAME}", score=0.70),
    # The honorific is excluded so "Der Antragsteller Herr Müller" yields "Müller"
    # and not "Herr Müller" -- a title inside the value keys the pseudonym on the
    # title, which splits one person across two placeholders.
    Pattern(
        name="role_noun_name",
        regex=rf"(?<=\b{_ROLE_NOUNS}\s+)(?!{_HONORIFICS}\b){_NAME}",
        score=0.60,
    ),
    Pattern(name="birth_name", regex=rf"(?<=\b{_BIRTH_NAME_MARKERS}\s+){_NAME}", score=0.65),
    # An initial before a surname ("B. Winkler", "M. Schmidt-Rottluff"). A single
    # capital letter with a period, immediately before a capitalized word, is an
    # initial in almost all correspondence; the residual risk ("Abschnitt B.
    # Vertragsdaten") is why this sits at the BOTTOM of the review tier.
    #
    # 0.6, not lower: PERSON carries a 0.6 confidence_threshold, and detect_unit
    # drops anything below it silently. At 0.55 this pattern matched, produced a
    # raw 0.55 PERSON result, and was then discarded before it became a finding
    # -- so it looked implemented while contributing exactly nothing.
    Pattern(name="initial_name", regex=rf"(?<=\b\p{{Lu}}\.\s{{0,2}}){_NAME}", score=0.60),
]

# Links, SCHEME- or www-anchored. This replaces Presidio's own UrlRecognizer,
# which is removed below: its non-scheme pattern matches any `word.tld`-ish run at
# score 0.5, and measured on a real workbook that produced not just noise
# ("v1.md" from "Prompt v1.md", "bank.de" from inside an email address) but BROKEN
# SPANS -- "d.ve" out of the middle of "d.velop". A partial span is worse than a
# false positive: applying it writes "[LINK_1]lop" into the output.
#
# Requiring a scheme or a www. prefix removes that whole class. The cost is a bare
# "metzler.com" with neither, which is indistinguishable from a filename
# ("Datei.xlsx") without a hostname list -- and the completeness scan carries a
# link pattern as a backstop for it.
#
# The trailing character class excludes sentence punctuation so a link at the end
# of a sentence does not swallow the full stop (Presidio's did: it matched
# "https://api.openai.com/v1/responses." including the period, and redacting that
# span deletes the sentence boundary from the output).
_URL_TAIL = r"[^\s<>\"'\]\)]*[^\s<>\"'\]\),.;:!?]"
_URL_PATTERNS = [
    Pattern(name="url_scheme", regex=rf"\bhttps?://{_URL_TAIL}", score=0.7),
    Pattern(name="url_www", regex=rf"\bwww\.{_URL_TAIL}", score=0.7),
]

# German system exports routinely transliterate umlauts (this repo's own fixtures
# say "Mueller", not "Müller"), and a word-list recognizer written with umlauts
# only silently misses every one of those spellings -- measured: 'arbeitsunfaehig',
# 'Arbeitsunfaehigkeit', 'erwerbsunfaehig', 'berufsunfaehig' and 'juedisch' all
# returned nothing while their umlaut spellings were caught. Hand-listing both
# spellings of every term does not converge, so a recognizer opts in with
# `umlaut_variants: true` and every umlaut in its patterns is expanded here.
_UMLAUT_VARIANTS = {
    "ä": "(?:ä|ae)", "ö": "(?:ö|oe)", "ü": "(?:ü|ue)", "ß": "(?:ß|ss)",
    "Ä": "(?:Ä|Ae)", "Ö": "(?:Ö|Oe)", "Ü": "(?:Ü|Ue)",
}


def _expand_umlauts(pattern: str) -> str:
    """Rewrites every umlaut in a regex as an (umlaut|transliteration) alternation.

    Refuses to touch a pattern with an umlaut inside a CHARACTER CLASS: splicing
    a group into `[a-zäöü]` would produce `[a-zä(?:ö|oe)ü]`, a regex that still
    compiles and silently matches the wrong thing -- a redaction tool must not
    have a way to fail quietly, so this fails loud at analyzer-build time instead.
    """
    out: list[str] = []
    in_class = False
    escaped = False
    for ch in pattern:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch == "[" and not in_class:
            in_class = True
        elif ch == "]" and in_class:
            in_class = False
        if ch in _UMLAUT_VARIANTS:
            if in_class:
                raise ValueError(
                    f"umlaut_variants cannot expand {ch!r} inside a character class: {pattern!r}. "
                    "Write the class with both spellings explicitly, or drop the flag."
                )
            out.append(_UMLAUT_VARIANTS[ch])
            continue
        out.append(ch)
    return "".join(out)


def build_analyzer(config: dict, *, gliner_backend=None) -> AnalyzerEngine:
    """Builds the Presidio analyzer (spaCy NLP engine + recognizers). Detection
    logic itself lives in `core`; language *selection* per document lives in
    `pipeline`/`language`. This just assembles an engine that can run either
    supported language on demand.

    `gliner_backend` lets a caller inject a ready GlinerBackend (tests pass a
    deterministic fake); when None and GLiNER is enabled, the real ONNX backend
    is loaded here and a load failure propagates -- the intended hard-fail."""
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

    # Custom recognizers: by DEFAULT registered for every supported language, so a
    # scan in any single language still catches the German bank identifiers (a
    # German ID can appear in an otherwise-English document).
    #
    # A recognizer may opt OUT of that with `languages: [de]`. Cross-registration is
    # right for STRUCTURAL patterns (an IBAN is an IBAN in any prose) but wrong for
    # NATURAL-LANGUAGE word lists: the German Art. 9 lists fired on ordinary English
    # financial vocabulary ("The Great Depression started in 1929" -> health data),
    # and an Art. 9 finding is ONE-WAY anonymized, so that irreversibly destroyed
    # text in an English document. The cost of the opt-out is the mirror case -- a
    # German Art. 9 term inside a document routed to English is not seen -- which is
    # the cheaper error: a missed German word in an English document still faces the
    # completeness scan, while a wrongly one-way-redacted English word is gone.
    for rec_cfg in config.get("custom_recognizers", []):
        regexes = [p["regex"] for p in rec_cfg["patterns"]]
        if rec_cfg.get("umlaut_variants"):
            regexes = [_expand_umlauts(r) for r in regexes]
        patterns = [
            Pattern(name=rec_cfg["name"], regex=regex, score=p["score"])
            for regex, p in zip(regexes, rec_cfg["patterns"])
        ]
        kwargs = {}
        if rec_cfg.get("case_sensitive"):
            kwargs["global_regex_flags"] = _CASE_SENSITIVE_FLAGS
        only = rec_cfg.get("languages")
        targets = [lang for lang in languages if not only or lang in only]
        for lang in targets:
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

    # Links. Presidio's UrlRecognizer is dropped in favour of the anchored patterns
    # above -- see _URL_PATTERNS for the measured broken-span problem it caused.
    # Removed per language so no loose variant survives in a multi-language config.
    for lang in languages:
        try:
            analyzer.registry.remove_recognizer("UrlRecognizer", language=lang)
        except Exception:  # noqa: BLE001 -- absent is the desired end state either way
            pass
        analyzer.registry.add_recognizer(
            PatternRecognizer(
                supported_entity="URL",
                patterns=_URL_PATTERNS,
                context=["link", "url", "confluence", "intranet", "sharepoint", "seite", "webseite"],
                supported_language=lang,
            )
        )

    # GLiNER second-pass recognizer (optional, config-gated). Registered per
    # language, but the backend is language-agnostic, so in the normal
    # single-language-narrowed scan it runs once over the full text and can catch
    # an English tool name inside a German document. A load failure propagates
    # (hard-fail); the message tells the user to disable ML detection in Settings.
    gliner_cfg = config.get("gliner") or {}
    if gliner_cfg.get("enabled"):
        from .gliner_recognizer import GlinerRecognizer, load_gliner_backend

        backend = gliner_backend if gliner_backend is not None else load_gliner_backend(gliner_cfg)
        label_map = gliner_cfg.get("labels") or {}
        for lang in languages:
            analyzer.registry.add_recognizer(
                GlinerRecognizer(
                    backend,
                    label_map,
                    supported_language=lang,
                    min_chars=int(gliner_cfg.get("min_chars", 3)),
                    min_score=float(gliner_cfg.get("min_score", 0.3)),
                )
            )
    return analyzer
