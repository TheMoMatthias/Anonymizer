from __future__ import annotations

import sqlite3
from pathlib import Path

import keyring
from cryptography.fernet import Fernet

from .config import app_data_dir

SERVICE = "anonymizer-mapping-db"
KEY_NAME = "key"

SCHEMA = """
CREATE TABLE IF NOT EXISTS mappings (
    entity_type TEXT NOT NULL,
    value_key TEXT NOT NULL,
    original_value TEXT NOT NULL,
    placeholder TEXT NOT NULL,
    PRIMARY KEY (entity_type, value_key)
);
"""


def _get_or_create_key() -> bytes:
    key = keyring.get_password(SERVICE, KEY_NAME)
    if not key:
        key = Fernet.generate_key().decode()
        keyring.set_password(SERVICE, KEY_NAME, key)
    return key.encode()


class MappingStore:
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or (app_data_dir() / "mappings.db")
        self._fernet = Fernet(_get_or_create_key())
        self.conn = sqlite3.connect(":memory:")
        self._load()

    def _load(self) -> None:
        if self.db_path.exists() and self.db_path.stat().st_size > 0:
            encrypted = self.db_path.read_bytes()
            sql_dump = self._fernet.decrypt(encrypted).decode("utf-8")
            self.conn.executescript(sql_dump)
        else:
            self.conn.executescript(SCHEMA)

    def save(self) -> None:
        dump = "\n".join(self.conn.iterdump())
        encrypted = self._fernet.encrypt(dump.encode("utf-8"))
        self.db_path.write_bytes(encrypted)

    def get_or_create(self, entity_type: str, value: str) -> str:
        key = value.strip().lower()
        row = self.conn.execute(
            "SELECT placeholder FROM mappings WHERE entity_type=? AND value_key=?",
            (entity_type, key),
        ).fetchone()
        if row:
            return row[0]
        count = self.conn.execute(
            "SELECT COUNT(*) FROM mappings WHERE entity_type=?", (entity_type,)
        ).fetchone()[0]
        placeholder = f"{entity_type}_{count + 1}"
        self.conn.execute(
            "INSERT INTO mappings(entity_type, value_key, original_value, placeholder) VALUES (?,?,?,?)",
            (entity_type, key, value, placeholder),
        )
        return placeholder

    def close(self) -> None:
        self.save()
        self.conn.close()

    def __enter__(self) -> "MappingStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
