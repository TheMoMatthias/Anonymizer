from __future__ import annotations

from pathlib import Path

_WORD_DOCX = 12
_EXCEL_XLSX = 51
_PPT_PPTX = 24

# A password the user cannot plausibly have chosen. Passing it to Open() makes an
# ENCRYPTED file fail immediately with a COM error instead of raising a modal
# password prompt -- which is the whole point: a Visible=False, headless Office
# instance renders that dialog offscreen and then waits for input that can never
# arrive, wedging the conversion (and the calling worker) forever. DisplayAlerts
# does NOT suppress the password prompt; only supplying a password does.
_NO_PASSWORD = "\x00anonymizer-refuses-encrypted-input\x00"

# Per-application Open() keyword arguments. The parameter names differ per Office
# app, so they cannot be shared. Common intent: never prompt, never modify the
# source, never touch the user's recent-files list, never follow external links.
_WORD_OPEN = {
    "ConfirmConversions": False,
    "ReadOnly": True,
    "AddToRecentFiles": False,
    "PasswordDocument": _NO_PASSWORD,
    "WritePasswordDocument": _NO_PASSWORD,
    "Visible": False,
}
_EXCEL_OPEN = {
    "UpdateLinks": 0,  # never resolve external links (they can reach the network)
    "ReadOnly": True,
    "AddToRecentFiles": False,
    "Password": _NO_PASSWORD,
    "WriteResPassword": _NO_PASSWORD,
    "IgnoreReadOnlyRecommended": True,
}
# PowerPoint's Open() exposes no password parameter at all, so an encrypted .ppt
# cannot be refused this way -- see convert_to_modern's docstring.
_PPT_OPEN = {"ReadOnly": True, "Untitled": False, "WithWindow": False}

_APP_MAP = {
    ".doc": ("Word.Application", "Documents", _WORD_DOCX, ".docx", _WORD_OPEN),
    ".xls": ("Excel.Application", "Workbooks", _EXCEL_XLSX, ".xlsx", _EXCEL_OPEN),
    ".ppt": ("PowerPoint.Application", "Presentations", _PPT_PPTX, ".pptx", _PPT_OPEN),
}

LEGACY_EXTENSIONS = set(_APP_MAP)


class EncryptedLegacyFileError(Exception):
    """A legacy binary file is password-protected, so it cannot be converted."""


def _looks_like_password_failure(exc: Exception) -> bool:
    """COM surfaces a rejected password as a generic automation error, so the
    message is the only signal available. Matching is deliberately broad: the
    cost of a false positive is a clearer-but-wrong error message, while the cost
    of a false negative is the user seeing a raw COM traceback."""
    text = str(exc).lower()
    return any(t in text for t in ("password", "kennwort", "protected", "geschützt", "geschuetzt"))


def convert_to_modern(path: Path, out_dir: Path) -> Path:
    """Converts a legacy binary Office file to its modern OOXML equivalent via
    local COM automation (requires MS Office installed). Used because no
    pure-Python library can write the legacy binary formats.

    These files come from external clients and are therefore untrusted, so the
    application is configured to run inert: macros force-disabled, alerts and
    link-updates suppressed, and a sentinel password supplied so an ENCRYPTED
    file fails fast instead of blocking on a modal prompt no one can answer.

    Residual limitation: PowerPoint's Open() takes no password argument, so an
    encrypted .ppt can still raise a prompt inside a headless instance. The .doc
    and .xls paths -- the formats that actually appear in bank workbooks -- are
    covered.
    """
    import win32com.client as win32

    ext = path.suffix.lower()
    if ext not in _APP_MAP:
        raise ValueError(f"Not a legacy format: {ext}")
    app_name, collection_name, file_format, new_ext, open_kwargs = _APP_MAP[ext]
    out_path = out_dir / (path.stem + new_ext)

    app = win32.gencache.EnsureDispatch(app_name)
    # Force-disable macros (msoAutomationSecurityForceDisable=3) so an auto-macro
    # can't run on Open, and suppress the modal alerts (repair / update-links)
    # that would hang a headless app. Best-effort: the available properties
    # differ per application -- notably PowerPoint REJECTS Visible=False, so that
    # assignment must be guarded too rather than left to raise.
    for prop, value in (
        ("Visible", False),
        ("AutomationSecurity", 3),
        ("DisplayAlerts", False),
        ("AskToUpdateLinks", False),
        ("EnableEvents", False),
    ):
        try:
            setattr(app, prop, value)
        except Exception:  # noqa: BLE001 -- not every app exposes every property
            pass
    try:
        collection = getattr(app, collection_name)
        try:
            doc = collection.Open(str(path), **open_kwargs)
        except Exception as exc:  # noqa: BLE001 -- classify, then re-raise
            if _looks_like_password_failure(exc):
                raise EncryptedLegacyFileError(
                    f"'{path.name}' is password-protected and cannot be converted. "
                    "Remove the password in Office and save a copy, then anonymize that copy."
                ) from exc
            raise
        try:
            doc.SaveAs(str(out_path), FileFormat=file_format)
        finally:
            if collection_name == "Presentations":
                doc.Close()
            else:
                doc.Close(False)
    finally:
        # Runs even when Open() itself failed, so a refused encrypted file can
        # never leave an orphaned invisible Office process behind.
        app.Quit()
    return out_path
