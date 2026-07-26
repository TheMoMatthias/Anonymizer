"""Phase A tests for the GLiNER second-pass recognizer.

The real ONNX model is NOT installed in the test/dev environment (it is vendored
into the offline bundle on a connected machine). These tests therefore exercise
the full integration -- recognizer -> engine registration -> core.detect_unit ->
precision gate / confidence-override -> Finding -- through a DETERMINISTIC fake
backend injected via build_analyzer(config, gliner_backend=...). That is exactly
the seam the design put there so detection logic is testable with no ML deps.

See docs/run_gliner-integration_2026-07-24.md.
"""

from __future__ import annotations

import copy
import sys
import types

import pytest

from anonymizer import core
from anonymizer.config import DEFAULT_CONFIG_PATH
from anonymizer.engine import build_analyzer
from anonymizer.gliner_recognizer import (
    GLINER_SOURCE,
    PACK_CACHE_DIRNAME,
    GlinerRecognizer,
    load_gliner_backend,
    resolve_model_path,
)
from anonymizer.models import TextUnit

import yaml


class FakeBackend:
    """Deterministic GlinerBackend: emits a fixed set of (label, surface, score)
    spans wherever the surface occurs in the text. Substring-based so tests read
    naturally; deterministic so it stands in for the model's parity guarantee."""

    def __init__(self, spans):
        self._spans = list(spans)

    def predict(self, text, labels):
        out = []
        for label, surface, score in self._spans:
            if label not in labels:
                continue
            idx = text.find(surface)
            if idx >= 0:
                out.append({"label": label, "start": idx, "end": idx + len(surface), "score": score})
        return out


@pytest.fixture
def gliner_config():
    """Shipped config with GLiNER turned ON (shipped default is off until the
    model is bundled). Single-language (de) so detect_unit takes its normal
    single-language path."""
    cfg = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    cfg = copy.deepcopy(cfg)
    cfg["gliner"]["enabled"] = True
    cfg["languages"] = ["de"]
    # Isolate GLiNER behaviour from the free-text-NER corroboration filter.
    cfg["corroboration_only"] = False
    return cfg


def _detect(cfg, backend, text):
    analyzer = build_analyzer(cfg, gliner_backend=backend)
    unit = TextUnit(id="u1", text=text)
    return core.detect_unit(analyzer, unit, cfg)


def test_shipped_default_is_disabled():
    """Shipping enabled would hard-fail every scan (no model present). It must
    default off until Phase C vendors the model."""
    cfg = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    assert cfg["gliner"]["enabled"] is False


def test_disabled_registers_no_gliner(base_config):
    """With GLiNER off (shipped default) no recognizer is added and nothing about
    the existing pipeline changes."""
    analyzer = build_analyzer(base_config)
    names = {r.name for r in analyzer.registry.recognizers}
    assert GLINER_SOURCE not in names


def test_detects_person_org_location(gliner_config):
    """Phase-A checkpoint: person / organization / location flow through GLiNER
    into findings tagged source='gliner'."""
    backend = FakeBackend([
        ("person", "Ada Lovelace", 0.95),
        ("organization", "Analytical Engines GmbH", 0.9),
        ("location", "Karlsruhe", 0.88),
    ])
    text = "Ada Lovelace arbeitet bei Analytical Engines GmbH in Karlsruhe."
    findings = _detect(gliner_config, backend, text)
    by_val = {f.value: f for f in findings}
    assert "Ada Lovelace" in by_val and by_val["Ada Lovelace"].entity_type == "PERSON"
    assert "Analytical Engines GmbH" in by_val and by_val["Analytical Engines GmbH"].entity_type == "ORGANIZATION"
    assert "Karlsruhe" in by_val and by_val["Karlsruhe"].entity_type == "LOCATION"
    assert all(by_val[v].source == GLINER_SOURCE for v in ("Ada Lovelace", "Analytical Engines GmbH", "Karlsruhe"))


