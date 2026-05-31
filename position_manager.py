# -*- coding: utf-8 -*-
"""Position management advisor for open scanner signals.

This module never opens, closes, or modifies exchange positions. It only
detects open-journal conflicts and returns Telegram-ready advisory messages.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from core.analytics_reporting import load_csv_safely


BASE_DIR = Path(__file__).resolve().parent
JOURNAL = BASE_DIR / "logs" / "signals.csv"
POSITION_REVIEW_HOURS = 6


@dataclass
class PositionAdvice:
    action: str
    should_send_signal: bool
    message: str = ""
    reason: str = ""


def normalize_journal(df: pd.DataFrame) -> pd.DataFrame:
    data = df.copy()
    defaults = {
        "timestamp": "",
        "symbol": "",
        "side": "",
        "entry": "",
        "stop_loss": "",
        "tp1": "",
        "tp2": "",
        "result": "OPEN",
        "signal_status": "sent",
    }
    for column, default in defaults.items():
        if column not in data.columns:
            data[column] = default
    data["timestamp"] = pd.to_datetime(data["timestamp"], utc=True, errors="coerce")
    data["symbol"] = data["symbol"].fillna("").astype(str).str.upper()
    data["side"] = data["side"].fillna("").astype(str).str.upper()
    data["result"] = data["result"].fillna("OPEN").replace("", "OPEN").astype(str).str.upper()
    data["signal_status"] = data["signal_status"].fillna("sent").replace("", "sent").astype(str).str.lower()
    for column in ["entry", "stop_loss", "tp1", "tp2"]:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    return data


def open_positions(df: pd.DataFrame) -> pd.DataFrame:
    data = normalize_journal(df)
    if data.empty:
        return data
    return data[(data["signal_status"] == "sent") & (data["result"] == "OPEN")].copy()


def latest_open_positions(df: pd.DataFrame) -> pd.DataFrame:
    positions = open_positions(df)
    if positions.empty:
        return positions
    return positions.sort_values("timestamp", ascending=False).drop_duplicates("symbol", keep="first")


def latest_open_position(df: pd.DataFrame, symbol: str) -> pd.Series | None:
    positions = latest_open_positions(df)
    positions = positions[positions["symbol"] == symbol.upper()].copy()
    if positions.empty:
        return None
    return positions.sort_values("timestamp", ascending=False).iloc[0]


def duration_text(opened_at: Any, now: pd.Timestamp | None = None) -> tuple[str, float]:
    now = now or pd.Timestamp.now(tz="UTC")
    timestamp = pd.to_datetime(opened_at, utc=True, errors="coerce")
    if pd.isna(timestamp):
        return "-", 0.0
    minutes = max(0.0, float((now - timestamp).total_seconds() / 60))
    hours = int(minutes // 60)
    mins = int(minutes % 60)
    if hours:
        return f"{hours}h {mins:02d}m", minutes / 60
    return f"{mins}m", minutes / 60


def estimate_current_pnl(existing: pd.Series, current_price: float | None) -> str:
    if current_price is None or current_price <= 0:
        return "-"
    entry = pd.to_numeric(pd.Series([existing.get("entry")]), errors="coerce").iloc[0]
    if pd.isna(entry) or entry <= 0:
        return "-"
    side = str(existing.get("side", "")).upper()
    pnl = (current_price - entry) / entry * 100 if side == "LONG" else (entry - current_price) / entry * 100
    return f"{pnl:+.2f}%"


def signal_value(signal: Any, name: str, default: Any = "") -> Any:
    if isinstance(signal, dict):
        return signal.get(name, default)
    return getattr(signal, name, default)


def build_hold_message(existing: pd.Series, signal: Any, now: pd.Timestamp | None = None) -> str:
    duration, _ = duration_text(existing.get("timestamp"), now)
    return (
        "POSITION UPDATE / HOLD\n\n"
        f"Symbol: {existing.get('symbol')}\n"
        f"Existing direction: {existing.get('side')}\n"
        f"Old entry: {existing.get('entry')}\n"
        f"Current signal direction: {signal_value(signal, 'direction')}\n"
        f"Open duration: {duration}\n"
        "Recommendation: HOLD / KEEP POSITION\n\n"
        "For educational analysis only. Not financial advice."
    )


def build_opposite_message(existing: pd.Series, signal: Any, now: pd.Timestamp | None = None) -> str:
    duration, _ = duration_text(existing.get("timestamp"), now)
    current_price = signal_value(signal, "entry", None)
    return (
        "OPPOSITE SIGNAL DETECTED\n\n"
        f"Symbol: {existing.get('symbol')}\n"
        f"Current position: {existing.get('side')} @ {existing.get('entry')}\n"
        f"New direction: {signal_value(signal, 'direction')}\n"
        f"Open duration: {duration}\n"
        f"Current PnL: {estimate_current_pnl(existing, current_price)}\n"
        "Recommendation: EXIT / WAIT / REVIEW\n\n"
        "For educational analysis only. Not financial advice."
    )


def build_review_message(existing: pd.Series, now: pd.Timestamp | None = None) -> str:
    duration, _ = duration_text(existing.get("timestamp"), now)
    return (
        "POSITION REVIEW\n\n"
        f"Symbol: {existing.get('symbol')}\n"
        f"Direction: {existing.get('side')}\n"
        f"Entry: {existing.get('entry')}\n"
        f"SL: {existing.get('stop_loss')}\n"
        f"TP1: {existing.get('tp1')}\n"
        f"TP2: {existing.get('tp2')}\n"
        f"Open duration: {duration}\n"
        "Recommendation: REVIEW POSITION MANAGEMENT\n\n"
        "For educational analysis only. Not financial advice."
    )


def evaluate_new_signal(
    signal: Any,
    journal_path: Path = JOURNAL,
    now: pd.Timestamp | None = None,
    review_hours: int = POSITION_REVIEW_HOURS,
) -> PositionAdvice:
    df = load_csv_safely(journal_path)
    if df.empty:
        return PositionAdvice("none", True)
    symbol = str(signal_value(signal, "symbol", "")).upper()
    direction = str(signal_value(signal, "direction", "")).upper()
    existing = latest_open_position(df, symbol)
    if existing is None:
        return PositionAdvice("none", True)

    _, open_hours = duration_text(existing.get("timestamp"), now)
    existing_direction = str(existing.get("side", "")).upper()
    if existing_direction == direction:
        if open_hours >= review_hours:
            return PositionAdvice(
                "position_review",
                False,
                build_review_message(existing, now),
                "position_review_open_over_6h",
            )
        return PositionAdvice(
            "position_hold",
            False,
            build_hold_message(existing, signal, now),
            "same_symbol_same_direction_open",
        )

    return PositionAdvice(
        "opposite_signal",
        False,
        build_opposite_message(existing, signal, now),
        "same_symbol_opposite_direction_open",
    )


def review_open_positions(journal_path: Path = JOURNAL, now: pd.Timestamp | None = None, review_hours: int = POSITION_REVIEW_HOURS) -> list[str]:
    df = load_csv_safely(journal_path)
    messages = []
    for _, row in latest_open_positions(df).iterrows():
        _, open_hours = duration_text(row.get("timestamp"), now)
        if open_hours >= review_hours:
            messages.append(build_review_message(row, now))
    return messages


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review open scanner positions from logs/signals.csv.")
    parser.add_argument("--journal", type=Path, default=JOURNAL)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    messages = review_open_positions(args.journal)
    if not messages:
        print("No position reviews needed.")
        return 0
    for message in messages:
        print(message)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
