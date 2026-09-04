# -*- coding: utf-8 -*-
"""Prospective moving-stop shadow analytics.

This module is research-only. It observes production sent signals and records
what the remaining-position path would look like after TP1 if stop were moved
to breakeven. It does not send Telegram/Cornix messages or modify live scanner
decisions.
"""

from __future__ import annotations

import argparse
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.signal_identity import canonical_signal_key, normalize_side, normalize_symbol, normalize_timestamp


BASE_DIR = Path(__file__).resolve().parents[1]
BINANCE_FUTURES_KLINES = "https://fapi.binance.com/fapi/v1/klines"
TIMEFRAME = "15m"
SHADOW_VERSION = "moving-sl-shadow-v1"
TERMINAL_LIFECYCLE_CLASSES = {
    "TP2_BEFORE_BE",
    "BE_BEFORE_TP2",
    "SL_BEFORE_TP1",
    "AMBIGUOUS",
}
RECHECKABLE_LIFECYCLE_CLASSES = {"", "TP1_REACHED_UNRESOLVED", "DATA_INSUFFICIENT"}
BINANCE_15M_MS = 15 * 60 * 1000

FIELDNAMES = [
    "canonical_signal_key",
    "timestamp_utc",
    "symbol",
    "side",
    "entry",
    "original_sl",
    "tp1",
    "tp2",
    "signal_status",
    "original_result",
    "original_hit_target",
    "original_closed_at",
    "tp1_reached",
    "tp1_reached_at",
    "shadow_action",
    "shadow_action_at",
    "be_price",
    "be_reached_after_tp1",
    "be_reached_at",
    "tp2_reached_after_tp1",
    "tp2_reached_at",
    "lifecycle_class",
    "mfe_after_tp1_price",
    "mfe_after_tp1_r_multiple_if_geometry_only",
    "mae_after_tp1_price",
    "mae_after_tp1_r_multiple_if_geometry_only",
    "minutes_tp1_to_be",
    "minutes_tp1_to_tp2",
    "ambiguous_same_candle",
    "data_quality_notes",
    "market_session",
    "watchlist_tier",
    "setup_strength",
    "score",
    "confidence",
    "sr_class",
    "market_exhaustion_class",
    "entry_timing_class",
    "btc_regime",
    "shadow_version",
    "prospective_start_timestamp_utc",
    "generated_at_utc",
]


@dataclass(frozen=True)
class MovingSLCandidate:
    canonical_signal_key: str
    timestamp_utc: str
    symbol: str
    side: str
    entry: float
    original_sl: float
    tp1: float
    tp2: float
    signal_status: str = "sent"
    original_result: str = ""
    original_hit_target: str = ""
    original_closed_at: str = ""
    market_session: str = ""
    watchlist_tier: str = ""
    setup_strength: str = ""
    score: str = ""
    confidence: str = ""
    sr_class: str = ""
    market_exhaustion_class: str = ""
    entry_timing_class: str = ""
    btc_regime: str = ""


@dataclass(frozen=True)
class MovingSLLifecycle:
    tp1_reached: bool
    tp1_reached_at: str
    shadow_action: str
    shadow_action_at: str
    be_price: float | None
    be_reached_after_tp1: bool
    be_reached_at: str
    tp2_reached_after_tp1: bool
    tp2_reached_at: str
    lifecycle_class: str
    mfe_after_tp1_price: float | None
    mfe_after_tp1_r_multiple_if_geometry_only: float | None
    mae_after_tp1_price: float | None
    mae_after_tp1_r_multiple_if_geometry_only: float | None
    minutes_tp1_to_be: float | None
    minutes_tp1_to_tp2: float | None
    ambiguous_same_candle: bool
    data_quality_notes: str


@dataclass(frozen=True)
class MovingSLCollectionSummary:
    shadow_enabled: bool
    live_enabled: bool
    prospective_start_utc: str
    sent_rows_total: int
    prospective_sent_rows: int
    valid_prospective_candidates: int
    candidates_needing_candle_evaluation: int
    terminal_rows_skipped: int
    binance_request_count: int | str
    output_rows: int
    elapsed_seconds: float
    dry_run: bool


class CountingSession:
    """Tiny requests.Session wrapper used for CLI diagnostics."""

    def __init__(self, session: requests.Session) -> None:
        self.session = session
        self.request_count = 0

    def get(self, *args: Any, **kwargs: Any) -> requests.Response:
        self.request_count += 1
        return self.session.get(*args, **kwargs)


def load_project_env(base_dir: Path = BASE_DIR) -> None:
    load_dotenv(base_dir / ".env")


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def utc_now_text() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def safe_float(value: Any) -> float | None:
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric):
        return None
    try:
        result = float(numeric)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _minutes_between(start: str, end: str) -> float | None:
    start_ts = pd.to_datetime(start, utc=True, errors="coerce")
    end_ts = pd.to_datetime(end, utc=True, errors="coerce")
    if pd.isna(start_ts) or pd.isna(end_ts):
        return None
    return max(0.0, float((end_ts - start_ts).total_seconds() / 60.0))


