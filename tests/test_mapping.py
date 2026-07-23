import gc
import os
import sqlite3

import keyring
import pytest
from cryptography.fernet import Fernet

from anonymizer import mapping as mapping_mod
from anonymizer.mapping import (
    KEY_NAME,
    SERVICE,
    MappingConflictError,
    MappingCorruptError,
    MappingKeyError,
    MappingLockError,
    MappingRekeyError,
    MappingStore,
)


@pytest.fixture(autouse=True)
def _isolate_app_data(tmp_path, monkeypatch):
    """Keeps rotate_key's lists.enc re-key off the developer's REAL
    %LOCALAPPDATA%\\Anonymizer. Since that re-key now fails LOUDLY (it used to be
    swallowed), a test rotating with the in-memory test keyring would otherwise
    trip over the user's genuine, differently-encrypted lists.enc."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))


def test_consistent_placeholder_within_session(mapping_db_path):
    with MappingStore(mapping_db_path) as store:
        p1 = store.get_or_create("PERSON", "Hans Mueller")
        p2 = store.get_or_create("PERSON", "hans mueller")  # case-insensitive key
        assert p1 == p2


def test_persists_and_encrypts_across_sessions(mapping_db_path):
    with MappingStore(mapping_db_path) as store:
        placeholder = store.get_or_create("PERSON", "Hans Mueller")

    raw_bytes = mapping_db_path.read_bytes()
    assert b"Hans Mueller" not in raw_bytes  # encrypted at rest

    with MappingStore(mapping_db_path) as store:
        assert store.get_or_create("PERSON", "Hans Mueller") == placeholder


def test_different_values_get_distinct_placeholders(mapping_db_path):
    with MappingStore(mapping_db_path) as store:
        p1 = store.get_or_create("PERSON", "Hans Mueller")
        p2 = store.get_or_create("PERSON", "Petra Schmidt")
        assert p1 != p2


def test_placeholder_not_reused_after_erase(mapping_db_path):
    """Regression: numbering by COUNT(*) reused a retired number after erase(),
    re-identifying a token to the WRONG person. Max+1 must never reuse it."""
    with MappingStore(mapping_db_path) as store:
        assert store.get_or_create("PERSON", "Mueller") == "PERSON_1"
        assert store.get_or_create("PERSON", "Schmidt") == "PERSON_2"
        store.erase("PERSON_1")
        assert store.get_or_create("PERSON", "Weber") == "PERSON_3"
    with MappingStore(mapping_db_path) as store:  # survives atomic-save round-trip
        assert store.reverse("PERSON_2") == "Schmidt"
        assert store.reverse("PERSON_3") == "Weber"


def test_aliased_entity_types_share_one_token(mapping_db_path):
    """PHONE_NUMBER and DE_PHONE both render as [PHONE_n]; the same real number
    caught by either recognizer must map to the SAME token, not PHONE_1/PHONE_2."""
    with MappingStore(mapping_db_path) as store:
        a = store.get_or_create("PHONE_NUMBER", "+49 30 12345678", label="PHONE")
        b = store.get_or_create("DE_PHONE", "+49 30 12345678", label="PHONE")
        assert a == b == "PHONE_1"


def test_rotate_key_keeps_data_readable(mapping_db_path):
    with MappingStore(mapping_db_path) as store:
        tok = store.get_or_create("IBAN_CODE", "DE89370400440532013000", label="IBAN")
        store.rotate_key()
    with MappingStore(mapping_db_path) as store:  # opens under the rotated key
        assert store.reverse(tok) == "DE89370400440532013000"


def test_rotate_key_crash_during_publish_leaves_file_recoverable(mapping_db_path, monkeypatch):
    """Regression (DATA LOSS): rotate_key used to write the file under the NEW key
    BEFORE publishing that key, so a crash in that window stranded the entire
    reversible mapping (its key existed only in memory). Keys are now published
    FIRST, so a crash during rotation must always leave the file decryptable by a
    key still in the keyring."""
    with MappingStore(mapping_db_path) as store:
        tok = store.get_or_create("PERSON", "Hans Mueller", label="PERSON")

    store = MappingStore(mapping_db_path)
    in_memory_set = keyring.set_password  # the conftest in-memory backend

    def crash(*_a, **_k):
        raise RuntimeError("crash during key publish")

    monkeypatch.setattr(keyring, "set_password", crash)
    with pytest.raises(RuntimeError):
        store.rotate_key()
    store.close(save=False)

    monkeypatch.setattr(keyring, "set_password", in_memory_set)  # restore (not undo -> keep isolation)
    assert keyring.get_password("anonymizer-mapping-db", KEY_NAME), "current key must still be present"
    with MappingStore(mapping_db_path) as recovered:
        assert recovered.reverse(tok) == "Hans Mueller"  # file still decryptable after the crash


def test_rotate_key_also_rekeys_the_encrypted_lists(tmp_path, monkeypatch):
    """Regression: rotate_key re-keyed only mappings.db, not the lists.enc that
    shares the same key -> two rotations evicted the lists' key from the single PREV
    slot and stranded them. rotate_key must re-key the lists too."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    from anonymizer import config as cfg_mod

    cfg_mod._save_secure_lists({"deny_list": ["Klaus Mueller"], "allow_list": []})
    store = MappingStore(tmp_path / "Anonymizer" / "mappings.db")
    store.rotate_key()
    store.rotate_key()  # the second rotation is what used to strand the lists
    store.close(save=False)

    assert "Klaus Mueller" in cfg_mod._load_secure_lists().get("deny_list", [])


