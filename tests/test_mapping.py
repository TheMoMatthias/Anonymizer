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
