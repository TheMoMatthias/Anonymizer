from __future__ import annotations

import functools
import re
from dataclasses import replace
from pathlib import Path

import openpyxl
from openpyxl.utils import column_index_from_string, get_column_letter

from .. import taxonomy
from ..actions import decisions_lookup, resolve_replacement, token_label
from ..core import _resolve_overlaps, detect_unit, neutralize_structural_noise, precompute_nlp_artifacts
from ..engine import DEFAULT_LANGUAGES
from ..models import CellInfo, ColumnInfo, Finding, TextUnit

EXTENSIONS = (".xlsx", ".xlsm", ".xls")

# A column header that declares its cells are people. This is the one place a
# bare surname legitimately appears with no prose around it, and it is exactly
# where NER collapses: measured, de_core_news_lg finds only ~35% of ordinary
# German surnames (Müller, Weber, Bauer) in a bare cell, because its training
# gives it nothing to lean on without a sentence.
#
# The header is stronger evidence than any model: the spreadsheet's own author
# labelled the column. Note this does NOT work via Presidio's context boost --
# that only lifts PATTERN recognizers, and spaCy NER gets no boost from it at
# all, which is why "Kunde: Müller" in a cell still missed.
# Built-in column-header stems that declare a column holds PEOPLE. Matched
# case-insensitively as a SUBSTRING, so "leiter" covers Projekt-/Team-/Abteilungs-
# leiter and "berechtigt" covers zeichnungs-/bevollmächtigt forms. The shipped set
# was too narrow for a real "database" workbook (it missed Projektleiter, Betreuer,
# Verantwortlich, ...), so a name column with such a header leaked ~65% via NER
# alone. Extend per workbook via config["name_column_headers"] (Settings > Detection).
_NAME_HEADER_TERMS = (
    "name", "kunde", "kundin", "inhaber", "empfänger", "empfaenger",
    "sachbearbeiter", "ansprechpartner", "berater", "beraterin", "mitarbeiter",
    "antragsteller", "vertragspartner", "begünstigter", "beguenstigter",
    # widened: common German business / bank name-column headers.
    "projektleiter", "leiter", "betreuer", "verantwortlich", "referent",
    "gesellschafter", "geschäftsführer", "geschaeftsfuehrer", "prokurist",
    "zeichnungsberechtigt", "bevollmächtigt", "bevollmaechtigt", "berechtigt",
    "unterzeichner", "auftraggeber", "eigentümer", "eigentuemer",
    "vorname", "nachname", "familienname", "teilnehmer", "kontaktperson",
)


@functools.lru_cache(maxsize=8)
def _name_header_re(extra_terms: tuple[str, ...] = ()):
    """Compiled people-column-header matcher for the built-in stems plus any
    workbook-specific extras. lru_cached on the extra-terms tuple so the per-cell
    hot path never recompiles."""
    terms = _NAME_HEADER_TERMS + tuple(t.strip().lower() for t in extra_terms if t.strip())
    return re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)
# Cell contents that a name column can still hold but which are not names.
_NOT_A_NAME = re.compile(r"^[\W\d_]*$|^(unbekannt|n/?a|keine?|leer|-{1,3}|divers)$", re.IGNORECASE)
_NAME_COLUMN_SCORE = 0.8

# --- topical (non-personal) category detection --------------------------------
# Header-confirmed, so scored into the auto-accept tier; source-tagged so it is
# never mistaken for a bare NER guess (bypasses corroboration-only / noise
# filters, which only gate NER entity types).
_TOPICAL_SCORE = 0.9
# A gazetteer term must be name-shaped (short), not a whole prose description --
# long PROJECT-description cells are handled whole-cell, not propagated.
_MAX_GAZETTEER_LEN = 40


def _topical_categories(config: dict) -> dict:
    """{CATEGORY: {header_terms, terms}} from config, or {} when topical
    detection is disabled/absent."""
    t = config.get("topical") or {}
    if not t.get("enabled", True):
        return {}
    return t.get("categories") or {}