# --- A1: concurrent stores ---------------------------------------------------


def test_second_store_is_refused_instead_of_clobbering(mapping_db_path):
    """Regression (SILENT DATA LOSS): a store slurps the whole DB into memory and
    save() dumps that whole image back, so two overlapping stores ended with the
    last saver ERASING every mapping the other made -- and then re-issuing those
    numbers to different people. Before the fix this test read back
    reverse("PERSON_1") == "Schmidt": Mueller's mapping was gone and his number had
    been handed to someone else. A second store must now fail loudly instead."""
    first = MappingStore(mapping_db_path)
    first.get_or_create("PERSON", "Mueller", label="PERSON")
    with pytest.raises(MappingLockError):
        MappingStore(mapping_db_path, lock_timeout=0.2)
    first.close()

    with MappingStore(mapping_db_path) as check:
        assert check.reverse("PERSON_1") == "Mueller"


def test_lock_is_released_so_stores_can_be_used_back_to_back(mapping_db_path):
    """The lock must not outlive the store, or the very next document would be
    unable to open the mapping at all."""
    for value in ("Mueller", "Schmidt", "Weber"):
        with MappingStore(mapping_db_path) as store:
            store.get_or_create("PERSON", value, label="PERSON")
    with MappingStore(mapping_db_path) as store:
        assert store.entry_count() == 3
        assert store.reverse("PERSON_3") == "Weber"


def test_lock_is_released_when_opening_fails(mapping_db_path):
    """A failed open (bad key, corrupt file) must not leave the lock held: the
    next attempt would then report a misleading 'in use' error forever, hiding the
    real cause."""
    with MappingStore(mapping_db_path) as store:
        store.get_or_create("PERSON", "Mueller", label="PERSON")

    good_key = keyring.get_password(SERVICE, KEY_NAME)
    keyring.set_password(SERVICE, KEY_NAME, "")  # credential lost
    with pytest.raises(MappingKeyError):
        MappingStore(mapping_db_path, lock_timeout=0.2)
    keyring.set_password(SERVICE, KEY_NAME, good_key)

    with MappingStore(mapping_db_path, lock_timeout=0.2) as store:  # not wedged
        assert store.reverse("PERSON_1") == "Mueller"


def test_save_folds_in_rows_written_by_a_non_locking_writer(mapping_db_path, monkeypatch):
    """Defence in depth behind the lock: if the file changed under us anyway (a
    build that predates the lock), save() must MERGE those rows instead of dumping
    our stale image over them."""
    with MappingStore(mapping_db_path) as first:
        first.get_or_create("PERSON", "Mueller", label="PERSON")

    ours = MappingStore(mapping_db_path)
    ours.get_or_create("ORG", "Beispiel AG", label="ORG")

    monkeypatch.setattr(mapping_mod, "_try_lock", lambda fh: None)  # a writer that ignores the lock
    with MappingStore(mapping_db_path) as legacy:
        assert legacy.get_or_create("PERSON", "Schmidt", label="PERSON") == "PERSON_2"

    ours.close()  # saves; must not erase Schmidt

    with MappingStore(mapping_db_path) as check:
        assert check.reverse("PERSON_1") == "Mueller"
        assert check.reverse("PERSON_2") == "Schmidt"
        assert check.reverse("ORG_1") == "Beispiel AG"
        # The merged-in number must also raise our high-water mark.
        assert check.get_or_create("PERSON", "Weber", label="PERSON") == "PERSON_3"


