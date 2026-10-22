"""Read-only periodic report; never activates, backfills or sends messages."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from core.breakout_confirmation_shadow import report
from core.historical_lifecycle_reconstruction import load_canonical_reporting_population


def main(argv=None):
    base = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=base / "research/scanner_research_v1.db")
    parser.add_argument("--signals", type=Path, default=base / "logs/signals.csv")
    parser.add_argument("--history", type=Path, default=base / "logs/signals_history.csv")
    parser.add_argument("--execution", type=Path, default=base / "logs/binance_execution_truth_v1.csv")
    parser.add_argument("--output", type=Path, help="Optional local JSON export; otherwise stdout")
    args = parser.parse_args(argv)
    # Reporting and import never create a database or activation boundary.
    result = report(args.db)
    if result["status"] == "ACTIVE":
        population, counts = load_canonical_reporting_population(args.signals, args.history, args.db)
        execution = []
        if args.execution.exists():
            with args.execution.open(encoding="utf-8-sig", newline="") as handle:
                execution = list(csv.DictReader(handle))
        result = report(args.db, population.to_dict("records"), execution)
        result["canonical_population_counts_reference_only"] = counts
    content = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(content, encoding="utf-8")
    else:
        print(content, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
