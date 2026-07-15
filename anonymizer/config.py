from __future__ import annotations

import os
import shutil
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


def load_config() -> dict:
    path = user_config_path()
    if not path.exists():
        shutil.copy(DEFAULT_CONFIG_PATH, path)
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if _ensure_defaults(cfg):
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
    path = user_config_path()
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)


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