def test_merge_refuses_to_reconcile_a_duplicated_placeholder(mapping_db_path, monkeypatch):
    """If two writers did manage to hand PERSON_1 to two different people, both
    numbers are already printed in their documents and NO merge can be correct.
    Fail loud (no file written) rather than pick a winner and mis-identify."""
    monkeypatch.setattr(mapping_mod, "_try_lock", lambda fh: None)  # writers ignoring the lock

    ours = MappingStore(mapping_db_path)
    assert ours.get_or_create("PERSON", "Mueller", label="PERSON") == "PERSON_1"
    other = MappingStore(mapping_db_path)
    assert other.get_or_create("PERSON", "Schmidt", label="PERSON") == "PERSON_1"
    other.close()

    with pytest.raises(MappingConflictError):
        ours.close()


# --- A2: high-water mark -----------------------------------------------------


def test_erasing_the_highest_placeholder_does_not_recycle_it(mapping_db_path):
    """Regression (WRONG-PERSON re-identification): max-suffix numbering protected
    a non-highest erase only. Erasing the HIGHEST row made the next new person
    PERSON_2 again, so every archived document saying [PERSON_2] re-identified to a
    different human being."""
    with MappingStore(mapping_db_path) as store:
        assert store.get_or_create("PERSON", "Mueller", label="PERSON") == "PERSON_1"
        assert store.get_or_create("PERSON", "Schmidt", label="PERSON") == "PERSON_2"
        assert store.erase("PERSON_2") is True
        assert store.get_or_create("PERSON", "Weber", label="PERSON") == "PERSON_3"


def test_reset_does_not_restart_numbering(mapping_db_path):
    """reset() wipes the table, but the ANONYMIZED DOCUMENTS it produced still
    exist and still say [PERSON_2]. Restarting at 1 would re-issue those numbers to
    other people, silently re-pointing every archived token."""
    with MappingStore(mapping_db_path) as store:
        store.get_or_create("PERSON", "Mueller", label="PERSON")
        store.get_or_create("PERSON", "Schmidt", label="PERSON")
        store.reset()
        assert store.entry_count() == 0
        assert store.get_or_create("PERSON", "Weber", label="PERSON") == "PERSON_3"


def test_reset_can_restart_numbering_only_when_asked_explicitly(mapping_db_path):
    """The opt-in escape hatch, for when no anonymized output survives at all."""
    with MappingStore(mapping_db_path) as store:
        store.get_or_create("PERSON", "Mueller", label="PERSON")
        store.get_or_create("PERSON", "Schmidt", label="PERSON")
        store.reset(restart_numbering=True)
        assert store.get_or_create("PERSON", "Weber", label="PERSON") == "PERSON_1"


def test_high_water_mark_survives_the_save_load_round_trip(mapping_db_path):
    """The mark is only useful if it is DURABLE -- it has to outlive the process
    that retired the number, because the archived documents do."""
    with MappingStore(mapping_db_path) as store:
        store.get_or_create("PERSON", "Mueller", label="PERSON")
        store.get_or_create("PERSON", "Schmidt", label="PERSON")
        store.erase("PERSON_2")
        store.reset()
    with MappingStore(mapping_db_path) as store:
        assert store.entry_count() == 0
        assert store.get_or_create("PERSON", "Weber", label="PERSON") == "PERSON_3"


def test_pre_counter_database_still_retires_numbers(mapping_db_path):
    """A mapping written before the counters table existed must be migrated on
    load, or the first erase()/reset() on such a file would drop the only record of
    which numbers are already printed in archived documents."""
    with MappingStore(mapping_db_path) as store:
        store.get_or_create("PERSON", "Mueller", label="PERSON")
        store.get_or_create("PERSON", "Schmidt", label="PERSON")
        store.conn.execute("DROP TABLE placeholder_counters")  # what an old file looks like

    with MappingStore(mapping_db_path) as store:
        assert store.erase("PERSON_2") is True
        assert store.get_or_create("PERSON", "Weber", label="PERSON") == "PERSON_3"


