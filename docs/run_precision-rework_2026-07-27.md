# Run: detection precision rework + recall gap closure (2026-07-27)

Spec + resumable run-file. Grilled 2026-07-27 (25 questions, 6 rounds + 1
reconciliation). Supersedes the precision posture set in
`run_detection-precision_2026-07-23.md` where the two conflict.

## ⇢ WHAT IS LEFT (single source of truth — read this first)

State as of 2026-07-27, suite **512 passed**, everything below verified by measurement
rather than assertion. Phases 1 and 2 are DONE; 2.5 (ML speed) is DONE.

### ⛔ BLOCKER — diagnose before flipping PERSON to corroboration-only

The flip is built, measured and **reverted**, because it breaks the apply round trip.

**The prize is large and confirmed:** false positives **28/84 → 5/84** (82% cut) with
recall holding at **293/293**. Reproduce by adding `"PERSON"` to
`core._CORROBORATION_ONLY_ENTITIES` — one line, everything else is already in place.

**The blocker:** with PERSON added, the fail-loud verify reports **5 removed values still
present verbatim** — `Amina`, `Koch`, `Kowalski`, `Schneider`, `Weber`. Isolated by
experiment, not assumed: removing PERSON from the set makes apply pass again.

Two clues, both unexplained:
- `Weber` / `Koch` survive in **no cell at all** → a non-cell surface (sheet name,
  formula literal, comment) or a package part the cell walk does not reach.
- `Amina` survives inside a **mangled sentence**:
  `"Ms Priya Whitfield [ORIGIN] Mr Amina Adeyemi [ORIGIN]"`. A one-way NRP/Art. 9 span
  has eaten the connective text **and swallowed a name that then stayed in the clear**.

The second clue is the serious one and the place to start: it points at
`_split_special_category_spans` / `_survives_special_category` interacting with demotion.
`_survives_special_category` keys on `entity_type == "PERSON" or validated is True`, so
anything that changes which PERSON findings exist changes what survives inside a one-way
Art. 9 span. A one-way token destroying text is unrecoverable, which is why this must be
understood rather than worked around.

Suggested order: reproduce with the one-line change, then dump the findings for
`Board Minutes EN!C2` before and after to see which span wins overlap resolution.

### Then — the rest of Phase 3

This is the phase that actually removes the false positives. Signed off in the grill,
not yet started. In dependency order:

1. ~~**PERSON becomes corroboration-only, DEMOTED not dropped**~~ — BUILT, MEASURED (28→5 FPs), REVERTED. See the BLOCKER above.
   `_CORROBORATION_ONLY_ENTITIES` (core.py:81) currently holds NER_MISC/ORGANIZATION/
   LOCATION. Adding PERSON is a one-line change with a large blast radius — read the
   note under WHAT THE AUDIT ACTUALLY FOUND first: on the reported export EVERY real
   person had `is_ner_guess=True`, so this MUST land together with item 2 or it drops
   every name.
2. **The four corroboration sources** (grill decision 5): repaired name-column headers
   (DONE in Phase 1 — 83 people recovered), a curated given-name gazetteer (DONE, 909e408),
   GLiNER hits (already count, core.py:810), and column-level name inference (NOT built).
3. **The enum / controlled-vocabulary signal** (grill decisions 3/10): read Excel's
   declared data validations AND value repetition, as a content-keyed set precomputed
   once per workbook, passed via config. The fixture already declares 3 real validation
   lists pointing at `DB_Setup`, verified to survive the save/load round trip, so this
   is measurable the day it is written.
4. **The ML veto as a DEMOTION** (grill decisions 14/15): model ran on a text and saw
   no person ⇒ demote the bare spaCy PERSON hit. Evidence it will work: GLiNER scores
   all three decoy classes BELOW the 0.3 threshold (`Die Effizienz der
   Reaktionszeiten` 0.057, `Datenfeeds` 0.276, `Portfoliobeitrag` 0.197).
