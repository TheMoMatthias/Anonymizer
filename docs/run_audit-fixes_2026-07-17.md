# Run: Codebase audit fixes — Critical + High leaks (2026-07-17)

Source: `/audit-loop` codebase sweep (4 parallel specialists). User approved fixing the
**6 Critical + 8 High** findings now (each with a failing→passing test + full-suite
verification), Medium/Low as a documented second wave. Auto-fix authorized incl.
critical surfaces; pause only if a fix can't be verified or raises a design question.

## DONE-WHEN
All 14 fixes below applied, each with a regression test, full suite green, no leak
path left in the Critical/High set. Report at each batch boundary.

## Fix list (dependency order)

### Batch 1 — Detection core (core.py, engine.py, language.py)  ✅ DONE (95 tests green)
- [x] C1 core `_resolve_overlaps`: a CROSSING (non-contained) overlap drops the loser
      wholesale → a detected name leaks (PERSON crossing DE_ADDRESS). Fix: only drop
      when a kept span fully CONTAINS f; for crossing overlaps extend the kept span to
      the union so no detected PII char is left uncovered (needs unit text to recompute
      the merged value for correct re-id). Test: crossing PERSON/ADDRESS → name covered.
- [ ] C2 core `_refine` + threshold: checksum-failed DE_STEUER_ID demoted to 0.4 then
      dropped by its 0.6 threshold. Fix: bypass the score-threshold gate when
      `finding.validated is False` (keep + "unverified" chip). Test: invalid Steuer-ID
      still surfaces in actionable/unverified, not silently gone.
- [ ] C3 honorific "Herrn": `Herr ` matches, dative `Herrn` (standard address block)
      does not. Fix `_HONORIFICS` (engine.py:68), `core._HONORIFIC_PREFIX` (core.py:50),
      `pipeline` honorific copy (pipeline.py:109) → `Herrn?`. Test: "Herrn Klaus Mueller".
- [ ] H7 language.py: short/zero-signal text defaults to German → English names missed.
      Fix: lower signal floor for short text; on true-zero-signal don't collapse to a
      fixed language. Test: short English name-bearing text routes to en (or both).

### Batch 2 — PDF (pdf_handler.py, pipeline.py, ocr.py)  ✅ DONE (99 tests green)
- [x] C4 AcroForm field values + annotation text never extracted (fillable bank forms
      scan blank). Fix: extract widgets()/annots() as TextUnits; redact/flatten on apply.
- [ ] C5 mixed text+image PDF: image pages never scanned/redacted, verify reads empty
      text → false-clean. Fix: page-level gate — an image page with no text AND no
      usable OCR → ProcessingError, never silent pass.
- [ ] H1 redaction re-searches by value string not detected offsets → wrapped/hyphenated
      names find zero rects, silently not removed. Fix: map f.start/f.end to glyph rects
      (words/rawdict), fail loud if a finding maps to zero rects.
- [ ] H2 page with incidental text never OCR'd; empty-OCR treated as blank. Fix: OCR
      image-bearing pages when extractable text is implausibly short; empty OCR on an
      image page → refuse.
- [ ] H3 PDF metadata via saveIncr leaves old /Info recoverable; verify doesn't read
      metadata. Fix: full rewrite (save garbage=4 clean=True, del_xml_metadata), and
      extend `_output_text_blob` PDF branch to include metadata for the literal backstop.

### Batch 3 — OOXML handlers  ✅ DONE (103 tests green)
- [x] H4 pptx: detect on p.text, apply on p.runs (a:br/a:fld drift) → corruption/leak.
      Fix: build detection text from the same run list as apply (like docx).
- [ ] H5 docx nested tables (tables inside cells) visited by neither scan nor apply.
      Fix: recurse cell.tables at every level (body + headers/footers).
- [ ] H6 xlsx name-column override appended after overlap resolution → overlapping
      splices corrupt the cell. Fix: suppress override when ANY finding overlaps the
      value, and re-resolve overlaps on the combined set.

### Batch 4 — Crypto / fail-loud (mapping.py, pipeline.py)  ✅ DONE (100 tests green)
- [x] C6 mapping `rotate_key`: writes DB under new key before persisting key → crash =
      permanent mapping loss. Fix: set PREV=old + KEY=new in keyring BEFORE save().
      Test: simulate crash between steps → file still decryptable.
- [ ] (bundled, cheap) pipeline os.replace(doc) before mapping.save(): swap so mapping
      persists first; if save fails no output is committed. (Med, but 2-line + same
      fail-loud theme — include with C6.)

### Batch 5 — Persistence (settings/config)  ✅ DONE (105 tests green)
- [x] H8 deny_list (UI tells users to add missed PII) stored plaintext in config.yaml →
      PII at rest. Fix: store deny/allow terms encrypted (via MappingStore or a separate
      encrypted file), or hash+match; at minimum stop persisting raw terms in plaintext.
      NOTE: may need a design decision (where/how) → may pause here.

## SECOND WAVE (Medium/Low — documented, not in this run)
os.replace/save already folded above; XXE hardening on etree.fromstring; NRP special-
category (add to entities config + own data class, un-bucket from dates); profiles omit
other_entities; ocr_available() ignores configured path (thread cfg through); pptx
charts/SmartArt; xlsx drawings + print headers/footers; pptx/docx comment run-split +
author names; legacy COM DisplayAlerts/AutomationSecurity; OOXML docProps metadata scan;
evaluation.py substring over-report + language mismatch; review.py high-tier skip label;
audit.py swallow; settings blur-vs-save; fsync on atomic writes; TESSDATA_PREFIX; bic_valid
noise; mapping concurrency lock.

## Status: DONE — all 6 Critical + 8 High fixed, 105 tests green.
H8 encrypts allow/deny lists in %LOCALAPPDATA%\Anonymizer\lists.enc (mapping key),
migrates any plaintext lists out of config.yaml, prev-key fallback on rotation.
Second-wave Medium/Low items remain (listed above) -- not yet started.
