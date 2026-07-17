from __future__ import annotations

from pathlib import Path

_WORD_DOCX = 12
_EXCEL_XLSX = 51
_PPT_PPTX = 24

_APP_MAP = {
    ".doc": ("Word.Application", "Documents", _WORD_DOCX, ".docx"),
    ".xls": ("Excel.Application", "Workbooks", _EXCEL_XLSX, ".xlsx"),
    ".ppt": ("PowerPoint.Application", "Presentations", _PPT_PPTX, ".pptx"),
}

LEGACY_EXTENSIONS = set(_APP_MAP)


def convert_to_modern(path: Path, out_dir: Path) -> Path:
    """Converts a legacy binary Office file to its modern OOXML equivalent via
    local COM automation (requires MS Office installed). Used because no
    pure-Python library can write the legacy binary formats."""
    import win32com.client as win32

    ext = path.suffix.lower()
    if ext not in _APP_MAP:
        raise ValueError(f"Not a legacy format: {ext}")
    app_name, collection_name, file_format, new_ext = _APP_MAP[ext]
    out_path = out_dir / (path.stem + new_ext)

    app = win32.gencache.EnsureDispatch(app_name)
    app.Visible = False
    # These files come from external clients (untrusted). Force-disable macros
    # (msoAutomationSecurityForceDisable=3) so an auto-macro can't run on Open, and
    # suppress modal alert dialogs (password / repair / update-links prompts) that
    # would otherwise hang a headless, Visible=False app forever. Best-effort: the
    # available properties differ per Office application.
    for prop, value in (("AutomationSecurity", 3), ("DisplayAlerts", False), ("AskToUpdateLinks", False)):
        try:
            setattr(app, prop, value)
        except Exception:  # noqa: BLE001 -- not every app exposes every property
            pass
    try:
        collection = getattr(app, collection_name)
        doc = collection.Open(str(path))
        try:
            doc.SaveAs(str(out_path), FileFormat=file_format)
        finally:
            if collection_name == "Presentations":
                doc.Close()
            else:
                doc.Close(False)
    finally:
        app.Quit()
    return out_path
