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


def test_persist_upload_accepts_uppercase_extension(work_dir):
    # SUPPORTED_EXTENSIONS is lowercase; the check must lowercase the suffix.
    path = gui_app._persist_upload("Kunde.DOCX", b"x")
    assert path is not None
    assert path.read_bytes() == b"x"


def test_persist_upload_rejects_windows_reserved_names(work_dir):
    # NUL/CON/COM1... would hit a legacy DOS device (silent discard / hang), not a file.
    for name in ("NUL.docx", "CON.docx", "COM1.docx", "LPT1.docx", "aux.docx"):
        assert gui_app._persist_upload(name, b"x") is None, name
    assert list(work_dir.iterdir()) == []


def test_persist_upload_rejects_ads_and_trims_trailing_dots(work_dir):
    # NTFS alternate-data-stream "file:stream" is stripped to "file" -> no valid ext.
    assert gui_app._persist_upload("payload:report.docx", b"x") is None
    # Trailing dots (Windows silently normalizes them) are trimmed, name still valid.
    p = gui_app._persist_upload("Kunde.docx...", b"x")
    assert p is not None and p.name == "Kunde.docx"


def test_persist_upload_returns_none_on_write_error(work_dir, monkeypatch):
    # A failed write must return None (caller notifies) -- never a silent vanish.
    def boom(self, data):
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.write_bytes", boom)
    assert gui_app._persist_upload("Kunde.docx", b"x") is None


def test_persist_upload_uniquifies_three_times(work_dir):
    names = [gui_app._persist_upload("Kunde.docx", bytes([i])) for i in range(3)]
    assert all(n is not None for n in names)
    assert len({n.name for n in names}) == 3  # all three distinct, none clobbered


def test_persist_upload_rejects_empty_and_bare_extension(work_dir):
    assert gui_app._persist_upload("", b"x") is None
    assert gui_app._persist_upload(".docx", b"x") is None  # no stem -> suffix "" -> rejected


def test_work_dir_is_rooted_at_app_data_not_system_temp(tmp_path, monkeypatch):
    # The work dir must derive from app_data_dir()/"work" (an app-owned location),
    # NOT tempfile.mkdtemp() with no dir= (which would land in the system %TEMP%
    # that nothing sweeps). Asserting the root proves the dir= is wired correctly.
    monkeypatch.setattr(gui_app, "_upload_dir", None)
    monkeypatch.setattr(gui_app.config_mod, "app_data_dir", lambda: tmp_path)
    d = gui_app._work_dir()
    try:
        assert d.parent == tmp_path / "work", "work dir must be app_data_dir()/work/upload_*"
        assert d.name.startswith("upload_")
    finally:
        gui_app._cleanup_work_dir()


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


def test_output_path_for_uniquifies_with_legacy_ext_override(tmp_path):
    # A legacy .doc outputs as .docx; the uniquify branch must reuse the OVERRIDDEN
    # extension, not path.suffix -- else a second .doc would collide silently.
    out = tmp_path / "Anonymized"
    out.mkdir()
    (out / "Legacy_psd.docx").write_text("existing")
    assert output_path_for(tmp_path / "Legacy.doc", out) == out / "Legacy_psd(2).docx"


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


def test_apply_document_wraps_output_dir_failure_as_processing_error(
    analyzer, base_config, sample_docx, tmp_path, monkeypatch
):
    """Fail-loud contract: a failure creating the output folder must surface as
    ProcessingError, not a raw OSError escaping the pipeline."""
    from anonymizer.pipeline import ProcessingError

    grouped = scan_document(sample_docx, analyzer, base_config).all_actionable()

    def boom(self, *a, **k):
        raise OSError("read-only volume")

    monkeypatch.setattr("pathlib.Path.mkdir", boom)
    with pytest.raises(ProcessingError):
        apply_document(sample_docx, grouped, analyzer, base_config, tmp_path / "m.db", tmp_path / "nope")


def test_persist_upload_rejects_reserved_name_with_extra_dot(work_dir):
    """Regression: 'nul.x.docx' also resolves to the NUL device (Path.stem only
    strips the LAST extension), and because NUL always 'exists' the uniquify loop
    span forever. The first dot-component must be checked."""
    assert gui_app._persist_upload("nul.x.docx", b"x") is None
    assert gui_app._persist_upload("COM1.report.docx", b"x") is None
