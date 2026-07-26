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
    # Which recognizer produced this -- "SpacyRecognizer" for a raw NER guess,
    # a PatternRecognizer/checksum-recognizer class name otherwise, or an
    # internal label ("propagation", "whole_cell_override", "deny_list") for
    # findings core.py/format handlers construct directly rather than via
    # Presidio. Used only to distinguish an unvalidated NER guess from an
    # unvalidated-but-pattern-anchored hit in the review UI's Medium tier.
    source: str = ""


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
    # True only if EVERY occurrence of this value came from raw spaCy NER with
    # no pattern/checksum corroboration anywhere -- i.e. this is purely a
    # model guess, not something a rule ever anchored. Drives a distinct
    # sub-band within the Medium tier in the review UI (see review.py).
    is_ner_guess: bool = False


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
    context: str = ""  # +/-40-char snippet around the value (GroupedFinding.context)


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
class ColumnInfo:
    """One spreadsheet column, surfaced to the reviewer so a whole-column policy
    can be set at scale (redact/pseudonymize every cell) without deciding per
    value. `key` is the stable identity a policy is keyed on ("Sheet!A")."""

    sheet: str
    column: str  # column letter, e.g. "A"
    header: str  # row-1 header text, or "" if none
    sample: str  # a representative non-empty value from the column
    pii_count: int  # findings the scan located in this column
    # True when this column's header matches the whole-cell PERSON override's
    # name-header list (xlsx_handler._NAME_HEADER_TERMS) -- every name-shaped
    # cell in it gets force-claimed as a person regardless of per-value NER.
    # Surfaced so a header that matched by coincidence (not because the column
    # actually holds names) is visible before Save, not just inferred from an
    # unexpectedly high pii_count.
    name_override: bool = False

    @property
    def key(self) -> str:
        return f"{self.sheet}!{self.column}"


@dataclass
class CellInfo:
    """One spreadsheet cell that carries a finding, surfaced so the reviewer can
    set a per-CELL policy (the finest granularity, an exception to the column
    decision). `key` is the stable identity a policy is keyed on ("Sheet!A5")."""

    sheet: str
    coord: str  # e.g. "A5"
    header: str  # the cell's column header, or ""
    sample: str  # a short preview of the cell's content / context
    entity_types: tuple = ()  # what detection found in the cell

    @property
    def key(self) -> str:
        return f"{self.sheet}!{self.coord}"


@dataclass
class ScanResult:
    # Actionable findings grouped by data class, most-sensitive first.
    groups: list[DataClassGroup] = field(default_factory=list)
    # Informational-only: sensitive-looking strings no recognizer matched.
    possible_misses: list[GroupedFinding] = field(default_factory=list)
    # Coverage/telemetry for the reviewer (units scanned, counts per tier, ...).
    stats: dict = field(default_factory=dict)
    # Spreadsheet columns (empty for non-tabular formats), for column-level policy.
    columns: list["ColumnInfo"] = field(default_factory=list)
    # Spreadsheet cells that carry a finding, for the per-cell exception layer.
    cells: list["CellInfo"] = field(default_factory=list)

    def all_actionable(self) -> list[GroupedFinding]:
        return [g for grp in self.groups for g in grp.items]
