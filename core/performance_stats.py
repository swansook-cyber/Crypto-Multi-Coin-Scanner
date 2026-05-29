# -*- coding: utf-8 -*-
"""Performance statistics for scanner validation datasets."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


def load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    for column, default in {
        "timestamp": "",
        "symbol": "",
        "side": "",
        "tier": "",
        "session": "",
        "rr": "",
        "real_rr": "",
        "setup_strength": "",
        "score": "",
        "market_regime": "",
        "htf_alignment": "",
        "volume_spike": "",
        "mfi": "",
        "atr": "",
        "result": "OPEN",
        "outcome": "",
        "pnl_percent": "",
        "holding_minutes": "",
        "ai_commentary_used": "",
    }.items():
        if column not in df.columns:
            df[column] = default
    if "watchlist_tier" in df.columns and df["tier"].fillna("").astype(str).str.strip().eq("").all():
        df["tier"] = df["watchlist_tier"]
    if "market_session" in df.columns and df["session"].fillna("").astype(str).str.strip().eq("").all():
        df["session"] = df["market_session"]
    if "risk_reward" in df.columns and df["rr"].fillna("").astype(str).str.strip().eq("").all():
        df["rr"] = df["risk_reward"]
    if "hit_target" in df.columns and df["outcome"].fillna("").astype(str).str.strip().eq("").all():
        result = df["result"].fillna("").astype(str).str.upper()
        target = df["hit_target"].fillna("").astype(str).str.upper()
        df["outcome"] = result.where(~result.eq("WIN"), "WIN_" + target.replace("", "TP1"))
        df.loc[result.eq("LOSS"), "outcome"] = "LOSS"
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    for column in ["rr", "real_rr", "setup_strength", "score", "pnl_percent", "holding_minutes", "atr"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df["result"] = df["result"].fillna("OPEN").astype(str).str.upper()
    df["outcome"] = df["outcome"].fillna("").astype(str).str.upper()
    df["symbol"] = df["symbol"].fillna("").astype(str).str.upper()
    df["tier"] = df["tier"].fillna("-").replace("", "-").astype(str).str.upper()
    df["session"] = df["session"].fillna("Other").replace("", "Other")
    df["ai_commentary_used"] = df["ai_commentary_used"].fillna("NO").replace("", "NO")
    return df


def closed_trades(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["outcome"].isin(["WIN_TP1", "WIN_TP2", "LOSS", "BREAKEVEN", "EXPIRED"])].copy()


def wins(df: pd.DataFrame) -> pd.Series:
    return df["outcome"].isin(["WIN_TP1", "WIN_TP2"])


def performance_by(df: pd.DataFrame, column: str) -> pd.DataFrame:
    df = normalize(df.copy())
    closed = closed_trades(df)
    if closed.empty or column not in closed.columns:
        return pd.DataFrame(columns=[column, "trades", "wins", "losses", "win_rate", "net_rr", "avg_holding_minutes"])
    rows: list[dict[str, Any]] = []
    for key, group in closed.groupby(closed[column].fillna("-").astype(str)):
        trade_count = len(group)
        win_count = int(wins(group).sum())
        loss_count = int((group["outcome"] == "LOSS").sum())
        rows.append({
            column: key,
            "trades": trade_count,
            "wins": win_count,
            "losses": loss_count,
            "win_rate": win_count / trade_count * 100 if trade_count else 0.0,
            "tp1_rate": (group["outcome"] == "WIN_TP1").mean() * 100 if trade_count else 0.0,
            "tp2_rate": (group["outcome"] == "WIN_TP2").mean() * 100 if trade_count else 0.0,
            "sl_rate": loss_count / trade_count * 100 if trade_count else 0.0,
            "avg_rr": group["rr"].mean(),
            "net_rr": group["real_rr"].fillna(0).sum(),
            "avg_holding_minutes": group["holding_minutes"].mean(),
        })
    return pd.DataFrame(rows).sort_values(["win_rate", "trades"], ascending=[False, False])


def current_streak(df: pd.DataFrame) -> str:
    closed = closed_trades(df).sort_values("timestamp")
    if closed.empty:
        return "-"
    last_is_win = bool(wins(closed.tail(1)).iloc[0])
    label = "WIN" if last_is_win else "LOSS"
    count = 0
    for is_win in reversed(wins(closed).tolist()):
        if bool(is_win) != last_is_win:
            break
        count += 1
    return f"{count} {label}"


def equity_status(drawdown: float, cumulative_rr: float) -> str:
    if drawdown <= -2:
        return "Drawdown"
    if cumulative_rr > 0:
        return "Growth"
    return "Flat"


def summary(df: pd.DataFrame) -> dict[str, Any]:
    normalized = normalize(df.copy())
    closed = closed_trades(normalized)
    total = len(normalized)
    win_count = int(wins(closed).sum()) if not closed.empty else 0
    loss_count = int((closed["outcome"] == "LOSS").sum()) if not closed.empty else 0
    closed_count = len(closed)
    symbol_perf = performance_by(normalized, "symbol")
    tier_perf = performance_by(normalized, "tier")
    session_perf = performance_by(normalized, "session")
    cumulative = closed["real_rr"].fillna(0).cumsum() if not closed.empty else pd.Series(dtype=float)
    peak = cumulative.cummax() if not cumulative.empty else pd.Series(dtype=float)
    drawdown = (cumulative - peak).min() if not cumulative.empty else 0.0
    return {
        "total_signals": total,
        "closed_trades": closed_count,
        "wins": win_count,
        "losses": loss_count,
        "win_rate": win_count / closed_count * 100 if closed_count else 0.0,
        "avg_rr": normalized["rr"].mean() if total else 0.0,
        "net_rr": closed["real_rr"].fillna(0).sum() if closed_count else 0.0,
        "avg_holding_minutes": closed["holding_minutes"].mean() if closed_count else 0.0,
        "max_drawdown": float(drawdown) if not pd.isna(drawdown) else 0.0,
        "best_symbol": symbol_perf.iloc[0]["symbol"] if not symbol_perf.empty else "-",
        "worst_symbol": symbol_perf.iloc[-1]["symbol"] if not symbol_perf.empty else "-",
        "best_session": session_perf.iloc[0]["session"] if not session_perf.empty else "-",
        "top_tier": tier_perf.iloc[0]["tier"] if not tier_perf.empty else "-",
        "current_streak": current_streak(normalized),
        "equity_status": equity_status(float(drawdown) if not pd.isna(drawdown) else 0.0, closed["real_rr"].fillna(0).sum() if closed_count else 0.0),
    }


def rejection_counts(rejected: pd.DataFrame) -> pd.DataFrame:
    if rejected.empty:
        return pd.DataFrame(columns=["reason", "count"])
    reason = rejected.get("reason", pd.Series([], dtype=str)).fillna("").astype(str)
    status = rejected.get("signal_status", pd.Series([], dtype=str)).fillna("").astype(str)
    combined = reason.where(reason.str.strip() != "", status)
    return combined.replace("", "unknown").value_counts().rename_axis("reason").reset_index(name="count")