def _format_number(value: float | None) -> str:
    if value is None or not math.isfinite(float(value)):
        return ""
    return f"{float(value):.6f}".rstrip("0").rstrip(".")


def _format_minutes(value: float | None) -> str:
    if value is None or not math.isfinite(float(value)):
        return ""
    return f"{float(value):.1f}"


def timestamp_on_or_after_prospective_start(timestamp: Any, prospective_start_utc: str = "") -> bool:
    start_text = normalize_timestamp(prospective_start_utc)
    if not start_text:
        return True
    signal_ts = pd.to_datetime(timestamp, utc=True, errors="coerce")
    start_ts = pd.to_datetime(start_text, utc=True, errors="coerce")
    if pd.isna(signal_ts) or pd.isna(start_ts):
        return False
    return bool(signal_ts >= start_ts)


def _risk(candidate: MovingSLCandidate) -> float:
    return abs(candidate.entry - candidate.original_sl)


def _geometry_multiple(candidate: MovingSLCandidate, price: float, *, favorable: bool) -> float | None:
    risk = _risk(candidate)
    if risk <= 0:
        return None
    if candidate.side == "LONG":
        raw = (price - candidate.entry) / risk if favorable else (candidate.entry - price) / risk
    else:
        raw = (candidate.entry - price) / risk if favorable else (price - candidate.entry) / risk
    return float(raw)


def _touches(candidate: MovingSLCandidate, candle: pd.Series) -> tuple[bool, bool, bool]:
    high = float(candle["high"])
    low = float(candle["low"])
    if candidate.side == "LONG":
        return low <= candidate.original_sl, high >= candidate.tp1, high >= candidate.tp2
    return high >= candidate.original_sl, low <= candidate.tp1, low <= candidate.tp2


def _be_or_tp2(candidate: MovingSLCandidate, candle: pd.Series) -> tuple[bool, bool]:
    high = float(candle["high"])
    low = float(candle["low"])
    if candidate.side == "LONG":
        return low <= candidate.entry, high >= candidate.tp2
    return high >= candidate.entry, low <= candidate.tp2


def _after_tp1_extremes(candidate: MovingSLCandidate, candles: pd.DataFrame) -> tuple[float | None, float | None, float | None, float | None]:
    if candles.empty:
        return None, None, None, None
    if candidate.side == "LONG":
        mfe_price = safe_float(candles["high"].max())
        mae_price = safe_float(candles["low"].min())
    else:
        mfe_price = safe_float(candles["low"].min())
        mae_price = safe_float(candles["high"].max())
    return (
        mfe_price,
        _geometry_multiple(candidate, mfe_price, favorable=True) if mfe_price is not None else None,
        mae_price,
        _geometry_multiple(candidate, mae_price, favorable=False) if mae_price is not None else None,
    )


def evaluate_lifecycle(candidate: MovingSLCandidate, candles: pd.DataFrame) -> MovingSLLifecycle:
    if candles is None or candles.empty:
        return MovingSLLifecycle(
            False, "", "", "", None, False, "", False, "", "DATA_INSUFFICIENT",
            None, None, None, None, None, None, False, "missing_candle_data",
        )
    required = {"high", "low", "close_time"}
    if not required.issubset(candles.columns):
        return MovingSLLifecycle(
            False, "", "", "", None, False, "", False, "", "DATA_INSUFFICIENT",
            None, None, None, None, None, None, False, "missing_required_candle_columns",
        )

    ordered = candles.copy()
    if "open_time" in ordered.columns:
        ordered = ordered.sort_values("open_time")
    ordered = ordered.reset_index(drop=True)

    for idx, candle in ordered.iterrows():
        close_time = pd.to_datetime(candle.get("close_time"), utc=True, errors="coerce")
        close_text = "" if pd.isna(close_time) else close_time.isoformat()
        sl_hit, tp1_hit, _tp2_hit = _touches(candidate, candle)
        if sl_hit and tp1_hit:
            return MovingSLLifecycle(
                True, close_text, "MOVE_SL_TO_BE", close_text, candidate.entry,
                False, "", False, "", "AMBIGUOUS", None, None, None, None,
                None, None, True, "sl_and_tp1_same_candle_before_shadow_action",
            )
        if sl_hit:
            return MovingSLLifecycle(
                False, "", "", "", None, False, "", False, "", "SL_BEFORE_TP1",
                None, None, None, None, None, None, False, "",
            )
        if not tp1_hit:
            continue

        tp1_at = close_text
        remaining = ordered.iloc[int(idx):].copy()
        mfe_price, mfe_r, mae_price, mae_r = _after_tp1_extremes(candidate, remaining)
        for next_idx, next_candle in remaining.iterrows():
            next_close = pd.to_datetime(next_candle.get("close_time"), utc=True, errors="coerce")
            next_close_text = "" if pd.isna(next_close) else next_close.isoformat()
            be_hit, tp2_hit = _be_or_tp2(candidate, next_candle)
            same_candle = int(next_idx) == int(idx)
            if be_hit and tp2_hit:
                return MovingSLLifecycle(
                    True, tp1_at, "MOVE_SL_TO_BE", tp1_at, candidate.entry,
                    True, next_close_text, True, next_close_text, "AMBIGUOUS",
                    mfe_price, mfe_r, mae_price, mae_r,
                    _minutes_between(tp1_at, next_close_text),
                    _minutes_between(tp1_at, next_close_text),
                    True,
                    "be_and_tp2_same_candle_after_tp1",
                )
            if be_hit:
                return MovingSLLifecycle(
                    True, tp1_at, "MOVE_SL_TO_BE", tp1_at, candidate.entry,
                    True, next_close_text, False, "", "BE_BEFORE_TP2",
                    mfe_price, mfe_r, mae_price, mae_r,
                    _minutes_between(tp1_at, next_close_text), None, same_candle,
                    "be_same_candle_as_tp1" if same_candle else "",
                )
            if tp2_hit:
                return MovingSLLifecycle(
                    True, tp1_at, "MOVE_SL_TO_BE", tp1_at, candidate.entry,
                    False, "", True, next_close_text, "TP2_BEFORE_BE",
                    mfe_price, mfe_r, mae_price, mae_r,
                    None, _minutes_between(tp1_at, next_close_text), same_candle,
                    "tp2_same_candle_as_tp1" if same_candle else "",
                )
        return MovingSLLifecycle(
            True, tp1_at, "MOVE_SL_TO_BE", tp1_at, candidate.entry,
            False, "", False, "", "TP1_REACHED_UNRESOLVED",
            mfe_price, mfe_r, mae_price, mae_r, None, None, False, "",
        )

    return MovingSLLifecycle(
        False, "", "", "", None, False, "", False, "", "DATA_INSUFFICIENT",
        None, None, None, None, None, None, False, "no_tp1_or_sl_within_candle_window",
    )