5. ~~**Demoted band as its own export section**~~ — DONE (c5f4e4c): ScanResult.demoted, a `demoted` export bucket, and a stats count.

**Target:** the 31 false positives on the 84 decoys. That number is reproducible today
(`scripts/score_test_workbook.py`), so progress is measurable per change.

### Then, in order

6. **Flip `gliner.enabled: true`** — the user's steer is to use ML by default where it
   is better, gated on Phase 3 landing, because today ML is measurably HARMFUL on
   structured workbooks (claims 3 decoys, mistypes tools as LOCATION, turned the project
   `Marschall` into a PERSON and displaced the correct finding). Needs a `-WithML`
   bundle verified on a second machine.
7. **Re-measure the real workbook.** Canonical file is
   `Downloads/mdx-big-beautiful-innovation-spreadsheet.xlsm`; baseline **442 flagged
   values / 200 PERSON / 194 bare-guess values**, taken with the pre-Phase-1 commit on
   that exact file. NOTE the exported CSV is NOT a valid baseline — it came from a
   different version of the workbook (`Malcom Werther` and `Ukom` are not in the file's
   bytes at all).

### Known bugs, unfixed, with evidence

- ~~Two Art. 9 word-list gaps~~ — FIXED (42da0e3): `neuapostolisch` (religion) and
  `Bandscheibenvorfall` (health). Found by `scripts/measure_recall.py`. Art. 9 is the
  their unambiguous siblings. structured_bare Art.9 is now 100%.
- **`*_Kommentar` columns are never treated as DESCRIPTION columns.**
  `_topical_header_res` uses `\b`, and `_` is a word character, so `\bkommentar\b` never
  matches `Strat_Innovation_Kommentar`. Free-text commentary columns are therefore never
  summarized — the same `\b`-vs-`_` defect fixed for PEOPLE headers in Phase 1, still
  present for TOPICAL ones. Deliberately not fixed: it changes what gets wholesale
  summarized, which is a behaviour decision, not a bug fix.
- **`bare_cell` recall is 30%** and no model will fix it (`german_rare/bare_cell` is
  already 100%; the obstacle is the word — "Koch" IS the word for cook). Structural
  signals only.
- **2 planted secrets still leak at apply** on the fixture: `Alteryx`, `OpenClaw` — tool
  names in prose, reached only via propagation. ML fixed `OpenClaw`; `Alteryx` remains.

### Closed, do not reopen without new evidence

- **ONNX/int8 export — DROPPED.** Its size win is partial (torch cannot leave the
  runtime: `gliner/model.py:11` AND `gliner/onnx/model.py:14` both import it) and its
  speed win is spent, since batching + memo replay already gave 3.4×.
- **The <5-minute ceiling — no longer a hard gate** (user, 2026-07-27). Quality wins
  ties. The DEFERRED content-keyed soft cap is dropped with it: it reduces coverage.
- **Label pruning (13 → 6, worth 1.8×) and skipping short structured cells** — both
  trade quality for speed, which the steer rules out. Available if that ever changes.
- **spaCy lg → sm** — tried and reverted (`d74d52c`).

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

## Phase 2 — ML measured. GLiNER earns its place; the ONNX shrink does not pay for itself yet

### The plan changed twice, both times because a measurement contradicted it

1. **The "torch-free ONNX first" decision from this grill was already dead.**
   `run_gliner-completion_2026-07-26.md:178` had recorded, from measurement, that
   PyPI's Windows torch wheel is CPU-only and there is no CUDA trap to escape.
   Re-verified here: `torch==2.13.0`, `onnxruntime==1.28.0`, **zero
   `nvidia-*`/`cuda-*`**.
   *Correction on the cause:* I first blamed a stale KNOWN-ISSUE framing in
   `GLINER_VERIFICATION_CHECKLIST.md`. That was wrong — the checklist's step 1 already
   said "the feared trap does not exist here". The real cause was that I trusted a
   summary of an earlier session instead of reading the file, then recommended the
   torch-free path in the grill on that basis. Worth recording because the failure mode
   is subtle and repeatable: a carried-over summary can be stale in ways the repo is not.
