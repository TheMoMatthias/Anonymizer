from anonymizer.mapping import MappingStore


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
