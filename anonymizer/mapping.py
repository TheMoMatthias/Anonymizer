from __future__ import annotations

import base64
import itertools
import os
import sqlite3
import tempfile
import time
import traceback
import weakref
from pathlib import Path

import keyring
from cryptography.fernet import Fernet, InvalidToken

from .config import app_data_dir

try:  # Windows: mandatory byte-range locks, released automatically when the
    # owning handle closes (so a crashed process can never leave a stale lock).
    import msvcrt
except ImportError:  # pragma: no cover -- POSIX fallback, dev machines only
    msvcrt = None
    import fcntl

SERVICE = "anonymizer-mapping-db"
KEY_NAME = "key"
# The key in force just before the last rotation, kept so a file still encrypted
# under it (e.g. a crash between saving and re-publishing the key) can be
# recovered instead of the whole mapping being lost.
PREV_KEY_NAME = "key_prev"

# How long a second MappingStore waits for the exclusive lock before giving up.
# Bounded on purpose: a GUI that hangs forever is unacceptable, and a loud
# refusal is far better than the silent clobber this lock exists to prevent.
LOCK_TIMEOUT_SECONDS = 10.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS mappings (
    entity_type TEXT NOT NULL,
    value_key TEXT NOT NULL,
    original_value TEXT NOT NULL,
    placeholder TEXT NOT NULL,
    PRIMARY KEY (entity_type, value_key)
);
CREATE INDEX IF NOT EXISTS idx_mappings_placeholder ON mappings(placeholder);
-- Per-label HIGH-WATER MARK of issued placeholder numbers. It deliberately
-- OUTLIVES the rows it counts: erase() and reset() delete mappings, but the
-- already-archived documents that carry [PERSON_2] do not disappear with them,
-- so number 2 must never be handed to a different person again.
CREATE TABLE IF NOT EXISTS placeholder_counters (
    label TEXT PRIMARY KEY,
    next_n INTEGER NOT NULL
);
"""


class MappingError(RuntimeError):
    """Base class for mapping-store failures. Every one of these is fail-loud on
    purpose: the mapping is the reversible re-identification table, so a silent
    fallback here mis-identifies a human being."""


class MappingLockError(MappingError):
    """Another MappingStore holds the exclusive lock on this mapping file."""


class MappingKeyError(MappingError):
    """The mapping file exists but no key in the Credential Manager decrypts it."""


class MappingConflictError(MappingError):
    """Two stores independently issued the same placeholder to different values."""


class MappingRekeyError(MappingError):
    """Key rotation re-keyed the mapping but not everything sharing that key."""


class MappingCorruptError(MappingError):
    """The mapping file exists but its CONTENT is damaged (zero-length, truncated,
    or not a Fernet token at all). Deliberately distinct from MappingKeyError: a
    damaged file and a lost credential need opposite remedies, and sending the
    operator hunting for a credential that is sitting right there costs time the
    mapping may not have."""


class UnreversibleTokenError(MappingError):
    """A pseudonym placeholder was about to be rendered into a document in a shape
    that re-identification could never find again."""


_KEY_LOST_MSG = (
    "The pseudonym mapping '{path}' exists but cannot be decrypted: the Windows "
    "Credential Manager entry '{service}/{name}' is missing or no longer matches "
    "it. Without that key the mapping is UNRECOVERABLE -- documents that were "
    "already anonymized can no longer be re-identified. Nothing has been "
    "overwritten. Restore the credential (or a backup of it) and try again; delete "
    "the mapping file only if you accept losing every existing pseudonym."
)

_CORRUPT_MSG = (
    "The pseudonym mapping '{path}' exists but is DAMAGED: {why}. Nothing was "
    "overwritten and no fresh mapping was started -- starting one would silently "
    "hand out PERSON_1, PERSON_2 ... all over again, and every archived document "
    "already carrying those tokens would re-identify to a DIFFERENT human being. "
    "Restore the file (or its backup) and try again. Delete it only if you accept "
    "that every existing pseudonym becomes permanently unresolvable."
)
_WHY_EMPTY = (
    "the file is zero bytes long, which is exactly what an interrupted write, a "
    "full disk or a power loss leaves behind"
)
_WHY_MALFORMED = (
    "its content is not a complete encrypted mapping (truncated or overwritten by "
    "something else) -- the encryption key is not the problem"
)

# Diagnostic registry of the mapping paths this PROCESS currently holds the lock
# on: resolved path -> (ticket, "file:line where it was opened"). Never used to
# decide whether to lock -- only to describe a conflict correctly. A conflict with
# a store inside this same process is an application bug (a store that was never
# closed), and the operator remedy for a cross-process conflict ("close the other
# window") is nonsense for it; giving the wrong remedy is what turns a leaked
# store into a wedge nobody can explain.
_OPEN_LOCKS: dict[str, tuple[int, str]] = {}
_LOCK_TICKETS = itertools.count(1)


def _release_locked_handle(fh, key: str, ticket: int) -> None:
    """Drops the OS lock and this store's registry entry. Registered through
    weakref.finalize as well as called from close(), so a store that was never
    closed still frees the mapping the moment it is collected -- close() stays the
    supported path, this is the safety net under it. Only pops the registry entry
    if it is still OURS: a later store on the same path must not have its entry
    removed by a predecessor's late finalizer."""
    entry = _OPEN_LOCKS.get(key)
    if entry is not None and entry[0] == ticket:
        del _OPEN_LOCKS[key]
    try:
        _unlock(fh)
    except OSError:
        pass  # closing the handle drops the lock anyway
    try:
        fh.close()
    except OSError:
        pass


