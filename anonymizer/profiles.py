"""Detection profiles: named presets that adapt default actions (and recall) to
a document type in one click, without hand-editing every entity.

A profile maps each data class to a default action, sets a global recall offset
(the sensitivity slider), and OWNS the ML detection cutoffs. "Balanced" is the
shipped default and changes nothing.

Why the profile owns the cutoffs
--------------------------------
GLiNER confidence is governed by three interacting numbers -- `min_score` (the
recognizer floor), `confidence_override` (the score at which a hit bypasses the
German-noun/POS filter), and the sensitivity offset. A reviewer moving one of them
cannot tell what the effective behaviour becomes, and the reviewer here is a bank
colleague triaging documents, not someone tuning a model. So the profile carries
them: you pick the kind of document you are working on, and the numbers follow.
The raw values stay reachable behind the Settings "Advanced" expander for tuning.

A profile may also REQUEST ML (`gliner.enabled: True`). The request is honoured
only when a model pack is actually installed -- see `apply_profile` -- so a profile
can never put a machine that never received the pack into the enabled-but-missing
state that hard-fails every scan. Profiles never turn ML OFF: silently disabling a
capability the operator deliberately enabled is worse than leaving it on.
"""

from __future__ import annotations

from . import taxonomy
from .gliner_recognizer import ml_available

# data-class key -> action. sensitivity is the recall offset added to config.
PROFILES: dict[str, dict] = {
    "Balanced (default)": {},
    "Contracts": {
        "sensitivity": 0.0,
        # Prose-heavy and name-dense, which is where the ML pass earns its keep;
        # but a contract is also the document you least want over-redacted, so the
        # floor sits above the shipped default.
        "gliner": {"enabled": True, "min_score": 0.5, "confidence_override": 0.9},
        "classes": {
            "people": "pseudonymize",
            # names spaCy couldn't classify (MISC) follow the people policy; Art. 9
            # special-category data is always one-way anonymized, never skipped.
            "other_entities": "pseudonymize",
            "special_category": "anonymize",
            "org_places": "pseudonymize",
            "contact": "pseudonymize",
            "financial_ids": "pseudonymize",
            "government_ids": "anonymize",
            "bank_internal": "pseudonymize",
            "dates_other": "skip",
        },
    },
    "Client statements": {
        "sensitivity": 0.05,
        # Structured and financial: the deterministic recognizers (IBAN, Konto,
        # Steuer-ID) already cover the identifying content with checksums, so the
        # ML pass is a backstop and is held to the highest bar of any profile.
        "gliner": {"enabled": True, "min_score": 0.6, "confidence_override": 0.9},
        "classes": {
            "people": "pseudonymize",
            "other_entities": "pseudonymize",
            "special_category": "anonymize",
            "org_places": "pseudonymize",
            "contact": "anonymize",
            "financial_ids": "anonymize",
            "government_ids": "anonymize",
            "bank_internal": "pseudonymize",
            "dates_other": "pseudonymize",
        },
    },
    "HR documents": {
        "sensitivity": 0.1,
        # The Art. 9 profile. German special-category detection is ANCHORED (it
        # needs a literal "Diagnose:"/"Konfession:" before the value), so it cannot
        # see the same disclosure made in prose -- exactly what a zero-shot model
        # can. Recall matters more than review volume here, so the floor is low.
        # Safe to be aggressive: an ML-sourced Art. 9 hit is never auto-applied
        # (core.build_scan_result), so a human sees every irreversible one.
        "gliner": {"enabled": True, "min_score": 0.35, "confidence_override": 0.8},
        "classes": {
            "people": "anonymize",
            "other_entities": "anonymize",
            "special_category": "anonymize",
            "org_places": "pseudonymize",
            "contact": "anonymize",
            "financial_ids": "anonymize",
            "government_ids": "anonymize",
            "bank_internal": "anonymize",
            "dates_other": "pseudonymize",
        },
    },
    "Maximize recall (strip everything)": {
        "sensitivity": 0.15,
        # Catch-everything mode: the lowest floor any profile sets, and the
        # override drops to the point where a strongly-detected German noun-shaped
        # name survives the POS filter. Review volume is the accepted cost.
        "gliner": {"enabled": True, "min_score": 0.25, "confidence_override": 0.7},
        "classes": {dc.key: "anonymize" for dc in taxonomy.DATA_CLASSES},
    },
}

PROFILE_NAMES = list(PROFILES)


def detection_keys(name: str) -> dict:
    """The profile's DETECTION settings only -- the ones that change what is found
    rather than what is done with it.

    The distinction drives the review screen: changing a default ACTION can be
    re-applied to findings already on screen, but changing detection cannot -- those
    findings came from a scan that used the old values. Switching a profile that
    changes these must therefore offer a re-scan rather than silently showing
    results that no longer match the settings."""
    profile = PROFILES.get(name) or {}
    keys = {}
    if "sensitivity" in profile:
        keys["sensitivity"] = profile["sensitivity"]
    if profile.get("gliner"):
        keys["gliner"] = dict(profile["gliner"])
    return keys


def apply_profile(config: dict, name: str) -> dict:
    """Returns a NEW config with the profile's per-class actions, sensitivity and
    ML cutoffs applied. Balanced/unknown returns config unchanged (a copy)."""
    import copy

    cfg = copy.deepcopy(config)
    profile = PROFILES.get(name)
    # Stamp the name even for an unknown/Balanced profile. detection_provenance
    # reads it to record WHICH profile produced a document, and without this it
    # read a key nobody ever wrote -- so every report claimed "Balanced (default)"
    # no matter what actually ran. A provenance field that is always the same value
    # is worse than no field: it looks like evidence.
    cfg["profile"] = name
    if not profile:
        return cfg

    gl = profile.get("gliner")
    if gl:
        merged = {**(cfg.get("gliner") or {}), **{k: v for k, v in gl.items() if k != "enabled"}}
        # A profile only ever REQUESTS ML on, and only when a pack is installed.
        # Requesting it on a machine that never received the pack would create the
        # enabled-but-missing state that hard-fails every scan -- caused by a
        # dropdown, on a document the operator just wanted to check. And a profile
        # never turns ML OFF: silently disabling a capability the operator
        # deliberately enabled is worse than leaving it on.
        if gl.get("enabled") and ml_available(merged):
            merged["enabled"] = True
        cfg["gliner"] = merged

    if profile.get("classes"):
        class_actions = profile["classes"]
        for entity_type, settings in cfg.get("entities", {}).items():
            action = class_actions.get(taxonomy.data_class_for(entity_type).key)
            if action:
                settings["default_action"] = action
    cfg["sensitivity"] = profile.get("sensitivity", cfg.get("sensitivity", 0.0))
    return cfg
