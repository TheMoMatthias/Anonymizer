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


# --- profile-owned ML cutoffs -------------------------------------------------
#
# GLiNER confidence is governed by three interacting numbers. A reviewer moving one
# cannot tell what the effective behaviour becomes -- and the reviewer here is a
# colleague triaging documents, not someone tuning a model. So the profile owns them.


def _ml_cfg(**gliner):
    cfg = _cfg()
    cfg["gliner"] = {"enabled": False, "model_path": "gliner-model", "min_score": 0.3,
                     "confidence_override": 0.85, **gliner}
    return cfg


def test_profile_sets_the_ml_cutoffs():
    hr = profiles.apply_profile(_ml_cfg(), "HR documents")["gliner"]
    statements = profiles.apply_profile(_ml_cfg(), "Client statements")["gliner"]
    assert hr["min_score"] < statements["min_score"], (
        "HR is the Art.9 profile and must favour recall; client statements are "
        "checksum-covered and must favour precision"
    )


def test_balanced_leaves_the_shipped_cutoffs_alone():
    """"Balanced (default)" is documented as changing nothing."""
    cfg = _ml_cfg()
    assert profiles.apply_profile(cfg, "Balanced (default)")["gliner"] == cfg["gliner"]


def test_profile_ml_request_is_ignored_without_a_model(monkeypatch, tmp_path):
    """A profile may REQUEST ML, but a machine that never received the model pack
    must not be switched into the enabled-but-missing state that hard-fails every
    scan -- caused by a dropdown, on a document the operator just wanted to check."""
    monkeypatch.setenv("ANONYMIZER_GLINER_MODEL", str(tmp_path / "absent"))
    out = profiles.apply_profile(_ml_cfg(enabled=False), "HR documents")
    assert out["gliner"]["enabled"] is False


def test_profile_ml_request_is_honoured_when_the_model_is_there(monkeypatch, tmp_path):
    pack = tmp_path / "gliner-model"
    pack.mkdir()
    monkeypatch.setenv("ANONYMIZER_GLINER_MODEL", str(pack))
    monkeypatch.setattr("anonymizer.profiles.ml_available", lambda cfg: True)
    out = profiles.apply_profile(_ml_cfg(enabled=False), "HR documents")
    assert out["gliner"]["enabled"] is True


def test_profile_never_turns_ml_off(monkeypatch):
    """Silently disabling a capability the operator deliberately enabled is worse
    than leaving it on -- so profiles only ever request ML ON."""
    monkeypatch.setattr("anonymizer.profiles.ml_available", lambda cfg: False)
    out = profiles.apply_profile(_ml_cfg(enabled=True), "Contracts")
    assert out["gliner"]["enabled"] is True


def test_applied_profile_records_its_own_name():
    """detection_provenance reports which profile produced a document by reading
    cfg["profile"]. Nothing wrote that key, so every report claimed "Balanced
    (default)" whatever actually ran -- a provenance field that is always the same
    value is worse than no field, because it looks like evidence."""
    assert profiles.apply_profile(_cfg(), "HR documents")["profile"] == "HR documents"
    assert profiles.apply_profile(_cfg(), "Balanced (default)")["profile"] == "Balanced (default)"


def test_detection_keys_separate_what_is_found_from_what_is_done():
    """The review screen needs this split: an ACTION change can be re-applied to
    findings already on screen, a DETECTION change cannot."""
    assert profiles.detection_keys("HR documents") != profiles.detection_keys("Client statements")
    # Balanced sets no detection overrides at all, so it must not look like a change.
    assert profiles.detection_keys("Balanced (default)") == {}


def test_nrp_is_special_category_and_never_skipped():
    """GDPR Art. 9 data (NRP) must be its own high-sensitivity class, and the
    unknown-entity fallback must NOT be the profile-skippable dates bucket."""
    from anonymizer import profiles, taxonomy

    assert taxonomy.data_class_for("NRP").key == "special_category"
    assert taxonomy.data_class_for("SOME_CUSTOM_ENTITY").key == "other_entities"
    for name, prof in profiles.PROFILES.items():
        assert prof.get("classes", {}).get("special_category") != "skip", name
