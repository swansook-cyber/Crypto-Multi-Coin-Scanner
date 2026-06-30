# -*- coding: utf-8 -*-
"""Operational summary for Entry Timing Engine shadow-mode data."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


BASE_DIR = Path(__file__).resolve().parent
ENTRY_TIMING = BASE_DIR / "logs" / "entry_timing_engine.csv"
JOURNAL = BASE_DIR / "logs" / "signals.csv"


def load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def readiness_status(linked_closed_outcomes: int) -> str:
    if linked_closed_outcomes < 30:
        return "NOT ENOUGH DATA"
    if linked_closed_outcomes < 100:
        return "EARLY DATA"
    return "REVIEW READY"


def _key(df: pd.DataFrame, direction_col: str) -> pd.Series:
    if df.empty:
        return pd.Series(dtype=str)
    timestamp = df.get("timestamp", pd.Series([""] * len(df))).fillna("").astype(str).str.slice(0, 16)
    symbol = df.get("symbol", pd.Series([""] * len(df))).fillna("").astype(str).str.upper()
    direction = df.get(direction_col, pd.Series([""] * len(df))).fillna("").astype(str).str.upper()
    entry = pd.to_numeric(df.get("entry", pd.Series([""] * len(df))), errors="coerce").round(8).fillna("").astype(str)
    return timestamp + "|" + symbol + "|" + direction + "|" + entry


def linked_closed_outcomes(entry: pd.DataFrame, journal: pd.DataFrame) -> int:
    if entry.empty or journal.empty:
        return 0
    closed = journal[journal.get("result", pd.Series(dtype=str)).fillna("").astype(str).str.upper().isin(["WIN", "LOSS"])]
    entry_keys = set(_key(entry, "direction").astype(str))
    closed_keys = set(_key(closed, "side").astype(str))
    return len(entry_keys & closed_keys)


def value_counts_lines(df: pd.DataFrame, column: str, total: int) -> list[str]:
    if df.empty or column not in df.columns or total <= 0:
        return ["- N/A"]
    counts = df[column].fillna("-").astype(str).replace("", "-").value_counts()
    return [f"- {name}: {count} ({count / total * 100:.1f}%)" for name, count in counts.items()]


def build_summary(entry: pd.DataFrame, journal: pd.DataFrame) -> str:
    total = int(len(entry))
    score = pd.to_numeric(entry.get("entry_quality_score", pd.Series(dtype=float)), errors="coerce")
    linked = linked_closed_outcomes(entry, journal)
    latest_cols = [column for column in ["timestamp", "symbol", "direction", "recommendation", "entry_quality_score"] if column in entry.columns]
    latest = entry.tail(10)[latest_cols].to_string(index=False) if total and latest_cols else "N/A"
    production_class_col = "production_universe_classification"
    if production_class_col not in entry.columns:
        production_class_col = "watchlist_tier"
    sections = [
        "Entry Timing Operational Summary",
        f"Total evaluated candidates: {total}",
        f"Average Entry Quality Score: {score.mean():.1f}" if score.notna().any() else "Average Entry Quality Score: N/A",
        f"Linked closed outcomes: {linked}",
        f"Data readiness: {readiness_status(linked)}",
        "",
        "Recommendation Counts:",
        *value_counts_lines(entry, "recommendation", total),
        "",
        "By Direction:",
        *value_counts_lines(entry, "direction", total),
        "",
        "By Session:",
        *value_counts_lines(entry, "market_session", total),
        "",
        "By Production Universe Classification:",
        *value_counts_lines(entry, production_class_col, total),
        "",
        "Latest 10 Evaluations:",
        latest,
        "",
        "Note: Entry Timing is shadow-mode only. Recommendation win rates are not shown until outcomes are correctly linked.",
    ]
    return "\n".join(sections)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Print Entry Timing operational summary.")
    parser.add_argument("--entry-timing", type=Path, default=ENTRY_TIMING)
    parser.add_argument("--journal", type=Path, default=JOURNAL)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print(build_summary(load_csv(args.entry_timing), load_csv(args.journal)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
