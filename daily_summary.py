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

from core.analytics_reporting import build_daily_performance_report, export_journal_csvs
from core.equity_tracker import equity_curve_status
from core.performance_stats import load_csv as load_performance_csv
from core.performance_stats import normalize as normalize_performance
from core.performance_stats import rejection_counts, summary as performance_summary


BASE_DIR = Path(__file__).resolve().parent
JOURNAL = BASE_DIR / "logs" / "signals.csv"
HISTORY = BASE_DIR / "logs" / "signals_history.csv"
REJECTED = BASE_DIR / "logs" / "rejected_signals.csv"
EQUITY = BASE_DIR / "logs" / "equity_curve.csv"
DAILY_SUMMARY = BASE_DIR / "logs" / "daily_summary.csv"
JOURNAL_EXPORT_DIR = BASE_DIR / "journal"

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
    "outcome": "",
    "closed_at": "",
    "market_session": "",
    "session": "",
    "watchlist_tier": "",
    "tier": "",
    "score_bucket": "",
    "risk_reward": "",
    "real_rr": "",
    "pnl_percent": "",
    "holding_minutes": "",
    "setup_strength": "",
    "confidence": "",
    "btc_regime": "",
    "wave_score": "",
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
    if df["outcome"].fillna("").astype(str).str.strip().eq("").all():
        result = df["result"].fillna("").astype(str).str.upper()
        target = df["hit_target"].fillna("").astype(str).str.upper().replace("", "TP1")
        df["outcome"] = result
        df.loc[result.eq("WIN"), "outcome"] = "WIN_" + target[result.eq("WIN")]
        df.loc[result.eq("LOSS"), "outcome"] = "LOSS"
    df["outcome"] = df["outcome"].fillna("").astype(str).str.upper()
    df["symbol"] = df["symbol"].fillna("").astype(str).str.upper()
    if df["market_session"].fillna("").astype(str).str.strip().eq("").all() and "session" in df.columns:
        df["market_session"] = df["session"]
    if df["watchlist_tier"].fillna("").astype(str).str.strip().eq("").all() and "tier" in df.columns:
        df["watchlist_tier"] = df["tier"]
    df["market_session"] = df["market_session"].fillna("Other").replace("", "Other")
    df["watchlist_tier"] = df["watchlist_tier"].fillna("-").replace("", "-").astype(str).str.upper()
    df["score_bucket"] = df["score_bucket"].fillna("-").replace("", "-")
    df["real_rr"] = pd.to_numeric(df["real_rr"], errors="coerce")
    df["pnl_percent"] = pd.to_numeric(df["pnl_percent"], errors="coerce")
    df["wave_score"] = pd.to_numeric(df["wave_score"], errors="coerce")
    df["btc_regime"] = df["btc_regime"].fillna("unclear").replace("", "unclear").astype(str).str.lower()
    return df


def load_journal(path: Path = JOURNAL) -> pd.DataFrame:
    if path == JOURNAL and HISTORY.exists():
        path = HISTORY
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


def current_streak(df: pd.DataFrame) -> str:
    closed = df[df["result"].isin(["WIN", "LOSS"])].copy()
    if closed.empty:
        return "-"
    sort_column = "closed_at" if closed["closed_at"].notna().any() else "timestamp"
    closed = closed.sort_values(sort_column)
    last_result = str(closed.iloc[-1]["result"]).upper()
    count = 0
    for result in reversed(closed["result"].astype(str).str.upper().tolist()):
        if result != last_result:
            break
        count += 1
    return f"{count} {last_result}"


def build_daily_summary(df: pd.DataFrame, date: str | None = None) -> dict[str, Any]:
    if date is None:
        date = latest_journal_day(df)
    if df.empty:
        day_df = df.copy()
    else:
        day_df = df[df["timestamp"].dt.strftime("%Y-%m-%d") == date].copy()

    total = int(len(day_df))
    tp1 = int((day_df["outcome"] == "WIN_TP1").sum()) if total else 0
    tp2 = int((day_df["outcome"] == "WIN_TP2").sum()) if total else 0
    sl = int((day_df["outcome"] == "LOSS").sum()) if total else 0
    pending = int((day_df["result"] == "OPEN").sum()) if total else 0
    closed_count = tp1 + tp2 + sl
    holding_minutes = pd.to_numeric(day_df["holding_minutes"], errors="coerce")
    if holding_minutes.dropna().empty:
        holding_minutes = (day_df["closed_at"] - day_df["timestamp"]).dt.total_seconds().div(60)
    avg_holding = format_hold_time(float(holding_minutes.dropna().mean())) if not holding_minutes.dropna().empty else "-"

    rejected = load_performance_csv(REJECTED)
    rejection_table = rejection_counts(rejected).head(7)
    equity = load_performance_csv(EQUITY)
    perf = performance_summary(normalize_performance(df.copy()))

    analytics = build_daily_performance_report(day_df, date)
    return {
        "date": date,
        "total_signals": total,
        "wins": tp1 + tp2,
        "losses": sl,
        "tp1_hits": tp1,
        "tp2_hits": tp2,
        "sl_hits": sl,
        "pending": pending,
        "win_rate": (tp1 + tp2) / closed_count * 100 if closed_count else 0.0,
        "net_rr": day_df["real_rr"].fillna(0).sum() if "real_rr" in day_df else 0.0,
        "max_drawdown": perf["max_drawdown"],
        "equity_change_pct": day_df["pnl_percent"].fillna(0).sum() if "pnl_percent" in day_df else 0.0,
        "equity_status": equity_curve_status(equity),
        "tp1_rate": tp1 / total * 100 if total else 0.0,
        "tp2_rate": tp2 / total * 100 if total else 0.0,
        "sl_rate": sl / total * 100 if total else 0.0,
        "best_symbol": best_group(day_df, "symbol"),
        "worst_symbol": worst_group(day_df, "symbol"),
        "best_coin": analytics["best_coin"],
        "worst_coin": analytics["worst_coin"],
        "btc_regime_breakdown": analytics["btc_regime_breakdown"],
        "wave_score_breakdown": analytics["wave_score_breakdown"],
        "best_session": best_group(day_df, "market_session"),
        "best_score_bucket": best_group(day_df, "score_bucket"),
        "current_streak": current_streak(df),
        "top_performing_tier": best_group(df, "watchlist_tier"),
        "avg_holding_time": avg_holding,
        "top_rejections": rejection_table.to_dict("records"),
    }


