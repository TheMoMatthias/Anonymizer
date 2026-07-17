from __future__ import annotations

from pathlib import Path

import fitz

from .. import ocr as ocr_mod
from ..actions import decisions_lookup, resolve_replacement
from ..core import detect_unit
from ..models import ProcessingError, TextUnit

EXTENSIONS = (".pdf",)

_BLACK = (0, 0, 0)

# Below this many characters, an image-bearing page is treated as scanned (its
# text layer is at most an incidental page number / watermark) and OCR'd -- a
# scanned page carrying a printed caption must NOT be trusted as "already text".
_MIN_TEXT_CHARS = 20
# An image only forces the scanned-page path when it covers a large share of the
# page, so a small logo on an otherwise-text page never triggers OCR/refusal.
_LARGE_IMAGE_FRACTION = 0.5


def _text_words(page) -> tuple[str, list]:
    """(assembled_text, [WordBox]) built from the text layer's word list, so char
    offsets map to word rectangles. Redaction is then driven by the DETECTED span,
    robust to line wraps and hyphenation -- unlike page.search_for(value), which
    re-searches by string and misses a name that wrapped a line."""
    parts: list[str] = []
    boxes: list = []
    cursor = 0
    for x0, y0, x1, y1, word, *_rest in page.get_text("words"):
        if not word:
            continue
        start = cursor
        parts.append(word)
        cursor += len(word)
        end = cursor
        parts.append(" ")
        cursor += 1
        boxes.append(ocr_mod.WordBox(word, x0, y0, x1, y1, start, end))
    return "".join(parts), boxes


def _has_large_image(page) -> bool:
    """True if images cover a large share of the page -- the signal for a scanned
    page. Uses TOTAL image coverage (summed rect areas), not the largest single
    image, so a partial-page scan OR a tiled scan (many individually-sub-threshold
    images) still trips the OCR/refuse path instead of being trusted as a thin text
    layer. Overlapping images may overcount, which only errs toward refusing (safe)."""
    page_area = abs(page.rect.width * page.rect.height)
    if page_area <= 0:
        return False
    covered = 0.0
    for img in page.get_images(full=True):
        try:
            for r in page.get_image_rects(img[0]):
                covered += abs(r.width * r.height)
        except Exception:  # noqa: BLE001 -- a malformed image xref must not crash extraction
            continue
    return covered >= _LARGE_IMAGE_FRACTION * page_area


def _page_content(page, config) -> tuple[str, list]:
    """(text, word_boxes) for a page.

    A page with a substantial text layer and no dominating image is used as-is.
    Otherwise (empty/short text, or a large image that may be a scan) we OCR when
    Tesseract is available -- this catches a scanned page carrying only an
    incidental page number/watermark, which a "any text = trust the text layer"
    rule would wrongly skip. If OCR is unavailable OR yields nothing while a large
    image is present, the page carries content we cannot read or verify ->
    ProcessingError (fail loud, never ship it unredacted). A genuinely blank page
    (no image) returns ("", []); the document-level guard refuses a whole PDF that
    yields no units at all."""
    text, boxes = _text_words(page)
    large_image = _has_large_image(page)
    if len(text.strip()) >= _MIN_TEXT_CHARS and not large_image:
        return text, boxes
    if ocr_mod.ocr_available(config):
        ocr_text, ocr_boxes = ocr_mod.ocr_page(page)
        if ocr_text.strip():
            return ocr_text, ocr_boxes
        if large_image:  # an image we rendered but OCR couldn't read -> refuse
            raise ProcessingError(
                f"Page {page.number + 1} is a scanned/image page whose text could not "
                "be read by OCR, so it cannot be anonymized safely and no output was "
                "written."
            )
        return text, boxes  # no image, OCR empty -> the short text layer is all there is
    if large_image:  # scanned page, no OCR available -> refuse
        raise ProcessingError(
            f"Page {page.number + 1} is a scanned/image page and OCR is not available "
            "(no Tesseract found), so it cannot be anonymized safely and no output was "
            "written. See the FAQ to enable OCR."
        )
    return text, boxes


