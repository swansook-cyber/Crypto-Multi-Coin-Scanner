# -*- coding: utf-8 -*-
"""Prospective cross-scan correlated-exposure shadow guard.

The module is observational only.  It reconstructs active SENT lifecycles from
the production signal journal, compares a new live candidate with same-side
open exposure, and writes a shadow decision.  It never changes the candidate,
the signal journal, scanner selection, or Telegram/Cornix routing.
"""

from __future__ import annotations

import csv
import hashlib
import math
import json
import os
import stat
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pandas as pd
import numpy as np

from core.signal_identity import canonical_signal_key, normalize_side, normalize_symbol


SHADOW_VERSION = "CROSS_SCAN_EXPOSURE_SHADOW_V1"
SELECTION_RULE = "FIRST_IN_CLUSTER"

FIELDNAMES = [
    "timestamp_utc",
    "canonical_signal_key",
    "signal_key",
    "symbol",
    "side",
    "candidate_setup_strength",
    "candidate_confidence",
    "existing_open_count",
    "existing_same_side_count",
    "shadow_retained_open_count",
    "shadow_retained_same_side_count",
    "correlated_open_count",
    "correlated_symbols",
    "max_pair_correlation",
    "cluster_exposure_count",
    "shadow_decision",
    "shadow_reason",
    "cluster_id",
    "representative_signal_key",
    "representative_symbol",
    "representative_selection_rule",
    "oldest_open_age_minutes",
    "btc_regime",
    "session",
    "exposure_state_source",
    "stale_open_excluded_count",
    "correlation_lookback_bars",
    "correlation_min_observations",
    "correlation_threshold",
    "max_correlated_open_positions",
    "live_result",
    "final_outcome",
    "final_r",
    "execution_truth_status",
    "execution_pnl_usdt",
    "execution_r",
    "prospective_start_timestamp_utc",
    "shadow_version",
    "generated_at_utc",
    "persistence_status",
]


class ShadowLockUnavailable(RuntimeError):
    """Raised when the short-lived persistence lock cannot be acquired."""


class ShadowPersistenceLock:
    """Cross-platform advisory OS lock; file existence never implies ownership."""

    def __init__(self, path: Path, *, timeout_seconds: float = 0.25, poll_seconds: float = 0.01) -> None:
        self.path = path
        self.timeout_seconds = max(0.0, timeout_seconds)
        self.poll_seconds = max(0.001, poll_seconds)
        self._handle: Any | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._handle = handle
                return
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    handle.close()
                    raise ShadowLockUnavailable(f"shadow persistence lock unavailable: {self.path}")
                time.sleep(self.poll_seconds)

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "ShadowPersistenceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()


