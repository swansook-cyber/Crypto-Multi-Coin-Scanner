# -*- coding: utf-8 -*-
"""Daily performance report from closed scanner outcomes."""

from __future__ import annotations

import argparse
import html
import logging
import os
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from dotenv import load_dotenv

from core.analytics_reporting import load_csv_safely
from core.entry_timing_engine import format_entry_timing_summary, summarize_entry_timing
from core.performance_analytics_v1 import (
    NA,
    build_complete_report,
    export_v1_outputs,
    format_minutes,
    format_value,
)


BASE_DIR = Path(__file__).resolve().parent
JOURNAL = BASE_DIR / "logs" / "signals.csv"
HISTORY = BASE_DIR / "logs" / "signals_history.csv"
EXTERNAL = BASE_DIR / "logs" / "external_signals.csv"
ENTRY_TIMING = BASE_DIR / "logs" / "entry_timing_engine.csv"
LOGS_DIR = BASE_DIR / "logs"
REPORTS_DIR = BASE_DIR / "reports"
SMALL_SAMPLE_CLOSED_TRADES = 30
TELEGRAM_MESSAGE_LIMIT = 3900
FULL_WEB_REPORT = REPORTS_DIR / "report.html"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOGGER = logging.getLogger("performance_report")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    data = df.copy()
    defaults = {
        "timestamp": "",
        "symbol": "",
        "side": "",
        "watchlist_tier": "",
        "market_session": "",
        "entry": "",
        "stop_loss": "",
        "tp1": "",
        "tp2": "",
        "risk_reward": "",
        "result": "OPEN",
        "hit_target": "",
        "signal_status": "sent",
        "pnl_percent": "",
        "closed_at": "",
    }
    for column, default in defaults.items():
        if column not in data.columns:
            data[column] = default
    data["timestamp"] = pd.to_datetime(data["timestamp"], utc=True, errors="coerce")
    data["closed_at"] = pd.to_datetime(data["closed_at"], utc=True, errors="coerce")
    data["symbol"] = data["symbol"].fillna("").astype(str).str.upper()
    data["side"] = data["side"].fillna("").astype(str).str.upper()
    data["watchlist_tier"] = data["watchlist_tier"].fillna("-").replace("", "-").astype(str).str.upper()
    data["market_session"] = data["market_session"].fillna("Other").replace("", "Other").astype(str)
    data["result"] = data["result"].fillna("OPEN").replace("", "OPEN").astype(str).str.upper()
    data["hit_target"] = data["hit_target"].fillna("").astype(str).str.upper()
    data["signal_status"] = data["signal_status"].fillna("sent").replace("", "sent").astype(str).str.lower()
    for column in ["entry", "stop_loss", "tp1", "tp2", "risk_reward", "pnl_percent"]:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    return data


def sent_signals(df: pd.DataFrame) -> pd.DataFrame:
    normalized = normalize(df)
    return normalized[normalized["signal_status"] == "sent"].copy()


def estimate_r(row: pd.Series) -> float:
    result = str(row.get("result", "")).upper()
    target = str(row.get("hit_target", "")).upper()
    rr = pd.to_numeric(pd.Series([row.get("risk_reward")]), errors="coerce").iloc[0]
    rr = 0.0 if pd.isna(rr) else float(rr)
    if result == "LOSS":
        return -1.0
    if result == "WIN" and target == "TP2":
        return rr if rr > 0 else 2.0
    if result == "WIN":
        return min(rr, 1.2) if rr > 0 else 1.0
    return 0.0


def pnl_percent(row: pd.Series) -> float:
    value = pd.to_numeric(pd.Series([row.get("pnl_percent")]), errors="coerce").iloc[0]
    if not pd.isna(value):
        return float(value)
    result = str(row.get("result", "")).upper()
    side = str(row.get("side", "")).upper()
    entry = float(row.get("entry", 0) or 0)
    if entry <= 0:
        return 0.0
    if result == "LOSS":
        sl = float(row.get("stop_loss", 0) or 0)
        if sl <= 0:
            return 0.0
        raw = (sl - entry) / entry * 100 if side == "LONG" else (entry - sl) / entry * 100
        return -abs(raw)
    if result == "WIN":
        target_col = "tp2" if str(row.get("hit_target", "")).upper() == "TP2" else "tp1"
        target = float(row.get(target_col, 0) or 0)
        if target <= 0:
            return 0.0
        raw = (target - entry) / entry * 100 if side == "LONG" else (entry - target) / entry * 100
        return abs(raw)
    return 0.0


