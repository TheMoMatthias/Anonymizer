"""Print the recall report. Run:  uv run python scripts/measure_recall.py

Fully offline. Plants known names/identifiers into realistic German bank text
and reports how much the pipeline actually catches, per stratum. Use this to
judge any detection change -- a model swap, a new recognizer, a threshold tweak
-- instead of trusting a model card or an intuition.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import yaml

from anonymizer.config import DEFAULT_CONFIG_PATH
from anonymizer.engine import build_analyzer
from anonymizer.evaluation import (
    format_report,
    measure_documents,
    measure_isolated,
    measure_structured,
)


def main() -> None:
    config = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    print("Loading models…")
    analyzer = build_analyzer(config)

    sections = {
        "STRUCTURED IDENTIFIERS (checksummed/patterned -- expect ~100%)": measure_structured(analyzer, config),
        "NAMES, ISOLATED (one occurrence, cold read -- pessimistic)": measure_isolated(analyzer, config),
    }
    with tempfile.TemporaryDirectory() as tmp:
        sections["NAMES, FULL LETTER (anchors + propagation -- what really happens)"] = measure_documents(
            analyzer, config, Path(tmp)
        )
    print(format_report(sections))


if __name__ == "__main__":
    main()
