"""Offline GLiNER zero-shot NER as a Presidio recognizer (the *second-pass
recognizer*). See docs/run_gliner-integration_2026-07-24.md.

Design intent
-------------
GLiNER complements spaCy rather than replacing it: spaCy stays the POS backbone
the precision filters in ``core`` depend on; GLiNER takes over the NER role it
does markedly better (and adds open-ended topical labels -- tool/project/... --
supplied as plain text *at inference time*, no training or gazetteer).

Dependency isolation
--------------------
The heavy ML stack (onnxruntime + the quantised model) is imported LAZILY, only
inside :func:`load_gliner_backend`. Importing this module pulls in nothing beyond
presidio (already a dependency), so the whole package -- and the entire test
suite -- imports and runs with no ML dependency installed. Detection logic is
exercised in tests through an injected deterministic :class:`GlinerBackend`; the
real ONNX-backed backend is the only other implementation.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol, runtime_checkable

from presidio_analyzer import EntityRecognizer, RecognizerResult

# finding.source value for a GLiNER hit. The precision gate keys its
# confidence-override on this EXACT string (see core._rejected_by_precision), so
# it is defined here as the single source of truth and imported there.
GLINER_SOURCE = "gliner"


@runtime_checkable
class GlinerBackend(Protocol):
    """The minimal inference surface the recognizer needs -- deliberately tiny so
    a test can inject a deterministic fake and the real model is the only other
    implementer."""

    def predict(self, text: str, labels: list[str]) -> list[dict]:
        """Return a list of ``{'label', 'start', 'end', 'score'}`` dicts. ``label``
        is one of the GLiNER labels passed in (the human phrase, e.g. 'internal
        tool'), NOT the mapped Presidio entity type; the recognizer maps it."""
        ...


class GlinerRecognizer(EntityRecognizer):
    """Wraps a :class:`GlinerBackend` as a Presidio ``EntityRecognizer`` so its
    hits flow through the SAME overlap resolution, precision gate and propagation
    as every other recognizer (core.detect_unit). Registered per language by
    engine.build_analyzer, but the backend is language-agnostic -- in the normal
    single-language-narrowed scan it runs exactly once over the full text, which
    is what lets it catch an English tool name inside a German document."""

    def __init__(
        self,
        backend: GlinerBackend,
        label_map: dict[str, str],
        *,
        supported_language: str = "de",
        min_chars: int = 3,
        min_score: float = 0.3,
    ) -> None:
        # label_map: {gliner_label: ENTITY_TYPE}. Presidio filters results to the
        # `entities` requested per analyze() call, so supported_entities is the
        # set of mapped entity types.
        self._backend = backend
        self._label_map = dict(label_map)
        self._labels = list(self._label_map.keys())
        self._min_chars = int(min_chars)
        self._min_score = float(min_score)
        super().__init__(
            supported_entities=sorted(set(self._label_map.values())),
            supported_language=supported_language,
            name=GLINER_SOURCE,
        )

    def load(self) -> None:  # Presidio lifecycle hook; backend is already built.
        return None

    def analyze(self, text: str, entities, nlp_artifacts=None) -> list[RecognizerResult]:
        # Cheap, STATELESS text-level gate (parity-safe: a pure function of the
        # text). Skips empties, too-short strings, and text with no alphabetic
        # character at all (pure numbers/dates/punctuation) -- pointless to run a
        # name model on. A value-level gate that can see the cell value apart from
        # its header, plus the content-keyed soft cap, are xlsx-handler concerns
        # (they need cross-cell state computed identically in scan and apply to
        # preserve parity) and are deliberately NOT done here. GLiNER does its OWN
        # tokenization, so the spaCy nlp_artifacts are intentionally unused.
        if not text or len(text.strip()) < self._min_chars:
            return []
        if not any(ch.isalpha() for ch in text):
            return []
        results: list[RecognizerResult] = []
        for ent in self._backend.predict(text, self._labels):
            etype = self._label_map.get(ent.get("label"))
            if etype is None:
                continue
            if entities and etype not in entities:
                continue
            score = float(ent.get("score", 0.0))
            if score < self._min_score:
                continue
            try:
                start, end = int(ent["start"]), int(ent["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (0 <= start < end <= len(text)):
                continue
            results.append(
                RecognizerResult(
                    entity_type=etype,
                    start=start,
                    end=end,
                    score=score,
                    analysis_explanation=None,
                    recognition_metadata={
                        RecognizerResult.RECOGNIZER_NAME_KEY: GLINER_SOURCE,
                        RecognizerResult.RECOGNIZER_IDENTIFIER_KEY: getattr(self, "id", GLINER_SOURCE),
                    },
                )
            )
        return results


# A model pack ships its own tiny Hugging Face cache. GLiNER's Encoder resolves the
# BASE encoder config by its hub id -- `AutoConfig.from_pretrained("microsoft/
# mdeberta-v3-base")` in gliner/modeling/encoder.py -- which reaches the network on
# a machine that has none. It does forward `cache_dir`, so a pre-populated cache
# INSIDE the pack makes that call resolve locally with nothing patched and
# `gliner_config.json` left untouched, which keeps the pack relocatable (the bundle
# is copied to an arbitrary folder off a network share). ~4 MB: config + tokenizer
# only, never the base encoder's weights -- GLiNER carries its own fine-tuned ones.
PACK_CACHE_DIRNAME = "hf-cache"


def resolve_model_path(gliner_cfg: dict) -> Path:
    """Resolve the configured model path. Absolute paths are used verbatim; a
    relative path is resolved against the ANONYMIZER_GLINER_MODEL env var (set by
    the offline bundle launcher) or, failing that, a ``models/`` dir beside this
    package. Existence is NOT asserted here -- load_gliner_backend raises the
    actionable error so the failure text is in one place."""
    raw = str(gliner_cfg.get("model_path") or "").strip()
    p = Path(raw)
    if p.is_absolute():
        return p
    env = os.environ.get("ANONYMIZER_GLINER_MODEL")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent / "models" / raw


def prime_gliner(analyzer, texts) -> int:
    """Pre-compute ML predictions for `texts` in BATCHES, before detection walks
    them one at a time. Returns the number of texts primed (0 if ML is not active).

    Two wins, both measured, neither costing anything:

    * **Batching.** One call per text costs 126.9 ms; batches of 8-16 cost 52.9 ms --
      2.4x. (32 is SLOWER at 59.5 ms, so there is a sweet spot, not a monotonic win.)
      Verified byte-identical spans AND scores between the two paths, so this cannot
      change what is detected.
    * **Replay at apply.** The memo lives on the backend and the GUI caches ONE
      analyzer for the session (gui/app.py::_ensure_analyzer), so the predictions
      scan computed are still there when apply re-detects. Measured, apply spent
      167 s of a 297 s round trip re-inferring what scan had already computed.

    Replay also STRENGTHENS scan/apply parity rather than risking it: apply reuses
    scan's exact spans instead of re-deriving spans that are merely *argued* to be
    identical. (They are -- determinism is verified 5/5 -- but reusing beats arguing.)

    Callers MUST prime with the same text set in scan and in apply, which is why the
    xlsx handler does it right next to _precompute_cell_artifacts in both passes.
    """
    # Duck-typed on purpose: priming is called unconditionally from the scan/apply
    # paths, and those are exercised with stand-in analyzers (tests/test_hardening.py)
    # that implement only the surface they need. A missing registry means "no ML
    # here", never an error -- an optimisation must not be able to break a caller.
    primed = 0
    registry = getattr(analyzer, "registry", None)
    for rec in getattr(registry, "recognizers", None) or []:
        if getattr(rec, "name", None) != GLINER_SOURCE:
            continue
        prime = getattr(rec._backend, "prime", None)
        if prime is None:  # an injected test fake need not implement priming
            continue
        primed = max(primed, prime(list(texts), rec._labels))
    return primed


# Batch size for primed inference. Measured on this model: 8 and 16 are equal best
# (52.9 ms/text), 32 regresses to 59.5 ms. 16 keeps the number of calls low without
# entering that regression.
_PRIME_BATCH = 16
# Memo ceiling. Entries are small (a text plus a handful of span dicts), but a long
# session over many documents would otherwise grow without bound. Eviction only costs
# speed, never correctness: a miss re-infers, and inference is deterministic.
_MEMO_MAX = 50_000


class _OnnxGlinerBackend:
    """The real model-backed backend, wrapping whichever variant
    `GLiNER.from_pretrained` returned (`GLiNER` is a factory that swaps in
    `UniEncoderSpanGLiNER` and friends -- `predict_entities` lives on the returned
    instance, not on the factory class).

    Deterministic by construction: a fixed-weight encoder in eval mode with no
    sampling, so identical input yields identical spans -- the property scan/apply
    parity relies on. VERIFIED 2026-07-26 against the real vendored pack, loading
    fully offline (HF_HUB_OFFLINE=1): repeated inference over the same text
    produced byte-identical spans and scores.

    The name is historical -- it serves the plain safetensors path too, which is
    what actually ships (see the `onnx` key in default_recognizers.yaml). Unit
    tests drive the recognizer through an injected fake backend instead, so the
    suite still runs with no ML dependency installed."""

    def __init__(self, model) -> None:
        self._model = model
        # (text, labels) -> spans. Filled by prime() in batches and read by predict().
        # Keyed by TEXT ONLY (plus the label set): the model is multilingual and this
        # backend is language-agnostic by design -- the same string yields the same
        # spans on a German and an English sheet, which is the whole reason it can
        # catch an English tool name inside a German document.
        self._memo: dict[tuple[str, tuple[str, ...]], list[dict]] = {}

    @staticmethod
    def _normalize(raw) -> list[dict]:
        return [
            {"label": e["label"], "start": int(e["start"]), "end": int(e["end"]), "score": float(e["score"])}
            for e in raw
        ]

    def _remember(self, key, spans) -> None:
        if len(self._memo) >= _MEMO_MAX:
            # Drop the oldest quarter. dicts preserve insertion order, so this is FIFO.
            for stale in list(self._memo)[: _MEMO_MAX // 4]:
                del self._memo[stale]
        self._memo[key] = spans

    def prime(self, texts: list[str], labels: list[str]) -> int:
        """Batch-infer every text not already memoized. See prime_gliner."""
        key_labels = tuple(labels)
        todo = list(dict.fromkeys(t for t in texts if (t, key_labels) not in self._memo))
        for i in range(0, len(todo), _PRIME_BATCH):
            chunk = todo[i : i + _PRIME_BATCH]
            # threshold=0.0 for the same reason predict() uses it: gating belongs to
            # the recognizer's min_score and detect_unit, in ONE place.
            for text, raw in zip(chunk, self._model.batch_predict_entities(chunk, labels, threshold=0.0)):
                self._remember((text, key_labels), self._normalize(raw))
        return len(todo)

    def predict(self, text: str, labels: list[str]) -> list[dict]:
        # gliner's predict_entities returns [{'text','label','start','end','score'}].
        # We pass threshold=0.0 and let the recognizer's min_score + detect_unit's
        # per-entity threshold/sensitivity gate uniformly (Round-3 decision).
        key = (text, tuple(labels))
        hit = self._memo.get(key)
        if hit is not None:
            return hit
        spans = self._normalize(self._model.predict_entities(text, labels, threshold=0.0))
        self._remember(key, spans)
        return spans


def ml_available(gliner_cfg: dict) -> bool:
    """Whether an ML pass COULD run: the runtime is importable and a model pack is
    on disk. Cheap by construction -- an importlib spec check and a stat, never a
    model load -- so it is safe to call on every profile switch.

    This is what lets a detection profile *request* ML without being able to create
    the hard-fail dead-end: on a machine that never received the model pack, the
    request is simply not honoured and scanning continues on spaCy + gazetteer."""
    import importlib.util

    if importlib.util.find_spec("gliner") is None:
        return False
    try:
        return resolve_model_path(gliner_cfg).exists()
    except Exception:  # noqa: BLE001 -- an unresolvable path is simply "not available"
        return False


def load_gliner_backend(gliner_cfg: dict) -> GlinerBackend:
    """Construct the real ONNX GLiNER backend, importing the ML deps LAZILY. On
    any failure raises RuntimeError with an actionable message; the caller
    (engine.build_analyzer) lets it propagate so scanning HARD-FAILS with a hint
    to the Settings disable toggle -- the design's fail-loud-but-escapable
    contract, not a silent quality drop."""
    model_path = resolve_model_path(gliner_cfg)
    try:
        from gliner import GLiNER
    except ImportError as e:  # package missing
        raise RuntimeError(
            "ML detection (GLiNER) is enabled but the 'gliner' runtime is not "
            "installed. Use an offline bundle that ships it, or turn off ML "
            "detection in Settings to scan with spaCy + gazetteer only."
        ) from e
    if not model_path.exists():
        raise RuntimeError(
            f"ML detection (GLiNER) is enabled but the model was not found at "
            f"'{model_path}'. Restore the bundled model, set ANONYMIZER_GLINER_MODEL, "
            f"or turn off ML detection in Settings."
        )
    kwargs = {
        "load_onnx_model": bool(gliner_cfg.get("onnx", False)),
        "load_tokenizer": True,
        # NEVER reach the network. This ships air-gapped, where an un-pinned hub
        # lookup does not fail fast -- it stalls on a connection timeout inside the
        # scan, which reads to the operator as a hung application rather than a
        # missing file. Fail immediately with the message below instead.
        "local_files_only": True,
    }
    cache = model_path / PACK_CACHE_DIRNAME
    if cache.is_dir():
        kwargs["cache_dir"] = str(cache)
    try:
        model = GLiNER.from_pretrained(str(model_path), **kwargs)
    except Exception as e:  # noqa: BLE001 -- surface any load error as a clear RuntimeError
        raise RuntimeError(
            f"ML detection (GLiNER) model at '{model_path}' failed to load: {e}. "
            f"The model pack must contain the weights, the tokenizer files AND a "
            f"'{PACK_CACHE_DIRNAME}' folder holding the base encoder config -- see "
            f"docs/run_gliner-completion_2026-07-26.md. Turn off ML detection in "
            f"Settings to continue with reduced detection."
        ) from e
    model.eval()  # fixed weights, no dropout -- the determinism scan/apply parity needs
    return _OnnxGlinerBackend(model)
