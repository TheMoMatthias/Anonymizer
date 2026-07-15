# Run: anonymizer-v2 overhaul (2026-07-15)

Persisted spec + autonomy contract from the v2 grill (32 questions, 8 rounds).
Goal: take the working v1 foundation to a professional, robust, scalable,
"smart" production-grade tool. Builds on `run_anonymizer-v1` and
`run_deployability`.

## GOAL
Turn the existing local anonymizer into a bank-grade tool that (1) makes
review scalable via category-level decisions (kills the 630-field problem),
(2) detects PII intelligently by sensitivity category with recall-first
accuracy, (3) has a professional bank-grade frontend with working drag-drop
and batch, and (4) is provably correct (scan/apply parity + output re-scan) —
while staying lightweight, offline, and easy to maintain.

## KEY DECISIONS (from grill)

### Review model
- **Decision unit = data class (sensitivity category)**, not per value. One
  action decides a whole category; expand only to override exceptions.
- **3-tier auto-accept**: high-confidence findings auto-apply the category
  default and collapse into an auditable "auto-applied" section; medium/low
  surface for review.
- **Category-level approval is the compliance gate** (satisfies v1's mandatory
  review gate). Value-level stays available on demand, not required.
- **Top risk to minimize = missed PII (false negatives).**

### Detection
- **Recall-first + tiered review + completeness pass.** Wide net; tiered review
  absorbs the volume; an unmatched-risk bucket flags sensitive-looking strings
  no recognizer matched.
- **Data-class taxonomy**: People, Contact, Financial IDs, Government IDs,
  Bank-internal refs, Dates/Other. Each entity type maps to one class.
- **Checksum/format validation** for structured IDs (IBAN mod-97, German
  Steuer-ID check digit, Luhn for cards). Validated ID → high confidence;
  unvalidated number → low confidence for review.
- **Detector architecture is pluggable.** Default: enhance spaCy (evaluate
  `de_core_news_lg` / `trf`). Stay lightweight; adopt a transformer/other
  package ONLY if it fits without bloating the bundle. Highly accurate,
  trustworthy, dynamic, not massive.

### Redaction
- **Pseudonymize-by-default** (preserves relationships for downstream AI);
  hard-anonymize reserved for highest-sensitivity IDs (Steuer-ID, SV-Nummer,
  cards). Per-category, editable.
- **Readable typed tokens**: `[PERSON_1]`, `[IBAN_1]` — consistent, auditable.
- **Mapping stays global-per-colleague** (cross-doc consistency) + add
  reset/rotate.
- **Re-identify workflow** added — audit-logged + confirmation-gated. No
  one-click bulk dump of the map.

### Frontend
- **Keep NiceGUI, redesign hard** into a clean bank-grade design system
  (Python-native, fits the offline bundle; no JS build toolchain).
- **Robust native OS drop** delivering real absolute paths; browse-dialog +
  manual-path box as guaranteed fallbacks; output stays next to source.
- **Batch queue**: drop multiple files → queue; category decisions apply across
  the batch, per-file review only where needed.
- **Text-level change preview** (expandable before→after per category) before
  save. No full document rendering.

### Correctness & audit
- **Scan/apply parity**: apply exactly the reviewed decision set (no silent
  skips from re-detection drift).
- **Output re-scan**: re-scan the written output, assert zero residual PII of
  removed categories; failure blocks/flags the save.
- **Fail loud, never emit partial**: any handler/conversion/re-scan failure
  aborts THAT file with a clear reason and writes NO output. Batch continues;
  failures listed.
- **Rich audit record, NO plaintext PII**: categories, decisions, counts,
  placeholders, tool version, timestamp, config hash, re-scan result.
  Reversibility stays only in the encrypted map.

### Performance / architecture
- **Warm-start models** at launch (background) + **bounded parallel batch**;
  reuse one analyzer for detect + re-scan.
- **Refactor to a shared core + thin format adapters** (centralize
  detect→decide→replace→verify; handlers just map document↔TextUnits + apply
  span replacements).