2. **ONNX+int8 cannot remove torch from the RUNTIME.** `gliner/model.py:11` imports
   torch, and so does `gliner/onnx/model.py:14` — gliner's own ONNX wrapper. The
   export therefore buys pack size only: **1.85 GB → ~0.98 GB**, not the ~520 MB I
   projected. Reaching 520 MB means dropping `gliner` and owning tokenization plus
   span decoding, which 2026-07-26 rejected and which still looks right.

Real sizes (my earlier figures were estimates): pack **1.16 GB** — already minimal,
since `fetch_gliner_model.py` excludes `pytorch_model.bin` as "1.1 GB of nothing", so
the "trim the duplicate weights" option I offered was already implemented — and
runtime **693 MB** (torch 497 of it).

### The pack works air-gapped

| check | result |
|---|---|
| loads with `HF_HUB_OFFLINE=1` | yes — 5.7 s, `UniEncoderSpanGLiNER` |
| pack size | 1126 MB |
| inference | 107–126 ms/text on CPU |
| deterministic over repeated inference | yes, 5/5 identical — the parity precondition |
| full suite with ML installed | 489 passed, no numpy/torch/spaCy conflict |

### The audit workbook is the WRONG instrument for the ML question

It scores **293/293 without ML**, so there is no headroom for a model to recover
anything. With ML: recall unchanged, the false-positive list **byte-identical**, scan
**2.6 s → 110 s (42×)**. That is not evidence GLiNER is useless — it is evidence of
measuring against a ceiling. Recorded because the mistake is easy to repeat: a 100%
instrument cannot score a recall improvement.

### `measure_recall.py` HAS headroom, and there GLiNER is decisive

| stratum | without ML | with ML |
|---|---|---|
| german_common_noun / prose_oblique | **25%** | **95%** |
| german_common_noun / prose_full_name | 90% | 100% |
| german_rare / prose_full_name | 88% | 100% |
| german_common_noun / bare_cell | 25% | **30%** |
| **OVERALL (isolated)** | **86%** | **93%** |

Recovered: Weber, Klein, Schwarz, Koch, Wolf, Berg, Fischer, Vogel, Hahn, Kaiser,
Fuchs, Sommer, Mueller, Bauer in oblique prose; Schwanitz and Stein in full-name prose.

**The honest limit: `bare_cell` barely moved (25% → 30%).** A lone common-noun surname
in a spreadsheet cell — the most frequent shape in the real workbooks — is irreducibly
ambiguous. Note `german_rare/bare_cell` was already 100% while
`german_common_noun/bare_cell` stays ~30%: the problem is the WORD, not the model.
"Koch" alone is the word for cook. No model quality fixes that; the fix is structural,
which is exactly what the Phase 1 header override did (83 people recovered on the real
workbook). Stated plainly so nobody later expects ML to close this stratum.

### …but on the structured workbook GLiNER is actively harmful right now

The 9 findings it added to the audit workbook, and ~8 are wrong:

| added | problem |
|---|---|
| `LICENSEE 'Lizenzgeber'` 0.65 | the German word for *licensor* — also a column HEADER |
| `LOCATION 'Camunda'` 0.89, `LOCATION 'OpenClaw'` 0.85 | tools mistyped as locations |
| `ORGANIZATION 'European Union'` 0.85 | a planted DECOY ("EU regulations apply") |
| `ORGANIZATION 'Nationwide Building Society'` 0.79 | a planted DECOY (counterparty) |
| `ORGANIZATION 'Ruecklage'` 0.61 | a planted DECOY — German for "reserve" |
| `PERSON 'Marschall'` 0.86 | a PROJECT name, and it DISPLACED the correct `PROJECT 'Marschall'` |
| `PERSON 'Sachbearbeiter'` 0.85 | "caseworker" — a role word |

