# Run-file — GLiNER completion + post-merge robustness

**Date:** 2026-07-26
**Starts from:** `1ac90e4` (merge of the GLiNER/precision/topical branch into the
audit-remediation master; 407 tests green)
**Grill:** 27 questions / 7 rounds — decisions table below.
**Supersedes as the live do-list:** `docs/GLINER_VERIFICATION_CHECKLIST.md`
(kept, and refreshed step-by-step as items complete; its "expect 203 passed"
baseline is stale — the post-merge baseline is **407**).

---

## GOAL

Take GLiNER from "scaffolded, disabled, no model" to "verified working on this
connected machine", and land the correctness/robustness fixes the merge exposed —
without regressing the ~445-flag false-positive baseline or the scan/apply parity
invariant.

## CONTRACT

- **Input:** unchanged — same documents, same formats.
- **Output:** unchanged `ScanResult`/`Finding` shape. GLiNER hits carry
  `source="gliner"` and map onto existing entity types.
- **Side effects:** a vendored model under `vendor/gliner-model/` (gitignored);
  a new `ml` install path; audit-log entries gain detection provenance; the 5
  existing profiles gain threshold ownership.
- **Invariant (HARD):** scan/apply parity. `verify_output` must still pass. This
  is now structural rather than probabilistic — see *memo replay* below.
- **Invariant (HARD):** no ML-sourced finding may be auto-applied one-way in an
  Art. 9 category. A human confirms every one.

## DECISIONS (from the grill)

| # | Decision | Choice |
|---|----------|--------|
| Scope | This session | Checklist steps 1–4 (runtime, model, smoke, functional) + the Art.9 span split. Step 5 (real workbook) stays with Moe. |
| Runtime | torch vs ONNX | **Timeboxed (~1h) torch-free ONNX path**; fall back to CPU-only torch and record the size cost rather than open-ended reimplementation. |
| Test data | Measurement corpus | **Synthetic** representative German workbook. No real customer data enters this session. |
| Model | Variant | **gliner_multi-v2.1** as already configured. Alternatives are DEFERRED on a weak-recall trigger. |
| Parity | Determinism strategy | **Pin CPU execution provider + thread counts, add a repeated-inference determinism test, AND memo-replay** so apply never re-infers. |
| Trust | GLiNER hit provenance | **Third state — "AI-detected"** beside pattern-backed and NER-guess. Not forced into the existing binary. |
| Art.9 | ML + special category | **Art.9 labels added, but review-only** — never auto-applied, because the action is irreversible. |
| Cap | Soft cap | **Scan-time counter only**; apply replays the memo. The content-keyed allow-set is no longer needed and is dropped. |
| UI | AI surfacing | **Stat tile + tier band + row chip**, mirroring the existing NER-guess split. |
| Labels | Extensibility | **User labels merged with shipped**, provenance-fingerprinted like `custom_recognizers`, so `_resync_builtins` cannot eat them. |
| Failure | Load failure | **Hard-fail, with a one-click "disable ML and scan anyway"** in the error itself. |
| Art.9 split | Survivors | **PERSON + checksum-validated IDs** survive and cut the span. |
| Art.9 split | Fragments | Fragments **containing a letter** stay Art.9 one-way; letter-free fragments are dropped. |
| spaCy | lg→sm | **Try it; revert to lg if the precision tests regress** (the recorded DEFAULT). |
| Settings | Thresholds | **Profile-owned.** The 5 existing profiles each carry their GLiNER thresholds; raw knobs move behind an Advanced expander that marks the profile modified when touched. |
| Settings | Profile + ML | A profile may **request** ML; the request is honoured only when a model is present, so a profile can never create the hard-fail dead-end. |
| Settings | Switching | **Split**: action changes apply live; detection changes prompt a one-click re-scan instead of silently showing stale results. |
| Compliance | Provenance | **Audit log per run** records spaCy models, GLiNER model id/version, whether ML was honoured, active profile, effective cutoffs. |
| Robustness | Sheet titles | Renaming a sheet **requires corroboration** — a bare NER guess is not enough to rewrite a structural element and every formula referencing it. |
| Packaging | Upgrades | **Versioned model pack** discovered via `ANONYMIZER_GLINER_MODEL`, with a documented drop-in procedure — no multi-GB re-copy for a model swap. |
| Process | Sequencing | **Correctness → GLiNER → UX.** Every stopping point leaves the tool strictly better. |
| Process | Git | Commit per logical step on `master`, tests green. **Nothing pushed** without Moe asking. |
| Process | Model location | `vendor/gliner-model/`, with `vendor/` added to `.gitignore`. |

