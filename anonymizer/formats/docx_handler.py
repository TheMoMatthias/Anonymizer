from __future__ import annotations

import zipfile
from pathlib import Path

from docx import Document
from lxml import etree

from ..core import detect_unit
from ..models import TextUnit
from .run_replace import XmlRunAdapter, apply_findings_to_runs

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{W_NS}}}"
EXTRA_PARTS = ["word/footnotes.xml", "word/endnotes.xml", "word/comments.xml"]

EXTENSIONS = (".docx", ".doc")


def _iter_paragraphs(doc: Document):
    for p in doc.paragraphs:
        yield p
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                yield from cell.paragraphs
    for section in doc.sections:
        for container in (section.header, section.footer):
            for p in container.paragraphs:
                yield p
            for table in container.tables:
                for row in table.rows:
                    for cell in row.cells:
                        yield from cell.paragraphs


def _extra_parts_paragraphs(path: Path):
    with zipfile.ZipFile(path, "r") as zf:
        names = zf.namelist()
        contents = {part: zf.read(part) for part in EXTRA_PARTS if part in names}
    for part, blob in contents.items():
        tree = etree.fromstring(blob)
        for p in tree.iter(f"{W}p"):
            yield part, tree, p


def extract_text_units(path: Path) -> list[TextUnit]:
    doc = Document(path)
    units = []
    for i, p in enumerate(_iter_paragraphs(doc)):
        if p.text.strip():
            units.append(TextUnit(id=f"p{i}", text=p.text))
    for i, (part, _tree, p) in enumerate(_extra_parts_paragraphs(path)):
        text = "".join((t.text or "") for t in p.iter(f"{W}t"))
        if text.strip():
            units.append(TextUnit(id=f"extra:{part}:{i}", text=text))
    return units


def scan(path: Path, analyzer, config) -> list:
    findings = []
    for unit in extract_text_units(path):
        findings.extend(detect_unit(analyzer, unit, config))
    return findings


def apply(path: Path, out_path: Path, decisions: dict, analyzer, config, mapping_store) -> None:
    doc = Document(path)
    for p in _iter_paragraphs(doc):
        if not p.text.strip():
            continue
        unit = TextUnit(id="tmp", text=p.text)
        findings = detect_unit(analyzer, unit, config)
        apply_findings_to_runs(p.runs, findings, decisions, mapping_store)
    doc.save(out_path)
    _apply_extra_parts(out_path, analyzer, config, decisions, mapping_store)


def _apply_extra_parts(path: Path, analyzer, config, decisions: dict, mapping_store) -> None:
    with zipfile.ZipFile(path, "r") as zf:
        names = zf.namelist()
        contents = {n: zf.read(n) for n in names}

    any_changed = False
    for part in EXTRA_PARTS:
        if part not in contents:
            continue
        tree = etree.fromstring(contents[part])
        part_changed = False
        for p_elem in tree.iter(f"{W}p"):
            t_elems = list(p_elem.iter(f"{W}t"))
            text = "".join((t.text or "") for t in t_elems)
            if not text.strip():
                continue
            unit = TextUnit(id="tmp", text=text)
            findings = detect_unit(analyzer, unit, config)
            if not findings:
                continue
            runs = [XmlRunAdapter(t) for t in t_elems]
            apply_findings_to_runs(runs, findings, decisions, mapping_store)
            part_changed = True
        if part_changed:
            contents[part] = etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True)
            any_changed = True

    if any_changed:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in names:
                zf.writestr(name, contents[name])