def win_rate_by(df: pd.DataFrame, column: str, best: bool) -> str:
    closed = df[df["result"].isin(["WIN", "LOSS"])].copy()
    if closed.empty or column not in closed.columns:
        return "-"
    rows = []
    for key, group in closed.groupby(closed[column].fillna("-").astype(str)):
        if not key or key == "-":
            continue
        trades = len(group)
        wins = int((group["result"] == "WIN").sum())
        rows.append({"key": key, "trades": trades, "win_rate": wins / trades * 100 if trades else 0.0})
    if not rows:
        return "-"
    ranked = pd.DataFrame(rows).sort_values(["win_rate", "trades"], ascending=[not best, False])
    top = ranked.iloc[0]
    return f"{top['key']} ({top['win_rate']:.1f}%, {int(top['trades'])})"


def direction_win_rate(df: pd.DataFrame, side: str) -> float:
    closed = df[(df["result"].isin(["WIN", "LOSS"])) & (df["side"] == side.upper())]
    if closed.empty:
        return 0.0
    return float((closed["result"] == "WIN").mean() * 100)


def build_report(df: pd.DataFrame, date: str | None = None) -> dict[str, Any]:
    report, _tables = build_complete_report(df, pd.DataFrame(), pd.DataFrame(), date)
    # Backward-compatible aliases used by older dashboard/tests.
    report["avg_win_pct"] = report.get("avg_profit_pct") or 0.0
    report["avg_loss_pct"] = report.get("avg_loss_pct") or 0.0
    report["win_rate"] = report.get("win_rate") or 0.0
    report["long_win_rate"] = report.get("long_win_rate") or 0.0
    report["short_win_rate"] = report.get("short_win_rate") or 0.0
    return report


