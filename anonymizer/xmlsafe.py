"""Hardened XML parsing for untrusted document parts.

Bank documents arrive from external clients, so their OOXML/XML parts are
untrusted input. lxml's default parser resolves entities, which exposes
billion-laughs (exponential-expansion DoS) and local-file (file://) entity
inclusion. Parse every document-derived XML through `fromstring` here instead of
`etree.fromstring`, which disables entities, DTD loading, network access, and the
unbounded-tree path.

A fresh parser is created per call: an lxml parser is not safe to share across
threads, and apply/scan run parsing on the run.io_bound thread pool.
"""

from __future__ import annotations

from lxml import etree


def fromstring(data):
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
    )
    return etree.fromstring(data, parser=parser)
