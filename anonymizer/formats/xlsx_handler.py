from __future__ import annotations

import functools
import re
from dataclasses import replace
from pathlib import Path

import openpyxl
from openpyxl.utils import column_index_from_string, get_column_letter

from ..actions import decisions_lookup, resolve_replacement, token_label
from ..core import _resolve_overlaps, detect_unit
from ..models import ColumnInfo, Finding, ProcessingError, TextUnit
from .run_replace import apply_aux_parts, aux_text_units

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


# A formula's double-quoted STRING LITERALS. `=("Kunde "&"Mueller")` and
# `=CONCATENATE("Hans Mueller";A2)` put real PII in a cell whose stored value is a
# formula, which _cell_scan_text deliberately skips -- so it was never scanned,
# never removed, and (being undecided) never re-checked by the verify either.
# Only the literals are scanned and spliced: a token anywhere else in the
# expression would corrupt the formula, and the structure (function names, cell
# refs, operators) is not PII. `""` is Excel's escaped quote and stays inside the
# literal.
_FORMULA_LITERAL_RE = re.compile(r'"((?:[^"]|"")*)"')


def _formula_literals(formula: str) -> list[tuple[int, int, str]]:
    """(start, end, text) of each string literal in a formula, offsets into `formula`."""
    return [(m.start(1), m.end(1), m.group(1)) for m in _FORMULA_LITERAL_RE.finditer(formula)]


def _cell_formula(cell) -> str | None:
    return cell.value if cell.data_type == "f" and isinstance(cell.value, str) else None


def _iter_cell_units(wb):
    """Yields (id, text, header) -- header is the column-1-row text for that
    column, given as context so recognizers relying on nearby German context
    words (Kontonummer, Depotnummer, ...) actually have something to match,
    since a bare cell value alone carries no context."""
    for ws in wb.worksheets:
        headers = _column_headers(ws)
        for row in ws.iter_rows():
            for cell in row:
                header = headers.get(cell.column) if cell.row != 1 else None
                text = _cell_scan_text(cell)
                if text is not None:
                    yield f"cell|{ws.title}|{cell.coordinate}", text, header
                formula = _cell_formula(cell)
                if formula is not None:
                    for k, (_s, _e, literal) in enumerate(_formula_literals(formula)):
                        if literal.strip():
                            yield f"formula|{ws.title}|{cell.coordinate}|{k}", literal, header
                if cell.comment is not None and cell.comment.text.strip():
                    yield f"comment|{ws.title}|{cell.coordinate}", cell.comment.text, header


def _iter_defined_name_units(wb):
    for name, defn in wb.defined_names.items():
        if isinstance(defn.value, str) and defn.value.strip():
            yield f"defined_name|{name}", defn.value


def _iter_sheet_name_units(wb):
    """The worksheet TITLES. Their authoritative copy is xl/workbook.xml
    <sheet name="...">, which no cell walk reaches -- and for this deployment a
    workbook with ONE SHEET PER CLIENT is a completely normal shape, so a sheet
    name is a first-class leak, not decoration. (docProps/app.xml TitlesOfParts is
    only a CACHE of these; blanking it removed the evidence, not the leak.)"""
    for ws in wb.worksheets:
        if ws.title.strip():
            yield f"sheetname|{ws.title}", ws.title


def extract_text_units(path: Path) -> list[TextUnit]:
    wb = openpyxl.load_workbook(path, data_only=False)
    units = [TextUnit(id=key, text=text) for key, text, _header in _iter_cell_units(wb)]
    units.extend(TextUnit(id=key, text=text) for key, text in _iter_defined_name_units(wb))
    units.extend(TextUnit(id=key, text=text) for key, text in _iter_sheet_name_units(wb))
    # Surfaces openpyxl's cell model never exposes: external hyperlink TARGETS
    # (the URL behind a cell link) and chart part text (see run_replace).
    units.extend(aux_text_units(path))
    return units


# --- column-level policy (redact/pseudonymize a whole column) -----------------
# A column policy blacks out EVERY non-empty cell in a column regardless of what
# detection found -- the only way to redact a column whose sensitivity is topical
# (a confidential project description) rather than an identifiable entity. Actions
# supported at the column level: "pseudonymize" (consistent per-column token) and
# "anonymize" (one-way). "skip" is intentionally NOT here: the value-keyed decision
# model can't express "keep this value here but remove it elsewhere", so a skipped
# column that shares a value with a removed one would trip the fail-loud verify.
_COLUMN_BLACKOUT_ACTIONS = ("pseudonymize", "anonymize")


