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
