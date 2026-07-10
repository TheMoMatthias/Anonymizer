from __future__ import annotations

import os
import shutil
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "default_recognizers.yaml"


def user_config_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Anonymizer"
    base.mkdir(parents=True, exist_ok=True)
    return base / "config.yaml"


def load_config() -> dict:
    path = user_config_path()
    if not path.exists():
        shutil.copy(DEFAULT_CONFIG_PATH, path)
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_config(config: dict) -> None:
    path = user_config_path()
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)


def app_data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Anonymizer"
    base.mkdir(parents=True, exist_ok=True)
    return base
