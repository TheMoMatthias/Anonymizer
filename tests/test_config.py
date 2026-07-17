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
