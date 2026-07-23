# Run: full-codebase audit remediation

Started 2026-07-23 from the `/audit-loop` full-codebase sweep. Two rounds of
multi-agent remediation (4 fix agents + 4 adversarial verifiers per round,
disjoint file ownership), plus lead-owned deployment hardening.
Tests: **125 -> 329, all green.**

## METHOD

Each cluster was fixed **test-first** (a test that demonstrably failed against
the tree first), then attacked by an **independent verifier instructed to refute
rather than confirm**. That second stage is what earned its keep:

- Round 1 finished with all four clusters reporting success and a green suite,
  yet every verifier returned `holds: false` — 2 criticals and 3 highs remained,
  **two of them regressions introduced by the fixes themselves**.
- Round 2 closed cluster A completely (`holds: true`) and all four verifiers
  confirmed `round1_still_holds: true` — no round-1 finding was re-opened.
- Round 2's own verifiers then found that **C's critical had been reported fixed
  and was not**: the agent stated its repair was "never let the tail start with a
  space" and shipped a pattern that still had the space. The lead fixed that one
  directly. Lesson worth keeping: a green suite plus a confident report is not
  evidence; the test that would have caught it existed but only parameterised the
  easy half of the input space.

## OPEN ITEMS (nothing here is a known leak unless marked)

### Needs a decision from the user — genuine trade-off, not a defect
- **Art.9 span swallows contained PII into a ONE-WAY redaction.** A line like
  `Diagnose: Depression bei Klaus Mueller, IBAN DE89...` is claimed whole by the
  Art.9 recognizer, which is `anonymize` (irreversible, no mapping row). The name
  and IBAN are destroyed rather than pseudonymized, in a tool whose purpose here
  is reversible pseudonymization.
  The obvious repair — terminate the value at a comma — is **wrong**: a German
  diagnosis legitimately contains commas (`Diabetes mellitus Typ 2,
  insulinpflichtig`), so it would leave health data in the file, i.e. trade a
  reversibility loss for a *leak*. The correct repair is to let a contained
  higher-specificity finding (PERSON, checksum-validated ID) survive and split the
  Art.9 span around it, which is a real change to `core._resolve_overlaps` and
  must not break the non-overlap invariant that stops spliced/garbled output.
  **Question for the user: is over-redaction (lose reversibility) or the
  span-splitting change the preferred direction?**

### Fail-loud verify (cluster B) — highest remaining severity
- **[CRITICAL] Word field-code cached results.** `w:fldSimple` instruction text is
  now surfaced, but the field's *cached result* runs nested inside it are not —
  and for DOCPROPERTY/MERGEFIELD/REF that cached result **is** the customer data.
  Reproduced: a docx whose result run holds `Hans Mueller, IBAN DE89...` applies
  successfully and the committed output still contains both verbatim.
- **[HIGH] Attributes are readable but not redactable.** The backstop now *reads*
  every XML attribute, but nothing redacts them. Where an attribute carries PII
  that was decided elsewhere (`w:comment/@w:author`, xlsx `tableColumn/@name`),
  the result is a **permanent hard fail** — the tool can never produce output for
  that document, and the message names nothing.
- **[HIGH] Sheet-rename misses hyperlink locations.** `_apply_sheet_renames`
  repoints formulas and defined names but not a cell hyperlink's `location`, so an
  index sheet linking to one client sheet each — the canonical companion to the
  "one sheet per client" workbook this feature exists for — hard-fails.
- [MEDIUM] Numeric attribute values are skipped by the widened blob, so a
  Steuer-ID/Kundennummer in an attribute is still invisible to the backstop. The
  docstrings claiming the blob holds "every attribute value" are inaccurate.
- [MEDIUM] `_is_phantom` treats a match covering a token's whole inner text as a
  phantom; a **one-way** token is exactly `[LABEL]`, so any removed value equal to
  a label (KONTO, PERSON, or any German column header) now causes a permanent
  no-output hard fail.

### Detection recall (cluster C)
- [MEDIUM] `DE_ADDRESS` false-positives on German bank boilerplate (`Zum Stichtag
  31.12.2024`, `In der Anlage 3`) and swallows the DATE_TIME finding. The
  hand-maintained exclusion list is not converging; a positive street-shape signal
  is likely the right answer.
- [MEDIUM] `CREDIT_CARD` fallback: `validated=False` bypasses the confidence gate,
  so a 16-digit internal reference cannot be filtered by configuration and its
  default action is one-way.
- [LOW] Date patterns fire on report typography (`* 2024 Prognose`, `Jahrgang
  2019`); Art.9 label:value claims negations (`Partei: keine`).

### Review UI (cluster D)
- [MEDIUM] The per-class caption and expansion header still say "N auto-accepted"
  from tier alone, so after a global Skip the header says 0 while the class card
  says 3.

### Carried from earlier waves
- Column **"skip"** policy (needs cell-level decisions); column-rule persistence;
  promote possible-misses to redaction; true row virtualization.
- PowerPoint `Open()` takes no password argument, so an encrypted `.ppt` can still
  raise a modal prompt. `.doc`/`.xls` are covered. A hard timeout would require
  running COM in a subprocess.

## RELEASE NOTES (must reach the operator)
- Documents anonymized **before** this build that contain a bare `[NOTIZEN_2]`
  style one-way column token are indistinguishable from a legitimate pseudonym,
  so re-identifying such a file can still substitute the wrong value. Only newly
  produced files are safe.
- A mapping written before the high-water counters existed, whose highest
  placeholder had already been erased, may recycle that one number once after
  migration.
- `Reset all mappings` deliberately **continues** numbering (so a number already
  printed in an archived document is never re-issued to a different person).
  `restart_numbering=True` exists but is opt-in and unsafe while any anonymized
  output survives.
- Opening Settings or Re-identify **while a document is being processed** now
  fails after a bounded ~10s wait with an explicit message, instead of silently
  clobbering the mapping. This is intended.
