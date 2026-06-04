# -*- coding: utf-8 -*-
"""Streamlit Dashboard V1 for Crypto Multi-Coin Scanner performance.

The dashboard is read-only: it reads local CSV logs and never sends Telegram,
places trades, calls exchange APIs, or mutates log files.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from core.analytics_reporting import load_csv_safely
from performance_report import build_report, normalize, sent_signals
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
    best_tier, _worst_tier = _best_worst_by_win_rate(sent, "watchlist_tier")
    best_session, _worst_session = _best_worst_by_win_rate(sent, "market_session")
    return {
        "Total sent signals": int(len(sent)),
        "Closed trades": int(len(closed)),
        "Wins": wins,
        "Losses": losses,
        "Win rate": wins / len(closed) * 100 if len(closed) else 0.0,
        "TP1 hits": int(hit_target.isin(["TP1", "TP2", "TP3", "1", "2", "3"]).sum()),
        "TP2 hits": int(hit_target.isin(["TP2", "TP3", "2", "3"]).sum()),
        "SL hits": int((hit_target == "SL").sum()),
        "Avg profit %": _numeric_mean(closed[closed["result"] == "WIN"], "max_profit_pct"),
        "Avg drawdown %": _numeric_mean(closed, "max_drawdown_pct"),
        "Best symbol": best_symbol,
        "Worst symbol": worst_symbol,
        "Best tier": best_tier,
        "Best session": best_session,
    }


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
                "net_r": float(closed.apply(lambda row: 1.0 if row["result"] == "WIN" else -1.0, axis=1).sum()) if len(closed) else 0.0,
            }
        )
    if not rows:
        return pd.DataFrame(columns=[column, "signals", "closed", "wins", "losses", "win_rate", "net_r"])
    return pd.DataFrame(rows).sort_values(["win_rate", "closed"], ascending=[False, False])


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
    columns = ["timestamp", "symbol", "side", "entry", "tp1", "tp2", "stop_loss", "watchlist_tier", "market_session"]
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
    return data


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

    st.set_page_config(page_title="Crypto Scanner Dashboard", layout="wide")
    data = load_dashboard_data()
    sent = data["sent"]

    st.title("Crypto Multi-Coin Scanner Dashboard V1")
    st.caption("Read-only analytics dashboard. No Telegram sends, no API calls, no auto trading.")

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
        sources = st.multiselect("Source", sorted(sent["source"].dropna().unique().tolist()) if "source" in sent.columns else [])

    filtered = apply_filters(sent, start_date, end_date, symbols, tiers, sessions, sides, sources)
    kpis = dashboard_kpis(filtered)

    metric_columns = st.columns(7)
    for index, label in enumerate([
        "Total sent signals", "Closed trades", "Wins", "Losses", "Win rate", "TP1 hits", "TP2 hits",
    ]):
        value = kpis[label]
        suffix = "%" if label == "Win rate" else ""
        metric_columns[index].metric(label, f"{_format_metric(value)}{suffix}")
    metric_columns = st.columns(7)
    for index, label in enumerate([
        "SL hits", "Avg profit %", "Avg drawdown %", "Best symbol", "Worst symbol", "Best tier", "Best session",
    ]):
        value = kpis[label]
        suffix = "%" if label in {"Avg profit %", "Avg drawdown %"} and value is not None else ""
        metric_columns[index].metric(label, f"{_format_metric(value)}{suffix}")

    left, right = st.columns(2)
    with left:
        st.subheader("Win/Loss by day")
        chart = win_loss_by_day(filtered)
        st.bar_chart(chart.set_index("date") if not chart.empty else chart)
        st.subheader("Win rate by symbol")
        symbol_perf = performance_table(filtered, "symbol")
        st.bar_chart(symbol_perf.set_index("symbol")["win_rate"] if not symbol_perf.empty else symbol_perf)
        st.subheader("Win rate by tier")
        tier_perf = performance_table(filtered, "watchlist_tier")
        st.bar_chart(tier_perf.set_index("watchlist_tier")["win_rate"] if not tier_perf.empty else tier_perf)
        st.subheader("Score bucket performance")
        bucket_perf = performance_table(filtered, "score_bucket")
        st.bar_chart(bucket_perf.set_index("score_bucket")["win_rate"] if not bucket_perf.empty else bucket_perf)

    with right:
        st.subheader("Win rate by session")
        session_perf = performance_table(filtered, "market_session")
        st.bar_chart(session_perf.set_index("market_session")["win_rate"] if not session_perf.empty else session_perf)
        st.subheader("Long vs Short performance")
        side_perf = performance_table(filtered, "side")
        st.bar_chart(side_perf.set_index("side")["win_rate"] if not side_perf.empty else side_perf)
        st.subheader("TP1/TP2/SL distribution")
        distribution = tp_sl_distribution(filtered)
        st.bar_chart(distribution.set_index("target")["count"] if not distribution.empty else distribution)
        st.subheader("External VIP approval/rejection summary")
        external = external_summary(data.get("external_signals", pd.DataFrame()))
        st.bar_chart(external.set_index("recommendation")["count"] if not external.empty else external)

    st.subheader("Recent sent signals")
    st.dataframe(latest_signals(filtered, 50), use_container_width=True)
    st.subheader("Closed trades")
    st.dataframe(recent_events(filtered, 50), use_container_width=True)

    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("Best symbols")
        st.dataframe(symbol_perf.sort_values(["win_rate", "closed"], ascending=[False, False]).head(10), use_container_width=True)
    with col_b:
        st.subheader("Worst symbols")
        st.dataframe(symbol_perf[symbol_perf["closed"] > 0].sort_values(["win_rate", "closed"], ascending=[True, False]).head(10), use_container_width=True)

    st.subheader("Position manager history")
    st.dataframe(data.get("position_management", pd.DataFrame()), use_container_width=True)
    st.subheader("Open positions")
    st.dataframe(open_positions(filtered), use_container_width=True)


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
