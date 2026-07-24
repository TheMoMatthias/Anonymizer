# Run-file — GLiNER second-pass ML detection

**Date:** 2026-07-24
**Status:** Phase A COMPLETE (checkpoint) → awaiting go for Phase B
**Grill:** 24 questions / 6 rounds (see decisions below). Research memo: `docs/research_offline-detection-models_2026-07-24.md`.

---

## PROGRESS LOG

### Phase A — core plumbing ✅ (2026-07-24)
Delivered (all tests green: **199 passed**, 12 new in `tests/test_gliner.py`):
- **`anonymizer/gliner_recognizer.py`** (NEW): `GlinerRecognizer` (Presidio `EntityRecognizer`), `GlinerBackend` protocol, lazy `load_gliner_backend` (hard-fail with actionable message), `resolve_model_path`, `_OnnxGlinerBackend`. **All ML deps isolated behind the lazy loader** — package + full suite run with no gliner/onnxruntime/torch installed.
- **`engine.py`**: `build_analyzer(config, *, gliner_backend=None)` registers the recognizer per language when `gliner.enabled`; injectable backend is the test seam.
- **`core.py`**: confidence-override in `_rejected_by_precision` (source+score+trust_override; GLiNER-only, default 1.0 keeps existing callers byte-identical); `detect_unit` requests `PROPAGATING_TOPICAL_TYPES` only when GLiNER on; passes source/score into the gate.
- **`config.py` + `default_recognizers.yaml`**: `gliner` block, `config_schema_version` 5→6, `_resync_builtins` re-syncs code-owned gliner fields while preserving the user's `enabled` toggle.

**Checkpoint verification:** person/org/location detected via GLiNER (fake backend); open topical types (tool/project) emitted; confidence-override keeps high-conf hits + filters low-conf; deterministic output (parity property); min_chars/min_score gates; hard-fail on missing runtime; non-GLiNER sources unaffected. Existing 187 tests unchanged (GLiNER off by default).

**Two conscious sequencing deviations from the spec (flagged, not silent):**
1. **Shipped default `enabled: false`** (spec said "default-on"). Default-on applies to the *bundle* (Phase C) once the model is vendored; enabling now — with no model on disk — would hard-fail every scan and break the app/tests. Flip to `true` in Phase C alongside the model.
2. **spaCy `lg→sm` downgrade deferred** to the packaging phase. It requires `de_core_news_sm`/`en_core_web_sm` installed (needs network, absent here) and the DEFAULT "revert sm→lg if POS degrades the German-noun tests" can't be evaluated offline. Flipping `SPACY_MODELS` without `sm` present would break every test. It's a size optimization orthogonal to the GLiNER plumbing — do it on the connected packaging machine.

**Not validatable in this environment (inherent, not a gap):** real ONNX model inference — the model is fetched once on a connected machine and vendored into the air-gapped bundle (Phase C). Phase A proves the integration via the deterministic fake backend, which is exactly the seam the design put there.

---

---

## GOAL
Add an offline, quantised **GLiNER zero-shot NER** model as a **second-pass recognizer** that recovers currently-missed sensitive items (prose names, orgs, internal tools, projects, and cell-level sensitive descriptions) — while keeping spaCy as the POS backbone for the precision filters, and without exceeding today's false-positive baseline (~445) or a 5-minute typical scan.

## CONTRACT
- **Input:** same documents as today (xlsx first; all formats via the format-agnostic `core.detect_unit` hook).
- **Output:** same `ScanResult`/`Finding` shape; GLiNER hits carry `source="gliner"` and map onto existing entity types (PERSON/ORG/LOCATION + TOPICAL types in `taxonomy.py:26`).
- **Side effects:** new ONNX model file in the bundle; new `gliner` config block; spaCy model downgraded lg→sm.
- **Invariant (hard):** scan/apply parity — deterministic inference (eval mode, fixed weights, no sampling) + shared memo cache both passes; `verify_output` must still pass byte-for-byte.

## DECISIONS (from grill)
| # | Decision | Choice |
|---|----------|--------|
| Runtime | ML runtime | **ONNX Runtime + int8-quantised** model (drop torch; keep bundle <1GB) |
| Model | GLiNER variant | **gliner_multi-v2.1** (general multilingual, Apache-2.0, flexible runtime labels) |
| spaCy | Fate of spaCy | **Keep for POS/stopwords, downgrade `de_core_news_lg`→`de_core_news_sm` and `en_core_web_md`→`en_core_web_sm`** |
| Bundling | Distribution | **Bundle model, default-on**, vendored like Tesseract in `build_offline_bundle.ps1` |
| Hook | Integration point | **Custom Presidio `EntityRecognizer`** registered at `engine.py:110` → flows through existing overlap/precision/propagation |
| ML scope | Which cells | **Pre-filter gate**: non-empty, has letters, len≥N, not already resolved by header/gazetteer; reuse memo cache |
| Formats | Coverage | **All formats** via detect_unit; tune thresholds on xlsx first |
| Scale cap | Big-doc safety | **Batch + gate + soft cap with visible notice** (deterministic ordering; no silent truncation) |
| Labels | Runtime labels | **Everything**: person, organization, location + tool, project, department, division, licensee + sensitive-description |
| Filters | Precision gate on GLiNER hits | **Filter all uniformly, BUT confidence-override**: a high-confidence GLiNER label bypasses the German-noun/POS filter (`core.py:189`); low-confidence noun-like hits still filtered |
| Description | DESCRIPTION handling | **Cell-level flag** (classify cell contains sensitive prose → whole-cell summarize/redact via `xlsx_handler.py:455` path), not fragile prose spans |
| Language | Mixed-language fix | **GLiNER language-agnostic** (one pass over DE+EN); `_narrow_language` (`pipeline.py:117`) kept ONLY to pick spaCy POS model |
| Threshold | Confidence control | **Tie to existing sensitivity slider** (PageState.sensitivity); higher sensitivity = lower cutoff |
| Load-fail | Robustness | **Hard-fail + block scan with clear error pointing to the Settings disable toggle** (escape hatch → resumes on spaCy+gazetteer) |
| Config | Schema | **New top-level `gliner` block**, `config_schema_version` 5→6, pushed via `_resync_builtins` (`config.py:158`) preserving user edits |
| Settings UI | Surface | **Enable/disable toggle + 'model loaded: yes/no (size, version)' status line** in `settings_page.py`; threshold = sensitivity slider |
| Success | Ship bar | Recall↑ (recovers missed items) AND FP ≤ ~445 baseline AND typical scan <5min AND all tests green AND parity byte-for-byte |
| Phasing | Rollout | **3 phases with checkpoints** |
| Durability | Obsolescence | **Pin gliner/onnxruntime versions + record model version; user-swappable `model_path`** for future model drop-in |

