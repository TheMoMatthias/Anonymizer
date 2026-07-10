# Run: anonymizer-v1 (2026-07-10)

Persisted spec/autonomy-contract from the pre-build grill. See conversation history for full rationale.

## Decisions
- GUI: NiceGUI. Presidio: data-privacy-stack/presidio fork.
- Pseudonymize = consistent global mapping table (SQLite, encrypted at rest via Fernet, key in Windows Credential Manager), stored outside any document folder (%LOCALAPPDATA%\Anonymizer\mappings.db).
- Anonymize = one-way removal/redaction, no mapping entry.
- Entities: standard PII + financial identifiers + German tax IDs + bank-internal refs. 5 curated custom recognizers to start.
- Config: YAML source of truth, full CRUD from an in-GUI Settings tab (no hand-editing required).
- Mandatory review gate every run: snippet list, grouped by (entity_type, value), no bypass.
- Formats: docx/xlsm/xlsx/xls/pptx/ppt/doc/pdf. Legacy binary formats (.doc/.xls/.ppt) converted on the fly to modern OOXML via Word/Excel/PowerPoint COM automation. XLSM macros always stripped on output. PDFs use true redaction (PyMuPDF), text-layer only.
- Scan scope: body, tables, headers, footers, footnotes, endnotes, comments, speaker notes, hidden sheets/rows/cols, cell comments, defined names. Known limitation: modern PPTX threaded comments not scanned (legacy comment parts only).
- Output: `<name>_psd.<ext>` next to source + a companion audit report. Overwrite-with-warning on re-run.
- Zero network calls at runtime. One-time spaCy model download during setup only.

## Out of scope (v1)
Scanned/OCR PDFs, batch/multi-file processing, standalone .exe packaging, macro editing/execution, document-rendered preview, cloud/LLM-assisted detection.

## DONE-WHEN
Synthetic-fixture test suite green across doc/docx, xls/xlsx/xlsm, ppt/pptx, pdf (detection, true content removal, round-trip, mapping consistency) + one manual end-to-end GUI walkthrough per format.

## DEFAULTS
Legacy binary formats convert-on-write to OOXML equivalents. Unrecognized extensions rejected with a clear error. Custom recognizer scope stays at the curated 5, not expanded speculatively.

## DEFERRED
Batch processing, OCR, in-GUI document preview, .exe packaging, threaded PPTX comments — resurface only on explicit request.
