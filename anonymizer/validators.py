"""Format/checksum validators for structured identifiers.

A validated ID is near-certainly a real one, so detection can promote it to the
high trust tier (auto-accept). An ID that matched a permissive numeric pattern
but fails its checksum is almost certainly a false positive and can be demoted.
This is how we get recall-first detection (wide nets) without drowning the
reviewer in noise -- the maths does the filtering, not the human.

All validators are pure, offline, and side-effect free.
"""

from __future__ import annotations

import re

# Maps an entity type to its validator. Types absent here have no checksum
# (e.g. account/deposit numbers carry no universal check digit) and are left to
# context-based scoring.
_DIGITS = re.compile(r"\d")


def _only_digits(value: str) -> str:
    return "".join(_DIGITS.findall(value))


def luhn_valid(value: str) -> bool:
    """Luhn (mod-10) checksum -- credit cards and many account schemes."""
    digits = _only_digits(value)
    if len(digits) < 12:
        return False
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        d = ord(ch) - 48
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def iban_valid(value: str) -> bool:
    """ISO 13616 IBAN mod-97 checksum. Ignores spaces/case."""
    iban = re.sub(r"\s+", "", value).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", iban):
        return False
    rearranged = iban[4:] + iban[:4]
    numeric = "".join(str(ord(c) - 55) if c.isalpha() else c for c in rearranged)
    try:
        return int(numeric) % 97 == 1
    except ValueError:
        return False


_ISO_3166_ALPHA2 = frozenset(
    "AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS "
    "BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE "
    "EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM "
    "HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC "
    "LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA "
    "NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW "
    "SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO "
    "TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW".split()
)
_BIC_RE = re.compile(r"[A-Z]{4}([A-Z]{2})[A-Z0-9]{2}(?:[A-Z0-9]{3})?")


def bic_valid(value: str) -> bool:
    """ISO 9362 BIC/SWIFT shape (8 or 11 chars) with a real ISO-3166 country
    code in positions 5-6. The country-code gate is what separates a genuine BIC
    from an 8-letter uppercase word, keeping the completeness backstop low-noise.
    Not wired into VALIDATORS on purpose: a BIC-shaped English word could still
    carry a valid country code, so BICs are surfaced for review, never auto-
    accepted on shape alone."""
    m = re.fullmatch(_BIC_RE, value.strip().upper())
    return bool(m and m.group(1) in _ISO_3166_ALPHA2)


def de_steuer_id_valid(value: str) -> bool:
    """German tax ID (steuerliche Identifikationsnummer) -- 11 digits with an
    ISO 7064 MOD 11,10 check digit. Also enforces the structural rule that the
    first ten digits contain exactly one repeated digit (appearing 2 or 3
    times), which rules out sequential/round test numbers."""
    digits = _only_digits(value)
    if len(digits) != 11 or digits[0] == "0":
        return False

    first_ten = digits[:10]
    counts = {d: first_ten.count(d) for d in set(first_ten)}
    repeated = [d for d, c in counts.items() if c >= 2]
    # Exactly one digit repeats (2 or 3 times); no digit appears more than 3x.
    if len(repeated) != 1 or any(c > 3 for c in counts.values()):
        return False

    product = 10
    for ch in first_ten:
        s = (int(ch) + product) % 10
        if s == 0:
            s = 10
        product = (s * 2) % 11
    check = (11 - product) % 10
    return check == int(digits[10])


# German Sozialversicherungsnummer (DRV Versicherungsnummer), 12 characters:
#   BB TTMMJJ A SS P
#   Bereichsnummer(2) Geburtsdatum(6, TTMMJJ) Anfangsbuchstabe(1) Serie(2) Prüfziffer(1)
_SV_RE = re.compile(r"(\d{2})(\d{6})([A-Z])(\d{2})(\d)")
# Official DRV weighting, applied to the 12 digits you get after replacing the
# letter with its two-digit ordinal (A=01 ... Z=26). Twelve weights, not ten --
# the letter expands to TWO digits, which is easy to miss and yields a validator
# that rejects every real number.
_SV_WEIGHTS = (2, 1, 2, 5, 7, 1, 2, 1, 2, 1, 2, 1)


def de_sv_nummer_valid(value: str) -> bool:
    """German social-security number checksum (mod 10 over the digit sums of the
    weighted digits), plus the two structural rules that make a CONTEXT-FREE
    match safe: the embedded DDMMYY must be a possible date, and the
    Bereichsnummer must be in the issued 02-89 band. Together these are what let
    a bare SV number in its own spreadsheet column be trusted without a nearby
    "SV-Nummer" label -- which is the only shape it has in an HR workbook."""
    compact = re.sub(r"[\s.\-/]", "", value).upper()
    m = _SV_RE.fullmatch(compact)
    if not m:
        return False
    bereich, birth, letter, serial, check = m.groups()
    if not 2 <= int(bereich) <= 89:
        return False
    day, month = int(birth[:2]), int(birth[2:4])
    if not (1 <= day <= 31 and 1 <= month <= 12):
        return False
    # Letter -> two digits, giving the 12-digit body the weights apply to.
    body = bereich + birth + f"{ord(letter) - 64:02d}" + serial
    # Quersumme of each product: products are at most 63, so divmod(_, 10) is it.
    total = sum(sum(divmod(int(d) * w, 10)) for d, w in zip(body, _SV_WEIGHTS))
    return total % 10 == int(check)


# entity_type -> validator callable. BIC_CODE is deliberately NOT here: putting
# it in VALIDATORS would let _refine promote any BIC-shaped word with a valid
# country-code substring (DOKUMENT -> "ME", Anfragen -> "AG") to auto-accept,
# bypassing the context gate. BICs stay context-gated; bic_valid is used only by
# the low-noise completeness backstop.
VALIDATORS = {
    "IBAN_CODE": iban_valid,
    "CREDIT_CARD": luhn_valid,
    "DE_STEUER_ID": de_steuer_id_valid,
    "DE_SV_NUMMER": de_sv_nummer_valid,
}


def validate(entity_type: str, value: str) -> bool | None:
    """Returns True/False if a checksum applies to this entity type, else None
    (no validator -- caller should fall back to the raw confidence score)."""
    validator = VALIDATORS.get(entity_type)
    if validator is None:
        return None
    return validator(value)
