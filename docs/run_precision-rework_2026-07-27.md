# Run: detection precision rework + recall gap closure (2026-07-27)

Spec + resumable run-file. Grilled 2026-07-27 (25 questions, 6 rounds + 1
reconciliation). Supersedes the precision posture set in
`run_detection-precision_2026-07-23.md` where the two conflict.

## GOAL

Cut the German-common-noun false positives that dominate the flagged export, and
close the two recall gaps the same audit uncovered — without regressing measured
recall on the scored audit workbook.

## WHAT THE AUDIT ACTUALLY FOUND

Measured against
`C:\Users\MauriceMatthias\Documents\Anonymized\mdx-big-beautiful-innovation-spreadsheet_anonymizer_version_flagged.csv`
(source: `Downloads/mdx-big-beautiful-innovation-spreadsheet.xlsm`, 12 293
non-empty text cells, 20 sheets — all of which were scanned).

Baseline: **453 flagged values / 1931 occurrences.**

| values | occurrences | bucket |
|---|---|---|
| **81** | **780** | PERSON — not a person |
| 15 | 73 | PERSON — plausibly a real person |
| 230 | 234 | DESCRIPTION — real prose |
| **7** | **73** | DESCRIPTION — content-free (only `_x001E_` escapes) |
| 56 | 450 | DEPARTMENT (header-corroborated) |
| 6 | 229 | DIVISION (header-corroborated) |
| 54 | 85 | DATE_TIME |
| 4 | 7 | PHONE_NUMBER / NRP / NER_MISC |

**84% of PERSON values and 91% of PERSON occurrences are not people.**

### Causes, in order of size

1. **The ML pass never ran.** `gliner` + `onnxruntime` absent from the repo venv
   *and* `dist/Anonymizer-offline`; no `vendor/gliner-model`; live config
   `AppData/Local/Anonymizer/config.yaml:437` has `enabled: false`. Every PERSON
   hit in the export scores exactly `0.85` — Presidio's flat spaCy score. The
   export is the pre-GLiNER behaviour.
2. **The precision gate cannot reject a capitalized German common noun.**
   `_NAME_LIKE_POS` (core.py:154) includes `NOUN` so a surname like "Bauer"
   survives; German capitalizes every noun. Re-running all 90 spaCy-guess PERSON
   values through the five filters: **85 survive**. The NOUN group alone is 31
   values / 518 occurrences.
3. **A cell is not a sentence, so spaCy tags capitalized tokens PROPN** —
   disabling both POS-based filters. Proof: `Validierung` has the `-ung` suffix
   and length ≥ 8 yet survives, because `_is_german_nominalization` requires
   `NOUN and not PROPN` (core.py:201) and in `Aktuelle_Phase: Validierung` spaCy
   tags it PROPN. 39 values / 242 occurrences in this group.
4. **The workbook's own controlled vocabulary is unused.** `Gering` 368×, `Idee`
   80×, `Stark` 71×, `Künstliche Intelligenz` 36× — **9 dropdown values are 618
   of the 780 noise occurrences (79%)**, from `Liste_3Pkt_Skala` /
   `Liste_Relevanz` / `Liste_Qualitaetstreiber`. The top *real* name appears 21×.
5. **`_NOT_A_NAME`'s empty-cell guard misses Excel's escape.** `^[\W\d_]*$`
   (xlsx_handler.py:63) doesn't match `_x001E_` — `x` and `E` are word
   characters. 7 empty cells flagged `tier=high`, `action=summarize`.

### Recall gaps

6. **People leak** from columns headed `Owner`, `Einreicher`, `MDX_Lead`,
   `MDX_Proxy`. Reported as 13 from the export; **CORRECTED during Phase 1 to 83
   distinct people / 221 occurrences**, measured by running the candidate header
   widening over the workbook itself. The original 13 came from a name-shape
   heuristic over the export and was explicitly flagged as a floor — it was one.
   Additional names recovered include Sergii Piskun, Nils Bräunlich, Constanza
   Hiemenz, Timo Brandt, Patrick Lortz, Silke Müller, Simon Unterbusch, Mathias
   Regiert, Emir Balzevic, Anne Zopf and Siegfried Eckstedt. `_NAME_HEADER_TERMS` is German-only and contains none of those,
   so the whole-cell PERSON override never fires — verified by testing the header
   regex directly against each. Every name that *was* caught came from bare spaCy.
   Full names leaked: **Pascal Sternheimer, Thomas Schachtner, Ulf Gericke,
   Stefan Woithe, Claudius Wetzler, Anna Franziska Lorenz**. First names:
   **Marco, Thomas, Sergii, Cordula, Mirijam, Niklas, Hendrik**. Partial-leak
   hazard: `Claudius` is flagged standalone, so propagation redacts it inside
   `Claudius Wetzler` and emits `[PERSON_1] Wetzler` — surname in the clear.