# --- A4/A5/A6/A7: rotation, key loss, durability, exotic labels --------------


def test_rotate_key_fails_loud_when_the_lists_cannot_be_rekeyed(mapping_db_path, monkeypatch):
    """Regression (SILENT, DELAYED DATA LOSS): the lists.enc re-key failure was
    swallowed by `except Exception: pass`. The lists then stayed under the old key,
    and the NEXT rotation evicted that key from the single PREV slot -- permanently
    stranding the deny list the user redacts named projects with, with no error
    ever shown."""
    from anonymizer import config as cfg_mod

    def boom():
        raise RuntimeError("lists.enc cannot be decrypted")

    monkeypatch.setattr(cfg_mod, "_load_secure_lists", boom)
    store = MappingStore(mapping_db_path)
    try:
        with pytest.raises(MappingRekeyError, match="rotate again"):
            store.rotate_key()
    finally:
        store.close(save=False)


def test_missing_key_fails_loud_and_never_overwrites_the_mapping(mapping_db_path):
    """Regression: with the Credential Manager entry gone, the store silently
    MINTED A NEW KEY and then died with a bare InvalidToken (empty message) -- the
    worst UX at the worst moment, one save() away from overwriting the only copy
    under a key that never encrypted it."""
    with MappingStore(mapping_db_path) as store:
        store.get_or_create("PERSON", "Hans Mueller", label="PERSON")
    before = mapping_db_path.read_bytes()

    keyring.set_password(SERVICE, KEY_NAME, "")  # credential deleted/lost
    with pytest.raises(MappingKeyError) as excinfo:
        MappingStore(mapping_db_path, lock_timeout=0.2)

    message = str(excinfo.value)
    assert "key" in message.lower() and "unrecoverable" in message.lower()
    assert "Hans Mueller" not in message  # never echo PII into an error
    assert mapping_db_path.read_bytes() == before  # untouched


def test_wrong_key_fails_loud_instead_of_raw_invalid_token(mapping_db_path):
    """A key that exists but does not match must produce the same explicit,
    actionable error -- not a bare cryptography InvalidToken traceback."""
    from cryptography.fernet import Fernet

    with MappingStore(mapping_db_path) as store:
        store.get_or_create("PERSON", "Hans Mueller", label="PERSON")

    keyring.set_password(SERVICE, KEY_NAME, Fernet.generate_key().decode())
    with pytest.raises(MappingKeyError):
        MappingStore(mapping_db_path, lock_timeout=0.2)


def test_save_fsyncs_before_the_atomic_rename(mapping_db_path, monkeypatch):
    """Regression (DURABILITY): os.replace is atomic for the directory entry only.
    Without an fsync the rename can land while the data is still in the OS cache,
    so a power loss leaves a zero-length or half-written mapping and every
    pseudonym in it is unrecoverable."""
    synced: list[int] = []
    order: list[int] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace", lambda a, b: (order.append(len(synced)), real_replace(a, b))[1])

    with MappingStore(mapping_db_path) as store:
        store.get_or_create("PERSON", "Mueller", label="PERSON")

    assert synced, "save() never fsynced the temp file"
    assert order and order[0] >= 1, "os.replace ran before any fsync"


def test_label_with_glob_metacharacters_still_numbers_correctly(mapping_db_path):
    """Regression: the max-suffix scan used `placeholder GLOB '<label>_*'`, but the
    label is caller-supplied (config-defined entity names, column headers). A '['
    in it turns the pattern into a character class that matches NOTHING, so
    numbering restarted at 1 and two different values were handed the SAME
    placeholder -- one of them re-identifying to the other's data."""
    with MappingStore(mapping_db_path) as store:
        first = store.get_or_create("X", "eins", label="PROJEKT_[A]")
        second = store.get_or_create("X", "zwei", label="PROJEKT_[A]")
        assert first != second, f"two different values both got {first}"
        assert store.reverse(first) == "eins"
        assert store.reverse(second) == "zwei"


# --- P5: a DAMAGED mapping must never be mistaken for a brand-new one --------


