# Run: Review-at-scale (column policies + tier bands + virtualization)

Started 2026-07-22. Goal: make reviewing 10k+ findings tractable for a
trust-and-spot-check workflow on large multi-sheet "database" workbooks.

## GOAL
Cut the review burden at scale via (1) per-COLUMN policies (the natural decision
unit for a database, and the only thing that catches topic-sensitive project
columns), (2) TIER-scoped bulk actions, then (3) row virtualization. Reviewer
accepts the bulk with confidence and spot-checks, relying on the fail-loud verify.

## CONTRACT
- **Column policy (spreadsheets):** per-column action `none | pseudonymize |
  anonymize | skip`, chosen in a new "Columns" panel atop the review.
  - `pseudonymize` / `anonymize` = **blackout EVERY non-empty cell** in the column,
    regardless of whether detection found anything (covers project-description text
    with no detectable entity).
  - `skip` = suppress all redaction in that column.
  - `none` (default) = current per-value behavior.
  - Pseudonymize keeps referential integrity via a **per-column entity type derived
    from the header** (→ readable, re-identifiable tokens like `[PROJEKT_1]`; empty
    header → column letter).
  - Applied at apply time, taking **precedence** over value-based decisions.
  - **Per-file only** (no persistence in v1).
- **Tier bands:** a global High/Medium/Low summary control (count + bulk action each)
  that bulk-sets all items of that tier across all classes. **Low is NEVER
  auto-skipped**; `validated is False` (checksum-failed) IDs always stay visible.
  High default = accept.
- **Virtualization (stage 2):** the review row list renders only visible rows.

## LAYERS
- `gui/review.py` — Columns panel, tier-band control, wiring; (stage 2) virtualization.
- `gui/app.py` — thread column policy onto `FileJob`, into scan/apply.
- `formats/xlsx_handler.py` — apply column policy (blackout/skip; synthesize
  per-column whole-cell findings); column identity from `(sheet, column)`.
- `pipeline.apply_document` / `scan_document` — pass column policy through.
- `models.py` — a `ColumnPolicy` structure on the job/config.
- `actions.py` / token label — per-column entity-type token.

## PLAN (stage 1 = column rules + tier bands)
1. Model: `ColumnPolicy` = map `(sheet, column_key) -> action` (+ scope=blackout).
   Carried on `FileJob.config["column_policies"]` so scan/apply parity holds.
2. `xlsx_handler.apply`: for each cell, resolve column policy first — blackout →
   force whole-cell redaction with per-column entity type + action; skip → leave;
   else existing value-based path. Thread policy in; keep the (header,text) memo but
   fold column identity into blackout resolution.
3. `review.py`: Columns panel per sheet — header, a sample value, detected-PII count,
   action select. Derive columns from scan-result unit_ids (`cell|Sheet|A2`) + headers.
4. `review.py`: global tier-band control (High/Medium/Low + bulk action + count),
   operating across all classes; never auto-skip low.
5. Preview reflects column policies.
6. Tests: blackout redacts UNDETECTED cells; skip suppresses; pseudonymize columns
   tokenize consistently; tier bulk sets correctly; low never auto-skipped; a
   blackout+value mix verifies (fail-loud passes).

## TESTS / DONE-WHEN
Full suite green + new tests; a benchmark on a ~10k-distinct workbook shows two
column rules collapse the decision to a handful, blackout redacts undetected cells,
and apply verifies clean.

## DEFAULTS (pre-authorized)
- Per-column pseudonymize token label = sanitized header; empty header → column letter.
- Formula cells: not blacked out in v1 (consistent with detection skipping them); note it.
- Columns panel shown for spreadsheet files only.
- Stage 2: if NiceGUI virtualization is infeasible, fall back to cap+summarize
  (top-N per class + "set remaining to X").

## DEFERRED
Column-rule persistence (per-shape / named profile); promote possible-misses to
redaction; standalone typed-projects feature; PDF-body pseudonym tokens.

## ROLLBACK
Additive; column policy defaults to `none` (= current behavior). Revert commits.

## OUT OF SCOPE
Non-spreadsheet column concept; semantic topic detection; rule persistence.

## PROGRESS
- [x] Stage 1: column BLACKOUT policies (pseudonymize/anonymize) + tier bulk bands (+ tests, 125 pass)
  - `models.ColumnInfo` + `ScanResult.columns`; `xlsx_handler.column_summary` + whole-column
    blackout in `apply` (per-column entity type -> `[HEADER_n]` tokens); `pipeline.scan_document`
    populates columns; `review._columns_panel` + `_tier_bands`; `app` threads
    `job.config["column_policies"]` through to apply. Engine verified: blackout redacts UNDETECTED
    topic-sensitive cells, repeats tokenize consistently, empty cells untouched, fail-loud verify passes.
  - **DEVIATION (surfaced, not silent):** column **"skip"** deferred. The decision model is keyed by
    VALUE not CELL, so it can't express "keep this value in column F but remove it in column C"; a
    skipped column sharing a value with a removed one would trip the fail-loud verify (hard-fail, no
    output). Blackout has no such conflict (cell replaced regardless). Skip needs cell-level decisions
    -> its own follow-up. Per-value skip (class cards) + per-class skip (bulk toggle) still available.
  - GUI import-clean + logic mirrors existing toggles, but the live native webview was NOT run
    headlessly -> user should visually confirm the review screen.
- [x] Stage 2: cap + summarize (chosen over true virtualization -- lower risk, keeps the card design,
      fits trust-and-spot-check). Each class renders at most `_REVIEW_CAP` (100) review rows and 100
      auto rows; the rest is a summary row with a bulk "set these to X" + "Show all" (expand state on
      the ScanResult, per-file). Per-class "Set all" now writes EVERY item incl. capped overflow.
      Headless build-smoke (scratchpad smoke_review.py): 250-item class renders capped (1450 elements)
      and expands (2945) with no build error; live webview still user-verified.
- [ ] Deferred follow-ups: column "skip" (needs cell-level decisions); column-rule persistence;
      promote possible-misses to redaction; true virtualization if cap+summarize proves insufficient.
