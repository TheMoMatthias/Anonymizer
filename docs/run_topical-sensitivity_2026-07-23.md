# Run: Topical sensitivity + cell-level granularity + summarize mode (2026-07-23)

Spec from a full design grill (28 questions / 7 rounds). Purpose: prepare
documents so an INTERNAL LLM can work with their structure/format while sensitive
content — personal AND organizational (internal tools, divisions, departments,
licensees, confidential project descriptions) — is withheld. Offline, no LLM.

## GOAL
Detect and redact/transform NON-personal "topical" sensitivity (tools, divisions,
departments, licensees, project descriptions) with a new CELL-LEVEL granularity
and a new SUMMARIZE mode, so a reviewer can withhold sensitive content while
keeping enough structure for a downstream internal LLM.

## CONTRACT
- New topical categories (entity types): TOOL, DIVISION, DEPARTMENT, LICENSEE,
  PROJECT — each its own type with a per-category default action, grouped under
  one "Internal / topical" data class in the review UI.
- Detection is AUTOMATIC and structure-driven (no manual lists required):
  1. **Header→category**: a column whose header matches a category's header-terms
     makes every non-empty cell in it that category. (Generalizes
     `name_column_headers`⇒PERSON to categories.)
  2. **Auto-gazetteer**: the values found in category-labelled columns are
     collected during the scan.
  3. **Category propagation**: those learned terms propagate document-wide
     carrying their category (reuses the now-filter-safe propagation engine).
  4. **Manual gazetteer**: optional supplement only.
  5. **NER candidates**: capitalized/OOV tokens not in a labelled column surface
     in an informational "Possible internal/topical terms" cluster (promote-or-
     skip; never auto-redacted).
- **Corroboration bypass**: a gazetteer/header match is always kept — exempt from
  the corroboration-only drop and the NER noise filters (it is user/structure-
  confirmed, not a bare guess).
- **Redaction modes** (column & cell): skip / pseudonymize (`[TOOL_1]`) / redact
  (`[TOOL]`) / **summarize** (structural placeholder, zero original content).
- **Cell policy**: `cell_policies = {"Sheet!A5": mode}` on `job.config`, alongside
  `column_policies`. Cell policy overrides column policy for that cell. It is the
  EXCEPTION layer — default review stays column + per-value; cells surface only
  when flagged/overridden, capped like `_REVIEW_CAP`.
- **Summarize output**: pure structural metadata (type, sentence/line/entry count,
  char size), e.g. `[Freitext: 3 Sätze, ~140 Zeichen]`. Contains ZERO original
  characters, so `verify_output` + `_literal_residual` pass by construction.
- **Per-category defaults**: TOOL/DIVISION/DEPARTMENT/LICENSEE → pseudonymize;
  PROJECT → summarize. All user-overridable per category/column/cell.

## LAYERS
- Detection: `core.py` (category types, gazetteer/candidate handling,
  corroboration bypass), `engine.py`/config (header-term maps, category
  recognizers), `pipeline.py` (`_with_propagation` generalized to categories),
  `xlsx_handler.py` (header→category, auto-gazetteer seed, cell iteration).
- Transform: `actions.py` (`resolve_replacement` new `summarize`/`redact` seam),
  a new structural-summary builder (zero original content).
- Granularity: `xlsx_handler.apply()` (cell_policies path, mirrors column_policies),
  `models.py` (CellInfo, category taxonomy).
- Review UI: `review.py` (Cells cluster + Internal/topical parent cluster + modes
  in `_labeled_toggle`), `app.py` (cell_policies wiring), `build_preview`
  (cell→placeholder before/after).
- Config: one consolidated `topical` block, German defaults, `config_schema_version`
  bump + `_resync_builtins` so existing users get it (preserving user data).

## PLAN (phased, checkpoint after each)
- **Phase A — Topical detection**: category types + taxonomy (Internal/topical
  data class), header→category, auto-gazetteer seed, category propagation,
  corroboration bypass, "possible topical" candidate cluster. Detection only;
  default action redact until B.