def _issue_two_persons(path) -> None:
    """PERSON_1 and PERSON_2 are now printed in (imagined) archived documents."""
    with MappingStore(path) as store:
        assert store.get_or_create("PERSON", "Mueller", label="PERSON") == "PERSON_1"
        assert store.get_or_create("PERSON", "Schmidt", label="PERSON") == "PERSON_2"


def test_zero_length_mapping_is_not_treated_as_a_fresh_mapping(mapping_db_path):
    """Regression (SILENT WRONG-PERSON RE-IDENTIFICATION): presence was decided by
    `st_size > 0`, so a ZERO-LENGTH mapping -- exactly what a pre-fsync power loss
    or a full disk leaves behind -- looked like 'no mapping yet'. A fresh key was
    minted, the file was skipped, and the high-water counters (which live INSIDE
    that file) were gone, so the very next mint handed out PERSON_1 again --
    re-issuing a number already printed in archived documents, with no error and
    no warning. An empty mapping file must fail LOUD."""
    _issue_two_persons(mapping_db_path)
    mapping_db_path.write_bytes(b"")  # interrupted write / full disk

    with pytest.raises(MappingCorruptError) as excinfo:
        MappingStore(mapping_db_path, lock_timeout=0.2)

    message = str(excinfo.value)
    assert "zero bytes" in message
    assert "PERSON_1" in message  # spells out the renumbering danger
    assert mapping_db_path.stat().st_size == 0  # never overwritten with a new mapping


def test_truncated_mapping_blames_the_file_not_the_credential(mapping_db_path):
    """A non-zero but truncated mapping did fail loud, but with the LOST-CREDENTIAL
    message -- sending the operator to hunt in the Credential Manager for a key
    that is present and perfectly fine, while the real problem (a damaged file that
    needs restoring from backup) goes unmentioned."""
    _issue_two_persons(mapping_db_path)
    blob = mapping_db_path.read_bytes()
    mapping_db_path.write_bytes(blob[:-8])  # tail lost

    with pytest.raises(MappingCorruptError) as excinfo:
        MappingStore(mapping_db_path, lock_timeout=0.2)

    message = str(excinfo.value).lower()
    assert "damaged" in message
    assert "credential manager" not in message
    assert "encryption key is not the problem" in message
    assert keyring.get_password(SERVICE, KEY_NAME), "the credential was fine all along"


def test_garbage_in_place_of_the_mapping_fails_loud(mapping_db_path):
    """Any non-Fernet content (a file overwritten by something else) is damage,
    not a missing mapping."""
    _issue_two_persons(mapping_db_path)
    mapping_db_path.write_bytes(b"this is not an encrypted mapping at all")

    with pytest.raises(MappingCorruptError):
        MappingStore(mapping_db_path, lock_timeout=0.2)


def test_a_valid_file_under_a_wrong_key_is_still_a_KEY_error(mapping_db_path):
    """The other side of the same discrimination: an INTACT file that no key opens
    must keep pointing at the credential, not send the operator looking for
    corruption that isn't there."""
    _issue_two_persons(mapping_db_path)
    keyring.set_password(SERVICE, KEY_NAME, Fernet.generate_key().decode())

    with pytest.raises(MappingKeyError):
        MappingStore(mapping_db_path, lock_timeout=0.2)


# --- P1: the lifetime lock must never wedge silently -------------------------


def _leak_a_store_pinned_by_a_traceback(path):
    """The exact shape that wedges: a store constructed outside with/try-finally,
    plus an exception whose traceback keeps this frame -- and therefore the store,
    and therefore the lock -- alive after the function returns."""
    store = MappingStore(path)
    store.get_or_create("PERSON", "Mueller", label="PERSON")
    try:
        raise RuntimeError("something failed while the mapping was open")
    except RuntimeError as exc:
        return exc


