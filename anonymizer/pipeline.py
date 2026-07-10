from __future__ import annotations

import tempfile
from pathlib import Path

from .engine import group_findings
from .formats import docx_handler, legacy, pdf_handler, pptx_handler, xlsx_handler
from .mapping import MappingStore
from .models import GroupedFinding
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


def _handler_for(path: Path):
    handler = _HANDLERS.get(path.suffix.lower())
    if handler is None:
        raise ValueError(f"Unsupported file type: {path.suffix}")
    return handler


def output_path_for(path: Path) -> Path:
    ext = _OUTPUT_EXT_OVERRIDE.get(path.suffix.lower(), path.suffix.lower())
    return path.with_name(f"{path.stem}_psd{ext}")


def scan_document(path: Path, analyzer, config: dict) -> list[GroupedFinding]:
    with tempfile.TemporaryDirectory() as tmp:
        resolved = legacy.convert_to_modern(path, Path(tmp)) if path.suffix.lower() in legacy.LEGACY_EXTENSIONS else path
        handler = _handler_for(resolved)
        findings = handler.scan(resolved, analyzer, config)
    return group_findings(findings, config)


def apply_document(
    path: Path,
    grouped: list[GroupedFinding],
    analyzer,
    config: dict,
    mapping_db_path: Path | None = None,
) -> tuple[Path, Path]:
    decisions = {(g.entity_type, g.value.strip().lower()): g.action for g in grouped}
    out_path = output_path_for(path)
    with tempfile.TemporaryDirectory() as tmp:
        resolved = legacy.convert_to_modern(path, Path(tmp)) if path.suffix.lower() in legacy.LEGACY_EXTENSIONS else path
        handler = _handler_for(resolved)
        with MappingStore(mapping_db_path) as mapping_store:
            handler.apply(resolved, out_path, decisions, analyzer, config, mapping_store)
    report_path = write_report(out_path, grouped)
    return out_path, report_path
