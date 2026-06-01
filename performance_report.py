# -*- coding: utf-8 -*-
"""Daily performance report from closed scanner outcomes."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from dotenv import load_dotenv

from core.analytics_reporting import load_csv_safely
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
LOGS_DIR = BASE_DIR / "logs"
REPORTS_DIR = BASE_DIR / "reports"
SMALL_SAMPLE_CLOSED_TRADES = 30


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
        f"Scanner win rate: {format_value(report.get('scanner_win_rate'), '%')}\n"
        f"External signals reviewed: {report.get('external_total', 0)}\n"
        f"External approved: {report.get('external_approved', 0)}\n"
        f"External rejected: {report.get('external_rejected', 0)}\n"
        f"External approval rate: {format_value(report.get('external_approval_rate'), '%')}\n"
        f"Top reject reasons: {report.get('external_top_reject_reasons') or NA}\n"
        f"Top approved symbols: {report.get('external_top_approved_symbols') or NA}\n\n"
        "Position Manager Outcomes:\n"
        f"HOLD count: {report.get('hold_count', 0)}\n"
        f"OPPOSITE signal count: {report.get('opposite_signal_count', 0)}\n"
        f"EXIT recommendation count: {report.get('exit_recommendation_count', 0)}\n"
        f"Stale position count: {report.get('stale_position_count', 0)}"
        f"{warning}"
    )


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


def send_telegram(message: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_REPORTS_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram skipped: TELEGRAM_BOT_TOKEN or TELEGRAM_REPORTS_CHAT_ID missing")
        return False
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": message},
        timeout=20,
    )
    if response.status_code != 200:
        print(f"Telegram failed: {response.text}")
        return False
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a daily performance report from closed outcomes.")
    parser.add_argument("--date", help="UTC date YYYY-MM-DD. Defaults to latest signal date.")
    parser.add_argument("--send", action="store_true", help="Send the report to Telegram.")
    parser.add_argument("--journal", type=Path, default=JOURNAL, help="Path to signals.csv.")
    parser.add_argument("--history", type=Path, default=HISTORY, help="Path to signals_history.csv.")
    parser.add_argument("--external", type=Path, default=EXTERNAL, help="Path to external_signals.csv.")
    return parser.parse_args()


def main() -> int:
    load_dotenv(BASE_DIR / ".env")
    args = parse_args()
    journal = load_csv_safely(args.journal)
    history = load_csv_safely(args.history)
    external = load_csv_safely(args.external)
    report, tables = build_full_report(journal, history, external, args.date)
    export_v1_outputs(report, tables, LOGS_DIR)
    persist_report(report)
    message = format_report(report)
    print(message)
    if args.send:
        send_telegram(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
