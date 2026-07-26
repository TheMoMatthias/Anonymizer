# Research memo — smarter *offline* sensitive-data detection

**Date:** 2026-07-24
**Question:** Is there a better fully-local ML approach to detect sensitive words / projects / names / tools than our current Presidio + spaCy `de_core_news_lg` pipeline — without sending any data off the machine?
**Answer (short):** **Yes — and one option (GLiNER) is almost purpose-built for our exact gap.** All options below run fully offline. This memo ranks them, verifies current facts (live-checked July 2026), and sketches integration. **No code was changed.**

---

## TL;DR ranked recommendation

| Rank | Approach | Solves | Offline | Bundle cost | Speed (CPU) | Fit for *tools/projects/descriptions* |
|------|----------|--------|---------|-------------|-------------|----------------------------------------|
| **1** | **GLiNER** (zero-shot NER) | prose names **+ arbitrary types** | ✅ | +~1.5–2.5 GB (torch) | ~75 ms/chunk (base) | **Excellent** — labels are runtime strings like `"internal tool"`, `"project name"` |
| 2 | **Flair `ner-german-large`** | prose names (PER/ORG/LOC) only | ✅ | +~1.5 GB | slow-ish | Poor — **fixed** label set, no zero-shot |
| 3 | **Embedding fuzzy-gazetteer** | typo/variant/semantic matches of *known* terms | ✅ | +~150 MB | fast | Good *complement*, not a detector on its own |
| ✗ | spaCy transformer `de_dep_news_trf` | — | ✅ | — | — | **Dead end: ships no NER component** |
| ✗ | Local LLM (Llama/Gemma) | everything, badly | ✅ | huge | very slow | Rejected earlier; also **over-redacts/falsifies text** in tests |

**Headline data point** (independent German benchmark, Durner, redacting German **organisation** names): **GLiNER 94.9%** vs **spaCy `de_core_news_lg` 78.0%** — and GLiNER beat a local Gemma-2-9B and Llama-3.1-8B too, at a fraction of the size. That is a ~17-point jump on exactly the entity class we struggle with, from an offline model.

**My recommendation:** add **GLiNER as an optional second-pass recognizer**, gated by a setting — *hybrid, not replacement*. Keep the fast deterministic layers (gazetteer, structural column detection, precision filters) as the cheap high-precision first pass; call the ML model only on the hard residual (prose names, tools, projects, "is this cell sensitive"). This is the only option that directly attacks *"sometimes we don't have names, just tools or descriptions"* — because with GLiNER the entity types are just strings you pass at runtime, no retraining, no gazetteer.

---

## The options in detail

### 1. GLiNER — Generalist zero-shot NER  ⭐ recommended
- **What it is:** a *small encoder* model (**not an LLM**) — 50 M (small) / 90 M (medium) / ~300 M (large) params — that does **zero-shot** NER. You hand it a list of labels **at inference time** (`["person", "internal tool", "project name", "software product", "department"]`) and it extracts spans for them. No fine-tuning, no gazetteer.
- **Why it fits us specifically:** our hardest problem is the open-ended stuff a fixed model was never trained on — internal tools ("Claudius", "DeepL Pro"), project code-names, sensitive free-text descriptions. GLiNER treats those as first-class because the label is just a string. This is the capability no amount of tuning our current spaCy model can give us.
- **German + mixed-language:** `urchade/gliner_multi_pii-v1` is fine-tuned for PII on **6 languages incl. German** (also EN/FR/ES/IT/PT). Being multilingual, **one pass handles a German doc peppered with English words** — dissolving our current "only one language gets scanned" problem without picking a language at all.
- **Ready-made PII variants:** `urchade/gliner_multi_pii-v1`, **NVIDIA `gliner-PII`**, and **GLiNER2-PII** (Fastino, 2025) are already tuned for PII labels out of the box.
- **GLiNER2** (July 2025): schema-driven, does **NER + text classification + structured extraction in one pass** — directly useful for our *cell-level* need ("classify this cell as sensitive/not **and** pull the sensitive spans" in a single call).
- **License:** **Apache 2.0** across the v2.1 models and GLiNER2 — commercial use fine.
- **Presidio integration exists:** there is a published **GLiNER recognizer plugin for Presidio**, so it slots into the recognizer registry we already use rather than being a rewrite. Measured ~**75 ms/chunk on CPU** for the base model.
- **Costs / risks (honest):**
  - **Bundle size:** pulls in `torch` + `transformers` + a 0.5–1.2 GB model → roughly **+1.5–2.5 GB** to the air-gapped bundle. This is the single biggest decision factor.
  - **Speed:** 75 ms/chunk is fine for prose but a big multi-sheet Excel is thousands of cells → **must** (a) run GLiNER only on cells that survive a cheap pre-filter, (b) reuse our existing per-cell memoization, (c) batch, and ideally (d) ship an **ONNX/quantised** build (supported) for a big CPU speedup.
  - **False positives:** zero-shot can over-flag → keep our *existing* precision filters as a **post-filter** on GLiNER output, plus a confidence threshold. Good news: that machinery already exists.
  - **Determinism:** output is stable for fixed weights; **pin the model version** in the bundle so results don't drift on upgrade.

