from __future__ import annotations

import json
from pathlib import Path

from .models import GroupedFinding


def write_report(out_doc_path: Path, grouped: list[GroupedFinding]) -> Path:
    report_path = out_doc_path.with_name(out_doc_path.stem + "_report.json")
    data = {
        "source_document": out_doc_path.name,
        "findings": [
            {
                "entity_type": g.entity_type,
                "occurrences": g.count,
                "max_confidence": round(g.max_score, 3),
                "action": g.action,
            }
            for g in grouped
        ],
    }
    report_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return report_path
