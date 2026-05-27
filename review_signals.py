# -*- coding: utf-8 -*-
"""Track real signal outcomes from Binance Futures candles."""

from __future__ import annotations

import os
import time
import argparse
import sys
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_DIR = Path(__file__).resolve().parent
JOURNAL = BASE_DIR / "logs" / "signals.csv"
BINANCE_FUTURES_KLINES = "https://fapi.binance.com/fapi/v1/klines"
ERROR_RETRY_SECONDS = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOGGER = logging.getLogger("outcome_checker")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUTCOME_COLUMNS = {
    "result": "OPEN",
    "hit_target": "",
    "closed_at": "",
    "max_profit_pct": "",
    "max_drawdown_pct": "",
    "outcome_alert_sent": 0,
    "outcome_alert_at": "",
    "outcome_id": "",
}

PROCESSED_OUTCOMES: set[str] = set()


@dataclass
class Outcome:
    result: str
    hit_target: str
    closed_at: str
    max_profit_pct: float
    max_drawdown_pct: float


@dataclass
class ReviewStats:
    open_trades: int = 0
    tp_hits: int = 0
    sl_hits: int = 0
    skipped_alerts: int = 0
    sent_alerts: int = 0
    errors: int = 0


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def build_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def display_symbol(symbol: str) -> str:
    cleaned = str(symbol).strip().upper().replace("BINANCE:", "").replace(".P", "")
    return f"{cleaned}.P"


def format_price(value: Any) -> str:
    price = float(value)
    if price >= 1000:
        return f"{price:.2f}"
    if price >= 10:
        return f"{price:.3f}"
    if price >= 1:
        return f"{price:.4f}"
    return f"{price:.6f}"


