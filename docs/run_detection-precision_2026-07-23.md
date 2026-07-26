# Run: Detection precision (NER_MISC noise) + review-GUI consistency (2026-07-23)

## Why
User scanned a business/audit-log spreadsheet (OldValue/NewValue columns holding free-form
German prose) and got 5,167 "Medium" findings, including ordinary German words ("aber",
"abdeckung") and fragments with leading punctuation ("-Erstellung", ".iboflow-Dateien").
Explore + grill traced this to specific, fixable causes rather than "the model is just bad":
NER_MISC's threshold (0.5, the lowest of any entity) barely filters anything since spaCy's
flat NER score (~0.85) always clears it; `taxonomy.tier_for()` means every raw NER hit lands
at Medium by construction (0.5 <= 0.85 < 0.9); xlsx's whole-cell PERSON override can claim an
entire prose cell as a person name off a loose header-substring match; and the leading
punctuation is raw spaCy tokenization fusing markdown/bullet syntax onto the next word with
NER then tagging the fused token. User confirmed: cut false positives even at some recall
cost. Separately, the review GUI has a same-day-introduced (commit 698cbfa) inconsistency
between the column-policy button group and the per-value button group, and the existing
"Preview changes" dialog is wired up but shows no surrounding context/highlight.

## Decisions (user-approved via grill, 2026-07-23; 32 questions / 8 rounds)

**Split into two passes.** This run covers Pass 1 only. Pass 2 (GUI) is a separate future run.

### Pass 1 — Detection precision (THIS RUN)
1. **NER_MISC threshold**: raise `confidence_threshold` from 0.5 to **0.75** in
   `default_recognizers.yaml`. Still under spaCy's flat ~0.85 score (so a plain MISC hit
   still surfaces by default), but well above generic-word noise. Applies platform-wide
   (shared `core.py:detect_unit`, not xlsx-specific).
2. **Lowercase single-word filter**: reject any `NER_MISC`/`ORGANIZATION`/`LOCATION` finding
   whose value is a single all-lowercase word. Not language-gated (proper entities are
   capitalized in essentially every supported language too). Near-zero recall risk.
3. **Stopword filter, layered**: reject a `NER_MISC`/`ORGANIZATION`/`LOCATION` finding whose
   value is a spaCy stopword (`nlp.vocab[value].is_stop`, zero extra maintenance) OR appears
   in the existing config-editable `allow_list` (extend its default entries with a few more
   common business/German terms if useful) — reuses the existing mechanism rather than
   inventing a new one.
4. **Whole-cell PERSON override name-shape gate** (`xlsx_handler.py`): the header-substring
   override may only claim a WHOLE cell as PERSON when the cell value also *looks like a
   name*: 1-4 words, each capitalized (or a recognized German name particle: von/van/de/der/
   zu), total length < 40 chars, no sentence-ending punctuation (`.`/`!`/`?`) inside it. A
   paragraph under a matching header no longer gets claimed wholesale.
5. **Leading-punctuation trim**: generic post-NER normalization in `core.py` — strip leading
   non-alphanumeric characters from any finding's span (adjusting start/end), regardless of
   entity type or root cause. Safe backstop.
6. **Markdown/structural pre-clean before detection**: SAME-LENGTH character substitution
   (never insert/delete, so span offsets stay exactly aligned with the original text) that
   neutralizes bullet markers, heading `#`, and bracket syntax fused directly onto a word
   with no space, replacing the structural character with a space. Redaction still operates
   on the untouched original text — this only changes what's fed to spaCy. Applied
   platform-wide (shared pipeline, all format handlers), per user's explicit choice.
7. **Borderline-word default** (pre-authorized, no need to stop and ask): when a word is
   both a common dictionary word AND a plausible name (e.g. "Klein"), default to EXCLUDING
   it as noise unless corroborated by an untouched pattern (honorific/labelled-name regex
   still catches "Herr Klein" independently of this filter).
8. **Regression tests**: new `tests/test_precision.py` (or extend `test_recall.py`) with
   realistic German business prose asserting specific common words are NOT flagged, plus
   coverage for the name-shape gate, leading-punctuation trim, and markdown pre-clean.