def _is_structurally_fernet(blob: bytes) -> bool:
    """True if `blob` has the SHAPE of a Fernet token -- urlsafe base64 of
    (version 0x80 | 8-byte timestamp | 16-byte IV | whole AES blocks | 32-byte
    HMAC) -- regardless of WHICH key produced it. This is what lets a failed
    decrypt be attributed correctly: a well-formed token that no key opens is a
    KEY problem, a malformed one is a damaged FILE. Both are fatal, but they have
    opposite remedies, and the old code reported every one of them as a lost
    credential."""
    try:
        raw = base64.urlsafe_b64decode(blob)
    except Exception:  # noqa: BLE001 -- any decode failure means "not a token"
        return False
    return len(raw) >= 57 and raw[0] == 0x80 and (len(raw) - 57) % 16 == 0


def _get_or_create_key() -> bytes:
    key = keyring.get_password(SERVICE, KEY_NAME)
    if not key:
        key = Fernet.generate_key().decode()
        keyring.set_password(SERVICE, KEY_NAME, key)
    return key.encode()


def _try_lock(fh) -> None:
    """Takes a non-blocking exclusive lock on `fh`, raising OSError if another
    handle holds it. Byte-range locks are used rather than a lock FILE whose mere
    existence means 'locked': the OS drops these when the handle closes, so a
    crashed run cannot leave a lock nobody can clear."""
    if msvcrt is not None:
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover -- POSIX fallback
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fh) -> None:
    if msvcrt is not None:
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover -- POSIX fallback
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class MappingStore:
    """Encrypted, per-colleague pseudonym table. Holds the reversible mapping
    from an original value to its consistent placeholder (e.g. PERSON_1). The
    on-disk file is Fernet-encrypted; the key lives in Windows Credential
    Manager, never in a document folder.

    Only ONE store may be open on a given mapping file at a time. The whole
    load..save span is the critical section -- the store works on an in-memory
    image and save() writes that whole image back -- so two overlapping stores
    used to end with the last saver silently erasing every mapping the other
    created (and then re-issuing those numbers to different people). The
    exclusive lock is therefore held for the store's entire lifetime, and a
    second store fails loudly after a bounded wait instead.

    Because the lock spans the whole lifetime, a store MUST be scoped: use
    `with MappingStore(path) as store:` or a bare construction paired with
    `try: ... finally: store.close(...)`. A store that is merely abandoned is
    freed by the collector (there is a weakref.finalize safety net), but a store
    pinned alive by something -- a stored exception, a retained traceback frame --
    keeps the mapping locked for as long as that reference lives. That state
    cannot be auto-healed without re-opening the concurrent-clobber hole the lock
    exists to close, so it is instead reported as exactly what it is (see
    _acquire_lock)."""

    def __init__(self, db_path: Path | None = None, lock_timeout: float = LOCK_TIMEOUT_SECONDS):
        self.db_path = db_path or (app_data_dir() / "mappings.db")
        # Intentional constructor side effect: the lock sidecar is opened before
        # anything else, so its directory has to exist even for a mapping that has
        # never been saved. Creating an empty directory is not a data side effect
        # and it is the same directory save() would create moments later.
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Where this store was opened, for the lock-conflict message. File and line
        # only -- never document content, so it can never carry PII.
        caller = traceback.extract_stack(limit=2)[0]
        self._opened_at = f"{Path(caller.filename).name}:{caller.lineno}"
        self._lock_fh = None
        self._lock_finalizer: weakref.finalize | None = None
        self._closed = False
        # Exact bytes last read from / written to the file. Compared byte-for-byte
        # rather than by (mtime, size): the clock granularity is coarser than two
        # back-to-back saves, so a timestamp check can miss a real change.
        self._disk_blob: bytes | None = None
        self._acquire_lock(lock_timeout)
        try:
            # Validate the file's SHAPE before touching the keyring. A zero-length
            # or truncated mapping must be reported as a damaged FILE; resolving a
            # key first would blame a credential that is present and fine.
            existing = self._read_db_bytes() if self._db_present() else None
            self._fernet = Fernet(self._resolve_key())
            self.conn = sqlite3.connect(":memory:")
            self._load(existing)
        except BaseException:
            # Never hold the lock after a failed open, or the next attempt (and
            # every later one) would report a lock error instead of the real cause.
            # The :memory: connection is closed too -- it may already exist if the
            # failure came from _load().
            conn = getattr(self, "conn", None)
            if conn is not None:
                conn.close()
            self._release_lock()
            raise

    # --- locking -------------------------------------------------------------

    def _lock_path(self) -> Path:
        return self.db_path.with_name(self.db_path.name + ".lock")

    def _acquire_lock(self, timeout: float) -> None:
        # A dedicated sidecar file, never the mapping itself: the mapping is
        # replaced wholesale by os.replace, which would drop a lock taken on it.
        # Nothing ever writes to the sidecar, so every handle sits at offset 0 and
        # they all contend for the same byte.
        #
        # The sidecar is created once and then left in place FOREVER -- that is
        # deliberate, not a leak. The lock is a byte-range lock on an open handle,
        # so the file's EXISTENCE carries no meaning whatsoever: a leftover
        # <db>.lock says nothing about whether anything is locked, and the OS drops
        # the real lock the instant the handle closes (even on a crash). Deleting
        # it on close would be strictly worse: the unlink races against another
        # process that is legitimately holding a lock on that same inode, and the
        # loser then locks a file nobody else can see -- i.e. two writers that both
        # believe they are exclusive, which is the silent-clobber bug this lock
        # exists to prevent. A stray zero-byte sidecar is the correct price.
        path = self._lock_path()
        key = str(self.db_path.resolve())
        fh = open(path, "a+b")  # noqa: SIM115 -- held for the store's lifetime
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            try:
                _try_lock(fh)
            except OSError:
                if time.monotonic() >= deadline:
                    fh.close()
                    raise MappingLockError(self._lock_conflict_message(key, timeout)) from None
                time.sleep(0.05)
                continue
            ticket = next(_LOCK_TICKETS)
            self._lock_fh = fh
            _OPEN_LOCKS[key] = (ticket, self._opened_at)
            # Safety net for a store that is never closed: releases the lock as
            # soon as the store is collected. It holds no reference to `self`, so
            # registering it cannot itself keep the store (and the lock) alive.
            self._lock_finalizer = weakref.finalize(self, _release_locked_handle, fh, key, ticket)
            return

    def _lock_conflict_message(self, key: str, timeout: float) -> str:
        """Says which KIND of conflict this is. The lock is never force-cleared:
        a forced second writer would dump its whole in-memory table back over the
        first one's and silently erase every pseudonym it had issued."""
        common = (
            f"The pseudonym mapping '{self.db_path}' is already open and locked "
            f"(waited {timeout:.0f}s). Refusing to open a second copy: both would "
            "write the whole table back and the last one would silently erase the "
            "other's pseudonyms. "
        )
        holder = _OPEN_LOCKS.get(key)
        if holder is not None:
            # In-process. "A store still in use" and "a store nobody closed" are
            # indistinguishable from in here -- both are simply a live object -- so
            # the message gives the remedy for each rather than guessing.
            return common + (
                "The lock is held from inside THIS Anonymizer process, by a mapping "
                f"opened at {holder[1]}. If another document is still being processed, "
                "let it finish and try again. If nothing else is running, that mapping "
                "was left open by a bug and nothing you close in the interface will "
                "clear it: restart the Anonymizer. The lock lives only as long as the "
                "process does, and restarting loses nothing -- the mapping file on disk "
                "is untouched."
            )
        return common + (
            "The lock is held by another Anonymizer window or process. Close the other "
            "operation and try again."
        )

    def _release_lock(self) -> None:
        finalizer, self._lock_finalizer = self._lock_finalizer, None
        self._lock_fh = None
        if finalizer is not None:
            finalizer()  # weakref.finalize runs at most once, so this is idempotent

    # --- key / crypto --------------------------------------------------------

    def _db_present(self) -> bool:
        """A mapping FILE is present. Existence alone -- deliberately not
        `st_size > 0`. A zero-length mapping is what a pre-fsync power loss or a
        full disk leaves behind, and treating it as 'no mapping yet' was silent
        data loss of the worst kind: a fresh key was minted, the file skipped, and
        the high-water counters (which live INSIDE that file) vanished, so the very
        next mint handed out PERSON_1 again -- re-issuing numbers already printed
        in archived documents. Presence is decided here; whether the content is
        USABLE is decided by _read_db_bytes(), which fails loud."""
        return self.db_path.exists()

    def _read_db_bytes(self) -> bytes:
        """The encrypted mapping bytes, or a loud MappingCorruptError. Every read
        of the file goes through here so a damaged mapping can never be mistaken
        for a missing one (silent renumbering) or for a missing key (wrong
        remedy)."""
        blob = self.db_path.read_bytes()
        if not blob:
            raise MappingCorruptError(_CORRUPT_MSG.format(path=self.db_path, why=_WHY_EMPTY))
        if not _is_structurally_fernet(blob):
            raise MappingCorruptError(_CORRUPT_MSG.format(path=self.db_path, why=_WHY_MALFORMED))
        return blob

    def _resolve_key(self) -> bytes:
        """The key to decrypt with. Minting a fresh key is only ever correct for a
        mapping that does not exist yet: doing it for an EXISTING file (the old
        behaviour) turned a lost credential into a raw InvalidToken traceback and
        left the store one save() away from overwriting the only copy under a key
        that was never used to encrypt it."""
        if self._db_present() and not keyring.get_password(SERVICE, KEY_NAME):
            if not keyring.get_password(SERVICE, PREV_KEY_NAME):
                raise MappingKeyError(_KEY_LOST_MSG.format(path=self.db_path, service=SERVICE, name=KEY_NAME))
        return _get_or_create_key()

    def _decrypt(self, encrypted: bytes) -> str:
        """Decrypts with the current key, falling back to the retained previous
        key if a rotation left the file under it -- so a rotation crash can't
        strand the entire reversible mapping."""
        try:
            return self._fernet.decrypt(encrypted).decode("utf-8")
        except InvalidToken:
            prev = keyring.get_password(SERVICE, PREV_KEY_NAME)
            if prev:
                try:
                    return Fernet(prev.encode()).decrypt(encrypted).decode("utf-8")
                except InvalidToken:
                    pass
            # Raise something that says what happened and what to do: a bare
            # InvalidToken (empty message) is the worst possible UX at the worst
            # possible moment.
            raise MappingKeyError(
                _KEY_LOST_MSG.format(path=self.db_path, service=SERVICE, name=KEY_NAME)
            ) from None

    # --- load / save ---------------------------------------------------------

    def _load(self, encrypted: bytes | None) -> None:
        """`encrypted` is the already-shape-checked file content, or None for a
        mapping that does not exist yet."""
        if encrypted is not None:
            sql_dump = self._decrypt(encrypted)
            self.conn.executescript(sql_dump)
            self._disk_blob = encrypted
        # Runs for a fresh AND an existing database: every statement is IF NOT
        # EXISTS, so this also migrates a mapping written before the high-water
        # counters existed.
        self.conn.executescript(SCHEMA)
        self._seed_counters_from_rows()

    def save(self) -> None:
        self._merge_disk_changes()
        dump = "\n".join(self.conn.iterdump())
        encrypted = self._fernet.encrypt(dump.encode("utf-8"))
        # Atomic write: encrypt to a sibling temp then os.replace, so a crash
        # mid-write can never corrupt or truncate the ONLY copy of the mapping.
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.db_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(encrypted)
                # os.replace is only atomic with respect to the DIRECTORY entry.
                # Without this fsync the rename can land while the data is still in
                # the OS cache, so a power loss leaves a zero-length or half-written
                # mapping -- every pseudonym in it unrecoverable.
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.db_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self._disk_blob = encrypted

    def _merge_disk_changes(self) -> None:
        """Folds in rows another writer added since we loaded, so save() cannot
        blank them. Defence in depth behind the lifetime lock: it also covers a
        file touched by a build that predates the lock. A row whose placeholder is
        already OURS for a different value is a genuine conflict -- both numbers
        are already printed in two different documents and no merge can undo
        that -- so it fails loudly rather than mis-identifying someone."""
        if not self._db_present():
            return
        blob = self._read_db_bytes()
        if blob == self._disk_blob:
            return
        other = sqlite3.connect(":memory:")
        try:
            other.executescript(self._decrypt(blob))
            other.executescript(SCHEMA)
            rows = other.execute(
                "SELECT entity_type, value_key, original_value, placeholder FROM mappings"
            ).fetchall()
            counters = other.execute("SELECT label, next_n FROM placeholder_counters").fetchall()
        finally:
            other.close()
        for entity_type, value_key, original_value, placeholder in rows:
            mine = self.conn.execute(
                "SELECT placeholder FROM mappings WHERE entity_type=? AND value_key=?",
                (entity_type, value_key),
            ).fetchone()
            if mine:
                if mine[0] != placeholder:
                    # Message carries tokens only -- never the original value.
                    raise MappingConflictError(
                        f"The mapping on disk gives '{entity_type}' a value that this "
                        f"session already stored as '{mine[0]}' but disk says "
                        f"'{placeholder}'. Two runs pseudonymized the same value "
                        "concurrently; merging would make one document re-identify "
                        "wrongly. Nothing was saved."
                    )
                continue
            taken = self.conn.execute(
                "SELECT 1 FROM mappings WHERE placeholder=?", (placeholder,)
            ).fetchone()
            if taken:
                raise MappingConflictError(
                    f"Placeholder '{placeholder}' was issued to two different values "
                    "(once on disk, once in this session). Both are already printed in "
                    "their documents, so no merge can be correct. Nothing was saved."
                )
            self.conn.execute(
                "INSERT INTO mappings(entity_type, value_key, original_value, placeholder) VALUES (?,?,?,?)",
                (entity_type, value_key, original_value, placeholder),
            )
        for label, next_n in counters:
            self._bump_counter(label, int(next_n))

    # --- placeholder numbering ----------------------------------------------

    def _bump_counter(self, label: str, next_n: int) -> None:
        row = self.conn.execute(
            "SELECT next_n FROM placeholder_counters WHERE label=?", (label,)
        ).fetchone()
        if row is None or int(row[0]) < next_n:
            self.conn.execute(
                "INSERT OR REPLACE INTO placeholder_counters(label, next_n) VALUES (?,?)",
                (label, next_n),
            )

    def _seed_counters_from_rows(self) -> None:
        """Raises every label's high-water mark to at least max(issued)+1. Needed
        once at load for a mapping written before the counters table existed --
        otherwise the first erase()/reset() on such a file would drop the only
        evidence of which numbers are already out in archived documents."""
        highest: dict[str, int] = {}
        for (placeholder,) in self.conn.execute("SELECT placeholder FROM mappings"):
            head, sep, tail = placeholder.rpartition("_")
            if sep and head and tail.isascii() and tail.isdigit():
                n = int(tail) + 1
                if n > highest.get(head, 0):
                    highest[head] = n
        for label, next_n in highest.items():
            self._bump_counter(label, next_n)

    def _max_issued_suffix(self, prefix: str) -> int:
        """Highest n among existing <prefix>_<n> placeholders. Scanned in Python
        rather than with `placeholder GLOB 'prefix_*'`: the label is caller-supplied
        (a config-defined entity name, a spreadsheet column header) and a GLOB
        metacharacter in it silently matches the wrong rows -- '[' most of all,
        which makes the scan match NOTHING, restart numbering at 1 and hand one
        placeholder to two different values."""
        head = prefix + "_"
        plen = len(head)
        max_n = 0
        for (placeholder,) in self.conn.execute("SELECT placeholder FROM mappings"):
            if placeholder.startswith(head):
                tail = placeholder[plen:]
                if tail.isascii() and tail.isdigit():
                    max_n = max(max_n, int(tail))
        return max_n

    def _next_placeholder(self, prefix: str) -> str:
        """The next never-yet-issued placeholder for `prefix`. Driven by the
        durable high-water counter, never by a COUNT or by the live rows: those
        recycle a number after erase()/reset(), and an archived document that says
        [PERSON_2] would then re-identify to a DIFFERENT human being."""
        row = self.conn.execute(
            "SELECT next_n FROM placeholder_counters WHERE label=?", (prefix,)
        ).fetchone()
        n = int(row[0]) if row else self._max_issued_suffix(prefix) + 1
        # Belt and braces: never hand out a number that is demonstrably live.
        while self.conn.execute(
            "SELECT 1 FROM mappings WHERE placeholder=?", (f"{prefix}_{n}",)
        ).fetchone():
            n += 1
        self.conn.execute(
            "INSERT OR REPLACE INTO placeholder_counters(label, next_n) VALUES (?,?)", (prefix, n + 1)
        )
        return f"{prefix}_{n}"

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
        placeholder = self._next_placeholder(canonical)
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
        row was removed. The number stays retired: the high-water counter is
        untouched, so it is never issued to someone else."""
        cur = self.conn.execute("DELETE FROM mappings WHERE placeholder=?", (placeholder,))
        return cur.rowcount > 0

    def reset(self, restart_numbering: bool = False) -> None:
        """Wipes every mapping; existing anonymized documents can no longer be
        re-identified. Numbering does NOT restart, because those documents still
        exist and still say [PERSON_2] -- re-issuing 2 would make an archived file
        re-identify to a DIFFERENT human being. `restart_numbering=True` is the
        explicit, opt-in exception, and is only safe once NO anonymized output
        produced from this mapping survives anywhere."""
        self.conn.execute("DELETE FROM mappings")
        if restart_numbering:
            self.conn.execute("DELETE FROM placeholder_counters")

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
        except Exception as exc:  # noqa: BLE001 -- re-raised loudly below
            # Swallowing this was a silent, delayed data loss: the lists stayed under
            # the old key, and the NEXT rotation evicted that key from the single PREV
            # slot, permanently stranding the deny list the user redacts named projects
            # with -- with no error ever shown.
            raise MappingRekeyError(
                "The mapping database was re-keyed, but the encrypted allow/deny lists "
                f"(lists.enc) could NOT be re-keyed: {exc}. They are still readable via "
                "the retained previous key, but do NOT rotate again until this is fixed "
                "-- a second rotation would evict that key and strand the deny list "
                "permanently."
            ) from exc

    def close(self, save: bool = True) -> None:
        if self._closed:
            return
        try:
            if save:
                self.save()
        finally:
            self._closed = True
            self.conn.close()
            self._release_lock()

    def __enter__(self) -> "MappingStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
