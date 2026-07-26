# GLiNER — verification checklist

The do-list for taking GLiNER from "scaffolded, disabled by default" to "verified
working on a real document". Depth and rationale live in
`docs/run_gliner-integration_2026-07-24.md` (original design) and
`docs/run_gliner-completion_2026-07-26.md` (what actually happened on a connected
machine, including three offline failures the design could not have found).

**Current state (2026-07-26, after the merge at `1ac90e4`):** Phases A+B+C done.
The model pack is **built and verified loading fully offline**, and the loader is
fixed for the air-gap. GLiNER still ships **disabled** (`gliner.enabled: false`)
— flipping that is gated on the real-workbook measurement in step 5, which is
**yours**, because it needs your reference document. **418 tests green.**

---

## ✅ 0. Baseline — DONE
- [x] Suite green: **418 passed** (was 203 pre-merge; the audit-remediation branch
      merged in). Use 418 as the "before" number, not 203.
- [x] Settings shows the **"AI detection (GLiNER)"** card with its status line.

## ✅ 1. ML runtime — DONE, and the feared trap does not exist here
- [x] **The torch/CUDA trap was a Linux-marker artifact of `uv lock`.** On this
      platform `gliner + onnxruntime` resolves with **zero `nvidia-*`/`cuda-*`
      packages**: PyPI's Windows torch wheel is CPU-only (122 MB), because
      CUDA-enabled Windows builds live on the separate pytorch index.
- [x] **Measured full install: 693 MB** (torch 471, transformers 51, onnxruntime
      40, sympy 29, numpy 43, rest <10 each).
- [x] **Consequence:** we keep the upstream `gliner` package. The torch-free ONNX
      path — reimplementing GLiNER's tokenizer and span decoding and owning that
      drift forever — is **not needed and not being done**. `gliner.onnx` is now
      `false` in the shipped config.
- Install with: `uv sync --extra ml`

## ✅ 2. Model pack — DONE, and it needs three things its own repo doesn't ship
```
uv run python scripts/fetch_gliner_model.py vendor/gliner-model
```
- [x] Pack built: **~1.16 GB** (fp32 `model.safetensors` 1156 MB + fast tokenizer
      16 MB + spm 4.3 MB + a 4.3 MB pack-local `hf-cache/`).
- [x] `urchade/gliner_multi-v2.1` ships **only** config + weights. The pack also
      needs (a) the mDeBERTa **tokenizer**, (b) a `tokenizer_class` key so
      transformers 5.x can build it, and (c) the **base encoder config** in a
      pack-local HF cache. All three are missing in a way that fails **only on a
      machine with no network** — see the script's docstring.
- [ ] *(optional, not done)* int8 ONNX export to shrink 1156 MB → ~290 MB. No
      upstream ONNX build exists and gliner ships no exporter, so this is our own
      torch.onnx/optimum step. Only worth it if bundle size actually bites.

## ✅ 3. Backend smoke test — DONE (offline)
Verified with `HF_HUB_OFFLINE=1`, which is the only honest test:
```
loaded in 4.0s   type=UniEncoderSpanGLiNER
'Ada Lovelace arbeitet bei DeepL Pro in Karlsruhe.'   (122 ms)
    person 'Ada Lovelace' 0.991 | organization 'DeepL Pro' 0.962 | location 'Karlsruhe' 0.987
'Das Projekt Derivatefreiheit wurde von Herrn Klaus Mueller geleitet.'   (56 ms)
    project 'Derivatefreiheit' 0.719 | person 'Herrn Klaus Mueller' 0.942
'Die Effizienz der Reaktionszeiten war zufriedenstellend.'   (54 ms)   -> (no entities)
deterministic across repeated inference: True
```
- [x] Mixed-language win real: the English product name inside German prose.
- [x] A German noun-shaped **project name survives**; ordinary nominalizations
      (*Effizienz*, *Reaktionszeiten*) are **not** flagged.
- [x] **Deterministic** — the property scan/apply parity depends on.
- [x] ~55–120 ms per text on CPU.

