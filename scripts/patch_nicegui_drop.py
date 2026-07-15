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

FIXED_BIND_DROP = '''    def bind_drop() -> None:
        window.evaluate_js(\'\'\'
            document.addEventListener("dragover", function(e) {
              if (e.dataTransfer && e.dataTransfer.types.indexOf("Files") >= 0) e.preventDefault();
            });
        \'\'\')

        def _on_drop(e):
            import time as _time
            from webview.dom import _dnd_state
            names = [f.get('name', '') for f in e.get('dataTransfer', {}).get('files', [])]
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
            send('drop', files=resolved)

        window.dom.document.events.drop += \\
            webview.dom.DOMEventHandler(_on_drop, True, False)
'''


def main() -> int:
    import nicegui.native.native_mode as native_mode
    from pathlib import Path

    path = Path(native_mode.__file__)
    text = path.read_text(encoding="utf-8")

    if FIXED_BIND_DROP in text:
        print(f"patch_nicegui_drop: already applied to {path}")
        return 0

    if ORIGINAL_BIND_DROP not in text:
        print(
            f"patch_nicegui_drop: expected original bind_drop() text not found in {path} "
            "(nicegui version may have changed) -- skipping, drop will fall back to no-op"
        )
        return 1

    path.write_text(text.replace(ORIGINAL_BIND_DROP, FIXED_BIND_DROP), encoding="utf-8")
    print(f"patch_nicegui_drop: applied to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
