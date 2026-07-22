# -*- coding: utf-8 -*-
"""Support/resistance trade-room shadow analytics.

Phase 1 is intentionally report-only. The result is written to a separate CSV
and must not change live scanner score, confidence, ranking, routing, or TP/SL.
"""

from __future__ import annotations

import csv
import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FIELDNAMES = [
    "timestamp_utc",
    "shadow_key",
    "signal_timestamp",
    "symbol",
    "side",
    "signal_status",
    "entry",
    "stop_loss",
    "tp1",
    "tp2",
    "risk_reward",
    "original_score",
    "original_confidence",
    "support",
    "resistance",
    "atr",
    "sr_gate_decision",
    "opposing_level",
    "opposing_distance",
    "opposing_distance_pct",
    "opposing_distance_atr",
    "risk_distance",
    "effective_sr_rr",
    "tp1_clearance",
    "sr_score_penalty_shadow",
    "breakout_context",
    "sr_gate_reason",
    "source",
]


@dataclass(frozen=True)
class SRGateConfig:
    hard_skip_effective_rr: float = 1.2
    caution_effective_rr: float = 1.8
    hard_skip_atr: float = 0.65
    caution_atr: float = 1.0


@dataclass(frozen=True)
class SRGateResult:
    decision: str
    opposing_level: float | None
    opposing_distance: float | None
    opposing_distance_pct: float | None
    opposing_distance_atr: float | None
    risk_distance: float | None
    effective_sr_rr: float | None
    tp1_clearance: float | None
    score_penalty_shadow: int
    breakout_context: str
    reason: str


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