7. **41 internal URLs, none flagged** — `https://emma.metzler.com/x/Oim3JQ`
   (Confluence), `https://meos.metzler.com/applications/meos/meos.nsf/lupOHB/27000001?openDocument`.
   **CORRECTED during Phase 1:** the initial diagnosis said "no URL recognizer
   exists". Wrong — Presidio's `UrlRecognizer` was already loaded and `URL` was
   already a supported entity. `URL` was simply absent from the `entities:` block,
   and `detect_unit` only requests configured entities (core.py:593), so it was
   never asked for. Verified: the shipped recognizer matches every real URL shape
   in this workbook and correctly ignores `Datei.xlsx` / `Version 2.0`.
8. **The possible-miss scanner found none of it** — all 100 rows are money
   amounts and project-ID tails (`26-001` from `BP-26-001`).
9. Mistypes: `MDX_PROXY_20` / `MDX_LEAD_51` / `PROJEKT_ID_37` → `DATE_TIME`;
   `227755` under a `Tatsaechliche_Kosten_EUR` header → `PHONE_NUMBER`;
   `24.03.2026 08` is a timestamp cut in half.
   **Cause found in Phase 1:** the **English** spaCy model tags a snake_case
   identifier as `DATE_TIME` at its flat 0.85 (reproduced for all three values),
   reached via the per-sheet language routing added in `21e0e19`. `DATE_TIME` is
   not in `_NER_ENTITIES`, so *no* precision filter applied to it at all.
10. **Suspected, NOT yet measured:** `_is_structural_nonname` rejects any span
    containing `_` (core.py:140), and multi-value cells carry `_x001E_`
    mid-string (`Kein Beitrag_x001E_Reiner Backoffice-Prozess`), so a name fused
    to one of those escapes is silently rejected. Verify in Phase 1.

## DECISIONS (from the grill)

| # | Decision | Chosen |
|---|---|---|
| 1 | Sequencing | Get ML running and re-measure **before** designing the gate |
| 2 | NOUN allow-set | Replaced by corroboration, not by a better word filter |
| 3 | Enum signal source | **Both** Excel data-validation lists **and** value repetition |
| 4 | Abstraction | **PERSON joins `_CORROBORATION_ONLY_ENTITIES`** |
| 5 | Corroboration sources | **All four**: fixed name-column headers · given-name gazetteer · GLiNER hits (already counts) · column-level name inference |
| 6 | Gazetteer | Curated, **given names only**, frequency-ranked, <~1MB. No surnames — a surname list would corroborate `Stark`/`Gering`, the exact noise being removed. No leak-derived dataset in a bank tool |
| 7 | Uncorroborated PERSON | **Demote** to a review band, never silently drop |
| 8 | Name-column headers | Ship English stems in the code-owned list; keep `name_column_headers` user-editable |
| 9 | Export shape | Demoted band gets its **own bucket section**, like `possible_miss` |
| 10 | Enum parity | **Content-keyed set, precomputed once per workbook**, precedent `topical_gazetteer` (xlsx_handler.py:119); passed via config so core stays format-agnostic |
| 11 | URL handling | **Whole URL, pseudonymized**, all hosts |
| 12 | Success instrument | **All three**: audit workbook is the gate, evaluation.py strata guard the bare-name case, MDX workbook is the sanity check |
| 13 | Test contract | **Rewrite deliberately**, one commit per intent change, each naming the decision that replaced it |
| 14 | ML veto | **Yes** — model silence is negative evidence… |
| 15 | …reconciled with #7 | …but it **demotes, it never drops**. One rule for both cases |
| 16 | ML cost gate | Run on **every distinct cell value**; measure, don't pre-optimise |
| 17 | ML install | **Torch-free onnxruntime first**. Fallback is NOT pre-authorized (see below) |
| 18 | Also in scope | `_xHHHH_` normalization at cell-read · DATE_TIME/PHONE mistypes · possible-miss retune · DEPARTMENT/DIVISION noise |
| 19 | Rollout | **4 phases, commit + measure each** |
| 20 | Extra scope | Flip `gliner.enabled` default · generalise the repetition enum to docx/pdf · retry spaCy lg→sm |
| 21 | Glossary | Terms stay in this run-file; `CONTEXT.md` untouched |

