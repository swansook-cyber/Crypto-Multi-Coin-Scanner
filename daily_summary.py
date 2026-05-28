# -*- coding: utf-8 -*-
"""Daily journal summary for Crypto Multi-Coin Scanner."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
JOURNAL = BASE_DIR / "logs" / "signals.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOGGER = logging.getLogger("daily_summary")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


REQUIRED_COLUMNS = {
    "timestamp": "",
    "symbol": "",
    "result": "OPEN",
    "hit_target": "",
    "closed_at": "",
    "market_session": "",
    "score_bucket": "",
    "risk_reward": "",
    "setup_strength": "",
    "confidence": "",
}


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def ensure_columns(df: pd.DataFrame) -> pd.DataFrame:
    for column, default in REQUIRED_COLUMNS.items():
        if column not in df.columns:
            df[column] = default
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df["closed_at"] = pd.to_datetime(df["closed_at"], utc=True, errors="coerce")
    df["result"] = df["result"].fillna("OPEN").astype(str).str.upper()
    df["hit_target"] = df["hit_target"].fillna("").astype(str).str.upper()
    df["symbol"] = df["symbol"].fillna("").astype(str).str.upper()
    df["market_session"] = df["market_session"].fillna("Other").replace("", "Other")
    df["score_bucket"] = df["score_bucket"].fillna("-").replace("", "-")
    return df


def load_journal(path: Path = JOURNAL) -> pd.DataFrame:
    if not path.exists():
        LOGGER.warning("Journal not found: %s", path)
        return ensure_columns(pd.DataFrame())
    try:
        df = pd.read_csv(path)
    except pd.errors.EmptyDataError:
        LOGGER.warning("Journal is empty: %s", path)
        return ensure_columns(pd.DataFrame())
    return ensure_columns(df)


def latest_journal_day(df: pd.DataFrame) -> str:
    if df.empty or df["timestamp"].dropna().empty:
        return pd.Timestamp.now(tz=timezone.utc).strftime("%Y-%m-%d")
    return df["timestamp"].dropna().max().strftime("%Y-%m-%d")


def format_hold_time(minutes: float) -> str:
    if pd.isna(minutes) or minutes <= 0:
        return "-"
    total = int(minutes)
    hours = total // 60
    mins = total % 60
    if hours and mins:
        return f"{hours}h {mins:02d}m"
    if hours:
        return f"{hours}h 00m"
    return f"{mins}m"


def best_group(df: pd.DataFrame, column: str) -> str:
    closed = df[df["result"].isin(["WIN", "LOSS"])].copy()
    if closed.empty or column not in closed.columns:
        return "-"
    scores: dict[str, float] = {}
    for key, group in closed.groupby(closed[column].fillna("-").astype(str)):
        if not key or key == "-":
            continue
        scores[key] = float((group["result"] == "WIN").mean() * 100)
    return max(scores, key=scores.get) if scores else "-"


def worst_group(df: pd.DataFrame, column: str) -> str:
    closed = df[df["result"].isin(["WIN", "LOSS"])].copy()
    if closed.empty or column not in closed.columns:
        return "-"
    scores: dict[str, float] = {}
    for key, group in closed.groupby(closed[column].fillna("-").astype(str)):
        if not key or key == "-":
            continue
        scores[key] = float((group["result"] == "WIN").mean() * 100)
    return min(scores, key=scores.get) if scores else "-"


def build_daily_summary(df: pd.DataFrame, date: str | None = None) -> dict[str, Any]:
    if date is None:
        date = latest_journal_day(df)
    if df.empty:
        day_df = df.copy()
    else:
        day_df = df[df["timestamp"].dt.strftime("%Y-%m-%d") == date].copy()

    total = int(len(day_df))
    tp1 = int(((day_df["result"] == "WIN") & (day_df["hit_target"] == "TP1")).sum()) if total else 0
    tp2 = int(((day_df["result"] == "WIN") & (day_df["hit_target"] == "TP2")).sum()) if total else 0
    sl = int((day_df["result"] == "LOSS").sum()) if total else 0
    pending = int((day_df["result"] == "OPEN").sum()) if total else 0
    holding_minutes = (day_df["closed_at"] - day_df["timestamp"]).dt.total_seconds().div(60)
    avg_holding = format_hold_time(float(holding_minutes.dropna().mean())) if not holding_minutes.dropna().empty else "-"

    return {
        "date": date,
        "total_signals": total,
        "tp1_hits": tp1,
        "tp2_hits": tp2,
        "sl_hits": sl,
        "pending": pending,
        "tp1_rate": tp1 / total * 100 if total else 0.0,
        "tp2_rate": tp2 / total * 100 if total else 0.0,
        "sl_rate": sl / total * 100 if total else 0.0,
        "best_symbol": best_group(day_df, "symbol"),
        "worst_symbol": worst_group(day_df, "symbol"),
        "best_session": best_group(day_df, "market_session"),
        "best_score_bucket": best_group(day_df, "score_bucket"),
        "avg_holding_time": avg_holding,
    }


def build_telegram_message(summary: dict[str, Any]) -> str:
    return (
        "📊 Daily Signal Summary\n"
        f"Date: {summary['date']}\n\n"
        f"Signals: {summary['total_signals']}\n"
        f"✅ TP1: {summary['tp1_hits']}\n"
        f"🏆 TP2: {summary['tp2_hits']}\n"
        f"❌ SL: {summary['sl_hits']}\n"
        f"⏳ Pending: {summary['pending']}\n\n"
        f"Best Symbol: {summary['best_symbol']}\n"
        f"Worst Symbol: {summary['worst_symbol']}\n"
        f"Best Session: {summary['best_session']}\n"
        f"Best Bucket: {summary['best_score_bucket']}\n"
        f"Avg Hold: {summary['avg_holding_time']}\n\n"
        "⚠️ Research tracking only. Past performance does not guarantee future results."
    )


def print_summary(summary: dict[str, Any]) -> None:
    print(build_telegram_message(summary))
    print()
    print(f"TP1 rate: {summary['tp1_rate']:.1f}%")
    print(f"TP2 rate: {summary['tp2_rate']:.1f}%")
    print(f"SL rate: {summary['sl_rate']:.1f}%")


def send_telegram(message: str) -> bool:
    if not env_bool("SEND_DAILY_SUMMARY", False):
        LOGGER.info("Daily summary Telegram skipped: SEND_DAILY_SUMMARY is off")
        return False
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        LOGGER.warning("Daily summary Telegram skipped: token/chat id missing")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        response = requests.post(url, data={"chat_id": chat_id, "text": message}, timeout=20)
    except requests.RequestException as exc:
        LOGGER.error("Daily summary Telegram failed: %s", exc)
        return False
    if response.status_code != 200:
        LOGGER.error("Daily summary Telegram failed: %s", response.text)
        return False
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send or print daily Crypto Scanner summary.")
    parser.add_argument("--date", help="UTC journal date to summarize, YYYY-MM-DD. Defaults to latest journal day.")
    parser.add_argument("--dry-run", action="store_true", help="Print summary without sending Telegram.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv(BASE_DIR / ".env")
    df = load_journal()
    summary = build_daily_summary(df, args.date)
    message = build_telegram_message(summary)
    print_summary(summary)
    if not args.dry_run:
        send_telegram(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
