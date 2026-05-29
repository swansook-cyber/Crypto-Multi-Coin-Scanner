# -*- coding: utf-8 -*-
"""Lightweight Elliott-style wave/market-structure scoring.

This is a context layer only. It does not produce entries, exits, or trade
instructions by itself.
"""

from __future__ import annotations

from typing import Any

import pandas as pd


def detect_swing_highs_lows(candles: pd.DataFrame, lookback: int = 3) -> list[dict[str, Any]]:
    if candles is None or candles.empty or len(candles) < lookback * 2 + 3:
        return []
    swings: list[dict[str, Any]] = []
    df = candles.reset_index(drop=True)
    for index in range(lookback, len(df) - lookback):
        window = df.iloc[index - lookback:index + lookback + 1]
        high = float(df.loc[index, "high"])
        low = float(df.loc[index, "low"])
        ts = df.loc[index, "close_time"] if "close_time" in df.columns else index
        if high == float(window["high"].max()) and high > float(window.drop(index, errors="ignore")["high"].max()):
            swings.append({"index": index, "type": "high", "price": high, "timestamp": ts})
        if low == float(window["low"].min()) and low < float(window.drop(index, errors="ignore")["low"].min()):
            swings.append({"index": index, "type": "low", "price": low, "timestamp": ts})
    return sorted(swings, key=lambda item: item["index"])


def detect_market_structure(swings: list[dict[str, Any]]) -> dict[str, Any]:
    highs = [item for item in swings if item["type"] == "high"]
    lows = [item for item in swings if item["type"] == "low"]
    higher_high = len(highs) >= 2 and highs[-1]["price"] > highs[-2]["price"]
    lower_high = len(highs) >= 2 and highs[-1]["price"] < highs[-2]["price"]
    higher_low = len(lows) >= 2 and lows[-1]["price"] > lows[-2]["price"]
    lower_low = len(lows) >= 2 and lows[-1]["price"] < lows[-2]["price"]
    bullish = bool(higher_high and higher_low)
    bearish = bool(lower_high and lower_low)
    range_structure = bool(not bullish and not bearish and len(highs) >= 2 and len(lows) >= 2)
    if bullish:
        structure = "bullish"
    elif bearish:
        structure = "bearish"
    elif range_structure:
        structure = "range"
    else:
        structure = "unclear"
    return {
        "higher_high": higher_high,
        "higher_low": higher_low,
        "lower_high": lower_high,
        "lower_low": lower_low,
        "bullish_structure": bullish,
        "bearish_structure": bearish,
        "range_structure": range_structure,
        "structure": structure,
        "high_count": len(highs),
        "low_count": len(lows),
    }


def _safe_latest(series: pd.Series, default: float = 0.0) -> float:
    if series is None or series.empty:
        return default
    value = pd.to_numeric(series, errors="coerce").dropna()
    if value.empty:
        return default
    return float(value.iloc[-1])


def _possible_phase(structure: str, candles: pd.DataFrame, swings: list[dict[str, Any]]) -> str:
    if structure not in {"bullish", "bearish"} or len(swings) < 4:
        return "correction" if structure == "range" else "unknown"
    close = float(candles["close"].iloc[-1])
    last_high = max((item["price"] for item in swings if item["type"] == "high"), default=close)
    last_low = min((item["price"] for item in swings if item["type"] == "low"), default=close)
    range_size = max(last_high - last_low, 0.0)
    if range_size <= 0:
        return "unknown"
    location = (close - last_low) / range_size
    if structure == "bullish":
        if location >= 0.75:
            return "possible_wave_5"
        if location >= 0.45:
            return "possible_wave_3"
        return "correction"
    if location <= 0.25:
        return "possible_wave_5"
    if location <= 0.55:
        return "possible_wave_3"
    return "correction"


def calculate_wave_score(candles: pd.DataFrame, indicators: dict[str, Any] | None = None) -> dict[str, Any]:
    indicators = indicators or {}
    notes: list[str] = []
    if candles is None or candles.empty or len(candles) < 30:
        return {"wave_score": 0, "structure": "unclear", "possible_phase": "unknown", "notes": ["not enough candle history"]}

    swings = detect_swing_highs_lows(candles)
    structure_data = detect_market_structure(swings)
    structure = structure_data["structure"]
    score = 0

    if structure in {"bullish", "bearish"}:
        score += 20
        notes.append(f"{structure} swing structure")
    elif structure == "range":
        score += 8
        notes.append("range structure")
    else:
        notes.append("unclear swing structure")

    swing_count = structure_data["high_count"] + structure_data["low_count"]
    if swing_count >= 6:
        score += 20
        notes.append("clear swing map")
    elif swing_count >= 4:
        score += 12
        notes.append("moderate swing clarity")
    else:
        score += 4
        notes.append("limited swing clarity")

    close = candles["close"].astype(float)
    high = candles["high"].astype(float)
    low = candles["low"].astype(float)
    latest_close = float(close.iloc[-1])
    ema20 = indicators.get("ema20", _safe_latest(candles.get("ema20", pd.Series(dtype=float)), latest_close))
    pullback_distance = abs(latest_close - float(ema20)) / latest_close * 100 if latest_close else 0.0
    if 0.15 <= pullback_distance <= 2.5:
        score += 20
        notes.append("healthy pullback distance")
    elif pullback_distance <= 4.0:
        score += 10
        notes.append("extended pullback")
    else:
        notes.append("poor pullback quality")

    recent_high = float(high.iloc[-21:-1].max())
    recent_low = float(low.iloc[-21:-1].min())
    if latest_close > recent_high or latest_close < recent_low:
        score += 20
        notes.append("breakout pressure")
    elif abs(latest_close - recent_high) / latest_close < 0.004 or abs(latest_close - recent_low) / latest_close < 0.004:
        score += 10
        notes.append("near breakout area")
    else:
        notes.append("no breakout pressure")

    volume = candles.get("volume")
    volume_sma = candles.get("volume_sma20")
    if volume is not None and not volume.empty:
        latest_volume = _safe_latest(volume)
        avg_volume = _safe_latest(volume_sma, float(volume.tail(20).mean())) if volume_sma is not None else float(volume.tail(20).mean())
        if avg_volume > 0 and latest_volume >= avg_volume * 1.2:
            score += 20
            notes.append("volume confirms move")
        elif avg_volume > 0 and latest_volume >= avg_volume:
            score += 10
            notes.append("volume neutral")
        else:
            notes.append("weak volume confirmation")
    else:
        notes.append("volume unavailable")

    return {
        "wave_score": int(max(0, min(100, score))),
        "structure": structure,
        "possible_phase": _possible_phase(structure, candles, swings),
        "notes": notes[:4],
    }