So GLiNER is strong on PROSE recall and unhelpful-to-harmful on structured business
data. That shapes Phase 3: the veto is worth having, but ML's *additive* output on
spreadsheet-shaped text needs its own gate, and mistyping (tool → LOCATION, project →
PERSON) is a distinct failure from over-claiming.

### Two bugs this surfaced, both fixed

**a) The scorer under-reported PRECISION (mine, from the honest-metrics commit).**
`covered()` was reused for decoys, but the two questions need opposite directions: a
secret is covered only by a finding at least as WIDE, whereas a decoy is falsely
claimed even by a NARROWER finding — it still redacts part of a sentence that should
have been untouched. GLiNER claiming `Nationwide Building Society` out of the decoy
`Credit Union: Nationwide Building Society` went uncounted. Split into `covered()` and
`claims()`. **Honest non-ML precision baseline is therefore 28/84, not 21/84.**

**b) A value that is also a COLUMN HEADER made every save fail** — general, not
ML-specific. Row 1 is used only as a schema label and never scanned
(`_iter_cell_units`), but `_literal_residual` read the whole package, so the tool could
demand removal of text it had already decided never to touch and then write NO file at
all. `Lizenzgeber`, claimed from prose on one sheet, collided with the row-1 header on
another. Fixed by exempting exact column-header text from the residual check
(`_column_header_texts`). Deliberately narrow: a header CONTAINING a name still
reports that name, and a deny-list term is never exempted — the user asserted it is
PII, so a leak stays a leak. Three regression tests.

### Final scored comparison, and the ceiling breach

With the header fix in place the ML round trip completes:

| | without ML | with ML |
|---|---|---|
| recall | 293/293 | 293/293 |
| false positives on decoys | 28/84 | **31/84** |
| scan | 4.5 s | **130.1 s** |
| apply | 4.5 s | **167.3 s** |
| planted secrets leaking into the output | 2 (`Alteryx`, `OpenClaw`) | **1 (`Alteryx`)** |

ML **fixed one of the two apply-level leaks** -- `OpenClaw`, a tool named in prose that
propagation missed. That is a genuine byte-level win, not a scan-side number. It cost
3 net false positives, all three decoys claimed as ORGANIZATION.

### DEFERRED TRIGGER HAS FIRED: the 5-minute ceiling

Round trip **9 s -> 297 s (~5 min) on the 16-sheet FIXTURE**. The real workbook has
roughly 8x the cells, so it is far over. The content-keyed soft cap is no longer
deferred -- its trigger condition is met.

But the cap is the second-best lever. **`apply` spent 167 s RE-RUNNING inference**,
which is pure waste: scan already computed exactly those spans, and the model is
deterministic (verified 5/5 identical), which is what makes replay safe. Memo-replay
was explicitly part of the 2026-07-26 determinism decision -- "Pin CPU execution
provider + thread counts, add a repeated-inference determinism test, AND memo-replay so
apply never re-infers" -- and it is NOT implemented. Doing it removes ~56% of the round
trip on its own AND strengthens parity (replaying scan's spans cannot diverge from
them, whereas re-inferring can only be argued to be identical). Recommended before the
soft cap, which merely reduces coverage.
- **Two Art. 9 word-list gaps**, unrelated to ML, both in the BARE form:
  `neuapostolisch` (religion) and `Bandscheibenvorfall` (health).
- **The ONNX/int8 export is NOT started**, pending the size decision: it buys
  1.85 GB → ~0.98 GB and nothing else.

## Phase 2.5 — ML speed, 3.4× with byte-identical output

### Policy change (user, 2026-07-27)