@functools.lru_cache(maxsize=8)
def _topical_header_res(categories_key: tuple):
    """Per-category compiled header matchers. lru_cached on a hashable
    ((CATEGORY, (term, ...)), ...) key so the per-cell hot path never recompiles.

    WORD-BOUNDARY matching (not bare substring, which the people-column matcher
    uses): a category column drives WHOLE-COLUMN redaction, so a false header
    match is high-impact. Substring matched 'gruppe' inside 'Produktgruppe'
    (a product group, not a department); \\b requires the term to stand as a word,
    so 'Gruppe'/'Team'/'Abteilung' match but 'Produktgruppe'/'Anwendungsfall' do
    not. German compounds where the category word is a suffix (Fachabteilung) are
    intentionally NOT matched -- add such headers explicitly if needed (favouring
    precision, since a wrong category column redacts every cell in it)."""
    out = {}
    for cat, terms in categories_key:
        cleaned = [t.strip().lower() for t in terms if t and t.strip()]
        if cleaned:
            out[cat] = re.compile(r"\b(?:" + "|".join(re.escape(t) for t in cleaned) + r")\b", re.IGNORECASE)
    return out


def _category_for_header(header: str | None, config: dict) -> str | None:
    """The topical category a column header declares, or None. First match wins
    in config order (deterministic)."""
    if not header:
        return None
    cats = _topical_categories(config)
    key = tuple((cat, tuple(spec.get("header_terms", []))) for cat, spec in cats.items())
    for cat, rx in _topical_header_res(key).items():
        if rx.search(header):
            return cat
    return None


def topical_gazetteer(path: Path, config: dict) -> list[tuple[str, str]]:
    """Auto-learn topical terms from the document's own structure: every
    name-shaped value in a column whose header maps to a category becomes a
    (category, value) the caller propagates document-wide. Derived identically
    at scan and apply (both call this), so scan/apply parity holds. Long prose
    (PROJECT descriptions) is excluded -- it is handled whole-cell, not
    propagated."""
    cats = _topical_categories(config)
    if not cats:
        return []
    wb = openpyxl.load_workbook(path, data_only=False, read_only=True)
    pairs: set[tuple[str, str]] = set()
    try:
        for ws in wb.worksheets:
            col_cat: dict[str, str] = {}
            for cell in next(ws.iter_rows(min_row=1, max_row=1), []):
                if isinstance(cell.value, str) and cell.value.strip():
                    cat = _category_for_header(cell.value.strip(), config)
                    if cat:
                        col_cat[get_column_letter(cell.column)] = cat
            if not col_cat:
                continue
            # Only NAME categories seed the gazetteer; DESCRIPTION (free text) is
            # whole-cell summarized, never propagated.
            col_cat = {col: cat for col, cat in col_cat.items() if cat in taxonomy.PROPAGATING_TOPICAL_TYPES}
            if not col_cat:
                continue
            for row in ws.iter_rows(min_row=2):
                for cell in row:
                    if cell.value in (None, ""):
                        continue  # read_only fills gaps with EMPTY_CELL (no .column)
                    cat = col_cat.get(get_column_letter(cell.column))
                    if not cat:
                        continue
                    v = str(cell.value).strip()
                    if 2 <= len(v) <= _MAX_GAZETTEER_LEN and any(ch.isalpha() for ch in v):
                        pairs.add((cat, v))
    finally:
        wb.close()
    return sorted(pairs)

# German name particles that don't themselves need to be capitalized ("Klaus
# von Bergen", "Anna de Wit").
_NAME_PARTICLES = {"von", "van", "de", "der", "zu", "zur", "zum"}
# Stripped before the sentence-punctuation check only (kept in the value
# itself for the word/capitalization check below) -- an honorific's own
# period ("Dr. Klaus Müller") is not a sentence boundary. Kept in sync with
# core._HONORIFIC_PREFIX / engine._HONORIFICS.
_HONORIFIC_PREFIX = re.compile(r"^(?:Herrn?|Frau|Hr\.|Fr\.|Dr\.|Prof\.)\s+")
_SENTENCE_PUNCT = re.compile(r"[.!?]")
_MAX_NAME_CELL_LEN = 40


