from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
# Lives inside the package (not the repo root) so it ships with the installed
# wheel regardless of where that ends up -- the offline bundle installs this
# package into a relocated standalone Python runtime, not a repo checkout.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "data" / "default_recognizers.yaml"


def user_config_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Anonymizer"
    base.mkdir(parents=True, exist_ok=True)
    return base / "config.yaml"


# The allow/deny lists can hold real customer PII -- the review UI tells users to
# add any sensitive string a recognizer missed to the deny list. They must NOT sit
# in plaintext config.yaml, so they are stored in a separate Fernet-encrypted file
# using the SAME Credential-Manager key as the mapping DB. In memory they stay as
# ordinary config keys, so detect_unit is unchanged.
_SECURE_LIST_KEYS = ("allow_list", "deny_list")


def _secure_lists_path() -> Path:
    return app_data_dir() / "lists.enc"


def _fernet():
    # Lazy import breaks the config<->mapping import cycle and reuses the mapping
    # DB's key so the lists share the same trust boundary (key in Credential
    # Manager, never in a document folder).
    from cryptography.fernet import Fernet

    from .mapping import _get_or_create_key

    return Fernet(_get_or_create_key())


def _load_secure_lists() -> dict:
    """The decrypted allow/deny lists, or {} if absent/unreadable. Falls back to
    the retained previous key (like the mapping store) so a key rotation can't
    strand the lists, re-encrypting under the current key when it does."""
    path = _secure_lists_path()
    if not path.exists() or path.stat().st_size == 0:
        return {}
    from cryptography.fernet import Fernet, InvalidToken

    blob = path.read_bytes()
    try:
        raw = _fernet().decrypt(blob)
    except InvalidToken:
        import keyring

        from .mapping import PREV_KEY_NAME, SERVICE

        prev = keyring.get_password(SERVICE, PREV_KEY_NAME)
        if not prev:
            return {}
        try:
            raw = Fernet(prev.encode()).decrypt(blob)
        except InvalidToken:
            return {}
        data = yaml.safe_load(raw.decode("utf-8")) or {}
        result = {k: list(data.get(k, [])) for k in _SECURE_LIST_KEYS}
        _save_secure_lists(result)  # re-encrypt under the current key
        return result
    data = yaml.safe_load(raw.decode("utf-8")) or {}
    return {k: list(data.get(k, [])) for k in _SECURE_LIST_KEYS}


def _save_secure_lists(config: dict) -> None:
    lists = {k: list(config.get(k, [])) for k in _SECURE_LIST_KEYS}
    blob = _fernet().encrypt(yaml.safe_dump(lists, allow_unicode=True).encode("utf-8"))
    path = _secure_lists_path()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_config() -> dict:
    path = user_config_path()
    if not path.exists():
        shutil.copy(DEFAULT_CONFIG_PATH, path)
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    # Migrate any plaintext allow/deny lists still sitting in config.yaml into the
    # encrypted store, then load the lists from there into the in-memory config.
    secure = _load_secure_lists()
    migrated = False
    for key in _SECURE_LIST_KEYS:
        if key in cfg:
            secure[key] = list(dict.fromkeys([*secure.get(key, []), *cfg.pop(key)]))
            migrated = True
    for key in _SECURE_LIST_KEYS:
        cfg[key] = secure.get(key, [])
    changed = _ensure_defaults(cfg)
    if migrated or changed:
        save_config(cfg)
    return cfg


def _ensure_defaults(cfg: dict) -> bool:
    """Additively upgrades an existing user config with any shipped defaults it
    is missing -- new top-level keys (tiers, sensitivity), new entities/custom
    recognizers, and new allow-list terms. NEVER overwrites a value the user
    already has (their customization wins). Returns True if anything changed."""
    shipped = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    changed = False

    for key in ("tiers", "sensitivity", "languages"):
        if key not in cfg and key in shipped:
            cfg[key] = shipped[key]
            changed = True

    if merge_new_recognizers(cfg) > 0:
        changed = True

    existing_allow = set(cfg.get("allow_list", []))
    new_allow = [a for a in shipped.get("allow_list", []) if a not in existing_allow]
    if new_allow:
        cfg.setdefault("allow_list", []).extend(new_allow)
        changed = True

    return changed


def save_config(config: dict) -> None:
    """Atomic: serialize to a sibling temp then os.replace, so a crash or a
    second instance can never leave a half-written config.yaml that
    yaml.safe_load then chokes on (or silently reads short). The allow/deny lists
    are written to the ENCRYPTED sidecar and stripped from the plaintext yaml."""
    _save_secure_lists(config)
    path = user_config_path()
    to_write = {k: v for k, v in config.items() if k not in _SECURE_LIST_KEYS}
    blob = yaml.safe_dump(to_write, allow_unicode=True, sort_keys=False)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(blob)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def merge_new_recognizers(cfg: dict) -> int:
    """Adds any shipped custom recognizer (by name) not already present in
    cfg. Never touches an existing entry, even if the shipped version has
    since changed -- a colleague's own customization always wins."""
    shipped = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    existing_names = {r["name"] for r in cfg.get("custom_recognizers", [])}
    added = 0
    for rec in shipped.get("custom_recognizers", []):
        if rec["name"] not in existing_names:
            cfg.setdefault("custom_recognizers", []).append(rec)
            existing_names.add(rec["name"])
            added += 1
    for entity_type, settings in shipped.get("entities", {}).items():
        if entity_type not in cfg.get("entities", {}):
            cfg.setdefault("entities", {})[entity_type] = settings
            added += 1
    return added


def app_data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Anonymizer"
    base.mkdir(parents=True, exist_ok=True)
    return base
