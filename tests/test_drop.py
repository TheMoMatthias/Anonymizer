"""Native drag-drop plumbing. The native window itself can't be driven
headlessly, but the app-side chain (event -> buffer -> drain) is pure and
testable. This proves everything AFTER the OS hands us a path works; only the
pywebview path-capture itself needs a real window.
"""

from types import SimpleNamespace

from anonymizer.gui import app as gui_app


def _reset():
    with gui_app._drop_lock:
        gui_app._pending_drops.clear()
        gui_app._drop_stats.update(events=0, empty_events=0, last_seen_empty=0)


def test_drop_event_buffers_paths():
    _reset()
    gui_app._handle_native_drop(SimpleNamespace(args={"files": ["C:/a.docx", "C:/b.pdf", ""]}))
    paths, empty = gui_app._take_pending_drops()
    assert paths == ["C:/a.docx", "C:/b.pdf"]  # empty string filtered out
    assert empty is False
    # buffer is drained
    again, _ = gui_app._take_pending_drops()
    assert again == []


def test_empty_drop_is_flagged_once():
    _reset()
    gui_app._handle_native_drop(SimpleNamespace(args={"files": [""]}))
    paths, empty = gui_app._take_pending_drops()
    assert paths == []
    assert empty is True
    # the empty flag is edge-triggered: a second drain without a new event is False
    _paths, empty2 = gui_app._take_pending_drops()
    assert empty2 is False


def test_drop_handler_never_raises_on_bad_event():
    _reset()
    gui_app._handle_native_drop(SimpleNamespace())  # no .args
    gui_app._handle_native_drop(SimpleNamespace(args={}))  # no files key
    paths, _ = gui_app._take_pending_drops()
    assert paths == []
