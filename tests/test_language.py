import pytest

from anonymizer.core import detect_unit
from anonymizer.language import detect_dominant
from anonymizer.models import TextUnit
from anonymizer.pipeline import _language_sample, _narrow_language


def test_detects_german():
    lang, confident = detect_dominant(
        "Sehr geehrte Damen und Herren, in der Anwendung koennen Sie antworten und Anfragen bearbeiten."
    )
    assert lang == "de" and confident is True


def test_detects_english():
    lang, confident = detect_dominant(
        "Dear Sir or Madam, please find the attached report for your review and approval by the team."
    )
    assert lang == "en" and confident is True


def test_low_signal_is_unconfident():
    # A bare name has no language signal -> caller should ask the user.
    _lang, confident = detect_dominant("Hans Mueller")
    assert confident is False


def test_german_common_words_not_flagged_as_people(analyzer, base_config):
    """Regression: the English NER pass used to label ordinary German words
    (Anwendung, antworten, Sie) as PERSON. With single-language detection those
    must not appear as entities."""
    cfg = dict(base_config)
    cfg["languages"] = ["de"]
    text = "In der Anwendung koennen Sie antworten und Anfragen bearbeiten. Die Vereinbarungen gelten."
    findings = detect_unit(analyzer, TextUnit("u1", text), cfg)
    flagged = {f.value.lower() for f in findings}
    for noise in ("anwendung", "antworten", "sie", "anfragen", "vereinbarungen"):
        assert noise not in flagged
    assert not any(f.entity_type == "PERSON" for f in findings)


def test_umlaut_names_in_english_are_not_confidently_german():
    """Regression: umlaut CHARACTERS alone used to flip English prose to a
    confident German verdict (de + umlauts*2), which then ran the German NER
    model over English text -- the original over-flagging bug in reverse. A few
    umlaut names must not do that; the doc should fall through to ask-the-user."""
    _lang, confident = detect_dominant("Account holder: Björn Müller, Düsseldorf")
    assert confident is False


def test_umlaut_names_do_not_beat_real_english_function_words():
    lang, confident = detect_dominant(
        "Please transfer to Björn Müller in Düsseldorf for the account review today"
    )
    assert lang == "en" and confident is True


def test_short_english_text_routes_confidently():
    """Regression (LEAK): a short English text with only 2-3 function words fell
    below the fixed floor of 4, so _narrow_language silently routed it to the
    German model and missed its English names. Short docs use a lower floor."""
    lang, confident = detect_dominant("Please transfer 500 to John Smith. Regards, Jane.")
    assert lang == "en" and confident is True, (lang, confident)


def test_zero_signal_short_text_stays_unconfident():
    """Names + numbers with no function words: still ambiguous -> ask the user,
    not a confident (and possibly wrong) guess."""
    _lang, confident = detect_dominant("Invoice 4471 John Carpenter")
    assert confident is False


def test_umlaut_names_plus_one_ambiguous_word_not_confident_german():
    """Regression: the lower short-doc floor let ONE _DE/_EN-ambiguous marker (the
    list used to include 'hat'/'die'/'den') plus umlaut proper nouns reach confident
    German, mis-routing an English sentence to the German NER model (a leak)."""
    lang, confident = detect_dominant("Björn wore a hat in Düsseldorf.")
    assert not (lang == "de" and confident), (lang, confident)


def test_language_sample_is_not_biased_to_the_head():
    """Regression (the reported over-flagging): a German spreadsheet was
    mis-detected as English because only the first units -- the header row and
    English-ish field-name cells -- were sampled. The sample must span the whole
    document so the German prose body dominates, as it does in the real file."""
    # Mimic the real shape: English-ish field-name/description cells first (with
    # real English function words, as a spreadsheet's header/first rows have),
    # then a far larger German prose body.
    head = [
        TextUnit(f"h{i}", t) for i, t in enumerate(
            ["The project status and the value of this", "for the owner to be updated by the team"]
        )
    ]
    body = [
        TextUnit(f"b{i}", "Der Prozess wurde abgeschlossen und die Kosten sind mit dem Team abgestimmt worden.")
        for i in range(300)
    ]
    units = head + body

    # Old behaviour (first units only) saw English; the whole-doc sample must see German.
    assert detect_dominant(" ".join(u.text for u in units[:2]))[0] == "en"
    lang, confident = detect_dominant(_language_sample(units))
    assert lang == "de" and confident, (lang, confident)

    narrowed = _narrow_language({"languages": ["de", "en"]}, units)
    assert narrowed["languages"] == ["de"]