# A determiner/pronoun/verb/conjunction anywhere in an otherwise name-shaped
# value is strong evidence it's an ordinary phrase, not a name -- "Alle
# Zielwerte" ("All target values") and "Kein Ergebnis" ("No result") are both
# capitalized 2-word phrases that pass the shape check above, but neither is
# a name. Measured with the actual tagger: name particles ("von"/"de") tag as
# PROPN in a real name's context, so they're exempted rather than relied on to
# tag correctly under every phrasing.
_NON_NAME_POS = frozenset({"VERB", "AUX", "DET", "ADP", "CCONJ", "SCONJ", "PRON", "ADV", "NUM", "PUNCT", "SYM", "INTJ"})


def _looks_like_name(value: str, analyzer=None, lang: str | None = None) -> bool:
    """Gate for the whole-cell PERSON override below: a header substring match
    (_NAME_HEADER_TERMS is deliberately broad -- see its comment) is only
    evidence the COLUMN is about people, not that every cell in it is a bare
    name. Without this, a free-text prose column that happens to sit under a
    header containing e.g. "verantwortlich" gets every paragraph forcibly
    claimed as a person. A cell must be SHAPED like a name -- short, 1-4
    capitalized words, no sentence-ending punctuation -- AND (when an
    analyzer is available) contain no determiner/verb/conjunction/etc."""
    if len(value) >= _MAX_NAME_CELL_LEN or _SENTENCE_PUNCT.search(_HONORIFIC_PREFIX.sub("", value)):
        return False
    if "_" in value:
        return False  # snake_case field/status identifier ("Aktueller_Status"), not a name
    words = value.split()
    if not words or len(words) > 4:
        return False
    if not all(w.lower() in _NAME_PARTICLES or w[:1].isupper() for w in words):
        return False
    if analyzer is not None and lang is not None:
        try:
            doc = analyzer.nlp_engine.process_text(value, lang).tokens
        except Exception:  # noqa: BLE001 -- best-effort refinement; shape check alone already passed
            return True
        # Iterate spaCy's own tokens (not the whitespace-split `words` above --
        # a hyphenated name can tokenize differently) so a particle exemption
        # never depends on the two sequences lining up index-for-index.
        if doc is not None:
            for tok in doc:
                if tok.text.lower() in _NAME_PARTICLES:
                    continue
                if tok.pos_ in _NON_NAME_POS:
                    return False
    return True


def _column_headers(ws) -> dict[int, str]:
    headers = {}
    first_row = next(ws.iter_rows(min_row=1, max_row=1), [])
    for cell in first_row:
        if isinstance(cell.value, str) and cell.value.strip():
            headers[cell.column] = cell.value.strip()
    return headers


def _cell_scan_text(cell) -> str | None:
    """The text to scan for a cell, or None to skip. String cells pass through;
    NUMBERS are coerced to a plain string so account / tax / customer / phone
    numbers STORED AS NUMBERS (very common in bank spreadsheets) are not
    invisible to detection -- previously only string cells were scanned, so a
    numeric account number sailed through into a "verified" file. Short numbers
    (< 5 digits: counts, small amounts) and non-integer decimals (monetary
    amounts) are skipped as not-identifiers; dates/booleans/formulas/errors are
    left to structure."""
    v = cell.value
    if v is None:
        return None
    if cell.data_type == "s" and isinstance(v, str):
        return v if v.strip() else None
    if cell.data_type == "n" and isinstance(v, (int, float)) and not isinstance(v, bool):
        if isinstance(v, float):
            if not v.is_integer():
                return None
            v = int(v)
        s = str(v)
        return s if len(s) >= 5 else None
    return None


