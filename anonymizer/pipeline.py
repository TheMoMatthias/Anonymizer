from __future__ import annotations

import bisect
import io
import os
import re
import tempfile
import zipfile
from pathlib import Path

from lxml import etree

from . import audit as audit_mod
from . import core
from . import language
from . import ocr as ocr_mod
from . import xmlsafe
from .engine import SPACY_MODELS, DEFAULT_LANGUAGES
from .gliner_recognizer import resolve_model_path
from .actions import decisions_lookup
from .formats import docx_handler, legacy, pdf_handler, pptx_handler, xlsx_handler
from .mapping import MappingStore
from .models import Finding, GroupedFinding, ProcessingError, ScanResult, TextUnit
from .report import write_report

__all__ = ["ProcessingError"]  # re-exported for callers doing `from .pipeline import ProcessingError`

_HANDLERS = {
    ".docx": docx_handler,
    ".xlsx": xlsx_handler,
    ".xlsm": xlsx_handler,
    ".xls": xlsx_handler,
    ".pptx": pptx_handler,
    ".pdf": pdf_handler,
}

_OUTPUT_EXT_OVERRIDE = {".doc": ".docx", ".xls": ".xlsx", ".ppt": ".pptx", ".xlsm": ".xlsx"}

SUPPORTED_EXTENSIONS = set(_HANDLERS) | set(legacy.LEGACY_EXTENSIONS)

# Actions that remove a value; used to decide what the output re-scan must not
# still contain.
_REMOVING_ACTIONS = ("pseudonymize", "anonymize")


def _handler_for(path: Path):
    handler = _HANDLERS.get(path.suffix.lower())
    if handler is None:
        raise ProcessingError(f"Unsupported file type: {path.suffix}")
    return handler


def output_path_for(path: Path, out_dir: Path | None = None) -> Path:
    """Where the anonymized copy is written. Default: next to the source as
    `<stem>_psd<ext>` (idempotent — re-running overwrites the file's own output).

    When `out_dir` is given (the GUI routes every save to a fixed
    Documents\\Anonymized folder, because dropped/uploaded files have no origin
    folder), the output goes there instead. Two different sources sharing a name
    must NOT clobber each other, so the name is uniquified (`_psd(2)`, `_psd(3)`)
    when the target already exists -- in a bank workflow, never losing a prior
    anonymized document beats tidiness."""
    ext = _OUTPUT_EXT_OVERRIDE.get(path.suffix.lower(), path.suffix.lower())
    if out_dir is None:
        return path.with_name(f"{path.stem}_psd{ext}")
    candidate = out_dir / f"{path.stem}_psd{ext}"
    n = 2
    while candidate.exists():
        candidate = out_dir / f"{path.stem}_psd({n}){ext}"
        n += 1
    return candidate


def _guard_extractable(resolved: Path, units: list) -> None:
    """Refuses an image/scanned PDF that yielded no text -- but only when OCR is
    unavailable. With a portable Tesseract present, image pages are OCR'd, so
    empty units there just mean a genuinely blank document. Never emit a
    false-clean output."""
    if resolved.suffix.lower() == ".pdf" and not units and not ocr_mod.ocr_available():
        raise ProcessingError(
            "This PDF has no extractable text layer -- it is almost certainly a "
            "scanned/image PDF. OCR is not available (no Tesseract found), so it "
            "cannot be anonymized safely and no output was written. See the FAQ "
            "to enable OCR."
        )


# Language detection is regex word-counting (no NER), so it is cheap even over a
# large sample; cap only to stay bounded on a pathologically huge document.
_LANG_SAMPLE_MAX_CHARS = 200_000


