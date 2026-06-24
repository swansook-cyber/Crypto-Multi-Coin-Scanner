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
PRODUCTION_UNIVERSE_MIN_TRADES = 5
REPORT_ONLY_STATUSES = {"tier_c_report_only", "weak_symbol_report_only", "session_risk_report_only"}


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
        "holding_minutes": "",
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
    normalized["holding_minutes"] = pd.to_numeric(data["holding_minutes"], errors="coerce")
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


def _closed_pool_summary(df: pd.DataFrame, label: str) -> dict[str, Any]:
    closed = _closed_trades(df)
    wins = closed[closed["result"] == "WIN"]
    losses = closed[closed["result"] == "LOSS"]
    pnl = pd.to_numeric(closed.get("pnl_percent", pd.Series(dtype=float)), errors="coerce")
    profit = pnl[pnl > 0]
    loss = pnl[pnl < 0]
    hit_levels = closed["hit_target"].map(_hit_level) if "hit_target" in closed else pd.Series(dtype=int)
    return {
        "Pool": label,
        "Closed Trades": int(len(closed)),
        "Wins": int(len(wins)),
        "Losses": int(len(losses)),
        "Win Rate": round(len(wins) / len(closed) * 100.0, 1) if len(closed) else 0.0,
        "Net R": round(float(pd.to_numeric(closed.get("estimated_r", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()), 2),
        "TP1 Hits": int(hit_levels.ge(1).sum()) if not hit_levels.empty else 0,
        "TP2 Hits": int(hit_levels.ge(2).sum()) if not hit_levels.empty else 0,
        "Avg Profit %": round(float(profit.mean()), 2) if not profit.empty else 0.0,
        "Avg Loss %": round(float(loss.mean()), 2) if not loss.empty else 0.0,
        "Avg Drawdown %": round(float(closed["max_drawdown_pct"].mean()), 2) if "max_drawdown_pct" in closed and closed["max_drawdown_pct"].notna().any() else 0.0,
        "Avg Max Profit %": round(float(closed["max_profit_pct"].mean()), 2) if "max_profit_pct" in closed and closed["max_profit_pct"].notna().any() else 0.0,
        "Avg Time To TP": round(float(wins["holding_minutes"].mean()), 1) if not wins.empty and wins["holding_minutes"].notna().any() else 0.0,
        "Avg Time To SL": round(float(losses["holding_minutes"].mean()), 1) if not losses.empty and losses["holding_minutes"].notna().any() else 0.0,
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


def _closed_scored_trades(df: pd.DataFrame) -> pd.DataFrame:
    closed = _closed_trades(_sent_signals(df))
    if closed.empty or "score" not in closed.columns:
        return pd.DataFrame(columns=list(closed.columns) + ["score_bucket"])
    data = closed.copy()
    data["score_bucket"] = data["score"].map(score_range_label)
    return data[data["score_bucket"].notna()].copy()


def score_cross_audit(df: pd.DataFrame, column: str, label: str, min_trades: int = 1) -> pd.DataFrame:
    columns = ["Score Bucket", label, "Trades", "Wins", "Losses", "Win Rate", "Net R"]
    data = _closed_scored_trades(df)
    if data.empty or column not in data.columns:
        return pd.DataFrame(columns=columns)
    rows = []
    order = ["75-79", "80-84", "85-89", "90-94", "95-99", "100+"]
    data["score_bucket"] = pd.Categorical(data["score_bucket"], categories=order, ordered=True)
    grouped = data.groupby(["score_bucket", data[column].fillna("-").replace("", "-").astype(str)], observed=True, dropna=False)
    for (score_bucket, key), group in grouped:
        if len(group) < min_trades:
            continue
        wins = int((group["result"] == "WIN").sum())
        losses = int((group["result"] == "LOSS").sum())
        rows.append(
            {
                "Score Bucket": str(score_bucket),
                label: key,
                "Trades": int(len(group)),
                "Wins": wins,
                "Losses": losses,
                "Win Rate": round(wins / len(group) * 100, 1) if len(group) else 0.0,
                "Net R": round(float(group["estimated_r"].fillna(0).sum()), 2),
            }
        )
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows, columns=columns).sort_values(["Score Bucket", "Net R", "Trades"], ascending=[True, False, False])


def score_efficiency_audit(df: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "Score Bucket",
        "Trades",
        "Win Rate",
        "Net R",
        "TP1 Hits",
        "TP2 Hits",
        "Avg Max Profit %",
        "Avg Drawdown %",
        "Avg Time To TP",
        "Avg Time To SL",
    ]
    data = _closed_scored_trades(df)
    if data.empty:
        return pd.DataFrame(columns=columns)
    order = ["75-79", "80-84", "85-89", "90-94", "95-99", "100+"]
    rows = []
    holding = pd.to_numeric(data.get("holding_minutes", pd.Series(dtype=float)), errors="coerce")
    data = data.assign(holding_minutes=holding)
    for bucket in order:
        group = data[data["score_bucket"] == bucket]
        if group.empty:
            continue
        wins = group[group["result"] == "WIN"]
        losses = group[group["result"] == "LOSS"]
        hit_levels = group["hit_target"].map(_hit_level)
        rows.append(
            {
                "Score Bucket": bucket,
                "Trades": int(len(group)),
                "Win Rate": round(len(wins) / len(group) * 100, 1) if len(group) else 0.0,
                "Net R": round(float(group["estimated_r"].fillna(0).sum()), 2),
                "TP1 Hits": int(hit_levels.ge(1).sum()),
                "TP2 Hits": int(hit_levels.ge(2).sum()),
                "Avg Max Profit %": round(float(group["max_profit_pct"].mean()), 2) if group["max_profit_pct"].notna().any() else 0.0,
                "Avg Drawdown %": round(float(group["max_drawdown_pct"].mean()), 2) if group["max_drawdown_pct"].notna().any() else 0.0,
                "Avg Time To TP": round(float(wins["holding_minutes"].mean()), 1) if not wins.empty and wins["holding_minutes"].notna().any() else 0.0,
                "Avg Time To SL": round(float(losses["holding_minutes"].mean()), 1) if not losses.empty and losses["holding_minutes"].notna().any() else 0.0,
            }
        )
    return pd.DataFrame(rows, columns=columns)


def score_calibration_report(df: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "Score Bucket",
        "Closed Trades",
        "Wins",
        "Losses",
        "Win Rate",
        "Net R",
        "TP1 Rate",
        "TP2 Rate",
        "Avg Profit %",
        "Avg Loss %",
        "Avg Max Profit %",
        "Avg Drawdown %",
        "Win Rate Rank",
        "Net R Rank",
        "Score Rank",
        "Calibration",
        "Diagnostics",
    ]
    data = _closed_scored_trades(df)
    if data.empty:
        return pd.DataFrame(columns=columns)

    order = ["75-79", "80-84", "85-89", "90-94", "95-99", "100+"]
    score_rank = {bucket: len(order) - index for index, bucket in enumerate(order)}
    rows = []
    pnl = pd.to_numeric(data.get("pnl_percent", pd.Series(dtype=float)), errors="coerce")
    data = data.assign(pnl_percent=pnl)
    for bucket in order:
        group = data[data["score_bucket"] == bucket]
        if group.empty:
            continue
        wins = group[group["result"] == "WIN"]
        losses = group[group["result"] == "LOSS"]
        hit_levels = group["hit_target"].map(_hit_level)
        profits = group["pnl_percent"][group["pnl_percent"] > 0]
        loss_values = group["pnl_percent"][group["pnl_percent"] < 0]
        rows.append(
            {
                "Score Bucket": bucket,
                "Closed Trades": int(len(group)),
                "Wins": int(len(wins)),
                "Losses": int(len(losses)),
                "Win Rate": round(len(wins) / len(group) * 100.0, 1) if len(group) else 0.0,
                "Net R": round(float(group["estimated_r"].fillna(0).sum()), 2),
                "TP1 Rate": round(float(hit_levels.ge(1).sum()) / len(group) * 100.0, 1) if len(group) else 0.0,
                "TP2 Rate": round(float(hit_levels.ge(2).sum()) / len(group) * 100.0, 1) if len(group) else 0.0,
                "Avg Profit %": round(float(profits.mean()), 2) if not profits.empty else 0.0,
                "Avg Loss %": round(float(loss_values.mean()), 2) if not loss_values.empty else 0.0,
                "Avg Max Profit %": round(float(group["max_profit_pct"].mean()), 2) if group["max_profit_pct"].notna().any() else 0.0,
                "Avg Drawdown %": round(float(group["max_drawdown_pct"].mean()), 2) if group["max_drawdown_pct"].notna().any() else 0.0,
                "Score Rank": score_rank[bucket],
            }
        )
    if not rows:
        return pd.DataFrame(columns=columns)

    table = pd.DataFrame(rows)
    table["Win Rate Rank"] = table["Win Rate"].rank(method="min", ascending=False).astype(int)
    table["Net R Rank"] = table["Net R"].rank(method="min", ascending=False).astype(int)
    calibrations = []
    diagnostics_values = []
    for _, row in table.iterrows():
        bucket = str(row["Score Bucket"])
        actual_rank = (int(row["Win Rate Rank"]) + int(row["Net R Rank"])) / 2.0
        expected_rank = int(row["Score Rank"])
        lower_buckets = table[table["Score Rank"] > expected_rank]
        diagnostics = []
        if actual_rank - expected_rank >= 1.0:
            calibration = "OVERVALUED"
            diagnostics.append("OVERCONFIDENT BUCKET")
        elif expected_rank - actual_rank >= 1.0:
            calibration = "UNDERVALUED"
            diagnostics.append("UNDERVALUED BUCKET")
        else:
            calibration = "FAIR"
        if not lower_buckets.empty:
            if float(lower_buckets["Win Rate"].max()) > float(row["Win Rate"]) + 5.0:
                diagnostics.append("HIGH SCORE UNDERPERFORMING" if bucket in {"95-99", "100+"} else "SCORE INVERSION")
            if float(lower_buckets["Net R"].max()) > float(row["Net R"]) + 2.0:
                diagnostics.append("SCORE INVERSION")
        if not diagnostics:
            diagnostics.append("OK")
        calibrations.append(calibration)
        diagnostics_values.append(", ".join(dict.fromkeys(diagnostics)))
    table["Calibration"] = calibrations
    table["Diagnostics"] = diagnostics_values
    table = table[columns]
    return table.sort_values("Score Rank", ascending=False)


def score_calibration_recommendations(table: pd.DataFrame) -> list[str]:
    if table.empty:
        return ["N/A"]
    recommendations = []
    overvalued = table[table["Calibration"].eq("OVERVALUED")]
    if not overvalued[overvalued["Score Bucket"].isin(["90-94", "95-99", "100+"])].empty:
        recommendations.append("Score inflation detected above 90")
    if not overvalued[overvalued["Score Bucket"].isin(["95-99", "100+"])].empty:
        recommendations.append("Score inflation detected above 95")
    if table["Diagnostics"].astype(str).str.contains("SCORE INVERSION", regex=False).any():
        recommendations.append("Score inversion detected")
    if table["Diagnostics"].astype(str).str.contains("HIGH SCORE UNDERPERFORMING", regex=False).any():
        recommendations.append("High-score signals underperform lower-score signals")
    if not recommendations:
        recommendations.append("Score system appears calibrated")
    return recommendations


def _strategy_summary(df: pd.DataFrame, scenario: str, current_win_rate: float, current_net_r: float) -> dict[str, Any]:
    closed = _closed_trades(df)
    wins = closed[closed["result"] == "WIN"]
    losses = closed[closed["result"] == "LOSS"]
    hit_levels = closed["hit_target"].map(_hit_level) if "hit_target" in closed else pd.Series(dtype=int)
    pnl = pd.to_numeric(closed.get("pnl_percent", pd.Series(dtype=float)), errors="coerce")
    profits = pnl[pnl > 0]
    loss_values = pnl[pnl < 0]
    win_rate = round(len(wins) / len(closed) * 100.0, 1) if len(closed) else 0.0
    net_r = round(float(pd.to_numeric(closed.get("estimated_r", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()), 2)
    return {
        "Rank": "",
        "Scenario": scenario,
        "Closed Trades": int(len(closed)),
        "Wins": int(len(wins)),
        "Losses": int(len(losses)),
        "Win Rate": win_rate,
        "Net R": net_r,
        "TP1 Rate": round(float(hit_levels.ge(1).sum()) / len(closed) * 100.0, 1) if len(closed) else 0.0,
        "TP2 Rate": round(float(hit_levels.ge(2).sum()) / len(closed) * 100.0, 1) if len(closed) else 0.0,
        "Avg Profit %": round(float(profits.mean()), 2) if not profits.empty else 0.0,
        "Avg Loss %": round(float(loss_values.mean()), 2) if not loss_values.empty else 0.0,
        "Avg Drawdown %": round(float(closed["max_drawdown_pct"].mean()), 2) if "max_drawdown_pct" in closed and closed["max_drawdown_pct"].notna().any() else 0.0,
        "Avg Max Profit %": round(float(closed["max_profit_pct"].mean()), 2) if "max_profit_pct" in closed and closed["max_profit_pct"].notna().any() else 0.0,
        "Diff vs Current Win Rate": round(win_rate - current_win_rate, 1),
        "Diff vs Current Net R": round(net_r - current_net_r, 2),
    }


def strategy_filter_simulator(df: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "Rank",
        "Scenario",
        "Closed Trades",
        "Wins",
        "Losses",
        "Win Rate",
        "Net R",
        "TP1 Rate",
        "TP2 Rate",
        "Avg Profit %",
        "Avg Loss %",
        "Avg Drawdown %",
        "Avg Max Profit %",
        "Diff vs Current Win Rate",
        "Diff vs Current Net R",
    ]
    data = normalize_for_v3(df)
    base = _closed_trades(_sent_signals(data))
    if base.empty:
        return pd.DataFrame(columns=columns)

    ranking = production_universe_ranking(data)
    if ranking.empty:
        production_symbols: set[str] = set()
    else:
        production_symbols = set(ranking.loc[~ranking["Classification"].eq("Report Only"), "Symbol"].astype(str))

    current = _strategy_summary(base, "Current", 0.0, 0.0)
    current_win_rate = float(current["Win Rate"])
    current_net_r = float(current["Net R"])
    score = pd.to_numeric(base["score"], errors="coerce")
    tier = base["tier"].fillna("").astype(str).str.upper()
    session = base["session"].fillna("").astype(str)
    symbol = base["symbol"].fillna("").astype(str)
    production_mask = symbol.isin(production_symbols)

    scenarios: list[tuple[str, pd.Series]] = [
        ("Current", pd.Series([True] * len(base), index=base.index)),
        ("Score >= 75", score.ge(75)),
        ("Score >= 80", score.ge(80)),
        ("Score >= 85", score.ge(85)),
        ("Score 75-89", score.ge(75) & score.le(89)),
        ("Score 80-89", score.ge(80) & score.le(89)),
        ("Score 75-94", score.ge(75) & score.le(94)),
        ("Tier A/B only", tier.isin(["A", "B"])),
        ("Tier A/B + Score >=80", tier.isin(["A", "B"]) & score.ge(80)),
        ("Tier A/B + Score 75-89", tier.isin(["A", "B"]) & score.ge(75) & score.le(89)),
        ("Production Universe only", production_mask),
        ("Production Universe + Score >=80", production_mask & score.ge(80)),
        ("Production Universe + Score 75-89", production_mask & score.ge(75) & score.le(89)),
        ("Production Universe + No NewYork", production_mask & session.ne("NewYork")),
        ("Production Universe + No London+NewYork", production_mask & session.ne("London+NewYork")),
        ("Production Universe + Score 75-89 + No NewYork", production_mask & score.ge(75) & score.le(89) & session.ne("NewYork")),
    ]
    rows = [_strategy_summary(base[mask.fillna(False)].copy(), name, current_win_rate, current_net_r) for name, mask in scenarios]
    table = pd.DataFrame(rows, columns=columns)
    candidates = table[table["Scenario"].ne("Current") & table["Closed Trades"].gt(0)].copy()
    if not candidates.empty:
        candidates = candidates.sort_values(["Net R", "Win Rate", "Closed Trades"], ascending=[False, False, False])
        for rank, index in enumerate(candidates.index, start=1):
            table.loc[index, "Rank"] = str(rank)
    return table


def top_strategy_candidates(table: pd.DataFrame, limit: int = 5) -> pd.DataFrame:
    columns = ["Rank", "Scenario", "Trades", "Win Rate", "Net R"]
    if table.empty or "Rank" not in table.columns:
        return pd.DataFrame(columns=columns)
    ranked = table[table["Rank"].astype(str).str.strip().ne("")].copy()
    if ranked.empty:
        return pd.DataFrame(columns=columns)
    ranked["Rank"] = pd.to_numeric(ranked["Rank"], errors="coerce")
    ranked = ranked.sort_values("Rank").head(limit)
    return pd.DataFrame(
        {
            "Rank": ranked["Rank"].astype(int),
            "Scenario": ranked["Scenario"],
            "Trades": ranked["Closed Trades"].astype(int),
            "Win Rate": ranked["Win Rate"],
            "Net R": ranked["Net R"],
        },
        columns=columns,
    )


def strategy_filter_recommendations(table: pd.DataFrame) -> list[str]:
    if table.empty:
        return ["N/A"]
    candidates = table[table["Scenario"].ne("Current") & table["Closed Trades"].gt(0)].copy()
    if candidates.empty:
        return ["Not enough sample warning"]
    recommendations = []
    best_net_r = candidates.sort_values(["Net R", "Win Rate", "Closed Trades"], ascending=[False, False, False]).iloc[0]
    best_wr = candidates.sort_values(["Win Rate", "Net R", "Closed Trades"], ascending=[False, False, False]).iloc[0]
    balanced = candidates[candidates["Closed Trades"] >= max(5, int(candidates["Closed Trades"].max() * 0.25))].copy()
    if balanced.empty:
        balanced = candidates
    best_balanced = balanced.sort_values(["Net R", "Win Rate", "Closed Trades"], ascending=[False, False, False]).iloc[0]
    recommendations.append(f"Best Net R candidate: {best_net_r['Scenario']} ({best_net_r['Net R']}R)")
    recommendations.append(f"Best Win Rate candidate: {best_wr['Scenario']} ({best_wr['Win Rate']}%)")
    recommendations.append(f"Best balanced candidate: {best_balanced['Scenario']} ({int(best_balanced['Closed Trades'])} trades)")
    if int(best_net_r["Closed Trades"]) < 5 or int(best_wr["Closed Trades"]) < 5:
        recommendations.append("Too few trades warning")
    if int(candidates["Closed Trades"].max()) < 10:
        recommendations.append("Not enough sample warning")
    return recommendations


def _classification(trades: int, win_rate: float, net_r: float) -> str:
    if trades >= 10 and win_rate >= 60.0 and net_r > 0:
        return "Tier S"
    if trades >= 5 and win_rate >= 55.0 and net_r >= 0:
        return "Tier A"
    if trades >= 5 and 40.0 <= win_rate < 55.0:
        return "Watch"
    if trades >= 5 and win_rate < 40.0:
        return "Report Only"
    return "Watch"


def _confidence_score(trades: int, win_rate: float, net_r: float, tp1_rate: float, avg_drawdown: float) -> float:
    net_r_per_trade = net_r / trades if trades else 0.0
    net_r_component = max(0.0, min(100.0, 50.0 + net_r_per_trade * 20.0))
    trade_count_component = min(100.0, trades / 20.0 * 100.0)
    drawdown_stability = max(0.0, min(100.0, 100.0 - abs(avg_drawdown) * 10.0))
    score = (
        win_rate * 0.35
        + net_r_component * 0.25
        + trade_count_component * 0.15
        + tp1_rate * 0.15
        + drawdown_stability * 0.10
    )
    return round(score, 1)


def production_universe_ranking(df: pd.DataFrame, min_trades: int = PRODUCTION_UNIVERSE_MIN_TRADES) -> pd.DataFrame:
    columns = [
        "Symbol",
        "Closed Trades",
        "Wins",
        "Losses",
        "Win Rate",
        "Net R",
        "Avg Max Profit %",
        "Avg Drawdown %",
        "TP1 Rate",
        "TP2 Rate",
        "Avg Time To TP",
        "Avg Time To SL",
        "Confidence Score",
        "Classification",
    ]
    closed = _closed_trades(df)
    if closed.empty or "symbol" not in closed.columns:
        return pd.DataFrame(columns=columns)

    rows = []
    for symbol, group in closed.groupby(closed["symbol"].fillna("").replace("", "-").astype(str), dropna=False):
        trades = int(len(group))
        if trades < min_trades or not symbol or symbol == "-":
            continue
        wins = group[group["result"] == "WIN"].copy()
        losses = group[group["result"] == "LOSS"].copy()
        win_count = int(len(wins))
        loss_count = int(len(losses))
        win_rate = round(win_count / trades * 100.0, 1) if trades else 0.0
        net_r = round(float(pd.to_numeric(group["estimated_r"], errors="coerce").fillna(0).sum()), 2)
        hit_levels = group["hit_target"].map(_hit_level)
        tp1_rate = round(float(hit_levels.ge(1).sum()) / trades * 100.0, 1) if trades else 0.0
        tp2_rate = round(float(hit_levels.ge(2).sum()) / trades * 100.0, 1) if trades else 0.0
        avg_profit = round(float(group["max_profit_pct"].mean()), 2) if group["max_profit_pct"].notna().any() else 0.0
        avg_drawdown = round(float(group["max_drawdown_pct"].mean()), 2) if group["max_drawdown_pct"].notna().any() else 0.0
        avg_time_to_tp = round(float(wins["holding_minutes"].mean()), 1) if not wins.empty and wins["holding_minutes"].notna().any() else 0.0
        avg_time_to_sl = round(float(losses["holding_minutes"].mean()), 1) if not losses.empty and losses["holding_minutes"].notna().any() else 0.0
        classification = _classification(trades, win_rate, net_r)
        rows.append(
            {
                "Symbol": symbol,
                "Closed Trades": trades,
                "Wins": win_count,
                "Losses": loss_count,
                "Win Rate": win_rate,
                "Net R": net_r,
                "Avg Max Profit %": avg_profit,
                "Avg Drawdown %": avg_drawdown,
                "TP1 Rate": tp1_rate,
                "TP2 Rate": tp2_rate,
                "Avg Time To TP": avg_time_to_tp,
                "Avg Time To SL": avg_time_to_sl,
                "Confidence Score": _confidence_score(trades, win_rate, net_r, tp1_rate, avg_drawdown),
                "Classification": classification,
            }
        )
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows, columns=columns).sort_values(
        ["Confidence Score", "Net R", "Win Rate", "Closed Trades"],
        ascending=[False, False, False, False],
    )


def post_filter_live_performance(df: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "Pool",
        "Closed Trades",
        "Wins",
        "Losses",
        "Win Rate",
        "Net R",
        "TP1 Hits",
        "TP2 Hits",
        "Avg Profit %",
        "Avg Loss %",
        "Avg Drawdown %",
        "Avg Max Profit %",
        "Avg Time To TP",
        "Avg Time To SL",
    ]
    data = normalize_for_v3(df)
    if data.empty:
        return pd.DataFrame(columns=columns)
    status = data["signal_status"].fillna("sent").astype(str).str.lower()
    historical = _closed_pool_summary(data, "Historical")
    post_filter = _closed_pool_summary(data[~status.isin(REPORT_ONLY_STATUSES)].copy(), "Post-Filter Live Pool")
    improvement = {
        "Pool": "Improvement",
        "Closed Trades": int(post_filter["Closed Trades"] - historical["Closed Trades"]),
        "Wins": int(post_filter["Wins"] - historical["Wins"]),
        "Losses": int(post_filter["Losses"] - historical["Losses"]),
        "Win Rate": round(float(post_filter["Win Rate"]) - float(historical["Win Rate"]), 1),
        "Net R": round(float(post_filter["Net R"]) - float(historical["Net R"]), 2),
        "TP1 Hits": int(post_filter["TP1 Hits"] - historical["TP1 Hits"]),
        "TP2 Hits": int(post_filter["TP2 Hits"] - historical["TP2 Hits"]),
        "Avg Profit %": round(float(post_filter["Avg Profit %"]) - float(historical["Avg Profit %"]), 2),
        "Avg Loss %": round(float(post_filter["Avg Loss %"]) - float(historical["Avg Loss %"]), 2),
        "Avg Drawdown %": round(float(post_filter["Avg Drawdown %"]) - float(historical["Avg Drawdown %"]), 2),
        "Avg Max Profit %": round(float(post_filter["Avg Max Profit %"]) - float(historical["Avg Max Profit %"]), 2),
        "Avg Time To TP": round(float(post_filter["Avg Time To TP"]) - float(historical["Avg Time To TP"]), 1),
        "Avg Time To SL": round(float(post_filter["Avg Time To SL"]) - float(historical["Avg Time To SL"]), 1),
    }
    return pd.DataFrame([historical, post_filter, improvement], columns=columns)


def production_universe_performance(df: pd.DataFrame) -> pd.DataFrame:
    ranking = production_universe_ranking(df)
    columns = [
        "Pool",
        "Closed Trades",
        "Wins",
        "Losses",
        "Win Rate",
        "Net R",
        "TP1 Hits",
        "TP2 Hits",
        "Avg Profit %",
        "Avg Loss %",
        "Avg Drawdown %",
        "Avg Max Profit %",
        "Avg Time To TP",
        "Avg Time To SL",
    ]
    data = normalize_for_v3(df)
    if data.empty or ranking.empty:
        return pd.DataFrame(columns=columns)
    classified = {
        classification: set(ranking.loc[ranking["Classification"].eq(classification), "Symbol"].astype(str))
        for classification in ["Tier S", "Tier A", "Watch", "Report Only"]
    }
    pools = [
        ("Tier S symbols only", classified["Tier S"]),
        ("Tier S + Tier A symbols", classified["Tier S"] | classified["Tier A"]),
        ("Watch symbols", classified["Watch"]),
        ("Report-only symbols", classified["Report Only"]),
    ]
    rows = []
    for label, symbols in pools:
        rows.append(_closed_pool_summary(data[data["symbol"].isin(symbols)].copy(), label))
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
    calibration = score_calibration_report(data)
    simulator = strategy_filter_simulator(data)
    return {
        "symbol_performance_v3": group_performance(data, "symbol", "Symbol"),
        "session_performance_v3": group_performance(data, "session", "Session"),
        "tier_performance_v3": group_performance(data, "tier", "Tier"),
        "direction_performance_v3": group_performance(data, "side", "Direction"),
        "hour_performance_v3": group_performance(data.dropna(subset=["hour"]).assign(hour=lambda x: x["hour"].astype(int).astype(str)), "hour", "Hour"),
        "score_performance_v3": score_bucket_performance(data),
        "score_tier_audit": score_cross_audit(data, "tier", "Tier"),
        "score_session_audit": score_cross_audit(data, "session", "Session"),
        "score_direction_audit": score_cross_audit(data, "side", "Direction"),
        "score_symbol_audit": score_cross_audit(data, "symbol", "Symbol", min_trades=5),
        "score_efficiency_audit": score_efficiency_audit(data),
        "score_calibration_report": calibration,
        "score_calibration_recommendations": score_calibration_recommendations(calibration),
        "strategy_filter_simulator": simulator,
        "top_strategy_candidates": top_strategy_candidates(simulator),
        "strategy_filter_recommendations": strategy_filter_recommendations(simulator),
        "production_universe_ranking": production_universe_ranking(data),
        "post_filter_live_performance": post_filter_live_performance(data),
        "production_universe_performance": production_universe_performance(data),
        "shadow_filter_backtest": shadow_filter_backtest(data),
        "recommended_actions": recommendations(data),
    }
