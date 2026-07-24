# GLiNER — weekend verification checklist

A hands-on checklist to take the GLiNER integration from "scaffolded, disabled by
default" to "verified working on a real document." Do this on a **connected
machine** (the model must be fetched once). Depth/rationale for every step lives
in `docs/run_gliner-integration_2026-07-24.md`; this file is the do-list.

**Current state:** Phases A + B done, Phase C offline scaffolding done. GLiNER ships
**disabled** (`gliner.enabled: false`) with no model present. 203 tests green.

---

## 0. Baseline (5 min)
- [ ] `git pull` (branch `master`).
- [ ] `uv sync` then `uv run pytest -q` → expect **203 passed**. This is the "before" state with GLiNER off.
- [ ] Launch the app, open Settings → confirm the new **"AI detection (GLiNER)"** card shows a toggle and a status line reading `runtime: MISSING · model: not found …` (correct while nothing is installed).

## 1. Install the ML runtime — mind the torch/CUDA trap (15–30 min)
> **Known issue:** `gliner` pulls the **full torch + CUDA stack** transitively. A naive install is many GB and GPU-oriented — the opposite of the ONNX/<1GB goal.
- [ ] Preferred: install a **torch-free ONNX path** — `onnxruntime` + `transformers`/`tokenizers` only — and load the ONNX graph + tokenizer directly, bypassing `gliner`'s torch import. (Adapt `_OnnxGlinerBackend.predict` in `anonymizer/gliner_recognizer.py` to this path.)
- [ ] Fallback: CPU-only torch (`uv pip install ... --index <pytorch-cpu>`), so no `nvidia-*`/`cuda-*` wheels. Larger, but no GPU stack.
- [ ] Confirm the resolved install has **no CUDA wheels**: `uv run python -c "import importlib.util as u; print('torch', u.find_spec('torch')); print('onnxruntime', u.find_spec('onnxruntime'))"`.

## 2. Prepare the model → `vendor/gliner-model/` (20–40 min)
- [ ] Fetch `urchade/gliner_multi-v2.1`, export to **ONNX**, and **int8-quantise** (per the gliner / optimum docs).
- [ ] Save the `from_pretrained` snapshot into `vendor/gliner-model/` (config json + `onnx/model.onnx` + tokenizer files).
- [ ] Note the on-disk size — this is what the bundle will carry (target: well under 1 GB).

## 3. Smoke-test the backend loads + predicts (5 min)
- [ ] ```
  uv run python -c "from anonymizer.gliner_recognizer import load_gliner_backend; b=load_gliner_backend({'model_path':'vendor/gliner-model','onnx':True}); print(b.predict('Ada Lovelace arbeitet bei DeepL Pro in Karlsruhe.', ['person','tool','location']))"
  ```
- [ ] Expect spans for a person, a tool (DeepL Pro), and a location, each with a score. If this raises, the model layout/path is wrong — fix before continuing.

## 4. Enable and run a functional check (15 min)
- [ ] Point the app at the model: set `ANONYMIZER_GLINER_MODEL=…\vendor\gliner-model` (the bundle launcher does this automatically from `gliner-model\`).
- [ ] Turn the Settings **AI detection** toggle **on** (or set `gliner.enabled: true` in `%LOCALAPPDATA%\Anonymizer\config.yaml`). Confirm the status line now reads `runtime: installed · model: … (NNN MB)`.
- [ ] Scan a **small** German test doc that contains: a prose name spaCy usually misses, an English tool name inside German text, and a project/tool term. Verify in the review screen:
  - [ ] GLiNER findings appear (source `gliner`), including the English tool in the German doc (the mixed-language win).
  - [ ] A high-confidence German noun-like tool/project name is **kept** (confidence-override working); ordinary German nouns like *Effizienz* are **not** flagged.
- [ ] **Hard-fail check:** rename `vendor/gliner-model` temporarily, keep GLiNER enabled, scan → expect a clear error pointing to the Settings toggle (not a silent degrade). Restore the folder.

## 5. DONE-WHEN measurement on the REAL workbook (30–60 min) — the real test
Run your reference workbook **twice**: once with GLiNER off, once on.
- [ ] **Recall ↑:** GLiNER recovers clearly-missed names/orgs (items absent from the old `_flagged.csv`). Use the "export flagged words" feature to diff.
- [ ] **Precision held:** total flagged **≤ ~445** (the current baseline). If it blows past that, raise `confidence_override` / per-entity thresholds, or lower the sensitivity slider.
- [ ] **Speed:** typical scan **< 5 min** with GLiNER on. If not, that's the trigger to implement the deferred soft cap (step 8).
- [ ] Record the three numbers (recall delta, FP count, scan time) in the run-file progress log.

## 6. Scan/apply parity (10 min) — must not regress
- [ ] Apply redactions on the test doc and confirm the output re-scan (`verify_output`) passes with **zero residual**. GLiNER inference must be deterministic (eval mode, fixed weights) — if parity ever fails, that determinism is the first thing to check.

## 7. (Optional) spaCy size downgrade lg→sm
- [ ] Switch `engine.SPACY_MODELS` to `de_core_news_sm` / `en_core_web_sm`, update the model-wheel URLs in `pyproject.toml`, `uv sync`.
- [ ] **Run `uv run pytest tests/test_precision.py tests/test_language.py`.** If the German-noun precision tests regress → **revert to `lg`** (documented default) and accept the size cost.

## 8. Build the offline bundle + finish the deferred work
- [ ] `./scripts/build_offline_bundle.ps1 -WithML` (after step 1's torch decision). Check final bundle size.
- [ ] Now that the model is measurable, implement the two DEFERRED items and validate against the real doc:
  - [ ] **Soft cap** — a content-keyed allow-set precomputed identically in scan + apply (a running counter breaks parity; see run-file). Log the notice when capped.
  - [ ] **Cell-level DESCRIPTION flag** — whole-cell summarize when GLiNER flags sensitive prose; if v2.1 zero-shot description quality is poor, escalate to GLiNER2 (DEFERRED note).

## 9. Flip the shipped default
- [ ] Once measurements pass, set `gliner.enabled: true` in `anonymizer/data/default_recognizers.yaml` so fresh installs get it on (`_resync_builtins` preserves any user's explicit choice).

---

## Rollback (if anything misbehaves)
- Turn the Settings toggle **off** (or `gliner.enabled: false`) → instantly back to spaCy + gazetteer, no code change.
- Or revert the GLiNER commits (`258cb35`..`039a15e`) → exact pre-GLiNER behaviour. spaCy downgrade is a `pyproject` revert.

## Quick reference
- Recognizer + backend: `anonymizer/gliner_recognizer.py`
- Precision-gate override / detect hook: `anonymizer/core.py` (`_rejected_by_precision`, `detect_unit`)
- Config block: `anonymizer/data/default_recognizers.yaml` (`gliner:`), schema v6
- Settings UI: `anonymizer/gui/settings_page.py` (`_gliner_section`)
- Bundle: `scripts/build_offline_bundle.ps1 -WithML`, `scripts/bundle_templates/launch.bat`
- Tests: `tests/test_gliner.py`, `tests/test_gui_render.py`
- Full spec + runbook: `docs/run_gliner-integration_2026-07-24.md`
