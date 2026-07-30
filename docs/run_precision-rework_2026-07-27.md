# Run: detection precision rework + recall gap closure (2026-07-27)

Spec + resumable run-file. Grilled 2026-07-27 (25 questions, 6 rounds + 1
reconciliation). Supersedes the precision posture set in
`run_detection-precision_2026-07-23.md` where the two conflict.

## ⇢ WHAT IS LEFT (single source of truth — read this first)

State as of 2026-07-30, suite **597 passed**, everything below verified by measurement
rather than assertion. Phases 1 and 2 are DONE; 2.5 (ML speed) is DONE.

### 2026-07-30 (latest) — OCR damage, proprietary names, and a POS gate that was reading noise

Four items landed after the PERSON flip. Current measured state:

| section | n | recall |
|---|---|---|
| structured identifiers | — | 100% |
| Art. 9 oblique / held-out / bare cells | 12 / 8 / 16 | **100% / 100% / 100%** |
| names isolated (cold read) | 952 | 82% |
| **names inside identifiers / paths** | 60 | **100%** (was 73%) |
| spreadsheet cells | 68 | 96% |
| full letter | 340 | 97% |
| scanned letter, mixed OCR damage | 24 | 96% |
| unanchored memo | 340 | 97% |
| decoy false positives | 84 | 4/84 |
| audit workbook · apply + verify · fixture leaks | — | 293/293 · passes · 1 |

**1. OCR-damaged names — `ocr_mixed` 75% → 96%.** The realistic scan is not a wholly
mangled document; it is one where the salutation survives cleanly and the body says
`Mul1er`. `core.ocr_skeleton()` folds only what a scanner actually does (i/l/1, O/0,
rn→m, `ii` for an umlaut) and is compared **only against names the document already
established** — that restriction is the entire safety argument, since the path can never
invent an entity, only recognise a second spelling of somebody already being redacted.
`b`/`h` is deliberately NOT folded: it would merge `Bauer` and `Hauer`, both real
surnames. Ambiguous skeletons (two different propagated values folding together) are
dropped outright.

Two wrong turns worth keeping:
* Tagging the match `source="propagation"` **rescued nothing** — propagation is
  deliberately not corroboration, so the damaged group was still demoted. It had to be
  skeleton **inheritance** in the demote loop, alongside the genitive rule.
* `_rejected_by_precision` discarded exactly the two corruptions containing **digits**
  (`Mul1er`, `0sterkamp`, 0/3) as number-like. It is a shape filter written for clean
  text; running it on deliberately damaged text rejects the evidence the path exists to
  use. Call removed with the justification recorded at the call site.

`Mul1er` and `0sterkamp` remain 0/5 — a hard floor, not a bug: both are digit-bearing
and the fold is deliberately conservative.

**2. `Alteryx` / `OpenClaw` diagnosed — a structural limit, not a defect.** They are the
only two tool names in the fixture that never appear in a declaring column
(`Eingesetztes Tool`). Every other tool has a whole-cell occurrence the topical gazetteer
learns and propagates. Three mechanisms shipped, because none covers the space alone:
* `data/product_names.txt` — ~120 curated commercial tools/vendors, loaded via
  `_product_names()`/`is_known_product()`, corroborating across all NER types.
  **This alone fixed `Alteryx`: fixture leaks 2 → 1.**
* `data/project_names.txt` — ships **empty** by design, with the reasoning in the file.
  `Nordstern`, `Seidenpfad`, `Habicht`, `Delphin`, `Marschall` are all in the fixture and
  all ordinary German nouns. Nothing separates "Projekt Delphin" from a sentence about
  dolphins, so only the declaring column or this file can find them.
* `looks_like_proprietary_name()` — capitalisation carries **zero** signal in German
  (every noun is capitalised), so the signal is vocabulary: a capitalised token that is
  neither German vocabulary nor a German compound (3-part `_decomposes_into_vocabulary`).
  Measured on the decoys: **0/12 compounds, 0/6 ordinary words, 7/11 proprietary names.**

Confining candidates to the demoted band cost 3 false positives across two attempts
before it held: an empty `source` did not mark them as guesses (`is_guess` tested only
`SpacyRecognizer`/`propagation`, so they read as **corroborated**), and then the
exact-value inheritance rule promoted them anyway. Fixed with
`PROPRIETARY_CANDIDATE_SOURCE`, `GroupedFinding.is_oov_candidate`, and an
**unconditional** guard in the demote loop.

**3. ⇢ The embedded-identifier strata were 50–92% because the POS gate was reading
noise, and the 92% was as unprincipled as the 50%.** This is the item most worth
remembering.

`_is_pos_implausible` called `char_span(..., alignment_mode="expand")`. When the name sits
inside a glued identifier token, `expand` widens the span from `Müller` to the **entire**
`AKTE_Müller_2024` and then asks spaCy's tagger for a verdict on a string it never saw in
training. Measured, one name per template:

| token | tag |
|---|---|
| `K-Braun-2024` | **CCONJ** (a conjunction) |
| `AKTE_Müller_2024` | **ADV** |
| `Vertrag_Fischer_final_v2.pdf` | **VERB** |
| `Vertrag_Bergmann-Pohl_final_v2.pdf` | **NUM** |
| `K-Müller-2024` | PROPN → kept |
| `AKTE_Koch_2024` | NOUN → kept |

Same person, opposite verdict decided by the surrounding boilerplate. So `ticket_ref` and
`unc_path` were never structurally easier than `id_hyphen` — their glued tokens simply
drew a name-like tag more often. **Any reading of those four numbers as a difficulty
ranking was wrong.** Note this survived the underscore-boundary fix in `7b1fcad`: that
made propagation *reach* into the identifier, and the gate then threw the match away
again, which is why the strata did not move.

Fix: `alignment_mode="contract"` in both `_is_pos_implausible` and
`_is_german_nominalization` — keep only tokens lying wholly within the value, which is
what both docstrings always claimed to test, and **abstain** when that is empty, because
a check that only ever REJECTS must not act on a verdict about a different string. Entity
spans on the direct NER path already align to token boundaries, so this is a no-op there;
it changes only the regex-offset caller (propagation), which is exactly where the
misalignment arises. `embedded` 73% → **100%** on all five contexts, every other stratum
unchanged, decoys 4/84 unchanged.

**Generalisable lesson:** `alignment_mode="expand"` turns "no evidence" into "confident
wrong evidence" whenever an offset comes from a regex rather than from spaCy's own
tokenizer. Any future filter reading POS at regex offsets needs `contract` + abstain.

**4. Still open here:** `OpenClaw` leaks by design (belongs in the user's
`project_names.txt`); `ocr_noise` sits at 75% (`Mul1er`, `0sterkamp`); `bare_lower`
across strata is the weakest remaining context; the surname lexicon behind a flag is
still unbuilt ("measure it first, then decide").

### 2026-07-30 — the recall harness was made much harder, and it found a lot

User steer that governs every trade below: **"we should never miss something and rather
review"** — a miss is a disclosure, an over-flag is review time. Precision work is still
wanted, but it never wins a tie against recall.

`anonymizer/evaluation.py` gained four axes it never measured. This matters more than any
single fix: **the old harness was scoring 86% on a set of contexts that excluded every
shape the tool was actually bad at.** The four axes:

1. **Adversarial name shapes** — new strata `particle` (von/van/de/zu, incl. stacked
   "von der Leyen"), `hyphenated`, `transliterated` (Nguyễn, Đorđević, Þórsdóttir).
2. **Oblique contexts** — `role_reference`, `after_preposition`, `distribution_list`,
   `maiden_name`, `initials`: a person named with NO honorific and NO label.
3. **Structured traps** (`measure_workbook_traps`) — a real xlsx whose column headers
   lie ("Status"), say nothing ("Feld_7"), are missing, or wrap the name in an id.
4. **Art. 9 stated obliquely** (`measure_art9_oblique`) — the fact in a plain sentence
   with no list word and no label ("dauerhaft auf den Rollstuhl angewiesen").

Plus two scoring rules that stop the report flattering itself: a **hyphenated name needs
BOTH halves** (leaving "Rottluff" is a disclosure, not a half-success), and a **particle
is not scored** (leaving a bare "von" discloses nobody). And a new
`measure_unanchored_documents` section — a memo that never uses an honorific, so
propagation has no seed. That is the tool's true floor and nothing measured it before.