# German umlauts MUST be transliterated, not preserved: actions.TOKEN_RE is
# `[A-Z0-9_]` and can never match one, so a pseudonymized 'Prüfung' column wrote
# [PRÜFUNG_1] tokens that reidentify_text could NEVER reverse -- permanently
# unreversible, and silently so. The German transliteration keeps the label
# readable (PRUEFUNG, not PRFUNG) while staying inside the token alphabet.
_UMLAUT_TRANSLITERATION = str.maketrans(
    {"Ä": "AE", "Ö": "OE", "Ü": "UE", "ä": "AE", "ö": "OE", "ü": "UE", "ß": "SS", "ẞ": "SS"}
)


def _column_entity_type(header: str, col_letter: str) -> str:
    """Per-column entity type for pseudonym tokens, derived from the header so a
    pseudonymized 'Projekt' column renders readable, re-identifiable [PROJEKT_n]
    tokens. Falls back to the column letter when there is no header."""
    ascii_header = (header or "").strip().translate(_UMLAUT_TRANSLITERATION)
    base = re.sub(r"[^0-9A-Za-z]+", "_", ascii_header).strip("_").upper()
    return base or f"COLUMN_{col_letter}"


def _coord_column(coord: str) -> str | None:
    m = re.match(r"([A-Z]+)", coord)
    return m.group(1) if m else None


def column_summary(path: Path, findings: list) -> list[ColumnInfo]:
    """Describe each spreadsheet column (sheet, letter, header, a sample value,
    and how many findings landed in it) so the reviewer can set a whole-column
    policy. Only columns that carry a header OR at least one finding are listed --
    empty structural columns are noise."""
    counts: dict[tuple[str, str], int] = {}
    for f in findings:
        parts = f.unit_id.split("|")
        if len(parts) >= 3 and parts[0] in ("cell", "comment", "formula"):
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
                    col = get_column_letter(cell.column)
                    if col in wanted and col not in samples and cell.value not in (None, ""):
                        samples[col] = str(cell.value)
            for col in sorted(wanted, key=column_index_from_string):
                out.append(
                    ColumnInfo(
                        sheet=ws.title,
                        column=col,
                        header=headers.get(col, ""),
                        sample=samples.get(col, ""),
                        pii_count=counts.get((ws.title, col), 0),
                    )
                )
    finally:
        wb.close()
    return out


