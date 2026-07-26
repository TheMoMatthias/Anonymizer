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
uv sync
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