def _language_sample(units: list) -> str:
    """Representative text for language detection, sampled ACROSS the whole
    document -- not just its first units.

    Sampling only `units[:80]` mis-detected a heavily-German spreadsheet as
    English (measured: de:en marker ratio was 1:5 in the first 80 units but
    4.7:1 across the whole file). The reason is structural: a spreadsheet's
    first units are the header row and structured field-name cells
    ("Project ID", "Status", "CostBlock" -- English-ish), while the German
    prose lives in the body. A confident-but-wrong 'en' then ran the English
    NER over German text, which tags ordinary German words as people/orgs --
    the exact over-flagging reported. Striding across the document (to a char
    budget) makes the body's language dominate, as it should."""
    texts = [u.text for u in units if getattr(u, "text", "") and u.text.strip()]
    if not texts:
        return ""
    if sum(len(t) for t in texts) <= _LANG_SAMPLE_MAX_CHARS:
        return " ".join(texts)
    step = max(1, len(texts) // 500)  # even spread, not the head
    picked, size = [], 0
    for t in texts[::step]:
        picked.append(t)
        size += len(t)
        if size >= _LANG_SAMPLE_MAX_CHARS:
            break
    return " ".join(picked)


def _narrow_language(config: dict, units: list) -> dict:
    """Collapses a multi-language config to the single detected language so only
    ONE spaCy NER model runs -- this is the fix for the English model flagging
    ordinary German words. Deterministic on the text, so scan and apply pick the
    same language and stay in parity. A config already pinned to one language
    (e.g. chosen in the GUI) is returned unchanged."""
    langs = config.get("languages") or list(DEFAULT_LANGUAGES)
    if len(langs) <= 1:
        return config
    lang, confident = language.detect_dominant(_language_sample(units))
    chosen = lang if (confident and lang in langs) else langs[0]
    narrowed = dict(config)
    narrowed["languages"] = [chosen]
    return narrowed


# Entity types worth propagating document-wide. Only free-text NER types: a
# structured ID either matches its pattern everywhere or nowhere, so it has
# nothing to gain and would only add false positives.
_PROPAGATABLE = ("PERSON",)
_MIN_PROPAGATE_LEN = 4
# `Herrn?` covers the dative "Herrn" address-block form; kept in sync with
# core._HONORIFIC_PREFIX and engine._HONORIFICS.
_HONORIFIC_PREFIX = re.compile(r"^(?:Herrn?|Frau|Hr\.|Fr\.|Dr\.|Prof\.)\s+")
# Every line-break form a document can leave INSIDE one detected span: CR/LF, the
# vertical tab OOXML uses for a soft break, a form feed, and the Unicode
# line/paragraph separators. A propagation seed must never straddle one of these.
_LINE_SPLIT = re.compile(r"[\r\n\v\f  ]+")


def _with_propagation(config: dict, units: list, analyzer) -> dict:
    """Pass 1: find the entity values this document confirms anywhere. Pass 2
    (in detect_unit) matches those values literally in EVERY unit, catching the
    occurrences NER dropped for lack of sentence context.

    Deterministic and parity-safe: scan and apply both call this with the same
    units and analyzer, so both derive the identical value set. Pass 1 runs on
    the config WITHOUT `propagate`, so it can never feed on itself."""
    if not config.get("propagate_enabled", True):
        return config
    values: set[tuple[str, str]] = set()
    # Propagation needs only the SET of confirmed values, so a unit whose text was
    # already scanned adds nothing -- skip it. In a spreadsheet the same cell text
    # recurs thousands of times, and detection (one NER pass per unit) is the whole
    # cost; deduping the pass-1 sweep by text is a large, result-preserving saving.
    distinct_texts = list(dict.fromkeys(u.text for u in units))
    languages = config.get("languages") or list(DEFAULT_LANGUAGES)
    # Batch-NLP every distinct text in one spaCy pipe() pass rather than one
    # analyze() call each -- this pre-pass re-runs detection over the WHOLE
    # document just to seed propagation, so it pays the same per-call overhead
    # scan() does; batching here is what makes a large spreadsheet's redundant
    # first pass cheap instead of doubling the scan cost.
    # Batch on the SAME cleaned text detect_unit will request via its own
    # neutralize_structural_noise call (see core.py) -- batching on the raw
    # text would precompute tokenization for a string detect_unit never uses.
    artifacts_by_clean = (
        core.precompute_nlp_artifacts(
            analyzer, (core.neutralize_structural_noise(t) for t in distinct_texts), languages[0]
        )
        if len(languages) == 1
        else {}
    )
    for text in distinct_texts:
        unit = TextUnit(id="propagate", text=text)
        artifacts = artifacts_by_clean.get(core.neutralize_structural_noise(text))
        for f in core.detect_unit(analyzer, unit, config, nlp_artifacts=artifacts):
            if f.entity_type not in _PROPAGATABLE:
                continue
            # A detected span can run ACROSS a line break -- a multi-line cell, a
            # wrapped paragraph, or a German address block ("Herrn\nHans Mueller\n
            # Hauptstrasse 5"). Seeding that verbatim is doubly wrong: the literal
            # pattern (pass 2 escapes it) matches nowhere else, and the "last
            # whitespace token" surname seed becomes the street name, so the real
            # surname is never propagated and a bare "Mueller" in another cell LEAKS.
            # Seed each LINE independently instead.
            for line in _LINE_SPLIT.split(f.value):
                value = _HONORIFIC_PREFIX.sub("", line).strip()
                if len(value) >= _MIN_PROPAGATE_LEN:
                    values.add((f.entity_type, value))
                # Also seed the surname alone: NER reliably catches "Björn Müller"
                # in prose but misses a bare "Müller" in a cell -- and the bare form
                # is precisely the measured gap. Two people sharing a surname both
                # seed it, so their bare-surname mentions collapse into ONE pseudonym
                # (documented trade-off: merging two subjects is wrong, but dropping
                # the seed LEAKS a real surname -- see test_hardening.py).
                parts = [p for p in value.split() if len(p) >= _MIN_PROPAGATE_LEN]
                if len(parts) > 1:
                    values.add((f.entity_type, parts[-1]))
    if not values:
        return config
    return {**config, "propagate": sorted(values)}


def _with_topical_gazetteer(config: dict, resolved: Path, handler) -> dict:
    """Merge the handler's auto-learned topical gazetteer (category, value) pairs
    into config['propagate'], so terms confirmed in a category-labelled column
    (a Tool/Abteilung/Lizenzgeber column) propagate document-wide carrying their
    category -- reusing the same propagation engine as person-name spreading.
    Called from BOTH scan and apply, so the derived set is identical (parity).
    Also folds in any manual per-category terms from config['topical']."""
    topical = config.get("topical") or {}
    if not topical.get("enabled", True):
        return config
    pairs: set[tuple[str, str]] = set()
    # Auto-learned header->category gazetteer is structural and currently
    # xlsx-only (only that handler exposes it); manual per-category terms below
    # propagate in EVERY format's text.
    if hasattr(handler, "topical_gazetteer"):
        pairs.update(handler.topical_gazetteer(resolved, config))
    for cat, spec in (topical.get("categories") or {}).items():
        for term in spec.get("terms", []) or []:
            if term and term.strip():
                pairs.add((cat, term.strip()))
    if not pairs:
        return config
    merged = list(config.get("propagate", ())) + sorted(pairs)
    return {**config, "propagate": merged}


def sniff_language(path: Path, config: dict) -> tuple[str, bool]:
    """(language, confident) for the GUI's 'ask the user if unsure' flow.
    Best-effort and never raises -- an unreadable file returns an unconfident
    German default so the caller prompts."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            resolved = (
                legacy.convert_to_modern(path, Path(tmp))
                if path.suffix.lower() in legacy.LEGACY_EXTENSIONS
                else path
            )
            handler = _handler_for(resolved)
            units = handler.extract_text_units(resolved)
        return language.detect_dominant(_language_sample(units))
    except Exception:  # noqa: BLE001
        return ("de", False)


def scan_document(path: Path, analyzer, config: dict) -> ScanResult:
    try:
        with tempfile.TemporaryDirectory() as tmp:
            resolved = (
                legacy.convert_to_modern(path, Path(tmp))
                if path.suffix.lower() in legacy.LEGACY_EXTENSIONS
                else path
            )
            handler = _handler_for(resolved)
            units = handler.extract_text_units(resolved)
            _guard_extractable(resolved, units)
            cfg = _narrow_language(config, units)
            cfg = _with_propagation(cfg, units, analyzer)
            cfg = _with_topical_gazetteer(cfg, resolved, handler)
            findings = handler.scan(resolved, analyzer, cfg)
            # Column + cell descriptors (spreadsheets only) for the column-level
            # policy and the per-cell exception layer; computed here while the
            # resolved file still exists.
            columns = handler.column_summary(resolved, findings, cfg) if hasattr(handler, "column_summary") else []
            cells = handler.cell_summary(findings) if hasattr(handler, "cell_summary") else []
    except ProcessingError:
        raise
    except Exception as exc:  # noqa: BLE001 -- fail loud, never silently pass
        raise ProcessingError(f"Could not read '{path.name}': {exc}") from exc
    result = core.build_scan_result(findings, units, cfg)
    result.columns = columns
    result.cells = cells
    return result


def verify_output(out_path: Path, decisions: dict, analyzer, config: dict) -> list[Finding]:
    """Re-scans a written output and returns any residual finding whose value
    was supposed to be removed -- i.e. a leak. Empty list == verified clean.

    This pass is HANDLER-DEPENDENT by construction (it re-reads the output with
    the same handler that wrote it), so it is only half the gate: a location the
    handler is blind to on the way IN is equally invisible on the way OUT, and a
    value the scan never surfaced is not in `decisions` and so is not checked here
    at all. `_literal_residual` is the other half -- recognizer- AND handler-
    independent, reading the raw package (every XML part's text AND attribute
    values, external relationship targets, embedded OOXML packages, PDF
    metadata/annotations/link URIs). Both
    must pass before an output is committed. The lesson from the surfaces closed
    in run_replace.aux_text_units: whenever an extractor gains reach, this pass
    gains it too -- never rely on this one alone."""
    handler = _handler_for(out_path)
    residual: list[Finding] = []
    for f in handler.scan(out_path, analyzer, config):
        if decisions_lookup(decisions, f.entity_type, f.value) in _REMOVING_ACTIONS:
            residual.append(f)
    return residual


_OOXML_EXTS = (".docx", ".xlsx", ".xlsm", ".pptx")
_OOXML_META_PARTS = ("docProps/core.xml", "docProps/app.xml", "docProps/custom.xml")
# Identifying metadata fields (OOXML local tag names) the body-text redaction
# never touches -- author / last editor / manager / company routinely carry the
# real advisor or author name, and dc:title is the classic home of a customer
# name ("Kreditakte Hans Mueller"). Matched CASE-INSENSITIVELY: core.xml spells
# them lowerCamel (cp:lastModifiedBy) but app.xml spells the same fields
# TitleCase (<Company>, <Manager>), so a case-sensitive set silently missed
# app.xml entirely.
_META_CLEAR_TAGS = frozenset(
    {
        "creator",
        "lastmodifiedby",
        "manager",
        "company",
        "lastprinted",
        # Descriptive fields -- never body text, so neither the redaction nor the
        # recognizer re-scan ever looked at them.
        "title",
        "subject",
        "description",
        "keywords",
        "category",
        "contentstatus",
    }
)
# app.xml caches every Word heading / Excel sheet name here, and this copy is not
# reached by any redaction pass, so the ORIGINAL text survived in a "verified"
# file. Blanking it is necessary but NEVER sufficient on its own: it is only a
# cache, so for a Word heading the authoritative copy is the body (redacted there)
# and for an Excel sheet name it is xl/workbook.xml <sheet name="...">. Clearing
# the cache alone would remove the evidence and leave the leak, which is why sheet
# names are surfaced and renamed by xlsx_handler (_iter_sheet_name_units /
# _apply_sheet_renames) rather than trusted to this.
_META_CLEAR_SUBTREES = frozenset({"titlesofparts"})


def _local_name(el) -> str:
    """The namespace-stripped tag, or "" for a comment/PI (whose .tag is not a
    string -- etree.QName would raise on those)."""
    tag = el.tag
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _scrub_metadata(out_path: Path) -> None:
    """Blanks identifying document metadata so a real name in docProps/PDF-info
    can't ride along in a file marked 'verified' (the body-text redaction and
    the recognizer re-scan both read body text only). Covers the identity fields
    (creator / lastModifiedBy / manager / company), the descriptive ones
    (title / subject / description / keywords / category), app.xml's cached
    heading + sheet-name list, and drops docProps/custom.xml outright."""
    suffix = out_path.suffix.lower()
    if suffix == ".pdf":
        import fitz

        # A FULL rewrite (garbage-collect + clean), NOT saveIncr: an incremental
        # save appends a revision and leaves the OLD /Info object (author name)
        # physically recoverable in the file bytes. Drop the XMP packet too, then
        # atomically replace.
        tmp = out_path.with_name(out_path.stem + ".metatmp.pdf")
        try:
            with fitz.open(out_path) as doc:
                doc.set_metadata({})  # clears author/title/subject/keywords/creator/producer
                # Drop the XMP packet (separate from /Info; garbage/clean do NOT remove
                # it). Do NOT swallow a failure here -- if we can't remove XMP we cannot
                # guarantee an author name isn't riding along, so fail loud rather than
                # ship a PDF marked "verified" with PII still in its metadata.
                doc.del_xml_metadata()
                doc.save(str(tmp), garbage=4, deflate=True, clean=True)
            os.replace(tmp, out_path)
        except BaseException:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise
        return
    if suffix not in _OOXML_EXTS:
        return
    with zipfile.ZipFile(out_path) as zf:
        names = zf.namelist()
        contents = {n: zf.read(n) for n in names}
    changed = False
    for part in _OOXML_META_PARTS:
        if part not in contents:
            continue
        try:
            tree = xmlsafe.fromstring(contents[part])
        except etree.XMLSyntaxError:
            continue
        part_changed = False
        if part == "docProps/custom.xml":
            # Custom properties are arbitrary user-defined fields (bank templates
            # put the case owner / customer number there) with no fixed names to
            # match, and they hold typed values (vt:i4, vt:filetime) that blanking
            # would make invalid. Drop the properties entirely -- an anonymized copy
            # keeps no custom metadata.
            for prop in list(tree):
                tree.remove(prop)
                part_changed = True
        for el in tree.iter():
            local = _local_name(el).lower()
            if local in _META_CLEAR_TAGS and (el.text or ""):
                el.text = ""
                part_changed = True
            elif local in _META_CLEAR_SUBTREES:
                for descendant in el.iter():
                    if descendant.text:
                        descendant.text = ""
                        part_changed = True
        if part_changed:
            contents[part] = etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True)
            changed = True
    if changed:
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for n in names:
                zf.writestr(n, contents[n])


# OOXML local tag names that delimit an INDEPENDENT value: a spreadsheet cell
# (`c`) or shared-string item (`si`), a word/slide paragraph (`p`), a table row
# (`tr`). Text is concatenated WITHIN these (so a name split across formatting
# runs -- <t>Mül</t><t>ler</t> -- still rejoins for the residual check) but a
# sentinel is inserted BETWEEN them, so gluing two unrelated cells can never
# forge a phantom match. Worksheets store string cells as integer shared-string
# INDICES in <v>, so without this the concatenated indices of adjacent cells
# coincidentally spell removed customer numbers and trip a false hard-fail.
_OOXML_VALUE_BOUNDARY = frozenset({"c", "si", "p", "tr"})


# A BARE NUMBER in an attribute is a size, index, count or id -- a column width
# ("8.7109375"), a sheetId, a window dimension. Document values never live there:
# a customer number typed into a cell is stored as element TEXT (<v>). Reading
# them back would be pure noise with a measurable cost: a large workbook carries
# thousands of such numbers, and a 6-digit removed customer number matching a
# substring of one is a spurious HARD FAIL (no output at all) on roughly every
# other big workbook.
_MACHINE_NUMBER_RE = re.compile(r"^[+-]?\d+(?:[.,]\d+)?$")
# Word REVISION-SAVE IDs (w:rsidR, w:rsidRPr, w:rsidTr, ...) are random 8-hex-digit
# session markers Word stamps on nearly every paragraph. Never content, and a
# fresh phantom-match surface on every save.
_RSID_ATTR_PREFIX = "rsid"


def _is_rsid_attr(name: str) -> bool:
    return name.rsplit("}", 1)[-1].lower().startswith(_RSID_ATTR_PREFIX)


def _ooxml_text_with_boundaries(tree, attributes: bool = True) -> str:
    """itertext(), but with a NUL sentinel wrapping every independent value
    container so cross-container concatenation can't forge a literal match.

    Also emits every ATTRIBUTE VALUE (each NUL-wrapped, so one can never glue onto
    its neighbour or onto element text). Reading text only left the backstop with a
    blind spot wide enough to drive a leak through: a sheet NAME lives in
    `xl/workbook.xml <sheet name="...">`, a cell hyperlink's tooltip in
    `<hyperlink tooltip="...">`, a Word field code in `<w:fldSimple w:instr="...">`,
    drawing alt-text in `<docPr descr="...">`, a tracked-change author in
    `<w:ins w:author="...">`. Each of those survived a "verified" write. An
    allow-list of interesting attribute names would just be the same blind spot
    with a shorter name, so ALL of them are read -- a spurious hard fail costs a
    re-run, a missed one ships the customer's name. lxml keeps namespace
    DECLARATIONS out of `.attrib`, so the machine-only xmlns URIs are excluded for
    free; the two remaining pure-plumbing parts are handled by the caller."""
    out: list[str] = []

    def walk(el) -> None:
        tag = el.tag
        local = tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""
        boundary = local in _OOXML_VALUE_BOUNDARY
        if boundary:
            out.append("\x00")
        if attributes and isinstance(tag, str):
            for name, value in el.attrib.items():
                if value and not _MACHINE_NUMBER_RE.match(value) and not _is_rsid_attr(name):
                    out.append(f"\x00{value}\x00")
        if el.text:
            out.append(el.text)
        for child in el:
            walk(child)
            if child.tail:
                out.append(child.tail)
        if boundary:
            out.append("\x00")

    walk(tree)
    return "".join(out)


def _output_text_blob(out_path: Path) -> str:
    """Every readable string in the output, INCLUDING parts the format handlers
    don't normally touch (OOXML metadata, text boxes, numeric cells, every XML
    part, every XML ATTRIBUTE VALUE -- sheet names, hyperlink tooltips, field
    codes, alt-text, tracked-change authors -- external relationship targets, and
    OOXML packages EMBEDDED inside a compressed part; every PDF page plus its
    metadata, form fields, annotations and link URIs). Text nodes are concatenated
    so a value split across runs still appears contiguous -- for the
    recognizer-independent residual check."""
    suffix = out_path.suffix.lower()
    if suffix == ".pdf":
        import fitz

        with fitz.open(out_path) as doc:
            parts: list[str] = []
            meta = doc.metadata or {}
            parts.append(" ".join(str(v) for v in meta.values() if v))  # /Info fields
            try:  # raw XMP packet -- author/creator can live here, not just in /Info
                xref = doc.xref_xml_metadata()
                if xref:
                    parts.append(doc.xref_stream(xref).decode("utf-8", "ignore"))
            except Exception:  # noqa: BLE001
                pass
            for page in doc:
                parts.append(page.get_text())
                # Form-field values and annotation text -- separate from the content
                # stream, so get_text() misses them; the literal backstop must see them.
                try:
                    for w in list(page.widgets() or []):
                        if isinstance(w.field_value, str):
                            parts.append(w.field_value)
                    for a in list(page.annots() or []):
                        parts.append((a.info or {}).get("content", ""))
                    # Link TARGETS: a URI lives in a link annotation, not the
                    # content stream, so get_text() never reaches it.
                    for link in page.get_links():
                        if link.get("uri"):
                            parts.append(link["uri"])
                except Exception:  # noqa: BLE001
                    pass
            return "\n".join(parts)
    if suffix in _OOXML_EXTS:
        with zipfile.ZipFile(out_path) as zf:
            return _ooxml_zip_blob(zf)
    return out_path.read_text(encoding="utf-8", errors="ignore")


# An OOXML package can EMBED another one (a chart's source workbook, an OLE
# object). Its text is compressed inside an already-compressed part, so neither a
# raw byte scan of the outer file nor an XML sweep of its parts can see it.
_EMBEDDED_PACKAGE_RE = re.compile(r"\.(?:xlsx|xlsm|docx|pptx)$", re.IGNORECASE)
_MAX_EMBED_DEPTH = 2

# Parts whose ATTRIBUTES are pure package plumbing or the app's own FORMATTING
# VOCABULARY, never user content: the relationship graph and the content-type
# manifest (part paths, MIME types, relationship ids), and the style / theme /
# font / settings tables, whose attributes are Word's and Excel's own English
# names -- "Hyperlink", "Normal", "heading 1", "Emphasis". Reading those would
# manufacture nothing but spurious HARD FAILS: a recognizer that (quite readily)
# claims "HYPERLINK" as an ORGANIZATION would then refuse to write ANY output for
# every document that uses the built-in hyperlink style.
#
# This is NOT a leak surface: none of these parts is extracted on the way in, so a
# value living only here can never reach the decision set the backstop re-checks.
# It costs coverage only in the absurd case of a value that is decided elsewhere
# AND also happens to be a user-defined STYLE name. Their element TEXT is still
# read, as before. Every other part -- body, worksheets, charts, comments,
# docProps -- is read attributes and all.
_PLUMBING_BASENAMES = frozenset(
    {
        "[content_types].xml",  # NOTE: this set is matched against a LOWER-CASED basename
        "styles.xml",
        "styleswitheffects.xml",
        "settings.xml",
        "websettings.xml",
        "fonttable.xml",
        "numbering.xml",
        "calcchain.xml",
        "tablestyles.xml",
        "presprops.xml",
        "viewprops.xml",
    }
)
_THEME_PART_RE = re.compile(r"(?:^|/)theme/[^/]*\.xml$", re.IGNORECASE)


def _is_plumbing_part(name: str) -> bool:
    base = name.rsplit("/", 1)[-1]
    return (
        name.endswith(".rels")
        or base.lower() in _PLUMBING_BASENAMES
        or bool(_THEME_PART_RE.search(name))
    )


def _ooxml_zip_blob(zf: zipfile.ZipFile, depth: int = 0) -> str:
    parts: list[str] = []
    for name in zf.namelist():
        if name.endswith(".xml") or name.endswith(".rels"):
            try:
                tree = xmlsafe.fromstring(zf.read(name))
            except etree.XMLSyntaxError:
                continue
            parts.append(_ooxml_text_with_boundaries(tree, attributes=not _is_plumbing_part(name)))
            if name.endswith(".rels"):
                # An EXTERNAL relationship target is real user content
                # (mailto:hans.mueller@bank.de, a UNC path holding a customer name).
                # Only External ones: internal targets are package paths
                # ("docProps/core.xml").
                for el in tree.iter():
                    if isinstance(el.tag, str) and el.get("TargetMode") == "External":
                        parts.append(f"\x00{el.get('Target') or ''}\x00")
        elif depth < _MAX_EMBED_DEPTH and _EMBEDDED_PACKAGE_RE.search(name):
            try:
                with zipfile.ZipFile(io.BytesIO(zf.read(name))) as inner:
                    parts.append(_ooxml_zip_blob(inner, depth + 1))
            except (zipfile.BadZipFile, OSError, RuntimeError):
                continue
    return "\n".join(parts)


# A rendered replacement token the tool itself writes: [PERSON_1], [KUNDENNR_3],
# [REDACTED], [PROJEKT_2]. Labels are upper-case by construction (actions.token_label
# / xlsx_handler._column_entity_type), so the pattern is deliberately case-SENSITIVE.
_TOKEN_RUN_RE = re.compile(r"\[[A-Z0-9_]+\]")


def _token_inner_spans(blob: str) -> list[tuple[int, int]]:
    """(start, end) of the text INSIDE each replacement-token run, in `blob`
    coordinates. Sorted and non-overlapping by construction, so a single bisect
    finds the only run that can contain a given offset."""
    return [(m.start() + 1, m.end() - 1) for m in _TOKEN_RUN_RE.finditer(blob)]


def _is_phantom(start: int, end: int, inners: list[tuple[int, int]], starts: list[int]) -> bool:
    """True when a match at [start, end) is the tool's OWN output rather than a leak.

    The one thing the mask is for: a removed value that is a PROPER substring of a
    replacement token's label. The classic case is an NER-misflagged header word
    "Kundennr" (removed as a LOCATION) sitting inside the [KUNDENNR_n] tokens that
    replaced the customer NUMBERS -- reporting that refuses to write ANY file.

    A match that covers a token's ENTIRE inner text, or spills outside it, is NOT
    suppressed: `[FALL_00219384]` in the output is indistinguishable from a minted
    token, and a bank case reference captured WITHOUT its delimiters
    ("FALL_00219384") is exactly the common recognizer shape -- round 1 masked the
    whole run and made that leak invisible. Fail-loud wins the tie: an ambiguous
    full-token match is reported."""
    i = bisect.bisect_right(starts, start) - 1
    if i < 0:
        return False
    ts, te = inners[i]
    return ts <= start and end <= te and not (start == ts and end == te)


def _strip_whitespace_with_map(blob: str) -> tuple[str, list[int]]:
    """The blob with all whitespace removed, plus the original index of each kept
    character -- so a match in the stripped form maps back to a real span and can
    still be phantom-tested."""
    chars: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(blob):
        if not ch.isspace():
            chars.append(ch)
            index.append(i)
    return "".join(chars), index


def _literal_residual(out_path: Path, removed_values: list[str], always_check=()) -> list[str]:
    """Recognizer-INDEPENDENT backstop: for every value the reviewer chose to
    remove, confirm its literal text is truly gone from the WHOLE output, not
    just the body the extractor reads. Catches leaks the re-scan cannot -- a
    name still in docProps, a number still in a cell the extractor skipped.
    Case-insensitive, and also checks a whitespace-stripped form for IDs/IBANs
    that may be reformatted. Values under 4 chars are skipped to avoid false hits
    on common substrings -- EXCEPT terms in `always_check` (the user's deny list),
    which are user-asserted PII and must be verified regardless of length.

    Matches inside the tool's own replacement tokens are suppressed PER VALUE (see
    `_is_phantom`). Round 1 did this by deleting every token run from the haystack
    up front, which had two failure modes -- it erased bracket-free values that
    genuinely survived inside a bracket run, and, once a bracket-bearing value
    forced a run to be kept, it handed EVERY other value a free false match inside
    that run. Deciding per match, per value, has neither."""
    blob = _output_text_blob(out_path)
    always = {v.strip().lower() for v in always_check}
    inners = _token_inner_spans(blob)
    starts = [s for s, _e in inners]
    low = blob.lower()
    blob_ns: str | None = None
    low_ns = ""
    ns_index: list[int] = []

    def _real_match(pattern_value: str, haystack: str, offsets: list[int] | None) -> bool:
        """True if `pattern_value` occurs in `haystack` at least once outside a
        replacement token. `offsets` maps haystack indices back to blob indices
        (identity when None)."""
        for m in re.finditer(re.escape(pattern_value), haystack, re.IGNORECASE):
            if offsets is None:
                start, end = m.start(), m.end()
            elif m.end() > m.start():
                start, end = offsets[m.start()], offsets[m.end() - 1] + 1
            else:
                continue
            if not _is_phantom(start, end, inners, starts):
                return True
        return False

    residual: list[str] = []
    for value in removed_values:
        v = value.strip().lower()
        if len(v) < 4 and v not in always:
            continue
        # Cheap membership test first: the overwhelmingly common (clean) case then
        # costs one substring scan per value, exactly as before, and the precise
        # span-by-span pass only runs for values that are actually present.
        if v in low and _real_match(v, blob, None):
            residual.append(value)
            continue
        # Second haystack with ALL whitespace removed, so an IBAN/ID that the output
        # reformatted ("DE89 3704 ...") is still caught. Built once, lazily.
        v_ns = re.sub(r"\s+", "", v)
        if len(v_ns) < 4:
            continue
        if blob_ns is None:
            blob_ns, ns_index = _strip_whitespace_with_map(blob)
            low_ns = blob_ns.lower()
        if v_ns in low_ns and _real_match(v_ns, blob_ns, ns_index):
            residual.append(value)
    return residual


def apply_document(
    path: Path,
    grouped: list[GroupedFinding],
    analyzer,
    config: dict,
    mapping_db_path: Path | None = None,
    out_dir: Path | None = None,
) -> tuple[Path, Path]:
    decisions = {(g.entity_type, g.value.strip().lower()): g.action for g in grouped}
    # Resolve the output path, create the (possibly not-yet-existing) fixed output
    # folder, and derive the sibling temp INSIDE the try, so a failure here (an
    # unwritable/missing Documents folder, disk full, path too long) surfaces as a
    # ProcessingError like every other pipeline failure -- fail-loud, never a raw
    # OSError escaping the contract. work_path stays None until bound so the
    # except arms don't reference an unbound name if resolution itself failed.
    work_path: Path | None = None
    try:
        out_path = output_path_for(path, out_dir)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a sibling temp so the final file appears only once fully written
        # AND verified -- a failure never leaves a partial/unverified _psd behind,
        # and never clobbers a good prior output. Keep the real extension
        # (….part.docx) so the verifier's format lookup works and the temp file is
        # unmistakably not the final output.
        work_path = out_path.with_name(f"{out_path.stem}.part{out_path.suffix}")
        with tempfile.TemporaryDirectory() as tmp:
            resolved = (
                legacy.convert_to_modern(path, Path(tmp))
                if path.suffix.lower() in legacy.LEGACY_EXTENSIONS
                else path
            )
            handler = _handler_for(resolved)
            units = handler.extract_text_units(resolved)
            cfg = _narrow_language(config, units)
            # Same derivation as scan_document, from the same units + analyzer,
            # so apply redacts exactly the set the reviewer approved.
            cfg = _with_propagation(cfg, units, analyzer)
            cfg = _with_topical_gazetteer(cfg, resolved, handler)
            # The mapping is persisted only after verification PASSES (never on a
            # verify failure -- that would leave orphan pseudonym entries for a
            # document that was never written), and BEFORE the output is committed.
            # If the mapping save fails, no output is committed, so we never ship a
            # file whose [PERSON_1] tokens map back to nothing.
            mapping_store = MappingStore(mapping_db_path)
            try:
                handler.apply(resolved, work_path, decisions, analyzer, cfg, mapping_store)
                # Scrub identifying metadata BEFORE verifying so a name left in
                # docProps is both removed and re-checked.
                _scrub_metadata(work_path)
                residual = verify_output(work_path, decisions, analyzer, cfg)
                removed_values = [g.value for g in grouped if g.action in _REMOVING_ACTIONS]
                literal = _literal_residual(work_path, removed_values, always_check=cfg.get("deny_list", []))
                if residual or literal:
                    parts = []
                    if residual:
                        sample = ", ".join(sorted({f.entity_type for f in residual}))[:200]
                        parts.append(f"{len(residual)} value(s) re-detected ({sample})")
                    if literal:
                        parts.append(f"{len(literal)} removed value(s) still present verbatim in the output")
                    raise ProcessingError(f"Verification failed: {'; '.join(parts)}. No file was written.")
                mapping_store.save()  # persist pseudonyms FIRST (verify already passed)
                os.replace(work_path, out_path)  # commit output only once the mapping is durable
            finally:
                mapping_store.close(save=False)
    except ProcessingError:
        if work_path is not None:
            _cleanup(work_path)
        raise
    except Exception as exc:  # noqa: BLE001
        if work_path is not None:
            _cleanup(work_path)
        raise ProcessingError(f"Could not anonymize '{path.name}': {exc}") from exc

    # `cfg`, not `config`: the provenance must describe the stack that ACTUALLY
    # ran -- language-narrowed, with propagation and the topical gazetteer derived.
    provenance = detection_provenance(cfg)
    # No filename in the audit line. audit.py promises the log holds no original
    # values, and a source document is routinely named after the person it is
    # about ("Kreditakte Hans Mueller.xlsx"). The per-document copy lives in the
    # _report.json next to the output, which is already scoped to that document.
    audit_mod.log_event("apply", provenance)
    report_path = write_report(out_path, grouped, config=config, verified=True, provenance=provenance)
    return out_path, report_path


def detection_provenance(cfg: dict) -> str:
    """A compact, value-free description of WHAT produced a redaction: models,
    versions, profile and the effective cutoffs.

    After GLiNER, a redaction decision depends on which detection stack ran -- the
    same document scanned with ML on and ML off legitimately produces different
    output. Nothing in the written file records that, so "why was this redacted
    and that not?", asked months later by an auditor or by the colleague who
    produced it, had no answer at all.

    Deliberately carries NO values, no filenames and no counts of anything
    sensitive -- only configuration. That is what keeps the audit log safe to
    retain and to hand to someone who is not allowed to see the documents."""
    langs = list(cfg.get("languages") or DEFAULT_LANGUAGES)
    parts = [f"spacy={'+'.join(SPACY_MODELS.get(lang, lang) for lang in langs)}"]

    gl = cfg.get("gliner") or {}
    if gl.get("enabled"):
        # The model is identified by the pack FOLDER, not by a version string --
        # the model pack is user-swappable by design, so the folder name is the
        # only honest identifier of what actually ran.
        try:
            pack = resolve_model_path(gl).name
        except Exception:  # noqa: BLE001 -- provenance must never break a save
            pack = "?"
        parts.append(f"gliner={pack}")
        parts.append(f"gliner_min_score={gl.get('min_score')}")
        parts.append(f"gliner_override={gl.get('confidence_override')}")
    else:
        parts.append("gliner=off")

    tiers = cfg.get("tiers") or {}
    parts.append(f"profile={cfg.get('profile') or 'Balanced (default)'}")
    parts.append(f"sensitivity={cfg.get('sensitivity', 0)}")
    parts.append(f"tiers=high:{tiers.get('high')}/medium:{tiers.get('medium')}")
    parts.append(f"corroboration_only={bool(cfg.get('corroboration_only', True))}")
    return " ".join(parts)


def _cleanup(work_path: Path) -> None:
    try:
        if work_path.exists():
            work_path.unlink()
    except OSError:
        pass