def _iter_cell_units(wb):
    """Yields (id, text, header) -- header is the column-1-row text for that
    column, given as context so recognizers relying on nearby German context
    words (Kontonummer, Depotnummer, ...) actually have something to match,
    since a bare cell value alone carries no context.

    Row 1 itself is used ONLY as the header/schema label (via _column_headers)
    -- never scanned as its own data unit. A schema label ("NewValue",
    "Project_ID") is a structural name, not user data, and its usual
    CamelCase/underscore-joined shape reads as a proper noun to NER (measured:
    both get tagged PROPN, same as a real name), producing a finding that is
    really just the column's own name. Row-1 comments are still scanned --
    an actual annotation, unlike the label itself."""
    for ws in wb.worksheets:
        headers = _column_headers(ws)
        for row in ws.iter_rows():
            for cell in row:
                header = headers.get(cell.column) if cell.row != 1 else None
                if cell.row != 1:
                    text = _cell_scan_text(cell)
                    if text is not None:
                        yield f"cell|{ws.title}|{cell.coordinate}", text, header
                if cell.comment is not None and cell.comment.text.strip():
                    yield f"comment|{ws.title}|{cell.coordinate}", cell.comment.text, header


def _iter_defined_name_units(wb):
    for name, defn in wb.defined_names.items():
        if isinstance(defn.value, str) and defn.value.strip():
            yield f"defined_name|{name}", defn.value


def extract_text_units(path: Path) -> list[TextUnit]:
    wb = openpyxl.load_workbook(path, data_only=False)
    units = [TextUnit(id=key, text=text) for key, text, _header in _iter_cell_units(wb)]
    units.extend(TextUnit(id=key, text=text) for key, text in _iter_defined_name_units(wb))
    return units


# --- column-level policy (redact/pseudonymize a whole column) -----------------
# A column policy blacks out EVERY non-empty cell in a column regardless of what
# detection found -- the only way to redact a column whose sensitivity is topical
# (a confidential project description) rather than an identifiable entity. Actions
# supported at the column level: "pseudonymize" (consistent per-column token) and
# "anonymize" (one-way). "skip" is intentionally NOT here: the value-keyed decision
# model can't express "keep this value here but remove it elsewhere", so a skipped
# column that shares a value with a removed one would trip the fail-loud verify.
_COLUMN_BLACKOUT_ACTIONS = ("pseudonymize", "anonymize", "summarize")


def _column_entity_type(header: str, col_letter: str) -> str:
    """Per-column entity type for pseudonym tokens, derived from the header so a
    pseudonymized 'Projekt' column renders readable, re-identifiable [PROJEKT_n]
    tokens. Falls back to the column letter when there is no header."""
    base = re.sub(r"[^0-9A-Za-zÄÖÜäöüß]+", "_", (header or "").strip()).strip("_").upper()
    return base or f"COLUMN_{col_letter}"


def _coord_column(coord: str) -> str | None:
    m = re.match(r"([A-Z]+)", coord)
    return m.group(1) if m else None


def cell_summary(findings: list) -> list[CellInfo]:
    """Every spreadsheet cell that carries a finding, so the reviewer can set a
    per-cell policy (the exception layer). Derived from the findings' unit_ids
    (`cell|Sheet|A5`) -- no workbook re-read -- with a short content preview and
    the detected entity types. Ordered by sheet then coordinate."""
    by_cell: dict[tuple[str, str], dict] = {}
    for f in findings:
        parts = f.unit_id.split("|")
        if len(parts) == 3 and parts[0] in ("cell", "comment"):
            sheet, coord = parts[1], parts[2]
            info = by_cell.setdefault((sheet, coord), {"types": set(), "sample": (f.context or f.value)})
            info["types"].add(f.entity_type)
    out = [
        CellInfo(sheet=s, coord=c, header="", sample=d["sample"], entity_types=tuple(sorted(d["types"])))
        for (s, c), d in by_cell.items()
    ]
    out.sort(key=lambda ci: (ci.sheet, column_index_from_string(_coord_column(ci.coord) or "A"), ci.coord))
    return out


