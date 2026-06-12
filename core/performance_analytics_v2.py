# -*- coding: utf-8 -*-
"""Performance Analytics V2 helpers.

Analytics-only module. It reads normalized signal outcomes and produces
rankings/warnings without changing scanner strategy, filters, or signal logic.
"""

from __future__ import annotations

from typing import Any

import pandas as pd


WARNING_MIN_TRADES = 5
WARNING_WIN_RATE = 40.0


def _num(value: Any) -> float | None:
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric):
        return None
    return float(numeric)


def _safe_mean(values: pd.Series | list[float | None]) -> float | None:
    series = pd.to_numeric(pd.Series(values), errors="coerce").dropna()
    if series.empty:
        return None
    return float(series.mean())


def _sent_signals(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    if "signal_status" not in df.columns:
        return df.copy()
    return df[df["signal_status"].fillna("sent").astype(str).str.lower() == "sent"].copy()


def _closed_trades(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "result" not in df.columns:
        return pd.DataFrame(columns=df.columns)
    return df[df["result"].isin(["WIN", "LOSS"])].copy()


def _winning_trades(df: pd.DataFrame) -> pd.DataFrame:
    closed = _closed_trades(df)
    return closed[closed["result"] == "WIN"].copy()


def _losing_trades(df: pd.DataFrame) -> pd.DataFrame:
    closed = _closed_trades(df)
    return closed[closed["result"] == "LOSS"].copy()


def _hit_level(value: Any) -> int:
    text = str(value).strip().upper()
    if not text or text in {"NAN", "NONE", "NULL"}:
        return 0
    if text.startswith("TP"):
        text = text.replace("TP", "", 1)
    numeric = _num(text)
    return 0 if numeric is None else max(0, int(numeric))


def _estimated_r(row: pd.Series) -> float:
    real_rr = _num(row.get("real_rr"))
    if real_rr is not None:
        return real_rr
    if str(row.get("result", "")).upper() == "LOSS":
        return -1.0
    rr = _num(row.get("rr")) or _num(row.get("risk_reward")) or 0.0
    level = _hit_level(row.get("hit_target", ""))
    if level >= 3:
        return rr if rr > 0 else 3.0
    if level >= 2:
        return rr if rr > 0 else 2.0
    if str(row.get("result", "")).upper() == "WIN":
        return min(rr, 1.2) if rr > 0 else 1.0
    return 0.0


def _calculate_pnl(row: pd.Series) -> float | None:
    pnl = _num(row.get("pnl_percent"))
    if pnl is not None:
        return pnl
    entry = _num(row.get("entry"))
    if entry is None or entry <= 0:
        return None
    side = str(row.get("side", "")).upper()
    result = str(row.get("result", "")).upper()
    if result == "LOSS":
        sl = _num(row.get("sl")) or _num(row.get("stop_loss"))
        if sl is None:
            return None
        raw = (sl - entry) / entry * 100 if side == "LONG" else (entry - sl) / entry * 100
        return -abs(raw)
    if result == "WIN":
        level = _hit_level(row.get("hit_target", ""))
        target_column = "tp3" if level >= 3 else "tp2" if level >= 2 else "tp1"
        target = _num(row.get(target_column))
        if target is None:
            return None
        raw = (target - entry) / entry * 100 if side == "LONG" else (entry - target) / entry * 100
        return abs(raw)
    return None


def _holding_minutes(df: pd.DataFrame) -> pd.Series:
    if df.empty:
        return pd.Series(dtype=float)
    if "holding_minutes" in df.columns:
        values = pd.to_numeric(df["holding_minutes"], errors="coerce")
        if values.notna().any():
            return values
    if "timestamp" not in df.columns or "closed_at" not in df.columns:
        return pd.Series(dtype=float)
    start = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    end = pd.to_datetime(df["closed_at"], utc=True, errors="coerce")
    return ((end - start).dt.total_seconds() / 60).clip(lower=0)


def canonical_session(value: Any) -> str:
    text = str(value or "").strip()
    compact = text.replace(" ", "").replace("_", "").lower()
    if not compact or compact in {"-", "other", "offhour", "offhours", "external"}:
        return "OffHours"
    has_london = "london" in compact
    has_newyork = "newyork" in compact or "ny" == compact
    if has_london and has_newyork:
        return "London+NewYork"
    if has_london:
        return "London"
    if has_newyork:
        return "NewYork"
    if "asia" in compact:
        return "Asia"
    return "OffHours"


def _group_table(df: pd.DataFrame, column: str, label: str) -> pd.DataFrame:
    columns = [
        label,
        "Trades",
        "Wins",
        "Losses",
        "Win Rate",
        "Net R",
        "Avg Profit %",
        "Avg Loss %",
        "Avg Time To TP",
        "Avg Time To SL",
    ]
    sent = _sent_signals(df)
    closed = _closed_trades(sent)
    if closed.empty or column not in closed.columns:
        return pd.DataFrame(columns=columns)

    rows: list[dict[str, Any]] = []
    for key, group in closed.groupby(closed[column].fillna("-").replace("", "-").astype(str), dropna=False):
        wins = _winning_trades(group)
        losses = _losing_trades(group)
        pnl_values = group.apply(_calculate_pnl, axis=1)
        win_pnl = [value for value in pnl_values if value is not None and value > 0]
        loss_pnl = [value for value in pnl_values if value is not None and value < 0]
        rows.append(
            {
                label: key,
                "Trades": int(len(group)),
                "Wins": int(len(wins)),
                "Losses": int(len(losses)),
                "Win Rate": round(len(wins) / len(group) * 100, 1) if len(group) else 0.0,
                "Net R": round(float(group.apply(_estimated_r, axis=1).sum()), 2),
                "Avg Profit %": round(_safe_mean(win_pnl) or 0.0, 2),
                "Avg Loss %": round(_safe_mean(loss_pnl) or 0.0, 2),
                "Avg Time To TP": round(_safe_mean(_holding_minutes(wins)) or 0.0, 1),
                "Avg Time To SL": round(_safe_mean(_holding_minutes(losses)) or 0.0, 1),
            }
        )

    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows, columns=columns).sort_values(["Win Rate", "Net R", "Trades"], ascending=[False, False, False])


def symbol_performance_table(df: pd.DataFrame) -> pd.DataFrame:
    return _group_table(df, "symbol", "Symbol")


def session_performance_table(df: pd.DataFrame) -> pd.DataFrame:
    data = df.copy()
    if "session" in data.columns:
        session_source = data["session"]
    elif "market_session" in data.columns:
        session_source = data["market_session"]
    else:
        session_source = pd.Series(["OffHours"] * len(data), index=data.index)
    data["session_v2"] = session_source.map(canonical_session)
    table = _group_table(data, "session_v2", "Session")
    if table.empty:
        return table
    order = ["Asia", "London", "NewYork", "London+NewYork", "OffHours"]
    table["_order"] = table["Session"].map(lambda value: order.index(value) if value in order else len(order))
    return table.sort_values("_order").drop(columns="_order")


def direction_performance_table(df: pd.DataFrame) -> pd.DataFrame:
    table = _group_table(df, "side", "Direction")
    if table.empty:
        return table
    order = {"LONG": 0, "SHORT": 1}
    table["_order"] = table["Direction"].map(lambda value: order.get(str(value).upper(), 99))
    return table.sort_values("_order").drop(columns="_order")


def tier_performance_table(df: pd.DataFrame) -> pd.DataFrame:
    tier_column = "tier" if "tier" in df.columns else "watchlist_tier"
    table = _group_table(df, tier_column, "Tier")
    if table.empty:
        return table
    order = {"A": 0, "B": 1, "C": 2}
    table["_order"] = table["Tier"].map(lambda value: order.get(str(value).upper(), 99))
    return table.sort_values("_order").drop(columns="_order")


def top_symbols(df: pd.DataFrame, limit: int = 10) -> pd.DataFrame:
    table = symbol_performance_table(df)
    if table.empty:
        return table
    return table.sort_values(["Net R", "Win Rate", "Trades"], ascending=[False, False, False]).head(limit)


def bottom_symbols(df: pd.DataFrame, limit: int = 10) -> pd.DataFrame:
    table = symbol_performance_table(df)
    if table.empty:
        return table
    return table.sort_values(["Net R", "Win Rate", "Trades"], ascending=[True, True, False]).head(limit)


def _warning_rows(table: pd.DataFrame, label: str, title: str, min_trades: int, max_win_rate: float) -> list[str]:
    if table.empty or label not in table.columns:
        return []
    risky = table[(table["Trades"] >= min_trades) & (table["Win Rate"] <= max_win_rate)]
    messages = []
    for _, row in risky.sort_values(["Win Rate", "Net R", "Trades"], ascending=[True, True, False]).iterrows():
        messages.append(
            f"⚠ {title} Warning:\n"
            f"{row[label]}\n"
            f"Trades: {int(row['Trades'])}\n"
            f"Win Rate: {float(row['Win Rate']):.1f}%"
        )
    return messages


def generate_performance_warnings(
    df: pd.DataFrame,
    min_trades: int = WARNING_MIN_TRADES,
    max_win_rate: float = WARNING_WIN_RATE,
) -> list[str]:
    warnings: list[str] = []
    warnings.extend(_warning_rows(symbol_performance_table(df), "Symbol", "Symbol", min_trades, max_win_rate))
    warnings.extend(_warning_rows(session_performance_table(df), "Session", "Session", min_trades, max_win_rate))
    warnings.extend(_warning_rows(direction_performance_table(df), "Direction", "Direction", min_trades, max_win_rate))
    warnings.extend(_warning_rows(tier_performance_table(df), "Tier", "Tier", min_trades, max_win_rate))
    return warnings


def build_performance_v2(df: pd.DataFrame) -> dict[str, Any]:
    symbol_table = symbol_performance_table(df)
    return {
        "symbol_performance_v2": symbol_table,
        "top_symbols": top_symbols(df),
        "bottom_symbols": bottom_symbols(df),
        "session_performance_v2": session_performance_table(df),
        "direction_performance_v2": direction_performance_table(df),
        "tier_performance_v2": tier_performance_table(df),
        "warnings": generate_performance_warnings(df),
    }
