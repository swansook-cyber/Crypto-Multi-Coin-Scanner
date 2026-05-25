# -*- coding: utf-8 -*-
"""Track real signal outcomes from Binance Futures candles."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
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

OUTCOME_COLUMNS = {
    "result": "OPEN",
    "hit_target": "",
    "closed_at": "",
    "max_profit_pct": "",
    "max_drawdown_pct": "",
}


@dataclass
class Outcome:
    result: str
    hit_target: str
    closed_at: str
    max_profit_pct: float
    max_drawdown_pct: float


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


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


def ensure_columns(df: pd.DataFrame) -> pd.DataFrame:
    for column, default in OUTCOME_COLUMNS.items():
        if column not in df.columns:
            df[column] = default
        df[column] = df[column].astype("object")
    return df


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


def main() -> int:
    load_dotenv(BASE_DIR / ".env")
    lookahead_hours = env_int("REVIEW_LOOKAHEAD_HOURS", 24)

    if not JOURNAL.exists():
        print("No journal found at logs/signals.csv")
        return 0

    try:
        df = pd.read_csv(JOURNAL)
    except pd.errors.EmptyDataError:
        print("Journal is empty.")
        return 0

    if df.empty:
        print("Journal is empty.")
        return 0

    df = ensure_columns(df)
    session = build_session()

    for index, row in df.iterrows():
        if str(row.get("result", "OPEN")).upper() != "OPEN":
            continue
        try:
            outcome = review_signal(session, row, lookahead_hours)
        except (requests.RequestException, ValueError, KeyError) as exc:
            print(f"Review skipped for {row.get('symbol', 'UNKNOWN')}: {exc}")
            continue

        df.at[index, "result"] = outcome.result
        df.at[index, "hit_target"] = outcome.hit_target
        df.at[index, "closed_at"] = outcome.closed_at
        df.at[index, "max_profit_pct"] = f"{outcome.max_profit_pct:.2f}"
        df.at[index, "max_drawdown_pct"] = f"{outcome.max_drawdown_pct:.2f}"
        time.sleep(0.2)

    df.to_csv(JOURNAL, index=False)
    print_summary(df)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
