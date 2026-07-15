"""Detection profiles: named presets that adapt default actions (and recall) to
a document type in one click, without hand-editing every entity.

A profile maps each data class to a default action and sets a global recall
offset (the sensitivity slider). Applying a profile rewrites the per-entity
`default_action` in the working config via the taxonomy mapping. "Balanced" is
the shipped default and changes nothing.
"""

from __future__ import annotations

from . import taxonomy

# data-class key -> action. sensitivity is the recall offset added to config.
PROFILES: dict[str, dict] = {
    "Balanced (default)": {},
    "Contracts": {
        "sensitivity": 0.0,
        "classes": {
            "people": "pseudonymize",
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
        "classes": {
            "people": "pseudonymize",
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
        "classes": {
            "people": "anonymize",
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
        "classes": {dc.key: "anonymize" for dc in taxonomy.DATA_CLASSES},
    },
}

PROFILE_NAMES = list(PROFILES)


def apply_profile(config: dict, name: str) -> dict:
    """Returns a NEW config with the profile's per-class actions + sensitivity
    applied. Balanced/unknown returns config unchanged (a copy)."""
    import copy

    cfg = copy.deepcopy(config)
    profile = PROFILES.get(name)
    if not profile or not profile.get("classes"):
        return cfg
    class_actions = profile["classes"]
    for entity_type, settings in cfg.get("entities", {}).items():
        action = class_actions.get(taxonomy.data_class_for(entity_type).key)
        if action:
            settings["default_action"] = action
    cfg["sensitivity"] = profile.get("sensitivity", cfg.get("sensitivity", 0.0))
    return cfg
