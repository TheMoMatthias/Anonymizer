"""Lightweight German-vs-English detection, dependency-free.

Running both spaCy models over every document was the root cause of the
over-flagging: the English NER model, run over German text, labels ordinary
German words ("Anwendung", "antworten") as people/orgs. So we detect the
document's dominant language and run only that model. The heuristic below
(common-word counts + German-specific characters) is deliberately simple and
maintainable -- it only has to separate German from English, not identify
arbitrary languages.
"""

from __future__ import annotations

import re

# Very common function words, near-exclusive to each language. Kept short on
# purpose -- more words add noise, not accuracy, for a de/en split.
# NB: German markers that are ALSO ordinary English words ("hat", "die", "den") are
# deliberately EXCLUDED -- as disambiguators they are worthless, and with the lower
# short-doc floor one such coincidental hit plus an umlaut proper noun ("Björn ...
# Düsseldorf") was enough to mis-flag an English sentence as confidently German
# (running the German NER over English text -> missed English names, a leak).
_DE = {
    "der", "das", "und", "sie", "nicht", "ein", "eine", "einen", "mit", "von", "für", "ist",
    "dem", "des", "im", "auf", "auch", "werden", "wird", "wurde", "sich", "bei", "aus", "zum", "zur", "oder",
    "sind", "haben", "wir", "uns", "ihre", "ihren", "sehr", "geehrte", "damen", "herren", "bitte",
}
_EN = {
    "the", "and", "of", "to", "in", "is", "for", "with", "that", "this", "are", "be", "on", "as", "by",
    "was", "were", "from", "have", "has", "you", "your", "our", "please", "dear", "regards", "kind", "an",
}

_WORD_RE = re.compile(r"[a-zäöüß]+", re.IGNORECASE)
_UMLAUT_RE = re.compile(r"[äöüß]", re.IGNORECASE)

# Minimum signal (matched marker words) and margin to be "confident". Below
# this the caller should ask the user rather than guess. Short documents (a memo,
# an invoice line, a salutation) rarely accumulate 4 marker words even when the
# language is obvious, so they used a lower floor -- otherwise a clearly-English
# short text stayed unconfident and _narrow_language silently routed it to the
# GERMAN model, missing its English names (a leak). Zero-signal text (names +
# numbers only, no function words) still returns unconfident so the UI asks.
# Short docs use a lower floor so a clearly-English short text (2-3 markers) isn't
# left unconfident and silently routed to the German model. The umlaut-name false
# positive is closed by excluding the "hat"/"die"/"den" collisions above + the
# umlaut cap tracking min_signal -- NOT by raising this floor (floor 3 wrongly
# flips short English with exactly 2 markers to unconfident -> German default).
_MIN_SIGNAL = 4
_MIN_SIGNAL_SHORT = 2
_SHORT_DOC_WORDS = 25
_MIN_MARGIN = 1.5


def detect_dominant(text: str) -> tuple[str, bool]:
    """Returns (language, confident). language is 'de' or 'en'. When the signal
    is weak or the two languages are close, confident is False so the UI can
    ask the user instead of guessing."""
    words = [w.lower() for w in _WORD_RE.findall(text)]
    de = sum(1 for w in words if w in _DE)
    en = sum(1 for w in words if w in _EN)
    # Short docs use a lower confidence floor (few function words even when the
    # language is obvious); long docs the standard floor.
    min_signal = _MIN_SIGNAL_SHORT if len(words) <= _SHORT_DOC_WORDS else _MIN_SIGNAL
    # Umlaut-bearing WORDS are a German hint, but only a secondary one: a couple of
    # umlaut *names* in English prose ("Björn Müller, Düsseldorf") must not flip the
    # whole document to German. Count words-with-an-umlaut (not raw umlaut chars) and
    # CAP their contribution BELOW the effective floor, so umlauts alone can never
    # reach "confident" -- such a doc falls through to ask-the-user instead of being
    # mis-routed. (The cap tracks min_signal, else the lower short-doc floor would
    # let a few umlaut names alone read as confidently German.)
    umlaut_words = sum(1 for w in words if _UMLAUT_RE.search(w))
    de_score = de + min(umlaut_words, max(0, min_signal - 1))

    if de_score == 0 and en == 0:
        return "de", False  # no signal -> default German, but unconfident
    if de_score >= en:
        leader, lead_score, other = "de", de_score, en
    else:
        leader, lead_score, other = "en", en, de_score
    confident = lead_score >= min_signal and lead_score >= max(1, other) * _MIN_MARGIN
    return leader, confident
