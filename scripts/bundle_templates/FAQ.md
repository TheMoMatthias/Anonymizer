# Document Anonymizer -- FAQ

## First-time setup
1. Copy this whole folder somewhere on your computer (e.g. Desktop or Documents).
2. Right-click `install.ps1` -> "Run with PowerShell". This adds a Desktop shortcut.
3. Double-click the "Document Anonymizer" shortcut (or `launch.bat` in this folder).

## "Windows protected your PC" warning
Files copied from a network share are sometimes flagged by Windows as
downloaded from an untrusted source. Running `install.ps1` should clear this
automatically. If you still see a warning when launching, right-click the
file -> Properties -> check "Unblock" -> OK.

## First scan is slow
The first time you scan a document, the app loads its language models
(~10-20 seconds). This only happens once per launch, not per file.

## .doc / .xls / .ppt files
These older formats need Microsoft Office installed to convert to the
modern format before anonymizing. If you don't have Office, save the file
as .docx/.xlsx/.pptx first (File -> Save As in the relevant Office app).

## Nothing happens when I double-click launch.bat
Make sure you ran `install.ps1` first. If it still doesn't work, contact
Maurice Matthias.

## Where does the anonymized file go?
Next to the original, named `<filename>_psd.<ext>` -- e.g. `Report.docx` ->
`Report_psd.docx`, plus a `_report.json` audit file.

## Is my data safe?
Yes -- this app makes no network calls. Everything happens on your own
computer. The mapping used to keep pseudonyms consistent (e.g. the same
name always becoming "PERSON_3") is stored encrypted, only on your machine,
and isn't shared with colleagues -- each person's mapping is independent.

## I customized my recognizer settings -- will an update wipe them?
No. Your settings live outside this folder (in
`%LOCALAPPDATA%\Anonymizer`), so re-copying a newer version of this folder
never touches them. If a new recognizer becomes available in a newer
version, use the "Check for new recognizers" button on the Settings page.
