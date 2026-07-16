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
from .engine import DEFAULT_LANGUAGES
from .actions import decisions_lookup
from .formats import docx_handler, legacy, pdf_handler, pptx_handler, xlsx_handler
from .mapping import MappingStore
from .models import Finding, GroupedFinding, ScanResult
from .report import write_report

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


class ProcessingError(Exception):
    """Raised when a document cannot be processed safely. The tool never emits a
    partial or unverified `_psd` file -- better no output than a falsely-clean
    one."""


def _handler_for(path: Path):
    handler = _HANDLERS.get(path.suffix.lower())
    if handler is None:
        raise ProcessingError(f"Unsupported file type: {path.suffix}")
    return handler


def output_path_for(path: Path) -> Path:
    ext = _OUTPUT_EXT_OVERRIDE.get(path.suffix.lower(), path.suffix.lower())
    return path.with_name(f"{path.stem}_psd{ext}")


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


def _narrow_language(config: dict, units: list) -> dict:
    """Collapses a multi-language config to the single detected language so only
    ONE spaCy NER model runs -- this is the fix for the English model flagging
    ordinary German words. Deterministic on the text, so scan and apply pick the
    same language and stay in parity. A config already pinned to one language
    (e.g. chosen in the GUI) is returned unchanged."""
    langs = config.get("languages") or list(DEFAULT_LANGUAGES)
    if len(langs) <= 1:
        return config
    sample = " ".join(u.text for u in units[:80])
    lang, confident = language.detect_dominant(sample)
    chosen = lang if (confident and lang in langs) else langs[0]
    narrowed = dict(config)
    narrowed["languages"] = [chosen]
    return narrowed


# Entity types worth propagating document-wide. Only free-text NER types: a
# structured ID either matches its pattern everywhere or nowhere, so it has
# nothing to gain and would only add false positives.
_PROPAGATABLE = ("PERSON",)
_MIN_PROPAGATE_LEN = 4
_HONORIFIC_PREFIX = re.compile(r"^(?:Herr|Frau|Hr\.|Fr\.|Dr\.|Prof\.)\s+")


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
    for unit in units:
        for f in core.detect_unit(analyzer, unit, config):
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
        return language.detect_dominant(" ".join(u.text for u in units[:80]))
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
            findings = handler.scan(resolved, analyzer, cfg)
    except ProcessingError:
        raise
    except Exception as exc:  # noqa: BLE001 -- fail loud, never silently pass
        raise ProcessingError(f"Could not read '{path.name}': {exc}") from exc
    return core.build_scan_result(findings, units, cfg)


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

        with fitz.open(out_path) as doc:
            doc.set_metadata({})  # clears author/title/subject/keywords/creator/producer
            doc.saveIncr()
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
            tree = etree.fromstring(contents[part])
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
            return "\n".join(page.get_text() for page in doc)
    if suffix in _OOXML_EXTS:
        parts = []
        with zipfile.ZipFile(out_path) as zf:
            for name in zf.namelist():
                if not (name.endswith(".xml") or name.endswith(".rels")):
                    continue
                try:
                    tree = etree.fromstring(zf.read(name))
                except etree.XMLSyntaxError:
                    continue
                parts.append("".join(tree.itertext()))
        return "\n".join(parts)
    return out_path.read_text(encoding="utf-8", errors="ignore")


def _literal_residual(out_path: Path, removed_values: list[str]) -> list[str]:
    """Recognizer-INDEPENDENT backstop: for every value the reviewer chose to
    remove, confirm its literal text is truly gone from the WHOLE output, not
    just the body the extractor reads. Catches leaks the re-scan cannot -- a
    name still in docProps, a number still in a cell the extractor skipped.
    Case-insensitive, and also checks a whitespace-stripped form for IDs/IBANs
    that may be reformatted. Values under 4 chars are skipped to avoid false
    hits on common substrings."""
    blob = _output_text_blob(out_path)
    low = blob.lower()
    low_ns = re.sub(r"\s+", "", low)
    residual: list[str] = []
    for value in removed_values:
        v = value.strip().lower()
        if len(v) < 4:
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
) -> tuple[Path, Path]:
    decisions = {(g.entity_type, g.value.strip().lower()): g.action for g in grouped}
    out_path = output_path_for(path)
    # Write to a sibling temp so the final file appears only once fully written
    # AND verified -- a failure never leaves a partial/unverified _psd behind,
    # and never clobbers a good prior output.
    # Keep the real extension (…​.part.docx) so the verifier's format lookup works
    # and the temp file is unmistakably not the final output.
    work_path = out_path.with_name(f"{out_path.stem}.part{out_path.suffix}")
    try:
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
            # The mapping is persisted ONLY after the verified output is
            # committed (os.replace). Otherwise a verify failure -- no file
            # written -- would still leave orphan pseudonym entries and advance
            # placeholder numbers for a document that never existed.
            mapping_store = MappingStore(mapping_db_path)
            try:
                handler.apply(resolved, work_path, decisions, analyzer, cfg, mapping_store)
                # Scrub identifying metadata BEFORE verifying so a name left in
                # docProps is both removed and re-checked.
                _scrub_metadata(work_path)
                residual = verify_output(work_path, decisions, analyzer, cfg)
                removed_values = [g.value for g in grouped if g.action in _REMOVING_ACTIONS]
                literal = _literal_residual(work_path, removed_values)
                if residual or literal:
                    parts = []
                    if residual:
                        sample = ", ".join(sorted({f.entity_type for f in residual}))[:200]
                        parts.append(f"{len(residual)} value(s) re-detected ({sample})")
                    if literal:
                        parts.append(f"{len(literal)} removed value(s) still present verbatim in the output")
                    raise ProcessingError(f"Verification failed: {'; '.join(parts)}. No file was written.")
                os.replace(work_path, out_path)
                mapping_store.save()  # commit pseudonyms only now that the file exists
            finally:
                mapping_store.close(save=False)
    except ProcessingError:
        _cleanup(work_path)
        raise
    except Exception as exc:  # noqa: BLE001
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
