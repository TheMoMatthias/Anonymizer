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
