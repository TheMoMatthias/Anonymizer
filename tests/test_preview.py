from anonymizer import core
from anonymizer.models import Finding, FileJob, TextUnit

CONFIG = {
    "entities": {
        "PERSON": {"default_action": "pseudonymize"},
        "IBAN_CODE": {"default_action": "pseudonymize"},
        "CREDIT_CARD": {"default_action": "anonymize"},
    },
    "tiers": {"high": 0.9, "medium": 0.5},
}


def _result():
    text = "Hans Mueller IBAN DE89370400440532013000 card 4111111111111111"
    units = [TextUnit("u1", text)]
    findings = [
        Finding("PERSON", "Hans Mueller", 0.85, "ctx", "u1", 0, 12),
        Finding("IBAN_CODE", "DE89370400440532013000", 0.98, "ctx", "u1", 18, 40, validated=True),
        Finding("CREDIT_CARD", "4111111111111111", 0.98, "ctx", "u1", 46, 62, validated=True),
    ]
    return core.build_scan_result(findings, units, CONFIG)


def test_preview_reflects_actions_and_tokens():
    result = _result()
    for g in result.all_actionable():
        if g.entity_type == "PERSON":
            g.action = "skip"  # skipped -> omitted from preview

    preview = core.build_preview(result.groups)
    flat = {r.entity_type: r for pg in preview for r in pg.rows}

    assert "PERSON" not in flat  # skipped
    assert flat["IBAN_CODE"].token == "[IBAN_#]"  # pseudonymize -> numbered template
    assert flat["CREDIT_CARD"].token == "[CARD]"  # anonymize -> bare label


def test_preview_empty_when_all_skipped():
    result = _result()
    for g in result.all_actionable():
        g.action = "skip"
    assert core.build_preview(result.groups) == []


def test_filejob_name():
    job = FileJob(path=r"C:\docs\Report.docx")
    assert job.name == "Report.docx"
    assert job.status == "pending"
