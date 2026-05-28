# -*- coding: utf-8 -*-
"""Suggest watchlist tier promotions/demotions from journal outcomes."""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
JOURNAL = BASE_DIR / "logs" / "signals.csv"
REPORT_DIR = BASE_DIR / "reports"


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def load_journal() -> pd.DataFrame:
    if not JOURNAL.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(JOURNAL)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def recommendation(symbol: str, tier: str, trades: int, win_rate: float, min_trades: int, promote_rate: int, demote_rate: int) -> str:
    if trades < min_trades:
        return f"{symbol}: keep Tier {tier} (need more data)"
    if win_rate >= promote_rate and tier == "B":
        return f"{symbol}: promote Tier B -> Tier A candidate"
    if win_rate >= promote_rate and tier == "C":
        return f"{symbol}: promote Tier C -> Tier B candidate"
    if win_rate < demote_rate and tier == "A":
        return f"{symbol}: demote Tier A -> Tier B candidate"
    if win_rate < demote_rate and tier == "B":
        return f"{symbol}: demote Tier B -> Tier C candidate"
    if win_rate < demote_rate and tier == "C":
        return f"{symbol}: demote Tier C / remove candidate"
    return f"{symbol}: keep Tier {tier}"


def build_recommendations(df: pd.DataFrame, min_trades: int, promote_rate: int, demote_rate: int) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["symbol", "current_tier", "trades", "wins", "losses", "win_rate", "recommendation"])
    for column, default in {"watchlist_tier": "B", "result": "", "symbol": ""}.items():
        if column not in df.columns:
            df[column] = default
    closed = df[df["result"].astype(str).str.upper().isin(["WIN", "LOSS"])].copy()
    rows = []
    for symbol, group in closed.groupby(closed["symbol"].astype(str).str.upper()):
        tier = str(group["watchlist_tier"].dropna().iloc[-1] if not group["watchlist_tier"].dropna().empty else "B").upper()
        wins = int((group["result"].astype(str).str.upper() == "WIN").sum())
        losses = int((group["result"].astype(str).str.upper() == "LOSS").sum())
        trades = wins + losses
        win_rate = wins / trades * 100 if trades else 0.0
        rows.append({
            "symbol": symbol,
            "current_tier": tier,
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "recommendation": recommendation(symbol, tier, trades, win_rate, min_trades, promote_rate, demote_rate),
        })
    return pd.DataFrame(rows).sort_values(["win_rate", "trades"], ascending=[False, False])


def main() -> int:
    load_dotenv(BASE_DIR / ".env")
    REPORT_DIR.mkdir(exist_ok=True)
    min_trades = env_int("TIER_REVIEW_MIN_TRADES", 20)
    promote_rate = env_int("TIER_PROMOTE_WINRATE", 60)
    demote_rate = env_int("TIER_DEMOTE_WINRATE", 40)
    df = load_journal()
    report = build_recommendations(df, min_trades, promote_rate, demote_rate)
    output = REPORT_DIR / "tier_recommendations.csv"
    report.to_csv(output, index=False)
    print("Tier Review Recommendations")
    print("---------------------------")
    if report.empty:
        print("No closed trade data yet.")
    else:
        for _, row in report.iterrows():
            print(row["recommendation"])
    print(f"Saved: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