The <5-minute ceiling is **no longer a hard blocker**: "I would rather have a quality
output and wait 5 minutes longer than being bound to an unreasoned time threshold.
However we should optimize whatever we can." So quality wins ties, ML should be used
by default where it is measurably better, and speed work is still expected — but only
where it costs nothing. The DEFERRED soft cap (which reduces COVERAGE) is therefore
dropped in favour of optimisations that change nothing about what is detected.

### The levers, measured before building anything

| lever | effect | quality cost |
|---|---|---|
| batch inference (batch 8–16) | 126.9 → 52.9 ms/text (**2.4×**) | **none** — verified byte-identical spans AND scores |
| memo-replay at apply | removes the whole 167 s | none; strengthens parity |
| label count 13 → 6 | 59.6 → 32.6 ms (1.8×) | real — drops Art.9/licensee/division |
| skip short structured cells | short cells cost 50 ms vs 78.7 ms for prose | improves precision, small recall loss |

Two results that stopped me guessing: **batch 32 is SLOWER than 16** (59.5 vs
53.0 ms/text), so the batch is pinned at 16 rather than "bigger is better"; and torch
already used all 8 cores, so thread tuning was a dead end before any time went into it.

**Built:** memo-replay + batched priming. **Not built:** label pruning and the
short-cell skip — both trade quality, and the user's steer was explicitly the opposite.

### Result

| | before | after |
|---|---|---|
| scan | 130.1 s | **63.4 s** (2.1×) |
| apply | 167.3 s | **24.4 s** (6.9×) |
| **round trip** | **297 s** | **88 s (3.4×)** |
| recall / FPs / leaks | 293/293 · 31/84 · 1 | **identical** |

### Batching under-delivered at first, and instrumenting said why

Priming only the xlsx handler's header+value texts still left **337 of 801 predictions
unprimed**, and every one was a bare cell value with no header — `'Fischer'`,
`'K-14655'`, `'Kirchweg 55, 60311 Frankfurt am Main'`. Cause: **`_with_propagation`
runs a SECOND full detection sweep over raw unit text** to seed the propagate list, so
with ML on it silently doubles the inference bill. Primed there too, in the same
function, so scan and apply still derive an identical set — parity holds by the same
argument that function already rests on.

Worth keeping in mind generally: any pre-pass that re-detects is now an ML-cost
multiplier, not just a spaCy one.

### Design notes

* The memo lives on the BACKEND and the GUI caches one analyzer per session
  (`gui/app.py::_ensure_analyzer`), so scan's predictions are still resident when apply
  re-detects. That is what makes replay free rather than a persistence problem.
* Replay makes parity **stronger**: apply reuses scan's exact spans instead of
  re-deriving spans that are only *argued* identical (they are — determinism verified
  5/5 — but reusing beats arguing).
* Memo eviction (`_MEMO_MAX`) can only cost speed, never correctness: a miss re-infers,
  and inference is deterministic.
* `prime_gliner` is duck-typed and returns 0 on anything without a registry or a
  priming backend. The hardening tests caught this — they use stand-in analyzers, and
  an optimisation must never be able to break a caller.

## RESOLVED (2026-07-27): honest metrics, and a CRITICAL Art. 9 config bug found behind them

Chasing the over-reported recall below led to the worst bug in this repo's history,
and to three fixes. All landed before Phase 2, on the principle that an evaluation
gate which over-reports would happily certify the ML work as fine.

### 1. CRITICAL -- German GDPR Art. 9 detection did not run at all

`config.py::_resync_builtins` keyed the shipped recognizers by NAME:

```python
shipped_recs = {r["name"]: r for r in shipped.get("custom_recognizers", [])}
```

A recognizer name is deliberately NOT unique -- one entity type is emitted by
several entries, a GERMAN word list and an ENGLISH one (`DE_RELIGION` ships 1 de +
2 en), plus case-sensitive twins for `DE_HEALTH_DATA` / `DE_UNION_PARTY`. Keying by
name collapsed the shipped **27 entries to 17**, and because the `en` variants sit
last in the file, every GERMAN Art. 9 word list was overwritten by its English
counterpart.

