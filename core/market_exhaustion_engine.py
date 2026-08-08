# -*- coding: utf-8 -*-
"""Market exhaustion shadow analytics.

Phase 1 is intentionally read-only/report-only. The evaluator returns context
and writes to a separate CSV, but it must never change live score, confidence,
ranking, routing, or TP/SL decisions.
"""

from __future__ import annotations

import csv
import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from core.wave_structure_analyzer import detect_swing_highs_lows


FIELDNAMES = [
    "timestamp_utc",
    "shadow_key",
    "signal_timestamp",
    "symbol",
    "side",
    "signal_status",
    "entry",
    "original_score",
    "original_confidence",
    "original_rr",
    "swing_distance_atr",
    "ema20_distance_atr",
    "ema50_distance_atr",
    "directional_run_1h",
    "directional_run_15m",
    "atr_expansion_ratio",
    "rsi",
    "mfi",
    "breakout_context",
    "momentum_exception_applied",
    "exhaustion_class",
    "exhaustion_penalty_shadow",
    "reason",
    "sr_gate_decision",
    "source",
]


@dataclass(frozen=True)
class MarketExhaustionConfig:
    fresh_swing_atr: float = 1.5
    normal_swing_atr: float = 2.5
    exhausted_swing_atr: float = 4.0
    ema20_extended_atr: float = 1.5
    ema20_exhausted_atr: float = 2.0
    ema50_extended_atr: float = 2.5
    ema50_exhausted_atr: float = 4.0
    directional_run_extended: int = 5
    directional_run_exhausted: int = 7
    atr_expansion_extended: float = 1.5
    atr_expansion_exhausted: float = 2.0
    strong_body_ratio: float = 0.55
    max_opposite_wick_ratio: float = 0.35
    rsi_long_exhaustion: float = 75.0
    rsi_short_exhaustion: float = 25.0
    mfi_long_exhaustion: float = 80.0
    mfi_short_exhaustion: float = 20.0


@dataclass(frozen=True)
class MarketExhaustionResult:
    swing_distance_atr: float | None
    ema20_distance_atr: float | None
    ema50_distance_atr: float | None
    directional_run_1h: int
    directional_run_15m: int
    atr_expansion_ratio: float | None
    rsi: float | None
    mfi: float | None
    breakout_context: str
    momentum_exception_applied: bool
    exhaustion_class: str
    exhaustion_penalty_shadow: int
    reason: str
    sr_gate_decision: str


def _to_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _round_or_blank(value: float | None, digits: int = 8) -> float | str:
    if value is None or not math.isfinite(value):
        return ""
    return round(value, digits)


