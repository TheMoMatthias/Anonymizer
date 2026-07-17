"""Measured recall, not asserted recall.

A redaction tool cannot be called "robust" on a feeling. This plants KNOWN names
and identifiers into realistic German bank text and reports how much the
pipeline actually finds -- broken down by the strata that decide the answer.

Why these strata: German NER does not fail at "hard" names, it fails at ORDINARY
ones. Nineteen of the twenty most common German surnames are everyday words
(Müller=miller, Weber=weaver, Bauer=farmer), and the model was trained on
Wikipedia prose, so it leans on sentence context that a form field or a table
cell simply does not have. Measuring one aggregate number would hide exactly
that: foreign surnames in prose score near-perfectly and would mask the
common-noun-in-a-cell case that actually leaks.

Two measurements, deliberately:
  * ISOLATED  - one occurrence, one context, nothing to propagate from. This is
                the pipeline's raw ability to see a name cold. Pessimistic.
  * DOCUMENT  - a realistic letter where the name recurs in several contexts, so
                anchors + document-wide propagation can do their job. This is
                what actually happens to a real file.

HONEST LIMITS -- report these numbers as an UPPER BOUND:
  * The names come from lists we chose, in documents we shaped. Real
    correspondence is messier (OCR noise, typos, nicknames, married names).
  * Recall here is measured against planted PII only; it says nothing about the
    PII we never thought to plant.
  * It is not a labelled sample of the bank's real mail, which is the only
    thing that would settle the question completely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .core import detect_unit
from .models import TextUnit

# --- the planted population --------------------------------------------------

# The measured failure mode: German surnames that are also ordinary nouns or
# adjectives. spaCy scores these far worse than exotic ones.
SURNAMES_COMMON_NOUN = [
    "Müller", "Schneider", "Weber", "Bauer", "Klein", "Schwarz", "Richter",
    "Koch", "Braun", "Wolf", "Jung", "Berg", "Fischer", "Vogel", "Hahn",
    "Stein", "Kaiser", "Fuchs", "Sommer", "Winkler",
]
# German surnames that are NOT everyday words -- the control group.
SURNAMES_GERMAN_RARE = [
    "Habermehl", "Bönnighausen", "Rüdenauer", "Osterkamp", "Kretschmar",
    "Wüstefeld", "Nagelschmidt", "Schwanitz",
]
# Non-German surnames, a large share of any German bank's customers.
SURNAMES_FOREIGN = [
    "Öztürk", "Yılmaz", "Nguyen", "Kowalczyk", "Rossi", "Ivanov", "Popescu",
    "Hussein", "Demir", "Petrov",
]

SURNAME_STRATA: dict[str, list[str]] = {
    "german_common_noun": SURNAMES_COMMON_NOUN,
    "german_rare": SURNAMES_GERMAN_RARE,
    "foreign": SURNAMES_FOREIGN,
}

GIVEN_NAMES = ["Björn", "Petra", "Thomas", "Ayşe", "Mehmet", "Anna", "Lukas", "Sofia"]

# Neutral bank prose with no names in it, so a document's only PII is what we
# planted (otherwise propagation could spread a filler name and flatter us).
FILLER = (
    "Die Abrechnung erfolgt quartalsweise gemäß den Allgemeinen Geschäftsbedingungen. "
    "Weitere Unterlagen finden Sie in der Anlage zu diesem Schreiben."
)


# --- the contexts a name appears in ------------------------------------------
# Each returns a line of text containing the surname exactly once.

def _salutation(given: str, surname: str) -> str:
    return f"Sehr geehrter Herr {surname},"


def _prose_full_name(given: str, surname: str) -> str:
    return f"{given} {surname} hat den Vertrag unterzeichnet."


def _prose_oblique(given: str, surname: str) -> str:
    return f"Die Unterlagen wurden von {surname} geprüft und freigegeben."


def _labelled_field(given: str, surname: str) -> str:
    return f"Kunde: {surname}"


def _bare(given: str, surname: str) -> str:
    return surname


def _signature(given: str, surname: str) -> str:
    return f"Mit freundlichen Grüßen {given} {surname}"


CONTEXTS = {
    "salutation": _salutation,
    "prose_full_name": _prose_full_name,
    "prose_oblique": _prose_oblique,
    "labelled_field": _labelled_field,
    "bare_cell": _bare,
    "signature": _signature,
}

# Structured identifiers: these have checksums or hard patterns, so they should
# score near 1.0. A dip here is a much louder alarm than a dip on names.
STRUCTURED_PROBES: dict[str, tuple[str, str]] = {
    "IBAN": ("DE89370400440532013000", "Bitte überweisen Sie auf IBAN {v} zugunsten des Kontos."),
    "STEUER_ID": ("86095742719", "Die Steuer-ID lautet {v} laut Bescheid."),
    "EMAIL": ("b.mueller@example.de", "Antworten Sie bitte an {v} zurück."),
    "PHONE_DE": ("0170 1234567", "Telefon: {v} für Rückfragen."),
    "ADDRESS": ("Königsallee 3", "Anschrift: {v} in der Akte."),
    "PLZ_CITY": ("50667 Köln", "Wohnort ist {v} laut Unterlagen."),
    "BIC": ("COBADEFFXXX", "Zahlung an BIC: {v} veranlassen."),
    "DATE_DOB": ("15.03.1980", "Geburtsdatum: {v} des Kunden."),
}


@dataclass
class StratumResult:
    stratum: str
    context: str
    found: int = 0
    total: int = 0
    missed: list[str] = field(default_factory=list)

    @property
    def recall(self) -> float:
        return self.found / self.total if self.total else 0.0


def _whole_token(needle: str, haystack: str) -> bool:
    """Whole-token match, NOT substring: a common-noun surname ("Berg", "Koch")
    must not count as found just because it is a substring of an unrelated finding
    ("Bergstraße", a "…berg" ORG) -- that over-reports recall for exactly the
    stratum this harness exists to measure honestly."""
    import re

    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None


def _found(findings, needle: str) -> bool:
    """A plant counts as found if ANY finding covers it (as a whole token). The
    entity TYPE is deliberately not checked: for redaction, catching "Bauer" as
    NER_MISC rather than PERSON still removes it -- the leak is what matters."""
    return any(_whole_token(needle, f.value) for f in findings)


def measure_isolated(analyzer, config: dict) -> list[StratumResult]:
    """One name, one context, no other occurrence to propagate from -- the
    pipeline's cold-read ability. Runs on text directly (no file I/O), which is
    valid here precisely because propagation has nothing to work with."""
    cfg = {**config, "languages": ["de"]}
    results: list[StratumResult] = []
    for stratum, surnames in SURNAME_STRATA.items():
        for ctx_name, builder in CONTEXTS.items():
            r = StratumResult(stratum=stratum, context=ctx_name)
            for i, surname in enumerate(surnames):
                given = GIVEN_NAMES[i % len(GIVEN_NAMES)]
                text = f"{FILLER} {builder(given, surname)}"
                findings = detect_unit(analyzer, TextUnit("u1", text), cfg)
                r.total += 1
                if _found(findings, surname):
                    r.found += 1
                else:
                    r.missed.append(surname)
            results.append(r)
    return results


def measure_structured(analyzer, config: dict) -> list[StratumResult]:
    cfg = {**config, "languages": ["de"]}
    results: list[StratumResult] = []
    for label, (value, template) in STRUCTURED_PROBES.items():
        r = StratumResult(stratum="structured", context=label, total=1)
        findings = detect_unit(analyzer, TextUnit("u1", template.format(v=value)), cfg)
        # Compare space-insensitively: a recognizer may claim a reformatted span.
        flat = {f.value.replace(" ", "") for f in findings}
        if any(value.replace(" ", "") in f for f in flat):
            r.found = 1
        else:
            r.missed.append(value)
        results.append(r)
    return results


def measure_documents(analyzer, config: dict, workdir: Path) -> list[StratumResult]:
    """A realistic letter: the name recurs across salutation, prose, a labelled
    field and a bare cell. Every occurrence must be caught -- this is where the
    anchors seed a name and propagation spreads it to the units NER cannot see.
    Scored per OCCURRENCE, not per document, so a partial catch cannot pass."""
    from docx import Document

    from .pipeline import scan_document

    results: list[StratumResult] = []
    for stratum, surnames in SURNAME_STRATA.items():
        r = StratumResult(stratum=stratum, context="full_letter_occurrences")
        for i, surname in enumerate(surnames):
            given = GIVEN_NAMES[i % len(GIVEN_NAMES)]
            doc = Document()
            doc.add_paragraph(_salutation(given, surname))
            doc.add_paragraph(FILLER)
            doc.add_paragraph(_prose_oblique(given, surname))
            doc.add_paragraph(_labelled_field(given, surname))
            table = doc.add_table(rows=1, cols=1)
            table.rows[0].cells[0].text = _bare(given, surname)
            doc.add_paragraph(_signature(given, surname))
            path = workdir / f"letter_{stratum}_{i}.docx"
            doc.save(path)

            planted = 5  # salutation, oblique, labelled, bare cell, signature
            result = scan_document(path, analyzer, config)
            caught = sum(
                g.count for g in result.all_actionable() if _whole_token(surname, g.value)
            )
            r.total += planted
            r.found += min(caught, planted)
            if caught < planted:
                r.missed.append(f"{surname}({caught}/{planted})")
        results.append(r)
    return results


def format_report(sections: dict[str, list[StratumResult]]) -> str:
    lines = ["", "=" * 74, "RECALL REPORT  (planted PII -- treat as an UPPER BOUND)", "=" * 74]
    for title, results in sections.items():
        lines.append("")
        lines.append(f"--- {title} ---")
        lines.append(f"{'stratum':<22}{'context':<26}{'recall':>9}  {'n':>5}")
        for r in results:
            flag = "" if r.recall >= 0.9 else ("  <-- WEAK" if r.recall >= 0.5 else "  <-- LEAKING")
            lines.append(f"{r.stratum:<22}{r.context:<26}{r.recall:>8.0%}  {r.total:>5}{flag}")
        total = sum(r.total for r in results)
        found = sum(r.found for r in results)
        lines.append(f"{'':<22}{'OVERALL':<26}{(found / total if total else 0):>8.0%}  {total:>5}")
    misses = [
        f"  {r.stratum}/{r.context}: {', '.join(r.missed)}"
        for results in sections.values()
        for r in results
        if r.missed
    ]
    if misses:
        lines += ["", "MISSED (what still leaks):"] + misses
    lines.append("")
    return "\n".join(lines)
