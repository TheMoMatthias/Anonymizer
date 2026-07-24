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
        # Cheap text-level gate; the richer cell-level pre-filter + soft cap live
        # in the xlsx handler (Phase B). GLiNER does its OWN tokenization, so the
        # spaCy nlp_artifacts are intentionally unused here.
        if not text or len(text.strip()) < self._min_chars:
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


class _OnnxGlinerBackend:
    """Real ONNX-backed backend. Deterministic by construction: GLiNER runs a
    fixed-weight encoder in eval mode with no sampling, so identical input yields
    identical spans -- the property scan/apply parity relies on. NOTE: this path
    requires the model + onnxruntime present and is validated when the model is
    vendored into the bundle (a networked/packaging step); the unit tests cover
    the recognizer via an injected fake backend instead."""

    def __init__(self, model) -> None:
        self._model = model

    def predict(self, text: str, labels: list[str]) -> list[dict]:
        # gliner's predict_entities returns [{'text','label','start','end','score'}].
        # We pass threshold=0.0 and let the recognizer's min_score + detect_unit's
        # per-entity threshold/sensitivity gate uniformly (Round-3 decision).
        raw = self._model.predict_entities(text, labels, threshold=0.0)
        return [
            {"label": e["label"], "start": int(e["start"]), "end": int(e["end"]), "score": float(e["score"])}
            for e in raw
        ]


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
    try:
        model = GLiNER.from_pretrained(
            str(model_path),
            load_onnx_model=bool(gliner_cfg.get("onnx", True)),
            load_tokenizer=True,
        )
    except Exception as e:  # noqa: BLE001 -- surface any load error as a clear RuntimeError
        raise RuntimeError(
            f"ML detection (GLiNER) model at '{model_path}' failed to load: {e}. "
            f"Turn off ML detection in Settings to continue with reduced detection."
        ) from e
    return _OnnxGlinerBackend(model)
