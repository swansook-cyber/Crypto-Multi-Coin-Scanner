# -*- coding: utf-8 -*-
"""Lightweight BTC market regime context for scanner risk filtering."""

from __future__ import annotations

from typing import Any

import pandas as pd


def _latest_float(df: pd.DataFrame, column: str, default: float = 0.0) -> float:
    if df is None or df.empty or column not in df:
        return default
    value = pd.to_numeric(df[column], errors="coerce").dropna()
    if value.empty:
        return default
    return float(value.iloc[-1])


def detect_btc_regime(candles: pd.DataFrame | None) -> dict[str, Any]:
    """Return a fail-safe BTC regime profile.

    The output is intentionally directional context only. It should never create
    trades or override the scanner entry/TP/SL logic.
    """
    fallback = {
        "regime": "unclear",
        "allow_long": True,
        "allow_short": True,
        "risk_multiplier": 1.0,
        "notes": ["BTC data unavailable"],
    }
    if candles is None or candles.empty or len(candles) < 60:
        return fallback

    try:
        df = candles.copy()
        close = df["close"].astype(float)
        ema20 = df["ema20"].astype(float) if "ema20" in df else close.ewm(span=20, adjust=False).mean()
        ema50 = df["ema50"].astype(float) if "ema50" in df else close.ewm(span=50, adjust=False).mean()
        atr_pct = _latest_float(df, "atr_pct")
        if not atr_pct and "atr14" in df:
            latest_close = float(close.iloc[-1])
            atr_pct = float(df["atr14"].iloc[-1]) / latest_close * 100 if latest_close else 0.0

        latest_close = float(close.iloc[-1])
        latest_ema20 = float(ema20.iloc[-1])
        latest_ema50 = float(ema50.iloc[-1])
        ema20_slope = (float(ema20.iloc[-1]) - float(ema20.iloc[-8])) / latest_close * 100 if latest_close else 0.0
        ema_gap_pct = abs(latest_ema20 - latest_ema50) / latest_close * 100 if latest_close else 0.0
        notes: list[str] = []

        if atr_pct >= 2.8:
            notes.append(f"BTC high volatility ATR {atr_pct:.2f}%")
            return {
                "regime": "high_volatility",
                "allow_long": True,
                "allow_short": True,
                "risk_multiplier": 0.5,
                "notes": notes,
            }

        if latest_close > latest_ema20 > latest_ema50 and ema20_slope > 0.08:
            notes.append("BTC bullish trend context")
            return {
                "regime": "bullish",
                "allow_long": True,
                "allow_short": True,
                "risk_multiplier": 1.0,
                "notes": notes,
            }

        if latest_close < latest_ema20 < latest_ema50 and ema20_slope < -0.08:
            notes.append("BTC bearish trend context")
            return {
                "regime": "bearish",
                "allow_long": True,
                "allow_short": True,
                "risk_multiplier": 1.0,
                "notes": notes,
            }

        if ema_gap_pct < 0.35 or abs(ema20_slope) < 0.08:
            notes.append("BTC sideways or low momentum context")
            return {
                "regime": "sideways",
                "allow_long": True,
                "allow_short": True,
                "risk_multiplier": 0.75,
                "notes": notes,
            }

        notes.append("BTC context unclear")
        return {
            "regime": "unclear",
            "allow_long": True,
            "allow_short": True,
            "risk_multiplier": 1.0,
            "notes": notes,
        }
    except Exception:
        return fallback
