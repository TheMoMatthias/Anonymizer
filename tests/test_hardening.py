"""Second-wave hardening: XXE-safe XML parsing and the short-deny-term backstop."""

from lxml import etree

from anonymizer import xmlsafe
from anonymizer.pipeline import _literal_residual


def test_xmlsafe_blocks_entity_expansion():
    """Untrusted document XML must not expand entities (billion-laughs DoS / local
    file inclusion). Either the entity is left unresolved or the parse is rejected
    -- never expanded."""
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY a "AAAAAAAA">]>'
        "<root>&a;</root>"
    ).encode("utf-8")
    try:
        tree = xmlsafe.fromstring(bomb)
    except etree.XMLSyntaxError:
        return  # rejected outright -> safe
    assert "AAAAAAAA" not in "".join(tree.itertext()), "entity was expanded"


def test_literal_residual_verifies_short_deny_terms(tmp_path):
    """A <4-char value is normally skipped by the backstop (avoids false hits on
    common substrings), but a user-asserted deny term must be verified regardless
    of length."""
    out = tmp_path / "out.txt"
    out.write_text("this still contains ng somewhere", encoding="utf-8")

    assert _literal_residual(out, ["ng"]) == []  # skipped: too short, not a deny term
    assert _literal_residual(out, ["ng"], always_check=["ng"]) == ["ng"]  # deny term -> checked


def test_literal_residual_ignores_values_inside_replacement_tokens(tmp_path):
    """A removed value that survives ONLY as a substring of the tool's OWN
    replacement tokens is not a leak -- it is the anonymized output. Regression
    (spurious HARD-FAIL): an NER-misflagged header word 'Kundennr' (removed as a
    LOCATION) is a substring of the [KUNDENNR_n] tokens that replaced the customer
    NUMBERS, so an unmasked substring scan reported a phantom leak and the fail-loud
    gate refused to write ANY output file."""
    out = tmp_path / "out.txt"
    out.write_text("[KUNDENNR_1] [KUNDENNR_2] [LOCATION_3] anonymisiert", encoding="utf-8")
    assert _literal_residual(out, ["Kundennr"]) == []


def test_literal_residual_still_catches_leak_outside_a_token(tmp_path):
    """The token mask must NOT hide a genuine leak: a removed value present in the
    body (not only inside a replacement token) is still reported."""
    out = tmp_path / "out.txt"
    out.write_text("Herr Mueller [PERSON_1] traf Mueller erneut", encoding="utf-8")
    assert _literal_residual(out, ["Mueller"]) == ["Mueller"]


def test_literal_residual_no_phantom_across_spreadsheet_cells(tmp_path):
    """Regression (spurious HARD-FAIL): _output_text_blob concatenated adjacent
    cells' <v> text with no separator, so two unrelated cells -- or the shared-
    string INDICES that string cells store in <v> -- glued into a phantom digit-run
    that matched a removed customer number and refused to write ANY output. Each
    independent cell must be delimited; text WITHIN a cell must still be contiguous."""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = 1000
    ws["B1"] = 20  # "1000" + "20" would glue into the phantom "100020" across cells
    ws["A2"] = "Kundengeheim"  # a real value living contiguously inside one cell
    path = tmp_path / "phantom.xlsx"
    wb.save(path)

    assert _literal_residual(path, ["100020"]) == []  # phantom across A1|B1 -> not a leak
    assert _literal_residual(path, ["Kundengeheim"]) == ["Kundengeheim"]  # real -> still caught
