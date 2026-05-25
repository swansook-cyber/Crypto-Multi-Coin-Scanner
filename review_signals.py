# -*- coding: utf-8 -*-
"""Review logged scanner signals from logs/signals.csv."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


BASE_DIR = Path(__file__).resolve().parent
JOURNAL = BASE_DIR / "logs" / "signals.csv"


def main() -> None:
    if not JOURNAL.exists():
        print("No journal found at logs/signals.csv")
        return

    df = pd.read_csv(JOURNAL)
    if df.empty:
        print("Journal is empty.")
        return

    rr = pd.to_numeric(df["risk_reward"], errors="coerce")
    confidence = pd.to_numeric(df["confidence"], errors="coerce")
    total = len(df)
    long_count = int((df["side"] == "LONG").sum())
    short_count = int((df["side"] == "SHORT").sum())

    wins = int((rr >= 1.8).sum())
    win_rate = wins / total * 100 if total else 0
    symbol_stats = df.groupby("symbol")["risk_reward"].apply(lambda s: pd.to_numeric(s, errors="coerce").mean())
    best_coin = symbol_stats.idxmax() if not symbol_stats.empty else "-"
    worst_coin = symbol_stats.idxmin() if not symbol_stats.empty else "-"

    print("Crypto Multi-Coin Scanner Review")
    print("--------------------------------")
    print(f"Signal count: {total}")
    print(f"Long / Short: {long_count} / {short_count}")
    print(f"Average RR: {rr.mean():.2f}")
    print(f"Average confidence: {confidence.mean():.1f}%")
    print(f"Rule-based win rate proxy (RR >= 1.8): {win_rate:.1f}%")
    print(f"Best coin by avg RR: {best_coin} ({symbol_stats.max():.2f})")
    print(f"Worst coin by avg RR: {worst_coin} ({symbol_stats.min():.2f})")


if __name__ == "__main__":
    main()