## 4. Functional check in the app — PARTLY DONE
- [x] Loader hardened for the air-gap: `local_files_only=True` (a hub lookup on an
      air-gapped box does not fail fast — it **stalls inside the scan**, which
      reads as a hung app), pack-local `cache_dir`, and `model.eval()`.
- [ ] Turn the Settings **AI detection** toggle on; confirm the status line reads
      `runtime: installed · model: … (NNN MB)`.
- [ ] Scan a small German doc and confirm GLiNER findings appear with source
      `gliner` in the review screen.
- [ ] **Hard-fail check:** rename `vendor/gliner-model`, keep GLiNER enabled,
      scan → expect a clear actionable error, not a silent degrade.

## 5. ⚠️ DONE-WHEN measurement on YOUR reference workbook — **yours to run**
This is the gate on shipping GLiNER enabled, and it needs your real document.
Run it **twice**, once with GLiNER off and once on:
- [ ] **Recall ↑** — GLiNER recovers clearly-missed names/orgs. Diff the
      "Export flagged terms" CSVs from the two runs.
- [ ] **Precision held** — total flagged **≤ ~445** (current baseline). If it
      blows past, raise `confidence_override` / lower sensitivity.
- [ ] **Speed** — typical scan **< 5 min** with GLiNER on.
- [ ] Record the three numbers in the run-file progress log.

## 6. Scan/apply parity — must not regress
- [ ] Apply on a GLiNER-enabled scan and confirm `verify_output` passes with zero
      residual. Determinism is verified (step 3); the planned **memo replay** (apply
      reuses scan's GLiNER output rather than re-inferring) makes this structural.

## 7. (Optional) spaCy lg→sm downgrade
- [ ] Switch `engine.SPACY_MODELS` to the `sm` models, update the wheel URLs,
      `uv sync`, then run `pytest tests/test_precision.py tests/test_language.py`.
      **If the German-noun precision tests regress → revert to `lg`** and accept
      the size cost. `_is_pos_implausible` / `_is_german_nominalization` read
      spaCy POS directly, so this is load-bearing.

## 8. Build the bundle + the deferred items
- [ ] `./scripts/build_offline_bundle.ps1 -WithML` — it copies
      `vendor\gliner-model` and `launch.bat` sets `ANONYMIZER_GLINER_MODEL`.
- [ ] **Soft cap** — now much simpler than designed: with memo replay, a plain
      **scan-time counter** is parity-safe and the content-keyed allow-set is no
      longer needed. Log a visible notice when it trips.
- [ ] **Cell-level DESCRIPTION flag** — escalate to GLiNER2 if v2.1's zero-shot
      description quality disappoints.

## 9. Flip the shipped default
- [ ] Only once step 5 passes: set `gliner.enabled: true` in
      `anonymizer/data/default_recognizers.yaml` (`_resync_builtins` preserves any
      user's explicit choice). Bump `config_schema_version` with it.

---

## Rollback
- Settings toggle **off** (or `gliner.enabled: false`) → instantly back to spaCy +
  gazetteer, no code change. This is also the escape hatch the load-failure error
  points at.
- Or revert the GLiNER commits (`258cb35`..`039a15e`, plus the 2026-07-26 fixes).

## Quick reference
- Recognizer + loader: `anonymizer/gliner_recognizer.py`
- **Pack builder: `scripts/fetch_gliner_model.py`** (run on a connected machine)
- Precision-gate override / detect hook: `anonymizer/core.py`
- Config block: `anonymizer/data/default_recognizers.yaml` (`gliner:`), schema v7
- Settings UI: `anonymizer/gui/settings_page.py` (`_gliner_section`)
- Bundle: `scripts/build_offline_bundle.ps1 -WithML`, `scripts/bundle_templates/launch.bat`
- Tests: `tests/test_gliner.py`, `tests/test_gui_render.py`
- Design: `docs/run_gliner-integration_2026-07-24.md`
- **What actually happened + remaining plan: `docs/run_gliner-completion_2026-07-26.md`**
