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