def build_full_report(
    journal: pd.DataFrame,
    history: pd.DataFrame,
    external: pd.DataFrame,
    date: str | None = None,
) -> tuple[dict[str, Any], dict[str, pd.DataFrame]]:
    return build_complete_report(journal, history, external, date)


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def format_report(report: dict[str, Any]) -> str:
    warning = "\n\nSample size is still small. Use for monitoring only." if report.get("small_sample_warning") else ""
    win_rate = format_value(report.get("win_rate"), "%")
    long_rate = format_value(report.get("long_win_rate"), "%")
    short_rate = format_value(report.get("short_win_rate"), "%")
    return (
        "Daily Performance Report\n"
        f"Date: {report['date']}\n\n"
        f"Total sent signals: {report['total_sent_signals']}\n"
        f"Closed signals: {report['closed_signals']}\n"
        f"Open signals: {report['open_signals']}\n"
        f"Wins: {report['wins']}\n"
        f"Losses: {report['losses']}\n"
        f"Win rate: {win_rate}\n"
        f"TP1 hits: {report['tp1_hits']}\n"
        f"TP2 hits: {report['tp2_hits']}\n"
        f"TP3 hits: {report.get('tp3_hits', 0) if report.get('tp3_hits', 0) else NA}\n"
        f"SL hits: {report['sl_hits']}\n"
        f"Net R estimate: {report['net_r_estimate']:.2f}R\n"
        f"Avg Profit %: {format_value(report.get('avg_profit_pct'), '%', 2)}\n"
        f"Avg Loss %: {format_value(report.get('avg_loss_pct'), '%', 2)}\n"
        f"Avg Drawdown %: {format_value(report.get('avg_drawdown_pct'), '%', 2)}\n"
        f"Avg Max Profit %: {format_value(report.get('avg_max_profit_pct'), '%', 2)}\n"
        f"Avg Time to TP: {format_minutes(report.get('avg_time_to_tp'))}\n"
        f"Avg Time to SL: {format_minutes(report.get('avg_time_to_sl'))}\n\n"
        f"Best symbol: {report['best_symbol']}\n"
        f"Worst symbol: {report['worst_symbol']}\n"
        f"Best tier: {report['best_tier']}\n"
        f"Worst tier: {report['worst_tier']}\n"
        f"Best session: {report['best_session']}\n"
        f"Worst session: {report['worst_session']}\n"
        f"Long win rate: {long_rate}\n"
        f"Short win rate: {short_rate}\n\n"
        "Scanner vs External:\n"
        f"Scanner win rate: {format_value(report.get('scanner_win_rate'), '%')}\n\n"
        "External Signal Performance\n"
        f"Reviewed: {report.get('external_total', 0)}\n"
        f"Approved: {report.get('external_approved', 0)}\n"
        f"Rejected: {report.get('external_rejected', 0)}\n"
        f"Approval Rate: {format_value(report.get('external_approval_rate'), '%')}\n\n"
        "Approved outcomes:\n"
        f"Wins: {report.get('external_wins', 0)}\n"
        f"Losses: {report.get('external_losses', 0)}\n"
        f"Open: {report.get('external_open', 0)}\n"
        f"Win Rate: {format_value(report.get('external_win_rate'), '%')}\n"
        f"Net R estimate: {report.get('external_net_r_estimate', 0.0):.2f}R\n\n"
        "Top Reject Reasons:\n"
        f"{report.get('external_top_reject_reasons') or NA}\n\n"
        "Top Approved Symbols:\n"
        f"{report.get('external_top_approved_symbols') or NA}\n\n"
        "Top Rejected Symbols:\n"
        f"{report.get('external_top_rejected_symbols') or NA}\n\n"
        "Performance Analytics V2 Warnings:\n"
        f"{report.get('performance_warnings') or NA}\n\n"
        "Performance Analytics V3\n"
        "By Symbol:\n"
        f"{report.get('performance_v3_symbol') or NA}\n\n"
        "By Session:\n"
        f"{report.get('performance_v3_session') or NA}\n\n"
        "By Tier:\n"
        f"{report.get('performance_v3_tier') or NA}\n\n"
        "By Direction:\n"
        f"{report.get('performance_v3_direction') or NA}\n\n"
        "By Hour UTC:\n"
        f"{report.get('performance_v3_hour') or NA}\n\n"
        "Score Performance Analytics\n"
        f"{report.get('score_performance_v3') or NA}\n\n"
        "Score Deep Audit\n"
        "Score x Tier:\n"
        f"{report.get('score_tier_audit') or NA}\n\n"
        "Score x Session:\n"
        f"{report.get('score_session_audit') or NA}\n\n"
        "Score x Direction:\n"
        f"{report.get('score_direction_audit') or NA}\n\n"
        "Score x Symbol (>=5 closed trades):\n"
        f"{report.get('score_symbol_audit') or NA}\n\n"
        "Score Efficiency Analysis:\n"
        f"{report.get('score_efficiency_audit') or NA}\n\n"
        "Score Calibration Report\n"
        f"{report.get('score_calibration_report') or NA}\n\n"
        "Score Calibration Recommendations\n"
        f"{report.get('score_calibration_recommendations') or NA}\n\n"
        "Strategy Filter Simulator\n"
        f"{report.get('strategy_filter_simulator') or NA}\n\n"
        "Top Strategy Candidates\n"
        f"{report.get('top_strategy_candidates') or NA}\n\n"
        "Strategy Filter Recommendations\n"
        f"{report.get('strategy_filter_recommendations') or NA}\n\n"
        "Production Universe Ranking\n"
        f"{report.get('production_universe_ranking') or NA}\n\n"
        "Recommended Production Universe\n"
        "Tier S:\n"
        f"{report.get('production_universe_tier_s') or NA}\n\n"
        "Tier A:\n"
        f"{report.get('production_universe_tier_a') or NA}\n\n"
        "Watch:\n"
        f"{report.get('production_universe_watch') or NA}\n\n"
        "Report Only:\n"
        f"{report.get('production_universe_report_only') or NA}\n\n"
        "Post-Filter Live Performance\n"
        f"{report.get('post_filter_live_performance') or NA}\n\n"
        "Production Universe Performance\n"
        f"{report.get('production_universe_performance') or NA}\n\n"
        "Shadow Filter Backtest\n"
        f"{report.get('shadow_filter_backtest') or NA}\n\n"
        "Recommended Actions\n"
        f"{report.get('recommended_actions') or NA}\n\n"
        "Root Cause Analytics\n"
        "Score x Session:\n"
        f"{report.get('root_score_session') or NA}\n\n"
        "Score x Direction:\n"
        f"{report.get('root_score_direction') or NA}\n\n"
        "Tier x Session:\n"
        f"{report.get('root_tier_session') or NA}\n\n"
        "Symbol x Session (>=5 closed trades):\n"
        f"{report.get('root_symbol_session') or NA}\n\n"
        "Symbol x Direction (>=5 closed trades):\n"
        f"{report.get('root_symbol_direction') or NA}\n\n"
        "Loss Cluster Analysis:\n"
        f"{report.get('root_loss_clusters') or NA}\n\n"
        "Win Cluster Analysis:\n"
        f"{report.get('root_win_clusters') or NA}\n\n"
        "Root Cause Recommendations\n"
        f"{report.get('root_cause_recommendations') or NA}\n\n"
        "Entry Timing Engine Shadow Summary\n"
        f"{report.get('entry_timing_shadow_summary') or NA}\n\n"
        "Tier C Experimental Performance\n"
        f"Reported: {report.get('tier_c_report_count', 0)}\n"
        f"Wins: {report.get('tier_c_report_wins', 0)}\n"
        f"Losses: {report.get('tier_c_report_losses', 0)}\n"
        f"Win Rate: {format_value(report.get('tier_c_report_win_rate'), '%')}\n\n"
        "Weak Symbol Experimental Performance\n"
        f"Reported: {report.get('weak_symbol_report_count', 0)}\n"
        f"Wins: {report.get('weak_symbol_report_wins', 0)}\n"
        f"Losses: {report.get('weak_symbol_report_losses', 0)}\n"
        f"Win Rate: {format_value(report.get('weak_symbol_report_win_rate'), '%')}\n\n"
        "Session Risk Experimental Performance\n"
        f"Reported: {report.get('session_risk_report_count', 0)}\n"
        f"Wins: {report.get('session_risk_report_wins', 0)}\n"
        f"Losses: {report.get('session_risk_report_losses', 0)}\n"
        f"Win Rate: {format_value(report.get('session_risk_report_win_rate'), '%')}\n\n"
        "London Long Experimental Performance\n"
        f"Reported: {report.get('london_long_report_count', 0)}\n"
        f"Wins: {report.get('london_long_report_wins', 0)}\n"
        f"Losses: {report.get('london_long_report_losses', 0)}\n"
        f"Open: {report.get('london_long_report_open', 0)}\n"
        f"Win Rate: {format_value(report.get('london_long_report_win_rate'), '%')}\n"
        f"Net R: {report.get('london_long_report_net_r', 0.0):.2f}R\n\n"
        "TP1 / Breakeven Management\n"
        f"TP1 alerts by watcher: {report.get('tp1_alerts_watcher', 0)}\n"
        f"TP1 alerts by outcome review: {report.get('tp1_alerts_outcome_review', 0)}\n"
        f"Breakeven recommendations: {report.get('breakeven_recommendations', 0)}\n"
        f"Open BE recommended stage: {report.get('open_tp1_be_recommended', 0)}\n\n"
        "Position Manager Outcomes:\n"
        f"HOLD count: {report.get('hold_count', 0)}\n"
        f"OPPOSITE signal count: {report.get('opposite_signal_count', 0)}\n"
        f"EXIT recommendation count: {report.get('exit_recommendation_count', 0)}\n"
        f"Stale position count: {report.get('stale_position_count', 0)}"
        f"{warning}"
    )