| section | old harness | hardened, BEFORE fixes | hardened, AFTER fixes |
|---|---|---|---|
| structured identifiers | 100% | 100% | **100%** |
| Art. 9 oblique | *not measured* | 50% | **50%** |
| names isolated | 86% | 74% | **88%** |
| spreadsheet cells | *not measured* | 48% | **77%** |
| full letter | 98% | 92% | **97%** |
| unanchored memo | *not measured* | 59% | **90%** |

Precision held exactly: **27/84 decoys, unchanged**, audit workbook **293/293**, apply +
fail-loud verify pass. So this was recall bought for free, not traded.

**What was fixed (each was a real defect, found only because the harness got harder):**

- **`engine._NAME` could not match a particle name at all.** It required every token to
  start `\p{Lu}`, so `"Sehr geehrter Herr von Bergen,"` — the most common line in a German
  bank letter — anchored NOTHING. `zu Guttenberg` was 0/5 in every context including a
  full letter. Particle stratum: salutation 38% → 100%, full letter 65% → 100%.
- **Naming somebody by ROLE defeated detection almost completely.** `role_reference`
  scored 0% for German common-noun surnames, 12% for rare German, 12–50% foreign. New
  `role_noun_name` anchor (Einreicher/Antragsteller/Zeuge/Bürge/… + English). Now 100%
  across every stratum.
- **Birth names** (`geb. Winkler`) — 20% → 100% via `birth_name`.
- **Initials** (`B. Winkler`) — new `initial_name` anchor. NOTE the trap here: it was
  first written at score 0.55, PERSON's `confidence_threshold` is **0.6**, and
  `detect_unit` drops below-threshold results silently — so the pattern matched, produced
  a raw result, and contributed exactly nothing while looking implemented. Caught by a
  unit test, not by the harness.
- **Column-level name inference** — the grill's fourth corroboration source, now BUILT
  (`xlsx_handler._inferred_name_columns`). A column is read as people from its CONTENT
  when the header will not say: ≥4 values, >50% distinct, ≥80% name-shaped, and ≥2 (and
  ≥20%) independently confirmed by the given-name gazetteer or the model. The confirmed
  minority is evidence about the column, which rescues the everyday-word German surnames
  the model reliably misses in a bare cell. `lying_header` 10% → 100%, `opaque_header`
  90% → 100%, `no_header` 80% → 100%, `initials_cell` 50% → 100%.
- **The enum/controlled-vocabulary guard** (grill item 3) landed with it, because it had
  to: the inference read the fixture's `DB_Setup` — a sheet that exists only to back
  dropdowns — as a column of people, for +2 false positives. Now any column that is a
  declared validation SOURCE, and any sheet holding one, is excluded from inference.
  Deliberately NOT applied to the header override: a dropdown of employee names under a
  "Bearbeiter" header is normal, and suppressing it would be a leak.

**Parity note:** `_inferred_name_columns` is derived in BOTH `scan()` and `apply()`, and
in apply it is computed BEFORE the sheet renames so its keys are the original titles.
Deriving it after would silently disagree with scan on every renamed sheet — a parity
break the fail-loud verify would catch, but only after the fact.

### 2026-07-30 (later) — the anchors were invisible whenever spaCy agreed

Two defects found while researching the remaining gaps. Both are architectural, both
were silently degrading the corroboration model, and fixing them is a pure win.

**1. Presidio deleted every anchor that spaCy also claimed.**
`EntityRecognizer.remove_duplicates()` runs INSIDE `analyze()`: it sorts by descending
score and drops any result contained in a higher-scored result of the SAME entity type.
spaCy reports PERSON at a **flat 0.85**, so `honorific_name` (0.75), `labelled_name`
(0.70), `role_noun_name`/`initial_name` (0.60) and `birth_name` (0.65) were destroyed
before `_absorb_corroborating_source` — or anything else in this codebase — could see
them. **The anchors only ever contributed on spans where spaCy did not fire.**

The smoking gun: under corroboration-only an ANCHORED letter scored **0/5** while an
unanchored memo scored **4/5** for the same name. The memo's role/initial anchors were
uncontested and survived; the letter's honorific anchor lost to spaCy and the group
arrived as pure `SpacyRecognizer`. Raw dump for "Winkler":
letter `source='SpacyRecognizer'` on every unit, memo `source='PatternRecognizer'` on two.

Fix: one constant, `engine._ANCHOR_SCORE = 0.86` for all five anchors — the same
sandwich `DE_ADDRESS` already documents (above spaCy's flat 0.85, below the 0.9
auto-accept bar, so the review TIER is unchanged). This is a corroboration fix, not a
confidence claim.

**2. Corroboration did not cross a group boundary.**
Groups are keyed by `(entity_type, value)`. Two consequences, both measured as the
uniform "4/5" across nearly every name in the harness:
* the same name typed PERSON in one sentence and ORGANIZATION in another (measured:
  `"Verteiler: Rechtsabteilung, Winkler, Innenrevision"` types Winkler ORGANIZATION)
  formed a second group that was demoted while the identical characters were being
  redacted elsewhere;
* `_inherits_from_base_name` handled a bare surname inheriting from a corroborated FULL
  name, but not the reverse — and the signature line "Mit freundlichen Grüßen Ayşe
  Winkler" forms its own group that must inherit from the corroborated bare "Winkler".

Fix: `corroborated_any_type` (exact string, any entity type — identical characters are
unarguably the same disclosure) plus the reverse token direction for PERSON only.
`corroborated_name_parts` now also splits on hyphens, so a double-barrelled name
contributes both halves.

Measured, both fixes, PERSON flip still OFF:

| | before | after |
|---|---|---|
| full letter | 97% | **100%** |
| unanchored memo | 90% | **100%** |
| names isolated | 88% | 88% |
| spreadsheet cells | 77% | 77% |
| decoy false positives | 27/84 | **27/84** |
| audit workbook | 293/293 | 293/293 |

### ✅ THE PERSON FLIP IS SHIPPED (2026-07-30)

`PERSON` is now in `_CORROBORATION_ONLY_ENTITIES`. The blocker below was cleared by
making the multi-value splitter reachable: `_value_segments` splits a cell on
`; | newline _x001E_`, `_analyze_cell_text` claims each name-shaped SEGMENT, and
crucially `_inferred_name_columns` now judges shape per segment too — without that last
part the splitter was built but unreachable, because a column of
`"von Bergen; intern geprüft"` never passed the whole-value shape gate and so was never
recognised as a people column at all. That took `multi_value_cell` 60% → **100%**.

Final state, versus the start of this session:

| | session start | **now** |
|---|---|---|
| decoy false positives | 27/84 | **4/84 (85% cut)** |
| audit workbook | 293/293 | 293/293 |
| apply + fail-loud verify | passes | passes |
| structured identifiers | 100% | 100% |
| names isolated | 86%¹ | **88%** |
| spreadsheet cells | *unmeasured* | **83%** |
| full letter | 98%¹ | **100%** |
| unanchored memo | *unmeasured* | **100%** |

¹ on the OLD, easier harness — not comparable; the hardened equivalents were 74% and 92%.

Every stratum equal or better, false positives down 85%. Suite 535.

### ~~⇢ THE PERSON FLIP IS NOW ESSENTIALLY FREE — one blocker left~~ (CLEARED)

Re-measured with both fixes above **and** `PERSON` added to
`_CORROBORATION_ONLY_ENTITIES`:

| | shipped (flip off) | flip ON + both fixes |
|---|---|---|
| decoy false positives | 27/84 | **4/84 (85% cut)** |
| audit workbook | 293/293 | 293/293 |
| apply + fail-loud verify | passes | **passes** |
| names isolated | 88% | 88% |
| full letter | 100% | **100%** |
| unanchored memo | 100% | **100%** |
| **spreadsheet cells** | **77%** | **67%** ← the only regression |

The entire remaining cost is **one trap**: `multi_value_cell` 60% → 0%
(`"von Bergen; intern geprüft"` — spaCy claims a dirty NER_MISC span over part of the
cell, which the flip then demotes). `id_shaped_cell` is 0% either way.

**So: fix multi-value cell splitting and the flip becomes strictly better than shipped
on every single axis.** That is the highest-value work item in this file.

### ✅ ART. 9 IS CLOSED (2026-07-30) — 25% → 100%, and it generalizes

| section | before | after |
|---|---|---|
| Art. 9 stated obliquely | 25% | **100%** (12/12) |
| Art. 9 in bare cells | 62% | **100%** (16/16) |
| Art. 9 HELD OUT (in no word list) | — | **100%** (8/8) |