9. **Sensitivity slider**: no math change — `max(0.0, threshold - sensitivity)` still holds;
   just a short code comment noting the NER_MISC threshold moved, since the same slider
   value now suppresses a different absolute set of findings than before.
10. **Rollout**: no in-place refresh mechanism for already-open, not-yet-saved scan sessions
    — new scans (after restart) get the improved accuracy, matching normal update behavior.
11. **Audit trail**: no new audit.py entry — pre-cleaning doesn't change what gets redacted
    or saved (redaction still operates on the original text), so nothing compliance-relevant
    to log beyond what's already recorded.

### Pass 2 — GUI (DEFERRED, separate future run — NOT this pass)
- Unify column-policy vs per-value button groups: same no-op label/position/color (source
  from one shared `theme.ACTION_COLORS` entry), extract one shared toggle-building helper
  to replace the two near-duplicate implementations in `review.py`.
- Preview dialog (`app.py:_preview_dialog`): add the already-computed `GroupedFinding.context`
  snippet to `PreviewRow`/`build_preview`, highlight the flagged span within it (color +
  a non-color cue like bracket markers, for accessibility), capped/paginated matching the
  existing per-value cap+summarize pattern (commit 00c134c).
- Sensitivity slider: debounced live re-score count in Settings (re-bucket already-detected
  findings against the new threshold — no re-detection needed, cheap).
- Tier-splitting: a source-based sub-tier distinguishing raw NER guesses from
  pattern/checksum-backed Medium findings.
- Columns panel: surface which columns triggered the whole-cell header override, so a bad
  match is visible before Save.

## Contract
- Entity/threshold changes live in `anonymizer/data/default_recognizers.yaml` and
  `anonymizer/core.py` (shared `detect_unit`, used identically by scan and apply — parity
  preserved by construction, per the module's existing invariant).
- New filters must not touch `_resolve_overlaps`, the honorific-anchored-name patterns, or
  checksum validation (`_refine`) — those are separately-validated, working mechanisms.
- Markdown pre-clean must be a pure same-length substitution — any implementation that
  shifts text length breaks span alignment for every subsequent finding in that unit and is
  out of bounds.
- Format-handler scope for pre-clean: all handlers (docx/pptx/pdf/xlsx) via the shared
  TextUnit path, since the user chose "apply everywhere."

## Tests
- Full existing suite stays green (125 tests as of this session).
- New `tests/test_precision.py`: realistic German business-prose samples asserting "aber",
  "abdeckung"-class common words are NOT flagged as MISC/ORG/LOCATION; name-shape gate keeps
  a prose paragraph out of the whole-cell PERSON override under a matching header; leading
  punctuation is trimmed from a finding's span; markdown bullet/heading fusion no longer
  produces a fused-token finding.
- A synthetic xlsx resembling the reported audit-log shape (OldValue/NewValue prose columns)
  used for an end-to-end verification pass (the original files were cleared from the app's
  work dir mid-session and are not available to re-test directly).

## Done-when
- Full pytest suite green (existing 125 + new precision tests).
- Verified on a real/representative German business-prose document: previously-noisy common
  words and leading-punctuation fragments no longer appear as findings, while known-real PII
  (names via honorific/labelled patterns, IBANs, etc.) still gets caught — confirming this
  is a precision improvement, not a blanket suppression.

## Defaults
- Borderline common-word-that's-also-a-name: exclude (favor precision), per item 7 above.
- Any other in-flight ambiguity during implementation: favor cutting false positives,
  consistent with the user's confirmed stance, UNLESS it would touch a checksum-validated
  or pattern-anchored recognizer (those stay untouched — they're not the source of the
  reported noise).

## Deferred
- Everything under "Pass 2 — GUI" above. Trigger to resurface: user asks to proceed with
  Pass 2, or the GUI inconsistency/missing-highlight complaint comes up again.

## Status: PASS 1 DONE

- All 10 detection-accuracy items implemented: NER_MISC threshold 0.5 -> 0.75
  (`default_recognizers.yaml`), lowercase-word + spaCy-stopword filter for
  NER_MISC/ORGANIZATION/LOCATION (`core.py:_is_noise_entity`), whole-cell
  PERSON override name-shape gate (`xlsx_handler.py:_looks_like_name`),
  generic leading-punctuation trim for any free-text finding
  (`core.py:_LEADING_NOISE`), same-length markdown/bullet-fusion pre-clean
  applied platform-wide via the shared `detect_unit` (`core.py:
  neutralize_structural_noise`), sensitivity-slider doc note.
- 135 tests green (125 existing + 10 new in `tests/test_precision.py`).
- Verified on a synthetic document shaped like the reported audit-log
  spreadsheet (OldValue/NewValue free-text columns, repeated prose samples
  lifted from the actual report): 1,055 units scanned -> 15 distinct findings,
  ZERO noise-word hits ("aber"/"abdeckung"/etc.), ZERO leading-punctuation
  hits, and all seeded real PII (a name, IBAN, account number, customer
  number) still correctly caught -- confirms this is a precision improvement,
  not a blanket suppression.
- Two real, live-reproduced bugs directly confirmed fixed by hand: the
  `.iboflow-Dateien` leading-period fusion (now trims to `iboflow-Dateien`),
  and ordinary words no longer surviving `_is_noise_entity`'s filter.
- Pass 2 (GUI unification, preview-dialog highlighting, sensitivity live-count,
  tier-splitting, columns-panel override visibility) is DEFERRED, scoped and
  ready to start as its own run whenever picked up.

## Status: PASS 2 DONE

- **Button unification**: `review.py`'s column-policy and per-value toggles
  now share one `_labeled_toggle()` helper, one label set (no-op renamed
  "Skip" everywhere, always first), and one colour map (`_ACTION_QCOLOR`,
  reused as `_COLUMN_QCOLOR`) instead of two independently-styled groups.
  `_overflow_row`/`_tier_bands` routed through the same helper for consistency.
- **Columns-panel override visibility**: `ColumnInfo.name_override` (new field,
  computed in `xlsx_handler.column_summary`) flags a column whose header
  matched the people-column list; `review.py` renders a "name override" chip
  with an explanatory tooltip so a coincidental match is visible before Save.
- **Preview dialog**: `PreviewRow.context` (new field, sourced from the
  already-computed `GroupedFinding.context`) flows through `build_preview`;
  `app.py`'s `_preview_dialog` renders the highlighted snippet (colour + kept
  bracket markers as a non-colour cue) via `_highlighted_context_html`
  (HTML-escaped), capped at 200 rows for responsiveness on a large scan.