- **Phase B — Redaction modes**: `summarize` (structural placeholder) + `redact`
  seam in `resolve_replacement`; verify-safety (zero original content); apply
  the per-category defaults.
- **Phase C — Cell-level granularity + UI**: `cell_policies` (Sheet!Coord) in
  xlsx apply; Cells cluster in review; cell→placeholder preview.

## TESTS (DONE-WHEN)
All 170 existing tests stay green, PLUS new tests: header→category detection;
auto-gazetteer seed + category propagation (with parity); corroboration bypass
keeps a gazetteer term that a bare guess would be dropped; summarize placeholder
contains zero original content; `verify_output`/`_literal_residual` pass on a
summarized output; cell_policies apply + override column policy; scan/apply parity
for the whole topical pipeline. Verified end-to-end on a realistic mixed German
business spreadsheet (tools/divisions/project-description columns).

## DEFAULTS (pre-authorized)
- Per-category default actions as above (tools/etc. pseudonymize; PROJECT
  summarize).
- Summarize placeholder format: German structural descriptor, zero content.
- Any mid-build ambiguity: favor the safe-by-default (withhold content) choice,
  consistent with the compliance stance; never emit original text in a summary.

## DEFERRED (revisit on trigger)
- docx/pptx TABLE-CELL policies (needs new cell identity in those handlers) —
  topical DETECTION still works in their flowing text via gazetteer/propagation.
- PDF block-level policy (no block unit).
- Persistent cross-document learned gazetteer (data-retention decision).
- Settings-tab editing UI for header-terms/gazetteers if not reached in a phase.

## ROLLBACK
- All changes gated behind the new `topical` config block; a `topical.enabled:
  false` (or absent) disables the subsystem, restoring current behavior.
- `corroboration_only` and existing filters unchanged in their own right.
- Config migration is additive/versioned; user data preserved.

## OUT OF SCOPE
- Any LLM/network dependency. Abstractive summaries. Detecting topical terms that
  appear ONLY in free prose and never in a labelled column (surfaced as NER
  candidates for manual promotion, not auto-detected).

## Status: PHASE A DONE (detection) — awaiting go-ahead for Phase B.

### Phase A delivered (2026-07-23)
- Taxonomy: TOOL/DIVISION/DEPARTMENT/LICENSEE/PROJECT + POSSIBLE_TOPICAL entity
  types; new "Internal / topical" data class + "Possible internal/topical terms"
  candidate class (`taxonomy.py`).
- Config: `topical` block (per-category header_terms + manual terms, `enabled`
  flag) + entity entries with per-category default actions; schema bumped to 3
  (existing users migrate via `_resync_builtins`, user data preserved).
- Header→category detection (`xlsx_handler._category_for_header`) — WORD-BOUNDARY
  matching (a category column drives whole-column redaction, so precision-first;
  'Produktgruppe' no longer matches 'gruppe'). Trimmed ambiguous default terms.
- Whole-cell topical override (`xlsx_handler._analyze_cell_text`) — a category
  column claims each cell as that category; runs in scan AND apply (parity).
- Auto-gazetteer (`xlsx_handler.topical_gazetteer`) — learns name-shaped values
  from category columns; `pipeline._with_topical_gazetteer` folds them (+ manual
  terms) into propagation, so a tool named in a Tools column is redacted where it
  recurs in a description cell (verified: count>=2). Manual terms propagate in
  ALL formats; auto-header-gazetteer is xlsx-only (per scope).
- Topical types are structural, excluded from the Presidio `analyze` call (no
  more "no recognizer" warnings) and from the NER noise/corroboration filters,
  so they are always kept (the corroboration bypass).
- **Fixed a pre-existing interaction bug** surfaced here: propagation was being
  treated as corroboration (`_absorb_corroborating_source` + the is_ner_guess
  aggregation), so a common word NER tagged PERSON in one cell propagated and
  vouched for an ORG hit on the same word elsewhere, defeating corroboration-only
  ("Sparen"). Propagation is now correctly NOT corroboration.