The held-out stratum is the one that matters. Its eight values are absent from every
shipped list on purpose, so they can only be caught by a disclosure FRAME — it answers
"does the mechanism generalize" rather than "did the list grow to contain its own
benchmark". **Do not delete it, and do not add its values to any list.**

**The key structural finding: frames and lexicons solve different halves, and the split
is predictable.** A frame generalizes when the frame itself is the evidence — "leidet an
X" states a health fact whatever X is. It cannot help when the signal is the NOUN:
"besucht die Moschee" and "besucht die Filiale" are the identical sentence shape. So
health went to frames, religion/union/ethnic vocabulary went to lists, and each covers
what the other structurally cannot.

Three sub-findings worth keeping:

* **German verb-final word order doubled the work.** "ist an Tuberkulose *erkrankt*" and
  "wurde in den Ausschuss *gewählt*" put the participle at the end, so every verb-first
  frame needs a wrapped twin or it covers only half the sentences its own trigger word
  appears in. Three of eight held-out probes failed on exactly this before the twins
  were added.
* **Same-sex partnership is marked by the POSSESSIVE, nothing else.** "seine Ehefrau" is
  an ordinary man's wife; "ihre Ehefrau" is a woman's wife. That pronoun is the entire
  Art. 9 signal — which is precisely why a bare "Ehefrau" must never be listed. It would
  flag every married customer in the file and one-way destroy the word.
* **A party is named mid-sentence far more often than at its start.** The list held
  "Die Grünen" case-sensitively, so "kandidierte für die Grünen" — the ordinary phrasing
  — matched nothing.

Every Art. 9 pattern added sits at 0.86 = **review tier, never auto-accept**, because
these types carry a one-way `anonymize` action and one-way destruction on a frame match
is unrecoverable. Pinned by a test.

One list extension was tried and **reverted**: every `…gemeinde` term
("Kirchengemeinde", "Pfarrgemeinde", "Freikirche") sits UNINFLECTED inside organisation
names, which breaks the property that makes these lists safe to extend at all — they
match uninflected forms only, so `\b` fails on a trailing inflection and org names
survive. An existing test caught it. That invariant is now pinned for the new terms too.

**ML on prose is NO LONGER NEEDED for Art. 9.** It was on the plan and is not being
built: deterministic frames and vocabulary reached 100% on all three sections at zero
runtime cost, while the measured ML path reached only 9/12, mistyped two of its three
catches (an Art. 9 type is one-way `anonymize`; ORGANIZATION/PERSON are reversible
`pseudonymize`, so the classification is not cosmetic) and added pronoun/role-noun false
positives (`Sie`, `Der Kunde`, `Mitglied`). Revisit only if a new gap appears that
frames cannot express.

### Art. 9: the 50% was never what it looked like (superseded — kept for the diagnosis)

Probed each of the 12 oblique probes, ML off vs ML on (`vendor/gliner-model`, offline):

* **Only 3 of 12 are caught by an actual Art. 9 recognizer** (`Chemotherapie`,
  `Burnout`, `Fingerabdruck` — all word-list hits). The rest of the "successes" are
  spaCy mistyping a capitalized German noun: `Ramadan` → PERSON, `Streikgeld` → PERSON,
  `Grünen` → ORGANIZATION. They are redacted by accident.
* **This means improving PERSON precision REMOVES Art. 9 catches.** The two goals are
  directly coupled, and the coupling is invisible in the aggregate number.
* **ML on takes it 6/12 → 9/12**, fixing `Moschee` (correctly, → DE_RELIGION 0.774),
  `Betriebsrat` (→ ORGANIZATION 0.685, mistyped) and `Ehefrau` (→ PERSON 0.786,
  mistyped). Mistyped still redacts, but an Art. 9 type is one-way `anonymize` while
  ORGANIZATION/PERSON are reversible `pseudonymize` — so the CLASSIFICATION matters.
* ML also adds noise: `Sie` → PERSON 0.902, `Der Kunde` → PERSON 0.899, `Mitglied`,
  `Kontoinhabers`, `Die Familie`. Pronouns and role nouns as people.
* Still leaking with ML on: `Rollstuhl`, `Kontingentflüchtlinge`, `Romanes`.

**spaCy word-vector similarity is DEAD — do not try it.** Measured cosine to category
prototypes: the DECOYS score higher than the targets. `Bearbeitungszeit`→health 0.488
and `Abrechnung`→union 0.537 both beat `Rollstuhl`→health 0.277 and `Moschee`→union
0.234; `Portfoliobeitrag` and `Datenfeeds` have no vector at all (German compounds).
Any threshold catching the true positives flags more false ones.