def _normalize_timestamp(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    except (TypeError, ValueError):
        return str(value)
    if pd.isna(timestamp):
        return str(value)
    return timestamp.isoformat()


def _series_latest(candles: pd.DataFrame | None, column: str) -> float | None:
    if candles is None or candles.empty or column not in candles.columns:
        return None
    values = pd.to_numeric(candles[column], errors="coerce").dropna()
    if values.empty:
        return None
    return _to_float(values.iloc[-1])


def _atr_expansion_from_candles(candles: pd.DataFrame | None, current_atr: float) -> float | None:
    if candles is None or candles.empty or "atr14" not in candles.columns:
        return None
    history = pd.to_numeric(candles["atr14"], errors="coerce").dropna()
    history = history[history > 0]
    if len(history) < 5:
        return None
    baseline = float(history.tail(30).median())
    if baseline <= 0 or not math.isfinite(baseline):
        return None
    return current_atr / baseline


def _directional_run(candles: pd.DataFrame | None, side: str) -> int:
    if candles is None or candles.empty or not {"open", "close"}.issubset(candles.columns):
        return 0
    run = 0
    for _, row in candles.tail(30).iloc[::-1].iterrows():
        open_price = _to_float(row.get("open"))
        close_price = _to_float(row.get("close"))
        if open_price is None or close_price is None:
            break
        same_direction = close_price > open_price if side == "LONG" else close_price < open_price
        if not same_direction:
            break
        run += 1
    return run


def _swing_distance_atr(candles: pd.DataFrame | None, side: str, entry: float, atr: float) -> float | None:
    if candles is None or candles.empty or not {"high", "low"}.issubset(candles.columns):
        return None
    swings = detect_swing_highs_lows(candles, lookback=3)
    if side == "LONG":
        lows = [item for item in swings if item.get("type") == "low" and _to_float(item.get("price")) is not None]
        candidates = [float(item["price"]) for item in lows if float(item["price"]) < entry]
        if not candidates:
            return None
        return (entry - candidates[-1]) / atr
    highs = [item for item in swings if item.get("type") == "high" and _to_float(item.get("price")) is not None]
    candidates = [float(item["price"]) for item in highs if float(item["price"]) > entry]
    if not candidates:
        return None
    return (candidates[-1] - entry) / atr


def _severity_from_swing(value: float | None, cfg: MarketExhaustionConfig) -> int:
    if value is None:
        return -1
    if value < cfg.fresh_swing_atr:
        return 0
    if value < cfg.normal_swing_atr:
        return 1
    if value < cfg.exhausted_swing_atr:
        return 2
    return 3


def _severity_from_ema(value: float | None, extended: float, exhausted: float) -> int:
    if value is None:
        return -1
    if value < extended * 0.5:
        return 0
    if value < extended:
        return 1
    if value < exhausted:
        return 2
    return 3


def _severity_from_run(run_1h: int, run_15m: int, cfg: MarketExhaustionConfig) -> int:
    combined = max(run_1h, run_15m // 3)
    if combined < 3:
        return 0
    if combined < cfg.directional_run_extended:
        return 1
    if combined < cfg.directional_run_exhausted:
        return 2
    return 3


def _severity_from_atr_expansion(value: float | None, cfg: MarketExhaustionConfig) -> int:
    if value is None:
        return -1
    if value < 1.1:
        return 0
    if value < cfg.atr_expansion_extended:
        return 1
    if value < cfg.atr_expansion_exhausted:
        return 2
    return 3


def _oscillator_note(side: str, rsi: float | None, mfi: float | None, cfg: MarketExhaustionConfig) -> tuple[int, str | None]:
    if side == "LONG":
        if (rsi is not None and rsi >= cfg.rsi_long_exhaustion) or (mfi is not None and mfi >= cfg.mfi_long_exhaustion):
            return 1, "oscillator elevated"
    if side == "SHORT":
        if (rsi is not None and rsi <= cfg.rsi_short_exhaustion) or (mfi is not None and mfi <= cfg.mfi_short_exhaustion):
            return 1, "oscillator stretched"
    return 0, None


def _class_from_severity(severities: list[int], missing_core: bool, reasons: list[str]) -> str:
    valid = [value for value in severities if value >= 0]
    if missing_core or not valid:
        return "UNKNOWN"
    high_count = sum(1 for value in valid if value >= 3)
    extended_count = sum(1 for value in valid if value >= 2)
    score = sum(valid)
    if high_count >= 2 or (high_count >= 1 and extended_count >= 3) or score > 10:
        return "EXHAUSTED"
    if extended_count >= 1 or score >= 5:
        return "EXTENDED"
    if score >= 1:
        return "NORMAL"
    return "FRESH"


def _reduce_classification(exhaustion_class: str) -> str:
    order = ["FRESH", "NORMAL", "EXTENDED", "EXHAUSTED"]
    if exhaustion_class not in order:
        return exhaustion_class
    return order[max(0, order.index(exhaustion_class) - 1)]


def _penalty_for_class(exhaustion_class: str, severities: list[int]) -> int:
    valid_sum = sum(value for value in severities if value > 0)
    if exhaustion_class == "FRESH":
        return 0
    if exhaustion_class == "NORMAL":
        return -min(3, max(1, valid_sum))
    if exhaustion_class == "EXTENDED":
        return -min(10, max(5, valid_sum + 2))
    if exhaustion_class == "EXHAUSTED":
        return -min(18, max(10, valid_sum + 5))
    return 0


def _momentum_exception_allowed(
    *,
    breakout_confirmed: bool,
    volume_spike: bool,
    body_ratio: float | None,
    opposite_wick_ratio: float | None,
    momentum_15m_confirmed: bool,
    htf_alignment: Any,
    sr_gate_decision: str,
    config: MarketExhaustionConfig,
) -> bool:
    alignment_text = str(htf_alignment or "").strip().upper()
    aligned = alignment_text in {"ALIGNED", "YES", "TRUE", "BULLISH", "BEARISH"}
    return (
        bool(breakout_confirmed)
        and bool(volume_spike)
        and (body_ratio is not None and body_ratio >= config.strong_body_ratio)
        and (opposite_wick_ratio is not None and opposite_wick_ratio <= config.max_opposite_wick_ratio)
        and bool(momentum_15m_confirmed)
        and aligned
        and str(sr_gate_decision or "").upper() != "SKIP"
    )


def evaluate_market_exhaustion(
    *,
    side: str,
    entry: Any,
    atr: Any,
    ema20: Any = None,
    ema50: Any = None,
    rsi: Any = None,
    mfi: Any = None,
    volume_spike: bool = False,
    body_ratio: Any = None,
    opposite_wick_ratio: Any = None,
    breakout_confirmed: bool = False,
    momentum_15m_confirmed: bool = False,
    htf_alignment: Any = "",
    candles_1h: pd.DataFrame | None = None,
    candles_15m: pd.DataFrame | None = None,
    sr_gate_decision: str = "",
    config: MarketExhaustionConfig | None = None,
) -> MarketExhaustionResult:
    """Evaluate whether an approved candidate is fresh or late-stage.

    This function is pure and has no side effects. It is used only for shadow
    analytics in Phase 1.
    """
    cfg = config or MarketExhaustionConfig()
    direction = str(side or "").upper()
    entry_f = _to_float(entry)
    atr_f = _to_float(atr)
    ema20_f = _to_float(ema20)
    ema50_f = _to_float(ema50)
    rsi_f = _to_float(rsi)
    mfi_f = _to_float(mfi)
    body_f = _to_float(body_ratio)
    wick_f = _to_float(opposite_wick_ratio)
    reasons: list[str] = []

    if direction not in {"LONG", "SHORT"} or entry_f is None or atr_f is None or atr_f <= 0:
        return MarketExhaustionResult(None, None, None, 0, 0, None, rsi_f, mfi_f, "UNKNOWN", False, "UNKNOWN", 0, "invalid direction/entry/ATR", sr_gate_decision)

    if ema20_f is None:
        ema20_f = _series_latest(candles_1h, "ema20")
    if ema50_f is None:
        ema50_f = _series_latest(candles_1h, "ema50")
    if rsi_f is None:
        rsi_f = _series_latest(candles_1h, "rsi14")
    if mfi_f is None:
        mfi_f = _series_latest(candles_1h, "mfi")

    swing_atr = _swing_distance_atr(candles_1h, direction, entry_f, atr_f)
    ema20_atr = abs(entry_f - ema20_f) / atr_f if ema20_f is not None else None
    ema50_atr = abs(entry_f - ema50_f) / atr_f if ema50_f is not None else None
    run_1h = _directional_run(candles_1h, direction)
    run_15m = _directional_run(candles_15m, direction)
    atr_expansion = _atr_expansion_from_candles(candles_1h, atr_f)

    swing_severity = _severity_from_swing(swing_atr, cfg)
    ema20_severity = _severity_from_ema(ema20_atr, cfg.ema20_extended_atr, cfg.ema20_exhausted_atr)
    ema50_severity = _severity_from_ema(ema50_atr, cfg.ema50_extended_atr, cfg.ema50_exhausted_atr)
    run_severity = _severity_from_run(run_1h, run_15m, cfg)
    atr_severity = _severity_from_atr_expansion(atr_expansion, cfg)
    oscillator_severity, oscillator_reason = _oscillator_note(direction, rsi_f, mfi_f, cfg)
    if oscillator_reason:
        reasons.append(oscillator_reason)

    if swing_atr is None:
        reasons.append("swing unavailable")
    elif swing_atr < cfg.fresh_swing_atr:
        reasons.append("fresh from recent swing")
    elif swing_atr < cfg.normal_swing_atr:
        reasons.append("normal swing distance")
    elif swing_atr < cfg.exhausted_swing_atr:
        reasons.append("extended from swing")
    else:
        reasons.append("far from recent swing")

    if ema20_atr is None or ema50_atr is None:
        reasons.append("EMA context unavailable")
    elif ema20_atr >= cfg.ema20_exhausted_atr or ema50_atr >= cfg.ema50_exhausted_atr:
        reasons.append("far above/below EMA context")
    elif ema20_atr >= cfg.ema20_extended_atr:
        reasons.append("extended from EMA20")

    if run_severity >= 2:
        reasons.append("long directional run")
    if atr_severity >= 2:
        reasons.append("ATR expansion elevated")

    missing_core = swing_atr is None or ema20_atr is None or ema50_atr is None
    severities = [swing_severity, ema20_severity, ema50_severity, run_severity, atr_severity, oscillator_severity]
    exhaustion_class = _class_from_severity(severities, missing_core, reasons)
    breakout_context = "NONE"
    if breakout_confirmed:
        breakout_context = "CONFIRMED_BREAKOUT" if body_f is not None and wick_f is not None and body_f >= cfg.strong_body_ratio and wick_f <= cfg.max_opposite_wick_ratio else "WEAK_BREAKOUT"

    exception_applied = False
    if exhaustion_class in {"EXTENDED", "EXHAUSTED"} and _momentum_exception_allowed(
        breakout_confirmed=breakout_confirmed,
        volume_spike=volume_spike,
        body_ratio=body_f,
        opposite_wick_ratio=wick_f,
        momentum_15m_confirmed=momentum_15m_confirmed,
        htf_alignment=htf_alignment,
        sr_gate_decision=sr_gate_decision,
        config=cfg,
    ):
        exhaustion_class = _reduce_classification(exhaustion_class)
        exception_applied = True
        breakout_context = "MOMENTUM_CONTINUATION_EXCEPTION"
        reasons.append("momentum continuation exception applied")

    penalty = _penalty_for_class(exhaustion_class, severities)
    return MarketExhaustionResult(
        swing_distance_atr=swing_atr,
        ema20_distance_atr=ema20_atr,
        ema50_distance_atr=ema50_atr,
        directional_run_1h=run_1h,
        directional_run_15m=run_15m,
        atr_expansion_ratio=atr_expansion,
        rsi=rsi_f,
        mfi=mfi_f,
        breakout_context=breakout_context,
        momentum_exception_applied=exception_applied,
        exhaustion_class=exhaustion_class,
        exhaustion_penalty_shadow=penalty,
        reason="; ".join(reasons[:6]) or "market exhaustion context unavailable",
        sr_gate_decision=sr_gate_decision,
    )


def shadow_key_for_signal(signal: Any, signal_status: str = "candidate") -> str:
    signal_id = str(getattr(signal, "signal_id", "") or getattr(signal, "candidate_id", "") or "").strip()
    if signal_id:
        return hashlib.sha256(signal_id.encode("utf-8")).hexdigest()[:24]
    timestamp_text = _normalize_timestamp(getattr(signal, "timestamp", ""))
    entry = _to_float(getattr(signal, "entry", None)) or 0.0
    parts = [
        str(getattr(signal, "symbol", "")).upper(),
        str(getattr(signal, "direction", "")).upper(),
        signal_status,
        timestamp_text,
        f"{entry:.6f}",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]


def build_shadow_record(signal: Any, result: MarketExhaustionResult, signal_status: str = "candidate", source: str = "scanner") -> dict[str, Any]:
    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "shadow_key": shadow_key_for_signal(signal, signal_status),
        "signal_timestamp": _normalize_timestamp(getattr(signal, "timestamp", "")),
        "symbol": getattr(signal, "symbol", ""),
        "side": getattr(signal, "direction", ""),
        "signal_status": signal_status,
        "entry": _round_or_blank(_to_float(getattr(signal, "entry", None))),
        "original_score": getattr(signal, "score", ""),
        "original_confidence": getattr(signal, "confidence", ""),
        "original_rr": _round_or_blank(_to_float(getattr(signal, "rr", None)), 4),
        "swing_distance_atr": _round_or_blank(result.swing_distance_atr, 4),
        "ema20_distance_atr": _round_or_blank(result.ema20_distance_atr, 4),
        "ema50_distance_atr": _round_or_blank(result.ema50_distance_atr, 4),
        "directional_run_1h": result.directional_run_1h,
        "directional_run_15m": result.directional_run_15m,
        "atr_expansion_ratio": _round_or_blank(result.atr_expansion_ratio, 4),
        "rsi": _round_or_blank(result.rsi, 4),
        "mfi": _round_or_blank(result.mfi, 4),
        "breakout_context": result.breakout_context,
        "momentum_exception_applied": int(result.momentum_exception_applied),
        "exhaustion_class": result.exhaustion_class,
        "exhaustion_penalty_shadow": result.exhaustion_penalty_shadow,
        "reason": result.reason,
        "sr_gate_decision": result.sr_gate_decision,
        "source": source,
    }


class MarketExhaustionShadowLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._known_keys: set[str] | None = None

    def _load_keys(self) -> set[str]:
        if self._known_keys is not None:
            return self._known_keys
        keys: set[str] = set()
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8", newline="") as handle:
                    reader = csv.DictReader(handle)
                    for row in reader:
                        key = str(row.get("shadow_key", "")).strip()
                        if key:
                            keys.add(key)
            except OSError:
                keys = set()
        self._known_keys = keys
        return keys

    def _has_valid_header(self) -> bool:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return True
        try:
            with self.path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.reader(handle)
                header = next(reader, [])
        except (OSError, csv.Error):
            return False
        return header == FIELDNAMES

    def append(self, record: dict[str, Any]) -> bool:
        key = str(record.get("shadow_key", "")).strip()
        if not key:
            return False
        keys = self._load_keys()
        if key in keys:
            return False
        exists = self.path.exists() and self.path.stat().st_size > 0
        if exists and not self._has_valid_header():
            return False
        try:
            with self.path.open("a", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
                if not exists:
                    writer.writeheader()
                writer.writerow({field: record.get(field, "") for field in FIELDNAMES})
                handle.flush()
        except OSError:
            return False
        keys.add(key)
        return True

    def log_signal(self, signal: Any, result: MarketExhaustionResult, signal_status: str = "candidate", source: str = "scanner") -> bool:
        return self.append(build_shadow_record(signal, result, signal_status, source))