def _valid_distance(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


def evaluate_sr_trade_weight(
    *,
    side: str,
    entry: Any,
    stop_loss: Any,
    tp1: Any,
    atr: Any,
    support: Any,
    resistance: Any,
    breakout_confirmed: bool = False,
    volume_spike: bool = False,
    mfi_confirmed: bool = False,
    body_ratio: Any = 0.0,
    opposite_wick_ratio: Any = 0.0,
    min_body_ratio: float = 0.45,
    max_opposite_wick_ratio: float = 0.45,
    config: SRGateConfig | None = None,
) -> SRGateResult:
    """Evaluate trade room against direction-aware opposing S/R.

    Returns SAFE/CAUTION/SKIP/UNKNOWN for analytics only. It never mutates the
    supplied signal or any live decision value.
    """
    cfg = config or SRGateConfig()
    direction = str(side or "").upper()
    entry_f = _to_float(entry)
    sl_f = _to_float(stop_loss)
    tp1_f = _to_float(tp1)
    atr_f = _to_float(atr)
    support_f = _to_float(support)
    resistance_f = _to_float(resistance)
    body_f = _to_float(body_ratio) or 0.0
    wick_f = _to_float(opposite_wick_ratio) or 0.0

    if direction not in {"LONG", "SHORT"}:
        return SRGateResult("UNKNOWN", None, None, None, None, None, None, None, 0, "UNKNOWN", "Invalid direction")
    if entry_f is None or sl_f is None or tp1_f is None:
        return SRGateResult("UNKNOWN", None, None, None, None, None, None, None, 0, "UNKNOWN", "Missing entry/SL/TP1")

    risk_distance = abs(entry_f - sl_f)
    if risk_distance <= 0:
        return SRGateResult("UNKNOWN", None, None, None, None, risk_distance, None, None, 0, "UNKNOWN", "Invalid SL distance")
    if atr_f is None or atr_f <= 0:
        return SRGateResult("UNKNOWN", None, None, None, None, risk_distance, None, None, 0, "UNKNOWN", "Invalid ATR")

    opposing_level = resistance_f if direction == "LONG" else support_f
    if opposing_level is None:
        return SRGateResult("UNKNOWN", None, None, None, None, risk_distance, None, None, 0, "NO_OPPOSING_LEVEL", "Missing opposing S/R")

    level_is_ahead = (direction == "LONG" and opposing_level > entry_f) or (direction == "SHORT" and opposing_level < entry_f)
    breakout_quality_confirmed = (
        bool(breakout_confirmed)
        and body_f >= min_body_ratio
        and wick_f <= max_opposite_wick_ratio
        and (bool(volume_spike) or bool(mfi_confirmed))
    )

    if not level_is_ahead:
        if breakout_quality_confirmed:
            return SRGateResult(
                "SAFE",
                opposing_level,
                None,
                None,
                None,
                risk_distance,
                None,
                None,
                0,
                "CONFIRMED_BREAKOUT",
                "Confirmed breakout through prior opposing level",
            )
        if breakout_confirmed:
            return SRGateResult(
                "CAUTION",
                opposing_level,
                None,
                None,
                None,
                risk_distance,
                None,
                None,
                6,
                "WEAK_BREAKOUT",
                "Weak breakout needs follow-through confirmation",
            )
        return SRGateResult("UNKNOWN", opposing_level, None, None, None, risk_distance, None, None, 0, "NO_OPPOSING_LEVEL", "No opposing level ahead of entry")

    opposing_distance = abs(opposing_level - entry_f)
    opposing_distance_pct = (opposing_distance / entry_f) * 100 if entry_f else None
    opposing_distance_atr = opposing_distance / atr_f if atr_f > 0 else None
    effective_sr_rr = opposing_distance / risk_distance if risk_distance > 0 else None
    tp1_distance = abs(tp1_f - entry_f)
    tp1_clearance = opposing_distance / tp1_distance if tp1_distance > 0 else None

    hard_skip = (
        (_valid_distance(effective_sr_rr) and effective_sr_rr < cfg.hard_skip_effective_rr)
        or (_valid_distance(opposing_distance_atr) and opposing_distance_atr < cfg.hard_skip_atr)
        or (_valid_distance(tp1_clearance) and tp1_clearance < 0.75)
    )
    caution = (
        (_valid_distance(effective_sr_rr) and effective_sr_rr < cfg.caution_effective_rr)
        or (_valid_distance(opposing_distance_atr) and opposing_distance_atr < cfg.caution_atr)
        or (_valid_distance(tp1_clearance) and tp1_clearance < 1.0)
    )

    if hard_skip:
        reason = "Resistance limits upside room" if direction == "LONG" else "Support limits downside room"
        return SRGateResult(
            "SKIP",
            opposing_level,
            opposing_distance,
            opposing_distance_pct,
            opposing_distance_atr,
            risk_distance,
            effective_sr_rr,
            tp1_clearance,
            15,
            "APPROACHING_LEVEL",
            reason,
        )
    if caution:
        reason = "Resistance is near entry/TP1" if direction == "LONG" else "Support is near entry/TP1"
        return SRGateResult(
            "CAUTION",
            opposing_level,
            opposing_distance,
            opposing_distance_pct,
            opposing_distance_atr,
            risk_distance,
            effective_sr_rr,
            tp1_clearance,
            8,
            "APPROACHING_LEVEL",
            reason,
        )

    return SRGateResult(
        "SAFE",
        opposing_level,
        opposing_distance,
        opposing_distance_pct,
        opposing_distance_atr,
        risk_distance,
        effective_sr_rr,
        tp1_clearance,
        0,
        "APPROACHING_LEVEL",
        "Opposing S/R has enough trade room",
    )


def shadow_key_for_signal(signal: Any, signal_status: str = "candidate") -> str:
    parts = [
        str(getattr(signal, "symbol", "")),
        str(getattr(signal, "direction", "")),
        str(signal_status),
        f"{_to_float(getattr(signal, 'entry', None)) or 0:.8f}",
        f"{_to_float(getattr(signal, 'sl', None)) or 0:.8f}",
        f"{_to_float(getattr(signal, 'tp1', None)) or 0:.8f}",
        f"{_to_float(getattr(signal, 'tp2', None)) or 0:.8f}",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]


def build_shadow_record(signal: Any, result: SRGateResult, signal_status: str = "candidate", source: str = "scanner") -> dict[str, Any]:
    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "shadow_key": shadow_key_for_signal(signal, signal_status),
        "signal_timestamp": getattr(getattr(signal, "timestamp", None), "isoformat", lambda: "")(),
        "symbol": getattr(signal, "symbol", ""),
        "side": getattr(signal, "direction", ""),
        "signal_status": signal_status,
        "entry": _round_or_blank(_to_float(getattr(signal, "entry", None))),
        "stop_loss": _round_or_blank(_to_float(getattr(signal, "sl", None))),
        "tp1": _round_or_blank(_to_float(getattr(signal, "tp1", None))),
        "tp2": _round_or_blank(_to_float(getattr(signal, "tp2", None))),
        "risk_reward": _round_or_blank(_to_float(getattr(signal, "rr", None)), 4),
        "original_score": getattr(signal, "score", ""),
        "original_confidence": getattr(signal, "confidence", ""),
        "support": _round_or_blank(_to_float(getattr(signal, "support", None))),
        "resistance": _round_or_blank(_to_float(getattr(signal, "resistance", None))),
        "atr": _round_or_blank(abs((_to_float(getattr(signal, "entry", None)) or 0.0) - (_to_float(getattr(signal, "sl", None)) or 0.0))),
        "sr_gate_decision": result.decision,
        "opposing_level": _round_or_blank(result.opposing_level),
        "opposing_distance": _round_or_blank(result.opposing_distance),
        "opposing_distance_pct": _round_or_blank(result.opposing_distance_pct, 4),
        "opposing_distance_atr": _round_or_blank(result.opposing_distance_atr, 4),
        "risk_distance": _round_or_blank(result.risk_distance),
        "effective_sr_rr": _round_or_blank(result.effective_sr_rr, 4),
        "tp1_clearance": _round_or_blank(result.tp1_clearance, 4),
        "sr_score_penalty_shadow": result.score_penalty_shadow,
        "breakout_context": result.breakout_context,
        "sr_gate_reason": result.reason,
        "source": source,
    }


class SRTradeWeightShadowLogger:
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

    def append(self, record: dict[str, Any]) -> bool:
        key = str(record.get("shadow_key", "")).strip()
        if not key:
            return False
        keys = self._load_keys()
        if key in keys:
            return False
        exists = self.path.exists() and self.path.stat().st_size > 0
        with self.path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
            if not exists:
                writer.writeheader()
            writer.writerow({field: record.get(field, "") for field in FIELDNAMES})
            handle.flush()
        keys.add(key)
        return True

    def log_signal(self, signal: Any, result: SRGateResult, signal_status: str = "candidate", source: str = "scanner") -> bool:
        return self.append(build_shadow_record(signal, result, signal_status, source))