def test_a_lock_wedged_by_a_leaked_store_says_so_and_how_to_clear_it(mapping_db_path):
    """Regression (UNOPENABLE CROWN JEWEL): the lock is held for the store's whole
    lifetime, so a store pinned alive by a retained exception keeps the mapping
    locked for the rest of the process -- and every later open reported 'in use by
    another Anonymizer window ... close the other operation', a remedy that cannot
    possibly work because the holder is invisible and inside THIS process.
    Auto-clearing the lock is not an option (a forced second writer re-opens the
    silent-clobber hole the lock exists to close), so the error must at least name
    the real situation, where it came from, and the one thing that does work."""
    pinned = _leak_a_store_pinned_by_a_traceback(mapping_db_path)
    gc.collect()

    with pytest.raises(MappingLockError) as excinfo:
        MappingStore(mapping_db_path, lock_timeout=0.2)
    message = str(excinfo.value)
    assert "THIS Anonymizer process" in message
    assert "restart" in message.lower()
    assert "test_mapping.py:" in message, "the message must name where the leak was opened"

    # And once the pin goes, the lock frees itself: close() is the supported path,
    # the finalizer is the safety net under it.
    del pinned
    gc.collect()
    with MappingStore(mapping_db_path, lock_timeout=1.0) as store:
        assert store.reverse("PERSON_1") is None  # the leaked store never saved


def test_an_abandoned_store_releases_the_lock_when_collected(mapping_db_path):
    """A store that is simply dropped (no traceback pinning it) must not leave the
    mapping locked -- otherwise one forgotten close() bricks the app."""

    def leak() -> None:
        MappingStore(mapping_db_path).get_or_create("PERSON", "Mueller", label="PERSON")

    leak()
    gc.collect()
    with MappingStore(mapping_db_path, lock_timeout=1.0) as store:
        assert store.entry_count() == 0


def test_a_genuinely_busy_store_is_still_refused_with_both_remedies(mapping_db_path):
    """The wedge wording must not swallow the ordinary case: a second store while
    the first is genuinely in use is still refused (that is the A1 clobber guard),
    and since 'in use' and 'leaked' are indistinguishable from inside the process,
    the message has to carry the remedy for both."""
    first = MappingStore(mapping_db_path)
    try:
        with pytest.raises(MappingLockError) as excinfo:
            MappingStore(mapping_db_path, lock_timeout=0.2)
        message = str(excinfo.value)
        assert "silently erase" in message
        assert "still being processed" in message
        assert "restart the Anonymizer" in message
    finally:
        first.close(save=False)


# --- P9/P10: constructor side effects and cleanup ----------------------------


def test_opening_a_store_creates_no_mapping_file(tmp_path):
    """The constructor deliberately mkdirs the parent (the lock sidecar lives
    there) but must NEVER create the mapping itself: a zero-length mappings.db is
    now a loud corruption error, so accidentally touching one into existence would
    brick every fresh installation."""
    target = tmp_path / "nested" / "mappings.db"
    store = MappingStore(target)
    try:
        assert target.parent.is_dir()
        assert not target.exists()
    finally:
        store.close(save=False)


def test_lock_sidecar_is_never_mistaken_for_the_mapping(mapping_db_path):
    """The <db>.lock sidecar is created next to every mapping and deliberately left
    behind (a byte-range lock makes the file's existence meaningless, and deleting
    it races another process that legitimately holds a lock on that inode). Guard:
    now that mapping presence is decided by existence alone, the leftover sidecar
    must not be confused with the mapping."""
    with MappingStore(mapping_db_path) as store:
        store.get_or_create("PERSON", "Mueller", label="PERSON")

    sidecar = mapping_db_path.with_name(mapping_db_path.name + ".lock")
    assert sidecar.exists() and sidecar.stat().st_size == 0
    assert sidecar != mapping_db_path

    mapping_db_path.unlink()  # mapping deliberately deleted, sidecar left behind
    with MappingStore(mapping_db_path) as store:
        assert store.get_or_create("PERSON", "Weber", label="PERSON") == "PERSON_1"


def test_failed_open_closes_the_in_memory_connection(mapping_db_path, monkeypatch):
    """The failed-open handler releases the lock but used to leak the :memory:
    sqlite connection it had already created. Harmless today, but that handler is
    the one that has to be exhaustive -- it runs on every corrupt/keyless open."""
    _issue_two_persons(mapping_db_path)
    keyring.set_password(SERVICE, KEY_NAME, Fernet.generate_key().decode())  # wrong key

    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def spy(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(mapping_mod.sqlite3, "connect", spy)
    with pytest.raises(MappingKeyError):
        MappingStore(mapping_db_path, lock_timeout=0.2)

    assert opened, "the failing open never got as far as creating a connection"
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")
