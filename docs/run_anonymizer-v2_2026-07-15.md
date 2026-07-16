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

## PROGRESS — pilot feedback fixes (post-Phase-3)
- [x] Install path fixed: spaCy models are direct-URL deps (uv sync installs +
  keeps them; the old `spacy download` needed pip the venv lacks). Root
  launchers Anonymizer.bat (sync+patch+launch) + Install.bat.
- [x] UI layout: neutralized NiceGUI's default content wrapper, no-wrap two-col,
  fixed a theme %-format bug.
- [x] Over-flagging root cause fixed: was running BOTH de+en NER and merging;
  English NER on German text flagged ordinary words as people. Now single
  detected language per document (language.py, deterministic -> parity),
  built-in patterns cross-registered per language, German model md->lg,
  Language control (Auto/German/English) that asks when unsure. Config
  auto-migrates new defaults into an existing user config.
  Verified: German paragraph 7+ garbage -> 4 clean findings.
- [x] Drag-drop hardened + self-diagnosing: native drop event was firing outside
  the client context (UI updates never reached the window). Now buffers paths +
  a ui.timer drains them in-context; startup banner when the drop patch isn't
  applied; on-screen notice when a drop yields no path. (Native path-capture
  itself still only verifiable on a real window.)

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

## Phase 4 — Deep audit + hardening (2026-07-16, /audit-loop-codebase)

5 read-only audit agents (recall, edge-cases, security, code-quality, UI) +
own independent read of the full critical surface. Posture (user-signed):
auto-fix safe/caution; PAUSE on critical-tier; detection-weighted; build a
static visual-preview artifact.

VERDICT: precision disaster (643-findings / German-words-as-names) is GENUINELY
FIXED. Confirmed-good & to KEEP: no runtime network (air-gap holds), scan/apply
parity by construction, PII-free report/audit trail, validators (IBAN mod-97 /
Luhn / Steuer-ID checksum+structural) brute-force-verified clean. NOT yet
comprehensive: real recall gaps + false-clean leaks + 2 default-config bugs.

User authorized ALL FOUR critical clusters. Progress:

- [ ] Cluster 1 — detection-correctness bugs
  - [ ] #1 detect_unit dedups exact (start,end) only -> overlapping spans (e.g.
        PHONE_NUMBER + DE_PHONE) survive -> apply splices -> DOCX/XLSX/PPTX text
        corruption + dropped redaction. Fix: interval-schedule overlap resolve
        (keep highest score, drop overlappers) across per-lang + deny-list set.
  - [ ] #2 language.detect_dominant: `de + umlauts*2` lets umlaut CHARS alone
        flip to ('de', True) on English text -> German NER on English (reverse
        of the original bug), and confident=True so GUI ask-fallback never fires.
        Fix: count umlaut-bearing WORDS capped, require min word-signal.
- [ ] Cluster 2 — recall recognizers (new default to REVIEW tier, not auto-redact)
  - [ ] #6 add BIC_CODE, DE_STREET, DE_PLZ_CITY, DE_DATE (German NER emits NO
        DATE), cross-register PhoneRecognizer to `de` (intl phones), DE_KUNDENNUMMER
        (bare numeric), broaden Konto/Depot context lists.
  - [ ] #7 completeness_scan textual backstop (BIC/alpha + capitalized-bigram);
        honest UI relabel. #12 add SV-Nummer checksum. #9(recall) demote invalid
        IBAN/card to ~0.45 review instead of zeroing.
- [ ] Cluster 3 — fail-loud coverage (touches all format handlers + verifier)
  - [ ] #3 scrub OOXML docProps + PDF /Author/XMP metadata on apply
  - [ ] #4 xlsx scan numeric cells (coerce str) not just data_type=="s"
  - [ ] #10 extract docx/pptx text boxes / drawing shapes (w:txbxContent)
  - [ ] #5 verify_output: recognizer-INDEPENDENT literal-value residual scan
        (decompress output zip / PDF streams, assert removed literals absent,
        match by value across all entity types)
  - [ ] #11 docx detect-text derived from same run concat used for replacement
