"""The allow/deny lists can hold real customer PII (the review UI tells users to
add missed values to the deny list), so they must be encrypted at rest, never
sitting in plaintext config.yaml. `_isolate_keyring` (autouse) gives these tests
an in-memory Credential Manager.
"""

import yaml

from anonymizer import config as cfg_mod


def test_deny_list_encrypted_at_rest_and_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    cfg = cfg_mod.load_config()
    cfg["deny_list"] = ["Klaus Mueller"]
    cfg_mod.save_config(cfg)

    base = tmp_path / "Anonymizer"
    yaml_text = (base / "config.yaml").read_text(encoding="utf-8")
    assert "Klaus Mueller" not in yaml_text, "deny term must not be plaintext in config.yaml"
    assert "deny_list" not in yaml_text, "deny_list key must not persist in config.yaml"
    enc = (base / "lists.enc").read_bytes()
    assert b"Klaus Mueller" not in enc, "deny term must be encrypted (not plaintext) at rest"

    cfg2 = cfg_mod.load_config()
    assert "Klaus Mueller" in cfg2.get("deny_list", []), "deny term must round-trip through the encrypted store"


def test_plaintext_lists_are_migrated_out_of_config_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    base = tmp_path / "Anonymizer"
    base.mkdir(parents=True, exist_ok=True)
    # An OLD config.yaml with a plaintext deny_list must be migrated to the store.
    (base / "config.yaml").write_text(
        yaml.safe_dump({"deny_list": ["Petra Schmidt"], "sensitivity": 0}), encoding="utf-8"
    )

    cfg = cfg_mod.load_config()
    assert "Petra Schmidt" in cfg.get("deny_list", []), "existing deny term must survive migration"
    yaml_text = (base / "config.yaml").read_text(encoding="utf-8")
    assert "Petra Schmidt" not in yaml_text, "plaintext deny term must be migrated OUT of config.yaml"
    assert (base / "lists.enc").exists()


def _shipped() -> dict:
    return yaml.safe_load(cfg_mod.DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))


def _shipped_group(name: str) -> list[dict]:
    return [r for r in _shipped()["custom_recognizers"] if r["name"] == name]


def _aged(group: list[dict]) -> list[dict]:
    """A previously-shipped version of a recognizer group -- same name, older
    pattern -- i.e. what sits in the config.yaml of an already-deployed machine."""
    return [{**r, "patterns": [{"regex": r"\bOLD-VERSION\b", "score": 0.5}]} for r in group]


def test_every_shipped_recognizer_reaches_a_fresh_config():
    """Merge deduped by NAME, so the SECOND entry of a duplicate-named recognizer
    (DE_HEALTH_DATA and DE_UNION_PARTY each ship a case-sensitive twin) could
    never be merged at all -- half of the Art. 9 detection silently never
    arrived."""
    from collections import Counter

    cfg = {"custom_recognizers": [], "entities": {}}
    cfg_mod.merge_new_recognizers(cfg)
    assert Counter(r["name"] for r in cfg["custom_recognizers"]) == Counter(
        r["name"] for r in _shipped()["custom_recognizers"]
    )


def test_shipped_improvement_reaches_an_already_deployed_config():
    """DEPLOYMENT BLOCKER: merge never updated an existing entry by name, so an
    improved shipped pattern was IGNORED on any machine that already had a
    %LOCALAPPDATA%\\Anonymizer\\config.yaml -- which is every deployed one. An
    entry we installed and the user never touched must be upgraded."""
    name = "DE_SV_NUMMER"
    old = _aged(_shipped_group(name))
    cfg = {
        "custom_recognizers": [dict(r) for r in old],
        # provenance: this is exactly what WE installed, unmodified since
        cfg_mod.SHIPPED_STATE_KEY: {name: cfg_mod.recognizer_fingerprint(old)},
        "entities": {},
    }
    assert cfg_mod.merge_new_recognizers(cfg) > 0
    assert [r for r in cfg["custom_recognizers"] if r["name"] == name] == _shipped_group(name)


