from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import keyring
from cryptography.fernet import Fernet, InvalidToken

from .config import app_data_dir

SERVICE = "anonymizer-mapping-db"
KEY_NAME = "key"
# The key in force just before the last rotation, kept so a file still encrypted
# under it (e.g. a crash between saving and re-publishing the key) can be
# recovered instead of the whole mapping being lost.
PREV_KEY_NAME = "key_prev"

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
            sql_dump = self._decrypt(encrypted)
            self.conn.executescript(sql_dump)
        else:
            self.conn.executescript(SCHEMA)

    def _decrypt(self, encrypted: bytes) -> str:
        """Decrypts with the current key, falling back to the retained previous
        key if a rotation left the file under it -- so a rotation crash can't
        strand the entire reversible mapping."""
        try:
            return self._fernet.decrypt(encrypted).decode("utf-8")
        except InvalidToken:
            prev = keyring.get_password(SERVICE, PREV_KEY_NAME)
            if prev:
                return Fernet(prev.encode()).decrypt(encrypted).decode("utf-8")
            raise

    def save(self) -> None:
        dump = "\n".join(self.conn.iterdump())
        encrypted = self._fernet.encrypt(dump.encode("utf-8"))
        # Atomic write: encrypt to a sibling temp then os.replace, so a crash
        # mid-write can never corrupt or truncate the ONLY copy of the mapping.
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.db_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(encrypted)
            os.replace(tmp, self.db_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def get_or_create(self, entity_type: str, value: str, label: str | None = None) -> str:
        """Returns the stable placeholder for (entity_type, value), creating one
        the first time. `label` controls the token prefix (e.g. 'IBAN'); it
        defaults to the entity type. Placeholder numbering is per-label so the
        same real value always renders to the same token across documents."""
        key = value.strip().lower()
        # Rows are keyed on the CANONICAL label, not the raw entity type: two
        # recognizers that render the same token (PHONE_NUMBER and DE_PHONE both
        # -> [PHONE_n]) must map one real value to ONE token, otherwise a reader
        # sees the same phone number as two different values.
        canonical = label or entity_type
        row = self.conn.execute(
            "SELECT placeholder FROM mappings WHERE entity_type=? AND value_key=?",
            (canonical, key),
        ).fetchone()
        if row:
            return row[0]
        # Rows written before canonical keying used the raw entity type; honour
        # them so already-anonymized documents keep re-identifying to the token
        # they were given.
        if canonical != entity_type:
            legacy = self.conn.execute(
                "SELECT placeholder FROM mappings WHERE entity_type=? AND value_key=?",
                (entity_type, key),
            ).fetchone()
            if legacy:
                return legacy[0]
        prefix = canonical
        # Number from the MAX existing suffix, never a COUNT: a count is reused
        # after erase()/reset(), so a new value could collide with a live
        # placeholder (PERSON_2 -> erase -> next new person also PERSON_2) and
        # re-identify to the wrong person. Max+1 never reuses a retired number.
        rows = self.conn.execute(
            "SELECT placeholder FROM mappings WHERE placeholder GLOB ?", (f"{prefix}_*",)
        ).fetchall()
        plen = len(prefix) + 1
        max_n = 0
        for (ph,) in rows:
            tail = ph[plen:]
            if tail.isdigit():
                max_n = max(max_n, int(tail))
        placeholder = f"{prefix}_{max_n + 1}"
        self.conn.execute(
            "INSERT INTO mappings(entity_type, value_key, original_value, placeholder) VALUES (?,?,?,?)",
            (canonical, key, value, placeholder),
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
        """Generates a fresh key and re-saves the mapping under it. Order is
        critical for crash-safety: PUBLISH the keys to the keyring FIRST (retain
        the old key as PREV, set the new key as current), and only THEN write the
        file under the new key. This guarantees the on-disk file is decryptable at
        EVERY crash point:
          * crash while publishing keys       -> file still under the old key, and
            (current or previous) still holds the old key -> recoverable.
          * crash during the re-save          -> file still under the old key,
            which is now retained as PREV     -> recoverable via the prev-key fallback.
          * completed                         -> file under the new (current) key.
        The old code saved under the new key BEFORE publishing it, so a crash in
        that window stranded the entire reversible mapping (the file's key existed
        only in memory) -- the exact loss this ordering prevents."""
        old_key = keyring.get_password(SERVICE, KEY_NAME)
        new_key = Fernet.generate_key()
        if old_key:
            keyring.set_password(SERVICE, PREV_KEY_NAME, old_key)  # retain for fallback
        keyring.set_password(SERVICE, KEY_NAME, new_key.decode())  # publish BEFORE saving
        self._fernet = Fernet(new_key)
        self.save()  # mapping file now under the already-published new key
        # The encrypted allow/deny lists (config.lists.enc) are encrypted under this
        # SAME key. Re-key them in the same operation -- loading them now decrypts via
        # the just-set PREV key and re-encrypts under the new key. Otherwise a SECOND
        # rotation would evict their key from the single PREV slot and strand them.
        try:
            from . import config as _config

            _config._load_secure_lists()  # side effect: re-encrypts under the current key
        except Exception:  # noqa: BLE001 -- mapping already rotated; prev-key fallback still covers one lag
            pass

    def close(self, save: bool = True) -> None:
        if save:
            self.save()
        self.conn.close()

    def __enter__(self) -> "MappingStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
