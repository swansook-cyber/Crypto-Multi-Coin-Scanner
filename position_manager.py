# -*- coding: utf-8 -*-
"""Position management advisor for open scanner signals.

This module never opens, closes, or modifies exchange positions. It only
detects open-journal conflicts and returns Telegram-ready advisory messages.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from core.analytics_reporting import load_csv_safely


BASE_DIR = Path(__file__).resolve().parent
JOURNAL = BASE_DIR / "logs" / "signals.csv"
POSITION_REVIEW_HOURS = 6
BINANCE_FUTURES_KLINES = "https://fapi.binance.com/fapi/v1/klines"
LOGGER = logging.getLogger("position_manager")


@dataclass
class PositionAdvice:
    action: str
    should_send_signal: bool
    message: str = ""
    reason: str = ""


@dataclass
class PositionSnapshot:
    current_price: float = 0.0
    trend_status: str = "unknown"
    confirmation_15m: str = "unknown"
    volume_status: str = "unknown"
    mfi: float = 0.0
    atr_pct: float = 0.0
    support: float = 0.0
    resistance: float = 0.0
    market_regime: str = "unknown"
    scanner_bias: str = "UNKNOWN"
    available: bool = False


@dataclass
class PositionReview:
    recommendation: str
    confidence: int
    reason: str
    suggested_actions: list[str]
    current_price: float
    current_r: float
    distance_to_tp1_pct: float
    distance_to_tp2_pct: float
    distance_to_sl_pct: float
    unrealized_profit_pct: float
    atr_multiple_from_entry: float
    max_profit_pct: float
    max_drawdown_pct: float
    trend_status: str
    momentum_status: str
    volume_status: str
    time_decay_status: str
    structure_status: str
    market_regime: str
    mfi: float
    atr_pct: float
    support: float
    resistance: float
    opposite_signal_risk: bool


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
        "max_profit_pct": "",
        "max_drawdown_pct": "",
        "position_recommendation": "",
        "current_r": "",
        "distance_to_tp1_pct": "",
        "distance_to_sl_pct": "",
        "position_confidence": "",
        "position_reason": "",
    }
    for column, default in defaults.items():
        if column not in data.columns:
            data[column] = default
    data["timestamp"] = pd.to_datetime(data["timestamp"], utc=True, errors="coerce")
    data["symbol"] = data["symbol"].fillna("").astype(str).str.upper()
    data["side"] = data["side"].fillna("").astype(str).str.upper()
    data["result"] = data["result"].fillna("OPEN").replace("", "OPEN").astype(str).str.upper()
    data["signal_status"] = data["signal_status"].fillna("sent").replace("", "sent").astype(str).str.lower()
    for column in [
        "entry", "stop_loss", "tp1", "tp2", "max_profit_pct", "max_drawdown_pct",
        "current_r", "distance_to_tp1_pct", "distance_to_sl_pct", "position_confidence",
    ]:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    return data


def _safe_float(value: Any, default: float = 0.0) -> float:
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric):
        return default
    return float(numeric)


def _format_price(value: float) -> str:
    if value <= 0:
        return "-"
    if value >= 1000:
        return f"{value:.2f}"
    if value >= 10:
        return f"{value:.3f}"
    if value >= 1:
        return f"{value:.4f}"
    return f"{value:.6f}"


def fetch_klines(symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
    response = requests.get(
        BINANCE_FUTURES_KLINES,
        params={"symbol": symbol.upper(), "interval": interval, "limit": limit},
        timeout=20,
    )
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
    for column in ["open", "high", "low", "close", "volume"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    return df.dropna(subset=["open", "high", "low", "close"])


def add_indicators(df: pd.DataFrame, mfi_period: int = 14) -> pd.DataFrame:
    if df.empty:
        return df
    data = df.copy()
    data["ema20"] = data["close"].ewm(span=20, adjust=False).mean()
    data["ema50"] = data["close"].ewm(span=50, adjust=False).mean()
    prev_close = data["close"].shift(1)
    true_range = pd.concat(
        [
            data["high"] - data["low"],
            (data["high"] - prev_close).abs(),
            (data["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    data["atr14"] = true_range.rolling(14).mean()
    data["atr_pct"] = data["atr14"] / data["close"] * 100
    typical = (data["high"] + data["low"] + data["close"]) / 3
    money_flow = typical * data["volume"]
    direction = typical.diff()
    positive = money_flow.where(direction > 0, 0.0).rolling(mfi_period).sum()
    negative = money_flow.where(direction < 0, 0.0).rolling(mfi_period).sum()
    ratio = positive / negative.replace(0, pd.NA)
    data["mfi"] = (100 - (100 / (1 + ratio))).fillna(50)
    data["volume_sma20"] = data["volume"].rolling(20).mean()
    return data


def fetch_position_snapshot(symbol: str) -> PositionSnapshot:
    try:
        df_1h = add_indicators(fetch_klines(symbol, "1h", 200))
        df_15m = add_indicators(fetch_klines(symbol, "15m", 200))
    except Exception as exc:
        LOGGER.warning("Position snapshot unavailable for %s: %s", symbol, exc)
        return PositionSnapshot()
    if df_1h.empty or df_15m.empty:
        return PositionSnapshot()
    latest_1h = df_1h.iloc[-1]
    latest_15m = df_15m.iloc[-1]
    close = float(latest_1h["close"])
    trend_status = "bullish" if close > latest_1h["ema20"] > latest_1h["ema50"] else "bearish" if close < latest_1h["ema20"] < latest_1h["ema50"] else "mixed"
    confirmation_15m = "bullish" if float(latest_15m["close"]) > float(latest_15m["ema20"]) else "bearish"
    volume_sma = _safe_float(latest_1h.get("volume_sma20"))
    volume = _safe_float(latest_1h.get("volume"))
    volume_ratio = volume / volume_sma if volume_sma > 0 else 0.0
    volume_status = "strong" if volume_ratio >= 1.2 else "normal" if volume_ratio >= 0.8 else "weak"
    atr_pct = _safe_float(latest_1h.get("atr_pct"))
    ema_gap = abs(float(latest_1h["ema20"]) - float(latest_1h["ema50"])) / close * 100 if close else 0.0
    market_regime = "High Volatility" if atr_pct >= 2.5 else "Trending" if ema_gap >= 0.2 and atr_pct >= 0.35 else "Sideway"
    support = float(df_1h["low"].tail(40).min())
    resistance = float(df_1h["high"].tail(40).max())
    scanner_bias = "LONG" if trend_status == "bullish" and confirmation_15m == "bullish" else "SHORT" if trend_status == "bearish" and confirmation_15m == "bearish" else "UNKNOWN"
    return PositionSnapshot(
        current_price=close,
        trend_status=trend_status,
        confirmation_15m=confirmation_15m,
        volume_status=volume_status,
        mfi=_safe_float(latest_1h.get("mfi")),
        atr_pct=atr_pct,
        support=support,
        resistance=resistance,
        market_regime=market_regime,
        scanner_bias=scanner_bias,
        available=True,
    )


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


def position_math(existing: pd.Series, current_price: float) -> tuple[float, float, float, float, float, float]:
    entry = _safe_float(existing.get("entry"))
    sl = _safe_float(existing.get("stop_loss"))
    tp1 = _safe_float(existing.get("tp1"))
    tp2 = _safe_float(existing.get("tp2"))
    side = str(existing.get("side", "")).upper()
    if entry <= 0 or current_price <= 0:
        return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    risk = entry - sl if side == "LONG" else sl - entry
    progress = current_price - entry if side == "LONG" else entry - current_price
    current_r = progress / risk if risk > 0 else 0.0
    distance_to_tp1 = ((tp1 - current_price) / current_price * 100) if side == "LONG" else ((current_price - tp1) / current_price * 100)
    distance_to_tp2 = ((tp2 - current_price) / current_price * 100) if side == "LONG" else ((current_price - tp2) / current_price * 100)
    distance_to_sl = ((current_price - sl) / current_price * 100) if side == "LONG" else ((sl - current_price) / current_price * 100)
    unrealized_profit = (current_price - entry) / entry * 100 if side == "LONG" else (entry - current_price) / entry * 100
    atr_pct = _safe_float(existing.get("atr_pct"), _safe_float(existing.get("atr")))
    atr_multiple = abs(unrealized_profit) / atr_pct if atr_pct > 0 else 0.0
    return float(current_r), float(distance_to_tp1), float(distance_to_tp2), float(distance_to_sl), float(unrealized_profit), float(atr_multiple)


def _direction_matches(side: str, status: str) -> bool:
    side = side.upper()
    if side == "LONG":
        return status == "bullish"
    if side == "SHORT":
        return status == "bearish"
    return False


def analyze_open_position(
    existing: pd.Series,
    now: pd.Timestamp | None = None,
    snapshot: PositionSnapshot | None = None,
    new_direction: str | None = None,
) -> PositionReview:
    side = str(existing.get("side", "")).upper()
    duration, open_hours = duration_text(existing.get("timestamp"), now)
    market = snapshot or fetch_position_snapshot(str(existing.get("symbol", "")))
    current_price = market.current_price or _safe_float(existing.get("entry"))
    current_r, distance_to_tp1, distance_to_tp2, distance_to_sl, unrealized_profit_pct, atr_multiple = position_math(existing, current_price)
    if atr_multiple == 0 and market.atr_pct > 0 and current_price > 0:
        entry = _safe_float(existing.get("entry"))
        move_pct = abs((current_price - entry) / entry * 100) if entry > 0 else 0.0
        atr_multiple = move_pct / market.atr_pct if market.atr_pct > 0 else 0.0
    max_profit_pct = _safe_float(existing.get("max_profit_pct"), max(0.0, current_r))
    max_drawdown_pct = _safe_float(existing.get("max_drawdown_pct"), min(0.0, current_r))

    trend_valid = _direction_matches(side, market.trend_status)
    momentum_valid = _direction_matches(side, market.confirmation_15m)
    mfi_valid = (side == "LONG" and market.mfi >= 55) or (side == "SHORT" and market.mfi <= 45)
    volume_valid = market.volume_status in {"strong", "normal"}
    structure_valid = trend_valid and momentum_valid and distance_to_sl > 0
    opposite_signal_risk = bool(new_direction and new_direction.upper() and new_direction.upper() != side)
    if market.scanner_bias in {"LONG", "SHORT"} and market.scanner_bias != side:
        opposite_signal_risk = True

    time_decay_status = "fresh" if open_hours < 4 else "stale" if open_hours > 10 else "watch"
    near_tp1 = current_r >= 0.75 or (distance_to_tp1 >= 0 and distance_to_tp1 <= max(0.25, market.atr_pct * 0.5))
    if opposite_signal_risk or not structure_valid or distance_to_sl <= 0:
        recommendation = "CLOSE POSITION"
        confidence = 82
        reason = "Structure or opposite-direction risk is against the open position."
        actions = ["do not add size", "review manual exit plan", "wait for a new clean setup"]
    elif open_hours > 10 and (not momentum_valid or not mfi_valid or market.volume_status == "weak"):
        recommendation = "REDUCE EXPOSURE"
        confidence = 76
        reason = "Position is stale and momentum is fading before TP1."
        actions = ["do not add size", "consider reducing exposure", "consider tightening SL", "monitor next 1H candle"]
    elif current_r >= 1.0:
        recommendation = "MOVE SL TO BREAKEVEN"
        confidence = 80
        reason = "Position has reached at least 1R; risk can be reviewed without adding exposure."
        actions = ["do not add size", "consider moving SL toward breakeven", "monitor next 1H candle"]
    elif near_tp1 and (market.volume_status == "weak" or not momentum_valid):
        recommendation = "TAKE PARTIAL PROFIT"
        confidence = 82
        reason = "Price is near TP1 while momentum or volume is weakening."
        actions = ["do not add size", "consider partial profit-taking", "monitor next 1H candle"]
    elif open_hours > 6 and current_r < 1:
        recommendation = "HOLD WITH CAUTION"
        confidence = 68
        reason = "Trade is open longer than expected without TP1 confirmation."
        actions = ["do not add size", "monitor next 1H candle", "wait for new setup"]
    elif open_hours < 4 and structure_valid:
        recommendation = "KEEP POSITION"
        confidence = 74
        reason = "Structure remains valid and position is still within normal review time."
        actions = ["do not add size", "monitor next 1H candle"]
    elif structure_valid and current_r > 0:
        recommendation = "KEEP POSITION"
        confidence = 70
        reason = "Position is positive and structure has not broken."
        actions = ["do not add size", "monitor next 1H candle"]
    else:
        recommendation = "WAIT / NO ACTION"
        confidence = 55
        reason = "Market context is unclear; no aggressive action suggested."
        actions = ["do not add size", "wait for new setup"]

    return PositionReview(
        recommendation=recommendation,
        confidence=confidence,
        reason=reason,
        suggested_actions=actions,
        current_price=current_price,
        current_r=current_r,
        distance_to_tp1_pct=distance_to_tp1,
        distance_to_tp2_pct=distance_to_tp2,
        distance_to_sl_pct=distance_to_sl,
        unrealized_profit_pct=unrealized_profit_pct,
        atr_multiple_from_entry=atr_multiple,
        max_profit_pct=max_profit_pct,
        max_drawdown_pct=max_drawdown_pct,
        trend_status=market.trend_status,
        momentum_status=f"15m {market.confirmation_15m}; MFI {market.mfi:.1f}",
        volume_status=market.volume_status,
        time_decay_status=f"{time_decay_status} ({duration})",
        structure_status="valid" if structure_valid else "broken/unclear",
        market_regime=market.market_regime,
        mfi=market.mfi,
        atr_pct=market.atr_pct,
        support=market.support,
        resistance=market.resistance,
        opposite_signal_risk=opposite_signal_risk,
    )


def signal_value(signal: Any, name: str, default: Any = "") -> Any:
    if isinstance(signal, dict):
        return signal.get(name, default)
    return getattr(signal, name, default)


def build_position_review_message(existing: pd.Series, review: PositionReview, now: pd.Timestamp | None = None) -> str:
    duration, _ = duration_text(existing.get("timestamp"), now)
    actions = "\n".join(f"- {action}" for action in review.suggested_actions)
    return (
        "POSITION REVIEW\n\n"
        f"Symbol: {existing.get('symbol')}\n"
        f"Direction: {existing.get('side')}\n"
        f"Entry: {_format_price(_safe_float(existing.get('entry')))}\n"
        f"Current price: {_format_price(review.current_price)}\n"
        f"SL: {_format_price(_safe_float(existing.get('stop_loss')))}\n"
        f"TP1: {_format_price(_safe_float(existing.get('tp1')))}\n"
        f"TP2: {_format_price(_safe_float(existing.get('tp2')))}\n"
        f"Open duration: {duration}\n"
        f"Current R: {review.current_r:+.2f}R\n"
        f"Distance to TP1: {review.distance_to_tp1_pct:+.2f}%\n"
        f"Distance to TP2: {review.distance_to_tp2_pct:+.2f}%\n"
        f"Distance to SL: {review.distance_to_sl_pct:+.2f}%\n\n"
        f"Unrealized profit: {review.unrealized_profit_pct:+.2f}%\n"
        f"ATR multiple from entry: {review.atr_multiple_from_entry:.2f}x\n\n"
        "AI/System Analysis:\n"
        f"- trend status: {review.trend_status}\n"
        f"- momentum status: {review.momentum_status}\n"
        f"- volume status: {review.volume_status}\n"
        f"- time decay status: {review.time_decay_status}\n"
        f"- structure status: {review.structure_status}\n"
        f"- market regime: {review.market_regime}\n"
        f"- support/resistance: {_format_price(review.support)} / {_format_price(review.resistance)}\n"
        f"- ATR: {review.atr_pct:.2f}%\n"
        f"- opposite signal risk: {'YES' if review.opposite_signal_risk else 'NO'}\n\n"
        f"Recommendation:\n{review.recommendation}\n\n"
        f"Suggested actions:\n{actions}\n\n"
        f"Recommendation Confidence: {review.confidence}%\n"
        f"Reason: {review.reason}\n\n"
        "Educational analysis only. No auto-close. No auto-trade. Not financial advice."
    )


def build_hold_message(existing: pd.Series, signal: Any, now: pd.Timestamp | None = None) -> str:
    duration, _ = duration_text(existing.get("timestamp"), now)
    review = analyze_open_position(existing, now, new_direction=signal_value(signal, "direction"))
    actions = "\n".join(f"- {action}" for action in review.suggested_actions)
    return (
        "POSITION UPDATE / HOLD\n\n"
        f"Symbol: {existing.get('symbol')}\n"
        f"Existing direction: {existing.get('side')}\n"
        f"Old entry: {existing.get('entry')}\n"
        f"Current signal direction: {signal_value(signal, 'direction')}\n"
        f"Open duration: {duration}\n"
        f"Current R: {review.current_r:+.2f}R\n"
        f"Unrealized profit: {review.unrealized_profit_pct:+.2f}%\n"
        f"Recommendation: {review.recommendation}\n\n"
        f"Recommendation Confidence: {review.confidence}%\n\n"
        f"Suggested actions:\n{actions}\n\n"
        "Educational analysis only. No auto-close. No auto-trade. Not financial advice."
    )


def build_opposite_message(existing: pd.Series, signal: Any, now: pd.Timestamp | None = None) -> str:
    duration, _ = duration_text(existing.get("timestamp"), now)
    current_price = signal_value(signal, "entry", None)
    review = analyze_open_position(existing, now, new_direction=signal_value(signal, "direction"))
    actions = "\n".join(f"- {action}" for action in review.suggested_actions)
    return (
        "OPPOSITE SIGNAL DETECTED\n\n"
        f"Symbol: {existing.get('symbol')}\n"
        f"Current position: {existing.get('side')} @ {existing.get('entry')}\n"
        f"New direction: {signal_value(signal, 'direction')}\n"
        f"Open duration: {duration}\n"
        f"Current PnL: {estimate_current_pnl(existing, current_price)}\n"
        f"Current R: {review.current_r:+.2f}R\n"
        f"Recommendation: {review.recommendation}\n\n"
        f"Recommendation Confidence: {review.confidence}%\n\n"
        f"Suggested actions:\n{actions}\n\n"
        "Educational analysis only. No auto-close. No auto-trade. Not financial advice."
    )


def build_review_message(existing: pd.Series, now: pd.Timestamp | None = None) -> str:
    review = analyze_open_position(existing, now)
    return build_position_review_message(existing, review, now)


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
