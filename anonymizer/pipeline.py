from __future__ import annotations

import os
import tempfile
from pathlib import Path

from . import core
from . import language
from . import ocr as ocr_mod
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
    langs = config.get("languages") or ["de"]
    if len(langs) <= 1:
        return config
    sample = " ".join(u.text for u in units[:80])
    lang, confident = language.detect_dominant(sample)
    chosen = lang if (confident and lang in langs) else langs[0]
    narrowed = dict(config)
    narrowed["languages"] = [chosen]
    return narrowed


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
            with MappingStore(mapping_db_path) as mapping_store:
                handler.apply(resolved, work_path, decisions, analyzer, cfg, mapping_store)
            residual = verify_output(work_path, decisions, analyzer, cfg)
            if residual:
                sample = ", ".join(sorted({f.entity_type for f in residual}))[:200]
                raise ProcessingError(
                    f"Verification failed: {len(residual)} sensitive value(s) still present in the "
                    f"output ({sample}). No file was written."
                )
        os.replace(work_path, out_path)
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