def evaluate_continuation_after_tp1(
    candidate: MovingSLCandidate,
    candles: pd.DataFrame,
    tp1_reached_at: str,
) -> MovingSLLifecycle:
    tp1_at = normalize_timestamp(tp1_reached_at)
    if not tp1_at:
        return evaluate_lifecycle(candidate, candles)
    if candles is None or candles.empty:
        return MovingSLLifecycle(
            True, tp1_at, "MOVE_SL_TO_BE", tp1_at, candidate.entry,
            False, "", False, "", "TP1_REACHED_UNRESOLVED",
            None, None, None, None, None, None, False, "missing_continuation_candle_data",
        )
    required = {"high", "low", "close_time"}
    if not required.issubset(candles.columns):
        return MovingSLLifecycle(
            True, tp1_at, "MOVE_SL_TO_BE", tp1_at, candidate.entry,
            False, "", False, "", "DATA_INSUFFICIENT",
            None, None, None, None, None, None, False, "missing_required_candle_columns",
        )

    ordered = candles.copy()
    if "open_time" in ordered.columns:
        ordered = ordered.sort_values("open_time")
    ordered = ordered.reset_index(drop=True)
    mfe_price, mfe_r, mae_price, mae_r = _after_tp1_extremes(candidate, ordered)
    for _, candle in ordered.iterrows():
        close_time = pd.to_datetime(candle.get("close_time"), utc=True, errors="coerce")
        close_text = "" if pd.isna(close_time) else close_time.isoformat()
        be_hit, tp2_hit = _be_or_tp2(candidate, candle)
        if be_hit and tp2_hit:
            return MovingSLLifecycle(
                True, tp1_at, "MOVE_SL_TO_BE", tp1_at, candidate.entry,
                True, close_text, True, close_text, "AMBIGUOUS",
                mfe_price, mfe_r, mae_price, mae_r,
                _minutes_between(tp1_at, close_text),
                _minutes_between(tp1_at, close_text),
                True,
                "be_and_tp2_same_candle_after_tp1",
            )
        if be_hit:
            return MovingSLLifecycle(
                True, tp1_at, "MOVE_SL_TO_BE", tp1_at, candidate.entry,
                True, close_text, False, "", "BE_BEFORE_TP2",
                mfe_price, mfe_r, mae_price, mae_r,
                _minutes_between(tp1_at, close_text), None, False, "",
            )
        if tp2_hit:
            return MovingSLLifecycle(
                True, tp1_at, "MOVE_SL_TO_BE", tp1_at, candidate.entry,
                False, "", True, close_text, "TP2_BEFORE_BE",
                mfe_price, mfe_r, mae_price, mae_r,
                None, _minutes_between(tp1_at, close_text), False, "",
            )
    return MovingSLLifecycle(
        True, tp1_at, "MOVE_SL_TO_BE", tp1_at, candidate.entry,
        False, "", False, "", "TP1_REACHED_UNRESOLVED",
        mfe_price, mfe_r, mae_price, mae_r, None, None, False, "",
    )