**Concern raised and reaffirmed:** I advised against including spaCy lg→sm (tried
and reverted in `d74d52c`; changing the NLP model while reworking the gate makes
every precision number ambiguous). The user reaffirmed it. It is therefore
sequenced **last**, after DONE-WHEN passes, so it can be reverted in isolation.

## CONTRACT

- **Inputs:** unchanged — the same documents, the same `config.yaml` (schema
  bumped v8 → v9 for the new keys, with `_resync_builtins` pushing code-owned
  defaults while preserving user-owned data, per the existing pattern).
- **Outputs:** the flagged export gains a **demoted-band section**; findings
  gain no new required fields (the band is derived from existing provenance).
- **Hard invariant:** scan/apply parity. Every new cross-cell signal is
  content-keyed and precomputed identically in both passes — never
  order- or counter-dependent, because scan iterates `_iter_cell_units(wb)`
  while apply iterates `ws.iter_rows()`.
- **Hard invariant:** with `ml_available()` False the tool still runs; ML absence
  may reduce precision but must never crash or silently reduce recall.

## LAYERS TOUCHED

`anonymizer/core.py` (the gate, corroboration set, build_scan_result) ·
`anonymizer/formats/xlsx_handler.py` (header stems, enum precompute, cell-read
normalization) · `anonymizer/gliner_recognizer.py` (veto path) ·
`anonymizer/data/default_recognizers.yaml` (URL recognizer, schema v9, new keys) ·
`anonymizer/config.py` (`_resync_builtins`) · `anonymizer/report.py` (export
section) · new data file for the gazetteer · `tests/test_precision.py`,
`test_recall.py`, `test_xlsx.py`, `test_export.py`, `test_config.py`.

## PLAN

