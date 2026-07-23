from anonymizer.validators import (
    de_steuer_id_valid,
    de_sv_nummer_valid,
    iban_valid,
    luhn_valid,
    validate,
)


def test_iban_checksum():
    assert iban_valid("DE89370400440532013000")
    assert iban_valid("DE89 3704 0044 0532 0130 00")  # spaces ignored
    assert not iban_valid("DE89370400440532013001")  # wrong check digits
    assert not iban_valid("not an iban")


def test_luhn():
    assert luhn_valid("4111111111111111")  # test Visa
    assert not luhn_valid("4111111111111112")
    assert not luhn_valid("123")  # too short


def test_steuer_id_check_digit():
    assert de_steuer_id_valid("86095742719")
    assert de_steuer_id_valid("86 095 742 719")
    assert not de_steuer_id_valid("12345678901")  # no repeated digit / bad check
    assert not de_steuer_id_valid("8609574271")  # only 10 digits


def test_sv_nummer_check_digit():
    """German Sozialversicherungsnummer (DRV Versicherungsnummer). Without a
    working check digit the SV recognizer can only fire next to a context word,
    so a bare SV number in its own spreadsheet column -- the normal shape in an
    HR/database workbook -- leaks entirely.

    Reference vector 65170839J003: Bereich 65, geb. 17.08.39, Anfangsbuchstabe
    J (-> 10), Serie 00, Prüfziffer 3. Weights 2,1,2,5,7,1,2,1,2,1,2,1 over the
    12 letter-expanded digits, sum of the products' digit sums mod 10 = 3.
    """
    assert de_sv_nummer_valid("65170839J003")
    assert de_sv_nummer_valid("65 170839 J 003")  # spaces/format ignored
    assert de_sv_nummer_valid("53020466A043")
    assert not de_sv_nummer_valid("65170839J004")  # check digit off by one
    assert not de_sv_nummer_valid("65170839K003")  # different letter -> new sum
    assert not de_sv_nummer_valid("65170839J00")  # too short


def test_sv_nummer_rejects_impossible_birth_date():
    """The embedded DDMMYY is a hard structural rule, and it is what keeps a
    standalone (context-free) match precise: 65321339J002 carries a VALID check
    digit but claims the 32nd of month 13, so it is not an SV number."""
    assert not de_sv_nummer_valid("65321339J002")


def test_validate_dispatch():
    assert validate("IBAN_CODE", "DE89370400440532013000") is True
    assert validate("DE_STEUER_ID", "12345678901") is False
    assert validate("DE_SV_NUMMER", "65170839J003") is True
    assert validate("DE_SV_NUMMER", "65170839J004") is False
    assert validate("PERSON", "Hans Mueller") is None  # no checksum applies
