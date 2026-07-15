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