def candidate_from_row(row: pd.Series, prospective_start_utc: str = "") -> MovingSLCandidate | None:
    status = _text(row.get("signal_status", "sent")).lower() or "sent"
    if status != "sent":
        return None
    timestamp = normalize_timestamp(row.get("timestamp"))
    if not timestamp_on_or_after_prospective_start(timestamp, prospective_start_utc):
        return None
    symbol = normalize_symbol(row.get("symbol"))
    side = normalize_side(row.get("side", row.get("direction", "")))
    entry = safe_float(row.get("entry"))
    stop = safe_float(row.get("stop_loss", row.get("sl")))
    tp1 = safe_float(row.get("tp1"))
    tp2 = safe_float(row.get("tp2"))
    if not timestamp or not symbol or side not in {"LONG", "SHORT"}:
        return None
    if entry is None or stop is None or tp1 is None or tp2 is None:
        return None
    if min(entry, stop, tp1, tp2) <= 0:
        return None
    key = canonical_signal_key(symbol=symbol, side=side, timestamp=timestamp, entry=entry)
    if not key:
        return None
    return MovingSLCandidate(
        canonical_signal_key=key,
        timestamp_utc=timestamp,
        symbol=symbol,
        side=side,
        entry=entry,
        original_sl=stop,
        tp1=tp1,
        tp2=tp2,
        signal_status=status,
        original_result=_text(row.get("result", "")),
        original_hit_target=_text(row.get("hit_target", "")),
        original_closed_at=normalize_timestamp(row.get("closed_at")) or _text(row.get("closed_at", "")),
        market_session=_text(row.get("market_session", row.get("session", ""))),
        watchlist_tier=_text(row.get("watchlist_tier", row.get("tier", ""))),
        setup_strength=_text(row.get("setup_strength", "")),
        score=_text(row.get("score", row.get("raw_score", ""))),
        confidence=_text(row.get("confidence", "")),
        sr_class=_text(row.get("sr_class", row.get("sr_gate_decision", ""))),
        market_exhaustion_class=_text(row.get("market_exhaustion_class", row.get("exhaustion_class", ""))),
        entry_timing_class=_text(row.get("entry_timing_class", row.get("entry_timing_recommendation", ""))),
        btc_regime=_text(row.get("btc_regime", "")),
    )


def load_candidates(signals_path: Path, prospective_start_utc: str = "") -> list[MovingSLCandidate]:
    return unique_candidates_from_frame(read_signals_csv(signals_path), prospective_start_utc)


