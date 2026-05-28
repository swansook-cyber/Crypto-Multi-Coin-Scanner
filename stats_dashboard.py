# -*- coding: utf-8 -*-
"""Generate performance reports from logs/signals.csv."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
JOURNAL = BASE_DIR / "logs" / "signals.csv"
REPORT_DIR = BASE_DIR / "reports"


def load_journal() -> pd.DataFrame:
    if not JOURNAL.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(JOURNAL)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    for column, default in {
        "signal_status": "sent",
        "result": "",
        "hit_target": "",
        "watchlist_tier": "B",
        "side": "",
        "market_regime": "",
        "mfi_confirmed": "",
        "market_session": "",
        "score_bucket": "",
        "setup_strength": "",
        "htf_alignment": "",
        "htf_conflict": "",
    }.items():
        if column not in df.columns:
            df[column] = default
    df["timestamp"] = pd.to_datetime(df.get("timestamp"), utc=True, errors="coerce")
    df["closed_at"] = pd.to_datetime(df.get("closed_at"), utc=True, errors="coerce")
    df["risk_reward"] = pd.to_numeric(df.get("risk_reward"), errors="coerce")
    df["setup_strength"] = pd.to_numeric(df.get("setup_strength"), errors="coerce").fillna(pd.to_numeric(df.get("confidence"), errors="coerce"))
    if df["score_bucket"].fillna("").astype(str).str.strip().eq("").all():
        df["score_bucket"] = df["setup_strength"].apply(bucket_strength)
    df["htf_conflict"] = df["htf_conflict"].where(df["htf_conflict"].astype(str).str.strip() != "", df["htf_alignment"].astype(str).str.upper().map(lambda value: "YES" if value == "CONFLICT" else "NO"))
    return df


def bucket_strength(value: float) -> str:
    if pd.isna(value):
        return "Unknown"
    if value >= 90:
        return "A+"
    if value >= 80:
        return "A"
    if value >= 70:
        return "B"
    return "C"


def strength_range(value: float) -> str:
    if pd.isna(value):
        return "Unknown"
    lower = int(value // 10 * 10)
    upper = lower + 9
    return f"{lower}-{upper}"


def closed_trades(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["result"].astype(str).str.upper().isin(["WIN", "LOSS"])].copy()


def holding_hours(df: pd.DataFrame) -> pd.Series:
    return (df["closed_at"] - df["timestamp"]).dt.total_seconds() / 3600


def profit_factor(df: pd.DataFrame) -> float:
    closed = closed_trades(df)
    if closed.empty:
        return 0.0
    wins = closed[closed["result"].astype(str).str.upper() == "WIN"]
    losses = closed[closed["result"].astype(str).str.upper() == "LOSS"]
    gross_win = pd.to_numeric(wins.get("risk_reward"), errors="coerce").fillna(0).sum()
    gross_loss = float(len(losses))
    return float(gross_win / gross_loss) if gross_loss else float(gross_win)


def performance_by(df: pd.DataFrame, column: str) -> pd.DataFrame:
    closed = closed_trades(df)
    if closed.empty or column not in closed.columns:
        return pd.DataFrame(columns=[column, "trades", "wins", "losses", "win_rate", "avg_rr"])
    rows = []
    for key, group in closed.groupby(closed[column].fillna("-").astype(str)):
        wins = int((group["result"].astype(str).str.upper() == "WIN").sum())
        losses = int((group["result"].astype(str).str.upper() == "LOSS").sum())
        trades = wins + losses
        rows.append({
            column: key,
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "win_rate": wins / trades * 100 if trades else 0.0,
            "tp1_rate": (group["hit_target"].astype(str).str.upper() == "TP1").mean() * 100 if trades else 0.0,
            "tp2_rate": (group["hit_target"].astype(str).str.upper() == "TP2").mean() * 100 if trades else 0.0,
            "sl_rate": losses / trades * 100 if trades else 0.0,
            "avg_rr": pd.to_numeric(group["risk_reward"], errors="coerce").mean(),
            "avg_holding_hours": holding_hours(group).mean(),
            "profit_factor": profit_factor(group),
        })
    return pd.DataFrame(rows).sort_values(["win_rate", "trades"], ascending=[False, False])


def build_summary(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame([{"metric": "total_signals", "value": 0}])
    sent = df[df["signal_status"].fillna("sent") == "sent"]
    closed = closed_trades(df)
    wins = int((closed["result"].astype(str).str.upper() == "WIN").sum())
    losses = int((closed["result"].astype(str).str.upper() == "LOSS").sum())
    closed_count = wins + losses
    symbol_perf = performance_by(df, "symbol")
    tier_perf = performance_by(df, "watchlist_tier")
    rows = [
        ("total_signals", len(df)),
        ("sent_signals", len(sent)),
        ("win_rate", wins / closed_count * 100 if closed_count else 0.0),
        ("tp1_hit_rate", (closed["hit_target"].astype(str).str.upper() == "TP1").mean() * 100 if not closed.empty else 0.0),
        ("tp2_hit_rate", (closed["hit_target"].astype(str).str.upper() == "TP2").mean() * 100 if not closed.empty else 0.0),
        ("sl_rate", losses / closed_count * 100 if closed_count else 0.0),
        ("avg_holding_hours", holding_hours(closed).mean() if not closed.empty else 0.0),
        ("avg_rr", pd.to_numeric(sent["risk_reward"], errors="coerce").mean() if not sent.empty else 0.0),
        ("profit_factor", profit_factor(df)),
        ("best_symbol", symbol_perf.iloc[0]["symbol"] if not symbol_perf.empty else "-"),
        ("worst_symbol", symbol_perf.iloc[-1]["symbol"] if not symbol_perf.empty else "-"),
        ("best_tier", tier_perf.iloc[0]["watchlist_tier"] if not tier_perf.empty else "-"),
        ("worst_tier", tier_perf.iloc[-1]["watchlist_tier"] if not tier_perf.empty else "-"),
    ]
    return pd.DataFrame(rows, columns=["metric", "value"])


def print_report(summary: pd.DataFrame, tables: dict[str, pd.DataFrame]) -> None:
    print("Crypto Scanner Stats Dashboard")
    print("------------------------------")
    print(summary.to_string(index=False))
    for name, table in tables.items():
        print()
        print(name)
        print("-" * len(name))
        if table.empty:
            print("No data")
        else:
            print(table.head(20).to_string(index=False))


def main() -> int:
    load_dotenv(BASE_DIR / ".env")
    REPORT_DIR.mkdir(exist_ok=True)
    df = normalize(load_journal())
    summary = build_summary(df)
    tables = {
        "Winrate by Symbol": performance_by(df, "symbol"),
        "Winrate by Tier": performance_by(df, "watchlist_tier"),
        "Winrate by Side": performance_by(df, "side"),
        "Winrate by Market Regime": performance_by(df, "market_regime"),
        "Winrate by MFI Confirmed": performance_by(df, "mfi_confirmed"),
        "Winrate by Score Bucket": performance_by(df, "score_bucket"),
        "Winrate by Setup Strength Range": performance_by(df.assign(setup_strength_range=df["setup_strength"].apply(strength_range)), "setup_strength_range"),
        "Winrate by HTF Alignment": performance_by(df, "htf_alignment"),
        "Winrate by HTF Conflict": performance_by(df, "htf_conflict"),
        "Winrate by Session": performance_by(df, "market_session"),
    }
    summary.to_csv(REPORT_DIR / "stats_summary.csv", index=False)
    tables["Winrate by Symbol"].to_csv(REPORT_DIR / "symbol_performance.csv", index=False)
    tables["Winrate by Tier"].to_csv(REPORT_DIR / "tier_performance.csv", index=False)
    print_report(summary, tables)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
