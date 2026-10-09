"""Position-weighted reporting truth and delayed +0.25R post-TP1 shadow.

This module is deliberately pure: it reads candle evidence and returns facts.
It never sends messages, changes orders, or calls an exchange.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd


TP1_FRACTION = 0.5
REMAINDER_FRACTION = 0.5
SHADOW_NAME = "post_tp1_delayed_quarter_r_v1"
SHADOW_VERSION = "post_tp1_delayed_quarter_r_v1"
POST_TP1_DELAYED_QUARTER_R_SHADOW_START_UTC = "2026-10-09T12:38:26Z"

TERMINAL_REPORTING_STATES = {
    "ORIGINAL_SL",
    "TP2_WIN",
    "TP1_THEN_ORIGINAL_SL",
    "TP1_THEN_PROTECTIVE_STOP",
}
TERMINAL_SHADOW_STATES = {
    "SHADOW_TP2",
    "SHADOW_PROTECTIVE_STOP",
    "BASELINE_ORIGINAL_SL",
    "SAME_CANDLE_AMBIGUOUS",
}


@dataclass(frozen=True)
class LifecycleResult:
    state: str
    lifecycle_r: float | None
    tp1_touch_time_utc: str = ""
    remainder_resolution_time_utc: str = ""
    same_candle_ambiguous: bool = False
    reason: str = ""

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_REPORTING_STATES


@dataclass(frozen=True)
class ShadowResult:
    state: str
    decision_candle_time_utc: str = ""
    decision_close: float | None = None
    move_stop_to_quarter_r: bool | None = None
    shadow_stop_price: float | None = None
    activation_time_utc: str = ""
    terminal_event_time_utc: str = ""
    baseline_r: float | None = None
    shadow_r: float | None = None
    delta_r: float | None = None
    same_candle_ambiguous: bool = False
    reason: str = ""

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_SHADOW_STATES

    def metrics(self) -> dict[str, Any]:
        return asdict(self)


def _number(row: pd.Series | dict[str, Any], *names: str) -> float:
    for name in names:
        value = pd.to_numeric(pd.Series([row.get(name)]), errors="coerce").iloc[0]
        if not pd.isna(value):
            return float(value)
    return 0.0


def _geometry(row: pd.Series | dict[str, Any]) -> tuple[str, float, float, float, float, float, float]:
    side = str(row.get("side", row.get("direction", ""))).strip().upper()
    entry = _number(row, "entry")
    stop = _number(row, "stop_loss", "sl")
    tp1 = _number(row, "tp1")
    tp2 = _number(row, "tp2")
    risk = abs(entry - stop)
    if side not in {"LONG", "SHORT"} or min(entry, stop, tp1, tp2, risk) <= 0:
        raise ValueError("invalid trade geometry")
    tp1_r = abs(tp1 - entry) / risk
    tp2_r = abs(tp2 - entry) / risk
    return side, entry, stop, tp1, tp2, tp1_r, tp2_r


def _ordered(candles: pd.DataFrame) -> pd.DataFrame:
    required = {"high", "low", "close", "close_time"}
    if candles is None or candles.empty or not required.issubset(candles.columns):
        return pd.DataFrame()
    result = candles.copy()
    for column in ("high", "low", "close"):
        result[column] = pd.to_numeric(result[column], errors="coerce")
    result["close_time"] = pd.to_datetime(result["close_time"], utc=True, errors="coerce")
    sort_column = "open_time" if "open_time" in result.columns else "close_time"
    if sort_column == "open_time":
        result["open_time"] = pd.to_datetime(result["open_time"], utc=True, errors="coerce")
    return result.dropna(subset=["high", "low", "close", "close_time"]).sort_values(sort_column).reset_index(drop=True)


def _time(candle: pd.Series) -> str:
    value = pd.to_datetime(candle.get("close_time"), utc=True, errors="coerce")
    return "" if pd.isna(value) else value.isoformat()


def _open_time(candle: pd.Series) -> str:
    value = pd.to_datetime(candle.get("open_time"), utc=True, errors="coerce")
    return "" if pd.isna(value) else value.isoformat()


def _hits(side: str, candle: pd.Series, stop: float, tp1: float, tp2: float) -> tuple[bool, bool, bool]:
    high, low = float(candle["high"]), float(candle["low"])
    if side == "LONG":
        return low <= stop, high >= tp1, high >= tp2
    return high >= stop, low <= tp1, low <= tp2


def position_weighted_r(tp1_r: float, remainder_r: float) -> float:
    return TP1_FRACTION * tp1_r + REMAINDER_FRACTION * remainder_r


def evaluate_reporting_lifecycle(row: pd.Series | dict[str, Any], candles: pd.DataFrame) -> LifecycleResult:
    """Resolve current 50/50 TP1+remainder reporting without changing source outcome fields."""
    side, _entry, stop, tp1, tp2, tp1_r, tp2_r = _geometry(row)
    ordered = _ordered(candles)
    if ordered.empty:
        return LifecycleResult("UNRESOLVED_REMAINDER", None, reason="no_complete_candle_evidence")
    tp1_at = ""
    for _, candle in ordered.iterrows():
        sl_hit, tp1_hit, tp2_hit = _hits(side, candle, stop, tp1, tp2)
        event_at = _time(candle)
        if not tp1_at:
            if sl_hit and (tp1_hit or tp2_hit):
                return LifecycleResult("UNRESOLVED_REMAINDER", None, event_at, event_at, True, "sl_and_target_same_candle")
            if sl_hit:
                return LifecycleResult("ORIGINAL_SL", -1.0, remainder_resolution_time_utc=event_at)
            if tp2_hit:
                return LifecycleResult("TP2_WIN", position_weighted_r(tp1_r, tp2_r), event_at, event_at)
            if tp1_hit:
                tp1_at = event_at
                continue
        else:
            if sl_hit and tp2_hit:
                return LifecycleResult("UNRESOLVED_REMAINDER", None, tp1_at, event_at, True, "sl_and_tp2_same_candle_after_tp1")
            if sl_hit:
                return LifecycleResult("TP1_THEN_ORIGINAL_SL", position_weighted_r(tp1_r, -1.0), tp1_at, event_at)
            if tp2_hit:
                return LifecycleResult("TP2_WIN", position_weighted_r(tp1_r, tp2_r), tp1_at, event_at)
    if tp1_at:
        return LifecycleResult("TP1_TOUCHED_REMAINDER_OPEN", None, tp1_at)
    return LifecycleResult("UNRESOLVED_REMAINDER", None, reason="tp1_and_sl_not_reached")


def evaluate_delayed_quarter_r_shadow(row: pd.Series | dict[str, Any], candles: pd.DataFrame) -> ShadowResult:
    """Evaluate the exact next-closed-15m-candle decision with activation one candle later."""
    side, entry, stop, tp1, tp2, tp1_r, tp2_r = _geometry(row)
    ordered = _ordered(candles)
    if ordered.empty:
        return ShadowResult("UNRESOLVED", reason="no_complete_candle_evidence")

    tp1_index: int | None = None
    tp1_at = ""
    for index, candle in ordered.iterrows():
        sl_hit, tp1_hit, tp2_hit = _hits(side, candle, stop, tp1, tp2)
        event_at = _time(candle)
        if sl_hit and (tp1_hit or tp2_hit):
            return ShadowResult("SAME_CANDLE_AMBIGUOUS", terminal_event_time_utc=event_at, same_candle_ambiguous=True, reason="sl_and_target_same_candle")
        if sl_hit:
            return ShadowResult("BASELINE_ORIGINAL_SL", terminal_event_time_utc=event_at, baseline_r=-1.0, shadow_r=-1.0, delta_r=0.0)
        if tp2_hit:
            win_r = position_weighted_r(tp1_r, tp2_r)
            return ShadowResult("SHADOW_TP2", terminal_event_time_utc=event_at, baseline_r=win_r, shadow_r=win_r, delta_r=0.0)
        if tp1_hit:
            tp1_index, tp1_at = index, event_at
            break
    if tp1_index is None:
        return ShadowResult("UNRESOLVED", reason="waiting_for_tp1")

    decision_index = tp1_index + 1
    if decision_index >= len(ordered):
        return ShadowResult("UNRESOLVED", reason="waiting_for_first_complete_15m_candle")
    decision_candle = ordered.iloc[decision_index]
    decision_at = _time(decision_candle)
    sl_hit, _tp1_hit, tp2_hit = _hits(side, decision_candle, stop, tp1, tp2)
    if sl_hit and tp2_hit:
        return ShadowResult("SAME_CANDLE_AMBIGUOUS", decision_at, float(decision_candle["close"]), terminal_event_time_utc=decision_at, same_candle_ambiguous=True, reason="original_sl_and_tp2_on_decision_candle")
    if sl_hit:
        baseline = position_weighted_r(tp1_r, -1.0)
        return ShadowResult("BASELINE_ORIGINAL_SL", decision_at, float(decision_candle["close"]), False, terminal_event_time_utc=decision_at, baseline_r=baseline, shadow_r=baseline, delta_r=0.0, reason="remainder_closed_before_shadow_activation")
    if tp2_hit:
        win_r = position_weighted_r(tp1_r, tp2_r)
        return ShadowResult("SHADOW_TP2", decision_at, float(decision_candle["close"]), False, terminal_event_time_utc=decision_at, baseline_r=win_r, shadow_r=win_r, delta_r=0.0, reason="tp2_before_shadow_activation")

    decision_close = float(decision_candle["close"])
    move = decision_close > tp1 if side == "LONG" else decision_close < tp1
    risk = abs(entry - stop)
    shadow_stop = entry + 0.25 * risk if side == "LONG" else entry - 0.25 * risk
    activation_index = decision_index + 1
    activation_at = _open_time(ordered.iloc[activation_index]) if activation_index < len(ordered) else ""

    for index in range(activation_index, len(ordered)):
        candle = ordered.iloc[index]
        event_at = _time(candle)
        active_stop = shadow_stop if move else stop
        stop_hit, _tp1_hit, tp2_hit = _hits(side, candle, active_stop, tp1, tp2)
        if stop_hit and tp2_hit:
            return ShadowResult("SAME_CANDLE_AMBIGUOUS", decision_at, decision_close, move, shadow_stop if move else None, activation_at, event_at, same_candle_ambiguous=True, reason="active_stop_and_tp2_same_candle")
        if stop_hit:
            remainder_r = 0.25 if move else -1.0
            shadow_r = position_weighted_r(tp1_r, remainder_r)
            state = "SHADOW_PROTECTIVE_STOP" if move else "BASELINE_ORIGINAL_SL"
            baseline_lifecycle = evaluate_reporting_lifecycle(row, ordered)
            baseline_r = baseline_lifecycle.lifecycle_r
            return ShadowResult(state, decision_at, decision_close, move, shadow_stop if move else None, activation_at, event_at, baseline_r, shadow_r, None if baseline_r is None else shadow_r - baseline_r)
        if tp2_hit:
            win_r = position_weighted_r(tp1_r, tp2_r)
            return ShadowResult("SHADOW_TP2", decision_at, decision_close, move, shadow_stop if move else None, activation_at, event_at, win_r, win_r, 0.0)

    return ShadowResult("UNRESOLVED", decision_at, decision_close, move, shadow_stop if move else None, activation_at, reason="remainder_open")