def _compact_value(value: Any, max_chars: int = 120) -> str:
    text = str(value or "").strip()
    if not text or text == NA:
        return NA
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    compact = " | ".join(lines[:2]) if lines else text
    return compact[: max_chars - 3].rstrip() + "..." if len(compact) > max_chars else compact


def _table_lines(value: Any, limit: int = 3) -> list[str]:
    text = str(value or "").strip()
    if not text or text == NA:
        return []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    return lines[1 : 1 + limit] if len(lines) > 1 else lines[:limit]


def _entry_timing_counts(entry_timing: pd.DataFrame) -> dict[str, Any]:
    recommendations = [
        "ENTER NOW",
        "WAIT FOR PULLBACK",
        "WAIT FOR BREAKOUT",
        "WAIT FOR BREAKOUT RETEST",
        "SKIP (poor timing)",
    ]
    counts = {item: 0 for item in recommendations}
    if entry_timing.empty or "recommendation" not in entry_timing.columns:
        return {"total": 0, "counts": counts, "best": "Collecting data"}
    rec = entry_timing["recommendation"].fillna("").astype(str)
    for item in recommendations:
        counts[item] = int((rec == item).sum())
    return {"total": int(len(entry_timing)), "counts": counts, "best": "Collecting data"}


def _better_direction(report: dict[str, Any]) -> str:
    long_rate = report.get("long_win_rate")
    short_rate = report.get("short_win_rate")
    try:
        long_value = float(long_rate)
        short_value = float(short_rate)
    except (TypeError, ValueError):
        return NA
    if long_value == short_value:
        return f"Mixed ({format_value(long_value, '%')})"
    if long_value > short_value:
        return f"LONG ({format_value(long_value, '%')})"
    return f"SHORT ({format_value(short_value, '%')})"