def column_summary(path: Path, findings: list, config: dict | None = None) -> list[ColumnInfo]:
    """Describe each spreadsheet column (sheet, letter, header, a sample value,
    and how many findings landed in it) so the reviewer can set a whole-column
    policy. Only columns that carry a header OR at least one finding are listed --
    empty structural columns are noise."""
    header_re = _name_header_re(tuple((config or {}).get("name_column_headers", ())))
    counts: dict[tuple[str, str], int] = {}
    for f in findings:
        parts = f.unit_id.split("|")
        if len(parts) == 3 and parts[0] in ("cell", "comment"):
            col = _coord_column(parts[2])
            if col:
                counts[(parts[1], col)] = counts.get((parts[1], col), 0) + 1

    wb = openpyxl.load_workbook(path, data_only=False, read_only=True)
    out: list[ColumnInfo] = []
    try:
        for ws in wb.worksheets:
            headers: dict[str, str] = {}
            for cell in next(ws.iter_rows(min_row=1, max_row=1), []):
                if isinstance(cell.value, str) and cell.value.strip():
                    headers[get_column_letter(cell.column)] = cell.value.strip()
            wanted = set(headers) | {col for (sheet, col) in counts if sheet == ws.title}
            samples: dict[str, str] = {}
            for i, row in enumerate(ws.iter_rows(min_row=2)):
                if i >= 200 or len(samples) >= len(wanted):  # sample from the first rows only
                    break
                for cell in row:
                    if cell.value in (None, ""):
                        continue  # read_only fills row gaps with EMPTY_CELL, which has no .column
                    col = get_column_letter(cell.column)
                    if col in wanted and col not in samples:
                        samples[col] = str(cell.value)
            for col in sorted(wanted, key=column_index_from_string):
                header = headers.get(col, "")
                out.append(
                    ColumnInfo(
                        sheet=ws.title,
                        column=col,
                        header=header,
                        sample=samples.get(col, ""),
                        pii_count=counts.get((ws.title, col), 0),
                        name_override=bool(header_re.search(header)),
                    )
                )
    finally:
        wb.close()
    return out


def _combined_cell_text(text: str, header: str | None) -> str:
    prefix = f"{header}: " if header else ""
    return prefix + text


def _precompute_cell_artifacts(wb, analyzer, config) -> dict[tuple[str | None, str], object]:
    """One spaCy pipe() batch over every DISTINCT (header, cell-text) combo in
    the workbook, instead of one analyze() call per cell -- measured ~5x faster
    for the many short, highly repetitive values a spreadsheet holds. Returns
    nlp_artifacts keyed by (header, text) so scan()/apply() can feed them
    straight into their existing per-cell cache. Empty (falls back to the
    per-call path) under a multi-language config, since a cached artifact is
    tied to one language."""
    languages = config.get("languages") or list(DEFAULT_LANGUAGES)
    if len(languages) != 1:
        return {}
    combined_by_key: dict[tuple[str | None, str], str] = {}
    for _, text, header in _iter_cell_units(wb):
        combined_by_key.setdefault((header, text), _combined_cell_text(text, header))
    for _, text in _iter_defined_name_units(wb):
        combined_by_key.setdefault((None, text), text)
    # Batch on the SAME cleaned text detect_unit will request via its own
    # neutralize_structural_noise call, so these precomputed artifacts (built
    # from a bullet/heading-fusion-neutralized copy) match what detect_unit
    # actually analyzes -- batching on the raw text here would precompute
    # tokenization for a string detect_unit never uses, silently discarding
    # the whole point of the cleanup.
    artifacts_by_clean = precompute_nlp_artifacts(
        analyzer, (neutralize_structural_noise(c) for c in combined_by_key.values()), languages[0]
    )
    return {key: artifacts_by_clean.get(neutralize_structural_noise(combined)) for key, combined in combined_by_key.items()}


