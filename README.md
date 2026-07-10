# Anonymizer

Local-only dashboard for anonymizing/pseudonymizing sensitive documents (Word,
Excel, PowerPoint, PDF) before using them as AI input. Built on
[Presidio](https://github.com/data-privacy-stack/presidio), extended with
custom recognizers for German bank-specific identifiers. Runs entirely on
your machine — no network calls after setup.

## Setup

Requires [uv](https://astral.sh/uv) and, for legacy `.doc`/`.xls`/`.ppt`
conversion, a local install of Microsoft Office.

```powershell
scripts\setup.ps1
```

## Run

```powershell
scripts\run.ps1
```

Opens the dashboard in your browser (`http://localhost:8080`). Paste a file
path, click **Scan**, review every detected entity (grouped by type and
value, nothing is applied automatically), adjust actions if needed, then
**Save `_psd`**. The anonymized copy is saved next to the source file
(`Report.docx` → `Report_psd.docx`), along with a `_report.json` audit file.

## How it works

- **Detection**: Presidio + spaCy (German primary, English secondary) plus a
  small curated set of custom recognizers (`config/default_recognizers.yaml`)
  for Steuer-ID, SV-Nummer, Kontonummer, Depotnummer, and internal bank
  reference formats.
- **Pseudonymize**: replaces a value with a consistent placeholder
  (`PERSON_1`, `IBAN_1`, ...) that's the same everywhere it recurs, across
  every document you ever process. The mapping is stored encrypted at
  `%LOCALAPPDATA%\Anonymizer\mappings.db`, keyed via Windows Credential
  Manager — never inside a document folder.
- **Anonymize**: one-way removal, no mapping entry, not reversible.
- **PDF**: always true redaction (content physically removed via PyMuPDF),
  regardless of the chosen action — PDFs have no reversible mode.
- **XLSM**: macros are always stripped from the anonymized output.
- **Legacy formats** (`.doc`/`.xls`/`.ppt`): converted to modern OOXML via
  local Office COM automation before processing; the anonymized copy is
  saved in the modern format (`Report.doc` → `Report_psd.docx`).

Every run scans headers, footers, footnotes, comments, tracked-change text,
speaker notes, hidden sheets/rows/columns, cell comments, and defined names —
not just the visible body. **Known limitation**: modern PowerPoint threaded
comments aren't scanned (only the legacy comment-part format is).

## Settings

The `/settings` page gives full control over entity default actions,
confidence thresholds, allow/deny lists, and custom recognizers — no need to
hand-edit the YAML config, though you still can
(`%LOCALAPPDATA%\Anonymizer\config.yaml`).

## Out of scope (v1)

Scanned/OCR PDFs, batch/multi-file processing, standalone `.exe` packaging,
macro editing, document-rendered preview, cloud/LLM-assisted detection.

## Tests

```powershell
uv run pytest
```
