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

## Why do I review by category now instead of hundreds of fields?
Findings are grouped into categories (People, Financial IDs, Government IDs,
Contact, …). You set one action per category; high-confidence, checksum-
validated items are auto-accepted and tucked away. Expand a category only to
override a specific value. This replaces clicking through every single field.

## Can I process several files at once?
Yes -- drag multiple files in (or add them one by one). They queue up; click a
file to review it. Pick a "profile" (Contracts, Client statements, HR…) to
preset the default action per category for that batch.

## What are "possible misses"?
Sensitive-looking strings (long numbers, emails, IBAN-shaped text) that no
recognizer matched. They are NOT redacted -- they're listed so you can catch
anything that slipped through. If one is sensitive, add it to the deny list in
Settings and re-scan.

## I got an AI answer back that says [PERSON_1] -- how do I read it?
Open "Re-identify" (top of the window), paste the text, and confirm. Tokens are
mapped back to the real values. This action is recorded in the audit log.

## It refused my scanned PDF
A scanned/photographed PDF has no real text layer, so without OCR nothing could
be detected and it would look "clean" while hiding everything. The app refuses
it on purpose rather than give you a false sense of safety.

To handle scanned PDFs, enable OCR (below).

## How do I enable OCR for scanned PDFs?
OCR reads text out of images. It needs a small, portable copy of Tesseract-OCR
-- no installer, no admin rights:

1. Get a portable Tesseract-OCR folder (e.g. copy an existing
   `C:\Program Files\Tesseract-OCR` folder, or a portable build).
2. Put it in this bundle so the layout is:
   `tesseract\tesseract.exe` and `tesseract\tessdata\deu.traineddata` +
   `eng.traineddata`.
3. Re-launch. Scanned PDFs are now OCR'd and redacted with black boxes over the
   detected text. You can confirm OCR is active on the Settings page.

Alternatively, set the exact path to `tesseract.exe` in Settings.

## Is my data safe?
Yes -- this app makes no network calls. Everything happens on your own
computer. The mapping used to keep pseudonyms consistent (e.g. the same
name always becoming "[PERSON_3]") is stored encrypted, only on your machine,
and isn't shared with colleagues -- each person's mapping is independent.

## I customized my recognizer settings -- will an update wipe them?
No. Your settings live outside this folder (in
`%LOCALAPPDATA%\Anonymizer`), so re-copying a newer version of this folder
never touches them. If a new recognizer becomes available in a newer
version, use the "Check for new recognizers" button on the Settings page.
