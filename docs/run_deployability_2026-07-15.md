# Run: deployability (2026-07-15)

Persisted spec/autonomy-contract from the deployability grill. See conversation history for full rationale.

## Decisions
- Deploy model: each colleague runs their own local instance on their own machine. Never a shared server -- raw PII must never leave the originating machine.
- Distribution: one self-contained offline bundle folder on an internal network share. No GitHub, no internet, no admin rights required.
- Packaging: relocatable standalone CPython runtime (python-build-standalone, same as uv already uses) with all deps + both spaCy models pre-installed directly into it. No PyInstaller.
- Launcher: `launch.bat`, plain double-clickable script. Native app window via pywebview, not a browser tab.
- First run: `install.ps1` clears Mark-of-the-Web + creates a Desktop shortcut. Visible loading notice during first analyzer/model load.
- Security posture: ships unsigned, no IT/InfoSec sign-off for now (documented conscious choice). README covers the SmartScreen unblock step.
- Per-colleague mapping DBs stay isolated -- different placeholders per person is intentional (smaller blast radius).
- Updates: manual re-copy of a versioned bundle folder from the shared drive. Settings gets a "Check for new recognizers" button that merges brand-new shipped custom recognizers into an already-customized local config without touching existing entries.
- Rollout: small pilot group first. Support is informal (the user) + a README FAQ.

## DONE-WHEN
Offline bundle built; `install.ps1` and `launch.bat` verified using only the bundled Python (not `uv run`); pywebview window opens; FAQ written. Verified on this dev machine only -- a second machine was not available to test on, which is a real gap, not silently ignored.

## DEFAULTS
Unsigned/no-IT-involvement posture holds until the user says otherwise. Config drift across colleagues is acceptable except for brand-new recognizer additions, which sync via the explicit "Check for new recognizers" button.

## DEFERRED
Code-signing, IT/InfoSec formal review, a "smarter" recognizer-sync mechanism that also flags changes to recognizers a colleague hasn't customized, testing on a genuinely separate machine, and any in-app update-check mechanism -- resurface only on explicit request.