def _analyze_cell_text(text: str, header: str | None, analyzer, config, unit_id: str = "tmp") -> list:
    prefix = f"{header}: " if header else ""
    combined = prefix + text
    unit = TextUnit(id=unit_id, text=combined)
    findings = detect_unit(analyzer, unit, config)
    offset = len(prefix)
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
    if (
        header_re.search(header or "")
        and value
        and not _NOT_A_NAME.match(value)
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

    def detect(text, header, key):
        base = cache.get((header, text))
        if base is None:
            base = _analyze_cell_text(text, header, analyzer, config)
            cache[(header, text)] = base
        return [replace(f, unit_id=key) for f in base]

    for key, text, header in _iter_cell_units(wb):
        findings.extend(detect(text, header, key))
    for key, text in _iter_defined_name_units(wb):
        findings.extend(detect(text, None, key))
    for key, text in _iter_sheet_name_units(wb):
        findings.extend(detect(text, None, key))
    # scan() builds its own unit stream rather than reusing extract_text_units, so
    # the auxiliary surfaces must be added here too or they would be reported to
    # the reviewer but never actually scanned.
    for unit in aux_text_units(path):
        findings.extend(detect_unit(analyzer, unit, config))
    return findings


def _apply_findings_to_text(text: str, header: str | None, analyzer, config, decisions: dict, mapping_store) -> str:
    findings = _analyze_cell_text(text, header, analyzer, config)
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


# --- sheet-name redaction -----------------------------------------------------
#
# Excel forbids [ ] : * ? / \ in a worksheet title and caps it at 31 characters,
# so a redacted title CANNOT carry a "[PERSON_1]" token -- there is no legal way to
# write one. A title that holds values the reviewer removed is therefore replaced
# wholesale by a neutral, unique placeholder. Deliberate trade-off: the TITLE is
# one-way even when the value in it was pseudonymized. Nothing becomes
# unrecoverable that was not already: the same person's name still appears as its
# pseudonym everywhere it occurs in cells, and the alternative -- leaving the tab
# reading "Kunde Hans Mueller" -- is the leak this exists to close.
_SHEET_TITLE_PLACEHOLDERS = ("Sheet", "Blatt", "Tabelle", "Anonym")
_MAX_SHEET_TITLE = 31


def _checked_removed_values(decisions: dict, config) -> set[str]:
    """The lower-cased values whose literal survival anywhere in the output is a
    leak, using the same rule as pipeline._literal_residual: everything the reviewer
    removed, minus values under 4 characters (too short to identify anyone and a
    magnet for substring false positives), plus every deny-list term regardless of
    length. Used to prove a REPLACEMENT title is itself clean."""
    removed = {
        value
        for (_entity, value), action in decisions.items()
        if action in _COLUMN_BLACKOUT_ACTIONS and len(value) >= 4
    }
    removed.update(t.strip().lower() for t in (config.get("deny_list") or []) if t.strip())
    return removed


def _sheet_renames(wb, redact, removed: set[str]) -> dict[str, str]:
    """{old title: new title} for every sheet whose NAME carries a removed value.

    The trigger is the ordinary redaction: if redacting the title changes it, the
    title holds something the reviewer removed. It has to be exactly that test and
    not a length-filtered one, because pipeline.verify_output re-scans the output
    with the recognizers and would re-detect a short value just the same.

    The replacement is a neutral placeholder PROVEN clean two ways -- no decided
    value redacts out of it, and no checked removed value is a substring of it. A
    workbook whose sheet is literally called "Sheet" would otherwise be renamed to
    "Sheet_1" and still contain the removed value, so such candidates are rejected
    and the next stem is tried. If none is safe we fail loud rather than ship it."""
    taken = set(wb.sheetnames)
    renames: dict[str, str] = {}
    for ws in wb.worksheets:
        title = ws.title
        if not title.strip() or redact(title, None) == title:
            continue
        candidate = None
        for stem in _SHEET_TITLE_PLACEHOLDERS:
            for n in range(1, len(wb.worksheets) + 50):
                trial = f"{stem}_{n}"
                if trial in taken or len(trial) > _MAX_SHEET_TITLE:
                    continue
                trial_low = trial.lower()
                if any(v in trial_low for v in removed) or redact(trial, None) != trial:
                    continue
                candidate = trial
                break
            if candidate:
                break
        if candidate is None:
            raise ProcessingError(
                f"Could not build a safe replacement name for sheet '{title}' -- every "
                "candidate still contained a value that had to be removed. No file was written."
            )
        taken.add(candidate)
        renames[title] = candidate
    return renames


def _rewrite_sheet_refs(expr: str, renames: dict[str, str]) -> str:
    """Repoints every sheet reference in a formula / defined-name expression at the
    renamed sheet. Both spellings Excel uses are handled: the quoted `'Kunde X'!A1`
    (internal apostrophes doubled) and the bare `Umsatz!A1`. The replacement is
    always emitted quoted, which is valid for any title."""
    for old, new in renames.items():
        quoted_old = "'" + old.replace("'", "''") + "'"
        expr = expr.replace(f"{quoted_old}!", f"'{new}'!")
        # Bare form only when nothing sheet-name-ish precedes it, so "BLA!A1" is
        # never mangled by a sheet called "A".
        expr = re.sub(rf"(?<![\w.']){re.escape(old)}!", f"'{new}'!", expr)
    return expr


def _apply_sheet_renames(wb, renames: dict[str, str]) -> None:
    if not renames:
        return
    for ws in wb.worksheets:
        new = renames.get(ws.title)
        if new is not None:
            ws.title = new
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                formula = _cell_formula(cell)
                if formula is not None:
                    rewritten = _rewrite_sheet_refs(formula, renames)
                    if rewritten != formula:
                        cell.value = rewritten
    for _name, defn in wb.defined_names.items():
        if isinstance(defn.value, str):
            rewritten = _rewrite_sheet_refs(defn.value, renames)
            if rewritten != defn.value:
                defn.value = rewritten


def _assert_no_stale_sheet_refs(wb, renames: dict[str, str]) -> None:
    """Fail loud if a renamed sheet's OLD title still appears in any formula or
    defined name. Two things go wrong at once if it does: the reference is now
    dangling (a #REF! in the colleague's copy), and the old title IS the PII we
    just removed -- so shipping the file would be the leak. Reference spellings
    this rewriter does not know (3D refs across a sheet RANGE) land here."""
    if not renames:
        return
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                formula = _cell_formula(cell)
                if formula and any(old in formula for old in renames):
                    raise ProcessingError(
                        f"Sheet '{ws.title}' cell {cell.coordinate} still references a sheet name "
                        "that had to be redacted, in a form this tool cannot rewrite safely. "
                        "No file was written."
                    )
    for name, defn in wb.defined_names.items():
        if isinstance(defn.value, str) and any(old in defn.value for old in renames):
            raise ProcessingError(
                f"Defined name '{name}' still references a sheet name that had to be redacted, "
                "in a form this tool cannot rewrite safely. No file was written."
            )


def apply(path: Path, out_path: Path, decisions: dict, analyzer, config, mapping_store) -> None:
    # keep_vba=False (the default) strips any macro project from the output,
    # which is intentional: anonymized copies are never macro-enabled.
    wb = openpyxl.load_workbook(path, data_only=False, keep_vba=False)
    # Whole-column blackout policies: {"Sheet!A": "pseudonymize"|"anonymize"}.
    column_policies = config.get("column_policies", {}) or {}
    blackout_cache: dict[tuple[str, str], str] = {}  # (col_key, value) -> token

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

    def redact(text: str, header: str | None) -> str:
        out = redact_cache.get((header, text))
        if out is None:
            out = _apply_findings_to_text(text, header, analyzer, config, decisions, mapping_store)
            redact_cache[(header, text)] = out
        return out

    def redact_formula(formula: str, header: str | None) -> str:
        """Redacts only the quoted string literals, right-to-left so earlier
        offsets stay valid -- the expression around them is left byte-identical."""
        result = formula
        for start, end, literal in reversed(_formula_literals(formula)):
            if not literal.strip():
                continue
            new_literal = redact(literal, header)
            if new_literal != literal:
                result = result[:start] + new_literal + result[end:]
        return result

    # Sheet TITLES are redacted before anything else, so the reference rewrite runs
    # while formulas still hold the original names -- and every later pass then
    # sees the already-safe title.
    renames = _sheet_renames(wb, redact, _checked_removed_values(decisions, config))
    _apply_sheet_renames(wb, renames)
    # Column policies were keyed by the reviewer against the ORIGINAL sheet titles,
    # so a renamed sheet must still find its policy.
    original_title = {new: old for old, new in renames.items()}

    for ws in wb.worksheets:
        sheet_key = original_title.get(ws.title, ws.title)
        headers = _column_headers(ws)
        for row in ws.iter_rows():
            for cell in row:
                col_letter = get_column_letter(cell.column)
                policy = column_policies.get(f"{sheet_key}!{col_letter}")
                header = headers.get(cell.column) if cell.row != 1 else None
                # A column blackout wins over any per-value decision: EVERY non-empty
                # cell (header row excluded) is replaced, including cells detection
                # never flagged. Formula cells are left to the value path below,
                # which redacts the PII inside their string literals without
                # destroying the expression.
                if (
                    policy in _COLUMN_BLACKOUT_ACTIONS
                    and cell.row != 1
                    and cell.data_type in ("s", "n")
                    and cell.value not in (None, "")
                ):
                    cell.value = blackout(
                        f"{sheet_key}!{col_letter}", headers.get(cell.column, ""), col_letter, str(cell.value), policy
                    )
                    continue
                text = _cell_scan_text(cell)
                if text is not None:
                    new_value = redact(text, header)
                    if new_value != text:
                        # A redacted numeric cell must become a string cell so
                        # the token ("[KONTO_1]") can be stored at all.
                        cell.value = new_value
                formula = _cell_formula(cell)
                if formula is not None:
                    new_formula = redact_formula(formula, header)
                    if new_formula != formula:
                        cell.value = new_formula
                if cell.comment is not None and cell.comment.text.strip():
                    new_text = redact(cell.comment.text, header)
                    if new_text != cell.comment.text:
                        cell.comment.text = new_text
    for name, defn in wb.defined_names.items():
        if isinstance(defn.value, str) and defn.value.strip():
            new_value = redact(defn.value, None)
            if new_value != defn.value:
                defn.value = new_value
    # Checked LAST, once the string literals inside formulas have been redacted, so
    # a leftover really is an unrewritten reference rather than redacted prose.
    _assert_no_stale_sheet_refs(wb, renames)
    wb.save(out_path)
    apply_aux_parts(out_path, analyzer, config, decisions, mapping_store)
