from __future__ import annotations

import os
import re
import tempfile
import zipfile
from pathlib import Path

from lxml import etree

from . import core
from . import language
from . import ocr as ocr_mod
from . import xmlsafe
from .engine import DEFAULT_LANGUAGES
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
            value = _HONORIFIC_PREFIX.sub("", f.value).strip()
            if len(value) >= _MIN_PROPAGATE_LEN:
                values.add((f.entity_type, value))
            # Also seed the surname alone: NER reliably catches "Björn Müller"
            # in prose but misses a bare "Müller" in a cell -- and the bare form
            # is precisely the measured gap.
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
    was supposed to be removed -- i.e. a leak. Empty list == verified clean."""
    handler = _handler_for(out_path)
    residual: list[Finding] = []
    for f in handler.scan(out_path, analyzer, config):
        if decisions_lookup(decisions, f.entity_type, f.value) in _REMOVING_ACTIONS:
            residual.append(f)
    return residual


_OOXML_EXTS = (".docx", ".xlsx", ".xlsm", ".pptx")
_OOXML_META_PARTS = ("docProps/core.xml", "docProps/app.xml")
# Identifying metadata fields (OOXML local tag names) the body-text redaction
# never touches -- author / last editor / manager / company routinely carry the
# real advisor or author name.
_META_CLEAR_TAGS = frozenset({"creator", "lastModifiedBy", "manager", "company", "lastPrinted"})


def _scrub_metadata(out_path: Path) -> None:
    """Blanks identifying document metadata so a real name in docProps/PDF-info
    can't ride along in a file marked 'verified' (the body-text redaction and
    the recognizer re-scan both read body text only)."""
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
        for el in tree.iter():
            if etree.QName(el).localname in _META_CLEAR_TAGS and (el.text or ""):
                el.text = ""
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


def _ooxml_text_with_boundaries(tree) -> str:
    """itertext(), but with a NUL sentinel wrapping every independent value
    container so cross-container concatenation can't forge a literal match."""
    out: list[str] = []

    def walk(el) -> None:
        tag = el.tag
        local = tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""
        boundary = local in _OOXML_VALUE_BOUNDARY
        if boundary:
            out.append("\x00")
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
    part; every PDF page). Text nodes are concatenated so a value split across
    runs still appears contiguous -- for the recognizer-independent residual
    check."""
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
                except Exception:  # noqa: BLE001
                    pass
            return "\n".join(parts)
    if suffix in _OOXML_EXTS:
        parts = []
        with zipfile.ZipFile(out_path) as zf:
            for name in zf.namelist():
                if not (name.endswith(".xml") or name.endswith(".rels")):
                    continue
                try:
                    tree = xmlsafe.fromstring(zf.read(name))
                except etree.XMLSyntaxError:
                    continue
                parts.append(_ooxml_text_with_boundaries(tree))
        return "\n".join(parts)
    return out_path.read_text(encoding="utf-8", errors="ignore")


def _literal_residual(out_path: Path, removed_values: list[str], always_check=()) -> list[str]:
    """Recognizer-INDEPENDENT backstop: for every value the reviewer chose to
    remove, confirm its literal text is truly gone from the WHOLE output, not
    just the body the extractor reads. Catches leaks the re-scan cannot -- a
    name still in docProps, a number still in a cell the extractor skipped.
    Case-insensitive, and also checks a whitespace-stripped form for IDs/IBANs
    that may be reformatted. Values under 4 chars are skipped to avoid false hits
    on common substrings -- EXCEPT terms in `always_check` (the user's deny list),
    which are user-asserted PII and must be verified regardless of length."""
    blob = _output_text_blob(out_path)
    # Mask the tool's OWN replacement tokens ([PERSON_1], [KUNDENNR_3], [REDACTED], ...)
    # before searching. A removed value that is a substring of a token is NOT a leak
    # -- it is the anonymized output: e.g. an NER-misflagged header word "Kundennr"
    # (removed as a LOCATION) is a substring of the [KUNDENNR_n] tokens that replaced
    # the customer NUMBERS, so an unmasked substring scan reports a phantom leak and
    # the fail-loud gate refuses to write ANY file. The sentinel (NUL: never in real
    # text, not whitespace so the stripped form can't bridge across it) replaces each
    # token so it neither matches a removed value nor joins two real fragments. A real
    # leak sitting OUTSIDE a token is untouched and still caught.
    blob = re.sub(r"\[[A-Z0-9_]+\]", "\x00", blob)
    low = blob.lower()
    low_ns = re.sub(r"\s+", "", low)
    always = {v.strip().lower() for v in always_check}
    residual: list[str] = []
    for value in removed_values:
        v = value.strip().lower()
        if len(v) < 4 and v not in always:
            continue
        v_ns = re.sub(r"\s+", "", v)
        if v in low or (len(v_ns) >= 4 and v_ns in low_ns):
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

    report_path = write_report(out_path, grouped, config=config, verified=True)
    return out_path, report_path


def _cleanup(work_path: Path) -> None:
    try:
        if work_path.exists():
            work_path.unlink()
    except OSError:
        pass