# --- GDPR Art. 9 parity between German and English ---------------------------
#
# MEASURED GAP (2026-07-26 audit): the German Art. 9 recognizers are
# `languages: [de]` -- correctly, their word lists false-fired on ordinary English
# ("The Great Depression" -> health data) and the action is ONE-WAY. But no
# English equivalents existed, so on an English document FIVE of six Art. 9
# categories had ZERO detection: health, union/party, sexual orientation and
# ethnic origin were invisible; only religion was caught, incidentally, by spaCy's
# NRP. For a bank handling English documents that is the legally most-protected
# data with the least coverage. These tests pin both directions of the fix.

from anonymizer import taxonomy as _taxonomy


def _art9(analyzer, base_config, text, lang):
    cfg = {**base_config, "languages": [lang]}
    return [
        f
        for f in detect_unit(analyzer, TextUnit("u1", text), cfg)
        if _taxonomy.data_class_for(f.entity_type).key == "special_category"
    ]


@pytest.mark.parametrize(
    "text",
    [
        "Diagnosis: type 2 diabetes, insulin dependent",
        "The employee was diagnosed with multiple sclerosis last year.",
        "Medical condition: chronic back pain",
        "The patient is HIV positive.",
        "Religion: Roman Catholic",
        "The applicant is Muslim and requests prayer facilities.",
        "Trade union: Unite the Union",
        "Union membership: UNISON since 2019",
        "Political party: Labour Party",
        "Sexual orientation: homosexual",
        "The client is transgender.",
        "Nationality: Turkish",
        "Ethnic origin: Kurdish",
        "Country of birth: Syria",
    ],
)
def test_english_art9_is_detected(analyzer, base_config, text):
    """Every one of these was invisible on an English document before the audit."""
    assert _art9(analyzer, base_config, text, "en"), f"Art.9 leak in English: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        # Ordinary banking English that LOOKS like Art.9 vocabulary. A hit here is
        # ONE-WAY, so it destroys legitimate text with no way to recover it.
        "The Great Depression started in 1929 and reshaped banking.",
        "Credit Union: Nationwide Building Society",
        "The bank has catholic tastes in counterparties.",
        "An orthodox approach to risk management was applied.",
        "The union of the two datasets produced 4,000 rows.",
        "Race conditions in the settlement engine were fixed.",
        "European Union regulations apply to this transaction.",
        "Party: Deutsche Bank AG acting as counterparty",
        # Explicitly-empty values must never be claimed as a disclosure.
        "Diagnosis: none",
        "Religion: not stated",
        "Nationality: n/a",
    ],
)
def test_english_art9_does_not_fire_on_ordinary_banking_prose(analyzer, base_config, text):
    hits = _art9(analyzer, base_config, text, "en")
    assert not hits, f"ONE-WAY false positive would destroy legitimate text: {text!r} -> {hits}"


def test_art9_token_is_not_german_prefixed_in_an_english_document():
    """The DE_ prefix on these entity types is an internal identifier, not a
    language claim -- the English recognizers emit the same types. Redacting an
    English contract to "[DE_HEALTH_DATA]" reads as a bug to whoever receives it."""
    from anonymizer.actions import token_label

    for et in ("DE_HEALTH_DATA", "DE_RELIGION", "DE_UNION_PARTY", "DE_SEX_LIFE"):
        assert not token_label(et).startswith("DE_"), et