def _analyze_cell_text(text: str, header: str | None, analyzer, config, unit_id: str = "tmp", nlp_artifacts=None) -> list:
    combined = _combined_cell_text(text, header)
    unit = TextUnit(id=unit_id, text=combined)
    findings = detect_unit(analyzer, unit, config, nlp_artifacts=nlp_artifacts)
    offset = len(combined) - len(text)
    result = []
    for f in findings:
        if f.end <= offset:
            continue  # entirely inside the header context -- not the cell value
        if f.start < offset:
            # Span STRADDLES the header/value boundary: clip to the value side and
            # re-slice its value, rather than dropping it wholesale (which leaked the
            # in-value portion -- e.g. a deny term that included the header text).
            f.start = offset
            f.value = combined[f.start : f.end]
        f.start -= offset
        f.end -= offset
        result.append(f)

    # The column header declares this cell is a person. Trust it over the model,
    # but only where the whole cell isn't already claimed end-to-end.
    value = text.strip()
    header_re = _name_header_re(tuple(config.get("name_column_headers", ())))
    languages = config.get("languages") or list(DEFAULT_LANGUAGES)
    lang = languages[0] if len(languages) == 1 else None
    if (
        header_re.search(header or "")
        and value
        and not _NOT_A_NAME.match(value)
        and _looks_like_name(value, analyzer, lang)
        and not any(f.start == 0 and f.end >= len(value) for f in result)
    ):
        start = text.index(value)
        result.append(
            Finding(
                entity_type="PERSON",
                value=value,
                score=_NAME_COLUMN_SCORE,
                context=f"{header}: {value}",
                unit_id=unit_id,
                start=start,
                end=start + len(value),
                source="whole_cell_override",
            )
        )

    # Topical header override: a column whose header maps to a category (Tool,
    # Abteilung, Lizenzgeber, Projekt, ...) makes the WHOLE cell that category --
    # the document's own schema is authoritative. Covers name columns (the cell
    # IS the tool/division name) and description columns (the whole PROJECT
    # description is claimed for redact/summarize). Source-tagged so it bypasses
    # the NER noise/corroboration filters (those gate only NER entity types).
    category = _category_for_header(header, config)
    if category and value and not _NOT_A_NAME.match(value) and not any(
        f.start == 0 and f.end >= len(value) for f in result
    ):
        start = text.index(value)
        result.append(
            Finding(
                entity_type=category,
                value=value,
                score=_TOPICAL_SCORE,
                context=f"{header}: {value}",
                unit_id=unit_id,
                start=start,
                end=start + len(value),
                source="topical_header",
            )
        )

    # The whole-cell override can PARTIALLY overlap a finding NER did make (just the
    # surname, or a KONTO number in the same cell). Appending it raw left overlapping
    # spans, which the cell splicer assumes never happens -> garbled tokens. Re-resolve
    # the combined set so the no-overlap invariant holds (the override merges to cover
    # the cell rather than corrupting it).
    return _resolve_overlaps(result, text)


def scan(path: Path, analyzer, config) -> list:
    wb = openpyxl.load_workbook(path, data_only=False)
    findings = []
    # A "database" sheet repeats the same value thousands of times (a status, a
    # division, a city), and detection (one spaCy NER pass per cell) is the entire
    # cost. Memoize by (header, cell-text) for this scan: identical cells detect
    # once. Findings are re-stamped with each cell's unit_id (offsets/values are
    # relative to the cell text, so nothing else changes) so completeness-scan
    # coverage still maps to the right unit.
    cache: dict[tuple[str | None, str], list] = {}
    artifacts_by_key = _precompute_cell_artifacts(wb, analyzer, config)

    def detect(text, header, key):
        base = cache.get((header, text))
        if base is None:
            base = _analyze_cell_text(text, header, analyzer, config, nlp_artifacts=artifacts_by_key.get((header, text)))
            cache[(header, text)] = base
        return [replace(f, unit_id=key) for f in base]

    for key, text, header in _iter_cell_units(wb):
        findings.extend(detect(text, header, key))
    for key, text in _iter_defined_name_units(wb):
        findings.extend(detect(text, None, key))
    return findings


def _apply_findings_to_text(
    text: str, header: str | None, analyzer, config, decisions: dict, mapping_store, nlp_artifacts=None
) -> str:
    findings = _analyze_cell_text(text, header, analyzer, config, nlp_artifacts=nlp_artifacts)
    if not findings:
        return text
    result = text
    for f in sorted(findings, key=lambda f: -f.start):
        action = decisions_lookup(decisions, f.entity_type, f.value)
        replacement = resolve_replacement(f.entity_type, f.value, action, mapping_store)
        if replacement is None:
            continue
        result = result[: f.start] + replacement + result[f.end :]
    return result