def test_detects_open_topical_types(gliner_config):
    """Open-ended labels (tool/project) -- the capability spaCy can't provide --
    are requested from analyze() only because GLiNER is enabled, and surface as
    TOOL/PROJECT findings."""
    backend = FakeBackend([
        ("tool", "DeepL Pro", 0.9),
        ("project", "Claudius", 0.9),
    ])
    text = "Wir nutzen DeepL Pro im Projekt Claudius."
    findings = _detect(gliner_config, backend, text)
    types = {f.value: f.entity_type for f in findings}
    assert types.get("DeepL Pro") == "TOOL"
    assert types.get("Claudius") == "PROJECT"


def test_confidence_override_keeps_high_conf_german_noun(gliner_config):
    """A high-confidence GLiNER hit bypasses the German-nominalization filter that
    would otherwise drop it (core._is_german_nominalization). 'Derivatefreiheit'
    is a nominalized common noun spaCy tags NOUN -- normally filtered, but if
    GLiNER strongly calls it an org/project it must survive."""
    text = "Das Konzept Derivatefreiheit wurde vorgestellt."
    high = FakeBackend([("organization", "Derivatefreiheit", 0.95)])
    findings = _detect(gliner_config, high, text)
    assert any(f.value == "Derivatefreiheit" for f in findings), "high-confidence hit must bypass the noun filter"


def test_low_confidence_still_filtered(gliner_config):
    """Below the override threshold, a GLiNER hit still runs the full precision
    gate and gets dropped -- 'filter everything uniformly' except the high-
    confidence override. Uses a structural non-name ('Feld_Name', snake_case) so
    the assertion is deterministic and independent of spaCy's POS verdict."""
    text = "Das Feld_Name Element steht hier."
    low = FakeBackend([("organization", "Feld_Name", 0.5)])  # < confidence_override 0.85
    findings = _detect(gliner_config, low, text)
    assert not any(f.value == "Feld_Name" for f in findings), "low-confidence structural hit must be filtered"
    # Same value at high confidence survives -- the override boundary is the ONLY
    # difference, isolating exactly what confidence_override controls.
    high = FakeBackend([("organization", "Feld_Name", 0.95)])
    findings_hi = _detect(gliner_config, high, text)
    assert any(f.value == "Feld_Name" and f.source == GLINER_SOURCE for f in findings_hi)


def test_deterministic_output(gliner_config):
    """detect_unit is the shared scan/apply path; the same input must yield
    identical findings across runs -- the property parity depends on."""
    backend = FakeBackend([("person", "Ada Lovelace", 0.95), ("tool", "DeepL Pro", 0.9)])
    text = "Ada Lovelace nutzt DeepL Pro."
    a = _detect(gliner_config, backend, text)
    b = _detect(gliner_config, backend, text)
    key = lambda fs: sorted((f.entity_type, f.value, f.start, f.end, f.source) for f in fs)
    assert key(a) == key(b)


def test_min_chars_gate(gliner_config):
    """A too-short text is skipped before the backend is consulted."""
    backend = FakeBackend([("person", "Ao", 0.99)])
    findings = _detect(gliner_config, backend, "Ao")  # len 2 < min_chars 3
    assert not any(f.source == GLINER_SOURCE for f in findings)


def test_min_score_gate(gliner_config):
    """A hit under min_score never becomes a finding."""
    backend = FakeBackend([("person", "Ada Lovelace", 0.1)])  # < min_score 0.3
    findings = _detect(gliner_config, backend, "Ada Lovelace ist hier.")
    assert not any(f.value == "Ada Lovelace" and f.source == GLINER_SOURCE for f in findings)


def test_non_gliner_source_still_filtered():
    """The confidence-override is GLiNER-only: a non-GLiNER finding never bypasses
    the gate, no matter its score. 'abdeckung' (lowercase) trips the lowercase
    filter, which short-circuits before any analyzer/POS lookup."""
    assert core._rejected_by_precision(
        "ORGANIZATION", "abdeckung", 0, len("abdeckung"), None, "de", None,
        source="propagation", score=0.99, trust_override=0.85,
    ) is True


