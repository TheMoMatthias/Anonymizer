@echo off
cd /d "%~dp0"
rem If a portable Tesseract was dropped into the bundle's tesseract\ folder,
rem point the app at it (enables OCR for scanned PDFs, no install needed).
if exist "%~dp0tesseract\tesseract.exe" set "ANONYMIZER_TESSERACT=%~dp0tesseract\tesseract.exe"
rem If the GLiNER model was bundled into gliner-model\, point the app at it so
rem AI detection (when enabled in Settings) can load it with no download.
if exist "%~dp0gliner-model" set "ANONYMIZER_GLINER_MODEL=%~dp0gliner-model"
"%~dp0python\runtime\python.exe" -m anonymizer.gui.app
if errorlevel 1 (
    echo.
    echo Something went wrong starting the Document Anonymizer.
    echo Contact Maurice Matthias if this keeps happening.
    pause
)
