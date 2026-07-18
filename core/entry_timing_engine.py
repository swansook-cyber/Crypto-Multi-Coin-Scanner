# -*- coding: utf-8 -*-
"""Entry Timing Engine V1.

Shadow-mode analytics only. It evaluates timing quality for already-generated
signal candidates and writes separate research rows. It must not alter scanner
selection, routing, TP/SL, RR, score, Cornix output, or outcome review.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


FIELDNAMES = [
    "timestamp",
    "symbol",
    "direction",
    "market_session",
    "watchlist_tier",
    "entry",
    "support",
    "resistance",
    "atr_proxy",
    "distance_to_support_pct",
    "distance_to_resistance_pct",
    "pullback_opportunity",
    "breakout_confirmation",
    "breakout_retest_confirmation",
    "overextended_move",
    "entry_quality_score",
    "recommendation",
    "reason",
    "source_signal_score",
    "source_setup_strength",
    "market_regime",
    "volume_spike",
    "mfi_confirmed",
    "source_signal_id",
    "candidate_id",
    "final_signal_timestamp",
    "signal_status",
    "normalized_symbol",
    "normalized_direction",
]


@dataclass
class EntryTimingResult:
    timestamp: str
    symbol: str
    direction: str
    market_session: str
    watchlist_tier: str
    entry: float
    support: float
    resistance: float
    atr_proxy: float
    distance_to_support_pct: float
    distance_to_resistance_pct: float
    pullback_opportunity: str
    breakout_confirmation: str
    breakout_retest_confirmation: str
    overextended_move: str
    entry_quality_score: int
    recommendation: str
    reason: str
    source_signal_score: int
    source_setup_strength: int
    market_regime: str
    volume_spike: str
    mfi_confirmed: str
    source_signal_id: str = ""
    candidate_id: str = ""
    final_signal_timestamp: str = ""
    signal_status: str = ""
    normalized_symbol: str = ""
    normalized_direction: str = ""


def normalize_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    if ":" in text:
        text = text.split(":")[-1]
    return text.replace("#", "").replace(".P", "").replace("/", "").replace("-", "").replace("_", "")


def normalize_direction(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text == "BUY":
        return "LONG"
    if text == "SELL":
        return "SHORT"
    return text if text in {"LONG", "SHORT"} else ""


def _float(value: Any, default: float = 0.0) -> float:
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric):
        return default
    return float(numeric)


def _pct_distance(price: float, level: float) -> float:
    if price <= 0 or level <= 0:
        return 0.0
    return abs(price - level) / price * 100.0


def _yes(value: bool) -> str:
    return "YES" if value else "NO"


class EntryTimingEngine:
    """Evaluate candidate timing without changing production behavior."""

    def evaluate(self, signal: Any) -> EntryTimingResult:
        entry = _float(getattr(signal, "entry", 0.0))
        support = _float(getattr(signal, "support", 0.0))
        resistance = _float(getattr(signal, "resistance", 0.0))
        sl = _float(getattr(signal, "sl", 0.0))
        direction = str(getattr(signal, "direction", "")).upper()
        atr_proxy = abs(entry - sl) if entry > 0 and sl > 0 else 0.0
        atr_pct_proxy = atr_proxy / entry * 100.0 if entry > 0 and atr_proxy > 0 else _float(getattr(signal, "atr_pct", 0.0))
        dist_support = _pct_distance(entry, support)
        dist_resistance = _pct_distance(entry, resistance)

        if direction == "LONG":
            nearest_block_pct = dist_resistance
            nearest_pullback_pct = dist_support
            near_breakout = resistance > 0 and entry >= resistance * 0.995
            retest_zone = resistance > 0 and entry > resistance and (entry - resistance) <= max(atr_proxy * 0.55, entry * 0.002)
        else:
            nearest_block_pct = dist_support
            nearest_pullback_pct = dist_resistance
            near_breakout = support > 0 and entry <= support * 1.005
            retest_zone = support > 0 and entry < support and (support - entry) <= max(atr_proxy * 0.55, entry * 0.002)

        volume_spike = bool(getattr(signal, "volume_spike", False))
        mfi_confirmed = bool(getattr(signal, "mfi_confirmed", False))
        body_ratio = _float(getattr(signal, "body_ratio", 0.0))
        atr_expansion_ratio = _float(getattr(signal, "atr_expansion_ratio", 1.0), 1.0)
        regime = str(getattr(signal, "regime", ""))

        pullback_opportunity = nearest_pullback_pct >= max(atr_pct_proxy * 1.2, 0.65)
        breakout_confirmation = near_breakout and volume_spike and body_ratio >= 0.45
        breakout_retest_confirmation = retest_zone and (mfi_confirmed or volume_spike)
        overextended_move = atr_expansion_ratio >= 1.75 or nearest_pullback_pct >= max(atr_pct_proxy * 3.0, 2.5)

        score = 50
        reasons: list[str] = []
        if nearest_block_pct < max(atr_pct_proxy * 0.65, 0.35):
            score -= 22
            reasons.append("entry close to opposing S/R")
        else:
            score += 10
            reasons.append("space to opposing S/R")
        if pullback_opportunity:
            score -= 8
            reasons.append("better pullback may be available")
        else:
            score += 10
            reasons.append("entry not far from structure")
        if breakout_confirmation:
            score += 18
            reasons.append("breakout confirmation present")
        if breakout_retest_confirmation:
            score += 18
            reasons.append("breakout-retest confirmation present")
        if overextended_move:
            score -= 22
            reasons.append("move appears overextended")
        if volume_spike:
            score += 8
            reasons.append("volume supports timing")
        if mfi_confirmed:
            score += 6
            reasons.append("MFI confirms direction")
        if regime.lower() == "sideway":
            score -= 8
            reasons.append("sideway regime timing risk")

        score = int(max(0, min(100, score)))
        recommendation = self._recommendation(
            score,
            nearest_block_pct,
            pullback_opportunity,
            breakout_confirmation,
            breakout_retest_confirmation,
            overextended_move,
            atr_pct_proxy,
        )

        return EntryTimingResult(
            timestamp=datetime.now(timezone.utc).isoformat(),
            symbol=str(getattr(signal, "symbol", "")).upper(),
            direction=direction,
            market_session=str(getattr(signal, "market_session", "")),
            watchlist_tier=str(getattr(signal, "watchlist_tier", "")),
            entry=round(entry, 8),
            support=round(support, 8),
            resistance=round(resistance, 8),
            atr_proxy=round(atr_proxy, 8),
            distance_to_support_pct=round(dist_support, 4),
            distance_to_resistance_pct=round(dist_resistance, 4),
            pullback_opportunity=_yes(pullback_opportunity),
            breakout_confirmation=_yes(breakout_confirmation),
            breakout_retest_confirmation=_yes(breakout_retest_confirmation),
            overextended_move=_yes(overextended_move),
            entry_quality_score=score,
            recommendation=recommendation,
            reason="; ".join(reasons[:5]),
            source_signal_score=int(_float(getattr(signal, "score", 0))),
            source_setup_strength=int(_float(getattr(signal, "confidence", 0))),
            market_regime=regime,
            volume_spike=_yes(volume_spike),
            mfi_confirmed=_yes(mfi_confirmed),
            source_signal_id=str(getattr(signal, "source_signal_id", getattr(signal, "signal_id", "")) or ""),
            candidate_id=str(getattr(signal, "candidate_id", "") or ""),
            final_signal_timestamp=str(getattr(signal, "timestamp", "") or ""),
            signal_status="",
            normalized_symbol=normalize_symbol(getattr(signal, "symbol", "")),
            normalized_direction=normalize_direction(direction),
        )

    @staticmethod
    def _recommendation(
        score: int,
        nearest_block_pct: float,
        pullback_opportunity: bool,
        breakout_confirmation: bool,
        breakout_retest_confirmation: bool,
        overextended_move: bool,
        atr_pct_proxy: float,
    ) -> str:
        if score < 45 or overextended_move:
            return "SKIP (poor timing)"
        if breakout_retest_confirmation and score >= 65:
            return "WAIT FOR BREAKOUT RETEST"
        if breakout_confirmation and score >= 65:
            return "WAIT FOR BREAKOUT" if nearest_block_pct < max(atr_pct_proxy * 0.7, 0.4) else "ENTER NOW"
        if pullback_opportunity:
            return "WAIT FOR PULLBACK"
        if score >= 70:
            return "ENTER NOW"
        return "WAIT FOR PULLBACK"


class EntryTimingLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self.path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
                writer.writeheader()
        else:
            self._ensure_header()

    def _ensure_header(self) -> None:
        try:
            existing = pd.read_csv(self.path)
        except Exception:
            return
        missing = [field for field in FIELDNAMES if field not in existing.columns]
        if not missing:
            return
        for field in missing:
            existing[field] = ""
        existing.to_csv(self.path, index=False, columns=FIELDNAMES)

    def log(self, result: EntryTimingResult) -> None:
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
            writer.writerow({key: asdict(result).get(key, "") for key in FIELDNAMES})

    def log_many(self, results: list[EntryTimingResult]) -> None:
        for result in results:
            self.log(result)


def summarize_entry_timing(df: pd.DataFrame) -> pd.DataFrame:
    columns = ["Recommendation", "Candidates", "Avg Entry Quality", "Top Symbols"]
    if df.empty or "recommendation" not in df.columns:
        return pd.DataFrame(columns=columns)
    data = df.copy()
    data["entry_quality_score"] = pd.to_numeric(data.get("entry_quality_score"), errors="coerce")
    rows = []
    for recommendation, group in data.groupby(data["recommendation"].fillna("-").astype(str)):
        top_symbols = group.get("symbol", pd.Series(dtype=str)).fillna("-").astype(str).value_counts().head(3)
        rows.append(
            {
                "Recommendation": recommendation,
                "Candidates": int(len(group)),
                "Avg Entry Quality": round(float(group["entry_quality_score"].mean()), 1) if group["entry_quality_score"].notna().any() else 0.0,
                "Top Symbols": ", ".join(f"{symbol}: {count}" for symbol, count in top_symbols.items()) if not top_symbols.empty else "-",
            }
        )
    return pd.DataFrame(rows, columns=columns).sort_values(["Candidates", "Avg Entry Quality"], ascending=[False, False])


def format_entry_timing_summary(df: pd.DataFrame, limit: int = 8) -> str:
    summary = summarize_entry_timing(df)
    if summary.empty:
        return "N/A"
    return summary.head(limit).to_string(index=False)