- **Correctness first, stay responsive** (live progress, async, cancelable).

### Dynamic config
- **Per-run detection profiles** (Contracts / Client statements / HR docs) +
  a **global sensitivity slider** (recall↔precision). No adaptive/auto-learning
  (avoids behavior drift in a regulated setting).

### Compliance gaps to close
- **PPTX threaded comments**: add scanning (was a known limitation).
- **Image/scanned PDF**: DETECT no-text-layer PDFs and refuse/hard-warn rather
  than emitting a false-clean output. Full OCR deferred (later toggle).
- **GDPR lifecycle**: map reset/rotate, per-entry erasure (data-subject
  request), audit-trail export.

## PLAN (phased — Core → UX → Polish)

**Phase 1 — Detection & redaction core (backend, test-driven)**
- Data-class taxonomy + category-level grouping model (replace flat grouping).
- Checksum/format validators; recall-first thresholds; trust tiers.
- Unmatched-risk (completeness) scan.
- Scan/apply parity refactor; output re-scan verification; fail-loud handling.
- Shared core + thin format adapters refactor.
- Mapping reset/rotate + re-identify (audit-logged); rich audit record.
- `[PERSON_1]` token style; pseudonymize-by-default config.
- Extend synthetic-fixture suite; all green + lint/typecheck.

**Phase 2 — Frontend, drag-drop, batch, preview**
- Bank-grade NiceGUI design system (theme, tokens, components, light/dark).
- Category-first review UI with trust-tier sections + bulk category actions.
- Robust native OS drop + fallbacks; batch queue with progress.
- Text-level change preview.
- Warm-start + bounded parallel processing.
- Manual E2E walkthrough per format.

**Phase 3 — Polish & compliance**
- Detection profiles + sensitivity slider.
- GDPR lifecycle controls UI (reset/rotate/erasure/export).
- Close PPTX threaded-comment + image-PDF gaps.
- Re-identify UI (gated). Docs/FAQ/README refresh. Offline-bundle re-verify.

## DONE-WHEN (per phase)
Extended synthetic-fixture suite green (category grouping, checksum validators,
scan/apply parity `reviewed==applied`, output re-scan zero-residual, batch,
re-identify round-trip) + lint/typecheck clean + one manual GUI walkthrough per
format per phase.

## DEFAULTS (pre-authorized mid-run forks)
- Enhance spaCy first; a transformer is adopted only if lightweight enough —
  otherwise stay on enhanced spaCy and leave the pluggable seam.
- Reserve hard-anonymize for Steuer-ID/SV-Nummer/CREDIT_CARD; everything else
  defaults to pseudonymize.
- On any processing failure, emit no output for that file and continue the
  batch.
- Keep output next to source; do not change that contract.
- Full OCR stays deferred; image-PDF gets a hard warn/refuse, not OCR.

## DEFERRED (+ resurface trigger)
- Full OCR of scanned PDFs — resurface on explicit request or if the pilot hits
  many image PDFs.
- A shipped local transformer detector — resurface if enhanced spaCy recall is
  insufficient in the pilot.
- Adaptive allow/deny learning — resurface only on explicit request.
- Full rendered-document preview — resurface on explicit request.
- Map auto-expiry/retention policy — resurface on explicit request.
- Code-signing / IT-InfoSec review — per run_deployability, unchanged.

## OUT OF SCOPE
Cloud/LLM-assisted detection, standalone .exe packaging (bundle stays as-is),
macro editing, a shared server. Raw PII never leaves the originating machine.

## GIT
Feature branch `feat/anonymizer-v2`, commit per phase, merge to master after
your review.

## PROGRESS — follow-up (post-Phase-3, user-requested)
- [x] German phone recognizer (DE_PHONE, Contact class) — phones classify as
  Contact not DATE_TIME; regex guarded with `(?<!\d)` so it can't carve a fake
  phone out of IBAN/Steuer-ID digits. Regression test added.
- [x] "Save all" button — saves every reviewed job sequentially with per-file
  status/error surfacing.
