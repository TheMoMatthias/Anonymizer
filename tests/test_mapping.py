import keyring
import pytest

from anonymizer.mapping import KEY_NAME, MappingStore


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
