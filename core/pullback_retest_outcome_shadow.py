# -*- coding: utf-8 -*-
"""Pullback / Retest Outcome Shadow V1.

Analytics-only research module. It simulates deterministic retest entries for
historical scanner candidates and never changes production signal approval,
scoring, routing, TP/SL, RR, Telegram, Cornix, or scanner runtime behavior.
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.signal_identity import canonical_signal_key, normalize_float, normalize_side, normalize_symbol, normalize_timestamp


LOGGER = logging.getLogger("pullback_retest_outcome_shadow")

BINANCE_FUTURES_KLINES = "https://fapi.binance.com/fapi/v1/klines"
TIMEFRAME = "15m"
DEFAULT_LOOKAHEAD_HOURS = 24
DEFAULT_WAIT_WINDOWS = [45, 90, 180]
STRATEGIES = [
    "ATR_PULLBACK_0_30",
    "ATR_PULLBACK_0_50",
    "ATR_PULLBACK_0_75",
    "EMA_RETEST",
    "BREAKOUT_RETEST",
    "SR_RETEST",
]
MODELS = ["ORIGINAL_STRUCTURE", "PRESERVED_RISK_RR"]

FIELDNAMES = [
    "dedupe_key",
    "canonical_signal_key",
    "timestamp_utc",
    "symbol",
    "side",
    "source_population",
    "market_session",
    "model",
    "original_entry",
    "original_sl",
    "original_tp1",
    "original_tp2",
    "original_outcome",
    "original_r",
    "atr",
    "strategy",
    "wait_window_minutes",
    "target_retest_entry",
    "retest_status",
    "retest_fill_timestamp",
    "retest_entry",
    "effective_rr_at_retest",
    "retest_outcome",
    "retest_r",
    "resolution_timestamp",
    "r_delta_vs_original",
    "sl_avoided",
    "winner_missed",
    "loser_to_winner",
    "sr_class",
    "opposing_distance_atr",
    "effective_sr_rr",
    "entry_timing_class",
    "exhaustion_class",
    "swing_distance_atr",
    "ema20_distance_atr",
    "ema50_distance_atr",
    "directional_run",
    "atr_expansion_ratio",
    "prior_24h_move_atr",
    "prior_session_move_atr",
    "prior_day_known_net_r",
    "pre_retest_mae_atr",
    "pre_retest_mfe_atr",
    "generated_at_utc",
]

SENT_STATUSES = {"sent", "tier_c_report_only", "weak_symbol_report_only", "session_risk_report_only", "london_long_report_only"}
REJECTED_STATUSES = {
    "logged_quality_filter",
    "skipped_btc_regime",
    "skipped_correlation",
    "skipped_daily_risk_guard",
    "skipped_loss_cooldown",
    "skipped_not_top_candidate",
    "skipped",
}


@dataclass(frozen=True)
class Candidate:
    canonical_signal_key: str
    timestamp_utc: str
    symbol: str
    side: str
    source_population: str
    entry: float
    sl: float
    tp1: float
    tp2: float
    rr: float
    score: float
    confidence: float
    session: str
    original_outcome: str
    original_r: float
    atr: float = 0.0
    support: float = 0.0
    resistance: float = 0.0
    sr_class: str = ""
    opposing_distance_atr: float = math.nan
    effective_sr_rr: float = math.nan
    entry_timing_class: str = ""
    exhaustion_class: str = ""
    swing_distance_atr: float = math.nan
    ema20_distance_atr: float = math.nan
    ema50_distance_atr: float = math.nan
    directional_run: float = math.nan
    atr_expansion_ratio: float = math.nan
    prior_day_known_net_r: float = math.nan


@dataclass(frozen=True)
class StrategyTarget:
    strategy: str
    target_entry: float
    status: str
    reason: str = ""


@dataclass(frozen=True)
class RetestResult:
    status: str
    fill_timestamp: str = ""
    fill_index: int = -1
    pre_mae_atr: float = math.nan
    pre_mfe_atr: float = math.nan


@dataclass(frozen=True)
class OutcomeResult:
    outcome: str
    hit_target: str
    r_value: float
    resolution_timestamp: str


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def safe_float(value: Any, default: float = math.nan) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def first_value(row: Any, names: Iterable[str], default: Any = "") -> Any:
    for name in names:
        value = row.get(name, "") if isinstance(row, dict) else getattr(row, name, "")
        text = str(value or "").strip()
        if text and text.lower() not in {"nan", "none", "null", "nat"}:
            return value
    return default


def first_series(data: pd.DataFrame, names: list[str], default: Any = "") -> pd.Series:
    out = pd.Series([""] * len(data), index=data.index, dtype="object")
    for name in names:
        if name not in data.columns:
            continue
        values = data[name].where(~data[name].isna(), "")
        mask = out.fillna("").astype(str).str.strip().isin(["", "nan", "none", "null", "nat"])
        out = out.where(~mask, values)
    out = out.fillna("")
    if default != "":
        out = out.replace("", default)
    return out


def hit_level(value: Any) -> int:
    text = str(value or "").strip().upper()
    if text.startswith("WIN_"):
        text = text[4:]
    if text.startswith("TP"):
        text = text[2:]
    try:
        return max(0, int(float(text)))
    except (TypeError, ValueError):
        return 0


def original_r_from_row(row: pd.Series) -> float:
    result = str(row.get("result", "") or "").strip().upper()
    if result == "LOSS":
        return -1.0
    if result != "WIN":
        return 0.0
    rr = safe_float(first_value(row, ["risk_reward", "rr", "original_rr"]), 0.0)
    level = hit_level(first_value(row, ["hit_target", "outcome"]))
    if level >= 3:
        return rr if rr > 0 else 3.0
    if level >= 2:
        return rr if rr > 0 else 2.0
    return min(rr, 1.2) if rr > 0 else 1.0


def result_from_r(value: float) -> str:
    if value > 0:
        return "WIN"
    if value < 0:
        return "LOSS"
    return "OPEN"


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def fetch_futures_klines(session: requests.Session, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    response = session.get(
        BINANCE_FUTURES_KLINES,
        params={"symbol": normalize_symbol(symbol), "interval": TIMEFRAME, "startTime": start_ms, "endTime": end_ms, "limit": 1000},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if not data:
        return pd.DataFrame()
    frame = pd.DataFrame(
        data,
        columns=[
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "quote_asset_volume",
            "num_trades",
            "taker_buy_base_volume",
            "taker_buy_quote_volume",
            "ignore",
        ],
    )
    for column in ["open", "high", "low", "close"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["open_time"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True, errors="coerce")
    frame["close_time"] = pd.to_datetime(frame["close_time"], unit="ms", utc=True, errors="coerce")
    return frame.dropna(subset=["open_time", "close_time", "high", "low"]).copy()


def fetch_range(session: requests.Session, symbol: str, start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> pd.DataFrame:
    if pd.isna(start_ts) or pd.isna(end_ts) or end_ts <= start_ts:
        return pd.DataFrame()
    frames: list[pd.DataFrame] = []
    cursor = start_ts.floor("15min")
    end_ts = end_ts.ceil("15min")
    step = pd.Timedelta(minutes=15 * 999)
    while cursor < end_ts:
        chunk_end = min(cursor + step, end_ts)
        chunk = fetch_futures_klines(session, symbol, int(cursor.timestamp() * 1000), int(chunk_end.timestamp() * 1000))
        if not chunk.empty:
            frames.append(chunk)
        cursor = chunk_end + pd.Timedelta(milliseconds=1)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates(["open_time", "close_time"]).sort_values("open_time").reset_index(drop=True)


def build_candle_cache(candidates: list[Candidate], lookahead_hours: int, session: requests.Session) -> tuple[dict[str, pd.DataFrame], int]:
    grouped: dict[str, list[pd.Timestamp]] = {}
    for candidate in candidates:
        ts = pd.to_datetime(candidate.timestamp_utc, utc=True, errors="coerce")
        if pd.isna(ts):
            continue
        grouped.setdefault(candidate.symbol, []).append(ts)
    cache: dict[str, pd.DataFrame] = {}
    request_estimate = 0
    now = pd.Timestamp.now(tz="UTC")
    for symbol, timestamps in grouped.items():
        start_ts = min(timestamps)
        end_ts = min(max(timestamps) + pd.Timedelta(hours=lookahead_hours), now)
        if end_ts <= start_ts:
            cache[symbol] = pd.DataFrame()
            continue
        chunks = math.ceil(max((end_ts - start_ts).total_seconds() / 60, 1) / (15 * 999))
        request_estimate += chunks
        try:
            cache[symbol] = fetch_range(session, symbol, start_ts, end_ts)
        except Exception as exc:
            LOGGER.warning("Pullback retest candle fetch failed for %s: %s", symbol, exc)
            cache[symbol] = pd.DataFrame()
    return cache, request_estimate


def candles_for_candidate(candidate: Candidate, cache: dict[str, pd.DataFrame], lookahead_hours: int) -> pd.DataFrame:
    candles = cache.get(candidate.symbol, pd.DataFrame())
    if candles.empty:
        return pd.DataFrame()
    ts = pd.to_datetime(candidate.timestamp_utc, utc=True, errors="coerce")
    if pd.isna(ts):
        return pd.DataFrame()
    end_ts = ts + pd.Timedelta(hours=lookahead_hours)
    mask = (candles["open_time"] >= ts) & (candles["close_time"] <= end_ts)
    return candles.loc[mask].copy().reset_index(drop=True)


def normalize_signal_frame(data: pd.DataFrame) -> pd.DataFrame:
    if data.empty:
        return pd.DataFrame()
    out = pd.DataFrame(index=data.index)
    out["timestamp_utc"] = first_series(data, ["timestamp", "timestamp_utc"]).map(normalize_timestamp)
    out["symbol"] = first_series(data, ["symbol", "normalized_symbol"]).map(normalize_symbol)
    out["side"] = first_series(data, ["side", "direction", "normalized_direction"]).map(normalize_side)
    out["entry"] = pd.to_numeric(first_series(data, ["entry", "entry_low"]), errors="coerce")
    out["sl"] = pd.to_numeric(first_series(data, ["stop_loss", "sl"]), errors="coerce")
    out["tp1"] = pd.to_numeric(first_series(data, ["tp1"]), errors="coerce")
    out["tp2"] = pd.to_numeric(first_series(data, ["tp2"]), errors="coerce")
    out["rr"] = pd.to_numeric(first_series(data, ["risk_reward", "rr", "original_rr"]), errors="coerce").fillna(0.0)
    out["score"] = pd.to_numeric(first_series(data, ["score", "raw_score"]), errors="coerce")
    out["confidence"] = pd.to_numeric(first_series(data, ["setup_strength", "confidence"]), errors="coerce")
    out["session"] = first_series(data, ["market_session", "session"], "Unknown").replace("", "Unknown")
    out["signal_status"] = first_series(data, ["signal_status"], "").astype(str).str.lower()
    out["result"] = first_series(data, ["result"], "OPEN").astype(str).str.upper().replace({"": "OPEN", "NAN": "OPEN", "NONE": "OPEN"})
    out["hit_target"] = first_series(data, ["hit_target", "outcome"]).astype(str).str.upper()
    out["atr"] = pd.to_numeric(first_series(data, ["atr"]), errors="coerce")
    out["support"] = pd.to_numeric(first_series(data, ["support"]), errors="coerce")
    out["resistance"] = pd.to_numeric(first_series(data, ["resistance"]), errors="coerce")
    out["canonical_signal_key"] = [
        canonical_signal_key(symbol=symbol, side=side, timestamp=timestamp, entry=entry)
        for symbol, side, timestamp, entry in zip(out["symbol"], out["side"], out["timestamp_utc"], out["entry"])
    ]
    out["original_r"] = out.apply(original_r_from_row, axis=1)
    return out


def normalize_shadow_context(data: pd.DataFrame, kind: str) -> pd.DataFrame:
    if data.empty:
        return pd.DataFrame()
    out = pd.DataFrame(index=data.index)
    out["timestamp_utc"] = first_series(data, ["signal_timestamp", "final_signal_timestamp", "timestamp", "timestamp_utc"]).map(normalize_timestamp)
    out["symbol"] = first_series(data, ["symbol", "normalized_symbol"]).map(normalize_symbol)
    out["side"] = first_series(data, ["side", "direction", "normalized_direction"]).map(normalize_side)
    out["entry"] = pd.to_numeric(first_series(data, ["entry", "entry_low"]), errors="coerce")
    existing = first_series(data, ["canonical_signal_key"])
    derived = pd.Series(
        [
            canonical_signal_key(symbol=symbol, side=side, timestamp=timestamp, entry=entry)
            for symbol, side, timestamp, entry in zip(out["symbol"], out["side"], out["timestamp_utc"], out["entry"])
        ],
        index=out.index,
    )
    out["canonical_signal_key"] = existing.where(existing.astype(str).str.strip().ne(""), derived)
    if kind == "sr":
        out["sr_class"] = first_series(data, ["sr_gate_decision"], "").astype(str).str.upper()
        out["support"] = pd.to_numeric(first_series(data, ["support"]), errors="coerce")
        out["resistance"] = pd.to_numeric(first_series(data, ["resistance"]), errors="coerce")
        out["atr"] = pd.to_numeric(first_series(data, ["atr"]), errors="coerce")
        out["opposing_distance_atr"] = pd.to_numeric(first_series(data, ["opposing_distance_atr"]), errors="coerce")
        out["effective_sr_rr"] = pd.to_numeric(first_series(data, ["effective_sr_rr"]), errors="coerce")
    elif kind == "entry":
        out["entry_timing_class"] = (
            first_series(data, ["recommendation"], "")
            .astype(str)
            .str.upper()
            .str.replace("SKIP (POOR TIMING)", "SKIP", regex=False)
            .str.replace("WAIT FOR PULLBACK", "WAIT PULLBACK", regex=False)
        )
        out["atr"] = pd.to_numeric(first_series(data, ["atr_proxy"]), errors="coerce")
        out["support"] = pd.to_numeric(first_series(data, ["support"]), errors="coerce")
        out["resistance"] = pd.to_numeric(first_series(data, ["resistance"]), errors="coerce")
    elif kind == "me":
        out["exhaustion_class"] = first_series(data, ["exhaustion_class"], "").astype(str).str.upper()
        out["swing_distance_atr"] = pd.to_numeric(first_series(data, ["swing_distance_atr"]), errors="coerce")
        out["ema20_distance_atr"] = pd.to_numeric(first_series(data, ["ema20_distance_atr"]), errors="coerce")
        out["ema50_distance_atr"] = pd.to_numeric(first_series(data, ["ema50_distance_atr"]), errors="coerce")
        out["directional_run"] = pd.to_numeric(first_series(data, ["directional_run_1h"]), errors="coerce")
        out["atr_expansion_ratio"] = pd.to_numeric(first_series(data, ["atr_expansion_ratio"]), errors="coerce")
    return out[out["canonical_signal_key"].astype(str).str.strip().ne("")].drop_duplicates("canonical_signal_key", keep="last")


def read_csv_safe(path: Path) -> pd.DataFrame:
    try:
        if path.exists() and path.stat().st_size > 0:
            return pd.read_csv(path, low_memory=False)
    except Exception as exc:
        LOGGER.warning("Could not read %s: %s", path, exc)
    return pd.DataFrame()


def load_candidates(base_dir: Path, population: str = "all") -> tuple[list[Candidate], dict[str, int]]:
    logs = base_dir / "logs"
    signals = normalize_signal_frame(read_csv_safe(logs / "signals.csv"))
    if signals.empty:
        return [], {"sent": 0, "rejected": 0}

    contexts = {
        "sr": normalize_shadow_context(read_csv_safe(logs / "sr_trade_weight_shadow.csv"), "sr"),
        "entry": normalize_shadow_context(read_csv_safe(logs / "entry_timing_engine.csv"), "entry"),
        "me": normalize_shadow_context(read_csv_safe(logs / "market_exhaustion_shadow.csv"), "me"),
    }
    data = signals.copy()
    for name, context in contexts.items():
        if not context.empty:
            context_payload = context.drop(columns=[c for c in ["timestamp_utc", "symbol", "side", "entry"] if c in context.columns])
            rename_map = {
                column: f"{name}_{column}"
                for column in context_payload.columns
                if column != "canonical_signal_key" and column in data.columns
            }
            context_payload = context_payload.rename(columns=rename_map)
            data = data.merge(context_payload, on="canonical_signal_key", how="left")

    for numeric in ["atr", "support", "resistance"]:
        for ctx_col in [f"sr_{numeric}", f"entry_{numeric}", f"me_{numeric}"]:
            if ctx_col in data.columns:
                data[numeric] = data[numeric].where(pd.to_numeric(data[numeric], errors="coerce").notna(), data[ctx_col])
    optional_defaults: dict[str, Any] = {
        "sr_class": "",
        "entry_timing_class": "",
        "exhaustion_class": "",
        "opposing_distance_atr": math.nan,
        "effective_sr_rr": math.nan,
        "swing_distance_atr": math.nan,
        "ema20_distance_atr": math.nan,
        "ema50_distance_atr": math.nan,
        "directional_run": math.nan,
        "atr_expansion_ratio": math.nan,
    }
    for column, default in optional_defaults.items():
        if column not in data.columns:
            data[column] = default
    data["source_population"] = "rejected"
    data.loc[data["signal_status"].isin(SENT_STATUSES), "source_population"] = "sent"
    data.loc[data["entry_timing_class"].fillna("").astype(str).str.contains("WAIT PULLBACK", na=False), "source_population"] = "wait_pullback"
    data.loc[data["sr_class"].fillna("").astype(str).str.upper().isin(["CAUTION", "SKIP"]), "source_population"] = data["source_population"].where(data["source_population"].eq("sent"), "sr_caution_skip")
    data.loc[data["exhaustion_class"].fillna("").astype(str).str.upper().isin(["EXTENDED", "EXHAUSTED"]), "source_population"] = data["source_population"].where(data["source_population"].isin(["sent", "wait_pullback", "sr_caution_skip"]), "exhaustion")

    if population != "all":
        aliases = {"rejected": "rejected", "sent": "sent", "wait_pullback": "wait_pullback", "sr": "sr_caution_skip", "exhaustion": "exhaustion"}
        data = data[data["source_population"].eq(aliases.get(population, population))]

    valid = data[
        data["canonical_signal_key"].astype(str).str.strip().ne("")
        & data["symbol"].astype(str).str.strip().ne("")
        & data["side"].isin(["LONG", "SHORT"])
        & pd.to_numeric(data["entry"], errors="coerce").gt(0)
        & pd.to_numeric(data["sl"], errors="coerce").gt(0)
        & pd.to_numeric(data["tp1"], errors="coerce").gt(0)
    ].drop_duplicates("canonical_signal_key", keep="last")

    candidates: list[Candidate] = []
    for _, row in valid.iterrows():
        atr = safe_float(row.get("atr"), math.nan)
        if not math.isfinite(atr) or atr <= 0:
            atr = abs(safe_float(row.get("entry"), 0.0) - safe_float(row.get("sl"), 0.0))
        candidates.append(
            Candidate(
                canonical_signal_key=str(row["canonical_signal_key"]),
                timestamp_utc=str(row["timestamp_utc"]),
                symbol=str(row["symbol"]),
                side=str(row["side"]),
                source_population=str(row["source_population"]),
                entry=safe_float(row["entry"], 0.0),
                sl=safe_float(row["sl"], 0.0),
                tp1=safe_float(row["tp1"], 0.0),
                tp2=safe_float(row.get("tp2"), safe_float(row["tp1"], 0.0)),
                rr=safe_float(row.get("rr"), 0.0),
                score=safe_float(row.get("score"), math.nan),
                confidence=safe_float(row.get("confidence"), math.nan),
                session=str(row.get("session") or "Unknown"),
                original_outcome=str(row.get("result") or "OPEN"),
                original_r=safe_float(row.get("original_r"), 0.0),
                atr=atr,
                support=safe_float(row.get("support"), 0.0),
                resistance=safe_float(row.get("resistance"), 0.0),
                sr_class=str(row.get("sr_class") or ""),
                opposing_distance_atr=safe_float(row.get("opposing_distance_atr"), math.nan),
                effective_sr_rr=safe_float(row.get("effective_sr_rr"), math.nan),
                entry_timing_class=str(row.get("entry_timing_class") or ""),
                exhaustion_class=str(row.get("exhaustion_class") or ""),
                swing_distance_atr=safe_float(row.get("swing_distance_atr"), math.nan),
                ema20_distance_atr=safe_float(row.get("ema20_distance_atr"), math.nan),
                ema50_distance_atr=safe_float(row.get("ema50_distance_atr"), math.nan),
                directional_run=safe_float(row.get("directional_run"), math.nan),
                atr_expansion_ratio=safe_float(row.get("atr_expansion_ratio"), math.nan),
            )
        )
    counts = {str(k): int(v) for k, v in valid["source_population"].value_counts().to_dict().items()}
    return candidates, counts


def strategy_target(candidate: Candidate, strategy: str) -> StrategyTarget:
    atr = candidate.atr
    if strategy.startswith("ATR_PULLBACK"):
        if not math.isfinite(atr) or atr <= 0:
            return StrategyTarget(strategy, math.nan, "NOT_APPLICABLE", "missing_atr")
        factor = {"ATR_PULLBACK_0_30": 0.30, "ATR_PULLBACK_0_50": 0.50, "ATR_PULLBACK_0_75": 0.75}[strategy]
        target = candidate.entry - factor * atr if candidate.side == "LONG" else candidate.entry + factor * atr
        return StrategyTarget(strategy, target, "APPLICABLE")
    if strategy == "EMA_RETEST":
        if not math.isfinite(candidate.ema20_distance_atr) or not math.isfinite(atr) or atr <= 0:
            return StrategyTarget(strategy, math.nan, "NOT_APPLICABLE", "missing_ema_context")
        distance = abs(candidate.ema20_distance_atr) * atr
        target = candidate.entry - distance if candidate.side == "LONG" else candidate.entry + distance
        return StrategyTarget(strategy, target, "APPLICABLE")
    if strategy == "BREAKOUT_RETEST":
        level = candidate.resistance if candidate.side == "LONG" else candidate.support
        if level <= 0:
            return StrategyTarget(strategy, math.nan, "NOT_APPLICABLE", "missing_breakout_level")
        if candidate.side == "LONG" and candidate.entry <= level:
            return StrategyTarget(strategy, math.nan, "NOT_APPLICABLE", "not_above_breakout_level")
        if candidate.side == "SHORT" and candidate.entry >= level:
            return StrategyTarget(strategy, math.nan, "NOT_APPLICABLE", "not_below_breakout_level")
        return StrategyTarget(strategy, level, "APPLICABLE")
    if strategy == "SR_RETEST":
        level = candidate.support if candidate.side == "LONG" else candidate.resistance
        if level <= 0:
            return StrategyTarget(strategy, math.nan, "NOT_APPLICABLE", "missing_sr_level")
        if candidate.side == "LONG" and level >= candidate.entry:
            return StrategyTarget(strategy, math.nan, "NOT_APPLICABLE", "support_not_below_entry")
        if candidate.side == "SHORT" and level <= candidate.entry:
            return StrategyTarget(strategy, math.nan, "NOT_APPLICABLE", "resistance_not_above_entry")
        return StrategyTarget(strategy, level, "APPLICABLE")
    return StrategyTarget(strategy, math.nan, "NOT_APPLICABLE", "unknown_strategy")


def extrema_from_entry(side: str, entry: float, candles: pd.DataFrame, atr: float) -> tuple[float, float]:
    if candles.empty or entry <= 0 or atr <= 0:
        return math.nan, math.nan
    if side == "LONG":
        mfe = (float(candles["high"].max()) - entry) / atr
        mae = (entry - float(candles["low"].min())) / atr
    else:
        mfe = (entry - float(candles["low"].min())) / atr
        mae = (float(candles["high"].max()) - entry) / atr
    return round(float(mae), 4), round(float(mfe), 4)


def evaluate_retest_fill(candidate: Candidate, target: float, candles: pd.DataFrame, wait_window_minutes: int) -> RetestResult:
    if candles.empty:
        return RetestResult("DATA_INSUFFICIENT")
    if not math.isfinite(target) or target <= 0:
        return RetestResult("NOT_APPLICABLE")
    start = pd.to_datetime(candidate.timestamp_utc, utc=True, errors="coerce")
    if pd.isna(start):
        return RetestResult("DATA_INSUFFICIENT")
    window_end = start + pd.Timedelta(minutes=wait_window_minutes)
    window = candles[pd.to_datetime(candles["close_time"], utc=True, errors="coerce") <= window_end].copy().reset_index(drop=True)
    if window.empty:
        return RetestResult("DATA_INSUFFICIENT")

    seen = pd.DataFrame()
    for idx, candle in window.iterrows():
        high = float(candle["high"])
        low = float(candle["low"])
        if candidate.side == "LONG":
            sl_hit = low <= candidate.sl
            target_hit = low <= target
        else:
            sl_hit = high >= candidate.sl
            target_hit = high >= target

        seen = pd.concat([seen, pd.DataFrame([candle])], ignore_index=True)
        mae, mfe = extrema_from_entry(candidate.side, candidate.entry, seen, candidate.atr)
        if sl_hit:
            return RetestResult("INVALIDATED_BEFORE_RETEST", pre_mae_atr=mae, pre_mfe_atr=mfe)
        if target_hit:
            ts = pd.to_datetime(candle["close_time"], utc=True, errors="coerce")
            return RetestResult("RETEST_FILLED", "" if pd.isna(ts) else ts.isoformat(), int(idx), mae, mfe)
    mae, mfe = extrema_from_entry(candidate.side, candidate.entry, window, candidate.atr)
    return RetestResult("NO_RETEST", pre_mae_atr=mae, pre_mfe_atr=mfe)


def levels_for_model(candidate: Candidate, retest_entry: float, model: str) -> tuple[float, float, float]:
    if model == "ORIGINAL_STRUCTURE":
        return candidate.sl, candidate.tp1, candidate.tp2
    risk = abs(candidate.entry - candidate.sl)
    if risk <= 0:
        return math.nan, math.nan, math.nan
    rr = candidate.rr if candidate.rr > 0 else 2.0
    if candidate.side == "LONG":
        return retest_entry - risk, retest_entry + risk, retest_entry + risk * rr
    return retest_entry + risk, retest_entry - risk, retest_entry - risk * rr


def effective_rr(side: str, entry: float, sl: float, tp2: float) -> float:
    risk = abs(entry - sl)
    reward = abs(tp2 - entry)
    if risk <= 0 or reward <= 0:
        return math.nan
    return round(reward / risk, 4)


def evaluate_after_retest(candidate: Candidate, candles: pd.DataFrame, fill_index: int, retest_entry: float, model: str) -> OutcomeResult:
    sl, tp1, tp2 = levels_for_model(candidate, retest_entry, model)
    if not all(math.isfinite(value) and value > 0 for value in [sl, tp1, tp2]):
        return OutcomeResult("DATA_INSUFFICIENT", "", 0.0, "")
    future = candles.iloc[max(fill_index, 0) :].copy()
    if future.empty:
        return OutcomeResult("OPEN", "", 0.0, "")
    rr2 = effective_rr(candidate.side, retest_entry, sl, tp2)
    for _, candle in future.iterrows():
        high = float(candle["high"])
        low = float(candle["low"])
        closed_at = pd.to_datetime(candle["close_time"], utc=True, errors="coerce")
        ts = "" if pd.isna(closed_at) else closed_at.isoformat()
        if candidate.side == "LONG":
            sl_hit = low <= sl
            tp2_hit = high >= tp2
            tp1_hit = high >= tp1
        else:
            sl_hit = high >= sl
            tp2_hit = low <= tp2
            tp1_hit = low <= tp1
        if sl_hit:
            return OutcomeResult("LOSS", "SL", -1.0, ts)
        if tp2_hit:
            return OutcomeResult("WIN_TP2", "TP2", rr2 if math.isfinite(rr2) and rr2 > 0 else 2.0, ts)
        if tp1_hit:
            return OutcomeResult("WIN_TP1", "TP1", 1.0, ts)
    return OutcomeResult("OPEN", "", 0.0, "")


def build_record(candidate: Candidate, strategy: StrategyTarget, window: int, model: str, fill: RetestResult, outcome: OutcomeResult) -> dict[str, Any]:
    retest_entry = strategy.target_entry if fill.status == "RETEST_FILLED" else math.nan
    rr_at_retest = math.nan
    if fill.status == "RETEST_FILLED":
        sl, _, tp2 = levels_for_model(candidate, retest_entry, model)
        rr_at_retest = effective_rr(candidate.side, retest_entry, sl, tp2)
    retest_r = outcome.r_value if fill.status == "RETEST_FILLED" else 0.0
    original_winner = candidate.original_r > 0
    original_loser = candidate.original_r < 0
    retest_winner = retest_r > 0
    dedupe = f"{candidate.canonical_signal_key}|{strategy.strategy}|{window}|{model}"
    return {
        "dedupe_key": dedupe,
        "canonical_signal_key": candidate.canonical_signal_key,
        "timestamp_utc": candidate.timestamp_utc,
        "symbol": candidate.symbol,
        "side": candidate.side,
        "source_population": candidate.source_population,
        "market_session": candidate.session,
        "model": model,
        "original_entry": normalize_float(candidate.entry),
        "original_sl": normalize_float(candidate.sl),
        "original_tp1": normalize_float(candidate.tp1),
        "original_tp2": normalize_float(candidate.tp2),
        "original_outcome": candidate.original_outcome,
        "original_r": f"{candidate.original_r:.4f}",
        "atr": normalize_float(candidate.atr),
        "strategy": strategy.strategy,
        "wait_window_minutes": window,
        "target_retest_entry": normalize_float(strategy.target_entry),
        "retest_status": fill.status if strategy.status == "APPLICABLE" else strategy.status,
        "retest_fill_timestamp": fill.fill_timestamp,
        "retest_entry": normalize_float(retest_entry),
        "effective_rr_at_retest": f"{rr_at_retest:.4f}" if math.isfinite(rr_at_retest) else "",
        "retest_outcome": outcome.outcome if fill.status == "RETEST_FILLED" else fill.status,
        "retest_r": f"{retest_r:.4f}",
        "resolution_timestamp": outcome.resolution_timestamp,
        "r_delta_vs_original": f"{(retest_r - candidate.original_r):.4f}",
        "sl_avoided": int(original_loser and fill.status in {"NO_RETEST", "INVALIDATED_BEFORE_RETEST"} or (original_loser and retest_r >= 0)),
        "winner_missed": int(original_winner and fill.status != "RETEST_FILLED"),
        "loser_to_winner": int(original_loser and retest_winner),
        "sr_class": candidate.sr_class,
        "opposing_distance_atr": f"{candidate.opposing_distance_atr:.4f}" if math.isfinite(candidate.opposing_distance_atr) else "",
        "effective_sr_rr": f"{candidate.effective_sr_rr:.4f}" if math.isfinite(candidate.effective_sr_rr) else "",
        "entry_timing_class": candidate.entry_timing_class,
        "exhaustion_class": candidate.exhaustion_class,
        "swing_distance_atr": f"{candidate.swing_distance_atr:.4f}" if math.isfinite(candidate.swing_distance_atr) else "",
        "ema20_distance_atr": f"{candidate.ema20_distance_atr:.4f}" if math.isfinite(candidate.ema20_distance_atr) else "",
        "ema50_distance_atr": f"{candidate.ema50_distance_atr:.4f}" if math.isfinite(candidate.ema50_distance_atr) else "",
        "directional_run": f"{candidate.directional_run:.4f}" if math.isfinite(candidate.directional_run) else "",
        "atr_expansion_ratio": f"{candidate.atr_expansion_ratio:.4f}" if math.isfinite(candidate.atr_expansion_ratio) else "",
        "prior_24h_move_atr": "",
        "prior_session_move_atr": "",
        "prior_day_known_net_r": f"{candidate.prior_day_known_net_r:.4f}" if math.isfinite(candidate.prior_day_known_net_r) else "",
        "pre_retest_mae_atr": f"{fill.pre_mae_atr:.4f}" if math.isfinite(fill.pre_mae_atr) else "",
        "pre_retest_mfe_atr": f"{fill.pre_mfe_atr:.4f}" if math.isfinite(fill.pre_mfe_atr) else "",
        "generated_at_utc": pd.Timestamp.now(tz="UTC").isoformat(),
    }


def evaluate_candidate(candidate: Candidate, candles: pd.DataFrame, windows: list[int] | None = None, models: list[str] | None = None) -> list[dict[str, Any]]:
    windows = windows or DEFAULT_WAIT_WINDOWS
    models = models or MODELS
    records: list[dict[str, Any]] = []
    for strategy_name in STRATEGIES:
        target = strategy_target(candidate, strategy_name)
        for window in windows:
            if target.status != "APPLICABLE":
                fill = RetestResult(target.status)
                outcome = OutcomeResult(target.status, "", 0.0, "")
                for model in models:
                    records.append(build_record(candidate, target, window, model, fill, outcome))
                continue
            fill = evaluate_retest_fill(candidate, target.target_entry, candles, window)
            for model in models:
                outcome = OutcomeResult(fill.status, "", 0.0, "")
                if fill.status == "RETEST_FILLED":
                    outcome = evaluate_after_retest(candidate, candles, fill.fill_index, target.target_entry, model)
                records.append(build_record(candidate, target, window, model, fill, outcome))
    return records


class PullbackRetestShadowLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    def ensure_schema(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            with self.path.open("w", newline="", encoding="utf-8") as handle:
                csv.DictWriter(handle, fieldnames=FIELDNAMES).writeheader()
            return
        existing = read_csv_safe(self.path)
        changed = False
        for field in FIELDNAMES:
            if field not in existing.columns:
                existing[field] = ""
                changed = True
        if changed or list(existing.columns) != FIELDNAMES:
            existing[FIELDNAMES].to_csv(self.path, index=False)

    def existing_keys(self) -> set[str]:
        try:
            data = pd.read_csv(self.path, usecols=["dedupe_key"])
        except (FileNotFoundError, pd.errors.EmptyDataError, ValueError):
            return set()
        return {str(value).strip() for value in data["dedupe_key"].dropna() if str(value).strip()}

    def append_records(self, records: list[dict[str, Any]]) -> int:
        existing = self.existing_keys()
        new_records = []
        for record in records:
            key = str(record.get("dedupe_key", "")).strip()
            if not key or key in existing:
                continue
            existing.add(key)
            new_records.append(record)
        if not new_records:
            return 0
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore")
            writer.writerows(new_records)
        return len(new_records)


def summarize_records(records: list[dict[str, Any]] | pd.DataFrame) -> pd.DataFrame:
    data = pd.DataFrame(records) if isinstance(records, list) else records.copy()
    columns = [
        "strategy",
        "wait_window_minutes",
        "model",
        "candidates",
        "applicable",
        "retest_filled",
        "no_retest",
        "invalidated_before_retest",
        "fill_rate",
        "resolved_hypothetical",
        "wr",
        "net_r",
        "avg_effective_rr",
        "r_delta_vs_original",
        "sl_avoided",
        "winners_missed",
        "loser_to_winner",
    ]
    if data.empty:
        return pd.DataFrame(columns=columns)
    for col in ["retest_r", "original_r", "effective_rr_at_retest", "r_delta_vs_original"]:
        data[col] = pd.to_numeric(data.get(col), errors="coerce")
    rows = []
    for (strategy, window, model), group in data.groupby(["strategy", "wait_window_minutes", "model"], dropna=False):
        applicable = group[~group["retest_status"].isin(["NOT_APPLICABLE"])]
        filled = group[group["retest_status"].eq("RETEST_FILLED")]
        resolved = filled[filled["retest_outcome"].isin(["WIN_TP1", "WIN_TP2", "LOSS"])]
        wins = int(resolved["retest_outcome"].astype(str).str.startswith("WIN").sum()) if not resolved.empty else 0
        rows.append(
            {
                "strategy": strategy,
                "wait_window_minutes": int(window),
                "model": model,
                "candidates": int(len(group)),
                "applicable": int(len(applicable)),
                "retest_filled": int(len(filled)),
                "no_retest": int(group["retest_status"].eq("NO_RETEST").sum()),
                "invalidated_before_retest": int(group["retest_status"].eq("INVALIDATED_BEFORE_RETEST").sum()),
                "fill_rate": round(float(len(filled) / len(applicable) * 100), 2) if len(applicable) else 0.0,
                "resolved_hypothetical": int(len(resolved)),
                "wr": round(float(wins / len(resolved) * 100), 2) if len(resolved) else 0.0,
                "net_r": round(float(resolved["retest_r"].fillna(0).sum()), 4) if not resolved.empty else 0.0,
                "avg_effective_rr": round(float(filled["effective_rr_at_retest"].mean()), 4) if not filled.empty else 0.0,
                "r_delta_vs_original": round(float(group["r_delta_vs_original"].fillna(0).sum()), 4),
                "sl_avoided": int(pd.to_numeric(group.get("sl_avoided"), errors="coerce").fillna(0).sum()),
                "winners_missed": int(pd.to_numeric(group.get("winner_missed"), errors="coerce").fillna(0).sum()),
                "loser_to_winner": int(pd.to_numeric(group.get("loser_to_winner"), errors="coerce").fillna(0).sum()),
            }
        )
    return pd.DataFrame(rows, columns=columns).sort_values(["net_r", "wr", "retest_filled"], ascending=[False, False, False])


def summarize_interaction(records: list[dict[str, Any]] | pd.DataFrame, column: str) -> pd.DataFrame:
    data = pd.DataFrame(records) if isinstance(records, list) else records.copy()
    columns = [column, "strategy", "model", "resolved", "wr", "net_r", "fill_rate"]
    if data.empty or column not in data.columns:
        return pd.DataFrame(columns=columns)
    rows = []
    for (value, strategy, model), group in data.groupby([column, "strategy", "model"], dropna=False):
        applicable = group[~group["retest_status"].isin(["NOT_APPLICABLE"])]
        filled = group[group["retest_status"].eq("RETEST_FILLED")]
        resolved = filled[filled["retest_outcome"].isin(["WIN_TP1", "WIN_TP2", "LOSS"])]
        if resolved.empty:
            continue
        wins = int(resolved["retest_outcome"].astype(str).str.startswith("WIN").sum())
        rows.append(
            {
                column: str(value or "UNKNOWN"),
                "strategy": strategy,
                "model": model,
                "resolved": int(len(resolved)),
                "wr": round(float(wins / len(resolved) * 100), 2),
                "net_r": round(float(pd.to_numeric(resolved["retest_r"], errors="coerce").fillna(0).sum()), 4),
                "fill_rate": round(float(len(filled) / len(applicable) * 100), 2) if len(applicable) else 0.0,
            }
        )
    return pd.DataFrame(rows, columns=columns).sort_values(["resolved", "net_r"], ascending=[False, False])


def run_shadow(
    base_dir: Path,
    output_path: Path,
    *,
    population: str = "all",
    limit: int | None = None,
    dry_run: bool = False,
    lookahead_hours: int = DEFAULT_LOOKAHEAD_HOURS,
    candle_provider: Callable[[Candidate, int], pd.DataFrame] | None = None,
) -> dict[str, Any]:
    candidates, population_counts = load_candidates(base_dir, population=population)
    if limit is not None:
        candidates = candidates[: max(limit, 0)]

    records: list[dict[str, Any]] = []
    request_estimate = 0
    cache: dict[str, pd.DataFrame] = {}
    if candle_provider is None and candidates:
        session = build_session()
        cache, request_estimate = build_candle_cache(candidates, lookahead_hours, session)
    else:
        unique_symbols = {candidate.symbol for candidate in candidates}
        request_estimate = len(unique_symbols)

    for candidate in candidates:
        try:
            candles = candle_provider(candidate, lookahead_hours) if candle_provider else candles_for_candidate(candidate, cache, lookahead_hours)
            records.extend(evaluate_candidate(candidate, candles))
        except Exception as exc:
            LOGGER.warning("Pullback retest skipped %s: %s", candidate.canonical_signal_key, exc)

    written = 0
    if not dry_run:
        written = PullbackRetestShadowLogger(output_path).append_records(records)

    summary = summarize_records(records)
    return {
        "candidates": len(candidates),
        "population_counts": population_counts,
        "records": records,
        "written": written,
        "dry_run": dry_run,
        "output_path": str(output_path),
        "estimated_api_requests": request_estimate,
        "summary": summary,
        "by_exhaustion": summarize_interaction(records, "exhaustion_class"),
        "by_sr": summarize_interaction(records, "sr_class"),
        "by_session": summarize_interaction(records, "market_session"),
        "by_population": summarize_interaction(records, "source_population"),
    }


def format_summary(result: dict[str, Any], limit: int = 20) -> str:
    lines = [
        "Pullback / Retest Outcome Shadow V1",
        "====================================",
        f"Candidates evaluated: {result['candidates']}",
        f"Dry run: {result['dry_run']}",
        f"Rows written: {result['written']}",
        f"Estimated Binance API requests: {result['estimated_api_requests']}",
        f"Output: {result['output_path']}",
        "",
        "Population counts:",
    ]
    counts = result.get("population_counts", {})
    lines.extend([f"- {key}: {value}" for key, value in sorted(counts.items())] or ["- N/A"])
    lines.append("")
    summary = result["summary"]
    lines.append("Strategy summary:")
    lines.append(summary.head(limit).to_string(index=False) if isinstance(summary, pd.DataFrame) and not summary.empty else "N/A")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analytics-only Pullback / Retest Outcome Shadow V1.")
    parser.add_argument("--base-dir", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--output", default="")
    parser.add_argument("--population", default="all", choices=["all", "sent", "rejected", "wait_pullback", "sr", "exhaustion"])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--lookahead-hours", type=int, default=env_int("REVIEW_LOOKAHEAD_HOURS", DEFAULT_LOOKAHEAD_HOURS))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if not env_bool("PULLBACK_RETEST_SHADOW_ENABLED", True):
        print("Pullback / Retest Outcome Shadow disabled by PULLBACK_RETEST_SHADOW_ENABLED")
        return 0
    if env_bool("PULLBACK_RETEST_LIVE_ENABLED", False):
        LOGGER.warning("PULLBACK_RETEST_LIVE_ENABLED is ignored in V1; analytics-only mode remains active.")

    base_dir = Path(args.base_dir)
    output_path = Path(args.output) if args.output else base_dir / "logs" / "pullback_retest_outcome_shadow.csv"
    result = run_shadow(
        base_dir,
        output_path,
        population=args.population,
        limit=args.limit,
        dry_run=args.dry_run,
        lookahead_hours=args.lookahead_hours,
    )
    print(format_summary(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
