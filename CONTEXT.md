# Context / Glossary

- **Offline bundle**: the self-contained distributable folder (a relocatable
  standalone Python runtime + all dependencies + both spaCy models
  pre-installed) that colleagues copy from an internal network share and run
  with zero internet access and no admin rights. Built by
  `scripts/build_offline_bundle.ps1`.
- **Data class / sensitivity category**: a human-meaningful grouping of entity
  types the reviewer decides on as a unit (People, Contact, Financial IDs,
  Government IDs, Bank-internal refs, Dates/Other). Each entity type maps to
  exactly one data class; the class carries the default action. Decisions are
  made per data class by default, not per detected value.
- **Trust tier**: the confidence band a finding falls into (high / medium /
  low). High-confidence findings auto-apply the category default and collapse
  into an auditable section; medium/low surface for active review.
- **Scan–apply parity**: the guarantee that the redactions written to the
  output are exactly the set the human reviewed — no finding introduced by a
  fresh re-detection at apply-time can slip through un-decided.
- **Output re-scan**: a verification pass that re-scans the written `_psd`
  output and asserts zero residual PII of any category marked for removal; a
  failure blocks/flags the save.
- **Unmatched-risk bucket**: a low-priority list of sensitive-looking strings
  (digit runs, name-shaped tokens, email/IBAN-shaped patterns) that matched no
  recognizer — surfaced so the reviewer can catch false negatives.
- **Re-identify**: the guarded reverse operation that maps placeholder tokens
  (e.g. `[PERSON_1]`) in returned AI output back to their original values via
  the encrypted mapping. Audit-logged and confirmation-gated.
- **Detection profile**: a named preset ("Contracts", "Client statements",
  "HR docs") selecting which data classes are active and their default actions,
  chosen per run/batch to adapt the tool to a document type in one click.
- **Topical category**: a non-personal sensitivity type describing organizational
  content rather than an individual — TOOL, DIVISION, DEPARTMENT, LICENSEE,
  PROJECT. Detected structurally/by gazetteer, not by personal-entity NER.
- **Header→category detection**: assigning a topical category to a spreadsheet
  column from its header text (e.g. a "Tool"/"System" header ⇒ every cell in the
  column is a TOOL). Generalizes the existing `name_column_headers`⇒PERSON rule.
- **Auto-gazetteer**: the set of topical terms LEARNED automatically from
  category-labelled columns during a scan (no manual list), then matched
  document-wide. May be supplemented by a manual list.
- **Category propagation**: spreading an auto-gazetteer term across the whole
  document carrying its category (a TOOL named in a Tools column is redacted as
  TOOL wherever it recurs) — the person-name propagation engine generalized.
- **Corroboration-only**: ORG/LOCATION/NER_MISC are surfaced only when backed by
  more than a bare NER guess (pattern/anchor/propagation/validation/name-column);
  a bare guess is dropped. **Corroboration bypass**: a gazetteer/header match is
  user-confirmed sensitive and is always kept, exempt from this drop.
- **Cell policy**: a per-cell redaction decision keyed on `Sheet!Coord` (e.g.
  `Sheet1!A5`), the fine-grained EXCEPTION layer between the whole-column policy
  and per-value entity replacement. Overrides the column policy for that cell.
- **Redaction mode**: what a column/cell decision does — skip / pseudonymize
  (consistent reversible `[TOOL_1]` token) / redact (one-way `[LABEL]`) /
  **summarize**.
- **Summarize mode / structural placeholder**: replacing a cell's content with a
  zero-content shape descriptor (e.g. `[Freitext: 3 Sätze, ~140 Zeichen]`) that
  conveys format/size to a downstream LLM while withholding all original text.
  Contains no original characters, so the fail-loud verify passes by construction.