Measured on the live config: `DE_RELIGION`, `DE_HEALTH_DATA`, `DE_UNION_PARTY`,
`DE_SEX_LIFE` and `NRP` were all registered for **`en` only**. So health data,
religion, union/party membership, sex life and ethnic origin were **not detected in
German documents at all** -- the exact class of leak `356337c` fixed in the other
direction, re-introduced on any schema bump. `merge_new_recognizers` already had
this right (see `recognizer_fingerprint`, which fingerprints the GROUP for exactly
this reason); this second path had been left behind.

Fixed group-aware, provenance refreshed so the two paths agree, and
`config_schema_version` bumped **9 -> 10** purely so the repair reaches configs
already in the broken state. Verified: 17 -> 27 recognizers, all five types
registered for `de` and `en`, and `Konfession: muslimisch` /
`Diagnose: chronische Migraene` / `Gewerkschaft: ver.di` detect again. Two
regression tests in `test_config.py`.

### 2. The scorer over-reported recall

`covered()` credited a secret when the DETECTED value was a substring of it
(`fv in v`), so planted German `muslimisch` scored as caught because an unrelated
finding on a DIFFERENT SHEET matched the English `Muslim`. That is what hid bug 1
for as long as it did. Now one-directional and whole-token, reusing
`evaluation._whole_token` (which already had the honest matcher, and the same
lesson in its docstring).

Tightening it initially swung the error the other way -- 12 postal addresses
reported as missed although DE_ADDRESS claims them as TWO spans ("Kirchweg 55" +
"60311 Frankfurt am Main", only the ", " unclaimed). So coverage may now come from
several findings together, by token union, with the limitation stated in the code.

### 3. A false positive could block the save entirely

Decisions are keyed by VALUE while apply re-detects per CELL, so a value falsely
claimed in one cell becomes a "removed value" that survives verbatim wherever
detection does not fire, and `_literal_residual` correctly refuses to write
anything. Four enum decoys did exactly that. Phase 3 of the scorer now skips
falsely-claimed decoys as a reviewer would, or one FP makes the apply path
unmeasurable.

The same investigation turned up a second, sharper instance: **openpyxl stamps
today's date into `dcterms:created`/`modified` on every save**, so a document
containing today's date as a data value, with dates being redacted, failed the
verify and produced NO output file -- nothing actually wrong. Date-dependent and
intermittent. Both fields are now scrubbed (privacy AND correctness); regression
test in `test_xlsx.py`.

### Honest baseline, as of this commit

`scripts/score_test_workbook.py` now exits 0 on the 16-sheet fixture:

| | value |
|---|---|
| recall | **293/293 (100%)** -- honestly matched |
| false positives on decoys | **21/84** |
| apply + fail-loud verify | passes |
| known leaks | 2 (`Alteryx`, `OpenClaw` -- tool names in prose, propagation) |

The 21 FPs are the enum-vocabulary and German-compound-noun classes. That is the
number Phase 3 has to move, and it is now reproducible.

## ORIGINAL FINDING (superseded by the above, kept for the record)

Discovered while extending the fixture. `scripts/score_test_workbook.py:165` counts a
planted secret as found when the detected value is a **substring** of it:

```python
for fv, g in found_vals.items():
    if v in fv or fv in v:     # <-- the `fv in v` direction is unsound
        return g
```

The `v in fv` direction is legitimate (a wider span, e.g. an address block, really
does cover the planted value). The `fv in v` direction is not: a SHORTER detection
does not cover a longer secret, and it does not even have to come from the same
sheet.

Measured consequence, and it is not hypothetical:

