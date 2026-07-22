import openpyxl

from anonymizer.formats import xlsx_handler
from anonymizer.pipeline import apply_document, scan_document


def test_detects_person_including_hidden_sheet(sample_xlsx, analyzer, base_config):
    grouped = scan_document(sample_xlsx, analyzer, base_config).all_actionable()
    assert any(g.entity_type == "PERSON" for g in grouped)
    units = xlsx_handler.extract_text_units(sample_xlsx)
    assert any("Hidden" in u.id for u in units)


def test_apply_replaces_cells(sample_xlsx, analyzer, base_config, mapping_db_path):
    grouped = scan_document(sample_xlsx, analyzer, base_config).all_actionable()
    for g in grouped:
        g.action = "pseudonymize"
    out_path, report_path = apply_document(sample_xlsx, grouped, analyzer, base_config, mapping_db_path)

    assert out_path.suffix == ".xlsx"
    wb = openpyxl.load_workbook(out_path)
    assert wb["Main"]["A1"].value != "Hans Mueller"
    assert wb["Hidden"]["A1"].value != "Hans Mueller"


def test_xlsm_output_has_macros_stripped(tmp_path, analyzer, base_config, mapping_db_path):
    wb = openpyxl.Workbook()
    wb.active["A1"] = "Hans Mueller"
    path = tmp_path / "sample.xlsm"
    wb.save(path)

    grouped = scan_document(path, analyzer, base_config).all_actionable()
    for g in grouped:
        g.action = "pseudonymize"
    out_path, _ = apply_document(path, grouped, analyzer, base_config, mapping_db_path)

    assert out_path.suffix == ".xlsx"


def test_xlsx_name_column_override_never_overlaps(analyzer, base_config):
    """Regression (CORRUPTION): the whole-cell name-column override was appended
    after overlap resolution, so it could partially overlap a finding NER did make
    (a KONTO number in the same cell) -> the cell splicer produced garbled tokens.
    The returned findings must be non-overlapping."""
    findings = xlsx_handler._analyze_cell_text(
        "Mueller, Konto 12345678", "Name", analyzer, {**base_config, "languages": ["de"]}
    )
    spans = sorted((f.start, f.end) for f in findings)
    for (s1, e1), (s2, e2) in zip(spans, spans[1:]):
        assert e1 <= s2, f"overlapping findings would corrupt the cell: {spans}"
    assert findings, "name-column cell yielded no findings at all"


def test_xlsx_header_straddling_span_is_clipped_not_dropped(analyzer, base_config):
    """Regression (LEAK): a finding whose span starts inside the injected 'header: '
    prefix but extends into the cell value was dropped wholesale, leaking the value.
    It must be clipped to the value side instead."""
    cfg = {**base_config, "languages": ["de"], "deny_list": ["Bemerkung: Geheimprojekt"]}
    findings = xlsx_handler._analyze_cell_text("Geheimprojekt", "Bemerkung", analyzer, cfg)
    assert any("Geheimprojekt" in f.value for f in findings), f"straddling value dropped: {findings}"


def test_xlsx_repeated_values_memoized_consistently(tmp_path, analyzer, base_config, mapping_db_path):
    """Detection/redaction is memoized per (header, cell-text) for speed on large
    'database' sheets. A value repeated across many cells and sheets must still be
    caught at EVERY occurrence, pseudonymize to the SAME token, and pass the
    fail-loud verify -- memoization must not drop or diverge any occurrence."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "A"
    ws["A1"] = "Name"
    ws2 = wb.create_sheet("B")
    ws2["A1"] = "Kunde"
    for r in range(2, 12):
        ws[f"A{r}"] = "Hans Mueller"   # 10 rows
        ws2[f"A{r}"] = "Hans Mueller"  # + 10 rows on another sheet = 20 occurrences
    path = tmp_path / "repeat.xlsx"
    wb.save(path)

    grouped = scan_document(path, analyzer, base_config).all_actionable()
    person_occurrences = sum(g.count for g in grouped if g.entity_type == "PERSON")
    assert person_occurrences >= 20, f"memoization dropped occurrences: {person_occurrences}"

    for g in grouped:
        g.action = "pseudonymize"
    out_path, _ = apply_document(path, grouped, analyzer, base_config, mapping_db_path)  # raises if verify fails
    out = openpyxl.load_workbook(out_path)
    tokens = {out["A"][f"A{r}"].value for r in range(2, 12)} | {out["B"][f"A{r}"].value for r in range(2, 12)}
    assert len(tokens) == 1, f"repeated value not consistently tokenized: {tokens}"
    assert next(iter(tokens)).startswith("[PERSON_"), f"unexpected token: {tokens}"


def test_column_summary_lists_headers_and_counts(analyzer, base_config, tmp_path):
    """Column summary reports each column's header + how many findings landed in it,
    so the reviewer can set a whole-column policy without opening the file."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["Kunde", "Projekt", "Notiz"])
    ws.append(["Hans Mueller", "Geheimprojekt Nordwind", "offen"])
    ws.append(["Petra Weber", "Fusion", "zu"])
    path = tmp_path / "cols.xlsx"
    wb.save(path)

    result = scan_document(path, analyzer, {**base_config, "languages": ["de"]})
    cols = {c.key: c for c in result.columns}
    assert cols["Data!A"].header == "Kunde" and cols["Data!A"].pii_count >= 2  # two names
    assert cols["Data!B"].header == "Projekt"
    assert cols["Data!C"].header == "Notiz"


