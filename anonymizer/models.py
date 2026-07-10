from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TextUnit:
    """A scannable piece of text within a document (a paragraph, cell, notes block, etc.)."""

    id: str
    text: str


@dataclass
class Finding:
    entity_type: str
    value: str
    score: float
    context: str
    unit_id: str
    start: int
    end: int


@dataclass
class GroupedFinding:
    entity_type: str
    value: str
    count: int
    max_score: float
    context: str
    action: str  # "pseudonymize" | "anonymize" | "skip"


@dataclass
class ScanResult:
    grouped: list[GroupedFinding] = field(default_factory=list)