def test_user_edited_recognizer_is_never_overwritten():
    """The mirror requirement: a colleague's own tuning of a shipped recognizer
    (and any recognizer they added themselves) must survive an upgrade."""
    name = "DE_SV_NUMMER"
    edited = [{**r, "patterns": [{"regex": r"\bMY-OWN\b", "score": 0.9}]} for r in _shipped_group(name)]
    mine = {"name": "MY_ENTITY", "language": "de", "patterns": [{"regex": r"\bX\b", "score": 0.5}], "context": []}
    cfg = {
        "custom_recognizers": [*(dict(r) for r in edited), mine],
        # provenance records a DIFFERENT content -> the entry has been edited since
        cfg_mod.SHIPPED_STATE_KEY: {name: cfg_mod.recognizer_fingerprint(_aged(_shipped_group(name)))},
        "entities": {},
    }
    cfg_mod.merge_new_recognizers(cfg)
    assert [r for r in cfg["custom_recognizers"] if r["name"] == name] == edited
    assert mine in cfg["custom_recognizers"], "a user's own recognizer must never be dropped"


def test_legacy_entry_is_upgraded_only_when_it_matches_a_version_we_shipped(monkeypatch):
    """A config.yaml written before provenance existed carries no marker at all.
    It is adopted (and upgraded) ONLY if its content fingerprint matches a version
    this repo actually shipped; anything else is assumed to be a user edit and
    left alone. That is what lets the already-deployed machine catch up without
    ever guessing."""
    name = "DE_SV_NUMMER"
    old = _aged(_shipped_group(name))
    legacy_fp = cfg_mod.recognizer_fingerprint(old)

    def _cfg():
        return {"custom_recognizers": [dict(r) for r in old], "entities": {}}

    untouched = _cfg()
    cfg_mod.merge_new_recognizers(untouched)
    assert [r for r in untouched["custom_recognizers"] if r["name"] == name] == old, (
        "an unrecognized unmarked entry must be treated as a user edit"
    )

    monkeypatch.setattr(cfg_mod, "_LEGACY_SHIPPED_FINGERPRINTS", frozenset({legacy_fp}))
    upgraded = _cfg()
    cfg_mod.merge_new_recognizers(upgraded)
    assert [r for r in upgraded["custom_recognizers"] if r["name"] == name] == _shipped_group(name)


def test_upgrade_survives_a_save_load_round_trip(tmp_path, monkeypatch):
    """The provenance has to persist in config.yaml, or every launch would re-run
    the legacy adoption and an edit made in between would be overwritten."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    cfg = cfg_mod.load_config()
    assert cfg.get(cfg_mod.SHIPPED_STATE_KEY), "provenance not recorded on a fresh install"
    reloaded = cfg_mod.load_config()
    assert reloaded[cfg_mod.SHIPPED_STATE_KEY] == cfg[cfg_mod.SHIPPED_STATE_KEY]
    assert cfg_mod.merge_new_recognizers(reloaded) == 0, "a current config must need no further merge"


def test_schema_bump_resyncs_builtins_but_preserves_user_data(tmp_path, monkeypatch):
    """Regression: the additive-only merge left an existing config permanently
    stuck on first-run built-in values (e.g. NER_MISC 0.5 long after it shipped
    at 0.75). A schema bump must re-sync code-owned built-ins from shipped while
    preserving user-owned data (sensitivity, deny/allow lists, added recognizers)."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    base = tmp_path / "Anonymizer"
    base.mkdir(parents=True, exist_ok=True)
    # A stale pre-versioning config: old NER_MISC threshold, a user tweak, a user recognizer.
    stale = {
        "entities": {"NER_MISC": {"default_action": "pseudonymize", "confidence_threshold": 0.5}},
        "sensitivity": 0.12,
        "custom_recognizers": [
            {"name": "MY_OWN", "language": "de", "patterns": [{"regex": "X", "score": 0.9}], "context": []}
        ],
    }
    (base / "config.yaml").write_text(yaml.safe_dump(stale), encoding="utf-8")

    cfg = cfg_mod.load_config()
    # Code-owned built-in re-synced to shipped:
    assert cfg["entities"]["NER_MISC"]["confidence_threshold"] == 0.75
    assert cfg.get("config_schema_version", 0) >= 2
    # User-owned data preserved:
    assert cfg["sensitivity"] == 0.12
    assert any(r["name"] == "MY_OWN" for r in cfg["custom_recognizers"]), "user-added recognizer must survive"
    # Built-in recognizers pulled in with their current shipped definitions:
    assert any(r["name"] == "DE_ADDRESS" for r in cfg["custom_recognizers"])


