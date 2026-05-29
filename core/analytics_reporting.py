# -*- coding: utf-8 -*-
"""Reusable analytics/reporting helpers for journal-derived performance data."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


JOURNAL_SIGNAL_COLUMNS = [
    "timestamp",
    "symbol",
    "direction",
    "wave_score",
    "btc_regime",
    "entry",
    "tp",
    "sl",
    "result",
    "pnl_percent",
]

JOURNAL_DAILY_COLUMNS = [
    "date",
    "signals",
    "wins",
    "losses",
    "pending",
    "win_rate",
    "btc_regime_breakdown",
    "wave_score_breakdown",
    "best_coin",
    "worst_coin",
]


def load_csv_safely(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except (pd.errors.EmptyDataError, FileNotFoundError, OSError):
        return pd.DataFrame()


def normalize_signals(df: pd.DataFrame) -> pd.DataFrame:
    normalized = df.copy()
    defaults = {
        "timestamp": "",
        "symbol": "",
        "side": "",
        "direction": "",
        "wave_score": "",
        "btc_regime": "",
        "entry": "",
        "tp1": "",
        "tp2": "",
        "tp": "",
        "stop_loss": "",
        "sl": "",
        "result": "OPEN",
        "pnl_percent": "",
        "hit_target": "",
    }
    for column, default in defaults.items():
        if column not in normalized.columns:
            normalized[column] = default

    if normalized["direction"].fillna("").astype(str).str.strip().eq("").all():
        normalized["direction"] = normalized["side"]
    if normalized["tp"].fillna("").astype(str).str.strip().eq("").all():
        normalized["tp"] = normalized["tp2"].where(
            normalized["tp2"].fillna("").astype(str).str.strip() != "",
            normalized["tp1"],
        )
    if normalized["sl"].fillna("").astype(str).str.strip().eq("").all():
        normalized["sl"] = normalized["stop_loss"]

    normalized["timestamp"] = pd.to_datetime(normalized["timestamp"], utc=True, errors="coerce")
    normalized["symbol"] = normalized["symbol"].fillna("").astype(str).str.upper()
    normalized["direction"] = normalized["direction"].fillna("").astype(str).str.upper()
    normalized["result"] = normalized["result"].fillna("OPEN").replace("", "OPEN").astype(str).str.upper()
    normalized["btc_regime"] = normalized["btc_regime"].fillna("unclear").replace("", "unclear").astype(str).str.lower()
    for column in ["wave_score", "entry", "tp", "sl", "pnl_percent"]:
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    return normalized


def wave_score_bucket(value: Any) -> str:
    score = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(score):
        return "unknown"
    score = float(score)
    if score >= 80:
        return "80-100"
    if score >= 60:
        return "60-79"
    if score >= 40:
        return "40-59"
    return "0-39"


def format_counts(series: pd.Series) -> str:
    if series.empty:
        return "-"
    counts = series.fillna("-").astype(str).replace("", "-").value_counts()
    return ", ".join(f"{key}: {count}" for key, count in counts.items()) if not counts.empty else "-"


def _symbol_by_winrate(df: pd.DataFrame, best: bool) -> str:
    closed = df[df["result"].isin(["WIN", "LOSS"])].copy()
    if closed.empty:
        return "-"
    rows = []
    for symbol, group in closed.groupby("symbol"):
        if not symbol:
            continue
        trades = len(group)
        wins = int((group["result"] == "WIN").sum())
        rows.append({"symbol": symbol, "trades": trades, "win_rate": wins / trades * 100 if trades else 0.0})
    if not rows:
        return "-"
    ranked = pd.DataFrame(rows).sort_values(["win_rate", "trades"], ascending=[not best, False])
    return str(ranked.iloc[0]["symbol"])


def build_daily_performance_report(df: pd.DataFrame, date: str | None = None) -> dict[str, Any]:
    normalized = normalize_signals(df)
    if date is None:
        valid_dates = normalized["timestamp"].dropna()
        date = valid_dates.max().strftime("%Y-%m-%d") if not valid_dates.empty else pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
    if normalized.empty:
        day_df = normalized
    else:
        day_df = normalized[normalized["timestamp"].dt.strftime("%Y-%m-%d") == date].copy()

    total = int(len(day_df))
    wins = int((day_df["result"] == "WIN").sum()) if total else 0
    losses = int((day_df["result"] == "LOSS").sum()) if total else 0
    pending = int((day_df["result"] == "OPEN").sum()) if total else 0
    closed = wins + losses
    wave_buckets = day_df["wave_score"].map(wave_score_bucket) if total else pd.Series(dtype=str)

    return {
        "date": date,
        "signals": total,
        "wins": wins,
        "losses": losses,
        "pending": pending,
        "win_rate": wins / closed * 100 if closed else 0.0,
        "btc_regime_breakdown": format_counts(day_df["btc_regime"]) if total else "-",
        "wave_score_breakdown": format_counts(wave_buckets) if total else "-",
        "best_coin": _symbol_by_winrate(day_df, best=True),
        "worst_coin": _symbol_by_winrate(day_df, best=False),
    }


def journal_signal_export(df: pd.DataFrame) -> pd.DataFrame:
    normalized = normalize_signals(df)
    export = pd.DataFrame(
        {
            "timestamp": normalized["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "symbol": normalized["symbol"],
            "direction": normalized["direction"],
            "wave_score": normalized["wave_score"],
            "btc_regime": normalized["btc_regime"],
            "entry": normalized["entry"],
            "tp": normalized["tp"],
            "sl": normalized["sl"],
            "result": normalized["result"],
            "pnl_percent": normalized["pnl_percent"],
        },
        columns=JOURNAL_SIGNAL_COLUMNS,
    )
    return export


def export_journal_csvs(df: pd.DataFrame, journal_dir: Path, summary: dict[str, Any]) -> tuple[Path, Path]:
    journal_dir.mkdir(parents=True, exist_ok=True)
    signals_path = journal_dir / "signals.csv"
    daily_path = journal_dir / "daily_summary.csv"

    journal_signal_export(df).to_csv(signals_path, index=False)
    daily_row = pd.DataFrame(
        [
            {
                "date": summary.get("date", summary.get("day", "")),
                "signals": summary.get("signals", summary.get("total_signals", 0)),
                "wins": summary.get("wins", 0),
                "losses": summary.get("losses", 0),
                "pending": summary.get("pending", 0),
                "win_rate": f"{float(summary.get('win_rate', 0.0)):.2f}",
                "btc_regime_breakdown": summary.get("btc_regime_breakdown", "-"),
                "wave_score_breakdown": summary.get("wave_score_breakdown", "-"),
                "best_coin": summary.get("best_coin", summary.get("best_symbol", "-")),
                "worst_coin": summary.get("worst_coin", summary.get("worst_symbol", "-")),
            }
        ],
        columns=JOURNAL_DAILY_COLUMNS,
    )
    if daily_path.exists():
        existing = load_csv_safely(daily_path)
        daily = pd.concat([existing, daily_row], ignore_index=True)
        if "date" in daily.columns:
            daily = daily.drop_duplicates("date", keep="last")
    else:
        daily = daily_row
    daily.to_csv(daily_path, index=False)
    return signals_path, daily_path
