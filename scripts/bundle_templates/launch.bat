@echo off
cd /d "%~dp0"
"%~dp0python\runtime\python.exe" -m anonymizer.gui.app
if errorlevel 1 (
    echo.
    echo Something went wrong starting the Document Anonymizer.
    echo Contact Maurice Matthias if this keeps happening.
    pause
)
