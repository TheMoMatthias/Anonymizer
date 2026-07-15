@echo off
rem Double-click for first-time setup (dependencies + language models).
rem You normally don't need this - Anonymizer.bat runs it automatically on the
rem first launch - but it's here if you want to set up explicitly.
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
    echo 'uv' is not installed. Install it from https://astral.sh/uv and run this again.
    pause
    exit /b 1
)

powershell -ExecutionPolicy Bypass -File "scripts\setup.ps1"
echo.
echo Done. Launch the app with Anonymizer.bat
pause
