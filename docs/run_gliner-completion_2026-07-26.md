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
  Awaiting sign-off before Stage 1.