- **Sensitivity live-preview**: reality check surfaced mid-implementation --
  the originally-described "cheap re-bucketing" was inaccurate (the offset
  gates candidates during detection itself; nothing short of a re-scan
  reflects a changed value), and Settings had no existing link to the main
  page's job state. Re-confirmed with the user; implemented as a debounced
  (600ms) REAL re-scan of the currently-open document via a new
  `app.current_review_job()` accessor (`_active_state` module global) and
  `settings_page._sensitivity_preview`.
- **Tier-splitting**: `Finding.source`/`GroupedFinding.is_ner_guess` (new
  fields) distinguish a raw spaCy NER guess (no pattern/checksum/override
  corroboration anywhere) from a pattern-backed Medium hit, using Presidio's
  own `recognition_metadata["recognizer_name"]`. `review.py`'s "By confidence"
  row splits Medium into two bands accordingly.
  - **Bug found and fixed during verification**: `_resolve_overlaps` was
    silently discarding the corroborating source whenever a raw-NER candidate
    won a same-span tie-break against a whole-cell-override/pattern candidate
    (a same-span "Klaus von Bergen" NER hit at 0.85 beat the override's fixed
    0.8 score), making a value TWO mechanisms independently confirmed read as
    "just a guess." Fixed via `_absorb_corroborating_source`; covered by a
    dedicated regression test.
- 145 tests green (135 after Pass 1 + 10 new: button/tier-split logic,
  preview-context/highlighting, columns-panel flag, the overlap-resolution
  corroboration-preservation regression).
- GUI rendering was NOT visually verified in a live browser/native window --
  this project has no live-UI test harness (consistent with its existing
  tests, which deliberately test only headless logic). User should eyeball
  the actual buttons/dialog/slider next time the app is open.

## Status: THIRD-WAVE FOLLOW-UP DONE (2026-07-23, same day)