def test_load_backend_missing_package_raises():
    """The hard-fail contract: enabled + runtime-absent raises an actionable
    RuntimeError (gliner is not installed in this environment)."""
    with pytest.raises(RuntimeError) as exc:
        load_gliner_backend({"model_path": "does-not-exist", "onnx": True})
    assert "ML detection" in str(exc.value)


def test_no_alpha_text_gated(gliner_config):
    """A cell with no alphabetic character (pure numbers/dates/punctuation) is
    skipped before the backend runs -- the stateless cheap gate."""
    backend = FakeBackend([("organization", "2026-07-24", 0.99)])
    findings = _detect(gliner_config, backend, "2026-07-24 / 12:30 -- 4,50")
    assert not any(f.source == GLINER_SOURCE for f in findings)


def test_language_agnostic_finds_english_term_in_german_config(gliner_config):
    """The config is narrowed to German (spaCy loads the de model for POS), yet
    GLiNER -- being multilingual and language-agnostic -- still catches an English
    tool name embedded in the German text. This is the 'German doc with English
    words' fix: GLiNER runs once over the whole text regardless of the detected
    language."""
    assert gliner_config["languages"] == ["de"]
    backend = FakeBackend([("tool", "DeepL Pro", 0.9)])
    text = "Die Abteilung nutzt das externe Werkzeug DeepL Pro für Übersetzungen."
    findings = _detect(gliner_config, backend, text)
    assert any(f.entity_type == "TOOL" and f.value == "DeepL Pro" and f.source == GLINER_SOURCE for f in findings)


def test_resolve_model_path_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ANONYMIZER_GLINER_MODEL", str(tmp_path / "m.onnx"))
    assert resolve_model_path({"model_path": "ignored"}) == tmp_path / "m.onnx"


def _grouped(cfg, backend, text):
    """The reviewer-facing GroupedFindings for a text, via the real detect path."""
    analyzer = build_analyzer(cfg, gliner_backend=backend)
    unit = TextUnit(id="u1", text=text)
    findings = core.detect_unit(analyzer, unit, cfg)
    return {g.value: g for g in core.build_scan_result(findings, [unit], cfg).all_actionable()}


def test_gliner_hit_is_a_third_state_not_a_ner_guess(gliner_config):
    """Provenance is three-valued. A GLiNER hit is neither a bare spaCy guess nor
    a rule-anchored match, and collapsing it into `is_ner_guess` would decide by
    accident -- via which side of a boolean it lands on -- a question that belongs
    to measurement: whether ML hits should face the corroboration-only drop."""
    backend = FakeBackend([("tool", "DeepL Pro", 0.9)])
    g = _grouped(gliner_config, backend, "Die Abteilung nutzt DeepL Pro taeglich.")["DeepL Pro"]
    assert g.is_ai_detected is True
    assert g.is_ner_guess is False


def test_non_gliner_finding_is_not_marked_ai_detected(gliner_config):
    """The flag must not leak onto rule-anchored findings that merely co-occur
    with an ML hit in the same unit."""
    backend = FakeBackend([("tool", "DeepL Pro", 0.9)])
    text = "DeepL Pro, IBAN DE89370400440532013000"
    groups = _grouped(gliner_config, backend, text)
    iban = next(g for v, g in groups.items() if "DE8937" in v)
    assert iban.is_ai_detected is False


def test_ml_sourced_art9_is_never_auto_accepted(gliner_config):
    """Art. 9 types default to ONE-WAY anonymize. An auto-applied zero-shot false
    positive therefore destroys legitimate content irreversibly -- and zero-shot
    confidence is not calibrated evidence that a sentence really discloses
    someone's health. A human confirms every one, however high it scored."""
    backend = FakeBackend([("health condition", "schwer erkrankt", 0.99)])
    g = _grouped(gliner_config, backend, "Der Kunde ist schwer erkrankt gewesen.")["schwer erkrankt"]
    assert g.entity_type == "DE_HEALTH_DATA"
    assert g.is_ai_detected is True
    assert g.tier != "high", "an ML Art.9 hit must never land in the auto-accept tier"
    assert g.action == "anonymize", "demoting the tier must not weaken the action"