def _atomic_write(path: Path, writer: Any) -> None:
    """Write in the target directory, fsync, then atomically replace target."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        if path.exists():
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def atomic_write_text(path: Path, value: str) -> None:
    _atomic_write(path, lambda handle: handle.write(value))


def atomic_write_csv(path: Path, rows: list[Mapping[str, Any]]) -> None:
    def write(handle: Any) -> None:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in FIELDNAMES} for row in rows)

    _atomic_write(path, write)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _text(value: Any) -> str:
    if value is None:
        return ""
    result = str(value).strip()
    return "" if result.lower() in {"nan", "nat", "none", "null"} else result


def _float(value: Any) -> float | None:
    result = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    return None if pd.isna(result) else float(result)


def _timestamp(value: Any) -> pd.Timestamp | None:
    result = pd.to_datetime(value, utc=True, errors="coerce")
    return None if pd.isna(result) else result


def _signal_value(signal: Any, *names: str, default: Any = "") -> Any:
    for name in names:
        if isinstance(signal, Mapping) and name in signal:
            return signal[name]
        if hasattr(signal, name):
            return getattr(signal, name)
    return default


def signal_key(signal: Any) -> str:
    return canonical_signal_key(
        symbol=_signal_value(signal, "symbol"),
        side=_signal_value(signal, "direction", "side"),
        timestamp=_signal_value(signal, "timestamp", "timestamp_utc"),
        entry=_signal_value(signal, "entry"),
        signal_id=_signal_value(signal, "signal_id"),
        candidate_id=_signal_value(signal, "candidate_id"),
    )


@dataclass(frozen=True)
class OpenExposure:
    canonical_signal_key: str
    symbol: str
    side: str
    timestamp: pd.Timestamp
    btc_regime: str = ""
    session: str = ""
    setup_strength: str = ""
    confidence: str = ""


@dataclass(frozen=True)
class ExposureState:
    positions: tuple[OpenExposure, ...]
    observed_positions: tuple[OpenExposure, ...] = ()
    stale_excluded_count: int = 0
    source: str = "signals.csv:SENT+OPEN"


@dataclass(frozen=True)
class ShadowRule:
    correlation_threshold: float = 0.75
    correlation_lookback_bars: int = 72
    correlation_min_observations: int = 48
    max_correlated_open_positions: int = 1
    stale_after_hours: float = 24.0


def load_open_exposure_state(
    journal_path: Path,
    *,
    now: Any | None = None,
    stale_after_hours: float = 24.0,
) -> ExposureState:
    """Load active production lifecycles from the outcome-updated journal.

    Only explicit ``signal_status=SENT`` and ``result=OPEN`` rows are eligible.
    WIN/LOSS/other states are never inferred to be open.  A bounded age is a
    fail-safe for an outcome watcher that stopped updating the journal.
    """

    try:
        data = pd.read_csv(journal_path, low_memory=False)
    except (FileNotFoundError, pd.errors.EmptyDataError, OSError):
        return ExposureState((), ())
    required = {"timestamp", "symbol", "side", "signal_status", "result", "entry"}
    if data.empty or not required.issubset(data.columns):
        return ExposureState((), ())

    statuses = data["signal_status"].fillna("").astype(str).str.strip().str.lower()
    results = data["result"].fillna("").astype(str).str.strip().str.upper()
    working = data[statuses.eq("sent") & results.eq("OPEN")].copy()
    if working.empty:
        return ExposureState((), ())

    working["_timestamp"] = pd.to_datetime(working["timestamp"], utc=True, errors="coerce")
    working = working[working["_timestamp"].notna()].copy()
    current = _timestamp(now) or pd.Timestamp.now(tz="UTC")
    ages = current - working["_timestamp"]
    stale = ages > pd.Timedelta(hours=max(0.0, stale_after_hours))
    future = ages < pd.Timedelta(0)
    stale_count = int((stale | future).sum())
    working = working[~stale & ~future].copy()

    exposures: list[OpenExposure] = []
    seen: set[str] = set()
    for _, row in working.sort_values("_timestamp").iterrows():
        key = canonical_signal_key(
            symbol=row.get("symbol", ""),
            side=row.get("side", ""),
            timestamp=row.get("timestamp", ""),
            entry=row.get("entry", ""),
        )
        if not key or key in seen:
            continue
        seen.add(key)
        exposures.append(
            OpenExposure(
                canonical_signal_key=key,
                symbol=normalize_symbol(row.get("symbol", "")),
                side=normalize_side(row.get("side", "")),
                timestamp=row["_timestamp"],
                btc_regime=_text(row.get("btc_regime", "")),
                session=_text(row.get("market_session", "")),
                setup_strength=_text(row.get("setup_strength", "")),
                confidence=_text(row.get("confidence", "")),
            )
        )
    resolved = tuple(exposures)
    return ExposureState(resolved, resolved, stale_count)


def timestamped_close_series(
    value: Any,
    *,
    timestamp_column: str = "close_time",
    now: Any | None = None,
) -> pd.Series:
    """Return unique, sorted, UTC-aware closed prices indexed by candle close."""

    if isinstance(value, pd.DataFrame):
        if timestamp_column not in value.columns or "close" not in value.columns:
            return pd.Series(dtype=float)
        timestamps = pd.to_datetime(value[timestamp_column], utc=True, errors="coerce")
        prices = pd.to_numeric(value["close"], errors="coerce")
        frame = pd.DataFrame({"timestamp": timestamps, "close": prices})
    elif isinstance(value, pd.Series) and isinstance(value.index, pd.DatetimeIndex):
        frame = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(value.index, utc=True, errors="coerce"),
                "close": pd.to_numeric(value.to_numpy(), errors="coerce"),
            }
        )
    else:
        # Positional histories are unsafe evidence because missing candles cannot
        # be aligned. They deliberately fail open rather than relying on row order.
        return pd.Series(dtype=float)
    frame = frame.dropna(subset=["timestamp", "close"])
    frame = frame[np.isfinite(frame["close"]) & frame["close"].gt(0)]
    cutoff = _timestamp(now) or pd.Timestamp.now(tz="UTC")
    frame = frame[frame["timestamp"].le(cutoff)]
    frame = frame.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    if frame.empty:
        return pd.Series(dtype=float)
    return pd.Series(frame["close"].to_numpy(), index=pd.DatetimeIndex(frame["timestamp"]), name="close")


def pair_return_correlation(
    first: Any,
    second: Any,
    *,
    lookback_bars: int = 72,
    min_observations: int = 48,
) -> tuple[float | None, int]:
    left = timestamped_close_series(first)
    right = timestamped_close_series(second)
    aligned_prices = (
        pd.concat([left.rename("left"), right.rename("right")], axis=1, join="inner")
        .dropna()
        .sort_index()
        .tail(max(2, lookback_bars + 1))
    )
    returns = aligned_prices.pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan).dropna()
    if len(returns) < min_observations:
        return None, len(returns)
    if returns["left"].nunique(dropna=True) <= 1 or returns["right"].nunique(dropna=True) <= 1:
        return None, len(returns)
    correlation = returns["left"].corr(returns["right"])
    if pd.isna(correlation) or not math.isfinite(float(correlation)):
        return None, len(returns)
    return float(correlation), len(returns)


def _cluster_id(side: str, representative_key: str) -> str:
    digest = hashlib.sha256(f"{side}|{representative_key}".encode("utf-8")).hexdigest()[:16]
    return f"xscan:v1:{side}:{digest}"


def evaluate_shadow_candidate(
    signal: Any,
    state: ExposureState,
    price_history: Mapping[str, Any],
    *,
    rule: ShadowRule | None = None,
    now: Any | None = None,
    prospective_start_timestamp_utc: str = "",
) -> dict[str, Any]:
    rule = rule or ShadowRule()
    candidate_symbol = normalize_symbol(_signal_value(signal, "symbol"))
    candidate_side = normalize_side(_signal_value(signal, "direction", "side"))
    candidate_key = signal_key(signal)
    observed_positions = state.observed_positions or state.positions
    observed_same_side = [item for item in observed_positions if item.side == candidate_side]
    same_side = [item for item in state.positions if item.side == candidate_side]
    correlations: list[tuple[OpenExposure, float]] = []
    evaluated_pairs = 0
    for exposure in same_side:
        correlation, observations = pair_return_correlation(
            price_history.get(candidate_symbol),
            price_history.get(exposure.symbol),
            lookback_bars=rule.correlation_lookback_bars,
            min_observations=rule.correlation_min_observations,
        )
        if correlation is None:
            continue
        evaluated_pairs += 1
        if correlation >= rule.correlation_threshold:
            correlations.append((exposure, correlation))

    correlations.sort(key=lambda item: (item[0].timestamp, item[0].canonical_signal_key))
    correlated_positions = [item[0] for item in correlations]
    max_pair = max((item[1] for item in correlations), default=None)
    if not same_side:
        decision, reason = "ALLOW", "no_open_same_side_exposure"
    elif evaluated_pairs == 0:
        decision, reason = "ALLOW", "correlation_unavailable_fail_open"
    elif not correlated_positions:
        decision, reason = "ALLOW", "same_side_exposure_not_materially_correlated"
    elif len(correlated_positions) >= max(1, rule.max_correlated_open_positions):
        decision, reason = "WOULD_BLOCK", "correlated_same_side_cluster_threshold_exceeded"
    else:
        decision, reason = "CAUTION", "correlated_same_side_exposure_below_threshold"

    representative = correlated_positions[0] if correlated_positions else None
    current = _timestamp(now) or pd.Timestamp.now(tz="UTC")
    oldest_age = ""
    if same_side:
        oldest = min(item.timestamp for item in same_side)
        oldest_age = f"{max(0.0, (current - oldest).total_seconds() / 60.0):.2f}"
    timestamp = _signal_value(signal, "timestamp", "timestamp_utc")
    if hasattr(timestamp, "isoformat"):
        timestamp = timestamp.isoformat()
    generated_at = utc_now_iso()
    return {
        "timestamp_utc": _text(timestamp),
        "canonical_signal_key": candidate_key,
        "signal_key": candidate_key,
        "symbol": candidate_symbol,
        "side": candidate_side,
        "candidate_setup_strength": _text(_signal_value(signal, "setup_strength", "confidence")),
        "candidate_confidence": _text(_signal_value(signal, "confidence")),
        "existing_open_count": len(observed_positions),
        "existing_same_side_count": len(observed_same_side),
        "shadow_retained_open_count": len(state.positions),
        "shadow_retained_same_side_count": len(same_side),
        "correlated_open_count": len(correlated_positions),
        "correlated_symbols": ",".join(item.symbol for item in correlated_positions),
        "max_pair_correlation": "" if max_pair is None else f"{max_pair:.6f}",
        "cluster_exposure_count": len(correlated_positions) + 1 if correlated_positions else 1,
        "shadow_decision": decision,
        "shadow_reason": reason,
        "cluster_id": _cluster_id(candidate_side, representative.canonical_signal_key) if representative else "",
        "representative_signal_key": representative.canonical_signal_key if representative else candidate_key,
        "representative_symbol": representative.symbol if representative else candidate_symbol,
        "representative_selection_rule": SELECTION_RULE,
        "oldest_open_age_minutes": oldest_age,
        "btc_regime": _text(_signal_value(signal, "btc_regime")),
        "session": _text(_signal_value(signal, "market_session", "session")),
        "exposure_state_source": state.source,
        "stale_open_excluded_count": state.stale_excluded_count,
        "correlation_lookback_bars": rule.correlation_lookback_bars,
        "correlation_min_observations": rule.correlation_min_observations,
        "correlation_threshold": f"{rule.correlation_threshold:.4f}",
        "max_correlated_open_positions": rule.max_correlated_open_positions,
        "live_result": "SENT_UNCHANGED",
        "final_outcome": "",
        "final_r": "",
        "execution_truth_status": "",
        "execution_pnl_usdt": "",
        "execution_r": "",
        "prospective_start_timestamp_utc": prospective_start_timestamp_utc,
        "shadow_version": SHADOW_VERSION,
        "generated_at_utc": generated_at,
    }


def modeled_r(row: Mapping[str, Any]) -> float | None:
    direct = _float(row.get("net_r_estimate", ""))
    if direct is not None:
        return direct
    outcome = _text(row.get("result", "")).upper()
    if outcome == "LOSS":
        return -1.0
    if outcome != "WIN":
        return None
    rr = _float(row.get("risk_reward", row.get("rr", "")))
    if _text(row.get("hit_target", "")).upper() == "TP2" and rr is not None:
        return rr
    return min(rr, 1.2) if rr is not None else 1.0


def _signal_outcomes(signals_path: Path) -> dict[str, tuple[str, float | None]]:
    try:
        data = pd.read_csv(signals_path, low_memory=False)
    except (FileNotFoundError, pd.errors.EmptyDataError, OSError):
        return {}
    outcomes: dict[str, tuple[str, float | None]] = {}
    for _, row in data.iterrows():
        outcome = _text(row.get("result", "")).upper()
        if outcome not in {"WIN", "LOSS"}:
            continue
        key = canonical_signal_key(
            symbol=row.get("symbol", ""), side=row.get("side", ""),
            timestamp=row.get("timestamp", ""), entry=row.get("entry", ""),
        )
        outcomes[key] = (outcome, modeled_r(row))
    return outcomes


def _execution_outcomes(execution_path: Path | None) -> dict[str, tuple[str, str, str]]:
    if execution_path is None:
        return {}
    try:
        data = pd.read_csv(execution_path, low_memory=False)
    except (FileNotFoundError, pd.errors.EmptyDataError, OSError):
        return {}
    outcomes: dict[str, tuple[str, str, str]] = {}
    for _, row in data.iterrows():
        key = _text(row.get("canonical_signal_key", ""))
        if not key:
            continue
        status = "/".join(
            value for value in (
                _text(row.get("match_status", "")),
                _text(row.get("execution_finality", "")),
                _text(row.get("accounting_finality", "")),
            ) if value
        )
        outcomes[key] = (
            status,
            _text(row.get("execution_pnl_usdt", "")),
            _text(row.get("execution_r", "")),
        )
    return outcomes


def refresh_shadow_outcomes(
    shadow_path: Path,
    signals_path: Path,
    execution_path: Path | None = None,
    *,
    lock_path: Path | None = None,
    lock_timeout_seconds: float = 0.25,
) -> int:
    """Enrich existing shadow rows without ever writing the signal journal."""

    signal_results = _signal_outcomes(signals_path)
    execution_results = _execution_outcomes(execution_path)
    try:
        lock = ShadowPersistenceLock(
            lock_path or shadow_path.with_suffix(".lock"), timeout_seconds=lock_timeout_seconds
        )
        lock.acquire()
    except ShadowLockUnavailable:
        return 0
    try:
        try:
            with shadow_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
        except (FileNotFoundError, OSError):
            return 0
        if not rows:
            return 0
        changed = 0
        for row in rows:
            row_changed = False
            key = _text(row.get("canonical_signal_key", row.get("signal_key", "")))
            if key in signal_results:
                outcome, result_r = signal_results[key]
                result_r_text = "" if result_r is None else f"{result_r:.6f}"
                if row.get("final_outcome", "") != outcome or row.get("final_r", "") != result_r_text:
                    row["final_outcome"] = outcome
                    row["final_r"] = result_r_text
                    row_changed = True
            if key in execution_results:
                status, pnl, result_r = execution_results[key]
                updates = {
                    "execution_truth_status": status,
                    "execution_pnl_usdt": pnl,
                    "execution_r": result_r,
                }
                for field, value in updates.items():
                    if row.get(field, "") != value:
                        row[field] = value
                        row_changed = True
            if row_changed:
                changed += 1
        if changed:
            atomic_write_csv(shadow_path, rows)
        return changed
    finally:
        lock.release()


class CrossScanExposureShadowLogger:
    """Idempotent, append-only prospective logger with a persisted boundary."""

    def __init__(
        self,
        path: Path,
        state_path: Path,
        signals_path: Path,
        *,
        execution_path: Path | None = None,
        rule: ShadowRule | None = None,
        prospective_start_timestamp_utc: str | None = None,
        lock_path: Path | None = None,
        lock_timeout_seconds: float = 0.25,
    ) -> None:
        self.path = path
        self.state_path = state_path
        self.lock_path = lock_path or path.with_suffix(".lock")
        self.lock_timeout_seconds = lock_timeout_seconds
        self.signals_path = signals_path
        self.execution_path = execution_path
        self.rule = rule or ShadowRule()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with ShadowPersistenceLock(self.lock_path, timeout_seconds=self.lock_timeout_seconds):
            self.prospective_start_timestamp_utc = self._resolve_start(prospective_start_timestamp_utc)
            self._ensure_header()

    def _resolve_start(self, explicit: str | None) -> str:
        if self.state_path.exists():
            try:
                state = json.loads(self.state_path.read_text(encoding="utf-8"))
                stored = _text(state.get("CROSS_SCAN_EXPOSURE_SHADOW_START_UTC", ""))
                if stored and _timestamp(stored) is not None:
                    return stored
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid cross-scan shadow state: {self.state_path}") from exc
            raise ValueError(f"missing prospective boundary in shadow state: {self.state_path}")
        start_value = _timestamp(explicit or utc_now_iso())
        if start_value is None:
            raise ValueError("invalid CROSS_SCAN_EXPOSURE_SHADOW_START_UTC")
        start = start_value.isoformat().replace("+00:00", "Z")
        payload = {
            "CROSS_SCAN_EXPOSURE_SHADOW_START_UTC": start,
            "shadow_version": SHADOW_VERSION,
            "correlation_threshold": self.rule.correlation_threshold,
            "correlation_lookback_bars": self.rule.correlation_lookback_bars,
            "correlation_min_observations": self.rule.correlation_min_observations,
            "max_correlated_open_positions": self.rule.max_correlated_open_positions,
            "representative_selection_rule": SELECTION_RULE,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.state_path, json.dumps(payload, indent=2))
        return start

    def _ensure_header(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            atomic_write_csv(self.path, [])
            return
        try:
            existing = pd.read_csv(self.path, low_memory=False)
        except (pd.errors.EmptyDataError, OSError):
            return
        if list(existing.columns) == FIELDNAMES:
            return
        for field in FIELDNAMES:
            if field not in existing.columns:
                existing[field] = ""
        atomic_write_csv(self.path, existing.to_dict("records"))

    def _read_rows(self) -> list[dict[str, str]]:
        try:
            with self.path.open("r", encoding="utf-8", newline="") as handle:
                return list(csv.DictReader(handle))
        except (FileNotFoundError, OSError):
            return []

    def log_candidate(
        self,
        signal: Any,
        price_history: Mapping[str, Any],
        *,
        now: Any | None = None,
    ) -> dict[str, Any]:
        candidate_timestamp = _timestamp(_signal_value(signal, "timestamp", "timestamp_utc"))
        boundary = _timestamp(self.prospective_start_timestamp_utc)
        if candidate_timestamp is None or boundary is None:
            record = evaluate_shadow_candidate(
                signal, ExposureState((), ()), price_history, rule=self.rule, now=now,
                prospective_start_timestamp_utc=self.prospective_start_timestamp_utc,
            )
            record["persistence_status"] = "INVALID_TIMESTAMP_NOT_WRITTEN"
            return record
        if candidate_timestamp < boundary:
            record = evaluate_shadow_candidate(
                signal, ExposureState((), ()), price_history, rule=self.rule, now=now,
                prospective_start_timestamp_utc=self.prospective_start_timestamp_utc,
            )
            record["persistence_status"] = "EXCLUDED_PRE_BOUNDARY"
            return record

        observed_state = load_open_exposure_state(
            self.signals_path, now=now, stale_after_hours=self.rule.stale_after_hours
        )
        try:
            lock = ShadowPersistenceLock(self.lock_path, timeout_seconds=self.lock_timeout_seconds)
            lock.acquire()
        except ShadowLockUnavailable:
            record = evaluate_shadow_candidate(
                signal, observed_state, price_history, rule=self.rule, now=now,
                prospective_start_timestamp_utc=self.prospective_start_timestamp_utc,
            )
            record["persistence_status"] = "LOCK_UNAVAILABLE_NOT_WRITTEN"
            return record
        try:
            rows = self._read_rows()
            suppressed_keys = {
                _text(row.get("canonical_signal_key", ""))
                for row in rows
                if _text(row.get("shadow_decision", "")).upper() == "WOULD_BLOCK"
            }
            retained = tuple(
                item for item in observed_state.positions
                if item.canonical_signal_key not in suppressed_keys
            )
            state = ExposureState(
                positions=retained,
                observed_positions=observed_state.positions,
                stale_excluded_count=observed_state.stale_excluded_count,
                source=observed_state.source,
            )
            record = evaluate_shadow_candidate(
                signal,
                state,
                price_history,
                rule=self.rule,
                now=now,
                prospective_start_timestamp_utc=self.prospective_start_timestamp_utc,
            )
            key = _text(record.get("canonical_signal_key", ""))
            known_keys = {_text(row.get("canonical_signal_key", "")) for row in rows}
            if key and key not in known_keys:
                record["persistence_status"] = "PERSISTED"
                atomic_write_csv(self.path, [*rows, record])
            else:
                record["persistence_status"] = "DUPLICATE_NOT_WRITTEN"
            return record
        finally:
            lock.release()

    def refresh_outcomes(self) -> int:
        return refresh_shadow_outcomes(
            self.path,
            self.signals_path,
            self.execution_path,
            lock_path=self.lock_path,
            lock_timeout_seconds=self.lock_timeout_seconds,
        )