User re-tested and still saw ~5,100+ flags, naming NEW words ("Abgelehnt",
"Alle Zielwerte") not covered by the first wave -- these are a genuinely
DIFFERENT failure mode, not evidence the first wave didn't work (confirmed:
git diff shows all first/second-wave changes present; direct re-testing
confirms "aber"/"abdeckung"-class noise and leading-punctuation fragments
stay fixed). Investigated and fixed three more root causes:

1. **"Abgelehnt" ("Rejected") tagged PERSON**, not NER_MISC -- outside the
   first wave's lowercase/stopword filter, which deliberately excludes PERSON
   to protect real lowercase surnames. Fix: `core._is_pos_implausible` --
   spaCy's OWN part-of-speech tagger (VERB for "Abgelehnt"/"Genehmigt", NOUN
   for "Bauer", PROPN for "Müller") disagrees with its NER call often enough
   to be a safe, case-independent signal, applied to all `_NER_ENTITIES`
   including PERSON. Verified identical behavior in English
   ("Rejected"/VERB vs "Baker"/PROPN) before shipping.
2. **"Alle Zielwerte" ("All target values")** passed the whole-cell-override
   name-shape gate (2 capitalized words, no punctuation) despite not being a
   name at all -- shape alone can't distinguish this from "Klaus Müller" in
   German. Fix: `xlsx_handler._looks_like_name` now also rejects a value
   containing a determiner/verb/conjunction/etc. token (POS-tagged), with
   name particles ("von"/"de") exempted since they tag PROPN in a real
   name's context.
3. **"BP-002"-style project/ticket codes** tagged NOUN/PROPN -- POS alone
   can't distinguish a real proper noun from an arbitrary alphanumeric code.
   Fix: `core._is_digit_bearing_code` -- a genuine name essentially never
   contains a digit.