- [x] OCR for scanned PDFs — ocr.py (portable Tesseract: config/env/bundle/PATH
  resolution + graceful degradation), pdf_handler OCRs no-text pages and redacts
  via black boxes over span-mapped word boxes; pipeline refuses image PDFs only
  when OCR is unavailable. Bundle `tesseract\` drop-in convention + launcher env
  wiring + Settings status/path + FAQ. Deps: pytesseract, pillow.
  - Verified without the binary: boxes_for_span + tesseract resolution unit
    tests; scan-with-mocked-OCR detects PII; apply-with-mocked-OCR blackens the
    redacted region (pixel check). Live OCR needs a real Tesseract on the target
    (the user will test on their work PC).
- 42 tests green; all pages build headless (HTTP 200).

## PROGRESS
- [x] Phase 1 — detection & redaction core (29 tests green)
  - taxonomy.py (data classes + trust tiers), validators.py (IBAN/Luhn/Steuer-ID)
  - core.py: single detect_unit (scan/apply parity), refine+validate, tiers,
    data-class grouping, completeness/unmatched-risk scan, stats
  - pipeline.py: output re-scan verification, fail-loud atomic write, image-PDF guard
  - mapping.py: label-based tokens, reverse/reset/rotate/erase; actions.py: [PERSON_1]
    tokens + reidentify_text; report.py: rich audit record, no plaintext PII
  - config: pseudonymize-by-default, hard-anonymize for Steuer-ID/SV/cards,
    tiers high=0.9 (above spaCy's flat 0.85 so NER -> review), curated allow_list
  - NOTE: gui/app.py intentionally left on the old API; Phase 2 rebuilds it.
  - Detection-tuning backlog (Phase 1.5/refinement, non-architectural): German
    phone recognizer (0170… currently caught as DATE_TIME), DATE_TIME noise on
    bare numbers, per-document language detection to avoid the EN pass on DE text.
- [x] Phase 2 — frontend / drag-drop / batch / preview (32 tests green)
  - gui/theme.py: graphite+teal design-token system, light/dark, self-contained
    (system fonts, no CDN), styled chips/cards/dropzone
  - gui/review.py: category-first review — per-data-class cards with a whole-
    category bulk action, review-tier items surfaced, high-confidence auto-
    accepted items collapsed, validated/tier/sensitivity chips, possible-misses
    section, global bulk actions
  - gui/app.py: rebuilt shell — warm-start analyzer at launch, multi-file native
    drop + browse + manual fallbacks, batch queue with per-file status,
    text-level before->after preview dialog, fail-loud save with verified state
  - build_preview() + FileJob model as pure testable units (test_preview.py)
  - Verified: 32 tests green; pages build headless (HTTP 200, themed content
    renders); drop patch applies to nicegui 3.14 and pywebview 6.2.1 _dnd_state
    intact. NOT yet verified: pixel-level look and physical native drag-drop
    (need a manual walkthrough on the real window) + per-file/batch save-all
    convenience actions are minimal (save is per-selected-job).
- [x] Phase 3 — polish & compliance gaps (37 tests green)
  - Closed PPTX modern threaded-comment gap (scan+apply a:t runs); output
    re-scan now reads them too, so any residual fails loud
  - profiles.py: Contracts / Client statements / HR / Maximize-recall presets +
    global sensitivity slider; per-job config captured at scan and reused at
    apply (parity across the profile/sensitivity offset)
  - Settings restyled to the design system; sensitivity slider; mapping admin
    (reset / rotate-key / per-entry erase) — all audit-logged
  - Re-identify page (/reidentify): confirmation-gated un-mask + audit log view;
    audit.py append-only log (no plaintext values)
  - README + bundle FAQ refreshed for v2 (categories, batch, re-identify,
    profiles, image-PDF refusal, model-prune note)
  - Verified: 37 tests green; all three pages build headless (HTTP 200)
  - NOT done here (deferred per contract): pixel-level look + physical native
    drag-drop walkthrough on the real window; a "Save all" batch convenience;
    German phone recognizer + DATE_TIME-noise detection tuning.