### Phase 1 — Recall + contained fixes (no gate change)
English/owner header stems · URL recognizer · `_xHHHH_` normalization at cell-read
(+ verify cause #10) · DATE_TIME/PHONE mistypes · possible-miss retune ·
DEPARTMENT/DIVISION noise. **Measure:** 13 names caught, 41 URLs caught, 0
content-free rows, parity clean.

### Phase 2 — Get ML actually running
Torch-free onnxruntime path → build the pack with `scripts/fetch_gliner_model.py`
→ enable → **re-measure the MDX baseline with ML on** → verify determinism and
parity. **STOP AND ASK if the torch-free path proves impossible.**

### Phase 3 — Gate rework
PERSON → corroboration-only with demotion · the four corroboration sources ·
the enum signal (validation lists + repetition, content-keyed, via config) · ML
veto as demotion · enum repetition half generalised to docx/pdf.

### Phase 4 — Export band, gazetteer, default flip
Demoted-band export section · curated given-name gazetteer · flip
`gliner.enabled: true` (requires a `-WithML` bundle verified on a second machine).

### Phase 5 — spaCy lg → sm (last, revertable alone)

## AUTONOMY CONTRACT

### DONE-WHEN (all MUST, machine-checkable)
1. `uv run pytest` fully green.
2. `scripts/score_test_workbook.py`: recall ≥ baseline **and** planted-secret
   leaks ≤ baseline.
3. `anonymizer/evaluation.py` strata: no stratum below its recorded baseline.
4. MDX workbook: **all 13** named leaks caught (list in cause #6).
5. MDX workbook: **all 41** internal URLs caught.
6. MDX workbook: **0** content-free DESCRIPTION rows.
7. Scan/apply parity clean (`verify_output`).

### REPORTED, not gated
Flagged-section size (baseline **453** values / **1931** occurrences) · PERSON
noise share (baseline **84%** of values, **91%** of occurrences) · scan
wall-clock against the 5-minute ceiling. Deliberately not gated: with demotion
chosen over dropping, a raw count target would be satisfied by moving rows
rather than by detecting better.

### DEFAULTS (pre-authorized — proceed, don't ask)
- Tune constants (frequency cutoff, enum repetition threshold, column-inference
  N) by measurement against the audit workbook; record each value and what it
  was measured against.
- Rewrite `test_precision.py` tests that encoded the replaced design, one commit
  per intent change, each commenting which decision replaced it. Anything not
  justifiable comes back as a question.
- Choose the gazetteer's source, size and shipping shape; record provenance.

### NOT pre-authorized — STOP AND ASK
- Torch fallback if the torch-free onnxruntime path fails. Blast radius is bundle
  size (~800MB CPU-only torch vs the agreed <1GB target), which is the user's
  call, not a measurement.

### DEFERRED
- **Content-keyed soft cap on ML.** Trigger: the Phase 2 or 3 measurement
  breaches the 5-minute ceiling on the MDX workbook.

### ROLLBACK
Each phase commits green independently. `gliner.enabled: false` reverts every ML
behaviour (including the veto) instantly. Phase 5 is isolated and revertable
alone. The v9 schema resync preserves user-owned lists and the `enabled` toggle.

### OUT OF SCOPE
Anything not listed above — in particular no changes to the mapping DB, the
encrypted lists, the OCR path, or the review UI beyond the demoted band.

## PROGRESS LOG

- 2026-07-27 — Audited the export, reproduced every false positive's gate
  verdict, and counter-checked the source workbook independently. Grilled and
  signed off.

### Phase 1 — recall + contained fixes

Baseline before: suite **472 passed**; audit workbook **204/206 (99.0%)**, 2 leaks
(`Alteryx`, `OpenClaw` — the two already recorded in `22443bf`), `verify_output`
passed.

Shipped:

1. **English people-column stems as a second, boundary-matched class**
   (`_NAME_HEADER_WORDS`). Substring matching stays for the German stems (one
   "leiter" must cover Projektleiter); the English ones are matched with a
   lowercase-letter boundary, because `_`-joined headers ("Rollout_Owner") defeat
   `\b` while a plain substring "owner" wrongly matches
   `Ownership_geklaert_Status`. **Chosen from measurement, not intuition:** the
   candidate list was run over the workbook first — 84 newly-claimed pairs, 83 of
   them real people, and the single false positive was exactly that
   `Ownership_geklaert_Status: 'Ausstehend'` (30×), which the boundary form removes.
   Only `owner`/`einreicher`/`lead`/`proxy` earn anything on this file; the rest are
   kept as they cost nothing measured here and close the same gap elsewhere.
2. **A weaker guess can no longer veto the header.** spaCy types some real names
   `NER_MISC`; that whole-cell MISC hit suppressed the override, and MISC — a bare
   guess — was then dropped outright by `corroboration_only`, so the name left in
   the clear from a column headed `Owner`. Now retyped IN PLACE to PERSON with
   `source=whole_cell_override` (adding a second finding would lose the overlap
   contest to MISC's flat 0.85 and change nothing). Found by chasing the one name
   the measurement still missed.
3. **URL detection.** Presidio's `UrlRecognizer` was already loaded — `URL` was
   simply not in the `entities` block, so `detect_unit` never requested it. Its
   loose non-scheme pattern turned out to produce **broken spans** (`d.ve` out of
   `d.velop`, `bank.de` out of an email address), which on apply would write
   `[LINK_1]lop`, so it is now REPLACED by scheme/`www.`-anchored patterns in
   `engine._URL_PATTERNS`. Those also exclude trailing sentence punctuation, which
   Presidio's did not.
4. **Excel `_xHHHH_` escapes neutralized**, same-length, inside
   `neutralize_structural_noise` — so offsets and therefore parity are untouched.
   Fixes both directions: the 7 empty cells flagged at the auto-accept tier, and
   the silent false negative where a name fused to an escape was rejected by
   `_is_structural_nonname`'s underscore rule.
5. **`DATE_TIME` snake_case rule.** Cause identified: the **English** model tags
   `MDX_PROXY_20`/`MDX_LEAD_51`/`PROJEKT_ID_37` as DATE at its flat 0.85, and
   DATE_TIME is not in `_NER_ENTITIES` so no filter applied at all.
6. **Possible-miss retune** — number shapes subtracted (never inclusion rules
   rewritten, so the audit workbook can prove nothing planted was lost), and the
   digit-run pattern widened to report `BP-26-001` whole instead of the tail
   `26-001`.
7. **Placeholder tokens excluded from the topical gazetteer** — a `Team` column
   held `team_1`..`team_5`, each learned as a DEPARTMENT and propagated
   document-wide. Guarded in the gazetteer (the path that actually produced them)
   as well as the whole-cell override.

**A constant I got wrong, caught by the suite:** the first possible-miss version
used a flat 8-digit floor for bare integers, which broke
`test_core.py::test_completeness_flags_unmatched_numbers_and_emails` — a 6-digit
contract number in prose must still surface. The test was protecting something
real, so the constant changed, not the test: a lone number in its own cell is a
quantity, the same digits inside prose are not.

**`DESCRIPTION` 237 → 145 is a precision win, not a regression** (verified, not
assumed): 80 of the 237 baseline rows were single snake_case field identifiers
(`Beschreibung_1` … `Beschreibung_N`) plus the 7 `_x001E_` empties. Separately
confirmed that a URL inside a description cell does NOT suppress the whole-cell
DESCRIPTION claim, which would have been a leak.