- [ ] Cluster 4 — mapping/crypto integrity (MIGRATION SPEC -> user sign-off FIRST)
  - [ ] #8 monotonic never-reused placeholder counter (survives erase/reset)
  - [ ] #9 key mapping rows on canonical label, not raw entity_type (PHONE alias)
  - [ ] #13 atomic save (mkstemp+os.replace); rotate_key saves under new key
        BEFORE keyring set; hold store open across verify, save after os.replace
  - [ ] #14 concurrency guard (serialize DB access / disable save while saving)
- [ ] SAFE auto-fixes (pre-authorized, non-critical): UI AA chip contrast,
      focus-visible + keyboard roles, per-value segmented toggle, saving-state,
      header/body alignment, layout wrap + scroll model, stat hierarchy, danger-
      zone styling, tooltips on truncated values; dead detect_all, version drift
      (0.1.0 vs 0.2.0), shared DEFAULT_LANGUAGES const, TOKEN_RE digit support,
      invalid-score hard-skip, config atomic write, OCR config-path caching,
      audit-write-failure surfaced.
- [ ] Visual-preview artifact (dark graphite+teal; requested).
- DONE-WHEN: all authorized clusters implemented + tests green + headless build
  HTTP 200; artifact delivered. Crypto cluster gated on migration sign-off.

### STATUS 2026-07-16 (end of session) — 66 tests green

- [x] Cluster 1 DONE: _resolve_overlaps (longer-span-then-score, NER deprioritized
      on ties) in core.detect_unit; language.detect_dominant umlaut reweight
      (capped umlaut-WORD count < _MIN_SIGNAL). Tests: test_core (overlap x3),
      test_language (umlaut x2).
- [x] Cluster 2 DONE: BIC_CODE (context-gated, NOT auto-promoted — validator
      promotion caused DOKUMENT/Anfragen false BICs, reverted), DE_ADDRESS
      (street + PLZ-city), DATE_TIME German-date recognizer (German NER emits no
      DATE), DE_KUNDENNUMMER, PhoneRecognizer cross-registered to de, broadened
      Konto/Depot context; validators.bic_valid (ISO-3166 gate) backs the
      completeness scan; invalid-checksum DEMOTE (0.4) not zero. Tests: test_recall.
- [x] Cluster 3 (safety trio) DONE: #3 _scrub_metadata (OOXML docProps + PDF),
      #4 xlsx numeric-cell scan (_cell_scan_text), #5 _literal_residual
      recognizer-independent backstop wired into apply_document verify. Tests:
      test_fail_loud.
  - [x] #10 text boxes + #11 run coverage DONE: _textbox_paragraphs walks
        w:txbxContent in body + every header/footer; _para_run_elements adds
        hyperlink runs (p.runs skips them) and does NOT descend into text boxes
        (would double-redact); detection and replacement now build text from the
        SAME run list via paragraph_runs(), so offsets match by construction.
        Tests: test_fail_loud (textbox, hyperlink) using an IBAN for determinism.
- [x] Cluster 4 (core) DONE: #8 monotonic max+1 placeholder counter, #13 atomic
      save (mkstemp+os.replace) + rotate saves-under-new-key-first + prev-key
      recovery fallback, M2 mapping saved only after os.replace. conftest now
      isolates keyring (in-memory). Tests: test_mapping.
  - [x] #9 token-alias keying DONE: rows keyed on the canonical label (so
        PHONE_NUMBER + DE_PHONE share one token for one value), with a legacy
        raw-entity_type fallback so existing mappings keep their tokens. No
        schema migration needed.
  - [x] #14 concurrency — addressed via UI saving-state (Save hidden during save).
