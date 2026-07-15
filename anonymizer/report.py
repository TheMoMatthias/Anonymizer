from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import __version__, taxonomy
from .actions import token_label
from .models import GroupedFinding


def _config_hash(config: dict | None) -> str:
    if not config:
        return ""
    blob = yaml.safe_dump(config, sort_keys=True, allow_unicode=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def build_report(grouped: list[GroupedFinding], config: dict | None, verified: bool) -> dict:
    """Builds the audit record. Deliberately records NO original values or
    context snippets -- only entity types, data classes, decisions, counts, and
    the placeholder labels used. Reversibility stays solely in the encrypted
    mapping; the report is safe to keep alongside the anonymized document."""
    by_class: dict[str, dict] = {}
    for g in grouped:
        dc = taxonomy.data_class_for(g.entity_type)
        bucket = by_class.setdefault(
            dc.key,
            {"data_class": dc.display, "sensitivity": dc.sensitivity, "entities": []},
        )
        bucket["entities"].append(
            {
                "entity_type": g.entity_type,
                "token_label": token_label(g.entity_type),
                "action": g.action,
                "distinct_values": 1,
                "occurrences": g.count,
                "max_confidence": round(g.max_score, 3),
                "tier": g.tier,
                "checksum_validated": g.validated,
            }
        )

    actions = {"pseudonymize": 0, "anonymize": 0, "skip": 0}
    for g in grouped:
        actions[g.action] = actions.get(g.action, 0) + 1

    return {
        "tool": "anonymizer",
        "tool_version": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config_hash": _config_hash(config),
        "verified_no_residual_pii": verified,
        "summary": {
            "distinct_findings": len(grouped),
            "total_occurrences": sum(g.count for g in grouped),
            "decisions": actions,
        },
        "by_data_class": list(by_class.values()),
    }


def write_report(
    out_doc_path: Path,
    grouped: list[GroupedFinding],
    config: dict | None = None,
    verified: bool = False,
) -> Path:
    report_path = out_doc_path.with_name(out_doc_path.stem + "_report.json")
    data = {"source_document": out_doc_path.name, **build_report(grouped, config, verified)}
    report_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return report_path