def test_schema_resync_is_idempotent_after_first_run(tmp_path, monkeypatch):
    """Once migrated, a later user threshold tweak must NOT be reset on the next
    load (re-sync fires only on a version bump, not every load)."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    cfg = cfg_mod.load_config()  # first run -> at current schema version
    cfg["entities"]["NER_MISC"]["confidence_threshold"] = 0.61  # a user tweak, same version
    cfg_mod.save_config(cfg)

    cfg2 = cfg_mod.load_config()
    assert cfg2["entities"]["NER_MISC"]["confidence_threshold"] == 0.61, "tweak must survive when version unchanged"


# --- GLiNER labels are user-extendable ---------------------------------------
#
# A zero-shot label is plain text handed to the model at inference time. Adding
# "Mandant", or re-pointing a shipped label that is noisy in this domain, is meant
# to be a config edit rather than a code change -- so a schema bump must not eat it.


def _stale_gliner_cfg(tmp_path, labels: dict, installed: dict | None = None) -> None:
    """A pre-bump config whose gliner labels are `labels`, with `installed` as the
    recorded provenance (None = a config written before provenance existed)."""
    base = tmp_path / "Anonymizer"
    base.mkdir(parents=True, exist_ok=True)
    cfg = {"entities": {}, "gliner": {"enabled": True, "labels": labels}}
    if installed is not None:
        cfg[cfg_mod.GLINER_LABELS_STATE_KEY] = installed
    (base / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")


def _shipped_labels() -> dict:
    return yaml.safe_load(cfg_mod.DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))["gliner"]["labels"]


def test_user_added_gliner_label_survives_a_schema_bump(tmp_path, monkeypatch):
    """Adding a label is the entire point of a zero-shot model. It must never cost
    a code change, and must never be silently dropped on upgrade."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    shipped = _shipped_labels()
    _stale_gliner_cfg(tmp_path, {**shipped, "client reference": "MANDANT"}, installed=shipped)

    labels = cfg_mod.load_config()["gliner"]["labels"]
    assert labels["client reference"] == "MANDANT", "a user-added label was eaten by the bump"
    assert labels["person"] == shipped["person"], "shipped labels must still re-sync"


def test_user_edited_gliner_label_is_not_reverted(tmp_path, monkeypatch):
    """Re-pointing a noisy shipped label is legitimate local tuning -- the shipped
    'department' matches the ordinary German noun 'Abteilung'. Silently reverting
    that on upgrade is how a config stops being trustworthy."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    shipped = _shipped_labels()
    edited = {**shipped, "department": "OTHER_ENTITIES"}
    _stale_gliner_cfg(tmp_path, edited, installed=shipped)

    labels = cfg_mod.load_config()["gliner"]["labels"]
    assert labels["department"] == "OTHER_ENTITIES", "a deliberate user edit was reverted"


def test_untouched_gliner_label_is_resynced(tmp_path, monkeypatch):
    """The mirror requirement: an entry we installed and nobody touched must pick
    up a shipped fix, or the label map freezes on whatever first shipped."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    shipped = _shipped_labels()
    stale = {**shipped, "person": "WRONG_TYPE"}
    # provenance says WE installed WRONG_TYPE -> untouched -> safe to upgrade
    _stale_gliner_cfg(tmp_path, stale, installed=stale)

    assert cfg_mod.load_config()["gliner"]["labels"]["person"] == shipped["person"]


def test_provenance_is_recorded_on_a_fresh_install(tmp_path, monkeypatch):
    """A fresh config is a straight COPY of the shipped file. Without recording
    provenance on first run, a label edited before the first bump would look
    untouched and be reverted."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    cfg = cfg_mod.load_config()
    assert cfg.get(cfg_mod.GLINER_LABELS_STATE_KEY), "no label provenance recorded on a fresh install"
    assert cfg[cfg_mod.GLINER_LABELS_STATE_KEY] == cfg["gliner"]["labels"]


def test_undecryptable_lists_raises_and_is_not_overwritten(tmp_path, monkeypatch):
    """Regression (silent data loss): an undecryptable lists.enc used to be treated
    like 'absent' -> load/save overwrote it empty -> the deny list was permanently,
    silently lost (a leak). It must RAISE and leave the file intact instead."""
    import pytest

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    base = tmp_path / "Anonymizer"
    base.mkdir(parents=True, exist_ok=True)
    corrupt = b"not-a-valid-fernet-token-at-all"
    (base / "lists.enc").write_bytes(corrupt)

    with pytest.raises(RuntimeError):
        cfg_mod._load_secure_lists()
    assert (base / "lists.enc").read_bytes() == corrupt, "corrupt lists.enc must not be overwritten"
