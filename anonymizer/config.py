from __future__ import annotations

import hashlib
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
        raw = None
        if prev:
            try:
                raw = Fernet(prev.encode()).decrypt(blob)
            except InvalidToken:
                raw = None
        if raw is None:
            # PRESENT but undecryptable by the current OR previous key. Do NOT treat
            # this as "absent" and return {}: load_config/save_config would then
            # OVERWRITE lists.enc with an empty blob and PERMANENTLY drop the user's
            # deny list -- silently turning off must-redact terms (a leak). Fail loud
            # instead, exactly like MappingStore._decrypt does for the mapping DB.
            raise RuntimeError(
                "The encrypted allow/deny lists (lists.enc) exist but cannot be "
                "decrypted with the current or previous key. Refusing to overwrite "
                "them, which would lose the deny list. Restore the Credential Manager "
                "key, or delete lists.enc to start the lists over."
            )
        # Decrypted under the previous key (post-rotation) -> re-encrypt under current.
        data = yaml.safe_load(raw.decode("utf-8")) or {}
        result = {k: list(data.get(k, [])) for k in _SECURE_LIST_KEYS}
        _save_secure_lists(result)
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
    """Upgrades an existing user config to the shipped defaults: new top-level
    keys (tiers, sensitivity), new allow-list terms, new entities, and new OR
    improved shipped recognizers (see merge_new_recognizers -- a recognizer the
    user has edited is never touched). Returns True if anything changed."""
    shipped = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    changed = False

    for key in ("tiers", "sensitivity", "languages", "corroboration_only", "topical", "gliner"):
        if key not in cfg and key in shipped:
            cfg[key] = shipped[key]
            changed = True

    # The provenance record can change without any recognizer content changing
    # (first run after the upgrade path landed); that still has to be SAVED, or
    # every launch would re-run the legacy adoption from scratch.
    state_before = dict(cfg.get(SHIPPED_STATE_KEY, {}))
    if merge_new_recognizers(cfg) > 0 or cfg.get(SHIPPED_STATE_KEY, {}) != state_before:
        changed = True

    # Record the GLiNER labels currently in the config as "what we installed", so a
    # later schema bump can tell a user's edit from an untouched shipped entry.
    # Written here rather than only at bump time because a fresh config is a
    # straight COPY of the shipped file -- without this, a label edited before the
    # first bump would look untouched and be silently reverted. The CURRENT labels
    # are recorded, not the shipped ones: on a config predating this record, its
    # own (possibly older) labels are the honest baseline for "unmodified".
    if GLINER_LABELS_STATE_KEY not in cfg and (cfg.get("gliner") or {}).get("labels"):
        cfg[GLINER_LABELS_STATE_KEY] = dict(cfg["gliner"]["labels"])
        changed = True

    existing_allow = set(cfg.get("allow_list", []))
    new_allow = [a for a in shipped.get("allow_list", []) if a not in existing_allow]
    if new_allow:
        cfg.setdefault("allow_list", []).extend(new_allow)
        changed = True

    if _resync_builtins(cfg, shipped):
        changed = True

    return changed


def _merge_gliner_labels(cfg: dict, shipped_labels: dict) -> dict:
    """The GLiNER label map after a schema bump: shipped labels re-synced, the
    user's own work kept.

    Three cases, decided by the provenance record (GLINER_LABELS_STATE_KEY):
      * a shipped label the user never touched  -> take the shipped value, so a
        wording fix or a re-pointed entity type actually reaches existing installs;
      * a shipped label the user CHANGED        -> keep theirs. Re-pointing a noisy
        label (the shipped "department" matches the ordinary German noun
        "Abteilung") is a legitimate local tuning decision, and silently reverting
        it on upgrade is how a config stops being trustworthy;
      * a label the user ADDED                  -> keep it. Adding a label is the
        entire point of a zero-shot model and must never cost a code change.

    With no provenance recorded (a config written before this existed) every
    shipped label is treated as untouched, matching how the recognizer upgrade
    path treats an unmarked legacy entry that still matches what we shipped."""
    user_labels = dict((cfg.get("gliner") or {}).get("labels") or {})
    installed = dict(cfg.get(GLINER_LABELS_STATE_KEY) or {})
    merged = dict(user_labels)
    for label, entity in shipped_labels.items():
        untouched = label not in user_labels or user_labels[label] == installed.get(label, user_labels[label])
        if untouched:
            merged[label] = entity
    return merged