def build_telegram_message(summary: dict[str, Any]) -> str:
    return (
        "📊 Daily Signal Summary\n"
        f"Date: {summary['date']}\n\n"
        f"Signals: {summary['total_signals']}\n"
        f"Wins: {summary['wins']}\n"
        f"Losses: {summary['losses']}\n"
        f"Today's Winrate: {summary['win_rate']:.1f}%\n"
        f"Net RR: {summary['net_rr']:.2f}\n"
        f"Max Drawdown: {summary['max_drawdown']:.2f}R\n"
        f"✅ TP1: {summary['tp1_hits']}\n"
        f"🏆 TP2: {summary['tp2_hits']}\n"
        f"❌ SL: {summary['sl_hits']}\n"
        f"⏳ Pending: {summary['pending']}\n\n"
        f"Best Coin: {summary.get('best_coin', summary['best_symbol'])}\n"
        f"Worst Coin: {summary.get('worst_coin', summary['worst_symbol'])}\n"
        f"BTC Regime: {summary.get('btc_regime_breakdown', '-')}\n"
        f"Wave Score: {summary.get('wave_score_breakdown', '-')}\n"
        f"Current Streak: {summary['current_streak']}\n"
        f"Top Tier: {summary['top_performing_tier']}\n"
        f"Best Session: {summary['best_session']}\n"
        f"Best Bucket: {summary['best_score_bucket']}\n"
        f"Avg Holding Time: {summary['avg_holding_time']}\n"
        f"Equity Change: {summary['equity_change_pct']:.2f}%\n"
        f"Equity Curve Status: {summary['equity_status']}\n"
        f"Top Rejections: {format_rejections(summary['top_rejections'])}\n"
        f"Avg Hold: {summary['avg_holding_time']}\n\n"
        "⚠️ Research tracking only. Past performance does not guarantee future results."
    )


def format_rejections(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "-"
    return ", ".join(f"{row.get('reason', '-')}: {row.get('count', 0)}" for row in rows[:5])


def print_summary(summary: dict[str, Any]) -> None:
    print(build_telegram_message(summary))
    print()
    print(f"TP1 rate: {summary['tp1_rate']:.1f}%")
    print(f"TP2 rate: {summary['tp2_rate']:.1f}%")
    print(f"SL rate: {summary['sl_rate']:.1f}%")


def persist_daily_summary(summary: dict[str, Any], path: Path = DAILY_SUMMARY) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "date": summary["date"],
        "signals": summary["total_signals"],
        "wins": summary["wins"],
        "losses": summary["losses"],
        "win_rate": f"{summary['win_rate']:.2f}",
        "net_rr": f"{summary['net_rr']:.4f}",
        "max_drawdown": f"{summary['max_drawdown']:.4f}",
        "current_streak": summary["current_streak"],
        "best_session": summary["best_session"],
        "worst_symbol": summary["worst_symbol"],
        "btc_regime_breakdown": summary.get("btc_regime_breakdown", "-"),
        "wave_score_breakdown": summary.get("wave_score_breakdown", "-"),
        "avg_holding_time": summary["avg_holding_time"],
        "equity_change_pct": f"{summary['equity_change_pct']:.4f}",
        "equity_status": summary["equity_status"],
    }
    if path.exists():
        try:
            df = pd.read_csv(path)
        except pd.errors.EmptyDataError:
            df = pd.DataFrame()
    else:
        df = pd.DataFrame()
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    df = df.drop_duplicates("date", keep="last")
    df.to_csv(path, index=False)


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
    persist_daily_summary(summary)
    export_journal_csvs(df, JOURNAL_EXPORT_DIR, summary)
    print_summary(summary)
    if not args.dry_run:
        send_telegram(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
