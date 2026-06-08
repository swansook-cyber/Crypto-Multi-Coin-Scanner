# -*- coding: utf-8 -*-
"""Streamlit Dashboard V2 for Crypto Multi-Coin Scanner performance.

The dashboard is read-only: it reads local CSV logs and never sends Telegram,
places trades, calls exchange APIs, or mutates log files.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from core.analytics_reporting import load_csv_safely
from performance_report import build_report, estimate_r, normalize, sent_signals
from position_manager import latest_open_positions


BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
REPORTS_DIR = BASE_DIR / "reports"
DASHBOARD_HTML = REPORTS_DIR / "dashboard.html"

DATA_PATHS = {
    "signals": LOGS_DIR / "signals.csv",
    "daily_performance": LOGS_DIR / "daily_performance.csv",
    "symbol_performance": LOGS_DIR / "symbol_performance.csv",
    "source_performance": LOGS_DIR / "source_performance.csv",
    "position_management": LOGS_DIR / "position_management.csv",
    "external_signals": LOGS_DIR / "external_signals.csv",
}


def _ensure_columns(df: pd.DataFrame, defaults: dict[str, Any]) -> pd.DataFrame:
    data = df.copy()
    for column, default in defaults.items():
        if column not in data.columns:
            data[column] = default
    return data


def load_dashboard_data(paths: dict[str, Path] | None = None) -> dict[str, pd.DataFrame]:
    source_paths = paths or DATA_PATHS
    data = {name: load_csv_safely(path) for name, path in source_paths.items()}
    signals = normalize(data.get("signals", pd.DataFrame()))
    if "source" not in signals.columns:
        signals["source"] = "scanner"
    signals["source"] = signals["source"].fillna("scanner").replace("", "scanner").astype(str).str.lower()
    if "score_bucket" not in signals.columns:
        signals["score_bucket"] = "-"
    if "setup_strength" not in signals.columns:
        signals["setup_strength"] = signals["confidence"] if "confidence" in signals.columns else ""
    if "raw_score" not in signals.columns:
        signals["raw_score"] = signals["score"] if "score" in signals.columns else ""
    for column in ["raw_score", "setup_strength", "max_profit_pct", "max_drawdown_pct"]:
        if column in signals.columns:
            signals[column] = pd.to_numeric(signals[column], errors="coerce")
    data["signals"] = signals
    data["sent"] = sent_signals(signals)
    return data


def _closed(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "result" not in df.columns:
        return pd.DataFrame(columns=df.columns)
    return df[df["result"].isin(["WIN", "LOSS"])].copy()


def _hit_series(df: pd.DataFrame) -> pd.Series:
    if "hit_target" not in df.columns:
        return pd.Series([""] * len(df), index=df.index)
    return df["hit_target"].fillna("").astype(str).str.upper()


def _numeric_mean(df: pd.DataFrame, column: str) -> float | None:
    if df.empty or column not in df.columns:
        return None
    values = pd.to_numeric(df[column], errors="coerce").dropna()
    if values.empty:
        return None
    return float(values.mean())


def _numeric_max(df: pd.DataFrame, column: str) -> float | None:
    if df.empty or column not in df.columns:
        return None
    values = pd.to_numeric(df[column], errors="coerce").dropna()
    if values.empty:
        return None
    return float(values.max())


def _numeric_min(df: pd.DataFrame, column: str) -> float | None:
    if df.empty or column not in df.columns:
        return None
    values = pd.to_numeric(df[column], errors="coerce").dropna()
    if values.empty:
        return None
    return float(values.min())


def _holding_minutes(df: pd.DataFrame) -> pd.Series:
    if df.empty or "timestamp" not in df.columns or "closed_at" not in df.columns:
        return pd.Series(dtype=float)
    start = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    end = pd.to_datetime(df["closed_at"], utc=True, errors="coerce")
    return ((end - start).dt.total_seconds() / 60).clip(lower=0)


def _net_r(df: pd.DataFrame) -> float:
    closed = _closed(df)
    if closed.empty:
        return 0.0
    return float(closed.apply(estimate_r, axis=1).sum())


def _best_worst_by_win_rate(df: pd.DataFrame, column: str) -> tuple[str, str]:
    table = performance_table(df, column)
    if table.empty:
        return "-", "-"
    filtered = table[table["closed"] > 0].copy()
    if filtered.empty:
        return "-", "-"
    best = filtered.sort_values(["win_rate", "closed"], ascending=[False, False]).iloc[0]
    worst = filtered.sort_values(["win_rate", "closed"], ascending=[True, False]).iloc[0]
    return str(best[column]), str(worst[column])


def dashboard_kpis(df: pd.DataFrame) -> dict[str, Any]:
    sent = sent_signals(df)
    closed = _closed(sent)
    wins = int((closed["result"] == "WIN").sum()) if not closed.empty else 0
    losses = int((closed["result"] == "LOSS").sum()) if not closed.empty else 0
    hit_target = _hit_series(closed)
    best_symbol, worst_symbol = _best_worst_by_win_rate(sent, "symbol")
    best_tier, worst_tier = _best_worst_by_win_rate(sent, "watchlist_tier")
    best_session, worst_session = _best_worst_by_win_rate(sent, "market_session")
    winners = closed[closed["result"] == "WIN"] if not closed.empty else closed
    losers = closed[closed["result"] == "LOSS"] if not closed.empty else closed
    tp_minutes = _holding_minutes(winners)
    sl_minutes = _holding_minutes(losers)
    symbol_perf = performance_table(sent, "symbol")
    best_net_symbol = "-"
    worst_net_symbol = "-"
    if not symbol_perf.empty:
        best_net_symbol = str(symbol_perf.sort_values(["net_r", "closed"], ascending=[False, False]).iloc[0]["symbol"])
        worst_net_symbol = str(symbol_perf.sort_values(["net_r", "closed"], ascending=[True, False]).iloc[0]["symbol"])
    return {
        "Total sent signals": int(len(sent)),
        "Closed trades": int(len(closed)),
        "Wins": wins,
        "Losses": losses,
        "Win rate": wins / len(closed) * 100 if len(closed) else 0.0,
        "Long win rate": _side_win_rate(sent, "LONG"),
        "Short win rate": _side_win_rate(sent, "SHORT"),
        "TP1 hits": int(hit_target.isin(["TP1", "TP2", "TP3", "1", "2", "3"]).sum()),
        "TP2 hits": int(hit_target.isin(["TP2", "TP3", "2", "3"]).sum()),
        "SL hits": int((hit_target == "SL").sum()),
        "TP1 hit rate": hit_target.isin(["TP1", "TP2", "TP3", "1", "2", "3"]).mean() * 100 if len(closed) else 0.0,
        "TP2 hit rate": hit_target.isin(["TP2", "TP3", "2", "3"]).mean() * 100 if len(closed) else 0.0,
        "SL hit rate": (hit_target == "SL").mean() * 100 if len(closed) else 0.0,
        "Avg profit %": _numeric_mean(winners, "max_profit_pct"),
        "Avg loss %": _numeric_mean(losers, "max_drawdown_pct"),
        "Avg max profit %": _numeric_mean(closed, "max_profit_pct"),
        "Avg max drawdown %": _numeric_mean(closed, "max_drawdown_pct"),
        "Avg drawdown winners": _numeric_mean(winners, "max_drawdown_pct"),
        "Avg drawdown losers": _numeric_mean(losers, "max_drawdown_pct"),
        "Max drawdown ever": _numeric_min(closed, "max_drawdown_pct"),
        "Avg time to TP": float(tp_minutes.mean()) if not tp_minutes.dropna().empty else None,
        "Avg time to SL": float(sl_minutes.mean()) if not sl_minutes.dropna().empty else None,
        "Net R": _net_r(sent),
        "Best symbol": best_symbol,
        "Worst symbol": worst_symbol,
        "Best symbol by net R": best_net_symbol,
        "Worst symbol by net R": worst_net_symbol,
        "Best tier": best_tier,
        "Worst tier": worst_tier,
        "Best session": best_session,
        "Worst session": worst_session,
    }


def _side_win_rate(df: pd.DataFrame, side: str) -> float:
    closed = _closed(df)
    if closed.empty or "side" not in closed.columns:
        return 0.0
    selected = closed[closed["side"] == side]
    if selected.empty:
        return 0.0
    return float((selected["result"] == "WIN").mean() * 100)


def performance_table(df: pd.DataFrame, column: str) -> pd.DataFrame:
    if df.empty or column not in df.columns:
        return pd.DataFrame(columns=[column, "signals", "closed", "wins", "losses", "win_rate", "net_r"])
    rows = []
    for key, group in df.groupby(df[column].fillna("-").replace("", "-").astype(str)):
        closed = _closed(group)
        wins = int((closed["result"] == "WIN").sum()) if not closed.empty else 0
        losses = int((closed["result"] == "LOSS").sum()) if not closed.empty else 0
        rows.append(
            {
                column: key,
                "signals": int(len(group)),
                "closed": int(len(closed)),
                "wins": wins,
                "losses": losses,
                "win_rate": wins / len(closed) * 100 if len(closed) else 0.0,
                "net_r": float(closed.apply(estimate_r, axis=1).sum()) if len(closed) else 0.0,
                "avg_drawdown": _numeric_mean(closed, "max_drawdown_pct"),
            }
        )
    if not rows:
        return pd.DataFrame(columns=[column, "signals", "closed", "wins", "losses", "win_rate", "net_r"])
    return pd.DataFrame(rows).sort_values(["win_rate", "closed"], ascending=[False, False])


def bucket_label(value: Any, step: int = 10) -> str:
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric):
        return "Unknown"
    lower = int(numeric // step * step)
    upper = lower + step - 1
    return f"{lower}-{upper}"


def ensure_score_buckets(df: pd.DataFrame) -> pd.DataFrame:
    data = df.copy()
    data["score_range"] = data.get("raw_score", pd.Series(dtype=float)).apply(bucket_label)
    data["confidence_range"] = data.get("setup_strength", pd.Series(dtype=float)).apply(bucket_label)
    return data


def equity_curve(df: pd.DataFrame) -> pd.DataFrame:
    closed = _closed(df)
    if closed.empty:
        return pd.DataFrame(columns=["closed_at", "cumulative_r"])
    data = closed.copy()
    sort_column = "closed_at" if "closed_at" in data.columns else "timestamp"
    data[sort_column] = pd.to_datetime(data[sort_column], utc=True, errors="coerce")
    data = data.dropna(subset=[sort_column]).sort_values(sort_column)
    data["r"] = data.apply(estimate_r, axis=1)
    data["cumulative_r"] = data["r"].cumsum()
    return data[[sort_column, "cumulative_r"]].rename(columns={sort_column: "closed_at"})


def daily_net_r(df: pd.DataFrame) -> pd.DataFrame:
    closed = _closed(df)
    if closed.empty:
        return pd.DataFrame(columns=["date", "net_r"])
    data = closed.copy()
    data["date"] = pd.to_datetime(data["closed_at"] if "closed_at" in data.columns else data["timestamp"], utc=True, errors="coerce").dt.date
    data["r"] = data.apply(estimate_r, axis=1)
    return data.dropna(subset=["date"]).groupby("date", as_index=False)["r"].sum().rename(columns={"r": "net_r"})


def win_loss_by_day(df: pd.DataFrame) -> pd.DataFrame:
    closed = _closed(df)
    if closed.empty or "timestamp" not in closed.columns:
        return pd.DataFrame(columns=["date", "WIN", "LOSS"])
    data = closed.copy()
    data["date"] = pd.to_datetime(data["timestamp"], utc=True, errors="coerce").dt.date
    data = data.dropna(subset=["date"])
    if data.empty:
        return pd.DataFrame(columns=["date", "WIN", "LOSS"])
    pivot = data.pivot_table(index="date", columns="result", values="symbol", aggfunc="count", fill_value=0)
    for column in ["WIN", "LOSS"]:
        if column not in pivot.columns:
            pivot[column] = 0
    return pivot[["WIN", "LOSS"]].reset_index()


def tp_sl_distribution(df: pd.DataFrame) -> pd.DataFrame:
    closed = _closed(df)
    if closed.empty:
        return pd.DataFrame(columns=["target", "count"])
    targets = _hit_series(closed).replace({"": "UNKNOWN"})
    return targets.value_counts().rename_axis("target").reset_index(name="count")


def external_summary(external: pd.DataFrame) -> pd.DataFrame:
    if external.empty or "recommendation" not in external.columns:
        return pd.DataFrame(columns=["recommendation", "count"])
    return external["recommendation"].fillna("UNKNOWN").astype(str).str.upper().value_counts().rename_axis("recommendation").reset_index(name="count")


def latest_signals(df: pd.DataFrame, limit: int = 25) -> pd.DataFrame:
    columns = [
        "timestamp", "symbol", "side", "source", "watchlist_tier", "market_session",
        "score_bucket", "entry", "tp1", "tp2", "stop_loss", "result", "hit_target",
    ]
    available = [column for column in columns if column in df.columns]
    if df.empty:
        return pd.DataFrame(columns=available)
    return df.sort_values("timestamp", ascending=False).head(limit)[available]


def open_positions(df: pd.DataFrame) -> pd.DataFrame:
    open_df = latest_open_positions(df)
    columns = [
        "timestamp", "symbol", "side", "entry", "tp1", "tp2", "stop_loss",
        "position_recommendation", "current_r", "distance_to_tp1_pct", "distance_to_sl_pct",
        "position_confidence", "position_reason", "watchlist_tier", "market_session",
    ]
    available = [column for column in columns if column in open_df.columns]
    if open_df.empty:
        return pd.DataFrame(columns=available)
    return open_df.sort_values("timestamp", ascending=False)[available]


def recent_events(df: pd.DataFrame, limit: int = 25) -> pd.DataFrame:
    events = _closed(df)
    columns = ["closed_at", "timestamp", "symbol", "side", "result", "hit_target", "entry", "tp1", "tp2", "stop_loss"]
    available = [column for column in columns if column in events.columns]
    if events.empty:
        return pd.DataFrame(columns=available)
    sort_column = "closed_at" if "closed_at" in events.columns and events["closed_at"].notna().any() else "timestamp"
    return events.sort_values(sort_column, ascending=False).head(limit)[available]


def apply_filters(
    df: pd.DataFrame,
    start_date: date | None = None,
    end_date: date | None = None,
    symbols: list[str] | None = None,
    tiers: list[str] | None = None,
    sessions: list[str] | None = None,
    sides: list[str] | None = None,
    sources: list[str] | None = None,
    results: list[str] | None = None,
    targets: list[str] | None = None,
    score_range: tuple[float, float] | None = None,
    confidence_range: tuple[float, float] | None = None,
) -> pd.DataFrame:
    data = df.copy()
    if data.empty:
        return data
    if "timestamp" in data.columns and (start_date or end_date):
        timestamps = pd.to_datetime(data["timestamp"], utc=True, errors="coerce")
        if start_date:
            data = data[timestamps.dt.date >= start_date]
            timestamps = pd.to_datetime(data["timestamp"], utc=True, errors="coerce")
        if end_date:
            data = data[timestamps.dt.date <= end_date]
    if symbols:
        data = data[data["symbol"].isin(symbols)]
    if tiers and "watchlist_tier" in data.columns:
        data = data[data["watchlist_tier"].isin(tiers)]
    if sessions and "market_session" in data.columns:
        data = data[data["market_session"].isin(sessions)]
    if sides and "side" in data.columns:
        data = data[data["side"].isin(sides)]
    if sources and "source" in data.columns:
        data = data[data["source"].isin(sources)]
    if results and "result" in data.columns:
        data = data[data["result"].isin(results)]
    if targets and "hit_target" in data.columns:
        data = data[data["hit_target"].isin(targets)]
    if score_range and "raw_score" in data.columns:
        scores = pd.to_numeric(data["raw_score"], errors="coerce")
        data = data[scores.between(score_range[0], score_range[1], inclusive="both")]
    if confidence_range and "setup_strength" in data.columns:
        confidence = pd.to_numeric(data["setup_strength"], errors="coerce")
        data = data[confidence.between(confidence_range[0], confidence_range[1], inclusive="both")]
    return data


def quality_views(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    closed = _closed(df)
    if closed.empty:
        empty = pd.DataFrame()
        return {
            "high_score_losses": empty,
            "low_score_wins": empty,
            "high_drawdown_winners": empty,
            "fast_sl": empty,
            "slow_tp": empty,
            "repeated_loss_symbols": empty,
            "poor_sessions": empty,
            "tier_c": empty,
        }
    data = closed.copy()
    data["hold_minutes"] = _holding_minutes(data)
    score = pd.to_numeric(data.get("raw_score", pd.Series(dtype=float)), errors="coerce")
    drawdown = pd.to_numeric(data.get("max_drawdown_pct", pd.Series(dtype=float)), errors="coerce")
    high_score_losses = data[(data["result"] == "LOSS") & (score >= 85)].sort_values("raw_score", ascending=False)
    low_score_wins = data[(data["result"] == "WIN") & (score < 75)].sort_values("raw_score", ascending=True)
    drawdown_threshold = drawdown.quantile(0.25) if drawdown.notna().any() else None
    high_drawdown_winners = (
        data[(data["result"] == "WIN") & (drawdown <= drawdown_threshold)]
        if drawdown_threshold is not None else data.iloc[0:0]
    )
    fast_sl = data[(data["result"] == "LOSS") & (data["hold_minutes"] <= 60)].sort_values("hold_minutes")
    slow_tp = data[(data["result"] == "WIN") & (data["hold_minutes"] >= 360)].sort_values("hold_minutes", ascending=False)
    repeated = performance_table(data[data["result"] == "LOSS"], "symbol")
    poor_sessions = performance_table(data, "market_session")
    tier_c = data[data.get("watchlist_tier", pd.Series(dtype=str)).astype(str).str.upper() == "C"]
    return {
        "high_score_losses": high_score_losses,
        "low_score_wins": low_score_wins,
        "high_drawdown_winners": high_drawdown_winners.sort_values("max_drawdown_pct").head(25),
        "fast_sl": fast_sl,
        "slow_tp": slow_tp,
        "repeated_loss_symbols": repeated[repeated["losses"] >= 2].sort_values("losses", ascending=False),
        "poor_sessions": poor_sessions[(poor_sessions["closed"] >= 3) & (poor_sessions["net_r"] < 0)].sort_values("net_r"),
        "tier_c": tier_c,
    }


def analytics_suggestions(df: pd.DataFrame) -> list[str]:
    suggestions = []
    symbol_perf = performance_table(df, "symbol")
    weak_symbols = symbol_perf[(symbol_perf["closed"] >= 5) & (symbol_perf["win_rate"] < 40)] if not symbol_perf.empty else pd.DataFrame()
    if not weak_symbols.empty:
        suggestions.append("Consider reviewing symbols with win rate below 40% and at least 5 closed trades.")
    session_perf = performance_table(df, "market_session")
    if not session_perf.empty and (session_perf["net_r"] < 0).any():
        suggestions.append("Consider reviewing sessions with negative net R.")
    if not quality_views(df)["high_score_losses"].empty:
        suggestions.append("Consider monitoring high-score losses for common failure patterns.")
    tier_perf = performance_table(df, "watchlist_tier")
    tier_c = tier_perf[tier_perf["watchlist_tier"].astype(str).str.upper() == "C"] if not tier_perf.empty else pd.DataFrame()
    if not tier_c.empty and float(tier_c.iloc[0]["win_rate"]) < 45 and int(tier_c.iloc[0]["closed"]) >= 5:
        suggestions.append("Consider Tier C restrictions if Tier C win rate remains weak.")
    if not suggestions:
        suggestions.append("No major rule-based warning from current filtered data. Continue collecting outcomes.")
    return suggestions


def _format_metric(value: Any) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def _streamlit_app() -> None:
    try:
        import streamlit as st
    except ModuleNotFoundError as exc:
        raise SystemExit("Streamlit is not installed. Run: pip install -r requirements.txt") from exc

    st.set_page_config(page_title="Crypto Scanner Dashboard V2", layout="wide")
    data = load_dashboard_data()
    sent = data["sent"]

    st.title("Crypto Multi-Coin Scanner Dashboard V2")
    st.caption("Read-only analytics dashboard. No Telegram sends, no API calls, no log writes, no auto trading.")

    with st.sidebar:
        st.header("Filters")
        timestamps = pd.to_datetime(sent.get("timestamp", pd.Series(dtype=str)), utc=True, errors="coerce").dropna()
        min_date = timestamps.dt.date.min() if not timestamps.empty else None
        max_date = timestamps.dt.date.max() if not timestamps.empty else None
        selected_range = st.date_input(
            "Date range",
            value=(min_date, max_date) if min_date and max_date else (date.today(), date.today()),
        )
        start_date = end_date = None
        if isinstance(selected_range, tuple) and len(selected_range) == 2:
            start_date, end_date = selected_range
        symbols = st.multiselect("Symbol", sorted(sent["symbol"].dropna().unique().tolist()) if "symbol" in sent.columns else [])
        tiers = st.multiselect("Tier", sorted(sent["watchlist_tier"].dropna().unique().tolist()) if "watchlist_tier" in sent.columns else [])
        sessions = st.multiselect("Session", sorted(sent["market_session"].dropna().unique().tolist()) if "market_session" in sent.columns else [])
        sides = st.multiselect("Direction", ["LONG", "SHORT"])
        results = st.multiselect("Result", ["WIN", "LOSS", "OPEN"])
        targets = st.multiselect("Hit target", ["TP1", "TP2", "TP3", "SL"])
        score_values = pd.to_numeric(sent.get("raw_score", pd.Series(dtype=float)), errors="coerce").dropna()
        confidence_values = pd.to_numeric(sent.get("setup_strength", pd.Series(dtype=float)), errors="coerce").dropna()
        score_range = st.slider(
            "Score range",
            0,
            100,
            (int(score_values.min()), int(score_values.max())) if not score_values.empty else (0, 100),
        )
        confidence_range = st.slider(
            "Setup strength range",
            0,
            100,
            (int(confidence_values.min()), int(confidence_values.max())) if not confidence_values.empty else (0, 100),
        )
        sources = st.multiselect("Source", sorted(sent["source"].dropna().unique().tolist()) if "source" in sent.columns else [])

    filtered = apply_filters(
        sent,
        start_date,
        end_date,
        symbols,
        tiers,
        sessions,
        sides,
        sources,
        results,
        targets,
        score_range,
        confidence_range,
    )
    filtered = ensure_score_buckets(filtered)
    kpis = dashboard_kpis(filtered)

    metric_columns = st.columns(4)
    for index, label in enumerate([
        "Total sent signals", "Closed trades", "Win rate", "Net R",
    ]):
        value = kpis[label]
        suffix = "%" if label == "Win rate" else ""
        metric_columns[index].metric(label, f"{_format_metric(value)}{suffix}")
    metric_columns = st.columns(4)
    for index, label in enumerate(["Long win rate", "Short win rate", "TP1 hit rate", "SL hit rate"]):
        metric_columns[index].metric(label, f"{_format_metric(kpis[label])}%")

    symbol_perf = performance_table(filtered, "symbol")
    tier_perf = performance_table(filtered, "watchlist_tier")
    session_perf = performance_table(filtered, "market_session")
    side_perf = performance_table(filtered, "side")
    score_perf = performance_table(filtered, "score_range")
    confidence_perf = performance_table(filtered, "confidence_range")
    distribution = tp_sl_distribution(filtered)
    quality = quality_views(filtered)

    with st.expander("Executive Summary", expanded=True):
        col1, col2, col3 = st.columns(3)
        col1.write(f"Best symbol by win rate: **{kpis['Best symbol']}**")
        col1.write(f"Worst symbol by win rate: **{kpis['Worst symbol']}**")
        col2.write(f"Best symbol by net R: **{kpis['Best symbol by net R']}**")
        col2.write(f"Worst symbol by net R: **{kpis['Worst symbol by net R']}**")
        col3.write(f"Best tier/session: **{kpis['Best tier']} / {kpis['Best session']}**")
        col3.write(f"Worst tier/session: **{kpis['Worst tier']} / {kpis['Worst session']}**")
        st.info("Analytics suggestion only. These observations do not auto-change config or strategy.")
        for suggestion in analytics_suggestions(filtered):
            st.write(f"- {suggestion}")

    with st.expander("Win/Loss Analytics", expanded=True):
        left, right = st.columns(2)
        daily = win_loss_by_day(filtered)
        left.bar_chart(daily.set_index("date") if not daily.empty else daily)
        net_daily = daily_net_r(filtered)
        right.line_chart(net_daily.set_index("date")["net_r"] if not net_daily.empty else net_daily)

    with st.expander("Drawdown Analytics"):
        cols = st.columns(4)
        for index, label in enumerate(["Avg max drawdown %", "Avg drawdown winners", "Avg drawdown losers", "Max drawdown ever"]):
            cols[index].metric(label, _format_metric(kpis[label]))
        left, right = st.columns(2)
        left.bar_chart(symbol_perf.set_index("symbol")["avg_drawdown"] if not symbol_perf.empty else symbol_perf)
        right.bar_chart(tier_perf.set_index("watchlist_tier")["avg_drawdown"] if not tier_perf.empty else tier_perf)

    with st.expander("Symbol Analytics"):
        left, right = st.columns(2)
        left.bar_chart(symbol_perf.set_index("symbol")["win_rate"] if not symbol_perf.empty else symbol_perf)
        right.bar_chart(symbol_perf.set_index("symbol")["net_r"] if not symbol_perf.empty else symbol_perf)
        st.dataframe(symbol_perf, use_container_width=True)

    with st.expander("Tier Analytics"):
        left, right = st.columns(2)
        left.bar_chart(tier_perf.set_index("watchlist_tier")["win_rate"] if not tier_perf.empty else tier_perf)
        right.bar_chart(tier_perf.set_index("watchlist_tier")["net_r"] if not tier_perf.empty else tier_perf)
        st.dataframe(tier_perf, use_container_width=True)

    with st.expander("Session Analytics"):
        left, right = st.columns(2)
        left.bar_chart(session_perf.set_index("market_session")["win_rate"] if not session_perf.empty else session_perf)
        right.bar_chart(session_perf.set_index("market_session")["net_r"] if not session_perf.empty else session_perf)
        st.dataframe(session_perf, use_container_width=True)

    with st.expander("Long vs Short Analytics"):
        left, right = st.columns(2)
        left.bar_chart(side_perf.set_index("side")["win_rate"] if not side_perf.empty else side_perf)
        right.bar_chart(side_perf.set_index("side")["net_r"] if not side_perf.empty else side_perf)
        st.dataframe(side_perf, use_container_width=True)

    with st.expander("Score / Confidence Analytics"):
        left, right = st.columns(2)
        left.bar_chart(score_perf.set_index("score_range")["win_rate"] if not score_perf.empty else score_perf)
        right.bar_chart(confidence_perf.set_index("confidence_range")["win_rate"] if not confidence_perf.empty else confidence_perf)
        st.write("Score bucket performance")
        st.dataframe(score_perf, use_container_width=True)
        st.write("Confidence bucket performance")
        st.dataframe(confidence_perf, use_container_width=True)

    with st.expander("TP/SL Analytics"):
        st.bar_chart(distribution.set_index("target")["count"] if not distribution.empty else distribution)
        cols = st.columns(4)
        for index, label in enumerate(["TP1 hit rate", "TP2 hit rate", "SL hit rate", "Avg time to TP"]):
            suffix = "%" if "rate" in label else " min"
            value = kpis[label]
            cols[index].metric(label, f"{_format_metric(value)}{suffix if value is not None else ''}")

    with st.expander("External VIP Signal Analytics"):
        external = external_summary(data.get("external_signals", pd.DataFrame()))
        st.bar_chart(external.set_index("recommendation")["count"] if not external.empty else external)
        st.dataframe(data.get("external_signals", pd.DataFrame()).tail(50), use_container_width=True)

    with st.expander("Position Manager Analytics"):
        st.dataframe(data.get("position_management", pd.DataFrame()).tail(100), use_container_width=True)
        st.write("Open positions")
        st.dataframe(open_positions(filtered), use_container_width=True)

    with st.expander("Risk-Quality Views"):
        st.write("High score but lost trades")
        st.dataframe(quality["high_score_losses"].head(25), use_container_width=True)
        st.write("Low score but won trades")
        st.dataframe(quality["low_score_wins"].head(25), use_container_width=True)
        st.write("High drawdown winners")
        st.dataframe(quality["high_drawdown_winners"].head(25), use_container_width=True)
        st.write("Fast SL trades")
        st.dataframe(quality["fast_sl"].head(25), use_container_width=True)
        st.write("Slow TP trades")
        st.dataframe(quality["slow_tp"].head(25), use_container_width=True)
        st.write("Symbols with repeated losses")
        st.dataframe(quality["repeated_loss_symbols"].head(25), use_container_width=True)
        st.write("Sessions with poor performance")
        st.dataframe(quality["poor_sessions"].head(25), use_container_width=True)
        st.write("Tier C risk review")
        st.dataframe(quality["tier_c"].head(50), use_container_width=True)

    with st.expander("Recent Sent Signals"):
        st.dataframe(latest_signals(filtered, 50), use_container_width=True)

    with st.expander("Closed Trades"):
        st.dataframe(recent_events(filtered, 50), use_container_width=True)


def _table_html(df: pd.DataFrame) -> str:
    if df.empty:
        return "<p class='muted'>No data.</p>"
    return df.to_html(index=False, classes="data-table", float_format=lambda value: f"{value:.2f}")


def render_dashboard(df: pd.DataFrame, output: Path = DASHBOARD_HTML) -> Path:
    """Backward-compatible static HTML renderer used by smoke tests."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    sent = sent_signals(df)
    report = build_report(sent)
    symbol_perf = performance_table(sent, "symbol")
    tier_perf = performance_table(sent, "watchlist_tier")
    session_perf = performance_table(sent, "market_session")
    direction_perf = performance_table(sent, "side")
    kpis = dashboard_kpis(sent)

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Crypto Scanner Dashboard</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; color: #111827; background: #f8fafc; }}
    h1, h2 {{ margin: 0 0 12px; }}
    section {{ margin: 24px 0; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }}
    .card {{ background: white; border: 1px solid #e5e7eb; border-radius: 8px; padding: 14px; }}
    .label {{ color: #64748b; font-size: 12px; text-transform: uppercase; }}
    .value {{ font-size: 24px; font-weight: 700; margin-top: 6px; }}
    .data-table {{ border-collapse: collapse; width: 100%; background: white; font-size: 13px; }}
    .data-table th, .data-table td {{ border: 1px solid #e5e7eb; padding: 8px; text-align: left; }}
    .data-table th {{ background: #f1f5f9; }}
    .muted {{ color: #64748b; }}
  </style>
</head>
<body>
  <h1>Crypto Scanner Dashboard</h1>
  <p class="muted">Local read-only dashboard. Telegram signal assistant only. No auto trading.</p>

  <section class="cards">
    <div class="card"><div class="label">Total Sent Signals</div><div class="value">{report['total_sent_signals']}</div></div>
    <div class="card"><div class="label">Closed Trades</div><div class="value">{report['closed_signals']}</div></div>
    <div class="card"><div class="label">Wins</div><div class="value">{report['wins']}</div></div>
    <div class="card"><div class="label">Losses</div><div class="value">{report['losses']}</div></div>
    <div class="card"><div class="label">Win Rate</div><div class="value">{report['win_rate']:.1f}%</div></div>
    <div class="card"><div class="label">Best Symbol</div><div class="value">{kpis['Best symbol']}</div></div>
  </section>

  <section><h2>Recent Sent Signals</h2>{_table_html(latest_signals(sent))}</section>
  <section><h2>Closed Trades</h2>{_table_html(recent_events(sent))}</section>
  <section><h2>Symbol Performance</h2>{_table_html(symbol_perf)}</section>
  <section><h2>Tier Performance</h2>{_table_html(tier_perf)}</section>
  <section><h2>Session Performance</h2>{_table_html(session_perf)}</section>
  <section><h2>Direction Performance</h2>{_table_html(direction_perf)}</section>
  <section><h2>Open Positions</h2>{_table_html(open_positions(sent))}</section>
</body>
</html>
"""
    output.write_text(html, encoding="utf-8")
    return output


def main() -> int:
    _streamlit_app()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