def _field_and_annot_texts(page) -> list[tuple[str, str]]:
    """[(kind_id, text)] for every fillable AcroForm field VALUE and annotation
    (comment / free-text) on the page. Bank PDFs are very often fillable forms --
    the account-holder name, IBAN and address live in field values, not the
    content stream, so page.get_text() never sees them."""
    out: list[tuple[str, str]] = []
    try:
        for w in list(page.widgets() or []):
            val = w.field_value
            if isinstance(val, str) and val.strip():
                out.append((f"widget|{w.field_name}", val))
    except Exception:  # noqa: BLE001 -- widget enumeration must not crash extraction
        pass
    try:
        for a in list(page.annots() or []):
            content = (a.info or {}).get("content", "")
            if isinstance(content, str) and content.strip():
                out.append(("annot", content))
    except Exception:  # noqa: BLE001
        pass
    return out


def extract_text_units(path: Path) -> list[TextUnit]:
    doc = fitz.open(path)
    units: list[TextUnit] = []
    try:
        for i, page in enumerate(doc):
            text, _boxes = _page_content(page, None)
            if text.strip():
                units.append(TextUnit(id=f"page{i}", text=text))
            for j, (kind, value) in enumerate(_field_and_annot_texts(page)):
                units.append(TextUnit(id=f"page{i}|{kind}|{j}", text=value))
    finally:
        doc.close()
    return units


def scan(path: Path, analyzer, config) -> list:
    findings = []
    doc = fitz.open(path)
    try:
        for i, page in enumerate(doc):
            text, _boxes = _page_content(page, config)
            if text.strip():
                findings.extend(detect_unit(analyzer, TextUnit(id=f"page{i}", text=text), config))
            for j, (kind, value) in enumerate(_field_and_annot_texts(page)):
                findings.extend(detect_unit(analyzer, TextUnit(id=f"page{i}|{kind}|{j}", text=value), config))
    finally:
        doc.close()
    return findings


def _redact_value(text: str, analyzer, config, decisions: dict, mapping_store) -> str:
    """Splice-redact a field/annotation value string. detect_unit returns an
    overlap-resolved set, so right-to-left splicing is safe."""
    findings = detect_unit(analyzer, TextUnit("tmp", text), config)
    result = text
    for f in sorted(findings, key=lambda f: -f.start):
        action = decisions_lookup(decisions, f.entity_type, f.value)
        replacement = resolve_replacement(f.entity_type, f.value, action, mapping_store)
        if replacement is None:
            continue
        result = result[: f.start] + replacement + result[f.end :]
    return result


def apply(path: Path, out_path: Path, decisions: dict, analyzer, config, mapping_store) -> None:
    # Body text: physically removed under a black box (PDF text can't be edited in
    # place). Redaction rects come from the DETECTED span via word boxes (text or
    # OCR), not a value re-search. Form-field values and annotation text are
    # separate objects the content stream never holds, so they are redacted by
    # splicing their value strings.
    doc = fitz.open(path)
    unmapped: list[str] = []
    try:
        for page in doc:
            text, boxes = _page_content(page, config)

            # 1) Fillable field values + annotation content.
            for w in list(page.widgets() or []):
                val = w.field_value
                if isinstance(val, str) and val.strip():
                    new = _redact_value(val, analyzer, config, decisions, mapping_store)
                    if new != val:
                        w.field_value = new
                        w.update()
            for a in list(page.annots() or []):
                content = (a.info or {}).get("content", "")
                if isinstance(content, str) and content.strip():
                    new = _redact_value(content, analyzer, config, decisions, mapping_store)
                    if new != content:
                        a.set_info(content=new)
                        a.update()

            # 2) Body text -> redaction rects from detected offsets.
            if text.strip():
                findings = detect_unit(analyzer, TextUnit(id="tmp", text=text), config)
                for f in findings:
                    if decisions_lookup(decisions, f.entity_type, f.value) == "skip":
                        continue
                    rects = ocr_mod.boxes_for_span(boxes, f.start, f.end)
                    if not rects:
                        # A non-skip finding that maps to no rectangle would ship
                        # unredacted -> fail loud rather than silently leak it.
                        unmapped.append(f.value)
                        continue
                    for r in rects:
                        page.add_redact_annot(fitz.Rect(*r), fill=_BLACK)
            page.apply_redactions()
        if unmapped:
            sample = ", ".join(sorted(set(unmapped))[:5])
            raise ProcessingError(
                f"{len(unmapped)} approved redaction(s) could not be located on the "
                f"page ({sample}) -- refusing to write a partially-redacted PDF."
            )
        # garbage=4 + clean drops orphaned objects -- specifically the OLD widget/
        # annotation appearance streams that still render the ORIGINAL value; a plain
        # save leaves them physically recoverable in the output bytes.
        doc.save(str(out_path), garbage=4, deflate=True, clean=True)
    finally:
        doc.close()
