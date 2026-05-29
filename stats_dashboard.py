# -*- coding: utf-8 -*-
"""Generate performance reports from logs/signals.csv."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from core.analytics_engine import update_validation_artifacts
from core.performance_stats import load_csv as load_performance_csv
from core.performance_stats import normalize as normalize_performance
from core.performance_stats import performance_by as core_performance_by
from core.performance_stats import rejection_counts, summary as core_summary


BASE_DIR = Path(__file__).resolve().parent
JOURNAL = BASE_DIR / "logs" / "signals.csv"
HISTORY = BASE_DIR / "logs" / "signals_history.csv"
REJECTED = BASE_DIR / "logs" / "rejected_signals.csv"
EQUITY = BASE_DIR / "logs" / "equity_curve.csv"
PERFORMANCE_REPORT = BASE_DIR / "logs" / "performance_report.txt"
REPORT_DIR = BASE_DIR / "reports"


def load_journal() -> pd.DataFrame:
    source = HISTORY if HISTORY.exists() else JOURNAL
    if not source.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(source)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def load_rejected() -> pd.DataFrame:
    return load_performance_csv(REJECTED)


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    for column, default in {
        "signal_status": "sent",
        "result": "",
        "hit_target": "",
        "watchlist_tier": "B",
        "tier": "",
        "side": "",
        "session": "",
        "market_regime": "",
        "mfi_confirmed": "",
        "market_session": "",
        "score_bucket": "",
        "setup_strength": "",
        "htf_alignment": "",
        "htf_conflict": "",
        "risk_reward": "",
        "timestamp": "",
        "closed_at": "",
        "holding_minutes": "",
    }.items():
        if column not in df.columns:
            df[column] = default
    if df["watchlist_tier"].fillna("").astype(str).str.strip().eq("").all() and "tier" in df.columns:
        df["watchlist_tier"] = df["tier"]
    if df["market_session"].fillna("").astype(str).str.strip().eq("").all() and "session" in df.columns:
        df["market_session"] = df["session"]
    if "rr" in df.columns and df["risk_reward"].fillna("").astype(str).str.strip().eq("").all():
        df["risk_reward"] = df["rr"]
    if "sl" in df.columns and "stop_loss" not in df.columns:
        df["stop_loss"] = df["sl"]
    df["timestamp"] = pd.to_datetime(df.get("timestamp"), utc=True, errors="coerce")
    df["closed_at"] = pd.to_datetime(df.get("closed_at"), utc=True, errors="coerce")
    df["risk_reward"] = pd.to_numeric(df.get("risk_reward"), errors="coerce")
    if "real_rr" in df.columns:
        df["real_rr"] = pd.to_numeric(df.get("real_rr"), errors="coerce")
    else:
        df["real_rr"] = df["risk_reward"].where(df["result"].astype(str).str.upper() == "WIN", -1)
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
    if "holding_minutes" in df.columns:
        minutes = pd.to_numeric(df["holding_minutes"], errors="coerce")
        if minutes.notna().any():
            return minutes / 60
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


def distribution(df: pd.DataFrame, column: str, bins: list[float], labels: list[str]) -> pd.DataFrame:
    if column not in df.columns:
        return pd.DataFrame(columns=["bucket", "count"])
    values = pd.to_numeric(df[column], errors="coerce").dropna()
    if values.empty:
        return pd.DataFrame(columns=["bucket", "count"])
    buckets = pd.cut(values, bins=bins, labels=labels, include_lowest=True)
    return buckets.value_counts().sort_index().rename_axis("bucket").reset_index(name="count")


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
        ("avg_holding_time_minutes", holding_hours(closed).mean() * 60 if not closed.empty else 0.0),
        ("profit_factor", profit_factor(df)),
        ("best_symbol", symbol_perf.iloc[0]["symbol"] if not symbol_perf.empty else "-"),
        ("worst_symbol", symbol_perf.iloc[-1]["symbol"] if not symbol_perf.empty else "-"),
        ("best_tier", tier_perf.iloc[0]["watchlist_tier"] if not tier_perf.empty else "-"),
        ("worst_tier", tier_perf.iloc[-1]["watchlist_tier"] if not tier_perf.empty else "-"),
    ]
    return pd.DataFrame(rows, columns=["metric", "value"])


def adaptive_suggestions(df: pd.DataFrame) -> list[str]:
    suggestions: list[str] = []
    symbol_perf = performance_by(df, "symbol")
    if not symbol_perf.empty:
        weak_symbols = symbol_perf[(symbol_perf["trades"] >= 10) & (symbol_perf["win_rate"] < 35)]
        for _, row in weak_symbols.iterrows():
            suggestions.append(
                f"Temporarily blacklist {row['symbol']} for 7 days: winrate {row['win_rate']:.1f}% over {int(row['trades'])} closed trades."
            )
    htf_perf = performance_by(df, "htf_alignment")
    if not htf_perf.empty:
        htf_no = htf_perf[htf_perf["htf_alignment"].astype(str).str.upper().isin(["NO", "CONFLICT", "MISALIGNED"])]
        poor_htf_no = htf_no[(htf_no["trades"] >= 3) & (htf_no["win_rate"] < 35)]
        if not poor_htf_no.empty:
            suggestions.append(
                "HTF alignment NO/Conflict is underperforming; consider reducing score for misaligned setups."
            )
    if not suggestions:
        suggestions.append("No adaptive filtering changes suggested yet; collect more closed outcomes.")
    return suggestions


def write_performance_report(summary: pd.DataFrame, tables: dict[str, pd.DataFrame], suggestions: list[str]) -> None:
    PERFORMANCE_REPORT.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "Crypto Multi-Coin Scanner Performance Report",
        "============================================",
        "",
        summary.to_string(index=False),
    ]
    for name, table in tables.items():
        lines.extend(["", name, "-" * len(name)])
        lines.append("No data" if table.empty else table.head(20).to_string(index=False))
    lines.extend(["", "Adaptive Filtering Suggestions", "-------------------------------"])
    lines.extend(f"- {item}" for item in suggestions)
    PERFORMANCE_REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
    journal = load_performance_csv(JOURNAL)
    if not journal.empty:
        update_validation_artifacts(journal, BASE_DIR / "logs")
    df = normalize(load_journal())
    rejected = load_rejected()
    core_df = normalize_performance(load_journal())
    core_stats = core_summary(core_df)
    summary = build_summary(df)
    tables = {
        "Winrate by Symbol": performance_by(df, "symbol"),
        "Winrate by Tier": performance_by(df, "watchlist_tier"),
        "Winrate by Side": performance_by(df, "side"),
        "Winrate by Market Regime": performance_by(df, "market_regime"),
        "Winrate by MFI Confirmed": performance_by(df, "mfi_confirmed"),
        "AI Commentary ON vs OFF": core_performance_by(core_df, "ai_commentary_used"),
        "Winrate by Score Bucket": performance_by(df, "score_bucket"),
        "Winrate by Setup Strength Range": performance_by(df.assign(setup_strength_range=df["setup_strength"].apply(strength_range)), "setup_strength_range"),
        "Winrate by HTF Alignment": performance_by(df, "htf_alignment"),
        "Winrate by HTF Conflict": performance_by(df, "htf_conflict"),
        "Winrate by Session": performance_by(df, "market_session"),
        "RR Distribution": distribution(core_df, "real_rr", [-10, -1, 0, 1, 2, 5, 10], ["<=-1R", "-1-0R", "0-1R", "1-2R", "2-5R", "5R+"]),
        "Holding Time Distribution": distribution(core_df, "holding_minutes", [0, 60, 240, 720, 1440, 100000], ["<1h", "1-4h", "4-12h", "12-24h", "24h+"]),
        "Top Rejection Reasons": rejection_counts(rejected).head(7),
    }
    suggestions = adaptive_suggestions(df)
    if core_stats.get("equity_status") == "Drawdown":
        suggestions.append("Equity curve is in drawdown; consider reducing signal frequency until recovery.")
    summary.to_csv(REPORT_DIR / "stats_summary.csv", index=False)
    tables["Winrate by Symbol"].to_csv(REPORT_DIR / "symbol_performance.csv", index=False)
    tables["Winrate by Tier"].to_csv(REPORT_DIR / "tier_performance.csv", index=False)
    write_performance_report(summary, tables, suggestions)
    print_report(summary, tables)
    print()
    print("Adaptive Filtering Suggestions")
    print("-------------------------------")
    for suggestion in suggestions:
        print(f"- {suggestion}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