def read_signals_csv(signals_path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(signals_path, low_memory=False)
    except (FileNotFoundError, pd.errors.EmptyDataError):
        return pd.DataFrame()


def sent_rows_total(data: pd.DataFrame) -> int:
    if data.empty:
        return 0
    status = data.get("signal_status", pd.Series(["sent"] * len(data), index=data.index))
    return int(status.fillna("").astype(str).str.strip().str.lower().replace("", "sent").eq("sent").sum())


def prospective_sent_rows_total(data: pd.DataFrame, prospective_start_utc: str = "") -> int:
    if data.empty:
        return 0
    count = 0
    for _, row in data.iterrows():
        status = _text(row.get("signal_status", "sent")).lower() or "sent"
        if status != "sent":
            continue
        timestamp = normalize_timestamp(row.get("timestamp"))
        if timestamp_on_or_after_prospective_start(timestamp, prospective_start_utc):
            count += 1
    return count


def unique_candidates_from_frame(data: pd.DataFrame, prospective_start_utc: str = "") -> list[MovingSLCandidate]:
    candidates: list[MovingSLCandidate] = []
    seen: set[str] = set()
    if data.empty:
        return candidates
    for _, row in data.iterrows():
        candidate = candidate_from_row(row, prospective_start_utc)
        if candidate is None or candidate.canonical_signal_key in seen:
            continue
        seen.add(candidate.canonical_signal_key)
        candidates.append(candidate)
    return candidates


def build_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_futures_klines(session: requests.Session, symbol: str, start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> pd.DataFrame:
    response = session.get(
        BINANCE_FUTURES_KLINES,
        params={
            "symbol": normalize_symbol(symbol),
            "interval": TIMEFRAME,
            "startTime": int(start_ts.timestamp() * 1000),
            "endTime": int(end_ts.timestamp() * 1000),
            "limit": 1000,
        },
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if not data:
        return pd.DataFrame()
    candles = pd.DataFrame(
        data,
        columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_asset_volume", "num_trades",
            "taker_buy_base_volume", "taker_buy_quote_volume", "ignore",
        ],
    )
    for column in ["open", "high", "low", "close"]:
        candles[column] = pd.to_numeric(candles[column], errors="coerce")
    candles["open_time"] = pd.to_datetime(candles["open_time"], unit="ms", utc=True, errors="coerce")
    candles["close_time"] = pd.to_datetime(candles["close_time"], unit="ms", utc=True, errors="coerce")
    return candles.dropna(subset=["open", "high", "low", "close", "close_time"]).sort_values("open_time").reset_index(drop=True)


def fetch_futures_klines_range(session: requests.Session, symbol: str, start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> pd.DataFrame:
    """Fetch closed 15m futures candles with pagination.

    Binance returns at most 1000 klines per request. This function walks forward
    by open time so multi-day unresolved signals are not silently truncated.
    """
    start = pd.to_datetime(start_ts, utc=True, errors="coerce")
    end = pd.to_datetime(end_ts, utc=True, errors="coerce")
    if pd.isna(start) or pd.isna(end) or end <= start:
        return pd.DataFrame()
    now = pd.Timestamp.now(tz="UTC")
    end = min(end, now)
    frames: list[pd.DataFrame] = []
    cursor_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    while cursor_ms < end_ms:
        chunk_start = pd.to_datetime(cursor_ms, unit="ms", utc=True)
        chunk = fetch_futures_klines(session, symbol, chunk_start, end)
        if chunk.empty:
            break
        closed = chunk[chunk["close_time"] <= now].copy()
        if closed.empty:
            break
        frames.append(closed)
        last_open_ms = int(pd.to_datetime(closed["open_time"].iloc[-1], utc=True).timestamp() * 1000)
        next_ms = last_open_ms + BINANCE_15M_MS
        if next_ms <= cursor_ms:
            break
        cursor_ms = next_ms
        if cursor_ms > end_ms:
            break
        time.sleep(0.05)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)


def candles_for_candidate(
    candidate: MovingSLCandidate,
    session: requests.Session,
    lookahead_hours: int = 0,
    start_timestamp_utc: str = "",
) -> pd.DataFrame:
    start_source = normalize_timestamp(start_timestamp_utc) or candidate.timestamp_utc
    start = pd.to_datetime(start_source, utc=True, errors="coerce")
    if pd.isna(start):
        return pd.DataFrame()
    now = pd.Timestamp.now(tz="UTC")
    end = now if lookahead_hours <= 0 else min(start + pd.Timedelta(hours=lookahead_hours), now)
    return fetch_futures_klines_range(session, candidate.symbol, start, end)


def build_record(
    candidate: MovingSLCandidate,
    lifecycle: MovingSLLifecycle,
    prospective_start_utc: str = "",
    generated_at_utc: str | None = None,
) -> dict[str, Any]:
    generated_at = generated_at_utc or utc_now_text()
    return {
        "canonical_signal_key": candidate.canonical_signal_key,
        "timestamp_utc": candidate.timestamp_utc,
        "symbol": candidate.symbol,
        "side": candidate.side,
        "entry": _format_number(candidate.entry),
        "original_sl": _format_number(candidate.original_sl),
        "tp1": _format_number(candidate.tp1),
        "tp2": _format_number(candidate.tp2),
        "signal_status": candidate.signal_status,
        "original_result": candidate.original_result,
        "original_hit_target": candidate.original_hit_target,
        "original_closed_at": candidate.original_closed_at,
        "tp1_reached": int(lifecycle.tp1_reached),
        "tp1_reached_at": lifecycle.tp1_reached_at,
        "shadow_action": lifecycle.shadow_action,
        "shadow_action_at": lifecycle.shadow_action_at,
        "be_price": _format_number(lifecycle.be_price),
        "be_reached_after_tp1": int(lifecycle.be_reached_after_tp1),
        "be_reached_at": lifecycle.be_reached_at,
        "tp2_reached_after_tp1": int(lifecycle.tp2_reached_after_tp1),
        "tp2_reached_at": lifecycle.tp2_reached_at,
        "lifecycle_class": lifecycle.lifecycle_class,
        "mfe_after_tp1_price": _format_number(lifecycle.mfe_after_tp1_price),
        "mfe_after_tp1_r_multiple_if_geometry_only": _format_number(lifecycle.mfe_after_tp1_r_multiple_if_geometry_only),
        "mae_after_tp1_price": _format_number(lifecycle.mae_after_tp1_price),
        "mae_after_tp1_r_multiple_if_geometry_only": _format_number(lifecycle.mae_after_tp1_r_multiple_if_geometry_only),
        "minutes_tp1_to_be": _format_minutes(lifecycle.minutes_tp1_to_be),
        "minutes_tp1_to_tp2": _format_minutes(lifecycle.minutes_tp1_to_tp2),
        "ambiguous_same_candle": int(lifecycle.ambiguous_same_candle),
        "data_quality_notes": lifecycle.data_quality_notes,
        "market_session": candidate.market_session,
        "watchlist_tier": candidate.watchlist_tier,
        "setup_strength": candidate.setup_strength,
        "score": candidate.score,
        "confidence": candidate.confidence,
        "sr_class": candidate.sr_class,
        "market_exhaustion_class": candidate.market_exhaustion_class,
        "entry_timing_class": candidate.entry_timing_class,
        "btc_regime": candidate.btc_regime,
        "shadow_version": SHADOW_VERSION,
        "prospective_start_timestamp_utc": normalize_timestamp(prospective_start_utc),
        "generated_at_utc": generated_at,
    }


class MovingSLProspectiveShadowStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> pd.DataFrame:
        if not self.path.exists():
            return pd.DataFrame(columns=FIELDNAMES)
        try:
            data = pd.read_csv(self.path, low_memory=False)
        except pd.errors.EmptyDataError:
            return pd.DataFrame(columns=FIELDNAMES)
        for column in FIELDNAMES:
            if column not in data.columns:
                data[column] = ""
        return data[FIELDNAMES].copy()

    def upsert(self, records: list[dict[str, Any]], dry_run: bool = False) -> pd.DataFrame:
        existing = self.read()
        if not records:
            return existing
        incoming = pd.DataFrame(records)
        for column in FIELDNAMES:
            if column not in incoming.columns:
                incoming[column] = ""
        combined = pd.concat([existing, incoming[FIELDNAMES]], ignore_index=True)
        lifecycle = combined["lifecycle_class"].fillna("").astype(str).str.upper()
        combined["_state_rank"] = lifecycle.map(lambda value: 2 if value in TERMINAL_LIFECYCLE_CLASSES else 0 if value == "DATA_INSUFFICIENT" else 1)
        combined["_row_order"] = range(len(combined))
        combined = (
            combined.sort_values(["canonical_signal_key", "_state_rank", "_row_order"])
            .drop_duplicates("canonical_signal_key", keep="last")
            .drop(columns=["_state_rank", "_row_order"])
        )
        if not dry_run:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("w", newline="", encoding="utf-8") as handle:
                combined[FIELDNAMES].to_csv(handle, index=False)
                handle.flush()
                os.fsync(handle.fileno())
        return combined[FIELDNAMES]

    def existing_state_by_key(self) -> dict[str, str]:
        data = self.read()
        if data.empty:
            return {}
        return {
            str(row.get("canonical_signal_key", "")).strip(): str(row.get("lifecycle_class", "")).strip().upper()
            for _, row in data.iterrows()
            if str(row.get("canonical_signal_key", "")).strip()
        }

    def existing_record_by_key(self) -> dict[str, dict[str, Any]]:
        data = self.read()
        if data.empty:
            return {}
        records: dict[str, dict[str, Any]] = {}
        for _, row in data.iterrows():
            key = str(row.get("canonical_signal_key", "")).strip()
            if key:
                records[key] = row.to_dict()
        return records


def candidates_for_collection(
    candidates: list[MovingSLCandidate],
    existing_state_by_key: dict[str, str],
) -> list[MovingSLCandidate]:
    selected: list[MovingSLCandidate] = []
    for candidate in candidates:
        state = existing_state_by_key.get(candidate.canonical_signal_key, "").upper()
        if state in TERMINAL_LIFECYCLE_CLASSES:
            continue
        if state and state not in RECHECKABLE_LIFECYCLE_CLASSES:
            continue
        selected.append(candidate)
    return selected


def observe_candidates(
    candidates: list[MovingSLCandidate],
    *,
    session: requests.Session | None = None,
    lookahead_hours: int = 0,
    candle_provider: Any | None = None,
    prospective_start_utc: str = "",
    existing_record_by_key: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    http = session or build_session()
    generated_at = utc_now_text()
    existing_records = existing_record_by_key or {}
    for candidate in candidates:
        try:
            existing = existing_records.get(candidate.canonical_signal_key, {})
            existing_lifecycle = str(existing.get("lifecycle_class", "")).strip().upper()
            tp1_reached_at = normalize_timestamp(existing.get("tp1_reached_at", ""))
            continuation_only = existing_lifecycle == "TP1_REACHED_UNRESOLVED" and bool(tp1_reached_at)
            if candle_provider:
                candles = candle_provider(candidate)
            else:
                candles = candles_for_candidate(
                    candidate,
                    http,
                    lookahead_hours,
                    start_timestamp_utc=tp1_reached_at if continuation_only else "",
                )
            lifecycle = (
                evaluate_continuation_after_tp1(candidate, candles, tp1_reached_at)
                if continuation_only
                else evaluate_lifecycle(candidate, candles)
            )
            records.append(build_record(candidate, lifecycle, prospective_start_utc, generated_at))
            if candle_provider is None:
                time.sleep(0.05)
        except Exception as exc:
            records.append(
                build_record(
                    candidate,
                    MovingSLLifecycle(
                        False, "", "", "", None, False, "", False, "", "DATA_INSUFFICIENT",
                        None, None, None, None, None, None, False, f"observer_error:{exc}",
                    ),
                    prospective_start_utc,
                    generated_at,
                )
            )
    return records


def _bool_series(data: pd.DataFrame, column: str) -> pd.Series:
    if column not in data.columns:
        return pd.Series([False] * len(data), index=data.index)
    return data[column].fillna("").astype(str).str.strip().str.lower().isin({"1", "true", "yes", "y"})


def _setup_bucket(value: Any) -> str:
    numeric = safe_float(value)
    if numeric is None:
        return "UNKNOWN"
    return "<=79" if numeric <= 79 else ">=80"


def _group_counts(data: pd.DataFrame, column: str) -> list[list[Any]]:
    if data.empty or column not in data.columns:
        return []
    rows: list[list[Any]] = []
    for key, group in data.groupby(data[column].fillna("UNKNOWN").replace("", "UNKNOWN").astype(str), dropna=False):
        tp1 = int(_bool_series(group, "tp1_reached").sum())
        tp2 = int((group["lifecycle_class"].astype(str).str.upper() == "TP2_BEFORE_BE").sum())
        be = int((group["lifecycle_class"].astype(str).str.upper() == "BE_BEFORE_TP2").sum())
        rows.append([key, int(len(group)), tp1, tp2, be, f"{(tp2 / tp1 * 100):.1f}%" if tp1 else "N/A"])
    return sorted(rows, key=lambda row: str(row[0]))


def _month(value: Any) -> str:
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    return "UNKNOWN" if pd.isna(ts) else ts.strftime("%Y-%m")


def _markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "_No rows._"
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(value) for value in row) + " |")
    return "\n".join(lines)


def build_report(records: pd.DataFrame) -> str:
    if records.empty:
        return "Prospective Moving-SL Shadow V1\nN/A: no observed sent-signal lifecycle rows yet."
    data = records.copy()
    data["lifecycle_class"] = data["lifecycle_class"].fillna("").astype(str).str.upper()
    total = int(len(data))
    tp1_reached = int(_bool_series(data, "tp1_reached").sum())
    sl_before = int((data["lifecycle_class"] == "SL_BEFORE_TP1").sum())
    tp1_pool = data[_bool_series(data, "tp1_reached")].copy()
    tp2_before_be = int((tp1_pool["lifecycle_class"] == "TP2_BEFORE_BE").sum()) if not tp1_pool.empty else 0
    be_before_tp2 = int((tp1_pool["lifecycle_class"] == "BE_BEFORE_TP2").sum()) if not tp1_pool.empty else 0
    unresolved = int((tp1_pool["lifecycle_class"] == "TP1_REACHED_UNRESOLVED").sum()) if not tp1_pool.empty else 0
    ambiguous = int((data["lifecycle_class"] == "AMBIGUOUS").sum())
    insufficient = int((data["lifecycle_class"] == "DATA_INSUFFICIENT").sum())
    resolved_tp1 = tp2_before_be + be_before_tp2 + int((tp1_pool["lifecycle_class"] == "AMBIGUOUS").sum()) if not tp1_pool.empty else 0
    data["setup_strength_bucket"] = data["setup_strength"].map(_setup_bucket)
    data["month"] = data["timestamp_utc"].map(_month)

    lines = [
        "Prospective Moving-SL Shadow V1",
        "================================",
        "",
        "Research only. No Net R improvement is reported.",
        "",
        f"Total observed SENT signals: {total}",
        f"TP1 reached: {tp1_reached}",
        f"Resolved TP1 population: {resolved_tp1}",
        f"SL before TP1: {sl_before}",
        "",
        "Among TP1 reached:",
        f"- TP2 before BE: {tp2_before_be}",
        f"- BE before TP2: {be_before_tp2}",
        f"- unresolved: {unresolved}",
        f"- ambiguous: {ambiguous}",
        f"- data insufficient: {insufficient}",
        "",
        f"TP2-before-BE rate: {(tp2_before_be / tp1_reached * 100):.1f}%" if tp1_reached else "TP2-before-BE rate: N/A",
        f"BE-before-TP2 rate: {(be_before_tp2 / tp1_reached * 100):.1f}%" if tp1_reached else "BE-before-TP2 rate: N/A",
        "Rate denominator: TP1 reached population.",
        "",
        "Prospective gate: KEEP RESEARCH",
        "Target: at least 100 NEW prospective TP1-reached observations.",
        "",
        "Breakdown: LONG / SHORT",
        _markdown_table(["Side", "Observed", "TP1", "TP2 before BE", "BE before TP2", "TP2/TP1"], _group_counts(data, "side")),
        "",
        "Breakdown: Market Session",
        _markdown_table(["Session", "Observed", "TP1", "TP2 before BE", "BE before TP2", "TP2/TP1"], _group_counts(data, "market_session")),
        "",
        "Breakdown: Setup Strength",
        _markdown_table(["Setup Strength", "Observed", "TP1", "TP2 before BE", "BE before TP2", "TP2/TP1"], _group_counts(data, "setup_strength_bucket")),
        "",
        "Breakdown: Tier",
        _markdown_table(["Tier", "Observed", "TP1", "TP2 before BE", "BE before TP2", "TP2/TP1"], _group_counts(data, "watchlist_tier")),
        "",
        "Breakdown: Month",
        _markdown_table(["Month", "Observed", "TP1", "TP2 before BE", "BE before TP2", "TP2/TP1"], _group_counts(data, "month")),
        "",
        "Minimum sample warning: use for monitoring only until the TP1-reached sample is large enough.",
    ]
    return "\n".join(lines)


def run_shadow(
    signals_path: Path,
    output_path: Path,
    *,
    prospective_start_utc: str = "",
    lookahead_hours: int = 0,
    dry_run: bool = False,
    limit: int = 0,
    candle_provider: Any | None = None,
) -> pd.DataFrame:
    data, _summary = collect_shadow(
        signals_path,
        output_path,
        prospective_start_utc=prospective_start_utc,
        lookahead_hours=lookahead_hours,
        dry_run=dry_run,
        limit=limit,
        candle_provider=candle_provider,
    )
    return data


def collect_shadow(
    signals_path: Path,
    output_path: Path,
    *,
    prospective_start_utc: str = "",
    lookahead_hours: int = 0,
    dry_run: bool = False,
    limit: int = 0,
    candle_provider: Any | None = None,
    session: requests.Session | None = None,
) -> tuple[pd.DataFrame, MovingSLCollectionSummary]:
    started = time.perf_counter()
    normalized_start = normalize_timestamp(prospective_start_utc)
    signals = read_signals_csv(signals_path)
    store = MovingSLProspectiveShadowStore(output_path)
    candidates = unique_candidates_from_frame(signals, normalized_start)
    existing_state = store.existing_state_by_key()
    candidates_to_evaluate = candidates_for_collection(candidates, existing_state)
    terminal_skipped = len(candidates) - len(candidates_to_evaluate)
    if limit > 0:
        candidates_to_evaluate = candidates_to_evaluate[:limit]

    counting_session: CountingSession | requests.Session | None = session
    if candidates_to_evaluate and candle_provider is None and session is None:
        counting_session = CountingSession(build_session())

    records: list[dict[str, Any]] = []
    if candidates_to_evaluate:
        records = observe_candidates(
            candidates_to_evaluate,
            session=counting_session,
            lookahead_hours=lookahead_hours,
            candle_provider=candle_provider,
            prospective_start_utc=normalized_start,
            existing_record_by_key=store.existing_record_by_key(),
        )
    data = store.upsert(records, dry_run=dry_run)
    request_count: int | str = 0
    if candidates_to_evaluate and candle_provider is not None:
        request_count = "custom_provider"
    elif counting_session is not None and hasattr(counting_session, "request_count"):
        request_count = int(getattr(counting_session, "request_count", 0))
    summary = MovingSLCollectionSummary(
        shadow_enabled=env_bool("MOVING_SL_SHADOW_ENABLED", True),
        live_enabled=env_bool("MOVING_SL_LIVE_ENABLED", False),
        prospective_start_utc=normalized_start,
        sent_rows_total=sent_rows_total(signals),
        prospective_sent_rows=prospective_sent_rows_total(signals, normalized_start),
        valid_prospective_candidates=len(candidates),
        candidates_needing_candle_evaluation=len(candidates_to_evaluate),
        terminal_rows_skipped=terminal_skipped,
        binance_request_count=request_count,
        output_rows=len(data),
        elapsed_seconds=time.perf_counter() - started,
        dry_run=dry_run,
    )
    return data, summary


def format_collection_summary(summary: MovingSLCollectionSummary) -> str:
    live_mode = "true (NO-OP)" if summary.live_enabled else "false"
    return "\n".join(
        [
            "Moving-SL Prospective Shadow Run",
            "================================",
            f"shadow enabled: {str(summary.shadow_enabled).lower()}",
            f"live enabled: {live_mode}",
            f"prospective start UTC: {summary.prospective_start_utc or 'N/A'}",
            f"sent rows total: {summary.sent_rows_total}",
            f"prospective sent rows: {summary.prospective_sent_rows}",
            f"valid prospective candidates: {summary.valid_prospective_candidates}",
            f"candidates needing candle evaluation: {summary.candidates_needing_candle_evaluation}",
            f"terminal rows skipped: {summary.terminal_rows_skipped}",
            f"Binance request count: {summary.binance_request_count}",
            f"output rows: {summary.output_rows}",
            f"dry run: {str(summary.dry_run).lower()}",
            f"elapsed seconds: {summary.elapsed_seconds:.3f}",
        ]
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Observe TP1-to-breakeven lifecycle paths for production sent signals.")
    parser.add_argument("--signals", type=Path, default=Path("logs/signals.csv"))
    parser.add_argument("--output", type=Path, default=Path("logs/moving_sl_prospective_shadow.csv"))
    parser.add_argument("--lookahead-hours", type=int, default=0, help="0 means track from signal timestamp through the current closed candle.")
    parser.add_argument("--prospective-start-utc", default=os.getenv("MOVING_SL_PROSPECTIVE_START_UTC", ""))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--run", action="store_true", help="Collect/update the prospective shadow CSV.")
    parser.add_argument("--dry-run", action="store_true", help="Build records without writing the shadow CSV.")
    parser.add_argument("--report", action="store_true", help="Print the existing shadow lifecycle report without collecting.")
    return parser.parse_args()


def main() -> int:
    load_project_env()
    args = parse_args()
    if not env_bool("MOVING_SL_SHADOW_ENABLED", True):
        print("Prospective Moving-SL Shadow V1 disabled by MOVING_SL_SHADOW_ENABLED")
        return 0
    if env_bool("MOVING_SL_LIVE_ENABLED", False):
        print("MOVING_SL_LIVE_ENABLED is ignored in V1. This module remains research-only.")
    if args.report:
        print(build_report(MovingSLProspectiveShadowStore(args.output).read()))
        return 0
    if args.run:
        _data, summary = collect_shadow(
            args.signals,
            args.output,
            prospective_start_utc=args.prospective_start_utc,
            lookahead_hours=args.lookahead_hours,
            dry_run=args.dry_run,
            limit=args.limit,
        )
        print(format_collection_summary(summary))
        return 0
    print("No action selected. Use --run to collect/update or --report to print the existing report.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