What is left for Art. 9, in order of expected value: **disclosure FRAMES** ("leidet an
X", "ist auf den X angewiesen", "Mitglied im X", "wurde in den X gewählt") which
generalize past any word list; a much larger compound-aware Art. 9 lexicon; and ML
restricted to prose units with its hits RETYPED to the right Art. 9 category.

### Newly discovered gaps (from the structural probe, not yet in the harness)

* **ALL-CAPS names are invisible**: `"WINKLER"` alone in a cell → nothing at all.
  Common in forms and legacy exports. Not currently a harness stratum.
* **`"Winkler | intern geprüft"` produces a DIRTY span**: `NER_MISC 'Winkler |'`,
  including the pipe.
* `"K-Winkler-2024"` and `"AKTE_Winkler_2024"` → nothing (already known, now confirmed
  for the underscore form too).

### Measured gaps that remain (honest, ranked)

1. **Art. 9 stated obliquely — 50%, 6 of 12 leaking**: `Rollstuhl`, `Moschee`,
   `Betriebsrat`, `Ehefrau`, `Kontingentflüchtlinge`, `Romanes`. The word-list approach
   does not generalize and this quantifies by how much. Highest-consequence gap in the
   tool. This is the strongest remaining argument for the ML pass (item 6).
2. **`id_shaped_cell` — 0%**: a name wrapped in an identifier (`K-Winkler-2024`). Nothing
   currently looks inside a delimited token for a known name.
3. **`german_common_noun` isolated, 20–25%** on `bare_cell`, `prose_oblique`,
   `distribution_list`, `after_preposition`. Largely mitigated in real documents
   (unanchored memo for this stratum is 96%) but not in isolation.
4. **`multi_value_cell` — 60%**: `"Winkler; intern geprüft"` fails the name-shape gate,
   so neither the header override nor the inference can claim it.

### ⛔ BLOCKER — the PERSON flip trades a big precision win for a real recall loss

Diagnosed 2026-07-27. The apply-round-trip failure recorded here previously is **SOLVED**
(three real bugs, all fixed and shipped — see below). What remains is a genuine
**quality trade-off**, not a defect, and it is the user's call rather than mine.

Reproduce with one line: add `"PERSON"` to `core._CORROBORATION_ONLY_ENTITIES`.

| | without PERSON (shipped) | with PERSON |
|---|---|---|
| false positives on decoys | 27/84 | **4/84 (86% cut)** |
| audit-workbook recall | 293/293 | 293/293 |
| apply + fail-loud verify | passes | **passes** |
| **per-occurrence recall, realistic letters** | **98%** | **80%** |
| — german_common_noun | 98% | 88% |
| — german_rare | 98% | 72% |
| — foreign | 100% | 70% |

**Why it is not shipped:** an 18-point drop on the most realistic stratum is a leak
increase, and for this tool a miss is a disclosure while an over-flag is review time.
Fully missed with the flip on: `Winkler`, `Habermehl`, `Osterkamp`, `Öztürk`,
`Kowalczyk`, `Demir` — all **0/5**.

**Note the instrument trap:** the audit workbook reads 293/293 *either way*, because its
recall matching is value-keyed and lenient. Only `scripts/measure_recall.py`, scored per
occurrence, exposes the loss. **Run both before touching that line.**

**What is still missing:** corroboration for a **bare surname**. Three sources were added
chasing it, taking the stratum 57% → 80%, but an anchored salutation
("Sehr geehrter Herr Winkler,") still is not reaching the bare-surname group for those
six names. That is the next thing to diagnose — start by dumping the findings and their
`source` for one such letter and seeing why the anchor does not corroborate the
single-token group.

The grill's fourth corroboration source, **column-level name inference**, is still
unbuilt and is the most likely remaining lever.

### Then — the rest of Phase 3

This is the phase that actually removes the false positives. Signed off in the grill,
not yet started. In dependency order:

1. ~~**PERSON becomes corroboration-only, DEMOTED not dropped**~~ — BUILT, MEASURED (28→5 FPs), REVERTED. See the BLOCKER above.
   `_CORROBORATION_ONLY_ENTITIES` (core.py:81) currently holds NER_MISC/ORGANIZATION/
   LOCATION. Adding PERSON is a one-line change with a large blast radius — read the
   note under WHAT THE AUDIT ACTUALLY FOUND first: on the reported export EVERY real
   person had `is_ner_guess=True`, so this MUST land together with item 2 or it drops
   every name.
2. ~~**The four corroboration sources**~~ (grill decision 5) — **ALL FOUR NOW BUILT**:
   repaired name-column headers (Phase 1, 83 people recovered), a curated given-name
   gazetteer (909e408), GLiNER hits (core.py:810), and **column-level name inference
   (2026-07-30, see above)**. This is what unblocks item 1 — re-measure the PERSON flip
   now that a bare surname in a column can be corroborated without the model.
3. **The enum / controlled-vocabulary signal** (grill decisions 3/10) — **PARTLY DONE**:
   declared data validations are read (`_validation_source_columns`) and value repetition
   gates the inference (`_INFER_MIN_DISTINCT_RATIO`). Both currently gate the INFERENCE
   only. Still open: using the same signal to demote enum values claimed by other paths —
   which is where most of the surviving 27 false positives live (status labels and German
   compound nouns under an `Ownership…` header).
4. **The ML veto as a DEMOTION** (grill decisions 14/15): model ran on a text and saw
   no person ⇒ demote the bare spaCy PERSON hit. Evidence it will work: GLiNER scores
   all three decoy classes BELOW the 0.3 threshold (`Die Effizienz der
   Reaktionszeiten` 0.057, `Datenfeeds` 0.276, `Portfoliobeitrag` 0.197).
5. ~~**Demoted band as its own export section**~~ — DONE (c5f4e4c): ScanResult.demoted, a `demoted` export bucket, and a stats count.

**Target:** the 31 false positives on the 84 decoys. That number is reproducible today
(`scripts/score_test_workbook.py`), so progress is measurable per change.

### Then, in order

6. **Flip `gliner.enabled: true`** — the user's steer is to use ML by default where it
   is better, gated on Phase 3 landing, because today ML is measurably HARMFUL on
   structured workbooks (claims 3 decoys, mistypes tools as LOCATION, turned the project
   `Marschall` into a PERSON and displaced the correct finding). Needs a `-WithML`
   bundle verified on a second machine.
7. **Re-measure the real workbook.** Canonical file is
   `Downloads/mdx-big-beautiful-innovation-spreadsheet.xlsm`; baseline **442 flagged
   values / 200 PERSON / 194 bare-guess values**, taken with the pre-Phase-1 commit on
   that exact file. NOTE the exported CSV is NOT a valid baseline — it came from a
   different version of the workbook (`Malcom Werther` and `Ukom` are not in the file's
   bytes at all).

### Known bugs, unfixed, with evidence

- ~~Two Art. 9 word-list gaps~~ — FIXED (42da0e3): `neuapostolisch` (religion) and
  `Bandscheibenvorfall` (health). Found by `scripts/measure_recall.py`. Art. 9 is the
  their unambiguous siblings. structured_bare Art.9 is now 100%.
- **`*_Kommentar` columns are never treated as DESCRIPTION columns.**
  `_topical_header_res` uses `\b`, and `_` is a word character, so `\bkommentar\b` never
  matches `Strat_Innovation_Kommentar`. Free-text commentary columns are therefore never
  summarized — the same `\b`-vs-`_` defect fixed for PEOPLE headers in Phase 1, still
  present for TOPICAL ones. Deliberately not fixed: it changes what gets wholesale
  summarized, which is a behaviour decision, not a bug fix.
- **`bare_cell` recall is 30%** and no model will fix it (`german_rare/bare_cell` is
  already 100%; the obstacle is the word — "Koch" IS the word for cook). Structural
  signals only.
- **2 planted secrets still leak at apply** on the fixture: `Alteryx`, `OpenClaw` — tool
  names in prose, reached only via propagation. ML fixed `OpenClaw`; `Alteryx` remains.

### Closed, do not reopen without new evidence

- **ONNX/int8 export — DROPPED.** Its size win is partial (torch cannot leave the
  runtime: `gliner/model.py:11` AND `gliner/onnx/model.py:14` both import it) and its
  speed win is spent, since batching + memo replay already gave 3.4×.
- **The <5-minute ceiling — no longer a hard gate** (user, 2026-07-27). Quality wins
  ties. The DEFERRED content-keyed soft cap is dropped with it: it reduces coverage.
- **Label pruning (13 → 6, worth 1.8×) and skipping short structured cells** — both
  trade quality for speed, which the steer rules out. Available if that ever changes.
- **spaCy lg → sm** — tried and reverted (`d74d52c`).

## GOAL

Cut the German-common-noun false positives that dominate the flagged export, and
close the two recall gaps the same audit uncovered — without regressing measured
recall on the scored audit workbook.

## WHAT THE AUDIT ACTUALLY FOUND

Measured against
`C:\Users\MauriceMatthias\Documents\Anonymized\mdx-big-beautiful-innovation-spreadsheet_anonymizer_version_flagged.csv`
(source: `Downloads/mdx-big-beautiful-innovation-spreadsheet.xlsm`, 12 293
non-empty text cells, 20 sheets — all of which were scanned).

Baseline: **453 flagged values / 1931 occurrences.**

| values | occurrences | bucket |
|---|---|---|
| **81** | **780** | PERSON — not a person |
| 15 | 73 | PERSON — plausibly a real person |
| 230 | 234 | DESCRIPTION — real prose |
| **7** | **73** | DESCRIPTION — content-free (only `_x001E_` escapes) |
| 56 | 450 | DEPARTMENT (header-corroborated) |
| 6 | 229 | DIVISION (header-corroborated) |
| 54 | 85 | DATE_TIME |
| 4 | 7 | PHONE_NUMBER / NRP / NER_MISC |

**84% of PERSON values and 91% of PERSON occurrences are not people.**

### Causes, in order of size

1. **The ML pass never ran.** `gliner` + `onnxruntime` absent from the repo venv
   *and* `dist/Anonymizer-offline`; no `vendor/gliner-model`; live config
   `AppData/Local/Anonymizer/config.yaml:437` has `enabled: false`. Every PERSON
   hit in the export scores exactly `0.85` — Presidio's flat spaCy score. The
   export is the pre-GLiNER behaviour.
2. **The precision gate cannot reject a capitalized German common noun.**
   `_NAME_LIKE_POS` (core.py:154) includes `NOUN` so a surname like "Bauer"
   survives; German capitalizes every noun. Re-running all 90 spaCy-guess PERSON
   values through the five filters: **85 survive**. The NOUN group alone is 31
   values / 518 occurrences.
3. **A cell is not a sentence, so spaCy tags capitalized tokens PROPN** —
   disabling both POS-based filters. Proof: `Validierung` has the `-ung` suffix
   and length ≥ 8 yet survives, because `_is_german_nominalization` requires
   `NOUN and not PROPN` (core.py:201) and in `Aktuelle_Phase: Validierung` spaCy
   tags it PROPN. 39 values / 242 occurrences in this group.
4. **The workbook's own controlled vocabulary is unused.** `Gering` 368×, `Idee`
   80×, `Stark` 71×, `Künstliche Intelligenz` 36× — **9 dropdown values are 618
   of the 780 noise occurrences (79%)**, from `Liste_3Pkt_Skala` /
   `Liste_Relevanz` / `Liste_Qualitaetstreiber`. The top *real* name appears 21×.
5. **`_NOT_A_NAME`'s empty-cell guard misses Excel's escape.** `^[\W\d_]*$`
   (xlsx_handler.py:63) doesn't match `_x001E_` — `x` and `E` are word
   characters. 7 empty cells flagged `tier=high`, `action=summarize`.

### Recall gaps

6. **People leak** from columns headed `Owner`, `Einreicher`, `MDX_Lead`,
   `MDX_Proxy`. Reported as 13 from the export; **CORRECTED during Phase 1 to 83
   distinct people / 221 occurrences**, measured by running the candidate header
   widening over the workbook itself. The original 13 came from a name-shape
   heuristic over the export and was explicitly flagged as a floor — it was one.
   Additional names recovered include Sergii Piskun, Nils Bräunlich, Constanza
   Hiemenz, Timo Brandt, Patrick Lortz, Silke Müller, Simon Unterbusch, Mathias
   Regiert, Emir Balzevic, Anne Zopf and Siegfried Eckstedt. `_NAME_HEADER_TERMS` is German-only and contains none of those,
   so the whole-cell PERSON override never fires — verified by testing the header
   regex directly against each. Every name that *was* caught came from bare spaCy.
   Full names leaked: **Pascal Sternheimer, Thomas Schachtner, Ulf Gericke,
   Stefan Woithe, Claudius Wetzler, Anna Franziska Lorenz**. First names:
   **Marco, Thomas, Sergii, Cordula, Mirijam, Niklas, Hendrik**. Partial-leak
   hazard: `Claudius` is flagged standalone, so propagation redacts it inside
   `Claudius Wetzler` and emits `[PERSON_1] Wetzler` — surname in the clear.
7. **41 internal URLs, none flagged** — `https://emma.metzler.com/x/Oim3JQ`
   (Confluence), `https://meos.metzler.com/applications/meos/meos.nsf/lupOHB/27000001?openDocument`.
   **CORRECTED during Phase 1:** the initial diagnosis said "no URL recognizer
   exists". Wrong — Presidio's `UrlRecognizer` was already loaded and `URL` was
   already a supported entity. `URL` was simply absent from the `entities:` block,
   and `detect_unit` only requests configured entities (core.py:593), so it was
   never asked for. Verified: the shipped recognizer matches every real URL shape
   in this workbook and correctly ignores `Datei.xlsx` / `Version 2.0`.
8. **The possible-miss scanner found none of it** — all 100 rows are money
   amounts and project-ID tails (`26-001` from `BP-26-001`).
9. Mistypes: `MDX_PROXY_20` / `MDX_LEAD_51` / `PROJEKT_ID_37` → `DATE_TIME`;
   `227755` under a `Tatsaechliche_Kosten_EUR` header → `PHONE_NUMBER`;
   `24.03.2026 08` is a timestamp cut in half.
   **Cause found in Phase 1:** the **English** spaCy model tags a snake_case
   identifier as `DATE_TIME` at its flat 0.85 (reproduced for all three values),
   reached via the per-sheet language routing added in `21e0e19`. `DATE_TIME` is
   not in `_NER_ENTITIES`, so *no* precision filter applied to it at all.
10. **Suspected, NOT yet measured:** `_is_structural_nonname` rejects any span
    containing `_` (core.py:140), and multi-value cells carry `_x001E_`
    mid-string (`Kein Beitrag_x001E_Reiner Backoffice-Prozess`), so a name fused
    to one of those escapes is silently rejected. Verify in Phase 1.

## DECISIONS (from the grill)

| # | Decision | Chosen |
|---|---|---|
| 1 | Sequencing | Get ML running and re-measure **before** designing the gate |
| 2 | NOUN allow-set | Replaced by corroboration, not by a better word filter |
| 3 | Enum signal source | **Both** Excel data-validation lists **and** value repetition |
| 4 | Abstraction | **PERSON joins `_CORROBORATION_ONLY_ENTITIES`** |
| 5 | Corroboration sources | **All four**: fixed name-column headers · given-name gazetteer · GLiNER hits (already counts) · column-level name inference |
| 6 | Gazetteer | Curated, **given names only**, frequency-ranked, <~1MB. No surnames — a surname list would corroborate `Stark`/`Gering`, the exact noise being removed. No leak-derived dataset in a bank tool |
| 7 | Uncorroborated PERSON | **Demote** to a review band, never silently drop |
| 8 | Name-column headers | Ship English stems in the code-owned list; keep `name_column_headers` user-editable |
| 9 | Export shape | Demoted band gets its **own bucket section**, like `possible_miss` |
| 10 | Enum parity | **Content-keyed set, precomputed once per workbook**, precedent `topical_gazetteer` (xlsx_handler.py:119); passed via config so core stays format-agnostic |
| 11 | URL handling | **Whole URL, pseudonymized**, all hosts |
| 12 | Success instrument | **All three**: audit workbook is the gate, evaluation.py strata guard the bare-name case, MDX workbook is the sanity check |
| 13 | Test contract | **Rewrite deliberately**, one commit per intent change, each naming the decision that replaced it |
| 14 | ML veto | **Yes** — model silence is negative evidence… |
| 15 | …reconciled with #7 | …but it **demotes, it never drops**. One rule for both cases |
| 16 | ML cost gate | Run on **every distinct cell value**; measure, don't pre-optimise |
| 17 | ML install | **Torch-free onnxruntime first**. Fallback is NOT pre-authorized (see below) |
| 18 | Also in scope | `_xHHHH_` normalization at cell-read · DATE_TIME/PHONE mistypes · possible-miss retune · DEPARTMENT/DIVISION noise |
| 19 | Rollout | **4 phases, commit + measure each** |
| 20 | Extra scope | Flip `gliner.enabled` default · generalise the repetition enum to docx/pdf · retry spaCy lg→sm |
| 21 | Glossary | Terms stay in this run-file; `CONTEXT.md` untouched |

**Concern raised and reaffirmed:** I advised against including spaCy lg→sm (tried
and reverted in `d74d52c`; changing the NLP model while reworking the gate makes
every precision number ambiguous). The user reaffirmed it. It is therefore
sequenced **last**, after DONE-WHEN passes, so it can be reverted in isolation.

## CONTRACT

- **Inputs:** unchanged — the same documents, the same `config.yaml` (schema
  bumped v8 → v9 for the new keys, with `_resync_builtins` pushing code-owned
  defaults while preserving user-owned data, per the existing pattern).
- **Outputs:** the flagged export gains a **demoted-band section**; findings
  gain no new required fields (the band is derived from existing provenance).
- **Hard invariant:** scan/apply parity. Every new cross-cell signal is
  content-keyed and precomputed identically in both passes — never
  order- or counter-dependent, because scan iterates `_iter_cell_units(wb)`
  while apply iterates `ws.iter_rows()`.
- **Hard invariant:** with `ml_available()` False the tool still runs; ML absence
  may reduce precision but must never crash or silently reduce recall.

## LAYERS TOUCHED

`anonymizer/core.py` (the gate, corroboration set, build_scan_result) ·
`anonymizer/formats/xlsx_handler.py` (header stems, enum precompute, cell-read
normalization) · `anonymizer/gliner_recognizer.py` (veto path) ·
`anonymizer/data/default_recognizers.yaml` (URL recognizer, schema v9, new keys) ·
`anonymizer/config.py` (`_resync_builtins`) · `anonymizer/report.py` (export
section) · new data file for the gazetteer · `tests/test_precision.py`,
`test_recall.py`, `test_xlsx.py`, `test_export.py`, `test_config.py`.

## PLAN

### Phase 1 — Recall + contained fixes (no gate change)
English/owner header stems · URL recognizer · `_xHHHH_` normalization at cell-read
(+ verify cause #10) · DATE_TIME/PHONE mistypes · possible-miss retune ·
DEPARTMENT/DIVISION noise. **Measure:** 13 names caught, 41 URLs caught, 0
content-free rows, parity clean.

### Phase 2 — Get ML actually running
Torch-free onnxruntime path → build the pack with `scripts/fetch_gliner_model.py`
→ enable → **re-measure the MDX baseline with ML on** → verify determinism and
parity. **STOP AND ASK if the torch-free path proves impossible.**

### Phase 3 — Gate rework
PERSON → corroboration-only with demotion · the four corroboration sources ·
the enum signal (validation lists + repetition, content-keyed, via config) · ML
veto as demotion · enum repetition half generalised to docx/pdf.

### Phase 4 — Export band, gazetteer, default flip
Demoted-band export section · curated given-name gazetteer · flip
`gliner.enabled: true` (requires a `-WithML` bundle verified on a second machine).

### Phase 5 — spaCy lg → sm (last, revertable alone)

## AUTONOMY CONTRACT

### DONE-WHEN (all MUST, machine-checkable)
1. `uv run pytest` fully green.
2. `scripts/score_test_workbook.py`: recall ≥ baseline **and** planted-secret
   leaks ≤ baseline.
3. `anonymizer/evaluation.py` strata: no stratum below its recorded baseline.
4. MDX workbook: **all 13** named leaks caught (list in cause #6).
5. MDX workbook: **all 41** internal URLs caught.
6. MDX workbook: **0** content-free DESCRIPTION rows.
7. Scan/apply parity clean (`verify_output`).

### REPORTED, not gated
Flagged-section size (baseline **453** values / **1931** occurrences) · PERSON
noise share (baseline **84%** of values, **91%** of occurrences) · scan
wall-clock against the 5-minute ceiling. Deliberately not gated: with demotion
chosen over dropping, a raw count target would be satisfied by moving rows
rather than by detecting better.

### DEFAULTS (pre-authorized — proceed, don't ask)
- Tune constants (frequency cutoff, enum repetition threshold, column-inference
  N) by measurement against the audit workbook; record each value and what it
  was measured against.
- Rewrite `test_precision.py` tests that encoded the replaced design, one commit
  per intent change, each commenting which decision replaced it. Anything not
  justifiable comes back as a question.
- Choose the gazetteer's source, size and shipping shape; record provenance.

### NOT pre-authorized — STOP AND ASK
- Torch fallback if the torch-free onnxruntime path fails. Blast radius is bundle
  size (~800MB CPU-only torch vs the agreed <1GB target), which is the user's
  call, not a measurement.

### DEFERRED
- **Content-keyed soft cap on ML.** Trigger: the Phase 2 or 3 measurement
  breaches the 5-minute ceiling on the MDX workbook.

### ROLLBACK
Each phase commits green independently. `gliner.enabled: false` reverts every ML
behaviour (including the veto) instantly. Phase 5 is isolated and revertable
alone. The v9 schema resync preserves user-owned lists and the `enabled` toggle.

### OUT OF SCOPE
Anything not listed above — in particular no changes to the mapping DB, the
encrypted lists, the OCR path, or the review UI beyond the demoted band.

## Phase 2 — ML measured. GLiNER earns its place; the ONNX shrink does not pay for itself yet

### The plan changed twice, both times because a measurement contradicted it

1. **The "torch-free ONNX first" decision from this grill was already dead.**
   `run_gliner-completion_2026-07-26.md:178` had recorded, from measurement, that
   PyPI's Windows torch wheel is CPU-only and there is no CUDA trap to escape.
   Re-verified here: `torch==2.13.0`, `onnxruntime==1.28.0`, **zero
   `nvidia-*`/`cuda-*`**.
   *Correction on the cause:* I first blamed a stale KNOWN-ISSUE framing in
   `GLINER_VERIFICATION_CHECKLIST.md`. That was wrong — the checklist's step 1 already
   said "the feared trap does not exist here". The real cause was that I trusted a
   summary of an earlier session instead of reading the file, then recommended the
   torch-free path in the grill on that basis. Worth recording because the failure mode
   is subtle and repeatable: a carried-over summary can be stale in ways the repo is not.
2. **ONNX+int8 cannot remove torch from the RUNTIME.** `gliner/model.py:11` imports
   torch, and so does `gliner/onnx/model.py:14` — gliner's own ONNX wrapper. The
   export therefore buys pack size only: **1.85 GB → ~0.98 GB**, not the ~520 MB I
   projected. Reaching 520 MB means dropping `gliner` and owning tokenization plus
   span decoding, which 2026-07-26 rejected and which still looks right.

Real sizes (my earlier figures were estimates): pack **1.16 GB** — already minimal,
since `fetch_gliner_model.py` excludes `pytorch_model.bin` as "1.1 GB of nothing", so
the "trim the duplicate weights" option I offered was already implemented — and
runtime **693 MB** (torch 497 of it).

### The pack works air-gapped

| check | result |
|---|---|
| loads with `HF_HUB_OFFLINE=1` | yes — 5.7 s, `UniEncoderSpanGLiNER` |
| pack size | 1126 MB |
| inference | 107–126 ms/text on CPU |
| deterministic over repeated inference | yes, 5/5 identical — the parity precondition |
| full suite with ML installed | 489 passed, no numpy/torch/spaCy conflict |

### The audit workbook is the WRONG instrument for the ML question

It scores **293/293 without ML**, so there is no headroom for a model to recover
anything. With ML: recall unchanged, the false-positive list **byte-identical**, scan
**2.6 s → 110 s (42×)**. That is not evidence GLiNER is useless — it is evidence of
measuring against a ceiling. Recorded because the mistake is easy to repeat: a 100%
instrument cannot score a recall improvement.

### `measure_recall.py` HAS headroom, and there GLiNER is decisive

| stratum | without ML | with ML |
|---|---|---|
| german_common_noun / prose_oblique | **25%** | **95%** |
| german_common_noun / prose_full_name | 90% | 100% |
| german_rare / prose_full_name | 88% | 100% |
| german_common_noun / bare_cell | 25% | **30%** |
| **OVERALL (isolated)** | **86%** | **93%** |

Recovered: Weber, Klein, Schwarz, Koch, Wolf, Berg, Fischer, Vogel, Hahn, Kaiser,
Fuchs, Sommer, Mueller, Bauer in oblique prose; Schwanitz and Stein in full-name prose.

**The honest limit: `bare_cell` barely moved (25% → 30%).** A lone common-noun surname
in a spreadsheet cell — the most frequent shape in the real workbooks — is irreducibly
ambiguous. Note `german_rare/bare_cell` was already 100% while
`german_common_noun/bare_cell` stays ~30%: the problem is the WORD, not the model.
"Koch" alone is the word for cook. No model quality fixes that; the fix is structural,
which is exactly what the Phase 1 header override did (83 people recovered on the real
workbook). Stated plainly so nobody later expects ML to close this stratum.

### …but on the structured workbook GLiNER is actively harmful right now

The 9 findings it added to the audit workbook, and ~8 are wrong:

| added | problem |
|---|---|
| `LICENSEE 'Lizenzgeber'` 0.65 | the German word for *licensor* — also a column HEADER |
| `LOCATION 'Camunda'` 0.89, `LOCATION 'OpenClaw'` 0.85 | tools mistyped as locations |
| `ORGANIZATION 'European Union'` 0.85 | a planted DECOY ("EU regulations apply") |
| `ORGANIZATION 'Nationwide Building Society'` 0.79 | a planted DECOY (counterparty) |
| `ORGANIZATION 'Ruecklage'` 0.61 | a planted DECOY — German for "reserve" |
| `PERSON 'Marschall'` 0.86 | a PROJECT name, and it DISPLACED the correct `PROJECT 'Marschall'` |
| `PERSON 'Sachbearbeiter'` 0.85 | "caseworker" — a role word |

So GLiNER is strong on PROSE recall and unhelpful-to-harmful on structured business
data. That shapes Phase 3: the veto is worth having, but ML's *additive* output on
spreadsheet-shaped text needs its own gate, and mistyping (tool → LOCATION, project →
PERSON) is a distinct failure from over-claiming.

### Two bugs this surfaced, both fixed

**a) The scorer under-reported PRECISION (mine, from the honest-metrics commit).**
`covered()` was reused for decoys, but the two questions need opposite directions: a
secret is covered only by a finding at least as WIDE, whereas a decoy is falsely
claimed even by a NARROWER finding — it still redacts part of a sentence that should
have been untouched. GLiNER claiming `Nationwide Building Society` out of the decoy
`Credit Union: Nationwide Building Society` went uncounted. Split into `covered()` and
`claims()`. **Honest non-ML precision baseline is therefore 28/84, not 21/84.**

**b) A value that is also a COLUMN HEADER made every save fail** — general, not
ML-specific. Row 1 is used only as a schema label and never scanned
(`_iter_cell_units`), but `_literal_residual` read the whole package, so the tool could
demand removal of text it had already decided never to touch and then write NO file at
all. `Lizenzgeber`, claimed from prose on one sheet, collided with the row-1 header on
another. Fixed by exempting exact column-header text from the residual check
(`_column_header_texts`). Deliberately narrow: a header CONTAINING a name still
reports that name, and a deny-list term is never exempted — the user asserted it is
PII, so a leak stays a leak. Three regression tests.

### Final scored comparison, and the ceiling breach

With the header fix in place the ML round trip completes:

| | without ML | with ML |
|---|---|---|
| recall | 293/293 | 293/293 |
| false positives on decoys | 28/84 | **31/84** |
| scan | 4.5 s | **130.1 s** |
| apply | 4.5 s | **167.3 s** |
| planted secrets leaking into the output | 2 (`Alteryx`, `OpenClaw`) | **1 (`Alteryx`)** |

ML **fixed one of the two apply-level leaks** -- `OpenClaw`, a tool named in prose that
propagation missed. That is a genuine byte-level win, not a scan-side number. It cost
3 net false positives, all three decoys claimed as ORGANIZATION.

### DEFERRED TRIGGER HAS FIRED: the 5-minute ceiling

Round trip **9 s -> 297 s (~5 min) on the 16-sheet FIXTURE**. The real workbook has
roughly 8x the cells, so it is far over. The content-keyed soft cap is no longer
deferred -- its trigger condition is met.

But the cap is the second-best lever. **`apply` spent 167 s RE-RUNNING inference**,
which is pure waste: scan already computed exactly those spans, and the model is
deterministic (verified 5/5 identical), which is what makes replay safe. Memo-replay
was explicitly part of the 2026-07-26 determinism decision -- "Pin CPU execution
provider + thread counts, add a repeated-inference determinism test, AND memo-replay so
apply never re-infers" -- and it is NOT implemented. Doing it removes ~56% of the round
trip on its own AND strengthens parity (replaying scan's spans cannot diverge from
them, whereas re-inferring can only be argued to be identical). Recommended before the
soft cap, which merely reduces coverage.
- **Two Art. 9 word-list gaps**, unrelated to ML, both in the BARE form:
  `neuapostolisch` (religion) and `Bandscheibenvorfall` (health).
- **The ONNX/int8 export is NOT started**, pending the size decision: it buys
  1.85 GB → ~0.98 GB and nothing else.

## Phase 2.5 — ML speed, 3.4× with byte-identical output

### Policy change (user, 2026-07-27)

The <5-minute ceiling is **no longer a hard blocker**: "I would rather have a quality
output and wait 5 minutes longer than being bound to an unreasoned time threshold.
However we should optimize whatever we can." So quality wins ties, ML should be used
by default where it is measurably better, and speed work is still expected — but only
where it costs nothing. The DEFERRED soft cap (which reduces COVERAGE) is therefore
dropped in favour of optimisations that change nothing about what is detected.

### The levers, measured before building anything

| lever | effect | quality cost |
|---|---|---|
| batch inference (batch 8–16) | 126.9 → 52.9 ms/text (**2.4×**) | **none** — verified byte-identical spans AND scores |
| memo-replay at apply | removes the whole 167 s | none; strengthens parity |
| label count 13 → 6 | 59.6 → 32.6 ms (1.8×) | real — drops Art.9/licensee/division |
| skip short structured cells | short cells cost 50 ms vs 78.7 ms for prose | improves precision, small recall loss |

Two results that stopped me guessing: **batch 32 is SLOWER than 16** (59.5 vs
53.0 ms/text), so the batch is pinned at 16 rather than "bigger is better"; and torch
already used all 8 cores, so thread tuning was a dead end before any time went into it.

**Built:** memo-replay + batched priming. **Not built:** label pruning and the
short-cell skip — both trade quality, and the user's steer was explicitly the opposite.

### Result

| | before | after |
|---|---|---|
| scan | 130.1 s | **63.4 s** (2.1×) |
| apply | 167.3 s | **24.4 s** (6.9×) |
| **round trip** | **297 s** | **88 s (3.4×)** |
| recall / FPs / leaks | 293/293 · 31/84 · 1 | **identical** |

### Batching under-delivered at first, and instrumenting said why

Priming only the xlsx handler's header+value texts still left **337 of 801 predictions
unprimed**, and every one was a bare cell value with no header — `'Fischer'`,
`'K-14655'`, `'Kirchweg 55, 60311 Frankfurt am Main'`. Cause: **`_with_propagation`
runs a SECOND full detection sweep over raw unit text** to seed the propagate list, so
with ML on it silently doubles the inference bill. Primed there too, in the same
function, so scan and apply still derive an identical set — parity holds by the same
argument that function already rests on.

Worth keeping in mind generally: any pre-pass that re-detects is now an ML-cost
multiplier, not just a spaCy one.

### Design notes

* The memo lives on the BACKEND and the GUI caches one analyzer per session
  (`gui/app.py::_ensure_analyzer`), so scan's predictions are still resident when apply
  re-detects. That is what makes replay free rather than a persistence problem.
* Replay makes parity **stronger**: apply reuses scan's exact spans instead of
  re-deriving spans that are only *argued* identical (they are — determinism verified
  5/5 — but reusing beats arguing).
* Memo eviction (`_MEMO_MAX`) can only cost speed, never correctness: a miss re-infers,
  and inference is deterministic.
* `prime_gliner` is duck-typed and returns 0 on anything without a registry or a
  priming backend. The hardening tests caught this — they use stand-in analyzers, and
  an optimisation must never be able to break a caller.

## RESOLVED (2026-07-27): honest metrics, and a CRITICAL Art. 9 config bug found behind them

Chasing the over-reported recall below led to the worst bug in this repo's history,
and to three fixes. All landed before Phase 2, on the principle that an evaluation
gate which over-reports would happily certify the ML work as fine.

### 1. CRITICAL -- German GDPR Art. 9 detection did not run at all

`config.py::_resync_builtins` keyed the shipped recognizers by NAME:

```python
shipped_recs = {r["name"]: r for r in shipped.get("custom_recognizers", [])}
```

A recognizer name is deliberately NOT unique -- one entity type is emitted by
several entries, a GERMAN word list and an ENGLISH one (`DE_RELIGION` ships 1 de +
2 en), plus case-sensitive twins for `DE_HEALTH_DATA` / `DE_UNION_PARTY`. Keying by
name collapsed the shipped **27 entries to 17**, and because the `en` variants sit
last in the file, every GERMAN Art. 9 word list was overwritten by its English
counterpart.

Measured on the live config: `DE_RELIGION`, `DE_HEALTH_DATA`, `DE_UNION_PARTY`,
`DE_SEX_LIFE` and `NRP` were all registered for **`en` only**. So health data,
religion, union/party membership, sex life and ethnic origin were **not detected in
German documents at all** -- the exact class of leak `356337c` fixed in the other
direction, re-introduced on any schema bump. `merge_new_recognizers` already had
this right (see `recognizer_fingerprint`, which fingerprints the GROUP for exactly
this reason); this second path had been left behind.

Fixed group-aware, provenance refreshed so the two paths agree, and
`config_schema_version` bumped **9 -> 10** purely so the repair reaches configs
already in the broken state. Verified: 17 -> 27 recognizers, all five types
registered for `de` and `en`, and `Konfession: muslimisch` /
`Diagnose: chronische Migraene` / `Gewerkschaft: ver.di` detect again. Two
regression tests in `test_config.py`.

### 2. The scorer over-reported recall

`covered()` credited a secret when the DETECTED value was a substring of it
(`fv in v`), so planted German `muslimisch` scored as caught because an unrelated
finding on a DIFFERENT SHEET matched the English `Muslim`. That is what hid bug 1
for as long as it did. Now one-directional and whole-token, reusing
`evaluation._whole_token` (which already had the honest matcher, and the same
lesson in its docstring).

Tightening it initially swung the error the other way -- 12 postal addresses
reported as missed although DE_ADDRESS claims them as TWO spans ("Kirchweg 55" +
"60311 Frankfurt am Main", only the ", " unclaimed). So coverage may now come from
several findings together, by token union, with the limitation stated in the code.

### 3. A false positive could block the save entirely

Decisions are keyed by VALUE while apply re-detects per CELL, so a value falsely
claimed in one cell becomes a "removed value" that survives verbatim wherever
detection does not fire, and `_literal_residual` correctly refuses to write
anything. Four enum decoys did exactly that. Phase 3 of the scorer now skips
falsely-claimed decoys as a reviewer would, or one FP makes the apply path
unmeasurable.

The same investigation turned up a second, sharper instance: **openpyxl stamps
today's date into `dcterms:created`/`modified` on every save**, so a document
containing today's date as a data value, with dates being redacted, failed the
verify and produced NO output file -- nothing actually wrong. Date-dependent and
intermittent. Both fields are now scrubbed (privacy AND correctness); regression
test in `test_xlsx.py`.

### Honest baseline, as of this commit

`scripts/score_test_workbook.py` now exits 0 on the 16-sheet fixture:

| | value |
|---|---|
| recall | **293/293 (100%)** -- honestly matched |
| false positives on decoys | **21/84** |
| apply + fail-loud verify | passes |
| known leaks | 2 (`Alteryx`, `OpenClaw` -- tool names in prose, propagation) |

The 21 FPs are the enum-vocabulary and German-compound-noun classes. That is the
number Phase 3 has to move, and it is now reproducible.

## ORIGINAL FINDING (superseded by the above, kept for the record)

Discovered while extending the fixture. `scripts/score_test_workbook.py:165` counts a
planted secret as found when the detected value is a **substring** of it:

```python
for fv, g in found_vals.items():
    if v in fv or fv in v:     # <-- the `fv in v` direction is unsound
        return g
```

The `v in fv` direction is legitimate (a wider span, e.g. an address block, really
does cover the planted value). The `fv in v` direction is not: a SHORTER detection
does not cover a longer secret, and it does not even have to come from the same
sheet.

Measured consequence, and it is not hypothetical:

* `muslimisch` is planted twice as `special_category` in `Personal Vertraulich`
  (D4, D7) and is **not detected at all** in a full-document scan. Neither are
  `roemisch-katholisch`, `evangelisch` or `konfessionslos`. The only DE_RELIGION
  values the scan finds are `Muslim`, `Protestant`, `Roman Catholic`, `Buddhist` --
  all from the ENGLISH sheet.
* They are nevertheless scored as caught, because the English `Muslim` is a
  substring of `muslimisch`.
* So **`special_category 34/34 = 100%` is false, and the `204/206` total this
  run-file recorded as the DONE-WHEN gate is over-reported.** Confirmed identical on
  the pre-existing fixture, so this is long-standing and not introduced here.

`_literal_residual` is what exposed it: the removed value `Muslim` survives
verbatim in the output because it sits inside an undetected `muslimisch`, and apply
correctly refuses to write the file. That is the fail-loud contract working exactly
as designed -- the scan-side recall number was the thing that was wrong.

Not fixed here, deliberately: tightening `covered()` lowers every recorded recall
number in the repo, and re-baselining the gate mid-run is the user's call, not a
measurement default. Recommendation: match whole-token and keep only the `v in fv`
direction, then re-baseline. NOTE: in isolation with `languages: ["de"]`,
`Konfession: muslimisch` IS detected as DE_RELIGION -- so the German Art.9 miss is
specific to the full-document path, and the cause is not yet identified. Per-sheet
routing is correct (`Personal Vertraulich` -> `de`) and per-text routing of
`muslimisch` -> `de`, so neither is the explanation. Open.

## PROGRESS LOG

- 2026-07-27 — Audited the export, reproduced every false positive's gate
  verdict, and counter-checked the source workbook independently. Grilled and
  signed off.

### Phase 1 — recall + contained fixes

Baseline before: suite **472 passed**; audit workbook **204/206 (99.0%)**, 2 leaks
(`Alteryx`, `OpenClaw` — the two already recorded in `22443bf`), `verify_output`
passed.

Shipped:

1. **English people-column stems as a second, boundary-matched class**
   (`_NAME_HEADER_WORDS`). Substring matching stays for the German stems (one
   "leiter" must cover Projektleiter); the English ones are matched with a
   lowercase-letter boundary, because `_`-joined headers ("Rollout_Owner") defeat
   `\b` while a plain substring "owner" wrongly matches
   `Ownership_geklaert_Status`. **Chosen from measurement, not intuition:** the
   candidate list was run over the workbook first — 84 newly-claimed pairs, 83 of
   them real people, and the single false positive was exactly that
   `Ownership_geklaert_Status: 'Ausstehend'` (30×), which the boundary form removes.
   Only `owner`/`einreicher`/`lead`/`proxy` earn anything on this file; the rest are
   kept as they cost nothing measured here and close the same gap elsewhere.
2. **A weaker guess can no longer veto the header.** spaCy types some real names
   `NER_MISC`; that whole-cell MISC hit suppressed the override, and MISC — a bare
   guess — was then dropped outright by `corroboration_only`, so the name left in
   the clear from a column headed `Owner`. Now retyped IN PLACE to PERSON with
   `source=whole_cell_override` (adding a second finding would lose the overlap
   contest to MISC's flat 0.85 and change nothing). Found by chasing the one name
   the measurement still missed.
3. **URL detection.** Presidio's `UrlRecognizer` was already loaded — `URL` was
   simply not in the `entities` block, so `detect_unit` never requested it. Its
   loose non-scheme pattern turned out to produce **broken spans** (`d.ve` out of
   `d.velop`, `bank.de` out of an email address), which on apply would write
   `[LINK_1]lop`, so it is now REPLACED by scheme/`www.`-anchored patterns in
   `engine._URL_PATTERNS`. Those also exclude trailing sentence punctuation, which
   Presidio's did not.
4. **Excel `_xHHHH_` escapes neutralized**, same-length, inside
   `neutralize_structural_noise` — so offsets and therefore parity are untouched.
   Fixes both directions: the 7 empty cells flagged at the auto-accept tier, and
   the silent false negative where a name fused to an escape was rejected by
   `_is_structural_nonname`'s underscore rule.
5. **`DATE_TIME` snake_case rule.** Cause identified: the **English** model tags
   `MDX_PROXY_20`/`MDX_LEAD_51`/`PROJEKT_ID_37` as DATE at its flat 0.85, and
   DATE_TIME is not in `_NER_ENTITIES` so no filter applied at all.
6. **Possible-miss retune** — number shapes subtracted (never inclusion rules
   rewritten, so the audit workbook can prove nothing planted was lost), and the
   digit-run pattern widened to report `BP-26-001` whole instead of the tail
   `26-001`.
7. **Placeholder tokens excluded from the topical gazetteer** — a `Team` column
   held `team_1`..`team_5`, each learned as a DEPARTMENT and propagated
   document-wide. Guarded in the gazetteer (the path that actually produced them)
   as well as the whole-cell override.

**A constant I got wrong, caught by the suite:** the first possible-miss version
used a flat 8-digit floor for bare integers, which broke
`test_core.py::test_completeness_flags_unmatched_numbers_and_emails` — a 6-digit
contract number in prose must still surface. The test was protecting something
real, so the constant changed, not the test: a lone number in its own cell is a
quantity, the same digits inside prose are not.

Separately confirmed that a URL inside a description cell does NOT suppress the
whole-cell DESCRIPTION claim, which would have been a leak.

#### The exported CSV is NOT a valid baseline — and two conclusions drawn from it were wrong

`Malcom Werther` and `Ukom` appear in the exported CSV but are **not present in
`Downloads/mdx-big-beautiful-innovation-spreadsheet.xlsm` at all** (searched the
raw parts, not just the cell model). The export therefore came from a DIFFERENT
version of the workbook, and every before/after count taken against it is
confounded. Re-measured properly instead: the pre-Phase-1 commit and HEAD, both
scanning the identical file with the identical shipped config, in a git worktree.

| | before (22443bf) | after (34b23ad) |
|---|---|---|
| flagged values | 442 | **481** |
| flagged occurrences | 2723 | 2770 |
| PERSON | 200 | 203 |
| DESCRIPTION | 150 | 145 |
| URL | 0 | **41** |
| DATE_TIME | 32 | 32 |
| possible-miss rows | 107 | **74** |
| bare-guess values / occurrences | 194 / 1593 | **183 / 1560** |
| scan | 22.6s | 23.2s |

Two claims made against the CSV baseline and now retracted:

* **"PERSON went 96 → 203, so fixing the `_x001E_` escape unmasked a flood of
  German-noun noise."** Wrong. On this file PERSON is **200 → 203** — flat. The
  `Must-Haves` / `Datenfeeds` / `Kernworkflow` values were already being flagged
  before Phase 1; they were absent from the old CSV only because it is a different
  workbook. Phase 1 added no measurable noise.
* **"DESCRIPTION 237 → 145, of which 80 were snake_case field identifiers."** True
  of the old CSV, not of this file: the real change here is **150 → 145**.

What Phase 1 actually did to the totals: **the entire flagged increase is the 41
links** that previously left in the clear. Bare-guess values went DOWN (194 → 183)
and possible-misses fell by a third, so precision improved slightly rather than
degrading. The German-common-noun flood is untouched and remains Phase 3's job.

(The two names the check reports as missing, `Florian Brueckner` and
`Nils Braeunlich`, are transliteration artifacts in the probe list — the workbook
spells them `Brückner` and `Bräunlich`, and both ARE caught. `Constanza Hiemenz`
went missing → caught, confirming the NER_MISC retype.)
