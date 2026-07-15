@echo off
rem Double-click to launch the Document Anonymizer.
rem On first run it sets up the environment automatically.
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
    echo [Anonymizer] 'uv' is not installed.
    echo Install it from https://astral.sh/uv and run this again.
    pause
    exit /b 1
)

if not exist ".venv\" (
    echo [Anonymizer] First run: setting up the environment.
    echo This downloads the language models and may take a few minutes...
    powershell -ExecutionPolicy Bypass -File "scripts\setup.ps1"
    if errorlevel 1 (
        echo [Anonymizer] Setup failed - see the messages above.
        pause
        exit /b 1
    )
)

echo [Anonymizer] Starting...
uv run anonymizer
if errorlevel 1 (
    echo.
    echo [Anonymizer] Something went wrong starting the app.
    pause
)
