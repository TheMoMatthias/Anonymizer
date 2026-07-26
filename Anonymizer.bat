@echo off
rem Double-click to launch the Document Anonymizer.
rem Syncs dependencies (fast when already up to date) so a fresh `git pull` just
rem works, then launches.
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
    echo [Anonymizer] 'uv' is not installed.
    echo Install it from https://astral.sh/uv and run this again.
    pause
    exit /b 1
)

echo [Anonymizer] Checking environment ^(first run / after an update downloads models, a few minutes^)...
rem `uv sync` installs EXACTLY the declared dependencies and PRUNES anything else,
rem including optional extras. A plain `uv sync` here therefore uninstalled the ML
rem detection stack on every launch -- so AI detection, once switched on, would
rem hard-fail the next time the app was started from this script. Sync the `ml`
rem extra whenever a model pack is actually present, and stay lean otherwise so a
rem colleague who never uses AI detection is not made to download ~700 MB of torch.
set "AZ_EXTRAS="
if exist "%~dp0vendor\gliner-model" set "AZ_EXTRAS=--extra ml"
if defined ANONYMIZER_GLINER_MODEL set "AZ_EXTRAS=--extra ml"
uv sync %AZ_EXTRAS%
if errorlevel 1 (
    echo [Anonymizer] Environment setup failed - see the messages above.
    pause
    exit /b 1
)

echo [Anonymizer] Starting...
uv run anonymizer
if errorlevel 1 (
    echo.
    echo [Anonymizer] Something went wrong starting the app.
    pause
)