def apply(path: Path, out_path: Path, decisions: dict, analyzer, config, mapping_store) -> None:
    # keep_vba=False (the default) strips any macro project from the output,
    # which is intentional: anonymized copies are never macro-enabled.
    wb = openpyxl.load_workbook(path, data_only=False, keep_vba=False)
    # Whole-column blackout policies: {"Sheet!A": "pseudonymize"|"anonymize"|"summarize"}.
    column_policies = config.get("column_policies", {}) or {}
    # Per-CELL policies: {"Sheet!A5": mode} -- the finest-grained EXCEPTION layer,
    # wins over the column policy for that one cell. Blackout modes only (same
    # verify-safety constraint as column policy: a value-keyed "skip" can't be
    # expressed out-of-band without tripping the fail-loud residual check).
    cell_policies = config.get("cell_policies", {}) or {}
    blackout_cache: dict[tuple[str, str], str] = {}  # (key, value) -> token

    def blackout(col_key: str, header: str, col_letter: str, value: str, action: str) -> str:
        cached = blackout_cache.get((col_key, value))
        if cached is None:
            entity = _column_entity_type(header, col_letter)
            cached = resolve_replacement(entity, value, action, mapping_store) or value
            blackout_cache[(col_key, value)] = cached
        return cached

    # Memoize the redacted output by (header, text) for this apply -- a repeated
    # cell redacts to the same string. Safe: the pseudonym mapping is value-keyed,
    # so the same value already maps to the same token whether recomputed or cached
    # (the first call creates the mapping entry; the rest reuse the string).
    redact_cache: dict[tuple[str | None, str], str] = {}
    artifacts_by_key = _precompute_cell_artifacts(wb, analyzer, config)

    def redact(text: str, header: str | None) -> str:
        out = redact_cache.get((header, text))
        if out is None:
            out = _apply_findings_to_text(
                text, header, analyzer, config, decisions, mapping_store,
                nlp_artifacts=artifacts_by_key.get((header, text)),
            )
            redact_cache[(header, text)] = out
        return out

    for ws in wb.worksheets:
        headers = _column_headers(ws)
        for row in ws.iter_rows():
            for cell in row:
                col_letter = get_column_letter(cell.column)
                header = headers.get(cell.column) if cell.row != 1 else None
                # Per-CELL policy first (finest granularity) -- wins over the
                # column policy and any per-value decision for this one cell.
                cell_policy = cell_policies.get(f"{ws.title}!{cell.coordinate}")
                if (
                    cell_policy in _COLUMN_BLACKOUT_ACTIONS
                    and cell.row != 1
                    and cell.data_type in ("s", "n")
                    and cell.value not in (None, "")
                ):
                    cell.value = blackout(
                        f"{ws.title}!{cell.coordinate}", headers.get(cell.column, ""), col_letter,
                        str(cell.value), cell_policy,
                    )
                    continue
                policy = column_policies.get(f"{ws.title}!{col_letter}")
                # A column blackout wins over any per-value decision: EVERY non-empty
                # cell (header row excluded) is replaced, including cells detection
                # never flagged. Formula cells are left (consistent with detection).
                if (
                    policy in _COLUMN_BLACKOUT_ACTIONS
                    and cell.row != 1
                    and cell.data_type in ("s", "n")
                    and cell.value not in (None, "")
                ):
                    cell.value = blackout(
                        f"{ws.title}!{col_letter}", headers.get(cell.column, ""), col_letter, str(cell.value), policy
                    )
                    continue
                # Row 1 is the schema label, never scanned as its own data unit
                # (see _iter_cell_units) -- excluded here too so apply() redacts
                # exactly what scan() surfaced, never more (scan/apply parity).
                text = _cell_scan_text(cell) if cell.row != 1 else None
                if text is not None:
                    new_value = redact(text, header)
                    if new_value != text:
                        # A redacted numeric cell must become a string cell so
                        # the token ("[KONTO_1]") can be stored at all.
                        cell.value = new_value
                if cell.comment is not None and cell.comment.text.strip():
                    new_text = redact(cell.comment.text, header)
                    if new_text != cell.comment.text:
                        cell.comment.text = new_text
    for name, defn in wb.defined_names.items():
        if isinstance(defn.value, str) and defn.value.strip():
            new_value = redact(defn.value, None)
            if new_value != defn.value:
                defn.value = new_value
    wb.save(out_path)