def format_period(start_iso: Any, end_iso: Any) -> str:
    start = pd.to_datetime(start_iso, utc=True, errors="coerce")
    end = pd.to_datetime(end_iso, utc=True, errors="coerce")
    if pd.isna(start) or pd.isna(end):
        return "-"
    total_minutes = max(0, int((end - start).total_seconds() // 60))
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours and minutes:
        return f"{hours} hr {minutes} min"
    if hours:
        return f"{hours} hr"
    return f"{minutes} min"


def ensure_columns(df: pd.DataFrame) -> pd.DataFrame:
    for column, default in OUTCOME_COLUMNS.items():
        if column not in df.columns:
            df[column] = default
        df[column] = df[column].astype("object")
    return df


def reload_journal() -> pd.DataFrame | None:
    if not JOURNAL.exists():
        LOGGER.info("No journal found at logs/signals.csv")
        return None
    try:
        df = pd.read_csv(JOURNAL)
    except pd.errors.EmptyDataError:
        LOGGER.info("Journal is empty.")
        return None
    if df.empty:
        LOGGER.info("Journal is empty.")
        return None
    df = ensure_columns(df)
    persisted_ids = {
        str(value).strip()
        for value in df["outcome_id"].dropna()
        if has_outcome_id(value)
    }
    PROCESSED_OUTCOMES.update(persisted_ids)
    LOGGER.info("Reloaded journal: %s rows, %s persisted outcome ids", len(df), len(persisted_ids))
    return df


def persist_journal(df: pd.DataFrame) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("w", newline="", encoding="utf-8") as handle:
        df.to_csv(handle, index=False)
        handle.flush()
        os.fsync(handle.fileno())
    LOGGER.info("CSV persisted: %s", JOURNAL)


def alert_already_sent(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def has_outcome_id(value: Any) -> bool:
    if value is None or pd.isna(value):
        return False
    return bool(str(value).strip())


def build_outcome_id(row: pd.Series) -> str:
    symbol = str(row.get("symbol", "")).strip().upper()
    side = str(row.get("side", "")).strip().upper()
    entry = str(row.get("entry", "")).strip()
    result = str(row.get("result", "")).strip().upper()
    closed_at = str(row.get("closed_at", "")).strip()
    return f"{symbol}|{side}|{entry}|{result}|{closed_at}"


def build_outcome_alert(row: pd.Series) -> str:
    side = str(row["side"]).upper()
    result = str(row["result"]).upper()
    hit_target = str(row.get("hit_target", "")).upper()
    entry = float(row["entry"])
    stop_loss = float(row["stop_loss"])
    tp1 = float(row["tp1"])
    tp2 = float(row["tp2"])
    period = format_period(row.get("timestamp"), row.get("closed_at"))

    if result == "WIN":
        target_price = tp2 if hit_target == "TP2" else tp1
        profit_pct = (target_price - entry) / entry * 100 if side == "LONG" else (entry - target_price) / entry * 100
        return (
            "✅ TAKE PROFIT HIT\n\n"
            f"🪙 {display_symbol(row['symbol'])}\n"
            f"📈 Direction: {side}\n"
            f"🎯 Hit: {hit_target}\n"
            f"💰 Entry: {format_price(entry)}\n"
            f"🎯 TP1: {format_price(tp1)}\n"
            f"🎯 TP2: {format_price(tp2)}\n"
            f"🛑 SL: {format_price(stop_loss)}\n"
            f"📊 Profit: +{profit_pct:.2f}%\n"
            f"⏱ Period: {period}"
        )

    loss_pct = (entry - stop_loss) / entry * 100 if side == "LONG" else (stop_loss - entry) / entry * 100
    return (
        "🛑 STOP LOSS HIT\n\n"
        f"🪙 {display_symbol(row['symbol'])}\n"
        f"📈 Direction: {side}\n"
        f"💰 Entry: {format_price(entry)}\n"
        f"🛑 SL: {format_price(stop_loss)}\n"
        f"📉 Loss: -{abs(loss_pct):.2f}%\n"
        f"⏱ Period: {period}"
    )


def send_telegram_alert(session: requests.Session, message: str) -> bool:
    if not env_bool("SEND_TELEGRAM", True) or not env_bool("SEND_OUTCOME_ALERTS", True):
        return False
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        LOGGER.warning("Outcome alert skipped: Telegram token/chat id missing")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        response = session.post(url, data={"chat_id": chat_id, "text": message}, timeout=20)
    except requests.RequestException as exc:
        LOGGER.error("Outcome alert failed: %s", exc)
        return False
    if response.status_code != 200:
        LOGGER.error("Outcome alert failed: %s", response.text)
        return False
    return True


def fetch_klines(session: requests.Session, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    params = {
        "symbol": symbol,
        "interval": "15m",
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": 1000,
    }
    response = session.get(BINANCE_FUTURES_KLINES, params=params, timeout=20)
    response.raise_for_status()
    data = response.json()
    if not data:
        return pd.DataFrame()

    df = pd.DataFrame(
        data,
        columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_asset_volume", "num_trades",
            "taker_buy_base_volume", "taker_buy_quote_volume", "ignore",
        ],
    )
    for column in ["open", "high", "low", "close"]:
        df[column] = df[column].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    return df


def calculate_extremes(side: str, entry: float, candles: pd.DataFrame) -> tuple[float, float]:
    if candles.empty or entry <= 0:
        return 0.0, 0.0
    if side == "LONG":
        max_profit = (candles["high"].max() - entry) / entry * 100
        max_drawdown = (candles["low"].min() - entry) / entry * 100
    else:
        max_profit = (entry - candles["low"].min()) / entry * 100
        max_drawdown = (entry - candles["high"].max()) / entry * 100
    return float(max_profit), float(max_drawdown)


def evaluate_outcome(row: pd.Series, candles: pd.DataFrame) -> Outcome:
    side = str(row["side"]).upper()
    entry = float(row["entry"])
    stop_loss = float(row["stop_loss"])
    tp1 = float(row["tp1"])
    tp2 = float(row["tp2"])
    max_profit_pct, max_drawdown_pct = calculate_extremes(side, entry, candles)

    if candles.empty:
        return Outcome("OPEN", "", "", max_profit_pct, max_drawdown_pct)

    for _, candle in candles.iterrows():
        high = float(candle["high"])
        low = float(candle["low"])
        closed_at = candle["close_time"].isoformat()

        if side == "LONG":
            sl_hit = low <= stop_loss
            tp2_hit = high >= tp2
            tp1_hit = high >= tp1
        else:
            sl_hit = high >= stop_loss
            tp2_hit = low <= tp2
            tp1_hit = low <= tp1

        # Conservative mode: if SL and TP happen in the same candle, SL wins.
        if sl_hit:
            return Outcome("LOSS", "SL", closed_at, max_profit_pct, max_drawdown_pct)
        if tp2_hit:
            return Outcome("WIN", "TP2", closed_at, max_profit_pct, max_drawdown_pct)
        if tp1_hit:
            return Outcome("WIN", "TP1", closed_at, max_profit_pct, max_drawdown_pct)

    return Outcome("OPEN", "", "", max_profit_pct, max_drawdown_pct)


def review_signal(session: requests.Session, row: pd.Series, lookahead_hours: int) -> Outcome:
    timestamp = pd.to_datetime(row["timestamp"], utc=True, errors="coerce")
    if pd.isna(timestamp):
        return Outcome("OPEN", "", "", 0.0, 0.0)

    start_ms = int(timestamp.timestamp() * 1000)
    end_ts = min(timestamp + pd.Timedelta(hours=lookahead_hours), pd.Timestamp.now(tz="UTC"))
    end_ms = int(end_ts.timestamp() * 1000)
    candles = fetch_klines(session, str(row["symbol"]).upper(), start_ms, end_ms)
    return evaluate_outcome(row, candles)


def print_summary(df: pd.DataFrame) -> None:
    total = len(df)
    wins = int((df["result"] == "WIN").sum())
    losses = int((df["result"] == "LOSS").sum())
    open_trades = int((df["result"] == "OPEN").sum())
    closed = wins + losses
    win_rate = wins / closed * 100 if closed else 0.0
    rr = pd.to_numeric(df["risk_reward"], errors="coerce")

    symbol_win_rates: dict[str, float] = {}
    for symbol, group in df[df["result"].isin(["WIN", "LOSS"])].groupby("symbol"):
        symbol_win_rates[symbol] = float((group["result"] == "WIN").mean() * 100)
    best_symbol = max(symbol_win_rates, key=symbol_win_rates.get) if symbol_win_rates else "-"
    worst_symbol = min(symbol_win_rates, key=symbol_win_rates.get) if symbol_win_rates else "-"

    print("Crypto Multi-Coin Scanner Outcome Review")
    print("----------------------------------------")
    print(f"Total signals: {total}")
    print(f"Wins: {wins}")
    print(f"Losses: {losses}")
    print(f"Open trades: {open_trades}")
    print(f"Win rate: {win_rate:.1f}%")
    print(f"Avg RR: {rr.mean():.2f}")
    print(f"Best symbol: {best_symbol}")
    print(f"Worst symbol: {worst_symbol}")
    print()
    print("Summary table")
    print("-------------")
    for _, row in df.tail(50).iterrows():
        target = row["hit_target"] if isinstance(row["hit_target"], str) and row["hit_target"] else ""
        print(f"{row['symbol']} {row['side']} {row['result']} {target}".rstrip())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review signal outcomes from Binance Futures candles.")
    parser.add_argument("--notify", action="store_true", help="Send Telegram alerts for newly closed outcomes.")
    return parser.parse_args()


def run_review_cycle(notify: bool, session: requests.Session, lookahead_hours: int, print_report: bool = True) -> ReviewStats:
    stats = ReviewStats()
    df = reload_journal()
    if df is None:
        return stats

    open_mask = df["result"].astype(str).str.upper() == "OPEN"
    stats.open_trades = int(open_mask.sum())

    for index, row in df.iterrows():
        previous_result = str(row.get("result", "OPEN")).upper()
        if previous_result == "OPEN":
            if alert_already_sent(row.get("outcome_alert_sent", 0)) or has_outcome_id(row.get("outcome_id", "")):
                stats.skipped_alerts += 1
                LOGGER.info("Outcome duplicate skipped before review: %s", row.get("outcome_id", ""))
                continue
            try:
                outcome = review_signal(session, row, lookahead_hours)
            except (requests.RequestException, ValueError, KeyError) as exc:
                stats.errors += 1
                LOGGER.error("Review skipped for %s: %s", row.get("symbol", "UNKNOWN"), exc)
                continue

            df.at[index, "result"] = outcome.result
            df.at[index, "hit_target"] = outcome.hit_target
            df.at[index, "closed_at"] = outcome.closed_at
            df.at[index, "max_profit_pct"] = f"{outcome.max_profit_pct:.2f}"
            df.at[index, "max_drawdown_pct"] = f"{outcome.max_drawdown_pct:.2f}"
            if outcome.result == "WIN":
                stats.tp_hits += 1
            elif outcome.result == "LOSS":
                stats.sl_hits += 1
        elif previous_result not in {"WIN", "LOSS"}:
            continue

        updated_row = df.loc[index]
        updated_result = str(updated_row.get("result", "")).upper()
        if updated_result not in {"WIN", "LOSS"}:
            time.sleep(0.2)
            continue

        outcome_id = build_outcome_id(updated_row)
        if alert_already_sent(updated_row.get("outcome_alert_sent", 0)) or has_outcome_id(updated_row.get("outcome_id", "")):
            stats.skipped_alerts += 1
            LOGGER.info("Outcome duplicate skipped: %s", updated_row.get("outcome_id", outcome_id))
            time.sleep(0.2)
            continue
        if outcome_id in PROCESSED_OUTCOMES:
            stats.skipped_alerts += 1
            LOGGER.info("Outcome duplicate skipped: %s", outcome_id)
            time.sleep(0.2)
            continue
        if not notify:
            stats.skipped_alerts += 1
            time.sleep(0.2)
            continue

        LOGGER.info("Processing outcome_id: %s", outcome_id)
        sent = send_telegram_alert(session, build_outcome_alert(updated_row))
        if sent:
            stats.sent_alerts += 1
            PROCESSED_OUTCOMES.add(outcome_id)
            df.at[index, "outcome_alert_sent"] = 1
            df.at[index, "outcome_alert_at"] = datetime.now(timezone.utc).isoformat()
            df.at[index, "outcome_id"] = outcome_id
            persist_journal(df)
        else:
            stats.skipped_alerts += 1
        time.sleep(0.2)

    persist_journal(df)
    if print_report:
        print_summary(df)
    return stats


def run_loop(notify: bool, lookahead_hours: int, interval_seconds: int) -> int:
    LOGGER.info("Outcome checker started")
    while True:
        session = build_session()
        try:
            stats = run_review_cycle(notify=notify, session=session, lookahead_hours=lookahead_hours, print_report=False)
            LOGGER.info(
                "Open trades: %s | TP hits: %s | SL hits: %s | skipped alerts: %s | sent alerts: %s | errors: %s",
                stats.open_trades,
                stats.tp_hits,
                stats.sl_hits,
                stats.skipped_alerts,
                stats.sent_alerts,
                stats.errors,
            )
            LOGGER.info("Next outcome check in %s seconds", interval_seconds)
            time.sleep(interval_seconds)
        except Exception as exc:
            LOGGER.exception("Outcome checker loop error: %s", exc)
            LOGGER.info("Retrying outcome checker in %s seconds", ERROR_RETRY_SECONDS)
            time.sleep(ERROR_RETRY_SECONDS)


def main() -> int:
    args = parse_args()
    load_dotenv(BASE_DIR / ".env")
    lookahead_hours = env_int("REVIEW_LOOKAHEAD_HOURS", 24)
    interval_seconds = env_int(
        "OUTCOME_LOOP_INTERVAL_SECONDS",
        env_int("OUTCOME_CHECK_INTERVAL_MINUTES", 15) * 60,
    )
    loop_mode = env_bool("OUTCOME_LOOP_MODE", False)

    if loop_mode:
        return run_loop(notify=True, lookahead_hours=lookahead_hours, interval_seconds=interval_seconds)

    session = build_session()
    run_review_cycle(notify=args.notify, session=session, lookahead_hours=lookahead_hours, print_report=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