def _executive_warnings(report: dict[str, Any], limit: int = 5) -> list[str]:
    warnings: list[str] = []
    for key in [
        "performance_warnings",
        "score_calibration_recommendations",
        "strategy_filter_recommendations",
        "root_cause_recommendations",
        "recommended_actions",
    ]:
        for line in _table_lines(report.get(key), 2):
            cleaned = " ".join(line.split())
            if cleaned and cleaned not in warnings:
                warnings.append(cleaned)
            if len(warnings) >= limit:
                return warnings
    if report.get("small_sample_warning"):
        warnings.append("Sample size is still small. Use for monitoring only.")
    return warnings[:limit] or ["No critical warnings."]


def format_executive_report(
    report: dict[str, Any],
    entry_timing: pd.DataFrame | None = None,
    dashboard_url: str | None = None,
) -> str:
    timing = _entry_timing_counts(entry_timing if entry_timing is not None else pd.DataFrame())
    counts = timing["counts"]
    warnings = _executive_warnings(report, 5)
    url = (dashboard_url or os.getenv("ANALYTICS_DASHBOARD_URL", "")).strip()
    production_symbols = []
    for label, key in [
        ("Tier S", "production_universe_tier_s"),
        ("Tier A", "production_universe_tier_a"),
        ("Report Only", "production_universe_report_only"),
    ]:
        value = _compact_value(report.get(key), 90)
        if value != NA:
            production_symbols.append(f"- {label}: {value}")
    if not production_symbols:
        production_symbols.append("- Collecting production universe data")

    lines = [
        "Daily Performance Summary",
        f"Date: {report.get('date', 'ALL')}",
        "",
        "Performance",
        f"- Closed: {report.get('closed_signals', 0)}",
        f"- Wins / Losses: {report.get('wins', 0)} / {report.get('losses', 0)}",
        f"- Win Rate: {format_value(report.get('win_rate'), '%')}",
        f"- Net R: {report.get('net_r_estimate', 0.0):.2f}R",
        f"- TP1 / TP2: {report.get('tp1_hits', 0)} / {report.get('tp2_hits', 0)}",
        f"- Avg time TP / SL: {format_minutes(report.get('avg_time_to_tp'))} / {format_minutes(report.get('avg_time_to_sl'))}",
        "",
        "Best Performance",
        f"- Best symbol: {report.get('best_symbol', NA)}",
        f"- Best tier: {report.get('best_tier', NA)}",
        f"- Best session: {report.get('best_session', NA)}",
        f"- Better direction: {_better_direction(report)}",
        "",
        "Warnings",
        *[f"- {item}" for item in warnings[:5]],
        "",
        "Production Universe",
        f"- Tier S + A performance: {_compact_value(report.get('production_universe_performance'), 120)}",
        *production_symbols,
        "",
        "Entry Timing Shadow",
        f"- Total evaluated: {timing['total']}",
        f"- ENTER NOW: {counts['ENTER NOW']}",
        f"- WAIT PULLBACK: {counts['WAIT FOR PULLBACK']}",
        f"- WAIT BREAKOUT: {counts['WAIT FOR BREAKOUT']}",
        f"- WAIT RETEST: {counts['WAIT FOR BREAKOUT RETEST']}",
        f"- SKIP: {counts['SKIP (poor timing)']}",
        f"- Best by WR: {timing['best']}",
        "",
        "Decision Summary",
        "- KEEP: production-safe routing and analytics exports unchanged",
        "- WATCH: weak warnings and entry timing shadow data",
        "- INVESTIGATE: review full dashboard before changing filters",
    ]
    if url:
        lines.extend(["", f"Full analytics: {url}"])
    return "\n".join(lines)


