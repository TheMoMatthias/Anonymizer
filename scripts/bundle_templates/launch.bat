@echo off
cd /d "%~dp0"
rem If a portable Tesseract was dropped into the bundle's tesseract\ folder,
rem point the app at it (enables OCR for scanned PDFs, no install needed).
if exist "%~dp0tesseract\tesseract.exe" set "ANONYMIZER_TESSERACT=%~dp0tesseract\tesseract.exe"
"%~dp0python\runtime\python.exe" -m anonymizer.gui.app
if errorlevel 1 (
    echo.
    echo Something went wrong starting the Document Anonymizer.
    echo Contact Maurice Matthias if this keeps happening.
    pause
)
