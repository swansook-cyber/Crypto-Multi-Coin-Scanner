# -*- coding: utf-8 -*-
"""Performance Analytics V3 and shadow filter backtests.

Report-only module. It consumes normalized signal rows and never modifies
scanner filters, signal routing, TP/SL logic, or source CSV files.
"""

from __future__ import annotations

from typing import Any, Callable

import pandas as pd

from core.performance_analytics_v2 import canonical_session


MIN_WEAK_SYMBOL_TRADES = 5
WEAK_SYMBOL_WIN_RATE = 40.0


def _num(value: Any) -> float | None:
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric):
        return None
    return float(numeric)


def _sent_signals(df: pd.DataFrame) -> pd.DataFrame:
    data = df.copy()
    if data.empty:
        return data
    if "signal_status" not in data.columns:
        data["signal_status"] = "sent"
    return data[data["signal_status"].fillna("sent").astype(str).str.lower().eq("sent")].copy()


def _closed_trades(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "result" not in df.columns:
        return pd.DataFrame(columns=df.columns)
    return df[df["result"].fillna("").astype(str).str.upper().isin(["WIN", "LOSS"])].copy()


def _hit_level(value: Any) -> int:
    text = str(value).strip().upper()
    if not text or text in {"NAN", "NONE", "NULL"}:
        return 0
    if text.startswith("TP"):
        text = text.replace("TP", "", 1)
    numeric = _num(text)
    return 0 if numeric is None else max(0, int(numeric))


def _first_numeric(data: pd.DataFrame, columns: list[str]) -> pd.Series:
    result = pd.Series([pd.NA] * len(data), index=data.index, dtype="Float64")
    for column in columns:
        if column in data.columns:
            values = pd.to_numeric(data[column], errors="coerce")
            result = result.where(result.notna(), values)
    return result


def estimated_r(row: pd.Series) -> float:
    real_rr = _num(row.get("real_rr"))
    if real_rr is not None:
        return real_rr
    net_r = _num(row.get("net_r_estimate"))
    if net_r is not None:
        return net_r
    result = str(row.get("result", "")).upper()
    if result == "LOSS":
        return -1.0
    rr = _num(row.get("rr")) or _num(row.get("risk_reward")) or 0.0
    level = _hit_level(row.get("hit_target", ""))
    if result == "WIN" and level >= 3:
        return rr if rr > 0 else 3.0
    if result == "WIN" and level >= 2:
        return rr if rr > 0 else 2.0
    if result == "WIN":
        return 1.0
    return 0.0


def pnl_percent(row: pd.Series) -> float | None:
    value = _num(row.get("pnl_percent"))
    if value is not None:
        return value
    value = _num(row.get("max_profit_pct"))
    if str(row.get("result", "")).upper() == "WIN" and value is not None:
        return abs(value)
    value = _num(row.get("max_drawdown_pct"))
    if str(row.get("result", "")).upper() == "LOSS" and value is not None:
        return -abs(value)
    return None


def normalize_for_v3(df: pd.DataFrame) -> pd.DataFrame:
    data = df.copy()
    defaults = {
        "timestamp": "",
        "symbol": "",
        "side": "",
        "tier": "",
        "watchlist_tier": "",
        "session": "",
        "market_session": "",
        "result": "OPEN",
        "hit_target": "",
        "signal_status": "sent",
        "rr": "",
        "risk_reward": "",
        "real_rr": "",
        "net_r_estimate": "",
        "pnl_percent": "",
        "max_profit_pct": "",
        "max_drawdown_pct": "",
        "score": "",
        "raw_score": "",
        "setup_strength": "",
    }
    for column, default in defaults.items():
        if column not in data.columns:
            data[column] = default
    normalized = pd.DataFrame(index=data.index)
    normalized["timestamp"] = pd.to_datetime(data["timestamp"], utc=True, errors="coerce")
    normalized["hour"] = normalized["timestamp"].dt.hour
    normalized["symbol"] = data["symbol"].fillna("").astype(str).str.upper()
    normalized["side"] = data["side"].fillna("").astype(str).str.upper()
    tier = data["tier"].where(data["tier"].fillna("").astype(str).str.strip().ne(""), data["watchlist_tier"])
    normalized["tier"] = tier.fillna("B").replace("", "B").astype(str).str.upper()
    session = data["session"].where(data["session"].fillna("").astype(str).str.strip().ne(""), data["market_session"])
    normalized["session"] = session.fillna("OffHours").replace("", "OffHours").map(canonical_session)
    normalized["result"] = data["result"].fillna("OPEN").replace("", "OPEN").astype(str).str.upper()
    normalized["hit_target"] = data["hit_target"].fillna("").astype(str).str.upper()
    normalized["signal_status"] = data["signal_status"].fillna("sent").replace("", "sent").astype(str).str.lower()
    normalized["estimated_r"] = data.apply(estimated_r, axis=1)
    normalized["pnl_percent"] = data.apply(pnl_percent, axis=1)
    normalized["score"] = _first_numeric(data, ["score", "raw_score", "setup_strength"])
    normalized["max_profit_pct"] = pd.to_numeric(data["max_profit_pct"], errors="coerce")
    normalized["max_drawdown_pct"] = pd.to_numeric(data["max_drawdown_pct"], errors="coerce")
    return normalized


def _summary(df: pd.DataFrame) -> dict[str, Any]:
    closed = _closed_trades(_sent_signals(df))
    wins = closed[closed["result"] == "WIN"]
    losses = closed[closed["result"] == "LOSS"]
    pnl = pd.to_numeric(closed.get("pnl_percent", pd.Series(dtype=float)), errors="coerce")
    profit = pnl[pnl > 0]
    loss = pnl[pnl < 0]
    return {
        "Total trades": int(len(closed)),
        "Wins": int(len(wins)),
        "Losses": int(len(losses)),
        "Win rate": round(len(wins) / len(closed) * 100, 1) if len(closed) else 0.0,
        "Net R estimate": round(float(pd.to_numeric(closed.get("estimated_r", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()), 2),
        "Avg profit %": round(float(profit.mean()), 2) if not profit.empty else 0.0,
        "Avg loss %": round(float(loss.mean()), 2) if not loss.empty else 0.0,
        "TP1 hits": int(closed["hit_target"].map(_hit_level).ge(1).sum()) if "hit_target" in closed else 0,
        "SL hits": int(len(losses)),
    }


def group_performance(df: pd.DataFrame, column: str, label: str) -> pd.DataFrame:
    columns = [label, "Trades", "Wins", "Losses", "Win Rate", "Net R"]
    closed = _closed_trades(_sent_signals(df))
    if closed.empty or column not in closed.columns:
        return pd.DataFrame(columns=columns)
    rows = []
    for key, group in closed.groupby(closed[column].fillna("-").replace("", "-").astype(str), dropna=False):
        wins = int((group["result"] == "WIN").sum())
        losses = int((group["result"] == "LOSS").sum())
        rows.append(
            {
                label: key,
                "Trades": int(len(group)),
                "Wins": wins,
                "Losses": losses,
                "Win Rate": round(wins / len(group) * 100, 1) if len(group) else 0.0,
                "Net R": round(float(group["estimated_r"].fillna(0).sum()), 2),
            }
        )
    return pd.DataFrame(rows, columns=columns).sort_values(["Net R", "Win Rate", "Trades"], ascending=[False, False, False])


def score_range_label(score: Any) -> str | None:
    value = _num(score)
    if value is None:
        return None
    if 75 <= value <= 79:
        return "75-79"
    if 80 <= value <= 84:
        return "80-84"
    if 85 <= value <= 89:
        return "85-89"
    if 90 <= value <= 94:
        return "90-94"
    if 95 <= value <= 99:
        return "95-99"
    if value >= 100:
        return "100+"
    return None


def score_bucket_performance(df: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "Score Range",
        "Trades",
        "Wins",
        "Losses",
        "Win Rate",
        "Net R",
        "TP1 Hits",
        "TP2 Hits",
        "Avg Max Profit %",
        "Avg Drawdown %",
    ]
    closed = _closed_trades(_sent_signals(df))
    if closed.empty or "score" not in closed.columns:
        return pd.DataFrame(columns=columns)
    data = closed.copy()
    data["score_range"] = data["score"].map(score_range_label)
    data = data[data["score_range"].notna()].copy()
    if data.empty:
        return pd.DataFrame(columns=columns)

    order = ["75-79", "80-84", "85-89", "90-94", "95-99", "100+"]
    rows = []
    for label in order:
        group = data[data["score_range"] == label]
        if group.empty:
            continue
        wins = int((group["result"] == "WIN").sum())
        losses = int((group["result"] == "LOSS").sum())
        hit_levels = group["hit_target"].map(_hit_level)
        rows.append(
            {
                "Score Range": label,
                "Trades": int(len(group)),
                "Wins": wins,
                "Losses": losses,
                "Win Rate": round(wins / len(group) * 100, 1) if len(group) else 0.0,
                "Net R": round(float(group["estimated_r"].fillna(0).sum()), 2),
                "TP1 Hits": int(hit_levels.ge(1).sum()),
                "TP2 Hits": int(hit_levels.ge(2).sum()),
                "Avg Max Profit %": round(float(group["max_profit_pct"].mean()), 2) if group["max_profit_pct"].notna().any() else 0.0,
                "Avg Drawdown %": round(float(group["max_drawdown_pct"].mean()), 2) if group["max_drawdown_pct"].notna().any() else 0.0,
            }
        )
    return pd.DataFrame(rows, columns=columns)


def weak_symbols(df: pd.DataFrame, min_trades: int = MIN_WEAK_SYMBOL_TRADES, max_win_rate: float = WEAK_SYMBOL_WIN_RATE) -> set[str]:
    table = group_performance(df, "symbol", "Symbol")
    if table.empty:
        return set()
    weak = table[(table["Trades"] >= min_trades) & (table["Win Rate"] < max_win_rate)]
    return set(weak["Symbol"].astype(str))


def shadow_filter_backtest(df: pd.DataFrame) -> pd.DataFrame:
    data = normalize_for_v3(df)
    current = _summary(data)
    weak = weak_symbols(data)
    scenarios: list[tuple[str, Callable[[pd.DataFrame], pd.Series]]] = [
        ("Current", lambda frame: pd.Series([True] * len(frame), index=frame.index)),
        ("No Tier C", lambda frame: frame["tier"].ne("C")),
        ("No NewYork session", lambda frame: frame["session"].ne("NewYork")),
        ("No London+NewYork session", lambda frame: frame["session"].ne("London+NewYork")),
        ("No Tier C + No NewYork", lambda frame: frame["tier"].ne("C") & frame["session"].ne("NewYork")),
        ("Exclude weak symbols <40% WR / >=5 trades", lambda frame: ~frame["symbol"].isin(weak)),
        ("A/B tiers only", lambda frame: frame["tier"].isin(["A", "B"])),
        ("Shorts only", lambda frame: frame["side"].eq("SHORT")),
        ("Longs only", lambda frame: frame["side"].eq("LONG")),
    ]
    rows = []
    for name, mask_func in scenarios:
        mask = mask_func(data)
        summary = _summary(data[mask].copy())
        summary["Scenario"] = name
        summary["Diff vs current Net R"] = round(summary["Net R estimate"] - current["Net R estimate"], 2)
        summary["Diff vs current win rate"] = round(summary["Win rate"] - current["Win rate"], 1)
        rows.append(summary)
    columns = [
        "Scenario",
        "Total trades",
        "Wins",
        "Losses",
        "Win rate",
        "Net R estimate",
        "Avg profit %",
        "Avg loss %",
        "TP1 hits",
        "SL hits",
        "Diff vs current Net R",
        "Diff vs current win rate",
    ]
    return pd.DataFrame(rows, columns=columns)


def recommendations(df: pd.DataFrame) -> pd.DataFrame:
    data = normalize_for_v3(df)
    symbol_table = group_performance(data, "symbol", "Symbol")
    columns = ["Symbol", "Trades", "Win Rate", "Net R", "Recommendation", "Reason"]
    if symbol_table.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    for _, row in symbol_table.iterrows():
        trades = int(row["Trades"])
        win_rate = float(row["Win Rate"])
        net_r = float(row["Net R"])
        if trades >= MIN_WEAK_SYMBOL_TRADES and win_rate < WEAK_SYMBOL_WIN_RATE:
            recommendation = "FLAG FOR REMOVAL"
            reason = ">=5 closed trades and win rate below 40%"
        elif trades < MIN_WEAK_SYMBOL_TRADES:
            recommendation = "WATCH"
            reason = "sample size below 5 closed trades"
        elif net_r < 0:
            recommendation = "WATCH"
            reason = "negative net R despite acceptable win rate"
        else:
            recommendation = "KEEP"
            reason = "performance above report-only warning threshold"
        rows.append(
            {
                "Symbol": row["Symbol"],
                "Trades": trades,
                "Win Rate": win_rate,
                "Net R": net_r,
                "Recommendation": recommendation,
                "Reason": reason,
            }
        )
    return pd.DataFrame(rows, columns=columns).sort_values(["Recommendation", "Net R"], ascending=[True, True])


def format_table(table: pd.DataFrame, limit: int = 8) -> str:
    if table.empty:
        return "N/A"
    view = table.head(limit).copy()
    return view.to_string(index=False)


def build_performance_v3(df: pd.DataFrame) -> dict[str, Any]:
    data = normalize_for_v3(df)
    return {
        "symbol_performance_v3": group_performance(data, "symbol", "Symbol"),
        "session_performance_v3": group_performance(data, "session", "Session"),
        "tier_performance_v3": group_performance(data, "tier", "Tier"),
        "direction_performance_v3": group_performance(data, "side", "Direction"),
        "hour_performance_v3": group_performance(data.dropna(subset=["hour"]).assign(hour=lambda x: x["hour"].astype(int).astype(str)), "hour", "Hour"),
        "score_performance_v3": score_bucket_performance(data),
        "shadow_filter_backtest": shadow_filter_backtest(data),
        "recommended_actions": recommendations(data),
    }
