import pytest

from anonymizer.actions import one_way_token, reidentify_text, resolve_replacement
from anonymizer.mapping import MappingStore, UnreversibleTokenError


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


# --- A3: the one-way / pseudonym token namespace must not overlap ------------


def test_one_way_token_from_a_numbered_column_label_is_not_reversible(mapping_db_path):
    """Regression (ONE-WAY REDACTION LEAKED SOMEONE ELSE'S DATA): a column policy
    derives its entity type from the HEADER, so 'Notizen 2' becomes NOTIZEN_2 and
    used to render the bare one-way token [NOTIZEN_2] -- exactly the pseudonym a
    PSEUDONYMIZED 'Notizen' column mints for its second value. Re-identifying that
    document replaced the ONE-WAY redacted cell with another row's REAL note."""
    with MappingStore(mapping_db_path) as store:
        resolve_replacement("NOTIZEN", "erste Notiz", "pseudonymize", store)
        assert resolve_replacement("NOTIZEN", "zweite Notiz", "pseudonymize", store) == "[NOTIZEN_2]"

        token = resolve_replacement("NOTIZEN_2", "streng vertraulich", "anonymize", store)
        restored, n = reidentify_text(f"Zelle: {token}", store)
        assert n == 0, f"one-way token {token} was reversed -> {restored!r}"
        assert "zweite Notiz" not in restored


def test_one_way_token_from_a_person_numbered_label_is_not_reversible(mapping_db_path):
    """Same class of bug via a column headed 'Person 1' -> [PERSON_1], which
    reversed to the first real detected person."""
    with MappingStore(mapping_db_path) as store:
        resolve_replacement("PERSON", "Hans Mueller", "pseudonymize", store)

        token = resolve_replacement("PERSON_1", "irgendein Text", "anonymize", store)
        restored, n = reidentify_text(f"Zelle: {token}", store)
        assert n == 0, f"one-way token {token} was reversed -> {restored!r}"
        assert "Hans Mueller" not in restored


def test_ordinary_labels_keep_the_plain_readable_one_way_token(mapping_db_path):
    """The disambiguation must cost nothing for the overwhelming majority of
    labels -- readable tokens were the whole point of header-derived types."""
    with MappingStore(mapping_db_path) as store:
        assert resolve_replacement("PERSON", "Hans Mueller", "anonymize", store) == "[PERSON]"
        assert resolve_replacement("IBAN_CODE", "DE89", "anonymize", store) == "[IBAN]"
        assert resolve_replacement("PROJEKT", "Alpha", "anonymize", store) == "[PROJEKT]"
        assert one_way_token("KUNDEN_NR") == "[KUNDEN_NR]"  # digits-free tail: unambiguous


def test_pseudonyms_of_a_numbered_label_still_round_trip(mapping_db_path):
    """A PSEUDONYMIZED 'Notizen 2' column must keep working and must not collide
    with the 'Notizen' column's tokens -- both are reversible, to their own data."""
    with MappingStore(mapping_db_path) as store:
        resolve_replacement("NOTIZEN", "erste Notiz", "pseudonymize", store)
        resolve_replacement("NOTIZEN", "zweite Notiz", "pseudonymize", store)
        token = resolve_replacement("NOTIZEN_2", "andere Spalte", "pseudonymize", store)
        assert token == "[NOTIZEN_2_1]"

        restored, n = reidentify_text(f"A {token} B [NOTIZEN_2]", store)
        assert restored == "A andere Spalte B zweite Notiz"
        assert n == 2


# --- P6 backstop: never mint a pseudonym re-identification cannot match ------


def test_umlaut_label_pseudonym_fails_loud_instead_of_becoming_unreversible(mapping_db_path):
    """Regression (PERMANENTLY UNREVERSIBLE DOCUMENT, silently): a pseudonymized
    column headed 'Prüfung' minted PRÜFUNG_1 and wrote [PRÜFUNG_1] into the output,
    but TOKEN_RE's label class is [A-Z0-9_] and can never match an umlaut. The
    document could never be re-identified again, and the interface reported
    "Restored 0 value(s)." as a cheerful INFO toast -- the failure was invisible
    and only discovered years later, when someone needed the original value."""
    with MappingStore(mapping_db_path) as store:
        with pytest.raises(UnreversibleTokenError) as excinfo:
            resolve_replacement("PRÜFUNG", "Wert", "pseudonymize", store)
        assert "PRÜFUNG_1" in str(excinfo.value)
        assert store.entry_count() == 0, "an unusable row was left in the mapping"


def test_label_outside_the_token_alphabet_fails_loud_too(mapping_db_path):
    """Same guarantee for every other character outside the token alphabet, not
    just umlauts: a config-defined entity type carrying brackets renders
    [PROJEKT_[A]_1], where re-identification matches only the inner [A] and
    restores nothing at all."""
    with MappingStore(mapping_db_path) as store:
        for entity in ("PROJEKT_[A]", "Projekt", "PROJEKT KURZ", "PROJEKT-A"):
            with pytest.raises(UnreversibleTokenError):
                resolve_replacement(entity, "eins", "pseudonymize", store)


def test_every_pseudonym_that_survives_the_guard_really_round_trips(mapping_db_path):
    """The positive half of the backstop: whatever it lets through must genuinely
    reverse, including the awkward labels that carry their own digits."""
    with MappingStore(mapping_db_path) as store:
        for entity in ("PERSON", "DE_BIC2", "NOTIZEN_2", "KUNDEN_NR", "COLUMN_AB"):
            token = resolve_replacement(entity, f"wert-{entity}", "pseudonymize", store)
            restored, n = reidentify_text(f"A {token} B", store)
            assert n == 1, f"{token} did not round-trip"
            assert restored == f"A wert-{entity} B"


def test_one_way_token_for_an_umlaut_label_is_allowed_and_never_reversed(mapping_db_path):
    """The asymmetry is deliberate. A ONE-WAY token is not supposed to round-trip,
    so a label TOKEN_RE cannot match fails SAFE there: re-identification simply
    leaves it alone, which is exactly the required behaviour. Refusing it would
    block a document that is entirely correct."""
    with MappingStore(mapping_db_path) as store:
        token = resolve_replacement("PRÜFUNG", "streng vertraulich", "anonymize", store)
        assert token == "[PRÜFUNG]"
        restored, n = reidentify_text(f"Zelle: {token}", store)
        assert n == 0
        assert restored == f"Zelle: {token}"