def _resync_builtins(cfg: dict, shipped: dict) -> bool:
    """One-time (per schema bump) re-sync of CODE-OWNED built-in definitions from
    the shipped default: entity thresholds/default-actions, built-in recognizer
    regexes, and tier boundaries. The additive-only merge above never overwrites
    an existing value, which meant a shipped improvement (e.g. NER_MISC 0.5 ->
    0.75, or a DE_ADDRESS regex fix) never reached a config created before it.
    This closes that gap without clobbering USER-OWNED data every load: it runs
    only when the user's config_schema_version is behind shipped, then records
    the new version so a user's later tweaks survive until the next bump.

    Preserved (user-owned): allow_list, deny_list, sensitivity,
    name_column_headers, and any custom_recognizers the user ADDED (a name not
    in the shipped set). Reset to shipped (code-owned): built-in entities,
    built-in recognizers (by name), tiers."""
    shipped_ver = int(shipped.get("config_schema_version", 1))
    if int(cfg.get("config_schema_version", 1)) >= shipped_ver:
        return False

    shipped_entities = shipped.get("entities", {})
    if shipped_entities:
        # shipped built-ins win; keep any user-added entity types not shipped.
        user_extra = {k: v for k, v in cfg.get("entities", {}).items() if k not in shipped_entities}
        cfg["entities"] = {**shipped_entities, **user_extra}

    if "tiers" in shipped:
        cfg["tiers"] = shipped["tiers"]

    # Re-sync recognizers by NAME GROUP, never entry-by-name.
    #
    # A recognizer name is deliberately NOT unique: one entity type is emitted by
    # several entries -- a GERMAN word list and an ENGLISH one (DE_RELIGION ships
    # 1 de + 2 en), plus case-sensitive twins for DE_HEALTH_DATA / DE_UNION_PARTY.
    # Keying a dict by name collapsed the shipped 27 entries to 17 and, because the
    # `en` variants sit last in the file, rewrote every GERMAN Art. 9 word list as
    # its English counterpart. Measured consequence: DE_RELIGION, DE_HEALTH_DATA,
    # DE_UNION_PARTY, DE_SEX_LIFE and NRP all ended up registered for `en` ONLY, so
    # GDPR Art. 9 detection did not run on German documents at all -- the exact leak
    # 356337c fixed in the other direction, re-introduced here on any schema bump.
    #
    # merge_new_recognizers() already got this right (see recognizer_fingerprint,
    # which fingerprints the GROUP for precisely this reason); this second path had
    # been left behind. Both now agree, and the provenance state is refreshed here
    # too -- otherwise the next merge_new_recognizers() would compare the group we
    # just installed against a stale fingerprint, read it as a user edit, and
    # refuse to ever upgrade it again.
    shipped_groups = _group_by_name(shipped.get("custom_recognizers", []))
    if shipped_groups:
        merged: list[dict] = []
        placed: set[str] = set()
        for rec in cfg.get("custom_recognizers", []):
            name = rec["name"]
            if name not in shipped_groups:
                merged.append(rec)  # user-ADDED recognizer: never touched
                continue
            if name not in placed:  # insert the whole shipped group at its position
                placed.add(name)
                merged.extend(dict(r) for r in shipped_groups[name])
        for name, group in shipped_groups.items():
            if name not in placed:
                merged.extend(dict(r) for r in group)
        cfg["custom_recognizers"] = merged
        state = cfg.setdefault(SHIPPED_STATE_KEY, {})
        for name, group in shipped_groups.items():
            state[name] = recognizer_fingerprint(group)

    # Topical block: re-sync the CODE-OWNED parts (which categories exist + their
    # header_terms) so a shipped category added after the user's config was
    # created (e.g. DESCRIPTION) actually reaches them -- while PRESERVING the
    # user's manual per-category `terms` and their `enabled` choice. Without this
    # the topical block, once created, stayed frozen on its first-seen categories.
    shipped_topical = shipped.get("topical")
    if shipped_topical:
        user_topical = cfg.get("topical") or {}
        user_cats = user_topical.get("categories") or {}
        merged_cats: dict = {}
        for cat, spec in (shipped_topical.get("categories") or {}).items():
            m = dict(spec)  # shipped header_terms win (code-owned)
            m["terms"] = (user_cats.get(cat) or {}).get("terms") or spec.get("terms", [])
            merged_cats[cat] = m
        for cat, spec in user_cats.items():  # keep any user-ADDED categories
            merged_cats.setdefault(cat, spec)
        cfg["topical"] = {
            **shipped_topical,
            "enabled": user_topical.get("enabled", shipped_topical.get("enabled", True)),
            "categories": merged_cats,
        }

    # GLiNER block: everything is code-owned (labels, model_path, thresholds,
    # caps) EXCEPT the user's `enabled` toggle -- the one GLiNER setting the UI
    # exposes. Re-syncing the rest means a shipped label-map fix or a new default
    # reaches existing configs, while a user who turned ML detection off (or on)
    # keeps that choice across the bump.
    shipped_gliner = shipped.get("gliner")
    if shipped_gliner:
        user_gliner = cfg.get("gliner") or {}
        cfg["gliner"] = {
            **shipped_gliner,
            "enabled": user_gliner.get("enabled", shipped_gliner.get("enabled", False)),
            "labels": _merge_gliner_labels(cfg, shipped_gliner.get("labels") or {}),
        }
        cfg[GLINER_LABELS_STATE_KEY] = dict(shipped_gliner.get("labels") or {})

    cfg["config_schema_version"] = shipped_ver
    return True


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


