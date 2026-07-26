"""Diagnostic findings export (2026-07-23): a CSV dump of every flagged term
with its original value + context, so detection precision can be understood and
tuned from real data. UNLIKE report.py, this intentionally contains original
values -- it is a tuning artifact, not a safe-to-share report.
"""

import csv

from anonymizer import core
from anonymizer.models import Finding, GroupedFinding, ScanResult, TextUnit

CONFIG = {
    "entities": {
        "NER_MISC": {"default_action": "pseudonymize"},
        "PERSON": {"default_action": "pseudonymize"},
        "IBAN_CODE": {"default_action": "pseudonymize"},
    },
    "tiers": {"high": 0.9, "medium": 0.5},
    # The export tests assert on the full finding set; keep the corroboration-only
    # drop policy off so the bare NER_MISC sample row is included.
    "corroboration_only": False,
}


def _result():
    findings = [
        Finding("PERSON", "Klaus Müller", 0.85, "ctx1", "u1", 0, 12, source="SpacyRecognizer"),
        Finding("PERSON", "Klaus Müller", 0.85, "ctx2", "u2", 0, 12, source="SpacyRecognizer"),
        Finding("IBAN_CODE", "DE89370400440532013000", 0.98, "ctx", "u3", 0, 22, validated=True, source="IbanRecognizer"),
        Finding("NER_MISC", "Migration", 0.85, "ctx", "u4", 0, 9, source="SpacyRecognizer"),
    ]
    result = core.build_scan_result(findings, [TextUnit("u1", "x")], CONFIG)
    result.possible_misses = [
        GroupedFinding("POSSIBLE_MISS", "AB1234567", 3, 0.0, "ctx", "skip", tier="low")
    ]
    return result


def test_export_rows_cover_actionable_and_possible_misses():
    rows = core.findings_export_rows(_result())
    buckets = {r["bucket"] for r in rows}
    assert buckets == {"flagged", "possible_miss"}
    values = {r["value"] for r in rows}
    assert "Klaus Müller" in values  # grouped: 2 occurrences -> 1 row
    assert "AB1234567" in values
    kl = next(r for r in rows if r["value"] == "Klaus Müller")
    assert kl["count"] == 2
    assert kl["is_ner_guess"] is True
    assert kl["data_class"] == "People"


def test_summary_breaks_down_by_type_and_flags_ner_guesses():
    summary = core.findings_summary(_result())
    assert summary["distinct_findings"] == 3
    assert summary["total_occurrences"] == 4  # Klaus ×2 + IBAN + Migration
    assert summary["ner_guess_findings"] == 2  # PERSON(guess) + NER_MISC(guess), not the IBAN
    assert summary["possible_misses"] == 1
    assert summary["by_entity_type"]["PERSON"] == 1


def test_write_findings_csv_roundtrips(tmp_path):
    result = _result()
    result.columns = []  # non-spreadsheet
    path = tmp_path / "flagged.csv"
    n = core.write_findings_csv(result, path)
    assert n == 4  # 3 actionable + 1 possible-miss

    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 4
    assert set(rows[0].keys()) == set(core._EXPORT_COLUMNS)
    # utf-8-sig so Excel renders the umlaut; the value must survive intact.
    assert any("Müller" in r["value"] for r in rows)


def test_write_findings_csv_appends_column_metadata(tmp_path):
    from anonymizer.models import ColumnInfo

    result = _result()
    result.columns = [ColumnInfo(sheet="Sheet", column="A", header="Verantwortlich", sample="x", pii_count=5, name_override=True)]
    path = tmp_path / "flagged.csv"
    core.write_findings_csv(result, path)
    text = path.read_text(encoding="utf-8-sig")
    assert "# columns" in text
    assert "Verantwortlich" in text
