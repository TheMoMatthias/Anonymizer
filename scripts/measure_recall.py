"""Print the recall report. Run:  uv run python scripts/measure_recall.py

Fully offline. Plants known names/identifiers into realistic German bank text
and reports how much the pipeline actually catches, per stratum. Use this to
judge any detection change -- a model swap, a new recognizer, a threshold tweak
-- instead of trusting a model card or an intuition.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import yaml

from anonymizer.config import DEFAULT_CONFIG_PATH
from anonymizer.engine import build_analyzer
from anonymizer.evaluation import (
    format_report,
    measure_art9_heldout,
    measure_art9_oblique,
    measure_art9_structured,
    measure_documents,
    measure_embedded_identifiers,
    measure_isolated,
    measure_structured,
    measure_unanchored_documents,
    measure_workbook_traps,
)


def main() -> None:
    # The report NAMES the plants it missed, and the hard strata are deliberately
    # full of characters Windows' default cp1252 console cannot encode (Nguyễn,
    # Đorđević, Þórsdóttir). Without this the whole run dies at the final print
    # -- after several minutes of measurement -- on a UnicodeEncodeError.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    config = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    print("Loading models…")
    analyzer = build_analyzer(config)

    sections = {
        "STRUCTURED IDENTIFIERS (checksummed/patterned -- expect ~100%)": measure_structured(analyzer, config),
        "ART. 9 STATED OBLIQUELY (no label, no list word -- the heaviest miss)": measure_art9_oblique(
            analyzer, config
        ),
        "ART. 9 HELD OUT (in no word list -- can only be caught by a FRAME)": measure_art9_heldout(
            analyzer, config
        ),
        "NAMES, ISOLATED (one occurrence, cold read -- pessimistic)": measure_isolated(analyzer, config),
    }
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        sections["NAMES INSIDE IDENTIFIERS / PATHS (name known elsewhere in the doc)"] = (
            measure_embedded_identifiers(analyzer, config, work)
        )
        sections["ART. 9 IN BARE CELLS (special category + structured -- both weak areas)"] = (
            measure_art9_structured(analyzer, config, work)
        )
        sections["NAMES IN SPREADSHEET CELLS (header gives no help -- the real files)"] = (
            measure_workbook_traps(analyzer, config, work)
        )
        sections["NAMES, FULL LETTER (anchors + propagation -- what really happens)"] = measure_documents(
            analyzer, config, work
        )
        sections["NAMES, UNANCHORED MEMO (no honorific anywhere -- the true floor)"] = (
            measure_unanchored_documents(analyzer, config, work)
        )
    print(format_report(sections))


if __name__ == "__main__":
    main()