* `muslimisch` is planted twice as `special_category` in `Personal Vertraulich`
  (D4, D7) and is **not detected at all** in a full-document scan. Neither are
  `roemisch-katholisch`, `evangelisch` or `konfessionslos`. The only DE_RELIGION
  values the scan finds are `Muslim`, `Protestant`, `Roman Catholic`, `Buddhist` --
  all from the ENGLISH sheet.
* They are nevertheless scored as caught, because the English `Muslim` is a
  substring of `muslimisch`.
* So **`special_category 34/34 = 100%` is false, and the `204/206` total this
  run-file recorded as the DONE-WHEN gate is over-reported.** Confirmed identical on
  the pre-existing fixture, so this is long-standing and not introduced here.

`_literal_residual` is what exposed it: the removed value `Muslim` survives
verbatim in the output because it sits inside an undetected `muslimisch`, and apply
correctly refuses to write the file. That is the fail-loud contract working exactly
as designed -- the scan-side recall number was the thing that was wrong.

Not fixed here, deliberately: tightening `covered()` lowers every recorded recall
number in the repo, and re-baselining the gate mid-run is the user's call, not a
measurement default. Recommendation: match whole-token and keep only the `v in fv`
direction, then re-baseline. NOTE: in isolation with `languages: ["de"]`,
`Konfession: muslimisch` IS detected as DE_RELIGION -- so the German Art.9 miss is
specific to the full-document path, and the cause is not yet identified. Per-sheet
routing is correct (`Personal Vertraulich` -> `de`) and per-text routing of
`muslimisch` -> `de`, so neither is the explanation. Open.

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

Separately confirmed that a URL inside a description cell does NOT suppress the
whole-cell DESCRIPTION claim, which would have been a leak.

#### The exported CSV is NOT a valid baseline — and two conclusions drawn from it were wrong

`Malcom Werther` and `Ukom` appear in the exported CSV but are **not present in
`Downloads/mdx-big-beautiful-innovation-spreadsheet.xlsm` at all** (searched the
raw parts, not just the cell model). The export therefore came from a DIFFERENT
version of the workbook, and every before/after count taken against it is
confounded. Re-measured properly instead: the pre-Phase-1 commit and HEAD, both
scanning the identical file with the identical shipped config, in a git worktree.

| | before (22443bf) | after (34b23ad) |
|---|---|---|
| flagged values | 442 | **481** |
| flagged occurrences | 2723 | 2770 |
| PERSON | 200 | 203 |
| DESCRIPTION | 150 | 145 |
| URL | 0 | **41** |
| DATE_TIME | 32 | 32 |
| possible-miss rows | 107 | **74** |
| bare-guess values / occurrences | 194 / 1593 | **183 / 1560** |
| scan | 22.6s | 23.2s |

Two claims made against the CSV baseline and now retracted:

* **"PERSON went 96 → 203, so fixing the `_x001E_` escape unmasked a flood of
  German-noun noise."** Wrong. On this file PERSON is **200 → 203** — flat. The
  `Must-Haves` / `Datenfeeds` / `Kernworkflow` values were already being flagged
  before Phase 1; they were absent from the old CSV only because it is a different
  workbook. Phase 1 added no measurable noise.
* **"DESCRIPTION 237 → 145, of which 80 were snake_case field identifiers."** True
  of the old CSV, not of this file: the real change here is **150 → 145**.

What Phase 1 actually did to the totals: **the entire flagged increase is the 41
links** that previously left in the clear. Bare-guess values went DOWN (194 → 183)
and possible-misses fell by a third, so precision improved slightly rather than
degrading. The German-common-noun flood is untouched and remains Phase 3's job.

(The two names the check reports as missing, `Florian Brueckner` and
`Nils Braeunlich`, are transliteration artifacts in the probe list — the workbook
spells them `Brückner` and `Bräunlich`, and both ARE caught. `Constanza Hiemenz`
went missing → caught, confirming the NER_MISC retype.)