# --- shipped-recognizer upgrade path ----------------------------------------
# The problem this solves: a detection improvement that only ever lands in the
# SHIPPED yaml never reaches a machine that already has a config.yaml -- i.e.
# every deployed one. Blind overwrite is not an option either: the Settings tab
# lets a colleague retune a shipped recognizer, and losing that silently is just
# as bad. So each shipped recognizer group carries PROVENANCE: the fingerprint of
# the definition we last installed. Content == recorded fingerprint means "we put
# it there and nobody has touched it" -> safe to upgrade. Anything else is a user
# edit and is left exactly as it is.
SHIPPED_STATE_KEY = "shipped_recognizers"

# The same provenance idea, for the GLiNER label map: the label->entity mapping we
# last installed. A zero-shot label is just plain text handed to the model at
# inference time, so adding "Mandant" or "Vertragsnummer", or re-pointing a noisy
# shipped label, is meant to be a config edit rather than a code change -- and it
# must not be silently eaten by the next schema bump. Recording what WE installed
# is what lets a re-sync tell "nobody touched this, safe to upgrade" from "the user
# changed it deliberately, leave it alone".
GLINER_LABELS_STATE_KEY = "shipped_gliner_labels"

# Configs written BEFORE provenance existed carry no marker. They are adopted
# (and upgraded) only if their content matches a definition this repo actually
# shipped -- these are the fingerprints of every custom-recognizer name group in
# the git history of default_recognizers.yaml, so an untouched old install is
# recognised while an edited one is not. This table is a ONE-TIME migration aid
# and never needs to grow: from this version on every install records its own
# provenance in SHIPPED_STATE_KEY. (Regenerate with the same fingerprint function
# over `git log --follow` of the yaml if it ever does need extending.)
_LEGACY_SHIPPED_FINGERPRINTS = frozenset({
    "612abbb4e9d5007e", "f1ae5575052d7c5d",  # BANK_INTERNAL_REF
    "5f605f9a776842a2", "e77fc41eace79aa8",  # BIC_CODE
    "682472c0a361d498",  # DATE_TIME
    "db4f735304e73761", "e752006af4313edd",  # DE_ADDRESS
    "07ca7620c01f37a2", "a18f54c3e72c78ba", "db7d7b5586dc7fff",  # DE_DEPOTNUMMER
    "1846f5e06ed8af08", "695ce4a307149f22", "b93d6bf6f8fa8d58",  # DE_KONTONUMMER
    "0c6192e54630fe2f", "2441faf78ee02b26",  # DE_KUNDENNUMMER
    "3e600a14afb82421",  # DE_PHONE
    "4bbfc707d5c7e6a4", "a18841b52657e530",  # DE_STEUER_ID
    "d1cccfc8e28d9e58", "fb39f0f2f868c484",  # DE_SV_NUMMER
})


