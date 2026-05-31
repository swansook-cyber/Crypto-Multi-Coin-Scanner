# -*- coding: utf-8 -*-
"""Simple local HTML dashboard for Crypto Multi-Coin Scanner monitoring."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from core.analytics_reporting import load_csv_safely
from performance_report import build_report, normalize, sent_signals
from position_manager import latest_open_positions


BASE_DIR = Path(__file__).resolve().parent
JOURNAL = BASE_DIR / "logs" / "signals.csv"
REPORTS_DIR = BASE_DIR / "reports"
DASHBOARD_HTML = REPORTS_DIR / "dashboard.html"


def performance_table(df: pd.DataFrame, column: str) -> pd.DataFrame:
    closed = df[df["result"].isin(["WIN", "LOSS"])].copy()
    if closed.empty or column not in closed.columns:
        return pd.DataFrame(columns=[column, "signals", "closed", "wins", "losses", "win_rate", "net_r"])
    rows = []
    for key, group in df.groupby(df[column].fillna("-").astype(str)):
        group_closed = group[group["result"].isin(["WIN", "LOSS"])]
        wins = int((group_closed["result"] == "WIN").sum())
        losses = int((group_closed["result"] == "LOSS").sum())
        closed_count = len(group_closed)
        rows.append(
            {
                column: key,
                "signals": len(group),
                "closed": closed_count,
                "wins": wins,
                "losses": losses,
                "win_rate": wins / closed_count * 100 if closed_count else 0.0,
                "net_r": group_closed.apply(lambda row: 1.0 if row["result"] == "WIN" else -1.0, axis=1).sum() if closed_count else 0.0,
            }
        )
    return pd.DataFrame(rows).sort_values(["win_rate", "closed"], ascending=[False, False])


def latest_signals(df: pd.DataFrame, limit: int = 25) -> pd.DataFrame:
    columns = ["timestamp", "symbol", "side", "watchlist_tier", "market_session", "entry", "tp1", "tp2", "stop_loss", "result", "hit_target"]
    available = [column for column in columns if column in df.columns]
    return df.sort_values("timestamp", ascending=False).head(limit)[available]


def open_positions(df: pd.DataFrame) -> pd.DataFrame:
    open_df = latest_open_positions(df)
    columns = ["timestamp", "symbol", "side", "entry", "tp1", "tp2", "stop_loss", "watchlist_tier", "market_session"]
    available = [column for column in columns if column in open_df.columns]
    return open_df.sort_values("timestamp", ascending=False)[available]


def recent_events(df: pd.DataFrame, limit: int = 25) -> pd.DataFrame:
    events = df[df["result"].isin(["WIN", "LOSS"])].copy()
    sort_column = "closed_at" if "closed_at" in events.columns and events["closed_at"].notna().any() else "timestamp"
    columns = ["closed_at", "timestamp", "symbol", "side", "result", "hit_target", "entry", "tp1", "tp2", "stop_loss"]
    available = [column for column in columns if column in events.columns]
    return events.sort_values(sort_column, ascending=False).head(limit)[available]


def _table_html(df: pd.DataFrame) -> str:
    if df.empty:
        return "<p class='muted'>No data.</p>"
    return df.to_html(index=False, classes="data-table", float_format=lambda value: f"{value:.2f}")


def render_dashboard(df: pd.DataFrame, output: Path = DASHBOARD_HTML) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    sent = sent_signals(df)
    report = build_report(sent)
    symbol_perf = performance_table(sent, "symbol")
    tier_perf = performance_table(sent, "watchlist_tier")
    session_perf = performance_table(sent, "market_session")
    direction_perf = performance_table(sent, "side")

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
  <p class="muted">Local monitoring dashboard. Telegram signal assistant only. No auto trading.</p>

  <section class="cards">
    <div class="card"><div class="label">Total Signals</div><div class="value">{report['total_sent_signals']}</div></div>
    <div class="card"><div class="label">Closed</div><div class="value">{report['closed_signals']}</div></div>
    <div class="card"><div class="label">Open</div><div class="value">{report['open_signals']}</div></div>
    <div class="card"><div class="label">Win Rate</div><div class="value">{report['win_rate']:.1f}%</div></div>
    <div class="card"><div class="label">Net R</div><div class="value">{report['net_r_estimate']:.2f}R</div></div>
  </section>

  <section><h2>Latest Signals</h2>{_table_html(latest_signals(sent))}</section>
  <section><h2>Symbol Performance</h2>{_table_html(symbol_perf)}</section>
  <section><h2>Tier Performance</h2>{_table_html(tier_perf)}</section>
  <section><h2>Session Performance</h2>{_table_html(session_perf)}</section>
  <section><h2>Direction Performance</h2>{_table_html(direction_perf)}</section>
  <section><h2>Open Positions</h2>{_table_html(open_positions(sent))}</section>
  <section><h2>Recent TP/SL Events</h2>{_table_html(recent_events(sent))}</section>
</body>
</html>
"""
    output.write_text(html, encoding="utf-8")
    return output


def main() -> int:
    df = normalize(load_csv_safely(JOURNAL))
    output = render_dashboard(df)
    print(f"Dashboard written to: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
