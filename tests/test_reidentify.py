from anonymizer.actions import reidentify_text, resolve_replacement
from anonymizer.mapping import MappingStore


def test_reidentify_round_trip(mapping_db_path):
    with MappingStore(mapping_db_path) as store:
        token = resolve_replacement("PERSON", "Hans Mueller", "pseudonymize", store)
        assert token == "[PERSON_1]"
        text = f"Der Kunde {token} hat angerufen."
        restored, n = reidentify_text(text, store)
        assert restored == "Der Kunde Hans Mueller hat angerufen."
        assert n == 1


def test_reidentify_ignores_unknown_and_anonymized_tokens(mapping_db_path):
    with MappingStore(mapping_db_path) as store:
        store.get_or_create("PERSON", "Hans Mueller", label="PERSON")
        text = "[PERSON_1] und [IBAN] und [PERSON_99]"
        restored, n = reidentify_text(text, store)
        assert restored == "Hans Mueller und [IBAN] und [PERSON_99]"
        assert n == 1


def test_label_based_placeholder_numbering(mapping_db_path):
    with MappingStore(mapping_db_path) as store:
        assert store.get_or_create("IBAN_CODE", "DE1", label="IBAN") == "IBAN_1"
        assert store.get_or_create("IBAN_CODE", "DE2", label="IBAN") == "IBAN_2"
        assert store.get_or_create("IBAN_CODE", "DE1", label="IBAN") == "IBAN_1"  # stable


def test_erase_and_reset(mapping_db_path):
    with MappingStore(mapping_db_path) as store:
        p = store.get_or_create("PERSON", "Hans Mueller", label="PERSON")
        assert store.reverse(p) == "Hans Mueller"
        assert store.erase(p) is True
        assert store.reverse(p) is None
        store.get_or_create("PERSON", "Petra Schmidt", label="PERSON")
        assert store.entry_count() == 1
        store.reset()
        assert store.entry_count() == 0
