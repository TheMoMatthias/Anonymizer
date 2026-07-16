from anonymizer.core import detect_unit
from anonymizer.language import detect_dominant
from anonymizer.models import TextUnit


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