- Default action PROJECT = anonymize (redact) until Phase B adds `summarize`.
- 177 tests green (170 + 7 new topical). Marker `0fb794`. Verified end-to-end on
  a synthetic tools/divisions/licensees/projects + description spreadsheet.

### Phase B delivered (2026-07-23)
- `summarize` mode (`actions.resolve_replacement` + `_structural_summary`): a
  cell becomes a zero-content structural placeholder, e.g. `[TEXT: 3 Sätze,
  ~87 Zeichen]` / `[PROJEKT: Liste, 5 Einträge]`. No original characters, so the
  fail-loud verify passes by construction (tested).
- Mode vocabulary now skip / pseudonymize / redact / summarize in the review
  toggle (`anonymize` value kept, displays as "Redact"); `summarize` added to
  `_COLUMN_BLACKOUT_ACTIONS`, `theme.ACTION_COLORS`, `build_preview` (shows the
  exact placeholder before Save).
- **Category split** (found during E2E: a project NAME was being summarized,
  and a "Projektbeschreibung" column's prose LEAKED because word-boundary
  'projekt' misses the compound): added a **DESCRIPTION** category for free-text
  columns (Beschreibung/Notiz/Kommentar/OldValue/... incl. compounds) →
  summarize; PROJECT (names) → pseudonymize. DESCRIPTION is whole-cell only,
  excluded from gazetteer propagation (`taxonomy.PROPAGATING_TOPICAL_TYPES`).
- **Config re-sync fix**: `_resync_builtins` now also re-syncs the code-owned
  topical block (which categories + header_terms) on a schema bump, preserving
  user manual `terms` + `enabled` — so a shipped category added later
  (DESCRIPTION) reaches existing users. Verified v2→v5 migration adds DESCRIPTION
  while preserving a user's manual TOOL term + sensitivity. Schema bumped to 5.
- E2E verified: a Projektname/Tool/Projektbeschreibung sheet →
  `[PROJEKT_1]`, `[TOOL_1]`, `[TEXT: 3 Sätze, ~87 Zeichen]` (structure kept,
  content withheld). 180 tests green. Marker `69b9a4`.

### Phase C delivered (2026-07-23)
- Per-CELL policy (`cell_policies = {"Sheet!A5": mode}`) — the finest-grained
  EXCEPTION layer, applied in `xlsx_handler.apply()` BEFORE the column policy so
  a cell decision wins over the column and per-value decisions. Blackout modes
  only (pseudonymize/redact/summarize) — same verify-safety constraint as column
  policy. `cell_policies` lives on `job.config` (parity, like column_policies).
- `CellInfo` model + `xlsx_handler.cell_summary(findings)` (from unit_ids, no
  workbook re-read); `ScanResult.cells`; wired through `scan_document`.
- Review UI: a **Cells** cluster (`review._cells_panel`) parallel to Columns —
  one row per flagged cell (Sheet!Coord + preview + entity chips + mode toggle),
  capped at 100, PLUS an add-by-reference input to mark a cell detection never
  flagged. `cell_policies` threaded through `render_review`/`app.py`.
- The topical categories already group under ONE "Internal / topical" data-class
  cluster automatically (taxonomy maps all topical types to INTERNAL_TOPICAL) --
  no rail explosion.
- 183 tests green (3 new cell-policy tests + headless render covers the Cells
  cluster). Marker `9ad7de`.

### Deferred (my recommendation: skip unless asked)
- "Possible topical" NER-candidate extraction cluster. The taxonomy slot
  (POSSIBLE_TOPICAL -> TOPICAL_CANDIDATES) exists, but nothing produces
  candidates yet. Offline candidate extraction (capitalized/OOV tokens) is
  exactly the noisy path the corroboration-only + filter work spent this session
  REMOVING; building it risks re-introducing the false positives. The structural
  header->category + gazetteer detection covers the high-value cases. Recommend
  leaving this out unless real use shows a concrete gap.
- docx/pptx table-cell policies; PDF blocks; cross-document learned gazetteer
  (as previously deferred).

## Status: ALL THREE PHASES DONE (topical detection + summarize/redact modes +
## cell-level exception layer). Candidate-extraction cluster intentionally left out.