4. **Bonus find (unprompted, directly relevant to the user's real file):**
   column headers themselves ("NewValue", "OldValue", "Project_ID" -- the
   user's own actual column names) were being scanned as DATA, not just used
   as context -- a CamelCase/underscore schema label reads as a proper noun
   to NER. Fixed in `xlsx_handler._iter_cell_units`/`apply()`: row 1 supplies
   header context but is never itself a scannable unit. Required updating
   the shared `sample_xlsx` test fixture (previously put its test PII in row
   1) for scan/apply parity.

**Verified end-to-end** on a synthetic audit-log-shaped document (OldValue/
NewValue prose, PII, and all four newly-reported words mixed in): DE findings
dropped from 189 -> 4 after these fixes; a parallel EN document produced 4
findings too, both with zero noise-word hits and all seeded real PII (names,
account number) still caught -- German and English perform equally well on
this test.

**Known asymmetry (answering "equally capable?"):** detection quality
itself is equal, but German has more RECALL safety nets than English --
honorific/labelled-name anchor patterns (engine.py's `_HONORIFICS`/
`_NAME_LABELS`), `_NAME_HEADER_TERMS` (34 German business-header stems), and
several German-specific structured-ID recognizers (Kontonummer, Steuer-ID,
BIC) have no English equivalents. An English document relies more heavily on
raw NER alone for names. Not fixed in this pass -- flagged as a natural next
increment if English documents are processed regularly.

- 150 tests green (145 + 5 new: POS-implausibility, digit-bearing-code,
  header-row-exclusion, real-name-shape regression, project-code regression).
- UI-coloring complaint not reproduced in code review (re-read the full
  Pass-2 diff; `_labeled_toggle`/`_ACTION_QCOLOR`/`_COLUMN_QCOLOR` are
  correctly shared and consistent). Leading hypothesis: the native app has
  `reload=False` and was not restarted since Pass 2 shipped, so the window
  is still running pre-Pass-2 code. Could not verify visually (no live-UI
  test harness) -- asked the user to fully close/reopen and report back with
  a fresh screenshot if it persists.

## Status: FOURTH-WAVE — DIAGNOSTIC EXPORT + root-cause reasoning (2026-07-23)

User still sees 4000+ flags and the UI difference after restart, and asked
(good instinct) for an EXPORT so the assistant can see the actual flagged
words instead of guessing against synthetic data a fourth time.

- **Confirmed via install inspection**: package is editable
  (`_editable_impl_anonymizer.pth`, `direct_url.json` editable:true) -- a
  restart DOES pick up source edits; no reinstall needed, no stale
  site-packages copy. So persistent old-looking UI is a not-restarted or
  a genuine-but-unreproduced issue, NOT an install problem.
- **Built the diagnostic export** (user-requested): `core.findings_export_rows`
  / `findings_summary` / `write_findings_csv` (pure, tested), plus an "Export
  flagged terms" button in the review screen (`app._export_findings`) that
  writes `<name>_flagged.csv` to the Anonymized folder with every flagged
  value + context + entity_type + tier + is_ner_guess, and notifies the top
  entity-type breakdown inline. Deliberately contains ORIGINAL values (unlike
  report.py) -- labelled a tuning artifact, not a safe report. 4 new tests.
- **Root-cause HYPOTHESIS for the 4000 (pending export confirmation)**: the
  reported document is dense German free-text project prose (NewValue column
  alone showed 4556 PII in the screenshot). Per-word heuristics can't fully
  fix this class: German capitalizes every noun, product/technical terms
  (Dashboard, Innovation, Asset, iboflow, MDX) read as ORG/MISC to NER, and
  every row holds UNIQUE prose -- so unlike the synthetic repeated-row tests
  (which dedup to ~4 findings), real data yields thousands of DISTINCT
  proper-noun-ish tokens, each a Medium `is_ner_guess`. The right lever is
  almost certainly column-level (skip the whole prose column, already
  supported) and/or a policy to down-rank free-text NER in non-name columns
  -- NOT a 4th per-word filter. Export will confirm the actual distribution
  before any such change is designed.
- 154 tests green (150 + 4 export).

## Status: FIFTH-WAVE — STALE-BUILD ROOT CAUSE + clustered UI redesign (2026-07-23)

**THE root cause of the entire multi-message saga**: the user reported seeing
NONE of the recent changes (no export button, no sensitivity slider) AND the
persistent 4000+/old-UI behaviour. Investigation found the desktop shortcut
"Document Anonymizer.lnk" launches `dist\Anonymizer-offline\launch.bat` -- a
FROZEN offline bundle built July 15 (its bundled `review.py` still contains the
pre-Pass-2 `"none": "Keep"` column UI, and the pre-fix detection engine). So
every change all session was invisible: the user was running two-week-old code.
The repo path (`Anonymizer.bat` -> `uv run anonymizer`, editable install) DOES
load current source (verified: `_export_findings` present, no "Keep"). The two
launchers look identical but run different code.

- **Build marker** (`app._build_marker`, shown in the header): "dev · <hash>+"
  when running the live repo, "bundle · v<ver>" for the frozen bundle -- so
  "which code am I running" is answerable at a glance, ending the confusion.
- **Clustered master/detail review screen** (user-chosen over card-grid/tabs):
  `review.render_review` rewritten -- LEFT rail lists clusters (Overview, each
  data class, Columns, Possible misses) with counts; RIGHT shows only the
  selected cluster's detail; the rail is sticky and the detail scrolls, so the
  old single long scroll (the stated accessibility problem) is gone. New CSS
  `.az-cluster-nav*` / `.az-review-split` in theme.py (incl. narrow-screen
  stacking).
- **Detection control bar on the review screen** (`app._detection_control_bar`):
  profile + language + a live sensitivity slider + Re-scan, all in one place --
  previously profile/language sat only in the intake panel (applies to files
  added NEXT, not the one on screen) and sensitivity only on the Settings page,
  with no "apply to this file" action. `PageState.sensitivity` plumbed into
  `scan_all` (overrides the persisted config value for GUI scans; initialized
  from it so a saved Settings value is respected).
- **Re-scan** (`app._rescan_job`): resets a reviewed/failed job to pending and
  reuses the exact `scan_all` path, so re-scan == first-scan by construction.
- **Diagnostic export** (prior turn) carried in: "Export flagged terms" button
  writes `<name>_flagged.csv` (original values + context + tier + is_ner_guess)
  so the real flag distribution can finally be seen instead of guessed.
- **Headless render harness** (`tests/test_gui_render.py`): NiceGUI has no
  browser here, so these use the synchronous `Client`-context (not the async
  `user` fixture, which needs the absent pytest-asyncio) to prove every review
  cluster + the control bar BUILD without raising -- a blank-screen/crash guard
  that finally covers the "can't verify UI" gap. 4 tests.
- 158 tests green (154 + 4 render smoke).
- Per user choice, the offline bundle was NOT rebuilt -- first confirm via the
  header marker whether they're even on the bundle. If the marker says "bundle",
  rebuild `dist\Anonymizer-offline` via scripts\build_offline_bundle.ps1.

## Status: SIXTH-WAVE — THE big one: wrong-language scan + stale-config shadow (2026-07-23)

Confirmed the user was on live repo code (marker), yet still saw thousands of
common German words flagged. The exported flag CSV (real data at last) exposed
TWO compounding root causes that all prior waves couldn't reach:

1. **Language auto-detection picked ENGLISH for a German document.**
   `detect_dominant` sampled only `units[:80]` -- in a spreadsheet those are the
   header row + structured field-name cells ("Project ID", "Status", "CostBlock"),
   which are English-ish (measured de:en = 1:5 there) while the German prose body
   is 4.7:1 German. So it CONFIDENTLY chose 'en' and ran the English NER over
   German text, which tags ordinary German words ("oder", "Der Prozess") as
   PERSON -- and every German-specific precision filter (stopwords, POS) was keyed
   to the wrong language and silently no-op'd. Fix: `pipeline._language_sample`
   samples ACROSS the whole document (strided to a char budget). Real-file effect:
   4232 -> 1498 findings, now correctly 'de'.

2. **The user's config.yaml shadowed every shipped built-in fix, forever.**
   `_ensure_defaults` was additive-only ("never overwrite a user value"), so a
   config auto-created from an OLD default kept NER_MISC at 0.5 and the old
   DE_ADDRESS regex -- my Pass-1 YAML fixes (NER_MISC 0.75, etc.) NEVER reached
   the user. Fix: `config_schema_version` + `config._resync_builtins` re-syncs
   code-owned built-ins (entity thresholds, built-in recognizer regexes, tiers)
   ONCE per schema bump, preserving user-owned data (allow/deny lists,
   sensitivity, name_column_headers, user-ADDED recognizers). Bumped to v2; the
   user's on-disk config is now migrated (NER_MISC 0.75, fixed DE_ADDRESS).

Also this wave:
- **Structural non-name filter** (`core._is_structural_nonname`, all NER entities
  incl. PERSON): single letters / 2-char fragments, snake_case field IDs
  ("Feld_Name"), and short ALL-CAPS acronyms ("CAPEX", "RAG") -- the dominant
  residual noise once the language was right. Spares real names (Müller, Deutsche
  Bank, FactSet, Yılmaz).
- **DE_ADDRESS number+unit false positive** ("66450 Euro", "39870 Minuten")
  fixed via a currency/unit negative-lookahead in the PLZ+city pattern.
- **Layered mixed-language** (user-chosen): English honorific/label name anchors
  (Mr/Mrs/Ms/Dr + Name, Client:/Customer:) added to `_ANCHORED_NAME_PATTERNS`,
  which run for EVERY scan language -- so an English name in a German-scanned doc
  is caught, without running a second full NER model (the noise trap). Structured
  IDs (email/IBAN/card/phone/BIC) were already language-independent.
- After all fixes the residual German-model findings are mostly REAL named
  entities (OpenAI, Azure, Signavio, Python) + German business compound nouns --
  defensible review items, not the earlier garbage. Further reduction is a
  review-workflow concern (column-skip, NER-guess bulk band), not more filtering.
- 165 tests green (159 + 6 new: config migration x2, structural filter,
  acronym/field-id end-to-end, DE_ADDRESS unit guard, English anchors,
  language-sample bias). Header source signature now `70ebb2`.

## Status: SEVENTH-WAVE — propagation bypass + corroboration-only + triage (2026-07-23)

Real re-scan gave 1313 findings (down from 4232). The export CSV exposed the
next root cause and the shape of the residual:

1. **Propagation bypassed EVERY precision filter.** A value seeded once (a
   snake_case field id "Aktueller_Status", or "Gering" used as the adjective
   "low") re-appeared document-wide as PII, unfiltered -- swamping the review
   and CORRUPTING is_ner_guess (propagation set source!=SpacyRecognizer, so
   obvious junk read as "corroborated"). Fix: `core._rejected_by_precision`
   (one shared gate) now runs on propagated matches too, re-validating each in
   its LOCAL context. Removes ~166 distinct junk rows (the *_Status fields)
   plus the huge count amplification.
2. **Corroboration-only for ORG/LOCATION/NER_MISC** (user-chosen): these
   "medium sensitivity" free-text guesses are surfaced only when backed by more
   than a bare spaCy hit (propagation/anchor/validation/name-column, i.e. NOT
   is_ner_guess). On business prose they were ~926 of the residual and almost
   all non-PII (product names, jargon, common nouns). PERSON + structured IDs
   never gated. Toggle: `corroboration_only` (default true).
3. **Curated jargon allow-list**: CAPEX/OPEX/RAG/Dashboard/"Lessons Learned"/…
   shipped in the default allow-list (propagates additively to existing users).
4. **Triage stats**: `likely_pii` vs `model_guess` counts in the review stat
   bar, so the reviewer sees how much of the workload is bare NER guesses and
   can bulk-skip them via the existing "Medium · NER guess" band -- the safe way
   to handle the PERSON residual (never dropped, since a real lowercase surname
   could hide there).
5. **Common-word frequency filter: investigated and DELIBERATELY NOT SHIPPED.**
   `wordfreq` data shows the most common German SURNAMES outrank common nouns
   (Müller 4.80 > Effizienz 3.86; Weber 4.36, Bauer 4.43, Schmidt 4.60) -- a
   blanket frequency filter would suppress the commonest real surnames, a
   serious PII leak. Applying it only to ORG/LOC/MISC would be redundant with
   corroboration-only and not worth the offline-bundle weight of the dependency.
   Reported to the user instead of shipping a leak.
6. **Language indicator**: the control bar now shows "Scanned in: German
   (auto-detected)" with a hint to change + Re-scan (answers "where do I see
   the language?").
- Conservative estimate on the real CSV: 1313 -> ~285 (real will be lower once
  the propagation fix reclassifies falsely-"corroborated" ORG/LOC/MISC). The
  residual is PERSON (real names + common-word noise, handled by the triage
  band) + dates + a few corroborated entities.
- 170 tests green. Header source signature now `0361a0`.

## Status: EIGHTH-WAVE — German nominalization filter + category chips + topical Settings

Real re-scan: 4232 -> 445 flagged (90% cut). Residual concentrated in 96 PERSON
(real names + German nouns). This wave:
- **German nominalization filter** (`core._is_german_nominalization`): a PERSON/
  NER value with a productive noun-forming suffix (-ung/-heit/-keit/-schaft/
  -tion/-ität/-enz/-ismus/...), length >= 8, POS NOUN and no PROPN token, is
  filtered ("Effizienz", "Derivatefreiheit", "Nutzung", "Reaktionszeiten",
  "Batch-Verarbeitung"). Layered (suffix + length + POS) so real surnames are
  spared -- verified Müller/Weber/Bauer/Metzler/Jung all kept; measured 29 of
  the 96 PERSON noise items dropped, zero real names.
- **Category chip in review rows** (`review._capture_row`): each finding shows
  its category (PERSON / TOOL / DEPT / IBAN / ...) so the new topical categories
  are visible per-row (they already grouped under the "Internal / topical"
  cluster).
- **Topical Settings editor** (`settings_page._topical_section`): per-category
  manual TERMS (a name the model misses in prose -- 'DeepL Pro', 'Claudius' --
  added here is redacted document-wide via propagation) + editable HEADER WORDS,
  plus an enable switch. Verified a manual 'DeepL Pro' term is caught in prose.
- Honest residual: ~40 PERSON items are common German nouns with no distinctive
  morphology (Gering/Stark/Pain/Kosten-words) -- indistinguishable from surnames
  offline; handled by the triage band + allow-list, not more filtering.
- Recall note (user obs "Max flagged, Claudius/DeepL Pro not"): offline NER is
  inconsistent on prose names/tools; the reliable path is structural columns +
  the manual gazetteer (now editable in Settings), not prose NER.
- 187 tests green. Header source signature `f6c582`.
