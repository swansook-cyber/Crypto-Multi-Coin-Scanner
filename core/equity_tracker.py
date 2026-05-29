# -*- coding: utf-8 -*-
"""Equity curve generation from realized RR."""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from .performance_stats import closed_trades, normalize


EQUITY_COLUMNS = ["timestamp", "balance", "daily_pnl", "cumulative_rr", "drawdown"]


def build_equity_curve(history: pd.DataFrame, starting_balance: float = 1000.0, risk_per_r: float = 10.0) -> pd.DataFrame:
    df = normalize(history.copy())
    closed = closed_trades(df).sort_values("timestamp")
    if closed.empty:
        return pd.DataFrame(columns=EQUITY_COLUMNS)
    closed["real_rr"] = pd.to_numeric(closed["real_rr"], errors="coerce").fillna(0.0)
    closed["date"] = closed["timestamp"].dt.strftime("%Y-%m-%d")
    daily = closed.groupby("date")["real_rr"].sum().reset_index(name="daily_rr")
    daily["daily_pnl"] = daily["daily_rr"] * risk_per_r
    daily["cumulative_rr"] = daily["daily_rr"].cumsum()
    daily["balance"] = starting_balance + daily["daily_pnl"].cumsum()
    daily["peak_balance"] = daily["balance"].cummax().clip(lower=starting_balance)
    daily["drawdown"] = daily["balance"] - daily["peak_balance"]
    return daily.rename(columns={"date": "timestamp"})[EQUITY_COLUMNS]


def sync_equity_curve(history: pd.DataFrame, path: Path, starting_balance: float = 1000.0, risk_per_r: float = 10.0) -> pd.DataFrame:
    curve = build_equity_curve(history, starting_balance=starting_balance, risk_per_r=risk_per_r)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        curve.to_csv(handle, index=False)
        handle.flush()
        os.fsync(handle.fileno())
    return curve


def equity_curve_status(curve: pd.DataFrame) -> str:
    if curve.empty:
        return "Flat"
    latest = curve.iloc[-1]
    drawdown = float(latest.get("drawdown", 0.0) or 0.0)
    cumulative_rr = float(latest.get("cumulative_rr", 0.0) or 0.0)
    if drawdown < 0:
        return "Drawdown"
    if cumulative_rr > 0:
        return "Growth"
    return "Flat"
