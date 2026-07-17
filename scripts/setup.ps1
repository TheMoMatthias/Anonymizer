if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "uv is not installed. Install it from https://astral.sh/uv before running this script."
    exit 1
}
# uv sync installs everything, including the two spaCy models (they are declared
# as direct-URL dependencies in pyproject.toml, so they are installed here and
# never pruned by a later sync -- a plain `spacy download` needs pip, which the
# uv-managed venv does not have).
uv sync
if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }

# Note: drag-and-drop now uses NiceGUI's built-in ui.upload (an in-page dropzone),
# so no dependency patching is required. The old patch_nicegui_drop.py step was
# removed -- see docs/run_dragdrop-uiupload_2026-07-17.md.

Write-Host ""
Write-Host "Setup complete. Double-click Anonymizer.bat (or run scripts\run.ps1) to start the app."
