from __future__ import annotations

from dataclasses import dataclass, field


class ProcessingError(Exception):
    """Raised when a document cannot be processed safely. The tool never emits a
    partial or unverified `_psd` file -- better no output than a falsely-clean one.
    Defined here (not in pipeline) so format handlers can fail loud without a
    circular import; pipeline re-exports it for existing callers."""


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
    # None = no checksum applies; True/False = checksum verdict (see validators).
    validated: bool | None = None


@dataclass
class GroupedFinding:
    entity_type: str
    value: str
    count: int
    max_score: float
    context: str
    action: str  # "pseudonymize" | "anonymize" | "skip"
    tier: str = "medium"  # "high" | "medium" | "low" (see taxonomy.tier_for)
    validated: bool | None = None


@dataclass
class DataClassGroup:
    """A sensitivity category the reviewer decides on as a unit."""

    key: str
    display: str
    sensitivity: str
    items: list[GroupedFinding] = field(default_factory=list)

    @property
    def count(self) -> int:
        return sum(g.count for g in self.items)

    @property
    def high_tier_items(self) -> list[GroupedFinding]:
        return [g for g in self.items if g.tier == "high"]

    @property
    def review_items(self) -> list[GroupedFinding]:
        return [g for g in self.items if g.tier != "high"]


@dataclass
class PreviewRow:
    entity_type: str
    value: str
    action: str
    token: str  # what the value becomes ("[PERSON_#]", "[IBAN]", ...)


@dataclass
class PreviewGroup:
    display: str
    rows: list["PreviewRow"] = field(default_factory=list)


@dataclass
class FileJob:
    """One file moving through the batch: pending -> scanning -> review ->
    saving -> done | failed."""

    path: str
    status: str = "pending"
    scan: "ScanResult | None" = None
    error: str = ""
    out_path: str = ""
    report_path: str = ""
    # The exact config this file was scanned with (profile + sensitivity applied)
    # so apply re-detects with identical thresholds -- scan/apply parity.
    config: dict | None = None

    @property
    def name(self) -> str:
        from pathlib import Path

        return Path(self.path).name


@dataclass
class ScanResult:
    # Actionable findings grouped by data class, most-sensitive first.
    groups: list[DataClassGroup] = field(default_factory=list)
    # Informational-only: sensitive-looking strings no recognizer matched.
    possible_misses: list[GroupedFinding] = field(default_factory=list)
    # Coverage/telemetry for the reviewer (units scanned, counts per tier, ...).
    stats: dict = field(default_factory=dict)

    def all_actionable(self) -> list[GroupedFinding]:
        return [g for grp in self.groups for g in grp.items]
