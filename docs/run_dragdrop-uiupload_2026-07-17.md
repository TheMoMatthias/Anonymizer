# Run: Replace fragile native drag-drop with ui.upload (2026-07-17)

## Why
Native drag-drop (pywebview patch → `_dnd_state` poller → mp bridge) is fully wired
and its init half provably runs (confirmed: `bind_drop()` logs on window load), but it
keeps failing in practice and is structurally fragile: it monkeypatches an installed
dependency file (`nicegui/native/native_mode.py`) that any `uv sync` silently reverts,
and depends on a 4-hop WebView2→poller→subprocess→timer chain. User chose to rewrite.

## Decisions (user-approved, 2026-07-17)
- **Mechanism:** NiceGUI `ui.upload` — self-contained dropzone (drag + click) over HTTP
  inside the webview. No pywebview patch, no `_dnd_state`, no poller, no subprocess bridge.
  Works identically native + browser; survives `uv sync`.
- **Output location:** fixed folder `~/Documents/Anonymized/<originalname>_psd.<ext>`.
  Uploaded files are bytes with no origin folder, so output must be predictable. ALL GUI
  saves route here for consistency. Original filename preserved. Never clobber a distinct
  prior output → uniquify (`_psd(2)`) when the target name already exists in the folder.

## Contract
- `ui.upload(multiple, auto_upload, on_upload, max_file_size)` → per-file handler reads
  bytes, `_persist_upload(name, data)` writes them to a managed temp working copy under the
  ORIGINAL filename (so tokens/report/output naming stay correct), returns path or None
  (unsupported ext). Handler then calls `state.add_files([path])` (existing scan flow).
- `output_path_for(path, out_dir=None)` — out_dir set → `out_dir/<stem>_psd<ext>`, uniquified.
- `apply_document(..., out_dir=None)` — ensures `out_path.parent` exists; rest unchanged
  (atomic sibling-temp write + verify + os.replace still hold).
- `_save_job` passes `out_dir=anonymized_dir()`.

## Removed
- app.py native-drop plumbing: `_drop_lock/_pending_drops/_drop_stats`,
  `_handle_native_drop`, `app.native.on("drop")`, `_take_pending_drops`,
  `drop_patch_status`, the `drain_drops` timer, the "patch not active" banner,
  the hand-rolled `dz` dropzone + drag handlers, tkinter `_pick_files`/browse.
- setup.ps1 / build_offline_bundle.ps1: the `patch_nicegui_drop.py` invocation.
- `scripts/patch_nicegui_drop.py` (dead), `tests/test_drop.py` (rewritten → test_upload.py).
- NOT touched: the installed venv's already-patched `native_mode.py` is left as-is —
  stock NiceGUI also registers a document-level drop listener with preventDefault, so
  the leftover patch cannot interfere with ui.upload any differently than stock, and a
  `uv sync` restores stock. Editing a dependency file is exactly the fragility removed.

## Tests
- `tests/test_upload.py`: `_persist_upload` writes bytes under original name, rejects
  unsupported ext, uniquifies name collisions; `output_path_for(out_dir=...)` routing +
  uniquification; `apply_document` writes into out_dir.

## Done-when
- Full pytest suite green; app launches; page shows the ui.upload dropzone and NO
  "patch not active" banner; a persisted upload feeds scan; save writes to
  Documents\Anonymized. Verified by unit tests + a headless launch/render check.

## Status: DONE
- 81 tests green (test_upload.py added, test_drop.py removed).
- App boots clean (`NiceGUI ready`, HTTP 200, no tracebacks after window load).
- QUploader restyled to the app's dashed-dropzone look (theme.py).
- Verified by unit tests + headless launch. The live drag-and-drop *gesture* itself
  is the one thing only the user can confirm on a real desktop — but it now rides on
  a standard, self-contained NiceGUI component instead of a patched dependency.
