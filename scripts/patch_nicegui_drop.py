"""Patches the installed nicegui package so native-mode drag-and-drop
captures a real file path on Windows.

Root cause (verified by reading the installed source directly): pywebview
6.2.1's Windows/WebView2 backend captures a dropped file's real path into
`webview.dom._dnd_state['paths']` (as basename/full-path pairs, populated by
a native FilesDropped handler in webview/platforms/edgechromium.py), but
nicegui's own drop binding (nicegui/native/native_mode.py) reads a
`pywebviewFullPath` property that this pywebview version never actually
sets on the JS-side dropped-file object -- so every drop silently yields an
empty path.

A runtime monkeypatch of nicegui.native.native_mode from application code
was tried and verified (via debug logging) NOT to reach the separate
process NiceGUI spawns for the native window -- only patching the installed
file itself guarantees the fix applies in whichever process imports it.

This version also writes diagnostic entries to
%LOCALAPPDATA%\\Anonymizer\\dnd_debug.log at every stage of the drop pipeline
(window loaded / listener registered / drop event fired / paths resolved),
since a real drag gesture repeatedly failed with no observable cause -- the
log is the only way to see which stage actually breaks on a given machine.

Run after every `uv sync` / fresh install (setup.ps1 and
build_offline_bundle.ps1 both call this automatically). Idempotent: safe to
run multiple times, and safe against a `uv sync` that restores the original
file, since it re-detects and re-applies the fix each time.
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

# Matches either the never-logging first fix or leaves room to re-patch cleanly.
FIXED_BIND_DROP_MARKER = "_anonymizer_dnd_log"

FIXED_BIND_DROP = '''    def bind_drop() -> None:
        import os as _os
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

        def _on_drop(e):
            from webview.dom import _dnd_state
            names = [f.get('name', '') for f in e.get('dataTransfer', {}).get('files', [])]
            _anonymizer_dnd_log('_on_drop FIRED, names=%r, dnd_state_paths=%r' % (names, _dnd_state['paths']))
            resolved = []
            deadline = _time.time() + 1.0
            for name in names:
                path = ''
                while _time.time() < deadline:
                    match = next((full for base, full in _dnd_state['paths'] if base == name), None)
                    if match is not None:
                        path = match
                        break
                    _time.sleep(0.02)
                resolved.append(path)
            _dnd_state['paths'] = [(base, full) for base, full in _dnd_state['paths'] if base not in names]
            _anonymizer_dnd_log('_on_drop resolved=%r' % (resolved,))
            send('drop', files=resolved)

        window.dom.document.events.drop += \\
            webview.dom.DOMEventHandler(_on_drop, True, False)
        _anonymizer_dnd_log('dom.document.events.drop handler registered successfully')
'''


def main() -> int:
    import nicegui.native.native_mode as native_mode
    from pathlib import Path

    path = Path(native_mode.__file__)
    text = path.read_text(encoding="utf-8")

    if FIXED_BIND_DROP_MARKER in text:
        print(f"patch_nicegui_drop: already applied (with logging) to {path}")
        return 0

    if ORIGINAL_BIND_DROP not in text:
        print(
            f"patch_nicegui_drop: expected original bind_drop() text not found in {path} "
            "(nicegui version may have changed, or an older patch version is applied) -- skipping"
        )
        return 1

    path.write_text(text.replace(ORIGINAL_BIND_DROP, FIXED_BIND_DROP), encoding="utf-8")
    print(f"patch_nicegui_drop: applied (with logging) to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
