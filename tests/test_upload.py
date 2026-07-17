"""File intake via ui.upload. A dropped/uploaded file arrives as BYTES (a browser
never exposes the real OS path), so the app writes a working copy under the
ORIGINAL name and feeds that into the pipeline; anonymized output is routed to a
fixed folder. These tests cover the pure, headless parts of that path -- byte
persistence and output routing -- without needing a live webview.
"""

from pathlib import Path

import pytest

from anonymizer.gui import app as gui_app
from anonymizer.pipeline import apply_document, output_path_for, scan_document


@pytest.fixture
def work_dir(tmp_path, monkeypatch):
    """Point the module's upload working dir at a pytest tmp dir so nothing leaks
    into the system temp, and reset it afterwards."""
    d = tmp_path / "work"
    d.mkdir()
    monkeypatch.setattr(gui_app, "_upload_dir", d)
    return d


def test_persist_upload_writes_bytes_under_original_name(work_dir):
    path = gui_app._persist_upload("Kundenliste.docx", b"payload-bytes")
    assert path is not None
    assert path.parent == work_dir
    assert path.name == "Kundenliste.docx"
    assert path.read_bytes() == b"payload-bytes"


def test_persist_upload_rejects_unsupported_extension(work_dir):
    assert gui_app._persist_upload("malware.exe", b"x") is None
    assert gui_app._persist_upload("notes.txt", b"x") is None
    # nothing was written for the rejected files
    assert list(work_dir.iterdir()) == []


def test_persist_upload_uniquifies_same_name(work_dir):
    a = gui_app._persist_upload("Kunde.docx", b"first")
    b = gui_app._persist_upload("Kunde.docx", b"second")
    assert a is not None and b is not None
    assert a != b, "second same-named upload must not overwrite the first"
    assert a.read_bytes() == b"first"
    assert b.read_bytes() == b"second"


def test_persist_upload_strips_path_separators(work_dir):
    # An uploaded name must never escape the work dir via a path separator.
    path = gui_app._persist_upload(r"..\..\evil\Kunde.docx", b"x")
    assert path is not None
    assert path.parent == work_dir
    assert path.name == "Kunde.docx"


def test_output_path_for_default_is_next_to_source():
    src = Path(r"C:\docs\Brief.docx")
    assert output_path_for(src) == Path(r"C:\docs\Brief_psd.docx")


def test_output_path_for_routes_to_out_dir(tmp_path):
    src = tmp_path / "src" / "Brief.docx"
    out = tmp_path / "Anonymized"
    out.mkdir()
    assert output_path_for(src, out) == out / "Brief_psd.docx"


def test_output_path_for_uniquifies_in_out_dir(tmp_path):
    src = tmp_path / "Brief.docx"
    out = tmp_path / "Anonymized"
    out.mkdir()
    (out / "Brief_psd.docx").write_text("existing")  # a distinct prior output
    assert output_path_for(src, out) == out / "Brief_psd(2).docx"


def test_apply_document_writes_into_out_dir(analyzer, base_config, sample_docx, tmp_path):
    """The end-to-end save must land the anonymized copy in the fixed output
    folder (created if missing), not next to the temp working file."""
    out_dir = tmp_path / "Anonymized"  # deliberately does not exist yet
    mapping_db = tmp_path / "m.db"
    grouped = scan_document(sample_docx, analyzer, base_config).all_actionable()

    out_path, report_path = apply_document(
        sample_docx, grouped, analyzer, base_config, mapping_db, out_dir
    )

    assert out_path.parent == out_dir
    assert out_path.exists()
    assert out_path.name == "sample_psd.docx"
    assert Path(report_path).exists()