### 2. Flair `ner-german-large`
- XLM-RoBERTa-large based, **F1 92.3 on CoNLL-03 German** vs ~85 self-reported for our spaCy model — a genuine precision upgrade **for names/orgs/locations**.
- **But:** fixed PER/ORG/LOC/MISC label set — **cannot** do zero-shot "internal tool"/"project". So it improves the *name* path but does nothing for the topical gap that motivated this research. Heavier and slower than GLiNER on CPU for no extra capability in our priority area. → **Consider only if we decide names-quality matters more than open-type detection.**

### 3. Embedding-based fuzzy gazetteer (complement)
- Small multilingual sentence-transformer (e.g. `paraphrase-multilingual-MiniLM`, ~120 MB) to match **typo'd / inflected / semantically-near** variants of terms already in our gazetteer — catches "DeepL-Pro", "Deepl pro", "the DeepL tool" when the exact string isn't present.
- Cheap and fully local, but it's an **enhancer for known terms**, not a discoverer of unknown ones. Pairs well under GLiNER, doesn't replace it.

### ✗ spaCy transformer `de_dep_news_trf`
- I checked specifically: the German transformer pipeline **does not ship an NER component** (Explosion only trains NER into `trf` pipelines where tagger+parser+NER share a dataset; German isn't one). So "just upgrade spaCy to the transformer" is **not** available for German without training our own head. Rule this out.

### ✗ Local LLM
- Already declined for offline/dependency reasons, and the independent German test corroborates the call: local Llama/Gemma **over-redact or falsify the input text**, and are far heavier/slower than GLiNER while scoring *lower* on the org-name task. Not worth revisiting.

---

## Recommended architecture (if we proceed) — *hybrid, gated*

```
Cell / text
   │
   ├─ 1. Deterministic first pass (unchanged, fast, high-precision)
   │      gazetteer + topical headers + structural column detection
   │      → confident hits short-circuit here (no ML needed)
   │
   ├─ 2. Cheap pre-filter: non-empty, has letters, not already resolved
   │      → decides which cells are even worth the ML pass
   │
   ├─ 3. GLiNER second pass (NEW, optional, setting-gated)
   │      labels = [person, org, location, internal tool,
   │                project name, department, sensitive description]
   │      → catches prose names + open-ended tools/projects
   │
   └─ 4. Our existing precision filters + confidence threshold as POST-filter
          → strips GLiNER over-flags (German nouns, acronyms, etc.)
```

Why hybrid: keeps the offline bundle usable for people who don't want the +2 GB (GLiNER stays an opt-in extra), keeps the common path fast, and adds ML muscle only where deterministic rules provably can't reach. It also reuses — rather than throws away — the precision work already shipped.

**Integration effort:** moderate. Presidio's recognizer-registry (already our architecture) + the existing GLiNER-Presidio plugin means this is an *additive recognizer*, not a rewrite. Main work is packaging torch+model into the offline bundle, the pre-filter/batching for Excel scale, and threshold tuning against our real `_flagged.csv`.

---

## Open decisions for the pre-implementation grill
1. **Bundle size:** is **+1.5–2.5 GB** acceptable for the air-gapped distribution, or must GLiNER be a separately-downloaded optional pack?
2. **Which GLiNER variant:** general `gliner_multi-v2.1` (flexible labels) vs PII-tuned `gliner_multi_pii-v1` / NVIDIA / GLiNER2-PII (sharper on personal data, less flexible on our custom "tool/project" labels)? Likely **GLiNER2** for the cell-classification+extraction combo.
3. **Scope of the ML pass:** all cells, or only cells flagged "possibly sensitive" by a cheap heuristic (for Excel-scale speed)?
4. **ONNX/quantised build** in the bundle for CPU speed — yes/no.
5. **Ship as default-on or opt-in** setting.
6. **Success criterion:** measured precision/recall on our real document vs today's ~445-flag baseline, at acceptable scan time.

## Sources
- GLiNER overview & sizes — [Zilliz blog](https://zilliz.com/blog/gliner-generalist-model-for-named-entity-recognition-using-bidirectional-transformer), [arXiv 2311.08526](https://arxiv.org/pdf/2311.08526)
- Multilingual / MoE — [GLiNER-MoE-MultiLingual (HF)](https://huggingface.co/Mayank6255/GLiNER-MoE-MultiLingual)
- PII variants & license (Apache 2.0, German) — [urchade/gliner_multi_pii-v1](https://huggingface.co/urchade/gliner_multi_pii-v1), [NVIDIA gliner-PII](https://huggingface.co/nvidia/gliner-PII), [GLiNER2-PII (Fastino)](https://pioneer.ai/blog/gliner2-pii-open-source-privacy-filtering-with-pii-detection), [gliner PyPI](https://pypi.org/project/gliner/0.2.5/)
- GLiNER2 multi-task — [arXiv 2507.18546](https://arxiv.org/html/2507.18546v1)
- German benchmark (GLiNER 94.9% vs spaCy 78.0%) — [Nils Durner, German NER: Presidio/spaCy/GLiNER](https://ndurner.github.io/ner)
- spaCy `de_dep_news_trf` has no NER — [spaCy models/de](https://spacy.io/models/de), [spaCy discussion #10929](https://github.com/explosion/spaCy/discussions/10929)
- Flair German F1 92.3 — [flair/ner-german-large (HF)](https://huggingface.co/flair/ner-german-large)
- Presidio + GLiNER integration — [Tom Aarsen / Presidio-GLiNER](https://www.linkedin.com/posts/tomaarsen_perform-high-quality-pii-filtering-using-activity-7175036436973289472-7zfd)