- [x] SAFE UI: AA-safe chips (color-mix fg), :focus-visible ring, saving-state
      branch, header/body px-6 alignment, segmented action toggle replacing the
      per-row dropdowns, tooltips on truncated value/context/score, hero "to
      review" stat, responsive stack breakpoint (az-main/az-rail), honest
      "Set all N" bulk label, scan-progress copy, font-weight 650->600,
      dead backdrop-filter removed.
- [x] HYGIENE: version drift 0.1.0->0.2.0, shared engine.DEFAULT_LANGUAGES,
      dead detect_all removed, TOKEN_RE accepts digits in labels, atomic
      save_config.
- [x] Visual-preview artifact delivered (dark/light graphite+teal review screen).
- VERIFIED: 69 tests green; all routes HTTP 200 headless; review screen rendered
  with a synthetic ScanResult (6 toggles + hero stat present).
- REMAINING (small, non-critical): settings "danger zone" styling; global
  "apply to everything" still full-re-renders (loses scroll/expansion); pptx
  grouped-shape / chart text; nested-scroll model (.az-scroll 58vh vs page);
  audit.log fails open; allow/deny list stored as plaintext at rest.

## Phase 5 — Name-recall research + Tier 1 (2026-07-16)

3 research agents (models / Presidio techniques / codebase). User authorized ALL
four workstreams; DISTRIBUTION ANSWER: **internal only, never distributed** (so
copyleft obligations don't trigger — but still prefer CC0/CC-BY, and NEVER use
`names-dataset`: it is derived from the Facebook 533M breach = GDPR problem).

MEASURED GROUND TRUTH (adjudicated myself; the two agents contradicted — probe:
scratchpad/adjudicate.py). de_core_news_lg on "Müller":
  - "Herr Müller hat das Konto eröffnet."  -> PER  (CAUGHT)
  - "Sehr geehrter Herr Müller,"           -> MISS (the standard salutation!)
  - bare / "Name: Müller" / "Kunde: Müller"-> MISS
  - "Die Unterlagen wurden von Müller geprüft." -> MISS
  - "Björn Müller wohnt in Köln."          -> PER  (first name rescues it)
  - "Herr Öztürk ..."                      -> PER  (foreign surname = easy)
=> Failure is COMMON-NOUN COLLISION, not foreignness. Top-20 German surnames:
   40% recall; foreign surnames 94%. 19 of the top 20 are ordinary German words.
=> de_core_news_lg's published 84.9 F1 is WikiNER in-domain; the only rigorous
   OOD German study (NER4all) puts PER recall at 0.76 -> ~1 name in 4 leaks.

