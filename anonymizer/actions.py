from __future__ import annotations

from .mapping import MappingStore


def resolve_replacement(entity_type: str, value: str, action: str, mapping_store: MappingStore) -> str | None:
    """Returns the replacement text for a decided action, or None if the match should be left untouched."""
    if action == "skip":
        return None
    if action == "pseudonymize":
        return mapping_store.get_or_create(entity_type, value)
    return f"[{entity_type}]"


def decisions_lookup(decisions: dict[tuple[str, str], str], entity_type: str, value: str) -> str:
    return decisions.get((entity_type, value.strip().lower()), "skip")
