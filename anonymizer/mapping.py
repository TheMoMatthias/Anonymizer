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
    """Encrypted, per-colleague pseudonym table. Holds the reversible mapping
    from an original value to its consistent placeholder (e.g. PERSON_1). The
    on-disk file is Fernet-encrypted; the key lives in Windows Credential
    Manager, never in a document folder."""

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

    def get_or_create(self, entity_type: str, value: str, label: str | None = None) -> str:
        """Returns the stable placeholder for (entity_type, value), creating one
        the first time. `label` controls the token prefix (e.g. 'IBAN'); it
        defaults to the entity type. Placeholder numbering is per-label so the
        same real value always renders to the same token across documents."""
        key = value.strip().lower()
        row = self.conn.execute(
            "SELECT placeholder FROM mappings WHERE entity_type=? AND value_key=?",
            (entity_type, key),
        ).fetchone()
        if row:
            return row[0]
        prefix = label or entity_type
        count = self.conn.execute(
            "SELECT COUNT(*) FROM mappings WHERE placeholder GLOB ?", (f"{prefix}_*",)
        ).fetchone()[0]
        placeholder = f"{prefix}_{count + 1}"
        self.conn.execute(
            "INSERT INTO mappings(entity_type, value_key, original_value, placeholder) VALUES (?,?,?,?)",
            (entity_type, key, value, placeholder),
        )
        return placeholder

    def reverse(self, placeholder: str) -> str | None:
        """Original value for a placeholder token (for re-identification), or
        None if unknown. `placeholder` is the inner token without brackets."""
        row = self.conn.execute(
            "SELECT original_value FROM mappings WHERE placeholder=?", (placeholder,)
        ).fetchone()
        return row[0] if row else None

    def entry_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM mappings").fetchone()[0]

    def all_entries(self) -> list[tuple[str, str, str]]:
        """(entity_type, placeholder, original_value) for every mapping. This is
        the sensitive re-identification set -- callers must guard its export."""
        return [
            (et, ph, orig)
            for et, ph, orig in self.conn.execute(
                "SELECT entity_type, placeholder, original_value FROM mappings ORDER BY placeholder"
            )
        ]

    def erase(self, placeholder: str) -> bool:
        """Deletes a single mapping (data-subject erasure). Returns True if a
        row was removed."""
        cur = self.conn.execute("DELETE FROM mappings WHERE placeholder=?", (placeholder,))
        return cur.rowcount > 0

    def reset(self) -> None:
        """Wipes every mapping. Placeholder numbering restarts from 1; existing
        anonymized documents can no longer be re-identified."""
        self.conn.execute("DELETE FROM mappings")

    def rotate_key(self) -> None:
        """Generates a fresh encryption key and re-saves under it. Old on-disk
        copies of the file become undecryptable."""
        new_key = Fernet.generate_key()
        keyring.set_password(SERVICE, KEY_NAME, new_key.decode())
        self._fernet = Fernet(new_key)
        self.save()

    def close(self) -> None:
        self.save()
        self.conn.close()

    def __enter__(self) -> "MappingStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