## PLAN (ordered — stop anywhere and the tool is still better)

**Stage 1 — correctness (no model needed, fully verifiable here)**
1. Art.9 span splitting in `core._resolve_overlaps`: a contained PERSON or
   checksum-validated ID survives; the Art.9 span splits around it; lettered
   fragments keep the one-way action; letter-free fragments are dropped. Tests
   must pin the non-overlap invariant and prove no spliced/garbled output.
2. Sheet-title redaction requires corroboration (not a bare NER guess).

**Stage 2 — GLiNER runtime + model (needs the network, which is up)**
3. Resolve the torch/CUDA trap; prove a CPU-only import set. Timebox the
   torch-free ONNX path, then fall back.
4. Fetch `urchade/gliner_multi-v2.1`, export to ONNX, int8-quantise, vendor into
   `vendor/gliner-model/`. Record on-disk size.
5. Smoke-test `load_gliner_backend(...).predict(...)`.
6. Pin execution provider + threads; determinism test; memo replay so apply
   never infers; scan-time soft cap with a visible notice.
7. Functional check on the synthetic German workbook: mixed-language tool name
   caught, high-confidence German noun-like project name kept, `Effizienz` not
   flagged, hard-fail path shows the one-click disable.

**Stage 3 — UX, config and provenance**
8. "AI-detected" third state: stat tile, tier band, row chip.
9. Profile-owned thresholds + Advanced expander + re-scan prompt on detection
   changes; profile may request ML, honoured only if the model is present.
10. Art.9 GLiNER labels, forced to review tier.
11. User-extendable labels with provenance fingerprints.
12. Detection provenance in the audit log.
13. spaCy lg→sm trial, reverting on precision regression.

## TESTS (success criterion)

- Art.9 split: contained PERSON/validated ID survives and stays reversible;
  health text on both sides still one-way; no overlapping spans; letter-free
  fragments produce no token; a diagnosis containing commas is NOT truncated.
- Sheet title: a corroborated name still renames; a bare NER guess does not.
- Determinism: repeated inference over the same text yields identical spans.
- Parity: apply performs **zero** inference calls (memo replay), and
  `verify_output` passes.
- Cap: the scan-time cap logs a visible notice; apply is unaffected by it.
- Profiles: switching a profile that changes detection prompts re-scan; an
  action-only change does not; a profile requesting ML with no model installed
  does not enable it and does not hard-fail.
- Art.9 + ML: an ML-sourced Art.9 finding is never auto-applied.
- Labels: a user-added label survives a schema bump / `_resync_builtins`.
- Full suite green (baseline **407**, not 203).

## AUTONOMY CONTRACT

**DONE-WHEN** — all of:
1. Full suite green, at or above 407 tests, order-independent.
2. Stage 1 complete with the tests above.
3. `load_gliner_backend` loads the vendored model and predicts on this machine;
   import set verified CPU-only (no `nvidia-*`/`cuda-*` wheels).
4. Synthetic-workbook functional check passes; scan under 5 minutes.
5. Apply performs zero GLiNER inference (parity structural), `verify_output`
   passes.
6. Run-file progress log + checklist updated to match reality.

