# -*- coding: utf-8 -*-
"""Hypothetical outcome tracker for rejected scanner candidates.

This module is analytics-only. It never sends Telegram/Cornix messages and does
not affect scanner approval, scoring, routing, TP/SL, RR, or filter order.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.signal_identity import (
    canonical_signal_key,
    normalize_float,
    normalize_side,
    normalize_symbol,
    normalize_timestamp,
)


LOGGER = logging.getLogger("rejected_outcome_shadow")

BINANCE_FUTURES_KLINES = "https://fapi.binance.com/fapi/v1/klines"
OUTCOME_TIMEFRAME = "15m"
DEFAULT_LOOKAHEAD_HOURS = 24

FIELDNAMES = [
    "canonical_signal_key",
    "timestamp_utc",
    "symbol",
    "side",
    "entry",
    "sl",
    "tp1",
    "tp2",
    "original_rr",
    "rejection_reason",
    "rejection_detail",
    "score",
    "confidence",
    "tier",
    "session",
    "hypothetical_outcome",
    "hypothetical_r",
    "tp1_hit",
    "tp2_hit",
    "sl_hit",
    "close_timestamp",
    "resolution_hours",
    "source",
]

TERMINAL_OR_LIVE_STATUSES = {
    "sent",
    "tier_c_report_only",
    "weak_symbol_report_only",
    "session_risk_report_only",
    "london_long_report_only",
}


@dataclass(frozen=True)
class RejectedCandidate:
    canonical_signal_key: str
    timestamp_utc: str
    symbol: str
    side: str
    entry: float
    sl: float
    tp1: float
    tp2: float
    original_rr: float
    rejection_reason: str
    rejection_detail: str
    score: str
    confidence: str
    tier: str
    session: str
    source: str = "scanner"


@dataclass(frozen=True)
class ShadowOutcome:
    hypothetical_outcome: str
    hit_target: str
    hypothetical_r: float
    close_timestamp: str
    resolution_hours: float
    tp1_hit: bool
    tp2_hit: bool
    sl_hit: bool


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


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not pd.notna(number) or number in {float("inf"), float("-inf")}:
        return default
    return number


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text and text.lower() not in {"nan", "none", "null"}:
            return text
    return ""


def normalize_rejection_category(signal_status: Any = "", skip_reason: Any = "") -> str:
    status = str(signal_status or "").strip().lower()
    reason = str(skip_reason or "").strip().lower()
    joined = f"{status} {reason}"

    if status in TERMINAL_OR_LIVE_STATUSES:
        return "OTHER"
    if "daily_risk" in status or "daily_guard" in status:
        return "DAILY_RISK_GUARD"
    if "correlation" in status:
        return "CORRELATION"
    if "btc" in status:
        return "BTC_REGIME"
    if "not_top" in status:
        return "NOT_TOP"
    if "quality" in status or "logged" in status:
        return "QUALITY"
    if "loss_cooldown" in status or "losing_streak" in status:
        return "LOSS_COOLDOWN"
    if "quality" in joined or "score" in joined or "confidence" in joined or "rr_below" in joined or "tier_c" in joined:
        return "QUALITY"
    if "daily_risk" in joined or "max_daily" in joined or "daily_guard" in joined:
        return "DAILY_RISK_GUARD"
    if "correlation" in joined or "major_correlated" in joined or "direction_per_candle" in joined:
        return "CORRELATION"
    if "btc_" in joined or "btc regime" in joined or "btc-regime" in joined:
        return "BTC_REGIME"
    if "not_top" in joined or "not_in_top" in joined or "send_cooldown" in joined:
        return "NOT_TOP"
    if "loss_cooldown" in joined or "losing_streak" in joined or "cooldown" in joined or "streak" in joined:
        return "LOSS_COOLDOWN"
    return "OTHER"


def is_rejected_status(signal_status: Any, result: Any = "") -> bool:
    status = str(signal_status or "").strip().lower()
    outcome = str(result or "").strip().upper()
    if not status:
        return outcome == "SKIPPED"
    if status in TERMINAL_OR_LIVE_STATUSES:
        return False
    return status.startswith(("skipped", "logged")) or outcome == "SKIPPED"


def candidate_from_record(record: dict[str, Any] | pd.Series, source: str = "scanner") -> RejectedCandidate | None:
    if isinstance(record, pd.Series):
        row: dict[str, Any] = record.to_dict()
    else:
        row = dict(record)

    if not is_rejected_status(row.get("signal_status", ""), row.get("result", "")):
        return None

    timestamp = normalize_timestamp(_first_text(row.get("timestamp"), row.get("timestamp_utc")))
    symbol = normalize_symbol(row.get("symbol", ""))
    side = normalize_side(_first_text(row.get("side"), row.get("direction")))
    entry = safe_float(row.get("entry"))
    sl = safe_float(_first_text(row.get("stop_loss"), row.get("sl")))
    tp1 = safe_float(row.get("tp1"))
    tp2 = safe_float(row.get("tp2"), tp1)

    if not timestamp or not symbol or side not in {"LONG", "SHORT"}:
        return None
    if entry <= 0 or sl <= 0 or tp1 <= 0:
        return None
    if tp2 <= 0:
        tp2 = tp1

    key = canonical_signal_key(symbol=symbol, side=side, timestamp=timestamp, entry=entry)
    if not key:
        return None

    detail = _first_text(row.get("skip_reason"), row.get("reason"), row.get("rejection_detail"))
    category = normalize_rejection_category(row.get("signal_status", ""), detail)
    return RejectedCandidate(
        canonical_signal_key=key,
        timestamp_utc=timestamp,
        symbol=symbol,
        side=side,
        entry=entry,
        sl=sl,
        tp1=tp1,
        tp2=tp2,
        original_rr=safe_float(_first_text(row.get("risk_reward"), row.get("rr"), row.get("original_rr"))),
        rejection_reason=category,
        rejection_detail=detail or str(row.get("signal_status", "") or "").strip(),
        score=_first_text(row.get("score"), row.get("raw_score")),
        confidence=_first_text(row.get("confidence"), row.get("setup_strength")),
        tier=_first_text(row.get("watchlist_tier"), row.get("tier")) or "Unknown",
        session=_first_text(row.get("market_session"), row.get("session")) or "Unknown",
        source=source,
    )


def load_rejected_candidates(path: Path, source: str = "scanner") -> list[RejectedCandidate]:
    try:
        df = pd.read_csv(path)
    except (FileNotFoundError, pd.errors.EmptyDataError):
        return []
    candidates: list[RejectedCandidate] = []
    seen: set[str] = set()
    for _, row in df.iterrows():
        candidate = candidate_from_record(row, source)
        if candidate is None or candidate.canonical_signal_key in seen:
            continue
        seen.add(candidate.canonical_signal_key)
        candidates.append(candidate)
    return candidates


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
    params = {
        "symbol": normalize_symbol(symbol),
        "interval": OUTCOME_TIMEFRAME,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": 1000,
    }
    response = session.get(BINANCE_FUTURES_KLINES, params=params, timeout=20)
    response.raise_for_status()
    data = response.json()
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(
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
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True, errors="coerce")
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True, errors="coerce")
    return df.dropna(subset=["high", "low", "close_time"]).copy()


def fetch_futures_klines_range(session: requests.Session, symbol: str, start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> pd.DataFrame:
    """Fetch a larger futures kline range in Binance-sized chunks.

    Binance caps kline responses at 1000 rows. On 15m candles that is roughly
    10 days, so grouping by symbol/time range keeps backfills away from the
    unsafe one-request-per-candidate pattern.
    """
    if pd.isna(start_ts) or pd.isna(end_ts) or end_ts <= start_ts:
        return pd.DataFrame()
    frames: list[pd.DataFrame] = []
    cursor = start_ts.floor("15min")
    end_ts = end_ts.ceil("15min")
    # 999 intervals leaves room for inclusive boundary behavior.
    step = pd.Timedelta(minutes=15 * 999)
    while cursor < end_ts:
        chunk_end = min(cursor + step, end_ts)
        start_ms = int(cursor.timestamp() * 1000)
        end_ms = int(chunk_end.timestamp() * 1000)
        chunk = fetch_futures_klines(session, symbol, start_ms, end_ms)
        if not chunk.empty:
            frames.append(chunk)
        cursor = chunk_end + pd.Timedelta(milliseconds=1)
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True, sort=False)
    combined = combined.drop_duplicates(subset=["open_time", "close_time"]).sort_values("open_time")
    return combined.reset_index(drop=True)


def build_candle_cache(
    candidates: list[RejectedCandidate],
    *,
    lookahead_hours: int,
    session: requests.Session,
) -> dict[str, pd.DataFrame]:
    cache: dict[str, pd.DataFrame] = {}
    if not candidates:
        return cache
    grouped: dict[str, list[pd.Timestamp]] = {}
    now = pd.Timestamp.now(tz="UTC")
    for candidate in candidates:
        ts = pd.to_datetime(candidate.timestamp_utc, utc=True, errors="coerce")
        if pd.isna(ts):
            continue
        grouped.setdefault(candidate.symbol, []).append(ts)
    for symbol, timestamps in grouped.items():
        start_ts = min(timestamps)
        end_ts = min(max(timestamps) + pd.Timedelta(hours=lookahead_hours), now)
        try:
            cache[symbol] = fetch_futures_klines_range(session, symbol, start_ts, end_ts)
        except Exception as exc:
            LOGGER.warning("Rejected outcome shadow candle cache failed for %s: %s", symbol, exc)
            cache[symbol] = pd.DataFrame()
    return cache


def candles_for_candidate_from_cache(
    candidate: RejectedCandidate,
    cache: dict[str, pd.DataFrame],
    *,
    lookahead_hours: int,
) -> pd.DataFrame:
    candles = cache.get(candidate.symbol, pd.DataFrame())
    if candles.empty:
        return pd.DataFrame()
    ts = pd.to_datetime(candidate.timestamp_utc, utc=True, errors="coerce")
    if pd.isna(ts):
        return pd.DataFrame()
    end_ts = min(ts + pd.Timedelta(hours=lookahead_hours), pd.Timestamp.now(tz="UTC"))
    open_time = pd.to_datetime(candles["open_time"], utc=True, errors="coerce")
    close_time = pd.to_datetime(candles["close_time"], utc=True, errors="coerce")
    mask = (open_time >= ts) & (close_time <= end_ts)
    return candles.loc[mask].copy()


def calculate_extremes(side: str, entry: float, candles: pd.DataFrame) -> tuple[float, float]:
    if candles.empty or entry <= 0:
        return 0.0, 0.0
    if side == "LONG":
        max_profit = (candles["high"].max() - entry) / entry * 100
        max_drawdown = (candles["low"].min() - entry) / entry * 100
    else:
        max_profit = (entry - candles["low"].min()) / entry * 100
        max_drawdown = (entry - candles["high"].max()) / entry * 100
    return float(max_profit), float(max_drawdown)


def evaluate_hypothetical_outcome(candidate: RejectedCandidate, candles: pd.DataFrame) -> ShadowOutcome:
    tp1_seen = False
    tp2_seen = False
    sl_seen = False
    if candles.empty:
        return ShadowOutcome("OPEN", "", 0.0, "", 0.0, False, False, False)

    for _, candle in candles.iterrows():
        high = float(candle["high"])
        low = float(candle["low"])
        closed_at = pd.to_datetime(candle["close_time"], utc=True, errors="coerce")
        close_timestamp = "" if pd.isna(closed_at) else closed_at.isoformat()

        if candidate.side == "LONG":
            sl_hit = low <= candidate.sl
            tp2_hit = high >= candidate.tp2
            tp1_hit = high >= candidate.tp1
        else:
            sl_hit = high >= candidate.sl
            tp2_hit = low <= candidate.tp2
            tp1_hit = low <= candidate.tp1

        tp1_seen = tp1_seen or tp1_hit or tp2_hit
        tp2_seen = tp2_seen or tp2_hit
        sl_seen = sl_seen or sl_hit

        # Conservative production rule: if SL and TP happen in the same candle, SL wins.
        if sl_hit:
            return ShadowOutcome("LOSS", "SL", -1.0, close_timestamp, resolution_hours(candidate.timestamp_utc, close_timestamp), tp1_seen, tp2_seen, True)
        if tp2_hit:
            rr = candidate.original_rr if candidate.original_rr > 0 else 2.0
            return ShadowOutcome("WIN_TP2", "TP2", rr, close_timestamp, resolution_hours(candidate.timestamp_utc, close_timestamp), True, True, sl_seen)
        if tp1_hit:
            return ShadowOutcome("WIN_TP1", "TP1", 1.0, close_timestamp, resolution_hours(candidate.timestamp_utc, close_timestamp), True, False, sl_seen)

    return ShadowOutcome("OPEN", "", 0.0, "", 0.0, tp1_seen, tp2_seen, sl_seen)


def resolution_hours(start: str, end: str) -> float:
    start_ts = pd.to_datetime(start, utc=True, errors="coerce")
    end_ts = pd.to_datetime(end, utc=True, errors="coerce")
    if pd.isna(start_ts) or pd.isna(end_ts):
        return 0.0
    return max(float((end_ts - start_ts).total_seconds() / 3600), 0.0)


def review_candidate(
    candidate: RejectedCandidate,
    *,
    lookahead_hours: int = DEFAULT_LOOKAHEAD_HOURS,
    session: requests.Session | None = None,
    candle_provider: Callable[[RejectedCandidate, int], pd.DataFrame] | None = None,
) -> ShadowOutcome:
    if candle_provider is not None:
        candles = candle_provider(candidate, lookahead_hours)
        return evaluate_hypothetical_outcome(candidate, candles)

    timestamp = pd.to_datetime(candidate.timestamp_utc, utc=True, errors="coerce")
    if pd.isna(timestamp):
        return ShadowOutcome("OPEN", "", 0.0, "", 0.0, False, False, False)
    active_session = session or build_session()
    start_ms = int(timestamp.timestamp() * 1000)
    end_ts = min(timestamp + pd.Timedelta(hours=lookahead_hours), pd.Timestamp.now(tz="UTC"))
    end_ms = int(end_ts.timestamp() * 1000)
    candles = fetch_futures_klines(active_session, candidate.symbol, start_ms, end_ms)
    return evaluate_hypothetical_outcome(candidate, candles)


def build_output_record(candidate: RejectedCandidate, outcome: ShadowOutcome) -> dict[str, Any]:
    return {
        "canonical_signal_key": candidate.canonical_signal_key,
        "timestamp_utc": candidate.timestamp_utc,
        "symbol": candidate.symbol,
        "side": candidate.side,
        "entry": normalize_float(candidate.entry),
        "sl": normalize_float(candidate.sl),
        "tp1": normalize_float(candidate.tp1),
        "tp2": normalize_float(candidate.tp2),
        "original_rr": f"{candidate.original_rr:.4f}" if candidate.original_rr else "",
        "rejection_reason": candidate.rejection_reason,
        "rejection_detail": candidate.rejection_detail,
        "score": candidate.score,
        "confidence": candidate.confidence,
        "tier": candidate.tier,
        "session": candidate.session,
        "hypothetical_outcome": outcome.hypothetical_outcome,
        "hypothetical_r": f"{outcome.hypothetical_r:.4f}",
        "tp1_hit": int(outcome.tp1_hit),
        "tp2_hit": int(outcome.tp2_hit),
        "sl_hit": int(outcome.sl_hit),
        "close_timestamp": outcome.close_timestamp,
        "resolution_hours": f"{outcome.resolution_hours:.4f}",
        "source": candidate.source,
    }


class RejectedOutcomeShadowLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            with self.path.open("w", newline="", encoding="utf-8") as handle:
                csv.DictWriter(handle, fieldnames=FIELDNAMES).writeheader()
            return
        try:
            existing = pd.read_csv(self.path)
        except pd.errors.EmptyDataError:
            existing = pd.DataFrame(columns=FIELDNAMES)
        changed = False
        for column in FIELDNAMES:
            if column not in existing.columns:
                existing[column] = ""
                changed = True
        if changed or list(existing.columns) != FIELDNAMES:
            existing = existing[FIELDNAMES]
            existing.to_csv(self.path, index=False)

    def existing_keys(self) -> set[str]:
        try:
            df = pd.read_csv(self.path, usecols=["canonical_signal_key"])
        except (FileNotFoundError, pd.errors.EmptyDataError, ValueError):
            return set()
        return {str(value).strip() for value in df["canonical_signal_key"].dropna() if str(value).strip()}

    def append_records(self, records: list[dict[str, Any]]) -> int:
        if not records:
            return 0
        existing = self.existing_keys()
        unique_records = []
        for record in records:
            key = str(record.get("canonical_signal_key", "")).strip()
            if not key or key in existing:
                continue
            existing.add(key)
            unique_records.append(record)
        if not unique_records:
            return 0
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore")
            for record in unique_records:
                writer.writerow(record)
        return len(unique_records)


def run_backfill(
    input_paths: list[Path],
    output_path: Path,
    *,
    lookahead_hours: int = DEFAULT_LOOKAHEAD_HOURS,
    limit: int | None = None,
    dry_run: bool = False,
    candle_provider: Callable[[RejectedCandidate, int], pd.DataFrame] | None = None,
) -> dict[str, Any]:
    candidates: list[RejectedCandidate] = []
    seen: set[str] = set()
    for path in input_paths:
        for candidate in load_rejected_candidates(path, source=path.name):
            if candidate.canonical_signal_key in seen:
                continue
            seen.add(candidate.canonical_signal_key)
            candidates.append(candidate)
    if limit is not None:
        candidates = candidates[: max(limit, 0)]

    records: list[dict[str, Any]] = []
    session = None if candle_provider is not None else build_session()
    candle_cache: dict[str, pd.DataFrame] = {}
    if candle_provider is None and session is not None:
        candle_cache = build_candle_cache(candidates, lookahead_hours=lookahead_hours, session=session)
    for candidate in candidates:
        try:
            if candle_provider is None:
                candles = candles_for_candidate_from_cache(candidate, candle_cache, lookahead_hours=lookahead_hours)
                outcome = evaluate_hypothetical_outcome(candidate, candles)
            else:
                outcome = review_candidate(candidate, lookahead_hours=lookahead_hours, session=session, candle_provider=candle_provider)
            records.append(build_output_record(candidate, outcome))
        except Exception as exc:
            LOGGER.warning("Rejected outcome shadow skipped %s: %s", candidate.canonical_signal_key, exc)

    written = 0
    if not dry_run:
        written = RejectedOutcomeShadowLogger(output_path).append_records(records)

    return {
        "input_rows_usable": len(candidates),
        "evaluated": len(records),
        "written": written,
        "dry_run": dry_run,
        "output_path": str(output_path),
    }


def summarize_records(records: pd.DataFrame) -> dict[str, Any]:
    if records.empty:
        return {"rows": 0, "closed": 0, "wins": 0, "losses": 0, "win_rate": "N/A", "net_r": 0.0}
    outcomes = records["hypothetical_outcome"].fillna("").astype(str).str.upper()
    closed = records[outcomes.isin(["WIN_TP1", "WIN_TP2", "LOSS"])].copy()
    wins = int(closed["hypothetical_outcome"].astype(str).str.upper().str.startswith("WIN").sum()) if not closed.empty else 0
    losses = int((closed["hypothetical_outcome"].astype(str).str.upper() == "LOSS").sum()) if not closed.empty else 0
    net_r = pd.to_numeric(closed.get("hypothetical_r", pd.Series(dtype=float)), errors="coerce").fillna(0).sum() if not closed.empty else 0.0
    return {
        "rows": int(len(records)),
        "closed": int(len(closed)),
        "wins": wins,
        "losses": losses,
        "win_rate": f"{(wins / len(closed) * 100):.2f}%" if len(closed) else "N/A",
        "net_r": round(float(net_r), 4),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill hypothetical outcomes for rejected scanner candidates.")
    parser.add_argument("--base-dir", default=str(Path(__file__).resolve().parents[1]), help="Project base directory.")
    parser.add_argument("--output", default="", help="Output CSV path. Defaults to logs/rejected_outcome_shadow.csv.")
    parser.add_argument("--lookahead-hours", type=int, default=env_int("REVIEW_LOOKAHEAD_HOURS", DEFAULT_LOOKAHEAD_HOURS))
    parser.add_argument("--limit", type=int, default=None, help="Optional max candidates to evaluate.")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate without writing output CSV.")
    args = parser.parse_args(argv)

    base_dir = Path(args.base_dir)
    input_paths = [base_dir / "logs" / "signals.csv", base_dir / "logs" / "rejected_signals.csv"]
    output_path = Path(args.output) if args.output else base_dir / "logs" / "rejected_outcome_shadow.csv"

    if not env_bool("REJECTED_OUTCOME_SHADOW_ENABLED", True):
        print("Rejected outcome shadow disabled by REJECTED_OUTCOME_SHADOW_ENABLED")
        return 0
    if env_bool("REJECTED_OUTCOME_LIVE_ENABLED", False):
        LOGGER.warning("REJECTED_OUTCOME_LIVE_ENABLED is ignored in V1; analytics-only mode remains active.")

    result = run_backfill(
        input_paths,
        output_path,
        lookahead_hours=args.lookahead_hours,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    print(
        "Rejected Outcome Shadow V1\n"
        f"Usable rejected rows: {result['input_rows_usable']}\n"
        f"Evaluated: {result['evaluated']}\n"
        f"Written: {result['written']}\n"
        f"Dry run: {result['dry_run']}\n"
        f"Output: {result['output_path']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