def test_anchored_art9_can_still_auto_accept(analyzer, base_config):
    """The mirror requirement: the demotion is about ML PROVENANCE, not about
    Art. 9 generally. An anchored recognizer demanded a literal 'Diagnose:' label
    before matching, and must keep whatever tier its score earns."""
    unit = TextUnit("u1", "Diagnose: Diabetes mellitus Typ 2")
    cfg = {**base_config, "languages": ["de"], "tiers": {"high": 0.8, "medium": 0.5}}
    findings = core.detect_unit(analyzer, unit, cfg)
    art9 = [f for f in findings if f.entity_type == "DE_HEALTH_DATA"]
    assert art9, "the anchored Art.9 recognizer should still fire"
    groups = core.build_scan_result(findings, [unit], cfg).all_actionable()
    g = next(g for g in groups if g.entity_type == "DE_HEALTH_DATA")
    assert g.is_ai_detected is False
    assert g.tier == "high", "an anchored 0.86 hit under a 0.8 bar must keep its tier"


def _fake_gliner_module(record: dict):
    """A stand-in for the `gliner` package that records how from_pretrained was
    called. gliner is deliberately NOT a dependency of the test environment (the
    whole suite runs with no ML stack), so the loader's call contract can only be
    pinned by injecting the module."""
    mod = types.ModuleType("gliner")

    class _FakeModel:
        def eval(self):
            record["eval_called"] = True

        def predict_entities(self, text, labels, **kw):
            return []

    class _FakeGLiNER:
        @staticmethod
        def from_pretrained(path, **kwargs):
            record["path"] = path
            record["kwargs"] = kwargs
            return _FakeModel()

    mod.GLiNER = _FakeGLiNER
    return mod


def test_load_backend_never_reaches_the_network(monkeypatch, tmp_path):
    """AIR-GAP CONTRACT. Without local_files_only the hub lookup does not fail
    fast on a machine with no network -- it stalls on a connection timeout INSIDE
    the scan, which reads to the operator as a hung application rather than a
    missing file. Verified 2026-07-26 against the real pack under HF_HUB_OFFLINE=1."""
    pack = tmp_path / "gliner-model"
    pack.mkdir()
    record: dict = {}
    monkeypatch.setitem(sys.modules, "gliner", _fake_gliner_module(record))

    load_gliner_backend({"model_path": str(pack)})

    assert record["kwargs"]["local_files_only"] is True, "the loader may never reach the hub"
    assert record["eval_called"] is True, "eval mode is what makes inference deterministic"


def test_load_backend_uses_the_pack_local_hf_cache(monkeypatch, tmp_path):
    """GLiNER's Encoder resolves the BASE encoder config by its HUB ID
    ("microsoft/mdeberta-v3-base"), not from the pack -- so without a pack-local
    cache the very first scan on the air-gapped target dies looking for it. The
    cache is passed only when present, so a pack built before this convention
    still loads (and fails with the actionable message instead)."""
    pack = tmp_path / "gliner-model"
    (pack / PACK_CACHE_DIRNAME).mkdir(parents=True)
    record: dict = {}
    monkeypatch.setitem(sys.modules, "gliner", _fake_gliner_module(record))

    load_gliner_backend({"model_path": str(pack)})
    assert record["kwargs"]["cache_dir"] == str(pack / PACK_CACHE_DIRNAME)

    no_cache = tmp_path / "bare-model"
    no_cache.mkdir()
    record2: dict = {}
    monkeypatch.setitem(sys.modules, "gliner", _fake_gliner_module(record2))
    load_gliner_backend({"model_path": str(no_cache)})
    assert "cache_dir" not in record2["kwargs"]