def test_column_blackout_redacts_undetected_cells_and_verifies(analyzer, base_config, tmp_path, mapping_db_path):
    """A whole-column blackout redacts EVERY non-empty cell -- including a topic-
    sensitive cell entity detection can't judge -- tokenizes repeats consistently,
    leaves empty cells alone, and still passes the fail-loud verify."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["Kunde", "Projekt"])
    ws.append(["Hans Mueller", "Streng geheimes Vorhaben Nordwind"])
    ws.append(["Petra Weber", "Marktstrategie 2027"])
    ws.append(["Klaus Bauer", "Streng geheimes Vorhaben Nordwind"])  # repeat -> same token
    ws.append(["Anna Klein", ""])  # empty -> stays empty
    path = tmp_path / "blackout.xlsx"
    wb.save(path)

    cfg = {**base_config, "languages": ["de"]}
    grouped = scan_document(path, analyzer, cfg).all_actionable()
    for g in grouped:
        g.action = "pseudonymize"
    apply_cfg = {**cfg, "column_policies": {"Data!B": "pseudonymize"}}
    out_path, _ = apply_document(path, grouped, analyzer, apply_cfg, mapping_db_path)  # raises if verify fails

    out = openpyxl.load_workbook(out_path)["Data"]
    assert out["B2"].value.startswith("[PROJEKT_"), f"undetected cell not blacked out: {out['B2'].value!r}"
    assert out["B2"].value == out["B4"].value, "repeated column value not consistently tokenized"
    assert out["B5"].value in (None, ""), "empty cell must stay empty"
    assert out["A2"].value != "Hans Mueller", "name column must still be redacted via the value path"


def test_column_entity_type_readable_and_safe():
    assert xlsx_handler._column_entity_type("Projekt", "B") == "PROJEKT"
    assert xlsx_handler._column_entity_type("Kunden-Nr.", "A") == "KUNDEN_NR"
    assert xlsx_handler._column_entity_type("", "D") == "COLUMN_D"  # no header -> column letter


def test_name_header_re_widened_and_configurable():
    """The built-in people-header set now covers common German business headers,
    is extendable via config, and does not match non-people headers."""
    assert xlsx_handler._name_header_re().search("Projektleiter")  # widened built-in
    assert xlsx_handler._name_header_re().search("Betreuer")
    assert xlsx_handler._name_header_re().search("Verantwortlich")
    assert not xlsx_handler._name_header_re().search("Betrag")  # not a people column
    assert not xlsx_handler._name_header_re().search("Sachwalter")  # only via config...
    assert xlsx_handler._name_header_re(("Sachwalter",)).search("Sachwalter")  # ...added here


def test_xlsx_configured_name_header_claims_bare_surname(analyzer, base_config):
    """A workbook-specific header added via config['name_column_headers'] makes the
    whole cell a person -- catching a bare common-noun surname NER misses in a cell."""
    cfg = {**base_config, "languages": ["de"], "name_column_headers": ["Sachwalter"]}
    findings = xlsx_handler._analyze_cell_text("Weber", "Sachwalter", analyzer, cfg)
    assert any("Weber" in f.value for f in findings), f"configured header did not claim the cell: {findings}"
