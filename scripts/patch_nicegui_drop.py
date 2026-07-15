"""Patches the installed nicegui package so native-mode drag-and-drop
captures a real file path on Windows.

Root cause chain (all verified empirically via debug logging, not just by
reading source):

1. pywebview 6.2.1's Windows/WebView2 backend captures a dropped file's real
   path natively (webview/platforms/edgechromium.py's FilesDropped handler)
   into `webview.dom._dnd_state['paths']`, as (basename, full_path) pairs.
2. nicegui's own drop binding (nicegui/native/native_mode.py) ignores that
   and instead reads a `pywebviewFullPath` property that this pywebview
   version never actually sets on the JS-side dropped-file object -- every
   drop silently yielded an empty path (verified: the property is absent
   from the installed pywebview source entirely).
3. A first fix correlated the JS drop event's filenames against
   _dnd_state['paths'] by basename with a 1s retry window -- but logging
   showed this races across threads: the native capture and the JS event
   are processed on different threads, and the JS-side handler's retry loop
   could run its entire 1s window without ever observing an entry that the
   native handler demonstrably did populate around the same time.

This version sidesteps the race entirely: instead of correlating two
independent, racy event sources, a dedicated poller thread watches
_dnd_state['paths'] directly and fires as soon as anything lands there,
decoupled from the JS event's timing. A no-op JS drop listener is still
registered because pywebview's native capture is gated on
_dnd_state['num_listeners'] > 0.

A runtime monkeypatch of nicegui.native.native_mode from application code
was tried first and verified (via debug logging) NOT to reach the separate
process NiceGUI spawns for the native window -- only patching the installed
file itself guarantees the fix applies in whichever process imports it.

This version also writes diagnostic entries to
%LOCALAPPDATA%\\Anonymizer\\dnd_debug.log at every stage of the drop pipeline,
since several real drag-and-drop attempts failed with no observable cause
otherwise -- the log is the only way to see which stage breaks on a given
machine.

Run after every `uv sync` / fresh install (setup.ps1 and
build_offline_bundle.ps1 both call this automatically). Idempotent: safe to
run multiple times, and safe against a `uv sync` that restores the original
file, since it re-detects and re-applies the fix each time.

NOTE: if you ever need to force a truly pristine reinstall of nicegui (e.g.
to test against upstream source again), run `uv cache clean nicegui` FIRST
-- uv's package cache can retain a previously-patched copy of a file across
`--reinstall-package`, so `--reinstall-package` alone is not sufficient.
"""

from __future__ import annotations

import sys

ORIGINAL_BIND_DROP = '''    def bind_drop() -> None:
        window.evaluate_js(\'\'\'
            document.addEventListener("dragover", function(e) {
              if (e.dataTransfer && e.dataTransfer.types.indexOf("Files") >= 0) e.preventDefault();
            });
        \'\'\')
        window.dom.document.events.drop += \\
            webview.dom.DOMEventHandler(lambda e: send('drop', files=[  # type: ignore[arg-type]
                file_.get('pywebviewFullPath', '') for file_ in e.get('dataTransfer', {}).get('files', [])
            ]), True, False)
'''

# Presence of this marker means the (correct, poller-based) fix is already applied.
FIXED_BIND_DROP_MARKER = "_poll_dnd_state"

FIXED_BIND_DROP = '''    def bind_drop() -> None:
        import os as _os
        import threading as _threading
        import time as _time

        def _anonymizer_dnd_log(msg):
            try:
                log_dir = _os.path.join(_os.environ.get('LOCALAPPDATA', _os.path.expanduser('~')), 'Anonymizer')
                _os.makedirs(log_dir, exist_ok=True)
                with open(_os.path.join(log_dir, 'dnd_debug.log'), 'a', encoding='utf-8') as _f:
                    _f.write('%s pid=%s %s\\n' % (_time.strftime('%H:%M:%S'), _os.getpid(), msg))
            except OSError:
                pass

        _anonymizer_dnd_log('bind_drop() called (window loaded)')
        window.evaluate_js(\'\'\'
            document.addEventListener("dragover", function(e) {
              if (e.dataTransfer && e.dataTransfer.types.indexOf("Files") >= 0) e.preventDefault();
            });
        \'\'\')

        # The JS-side drop event and the native WebView2 FilesDropped capture
        # (which lands in webview.dom._dnd_state['paths']) race across
        # threads -- correlating them by filename inside the JS event handler
        # unpredictably missed the native data even with a 1s retry window.
        # Decoupled: a dedicated poller watches _dnd_state['paths'] directly
        # and fires as soon as anything lands there, independent of the JS
        # event's timing. A no-op JS listener is still registered so
        # _dnd_state['num_listeners'] stays > 0 (gates the native capture).
        def _noop_drop(e):
            pass

        window.dom.document.events.drop += \\
            webview.dom.DOMEventHandler(_noop_drop, True, False)
        _anonymizer_dnd_log('dom.document.events.drop (noop) registered, starting dnd_state poller')

        def _poll_dnd_state():
            from webview.dom import _dnd_state
            while True:
                if _dnd_state['paths']:
                    batch = _dnd_state['paths'][:]
                    _dnd_state['paths'] = []
                    _anonymizer_dnd_log('poller found paths=%r' % (batch,))
                    send('drop', files=[full for _base, full in batch])
                _time.sleep(0.1)

        _threading.Thread(target=_poll_dnd_state, daemon=True).start()
'''


def main() -> int:
    import nicegui.native.native_mode as native_mode
    from pathlib import Path

    path = Path(native_mode.__file__)
    text = path.read_text(encoding="utf-8")

    if FIXED_BIND_DROP_MARKER in text:
        print(f"patch_nicegui_drop: already applied (poller-based) to {path}")
        return 0

    if ORIGINAL_BIND_DROP not in text:
        print(
            f"patch_nicegui_drop: expected original bind_drop() text not found in {path} "
            "(nicegui version may have changed, or an older patch version is applied -- "
            "run `uv cache clean nicegui` and reinstall for a truly pristine copy) -- skipping"
        )
        return 1

    path.write_text(text.replace(ORIGINAL_BIND_DROP, FIXED_BIND_DROP), encoding="utf-8")
    print(f"patch_nicegui_drop: applied (poller-based) to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