## LAYERS TOUCHED
- `pyproject.toml` — add pinned `gliner`, `onnxruntime`; swap spaCy model wheels lg→sm, md→sm.
- `anonymizer/engine.py` — register GLiNER `EntityRecognizer` (registry ~`:110`); `SPACY_MODELS` model-name change.
- `anonymizer/gliner_recognizer.py` (NEW) — ONNX GLiNER wrapper: load, deterministic infer, label→entity map, confidence, pre-filter gate, soft cap.
- `anonymizer/core.py` — confidence-override in `_rejected_by_precision` (`:204`); `source="gliner"` handling.
- `anonymizer/pipeline.py` — language-agnostic GLiNER vs per-lang spaCy split (`_narrow_language` `:117`).
- `anonymizer/formats/xlsx_handler.py` — pre-filter gate + cell-level description flag on the override path (`:455`); memo/parity (`:496`/`:554`).
- `anonymizer/config.py` + `data/default_recognizers.yaml` — `gliner` block, schema v6, resync.
- `anonymizer/gui/settings_page.py` — toggle + status line.
- `scripts/build_offline_bundle.ps1` — vendor ONNX model; ensure onnxruntime installs offline.
- `tests/` — new `test_gliner.py` (determinism, parity, gate, confidence-override, load-fail); update fixtures.

## PLAN (3 phases, checkpoint between each)
- **Phase A — core plumbing:** ONNX model load + deterministic infer wrapper; Presidio recognizer registered; spaCy lg→sm/md→sm; `gliner` config block + schema v6; parity + determinism tests. Checkpoint: person/org/location detected via GLiNER, parity green, scan runs.
- **Phase B — topical + filters:** topical labels (tool/project/dept/division/licensee) + cell-level description flag; confidence-override precision gate; pre-filter gate + soft cap; language-agnostic pass. Checkpoint: topical hits + description cells work; FP not regressed on fixtures.
- **Phase C — packaging + UI + tune:** vendor model in offline bundle; Settings toggle+status; measure recall/FP/time on the real reference workbook; tune threshold mapping. Checkpoint: DONE-WHEN met.

## TESTS (success criterion)
- Determinism: same input → identical findings across repeated inference.
- Parity: scan findings == apply findings; `verify_output` byte-for-byte.
- Gate: resolved/empty/numeric/short cells skipped; soft cap logs when exceeded.
- Confidence-override: high-conf German tool name survives the noun filter; low-conf noun-like hit filtered.
- Load-fail: enabled+unloadable → scan blocked with actionable error; disabling in Settings unblocks.
- Metric harness on real workbook: recall gain, FP ≤ 445, scan < 5 min.

## AUTONOMY CONTRACT
**DONE-WHEN** — all of: (1) new `test_gliner.py` + full suite green; (2) on the real reference workbook, GLiNER recovers ≥1 clearly-missed name/org class of items with total FP ≤ ~445 baseline; (3) typical scan < 5 min with GLiNER on; (4) scan/apply parity byte-for-byte (`verify_output` passes); (5) offline bundle builds and loads the model with no internet.

**DEFAULTS** (pre-authorized mid-run forks):
- Pre-filter `min_chars` default = 3; `cell_cap` soft-limit default = 5000 ML-eligible cells (log when exceeded).
- Confidence-override threshold default = 0.85; sensitivity slider maps [low→0.6 … high→0.4] base cutoff.
- If int8 quantisation costs >3 pts recall on fixtures, fall back to fp32-ONNX for the bundled model (still no torch).
- Label→entity map: organization→ORG, tool→TOOL, project→PROJECT, department→DEPARTMENT, division→DIVISION, licensee→LICENSEE, sensitive-description→DESCRIPTION.
- Keep `de_core_news_sm`; if `sm` POS visibly degrades the German-noun filter on existing precision tests, revert that model to `lg` (size cost accepted) and note it.

**DEFERRED** (postponed + resurface trigger):
- GLiNER2 schema/multi-task path — resurface if v2.1 cell-level description classification proves unreliable in Phase B.
- Embedding fuzzy-gazetteer — resurface only if post-GLiNER misses are dominated by typo/variant forms of known terms.
- docx/pptx per-cell / PDF-block ML tuning — resurface after xlsx metric target is met.

**ROLLBACK** — GLiNER is a registered recognizer behind a config toggle; disabling `gliner.enabled` (or reverting the commit) restores exact current behaviour. spaCy model downgrade is a pyproject revert. Blast radius: detection layer only; parity invariant protects written output.

## OUT OF SCOPE
- No local LLM (unchanged decision). No change to the reversible-mapping/re-identify flow. No new redaction modes. No cross-document learned gazetteer. No GPU path.
