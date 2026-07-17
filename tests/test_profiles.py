import copy

from anonymizer import profiles


def _cfg():
    return {
        "sensitivity": 0.0,
        "entities": {
            "PERSON": {"default_action": "pseudonymize"},
            "DE_STEUER_ID": {"default_action": "anonymize"},
            "IBAN_CODE": {"default_action": "pseudonymize"},
            "DATE_TIME": {"default_action": "pseudonymize"},
        },
    }


def test_balanced_is_noop():
    cfg = _cfg()
    out = profiles.apply_profile(cfg, "Balanced (default)")
    assert out["entities"] == cfg["entities"]


def test_profile_sets_actions_by_class_and_does_not_mutate_input():
    cfg = _cfg()
    original = copy.deepcopy(cfg)
    out = profiles.apply_profile(cfg, "HR documents")
    assert out["entities"]["PERSON"]["default_action"] == "anonymize"  # people -> anonymize in HR
    assert out["entities"]["DATE_TIME"]["default_action"] == "pseudonymize"
    assert out["sensitivity"] == 0.1
    assert cfg == original  # input untouched


def test_maximize_recall_anonymizes_everything():
    out = profiles.apply_profile(_cfg(), "Maximize recall (strip everything)")
    assert all(s["default_action"] == "anonymize" for s in out["entities"].values())
    assert out["sensitivity"] == 0.15


def test_nrp_is_special_category_and_never_skipped():
    """GDPR Art. 9 data (NRP) must be its own high-sensitivity class, and the
    unknown-entity fallback must NOT be the profile-skippable dates bucket."""
    from anonymizer import profiles, taxonomy

    assert taxonomy.data_class_for("NRP").key == "special_category"
    assert taxonomy.data_class_for("SOME_CUSTOM_ENTITY").key == "other_entities"
    for name, prof in profiles.PROFILES.items():
        assert prof.get("classes", {}).get("special_category") != "skip", name
