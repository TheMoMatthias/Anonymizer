# Anonymizer

Local-only desktop tool for anonymizing/pseudonymizing sensitive documents
(Word, Excel, PowerPoint, PDF) before using them as AI/ML input. Built on
[Presidio](https://github.com/data-privacy-stack/presidio) + spaCy, extended
with checksum-validated German bank-specific recognizers, a category-first
review model, and provable output verification. Runs entirely on your
machine — no network calls after setup.

## Setup

Requires [uv](https://astral.sh/uv) and, for legacy `.doc`/`.xls`/`.ppt`
conversion, a local install of Microsoft Office.

```powershell
scripts\setup.ps1
```

This syncs dependencies, installs both spaCy models, and patches NiceGUI's
native drag-and-drop. (If `uv sync` is re-run later it may prune the spaCy
models; re-run `setup.ps1` to restore them.)

## Run

```powershell
scripts\run.ps1
```

Or double-click **`Anonymizer.bat`** — it syncs dependencies first, so a fresh
`git pull` just works. **`Anonymizer.exe`** and **`Install.exe`** are native
equivalents of the two `.bat` files, for machines where policy blocks scripts
but allows executables; see [Launcher executables](#launcher-executables).

Opens the desktop window. **Drag one or more files in** (or click to browse, or
paste a path), pick a **detection profile** if you like, and they enter the
**queue**. Each file is scanned, then you review its findings **by category**,
**Preview changes**, and **Save** an anonymized copy (`Report.docx` →
`Report_psd.docx`) next to the source, with a `_report.json` audit file.

## Review model — decide by category, not by value

Findings are grouped into **data classes** (People, Government IDs, Financial
IDs, Contact, Bank-internal refs, Organizations & places, Dates) and you set one
action per category. Within each category:

- **Auto-accepted** — high-confidence, checksum-validated findings are
  pre-decided and collapsed out of the way.
- **To review** — the uncertain minority (e.g. spaCy name guesses) is surfaced
  for your attention; override any individual value if needed.
- **Possible misses** — sensitive-looking strings that *no* recognizer matched
  are listed (informational) so you can catch false negatives.

Each finding is **pseudonymized** (a consistent `[PERSON_1]` token, reversible
via the mapping) or **anonymized** (one-way `[PERSON]`), or skipped.

## How it works

- **Detection**: Presidio + spaCy (German primary, English secondary), a
  curated allow-list that suppresses structural false positives (Kunde, IBAN,
  Kontonummer…), and custom recognizers for Steuer-ID, SV-Nummer, Kontonummer,
  Depotnummer, and internal bank references.
- **Checksum validation**: IBAN (mod-97), credit cards (Luhn), and German
  Steuer-ID (check digit) are validated — a valid ID is auto-accepted; an
  invalid one is dropped from the actionable list and re-surfaced as a possible
  miss.
- **Pseudonymize**: a consistent `[PERSON_1]` placeholder, the same everywhere a
  value recurs across every document you process. Stored encrypted at
  `%LOCALAPPDATA%\Anonymizer\mappings.db`, keyed via Windows Credential
  Manager — never inside a document folder.
- **Anonymize**: one-way removal, no mapping entry, not reversible.
- **Scan/apply parity + verification**: the same detection runs at scan and
  apply, then the written output is **re-scanned** and the save is blocked if
  any removed-category value survives. Any read/convert/verify failure produces
  **no output at all** — never a falsely-clean file.
- **Re-identify**: the *Re-identify* screen maps `[PERSON_1]`-style tokens in AI
  output back to the originals (confirmation-gated, audit-logged).
- **PDF**: always true redaction (content physically removed via PyMuPDF).
  **Scanned/image PDFs** are OCR'd (via a portable Tesseract, if available) and
  redacted with black boxes over the detected regions; if OCR is unavailable
  they are refused rather than passed through as false-clean.
- **OCR (optional)**: drop a portable Tesseract into the bundle's `tesseract\`
  folder (or set its path in Settings) to enable scanned-PDF support — no
  install, no admin rights. Needs `deu`+`eng` traineddata. See the FAQ.
- **XLSM**: macros are always stripped from the anonymized output.
- **Legacy formats** (`.doc`/`.xls`/`.ppt`): converted to modern OOXML via local
  Office COM automation before processing.

Every run scans headers, footers, footnotes, comments, tracked-change text,
speaker notes, **modern threaded comments**, hidden sheets/rows/columns, cell
comments, and defined names — not just the visible body.

## Settings

The `/settings` page controls entity default actions, confidence thresholds, a
global **sensitivity** slider (recall↔precision), allow/deny lists, custom
recognizers, and **mapping administration** (reset, key rotation, per-entry
erasure for GDPR). Detection **profiles** (Contracts, Client statements, HR
documents, Maximize recall) are chosen per run on the main screen.

## Launcher executables

`Anonymizer.exe` and `Install.exe` sit in the repository root as native twins of
`Anonymizer.bat` and `Install.bat`. They exist for one reason: some managed
Windows machines block `.bat`/`.ps1` execution but still allow signed-or-unsigned
executables, and a `.exe` is the only way to find out whether double-click launch
works there at all.

They are **launchers, not a packaged application** — they still require `uv` and
this checked-out repository next to them, exactly like the `.bat` files do. Each
is self-contained rather than shelling out to its `.bat` twin, so if scripts
*are* blocked the `.exe` still works and the test result means something.

To rebuild after editing `scripts/launcher_src/*.cs`:

```powershell
scripts\build_launchers.ps1
```

That uses the C# compiler built into Windows itself (`.NET Framework 4`, a
Windows component since Windows 8) — no SDK or toolchain to install, and the
~8 KB output needs nothing installed on the target machine. The built `.exe`
files are committed so a `git pull` gets them without a build step.

These are **test scaffolding and safe to delete** (all four files: the two
`.exe`, `scripts/build_launchers.ps1`, `scripts/launcher_src/`) — the `.bat`
files remain the supported path.

## Out of scope (v2)

Standalone `.exe` packaging — i.e. a single-file bundle with Python and the
models inside, needing no `uv`; the launchers above are not that. Also: macro
editing, document-rendered preview, cloud/LLM-assisted detection. (Scanned PDFs
are now supported via optional local OCR, and `scripts\build_offline_bundle.ps1`
covers the air-gapped-install case.)

## Tests

```powershell
uv run pytest
```
