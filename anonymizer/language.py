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
_DE = {
    "der", "die", "das", "und", "sie", "nicht", "ein", "eine", "einen", "mit", "von", "für", "ist", "den",
    "dem", "des", "im", "auf", "auch", "werden", "wird", "wurde", "sich", "bei", "aus", "zum", "zur", "oder",
    "sind", "haben", "hat", "wir", "uns", "ihre", "ihren", "sehr", "geehrte", "damen", "herren", "bitte",
}
_EN = {
    "the", "and", "of", "to", "in", "is", "for", "with", "that", "this", "are", "be", "on", "as", "by",
    "was", "were", "from", "have", "has", "you", "your", "our", "please", "dear", "regards", "kind", "an",
}

_WORD_RE = re.compile(r"[a-zäöüß]+", re.IGNORECASE)
_UMLAUT_RE = re.compile(r"[äöüß]", re.IGNORECASE)

# Minimum signal (matched marker words) and margin to be "confident". Below
# this the caller should ask the user rather than guess.
_MIN_SIGNAL = 4
_MIN_MARGIN = 1.5


def detect_dominant(text: str) -> tuple[str, bool]:
    """Returns (language, confident). language is 'de' or 'en'. When the signal
    is weak or the two languages are close, confident is False so the UI can
    ask the user instead of guessing."""
    words = [w.lower() for w in _WORD_RE.findall(text)]
    de = sum(1 for w in words if w in _DE)
    en = sum(1 for w in words if w in _EN)
    # Umlaut-bearing WORDS are a German hint, but only a secondary one: a couple
    # of umlaut *names* in English prose ("Björn Müller, Düsseldorf") must not
    # flip the whole document to German. So we count words-with-an-umlaut (not
    # raw umlaut characters) and CAP their contribution below _MIN_SIGNAL -- this
    # guarantees umlauts alone can never reach "confident", so such a document
    # falls through to the ask-the-user path instead of being mis-routed.
    umlaut_words = sum(1 for w in words if _UMLAUT_RE.search(w))
    de_score = de + min(umlaut_words, _MIN_SIGNAL - 1)

    if de_score == 0 and en == 0:
        return "de", False  # no signal -> default German, but unconfident
    if de_score >= en:
        leader, lead_score, other = "de", de_score, en
    else:
        leader, lead_score, other = "en", en, de_score
    confident = lead_score >= _MIN_SIGNAL and lead_score >= max(1, other) * _MIN_MARGIN
    return leader, confident