def write_full_web_report(full_message: str, path: Path = FULL_WEB_REPORT) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    csv_links = []
    for csv_path in sorted(LOGS_DIR.glob("*.csv")):
        csv_links.append(f'<li><a href="../logs/{html.escape(csv_path.name)}">{html.escape(csv_path.name)}</a></li>')
    body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Crypto Scanner Full Analytics Report</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; line-height: 1.45; background: #f8fafc; color: #0f172a; }}
    main {{ max-width: 1120px; margin: 0 auto; }}
    pre {{ white-space: pre-wrap; overflow-x: auto; background: white; border: 1px solid #e2e8f0; padding: 16px; border-radius: 8px; }}
    a {{ color: #0369a1; }}
  </style>
</head>
<body>
<main>
  <h1>Crypto Scanner Full Analytics Report</h1>
  <p>Read-only report. No Telegram sends, no API calls, no trading execution.</p>
  <h2>CSV Downloads</h2>
  <ul>{''.join(csv_links) or '<li>No CSV exports found yet.</li>'}</ul>
  <h2>Full Analytics</h2>
  <pre>{html.escape(full_message)}</pre>
</main>
</body>
</html>
"""
    path.write_text(body, encoding="utf-8")
    alias = REPORTS_DIR / "analytics.html"
    alias.write_text(body, encoding="utf-8")
    return path


def persist_report(report: dict[str, Any], path: Path | None = None) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    output = path or REPORTS_DIR / "daily_performance_report.csv"
    row = pd.DataFrame([report])
    if output.exists():
        existing = load_csv_safely(output)
        data = pd.concat([existing, row], ignore_index=True).drop_duplicates("date", keep="last")
    else:
        data = row
    data.to_csv(output, index=False)
    return output


def reports_chat_id() -> str:
    return os.getenv("TELEGRAM_REPORTS_CHAT_ID", "").strip()


def log_startup_route() -> None:
    LOGGER.info("PERFORMANCE REPORT ROUTE chat_id=%s", reports_chat_id() or "-")


def send_telegram(message: str, session: requests.Session | None = None) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = reports_chat_id()
    LOGGER.info("PERFORMANCE REPORT ROUTE chat_id=%s", chat_id or "-")
    if not token or not chat_id:
        LOGGER.warning("Performance report Telegram skipped: TELEGRAM_BOT_TOKEN or TELEGRAM_REPORTS_CHAT_ID missing")
        return False
    client = session or requests.Session()
    chunks = split_telegram_message(message)
    LOGGER.info("Performance report Telegram chunks=%s", len(chunks))
    for index, chunk in enumerate(chunks, start=1):
        label = f"{index}/{len(chunks)}"
        if not send_telegram_chunk(client, token, chat_id, chunk, label):
            return False
    return True


def split_telegram_message(message: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split large reports so Telegram does not reject them with message-too-long."""

    text = str(message or "")
    if not text:
        return [""]
    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        block = block.rstrip()
        if not block:
            continue
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(block) <= limit:
            current = block
            continue
        for start in range(0, len(block), limit):
            part = block[start : start + limit]
            if len(part) == limit:
                chunks.append(part)
            else:
                current = part
    if current:
        chunks.append(current)
    return chunks or [text[:limit]]


def send_telegram_chunk(
    client: requests.Session,
    token: str,
    chat_id: str,
    message: str,
    chunk_label: str,
) -> bool:
    try:
        response = client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": message},
            timeout=20,
        )
    except requests.RequestException as exc:
        LOGGER.error("Performance report Telegram chunk %s failed: %s", chunk_label, exc)
        return False
    if response.status_code != 200:
        LOGGER.error(
            "Performance report Telegram chunk %s failed: status=%s body=%s",
            chunk_label,
            response.status_code,
            response.text,
        )
        return False
    LOGGER.info("Performance report Telegram chunk %s send success: status=%s", chunk_label, response.status_code)
    return True


def send_test_report() -> bool:
    chat_id = reports_chat_id()
    print(f"Performance report destination chat id: {chat_id or '-'}")
    message = (
        "🧪 Crypto Scanner Performance Report Test\n"
        "Destination: TELEGRAM_REPORTS_CHAT_ID only\n"
        "No signal. No trade execution."
    )
    return send_telegram(message)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a daily performance report from closed outcomes.")
    parser.add_argument("--date", help="UTC date YYYY-MM-DD. Defaults to latest signal date.")
    parser.add_argument("--send", action="store_true", help="Send the report to Telegram.")
    parser.add_argument("--executive", action="store_true", help="Print the concise executive report without sending.")
    parser.add_argument("--test-report", action="store_true", help="Send a diagnostic message to TELEGRAM_REPORTS_CHAT_ID only.")
    parser.add_argument("--journal", type=Path, default=JOURNAL, help="Path to signals.csv.")
    parser.add_argument("--history", type=Path, default=HISTORY, help="Path to signals_history.csv.")
    parser.add_argument("--external", type=Path, default=EXTERNAL, help="Path to external_signals.csv.")
    parser.add_argument("--entry-timing", type=Path, default=ENTRY_TIMING, help="Path to entry_timing_engine.csv.")
    return parser.parse_args()


def run_report(args: argparse.Namespace, session: requests.Session | None = None) -> int:
    journal = load_csv_safely(args.journal)
    history = load_csv_safely(args.history)
    external = load_csv_safely(args.external)
    entry_timing = load_csv_safely(args.entry_timing)
    report, tables = build_full_report(journal, history, external, args.date)
    report["entry_timing_shadow_summary"] = format_entry_timing_summary(entry_timing)
    tables["entry_timing_shadow_summary"] = summarize_entry_timing(entry_timing)
    export_v1_outputs(report, tables, LOGS_DIR)
    tables["entry_timing_shadow_summary"].to_csv(LOGS_DIR / "entry_timing_shadow_summary.csv", index=False)
    persist_report(report)
    full_message = format_report(report)
    write_full_web_report(full_message)
    executive_message = format_executive_report(report, entry_timing)
    message = executive_message if getattr(args, "executive", False) else full_message
    print(message)
    if args.send:
        telegram_message = executive_message if env_bool("TELEGRAM_EXECUTIVE_REPORT_ONLY", True) else full_message
        return 0 if send_telegram(telegram_message, session=session) else 1
    return 0


def main() -> int:
    load_dotenv(BASE_DIR / ".env")
    args = parse_args()
    log_startup_route()
    if args.test_report:
        return 0 if send_test_report() else 1
    return run_report(args)


if __name__ == "__main__":
    raise SystemExit(main())