def recognizer_fingerprint(group: list[dict]) -> str:
    """Content fingerprint of all recognizer entries sharing one name. Fingerprints
    the GROUP, not a single entry: DE_HEALTH_DATA and DE_UNION_PARTY each ship a
    case-sensitive twin under the same name, and the previous dedupe-by-name meant
    the second one could never be merged at all."""
    blob = yaml.safe_dump(group, sort_keys=True, allow_unicode=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _group_by_name(recognizers: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for rec in recognizers:
        groups.setdefault(rec["name"], []).append(rec)
    return groups


def merge_new_recognizers(cfg: dict) -> int:
    """Brings the user's config up to date with the shipped recognizers.

    Adds anything missing, UPGRADES a shipped group the user has not modified,
    and leaves every user edit (and every recognizer they added themselves)
    untouched. Returns the number of recognizer entries added or updated, plus
    newly added entity settings.

    Entity SETTINGS (default_action / confidence_threshold) are only ever added
    when missing, never updated: those are the knobs the Settings tab exists to
    turn, so an existing value is by definition the user's decision."""
    shipped = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    user_recognizers = cfg.setdefault("custom_recognizers", [])
    state = cfg.setdefault(SHIPPED_STATE_KEY, {})
    user_groups = _group_by_name(user_recognizers)
    changed = 0

    for name, shipped_group in _group_by_name(shipped.get("custom_recognizers", [])).items():
        shipped_fp = recognizer_fingerprint(shipped_group)
        current = user_groups.get(name)
        if not current:
            user_recognizers.extend(shipped_group)
            state[name] = shipped_fp
            changed += len(shipped_group)
            continue
        current_fp = recognizer_fingerprint(current)
        if current_fp == shipped_fp:
            state[name] = shipped_fp  # already current -- just record provenance
            continue
        recorded = state.get(name)
        untouched = (
            recorded == current_fp if recorded is not None else current_fp in _LEGACY_SHIPPED_FINGERPRINTS
        )
        if not untouched:
            continue  # the user's own version -- theirs always wins
        # Replace the whole group in place, keeping its position in the list so the
        # Settings tab does not reshuffle under the user.
        at = user_recognizers.index(current[0])
        for entry in current:
            user_recognizers.remove(entry)
        user_recognizers[at:at] = shipped_group
        state[name] = shipped_fp
        changed += len(shipped_group)

    for entity_type, settings in shipped.get("entities", {}).items():
        if entity_type not in cfg.get("entities", {}):
            cfg.setdefault("entities", {})[entity_type] = settings
            changed += 1
    return changed


def app_data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Anonymizer"
    base.mkdir(parents=True, exist_ok=True)
    return base
