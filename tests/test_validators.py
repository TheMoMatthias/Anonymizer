from anonymizer.validators import de_steuer_id_valid, iban_valid, luhn_valid, validate


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


def test_validate_dispatch():
    assert validate("IBAN_CODE", "DE89370400440532013000") is True
    assert validate("DE_STEUER_ID", "12345678901") is False
    assert validate("PERSON", "Hans Mueller") is None  # no checksum applies