- [x] TIER 1 DONE (74 tests green)
  - [x] MISC LEAK (live bug): spaCy tags "Frau Bauer" as MISC; Presidio's
        MODEL_TO_PRESIDIO_ENTITY_MAPPING has NO MISC key -> span silently
        DISCARDED -> name leaked. Fixed: engine._ENTITY_MAPPING maps MISC ->
        NER_MISC; new taxonomy DataClass OTHER_ENTITIES ("Other named
        entities", medium, order 6; DATES_OTHER->7, UNMATCHED->8); TOKEN_LABELS
        NER_MISC->ENTITY; entities.NER_MISC in YAML. Surfaces for review, never
        auto-accepted (not confidently a person).
  - [x] IGNORECASE (live bug I shipped): PatternRecognizer defaults to
        regex.I|M|S, so [A-Z] BIC pattern matched "geehrter"/"ausgefuehrt".
        Invisible at default but the sensitivity slider SUBTRACTS from the
        threshold -> "Maximize recall" (0.15) => eff. 0.35 < 0.4 base => every
        8-letter German word became a BIC. Fixed: `case_sensitive: true` per
        recognizer in YAML -> engine passes global_regex_flags=M|S (no I). Set
        on BIC_CODE, DE_ADDRESS, DE_SV_NUMMER, BANK_INTERNAL_REF.
  - [x] Anchored-name recognizer (engine._ANCHORED_NAME_PATTERNS): honorific
        (Herr|Frau|Hr.|Fr.|Dr.|Prof.) + labelled fields (Name|Kunde|Kontoinhaber
        |Sachbearbeiter|...). MUST be code not YAML, and MUST use variable-width
        LOOKBEHIND — Presidio returns the FULL match span, not group(1). Emits
        PERSON @0.75 (below the 0.9 auto-accept bar => stays under human eyes).
  - [x] Honorific trim in core.detect_unit: spaCy returns "Herr Müller" as the
        span; trimming to "Müller" makes it ONE token across the document
        (otherwise "Herr Müller" and bare "Müller" = two pseudonyms) AND gives
        propagation the right seed.
  - [x] Document-wide propagation: pipeline._with_propagation runs pass 1 over
        all units, collects PERSON values (honorific-stripped, len>=4, plus the
        surname of multi-token names), returns cfg+{"propagate": sorted set};
        core.detect_unit matches those literals with (?<!\w)...(?!\w) @0.85.
        Called from BOTH scan_document and apply_document from the same units +
        analyzer => parity + determinism by construction. Pass 1 runs on the
        config WITHOUT `propagate` so it cannot feed on itself.
  - [x] Dead context words removed: Presidio matches a context word as a
        SUBSTRING of a token lemma, so any entry with a space ("konto nr",
        "bank identifier") could NEVER match. Short stems do all the work
        ("konto" already matches kontonummer/kontoinhaber). German compounding
        is therefore ALREADY solved — no decompounder needed.

### MEASURED RESULT (2026-07-16) — `uv run python scripts/measure_recall.py`

    structured identifiers (IBAN/Steuer-ID/email/phone/address/PLZ/BIC/DOB) 100%  n=8
    full realistic letter, ALL strata                                      100%  n=190
    isolated cold read, overall                                             86%  n=228
      german_common_noun / salutation .......... 100%   (anchor works)
      german_common_noun / labelled_field ...... 100%   (anchor works)
      german_common_noun / signature ........... 100%
      german_common_noun / prose_full_name ...... 90%
      german_common_noun / prose_oblique ......... 25%  <-- LEAKS
      german_common_noun / bare_cell ............. 35%  <-- LEAKS (xlsx: now fixed)
      german_rare / * .......................... ~100%
      foreign / * ............................... 100%

READ: in a REAL letter the anchors seed the name and propagation catches every
occurrence -> 100%. What leaks is a common-noun surname appearing ONLY bare or
oblique with NO anchor anywhere in the document.

- [x] RECALL HARNESS DONE — anonymizer/evaluation.py + scripts/measure_recall.py.
      Isolated (cold read) + full-letter (anchors+propagation) + structured.
      Per-stratum, never one aggregate (an aggregate hides the only case that
      matters). Reported as an UPPER BOUND. It immediately earned its keep:
      caught that the anchor regex used [A-ZÄÖÜ][a-zäöüß] and silently missed
      "Yılmaz" (Turkish dotless ı) -> now \p{Lu}\p{L}+ (foreign salutation
      90%->100%).
- [x] NAME-COLUMN OVERRIDE DONE (xlsx_handler): a column headed Name/Kunde/
      Inhaber/... marks its cells PERSON @0.8 (review tier), yielding to any
      finding that already claims the whole cell. NB Presidio's context boost
      does NOT help here — it only lifts PATTERN recognizers, spaCy NER gets
      nothing from it, which is why passing the header as context still missed.
- [~] SURNAME GAZETTEER — **DECIDED AGAINST on the harness evidence** (user had
      approved it; overruled by measurement, flag if you disagree). Reasons:
      (1) full-letter recall is ALREADY 100%, so marginal gain is small;
      (2) German capitalises nouns, so any list aggressive enough to catch a
      bare "Müller" equally flags "Berg"(mountain)/"Koch"(cook) — a capitalised
      bare noun is ambiguous even to a human. That shotgun IS the 643-findings
      over-flagging. The precise fix for the real bare-surname case (a
      spreadsheet name column) is the header override above, now shipped.
      Revisit ONLY if the harness shows a real-document stratum it would fix.
- [ ] ONNX NER UNION — NOT STARTED. Now judgeable: run measure_recall.py before
      and after; adopt only if the isolated common_noun/prose_oblique + bare_cell
      strata actually improve. Plan unchanged (see below).
- KNOWN PRECISION COST of the MISC fix: bare capitalised German nouns can surface
      (measured: bare "Sparen" -> PERSON 0.85 from spaCy itself, pre-existing).
      Review-tier, never auto-redacted. Grow allow_list from real pilot docs.

- [ ] REMAINING (in this order)
  - [x] RECALL HARNESS: synthetic injection at the FORMAT layer into realistic
        German bank docs; per-stratum recall (German-common-noun surnames vs
        foreign; prose vs table vs form field); report as an UPPER BOUND.
        ~280 entities / ~30 docs for ±5% CI (clustering DEFF≈2.8). Do NOT use
        Faker de_DE names (mis-ranks the failure modes given the 40/94 split).
        Also cheap+now: reviewer counters accepted/(accepted+added), zero PII.
        NB presidio-evaluator API changed: predict_dataset -> CanonicalMapper ->
        calculate_score_on_df (evaluate_all/calculate_score now raise).
  - [ ] SURNAME GAZETTEER: no clean open German surname list exists to download.
        BUILD top-5-10k from Wikidata SPARQL (Q101352, CC0); surnames are
        head-heavy so this covers the measured failure. Demote the intersection
        with the Leipzig top-30k frequency list (CC-BY-4.0, ~1MB) to
        context-required (deny_list_score below threshold, let the context
        enhancer lift) so Koch/Bauer need context but Öztürk scores full.
        Given names: Berlin Vornamen (CC BY 3.0 DE) — real diverse registrations.
        DO NOT build the negative "capitalized but not a known noun" gazetteer:
        blind to the top-20 (they ARE dictionary words) and a decompounder
        backfires (Rosenberg/Neumann split into valid nouns => suppresses names).
        UNUSABLE: hunspell de_DE (GPL — the tri-licence covers the LIBRARY not
        Jacke's DATA), names-dataset (Facebook breach), DeReWo (CC BY-NC), GfdS
        (commercial), germandict (no licence grant), DWDS (CC BY-ND).
  - [ ] ONNX NER UNION: torch is BUILD-time only — export/download the int8
        .onnx (279MB, < the 610MB spaCy model already shipped) + tokenizer.json,
        ship them, run via onnxruntime (13.4MB MIT wheel, no admin; VC++2019
        already satisfied by CPython) + tokenizers, as a SECOND Presidio
        recognizer UNIONed with spaCy (recall only rises). ~100 lines of
        encode/infer/BIO-decode; skip optimum/transformers. Candidate:
        onnx-community/multilang-pii-ner-ONNX (MIT, ai4privacy-trained =
        form/letter-shaped) — but its 0.954 F1 is self-reported on synthetic
        data; JUDGE IT ON OUR HARNESS, not the model card.
        REJECTED: de_dep_news_trf (HAS NO NER COMPONENT at all); GLiNER (hard
        torch dep + weak German, 38.9 F1); gliner2-onnx (experimental v0.1.1).
        Optional: drop de_core_news_md + en_core_web_md (=114MB back).
  - [ ] Presidio has NO score fusion (remove_duplicates: highest score wins,
        never sums, recognizers never vote) — union/fusion must build on
        core._resolve_overlaps.
  - [ ] context_suffix_count=0 by default => "Müller, Sachbearbeiter" gets no
        boost. Consider raising.
  - [ ] HITL: build the ALLOW-list direction first ("Sparkasse is not a
        person"). Salted-hash name storage is STRICTLY DOMINATED (still personal
        data per EDPB 01/2025, brute-forceable at ~10^6 name space, and breaks
        match variants). deny_list compiles to literal alternation: matches
        Müller/MÜLLER/müller but NOT "Müllers"/"Mueller" — use regex patterns.
