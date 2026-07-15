"""Optional OCR for scanned/image PDFs, via a PORTABLE Tesseract.

Kept deliberately lightweight and offline: no PyTorch, no cloud. Tesseract is a
native binary, so on a locked-down PC (no admin, no installer) the app looks for
a portable copy shipped inside the bundle. If none is found, OCR stays inert and
the pipeline refuses image PDFs rather than emitting a false-clean file.

Resolution order for the tesseract executable:
  1. config["tesseract_path"]
  2. env ANONYMIZER_TESSERACT
  3. a bundled copy: <bundle>/tesseract/tesseract.exe (searched next to the
     package, its parents, and the Python executable)
  4. tesseract on PATH

When a portable tesseract is found, its sibling `tessdata` folder is registered
via TESSDATA_PREFIX so the language files (deu/eng) are picked up without any
global install.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

_EXE_NAME = "tesseract.exe" if os.name == "nt" else "tesseract"

# OCR renders the page at this zoom (≈216 DPI) before recognition -- enough for
# reliable text without huge images.
RENDER_ZOOM = 3.0
OCR_LANGS = "deu+eng"

_resolved_cmd: str | None = None
_resolution_done = False


def _bundle_candidates() -> list[Path]:
    # Search a `tesseract/` folder under several ancestors of both the package
    # and the Python runtime -- in the offline bundle the folder sits at the
    # bundle root, a few levels above the relocated runtime's python.exe.
    anchors: list[Path] = []
    anchors += list(Path(__file__).resolve().parents[:5])
    anchors += list(Path(sys.executable).resolve().parents[:4])
    seen: set[Path] = set()
    candidates: list[Path] = []
    for a in anchors:
        if a in seen:
            continue
        seen.add(a)
        candidates.append(a / "tesseract" / _EXE_NAME)
    return candidates


def find_tesseract(config: dict | None = None) -> str | None:
    """Resolves the tesseract executable path (see module docstring), or None."""
    explicit = (config or {}).get("tesseract_path") or os.environ.get("ANONYMIZER_TESSERACT")
    if explicit and Path(explicit).exists():
        return str(explicit)
    for cand in _bundle_candidates():
        if cand.exists():
            return str(cand)
    found = shutil.which("tesseract")
    return found


def _configure(cmd: str) -> None:
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = cmd
    tessdata = Path(cmd).parent / "tessdata"
    if tessdata.is_dir():
        os.environ.setdefault("TESSDATA_PREFIX", str(tessdata.parent))


def ocr_available(config: dict | None = None) -> bool:
    global _resolved_cmd, _resolution_done
    if not _resolution_done:
        _resolved_cmd = find_tesseract(config)
        if _resolved_cmd:
            try:
                _configure(_resolved_cmd)
            except Exception:  # noqa: BLE001 -- pytesseract import guard
                _resolved_cmd = None
        _resolution_done = True
    return _resolved_cmd is not None


def reset_resolution() -> None:
    """Forces re-resolution (e.g. after the user sets a path in Settings)."""
    global _resolution_done, _resolved_cmd
    _resolution_done = False
    _resolved_cmd = None


@dataclass
class WordBox:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    start: int  # char offset in the assembled page text
    end: int


def ocr_page(page, zoom: float = RENDER_ZOOM):
    """OCRs one PDF page. Returns (assembled_text, [WordBox]) with word boxes in
    PDF coordinate space and char offsets into the assembled text."""
    import io

    import fitz
    import pytesseract
    from PIL import Image

    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    data = pytesseract.image_to_data(img, lang=OCR_LANGS, output_type=pytesseract.Output.DICT)

    parts: list[str] = []
    boxes: list[WordBox] = []
    cursor = 0
    n = len(data["text"])
    for i in range(n):
        word = (data["text"][i] or "").strip()
        if not word:
            continue
        start = cursor
        parts.append(word)
        cursor += len(word)
        end = cursor
        parts.append(" ")
        cursor += 1
        left, top = data["left"][i] / zoom, data["top"][i] / zoom
        right = (data["left"][i] + data["width"][i]) / zoom
        bottom = (data["top"][i] + data["height"][i]) / zoom
        boxes.append(WordBox(word, left, top, right, bottom, start, end))
    return "".join(parts), boxes


def boxes_for_span(boxes: list[WordBox], start: int, end: int, pad: float = 1.0):
    """Rectangles (as (x0,y0,x1,y1) tuples, padded) for every word overlapping
    the char span [start, end) -- used to place redaction boxes."""
    rects = []
    for b in boxes:
        if b.end > start and b.start < end:
            rects.append((b.x0 - pad, b.y0 - pad, b.x1 + pad, b.y1 + pad))
    return rects