**DEFAULTS** (pre-authorized — proceed, don't ask):
- ONNX timebox ~1h → fall back to CPU-only torch, record the size delta.
- int8 costing >3 pts recall on the synthetic set → fall back to fp32 ONNX.
- spaCy `sm` regressing `test_precision.py`/`test_language.py` → revert to `lg`.
- `min_chars` 3; scan-time cap 5000 ML-eligible cells; `confidence_override`
  0.85; sensitivity mapping [low→0.6 … high→0.4].
- Label→entity map as configured; Art.9 labels map to the `DE_*` types.
- `gliner.enabled` stays **false** in the shipped default until Moe's real
  workbook measurement passes (checklist step 9 is his call, not this run's).

**DEFERRED** (postponed + resurface trigger):
- PII-tuned variants (`gliner_multi_pii-v1`, NVIDIA, GLiNER2-PII) — resurface if
  v2.1 recall on the synthetic set disappoints.
- GLiNER2 schema path / cell-level DESCRIPTION classification — resurface once
  v2.1's description quality is measurable on a real document.
- Embedding fuzzy-gazetteer — only if post-GLiNER misses are dominated by
  typo/variant forms of known terms.
- Mapping-store provenance stamping — audit-log provenance ships first; extend to
  the encrypted store only if a re-identify audit needs it.
- Real-workbook DONE-WHEN measurement (recall Δ, FP ≤ 445, scan time) — Moe's,
  on his reference workbook.

**ROLLBACK** — GLiNER stays behind `gliner.enabled` (shipped false); disabling
restores exact pre-GLiNER behaviour with no code change. Stage 1 correctness
fixes are independent commits, revertable individually. spaCy model choice is a
`pyproject` revert. Blast radius is the detection layer; the parity invariant
protects written output.

## OUT OF SCOPE

No local LLM. No change to the reversible-mapping / re-identify flow. No new
redaction modes. No cross-document learned gazetteer. No GPU path. No push to
`origin`. No real customer data.

---

## PROGRESS LOG

_(updated as work lands)_

- **2026-07-26** — Merge `1ac90e4` resolved, 407 tests green. Grill complete
  (27 questions / 7 rounds); this run-file written. Network verified reachable.
  Signed off.
- **2026-07-26 — Stage 1 complete ✅ (416 tests green)**
  - `fc42a69` **Art. 9 span splitting.** `_resolve_overlaps` now cuts a one-way
    Art. 9 span around a contained PERSON or checksum-validated id instead of
    destroying it. Lettered fragments keep the one-way action; letter-free gaps
    are dropped; a survivor covering the parent entirely does NOT split it (that
    would delete the Art. 9 finding and silently make the value reversible).
    Survivors are de-overlapped against each other first — each was compared only
    against the parent on the way in. 7 tests, incl. one pinning the *rejected*
    comma-termination fix and one asserting no alphabetic character of the
    original span is left uncovered. **Closes the open audit item.**
  - `3748c3d` **Sheet titles require corroboration.** A bare spaCy guess can no
    longer rename a worksheet (measured: "Tab" → PERSON @0.85 → tab renamed and
    every reference rewritten). Pattern/checksum/deny-list/name-column/propagated
    hits still rename. Parity holds by construction: a filtered finding never
    reaches decisions, so apply's `redact()` leaves the title alone.
- **2026-07-26 — Stage 2 finding: the torch/CUDA trap does NOT apply on Windows.**
  `uv pip compile` of `gliner + onnxruntime` on this machine resolves to
  **torch 2.13.0 with no `nvidia-*` and no `cuda-*` packages at all**, and the
  PyPI `torch` **win_amd64 cp312 wheel is 122 MB** — PyPI's Windows torch is
  CPU-only, because CUDA-enabled Windows builds live on the separate pytorch
  index. The ~500-line CUDA lock recorded on 2026-07-24 was a **Linux-marker
  artifact of `uv lock`** resolving every platform, not what this deployment
  installs. The tool targets Windows exclusively (`.bat` launchers,
  `%LOCALAPPDATA%`, a PowerShell bundle script).
  **Consequence:** the risky torch-free path — reimplementing GLiNER's tokenizer
  and span decoding against onnxruntime, and owning that drift forever — is
  **not needed**. We keep the upstream `gliner` package (correct, maintained) AND
  stay well under the 1 GB target. This is better than either option the grill
  offered; the ONNX timebox is therefore spent, unused.
  **Measured install** (throwaway venv, `gliner 0.2.28` + `onnxruntime 1.28`):
  **693 MB total, zero `nvidia-*`/`cuda-*` packages.** Breakdown: torch 471 MB,
  transformers 51, onnxruntime 40, sympy 29, numpy 43, the rest <10 each.
- **2026-07-26 — Stage 2 finding: the vendored pack needs a TOKENIZER that its
  own repo does not ship.** `urchade/gliner_multi-v2.1` contains exactly three
  files — `gliner_config.json`, `model.safetensors`, `pytorch_model.bin`. No
  tokenizer. Its `gliner_config.json` names `microsoft/mdeberta-v3-base` as the
  encoder, so `from_pretrained(..., load_tokenizer=True)` — which is what
  `load_gliner_backend` calls — resolves the tokenizer **from the Hub**. On the
  air-gapped target that is a hard failure at first scan, and it would only ever
  have been discovered on a machine with no network. The pack must therefore
  vendor the mDeBERTa SentencePiece tokenizer alongside the weights (this is also
  why `sentencepiece` appears in gliner's dependency closure). Verification is by
  loading with `HF_HUB_OFFLINE=1`, which is the only honest test of "does this
  work air-gapped".
- **2026-07-26 — Corrected a false alarm:** `dir(GLiNER)` shows no
  `predict_entities`, which looked like the scaffolding calling a non-existent
  API. It is not: `gliner.GLiNER` is a *factory* that swaps in a concrete variant
  (`UniEncoderSpanGLiNER` et al.), and `predict_entities(text, labels, flat_ner,
  threshold, ...)` lives on `BaseEncoderGLiNER`. `_OnnxGlinerBackend.predict` is
  correct as written. Recorded so the next reader does not re-derive it.
- **ONNX export:** gliner ships ONNX *runtime* wrappers (`gliner/onnx/model.py`,
  `*ORTModel`) but **no exporter** in the package, and the upstream repo publishes
  no pre-exported ONNX build. Export is therefore our own step (torch.onnx /
  optimum) — deferred behind a working torch-path smoke test, since the torch
  path is now known to be small enough to ship on its own. `gliner.onnx` flipped
  to `false` in the shipped config, `config_schema_version` 6 → 7.

### Stage 2 — three offline failures, all found by cutting the network

Each of these fails **only** on a machine with no network. On the build box every
one of them silently succeeds by reaching the Hub, so none could have been found
without deliberately setting `HF_HUB_OFFLINE=1`. This is why the pack is now built
by a committed script (`scripts/fetch_gliner_model.py`) rather than by hand.

1. **No tokenizer in the model repo.** `urchade/gliner_multi-v2.1` contains
   exactly `gliner_config.json`, `model.safetensors`, `pytorch_model.bin`. The
   tokenizer belongs to the base encoder named in the config
   (`microsoft/mdeberta-v3-base`), so `load_tokenizer=True` resolves it from the
   Hub. → vendor the mDeBERTa tokenizer into the pack.
2. **No `tokenizer_class`, so AutoTokenizer refuses.** mdeberta's
   `tokenizer_config.json` has two keys and names no tokenizer class, and the pack
   has no `config.json` for AutoTokenizer to infer a model type from — transformers
   5.13 then raises a *misleading* "you need sentencepiece installed" (it was
   installed). → write `tokenizer_class: DebertaV2Tokenizer`, then re-serialize a
   **fast `tokenizer.json`** so the target does no SentencePiece conversion at all.
3. **Encoder config fetched by hub id.** `gliner/modeling/encoder.py` calls
   `AutoConfig.from_pretrained("microsoft/mdeberta-v3-base")` — a hub id, not a
   path. It forwards `cache_dir`, so the pack ships a **4.3 MB pack-local
   `hf-cache/`** (config + tokenizer only, never the base encoder's weights) that
   `load_gliner_backend` passes back in. Chosen over rewriting
   `gliner_config.json`'s `model_name` to an absolute path because the bundle is
   copied to an arbitrary folder off a network share — the pack must stay
   **relocatable**.

**Loader hardening** (`gliner_recognizer.load_gliner_backend`): now passes
`local_files_only=True`, the pack-local `cache_dir` when present, and calls
`model.eval()`. The first matters most — without it a hub lookup on an air-gapped
box does **not** fail fast, it stalls on a connection timeout *inside the scan*,
which reads to the operator as a hung application rather than a missing file.
Pinned by two tests that inject a fake `gliner` module (the suite still runs with
no ML stack installed).

**Verified offline** (`HF_HUB_OFFLINE=1`), model pack ~1.16 GB:
- loads in 4.0 s as `UniEncoderSpanGLiNER`; ~55–120 ms per text on CPU
- `'Ada Lovelace arbeitet bei DeepL Pro in Karlsruhe.'` → person 0.991,
  **organization 'DeepL Pro' 0.962** (the mixed-language win), location 0.987
- `'Das Projekt Derivatefreiheit ... Herrn Klaus Mueller ...'` → **project
  'Derivatefreiheit' 0.719** (a German noun-shaped project name survives), person 0.942
- `'Die Effizienz der Reaktionszeiten war zufriedenstellend.'` → **no entities**
  (the nominalizations that plague spaCy)
- **deterministic across repeated inference: True** — the parity property
- full suite **418 passed** with the ML stack installed (no numpy/torch/spaCy conflict)

**End-to-end through the REAL shipped path** (`load_gliner_backend` →
`build_analyzer(gliner_backend=…)` → `core.detect_unit`), still offline:

```
'Die Abteilung nutzt das externe Werkzeug DeepL Pro fuer Uebersetzungen.' (152 ms)
    DEPARTMENT     'Abteilung'                     0.51  src=gliner
    TOOL           'DeepL Pro'                     0.77  src=gliner
'Das Projekt Derivatefreiheit wurde von Herrn Klaus Mueller geleitet.'    (63 ms)
    PROJECT        'Das Projekt Derivatefreiheit'  0.73  src=gliner
    PERSON         'Klaus Mueller'                 0.91  src=gliner
'Die Effizienz der Reaktionszeiten war zufriedenstellend.'               (65 ms)
    (none)
'Diagnose: Diabetes mellitus Typ 2, Herr Klaus Mueller, IBAN DE89370400440532013000'
    DE_HEALTH_DATA 'Diabetes mellitus Typ 2, Herr'  0.86  src=PatternRecognizer
    PERSON         'Klaus Mueller'                  0.97  src=gliner
    DE_HEALTH_DATA ', IBAN'                         0.86  src=PatternRecognizer
    IBAN_CODE      'DE89370400440532013000'         1.00  src=IbanRecognizer
detect_unit deterministic: True
```

The last case is **Stage 1's Art. 9 split confirmed against the real model**: the
name and the IBAN survive as their own reversible findings while the health text
either side of them stays one-way. Note the surviving PERSON here came from
GLiNER — `_survives_special_category` keys on entity type and checksum, not on
source, which is the intended reading of the grill decision.

**Two tuning observations for the step-5 measurement (not defects):**
- `DEPARTMENT 'Abteilung' 0.51` — the German common noun for "department" itself,
  matched as a department. A low-confidence zero-shot false positive of exactly
  the kind `min_score` exists to trim; it sits just above the 0.3 floor.
- `PROJECT 'Das Projekt Derivatefreiheit' 0.73` — the span over-reaches to include
  the article and the label word. Harmless for redaction (it over-covers) but it
  makes the pseudonym token less readable.
  Both are threshold/`min_score` calibration, which the real-workbook run drives.

### Stage 3 — complete ✅ (442 tests green)

- `a84ef58` **AI-detected as a third provenance state + Art. 9 ML hits are
  review-only.** `is_ai_detected` on GroupedFinding (set when ANY occurrence came
  from the ML pass). It was already mislabelling things: the stat bar counted ML
  hits inside "likely PII", and the Medium tier's two-way split filed them under
  "pattern-backed" — a zero-shot judgement presented as rule-anchored, in the one
  place the reviewer decides how far to trust a band. Now a tile (hidden when no ML
  ran), a three-way band split, and a per-row chip. Art. 9 labels added to the
  GLiNER map, but an ML-sourced Art. 9 finding is forced out of auto-accept however
  high it scored — the action is irreversible and zero-shot confidence is not
  calibrated evidence. Anchored Art. 9 keeps whatever tier it earns.
- `3cfbc21` **GLiNER labels are user-extendable**, with provenance so a schema bump
  cannot eat them: shipped+untouched re-syncs, shipped+edited is preserved,
  user-added always survives. Recorded on FIRST RUN, not only at bump time.
- `dfbd61c` **Detection provenance.** A value-free line (spaCy models, GLiNER pack +
  cutoffs, profile, sensitivity, tiers, corroboration_only) into the per-document
  `_report.json` and — without the filename — the audit log. Derived from the
  NARROWED cfg, so it describes the stack that actually ran.
- `47e0745` **Profiles own the ML cutoffs.** The five profiles carry min_score /
  confidence_override; raw knobs moved behind a Settings "Advanced" expander. A
  profile may REQUEST ML but never turns it off, and the request is honoured only
  when a pack is installed — so a dropdown can never create the
  enabled-but-missing state that hard-fails every scan. Because profiles now change
  DETECTION, switching one shows a re-scan notice instead of leaving results that
  quietly disagree with the settings above them.
- **spaCy lg→sm: TRIED AND REVERTED.** See the checklist. The headline is that the
  recorded gate (`test_precision.py` + `test_language.py`) went **green** while the
  full suite caught a document that could no longer be saved at all. The gate
  watched precision and never watched recall. Any future model swap runs the full
  suite.

### Where this run stopped

**All three stages are complete.** 442 tests green, tree clean, nothing pushed.
Checklist steps 0–3 and 7 done; step 4 done except the in-app click-through.

**Remaining, and both are Moe's:**
1. **Checklist step 5** — the recall / FP ≤ 445 / scan-time measurement on the real
   reference workbook. This gates step 9 (flipping `gliner.enabled` to true), and
   it needs his document, which never enters an agent session.
2. **Checklist step 4's click-through** — enable the toggle in the running app,
   confirm the status line, and pull the model folder to see the hard-fail error.
   Automatable in principle, but the point of it is that a human sees the message.

**Deferred, unchanged:** PII-tuned GLiNER variants (trigger: weak recall on the
real workbook), GLiNER2 + the cell-level DESCRIPTION flag, embedding fuzzy
gazetteer, mapping-store provenance stamping, and the int8 ONNX export (only worth
it if bundle size actually bites — it no longer obviously does).

**One thing worth deciding later:** a user can ADD a GLiNER label but not REMOVE a
shipped one. The measured `DEPARTMENT 'Abteilung' 0.51` false positive is
re-pointable (that is what the provenance work bought) but not deletable. Left
alone deliberately rather than invented on the spot — resurface it if the real
workbook shows shipped labels doing more harm than good.

**Environment note:** the project `.venv` now has the ML stack installed
(`uv sync --extra ml --extra dev`). A bare `uv sync` prunes BOTH extras and
removes pytest; `uv sync --extra dev` alone drops GLiNER back out, which is a
clean way to get the 693 MB back.
