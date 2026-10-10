"""Derived historical lifecycle truth for reporting (never trading decisions).

The source journals remain immutable.  This module reconciles their SENT
population with telemetry candidates, reconstructs the 50/50 TP1+TP2 path from
closed Binance Futures candles, and overlays the versioned derived facts for
reporting consumers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import pandas as pd
import requests

from core.post_tp1_lifecycle import evaluate_reporting_lifecycle
from core.signal_identity import canonical_signal_key, normalize_side, normalize_symbol, normalize_timestamp


SOURCE = "HISTORICAL_PRICE_PATH_RECONSTRUCTION"
RECONSTRUCTION_VERSION = "historical_lifecycle_v1"
PRICE_SOURCE = "BINANCE_USDM_FUTURES"
PRICE_INTERVAL = "15m"
TERMINAL_STATES = {"TP2_WIN", "TP1_THEN_ORIGINAL_SL", "ORIGINAL_SL"}
AMBIGUOUS_STATE = "SAME_CANDLE_AMBIGUOUS"
UNRESOLVED_STATES = {AMBIGUOUS_STATE, "UNRESOLVED_REMAINDER"}
LIVE_OPEN_STATE = "LIVE_OPEN_REMAINDER"
HISTORICAL_UNKNOWN_STATE = "HISTORICAL_REMAINDER_UNKNOWN"


def _text(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    number = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.notna(number):
        return float(number) == 1.0
    return _text(value).lower() in {"true", "yes", "y", "on"}


def parse_utc_mixed(value: Any) -> Any:
    """Parse scalar or vector timestamps with mixed precision without shifting UTC.

    Pandas 2 applies one inferred format to an entire Series.  Production joins
    microsecond CSV values with second-precision SQLite values, so that default
    can silently coerce valid DB timestamps to NaT.
    """
    try:
        return pd.to_datetime(value, utc=True, format="mixed", errors="coerce")
    except TypeError:  # Compatibility with pandas versions predating format="mixed".
        return pd.to_datetime(value, utc=True, errors="coerce")


def _value(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = row.get(name)
        if _text(value):
            return value
    return ""


def signal_key(row: Mapping[str, Any]) -> str:
    existing = _text(_value(row, "canonical_signal_key"))
    if existing.startswith(("sig:v1:", "id:v1:")):
        return existing
    return canonical_signal_key(
        symbol=_value(row, "symbol", "normalized_symbol"),
        side=_value(row, "side", "direction", "normalized_direction"),
        timestamp=_value(row, "timestamp", "timestamp_utc", "signal_timestamp"),
        entry=_value(row, "entry", "entry_low"),
    )


def _sent_rows(frame: pd.DataFrame, *, history: bool = False) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    rows = frame.to_dict("records")
    if history and "signal_status" not in frame.columns:
        return rows
    result: list[dict[str, Any]] = []
    for row in rows:
        status = _text(_value(row, "signal_status", "decision")).upper()
        if status in {"SENT", "LIVE_SENT"} or (history and not status):
            result.append(row)
    return result


def canonical_sent_population(
    current: pd.DataFrame,
    history: pd.DataFrame,
    db_candidates: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Return one row per canonical SENT signal with stable provenance."""
    selected: dict[str, dict[str, Any]] = {}
    counts = {"current_csv": 0, "history_only": 0, "db_only": 0, "duplicates_suppressed": 0}
    for label, rows in (
        ("CURRENT_SIGNALS", _sent_rows(current)),
        ("HISTORY_ONLY", _sent_rows(history, history=True)),
        ("DB_ONLY", _sent_rows(db_candidates if db_candidates is not None else pd.DataFrame())),
    ):
        for raw in rows:
            row = dict(raw)
            key = signal_key(row)
            if not key:
                fallback_identity = "|".join(_text(_value(row, name)) for name in ("timestamp", "timestamp_utc", "symbol", "side", "direction", "entry"))
                if not fallback_identity.replace("|", ""):
                    continue
                key = f"legacy:v1:{hashlib.sha256(fallback_identity.encode()).hexdigest()}"
            if key in selected:
                counts["duplicates_suppressed"] += 1
                # Preserve higher-priority row ownership while filling evidence
                # fields that exist only in the lower-priority representation.
                for column, value in row.items():
                    if not _text(selected[key].get(column)) and _text(value):
                        selected[key][column] = value
                continue
            row["canonical_signal_key"] = key
            row["population_provenance"] = label
            row.setdefault("signal_status", "sent")
            selected[key] = row
            if label == "CURRENT_SIGNALS":
                counts["current_csv"] += 1
            elif label == "HISTORY_ONLY":
                counts["history_only"] += 1
            else:
                counts["db_only"] += 1
    result = pd.DataFrame(selected.values())
    if not result.empty:
        timestamp = parse_utc_mixed(
            result.get("timestamp", result.get("timestamp_utc", pd.Series(index=result.index))),
        )
        result["timestamp"] = timestamp
        result = result.sort_values(["timestamp", "canonical_signal_key"], na_position="last").reset_index(drop=True)
    counts["canonical_sent"] = len(result)
    return result, counts


def load_db_sent_candidates(db_path: Path | str) -> pd.DataFrame:
    path = Path(db_path)
    if not path.exists():
        return pd.DataFrame()
    try:
        connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=1.5)
        try:
            return pd.read_sql_query(
                """SELECT canonical_signal_key,timestamp_utc AS timestamp,symbol,side,
                entry,sl,tp1,tp2,rr,decision,signal_status,source_mode,source_name
                FROM candidates WHERE UPPER(decision)='SENT' OR LOWER(signal_status)='sent'""",
                connection,
            )
        finally:
            connection.close()
    except (sqlite3.Error, pd.errors.DatabaseError):
        return pd.DataFrame()


def load_reconstructions(db_path: Path | str) -> pd.DataFrame:
    path = Path(db_path)
    if not path.exists():
        return pd.DataFrame()
    try:
        connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=1.5)
        try:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='historical_lifecycle_reconstructions'"
            ).fetchone()
            return pd.read_sql_query("SELECT * FROM historical_lifecycle_reconstructions", connection) if exists else pd.DataFrame()
        finally:
            connection.close()
    except (sqlite3.Error, pd.errors.DatabaseError):
        return pd.DataFrame()


def _truth_rank(row: Mapping[str, Any]) -> int:
    state = _text(row.get("lifecycle_state")).upper()
    source = _text(_value(row, "lifecycle_source", "source")).upper()
    source_mode = _text(row.get("source_mode")).upper()
    explicit_marker = _text(row.get("prospective_lifecycle_evidence")).lower() in {"1", "true", "yes"}
    source_marker = source.startswith(("LIVE", "PROSPECTIVE", "EXECUTION"))
    terminal = _truthy(row.get("lifecycle_terminal"))
    lifecycle_r = pd.to_numeric(pd.Series([row.get("lifecycle_r")]), errors="coerce").iloc[0]
    event_evidence = bool(_text(_value(row, "tp1_touch_utc", "tp1_touch_time_utc", "terminal_event_utc", "remainder_resolution_time_utc")))
    prospective_runtime_evidence = explicit_marker or source_marker or (
        source_mode == "PROSPECTIVE" and (event_evidence or (terminal and pd.notna(lifecycle_r)))
    )
    if state and source != SOURCE and prospective_runtime_evidence:
        return 3
    if source == SOURCE:
        return 2
    return 1


def apply_lifecycle_truth(population: pd.DataFrame, reconstructed: pd.DataFrame) -> pd.DataFrame:
    """Overlay lifecycle facts with prospective > reconstruction > legacy priority."""
    if population is None or population.empty:
        return pd.DataFrame() if population is None else population.copy()
    data = population.copy()
    for column, default in {
        "lifecycle_state": "", "lifecycle_r": pd.NA, "lifecycle_terminal": 0,
        "lifecycle_source": "", "tp1_touch_utc": "", "terminal_event_utc": "",
        "ambiguity_flag": 0, "ambiguity_reason": "",
    }.items():
        if column not in data.columns:
            data[column] = default
    facts = {}
    if reconstructed is not None and not reconstructed.empty:
        facts = reconstructed.set_index("canonical_signal_key").to_dict("index")
    for index, row in data.iterrows():
        key = _text(row.get("canonical_signal_key")) or signal_key(row.to_dict())
        data.at[index, "canonical_signal_key"] = key
        existing = row.to_dict()
        fact = facts.get(key)
        if fact and _truth_rank(existing) < 3:
            for target, source_column in {
                "lifecycle_state": "lifecycle_state", "lifecycle_r": "lifecycle_r",
                "tp1_touch_utc": "tp1_touch_utc", "terminal_event_utc": "terminal_event_utc",
                "ambiguity_flag": "ambiguity_flag", "ambiguity_reason": "ambiguity_reason",
            }.items():
                data.at[index, target] = fact.get(source_column)
            data.at[index, "lifecycle_source"] = SOURCE
            data.at[index, "lifecycle_terminal"] = int(_text(fact.get("lifecycle_state")).upper() in TERMINAL_STATES)
        state = _text(data.at[index, "lifecycle_state"]).upper()
        if not state:
            legacy_result = _text(row.get("result")).upper()
            legacy_target = _text(row.get("hit_target")).upper()
            if legacy_result == "LOSS":
                data.at[index, "lifecycle_state"] = "ORIGINAL_SL"
                data.at[index, "lifecycle_r"] = -1.0
                data.at[index, "lifecycle_terminal"] = 1
                data.at[index, "lifecycle_source"] = "LEGACY_FALLBACK"
            elif legacy_result == "WIN" and legacy_target in {"TP2", "TP3"}:
                entry = pd.to_numeric(pd.Series([_value(row, "entry")]), errors="coerce").iloc[0]
                stop = pd.to_numeric(pd.Series([_value(row, "sl", "stop_loss")]), errors="coerce").iloc[0]
                tp1 = pd.to_numeric(pd.Series([_value(row, "tp1")]), errors="coerce").iloc[0]
                tp2 = pd.to_numeric(pd.Series([_value(row, "tp2")]), errors="coerce").iloc[0]
                risk = abs(entry - stop) if pd.notna(entry) and pd.notna(stop) else 0.0
                if risk > 0 and pd.notna(tp1) and pd.notna(tp2):
                    data.at[index, "lifecycle_state"] = "TP2_WIN"
                    data.at[index, "lifecycle_r"] = 0.5 * abs(tp1 - entry) / risk + 0.5 * abs(tp2 - entry) / risk
                    data.at[index, "lifecycle_terminal"] = 1
                    data.at[index, "lifecycle_source"] = "LEGACY_FALLBACK"
            elif legacy_result == "WIN" and legacy_target in {"", "TP1"}:
                data.at[index, "lifecycle_state"] = HISTORICAL_UNKNOWN_STATE
                data.at[index, "lifecycle_terminal"] = 0
                data.at[index, "lifecycle_source"] = "LEGACY_FALLBACK"
            elif legacy_result in {"", "OPEN"}:
                data.at[index, "lifecycle_state"] = HISTORICAL_UNKNOWN_STATE
                data.at[index, "lifecycle_terminal"] = 0
                data.at[index, "lifecycle_source"] = "LEGACY_FALLBACK"
        elif state == "TP1_TOUCHED_REMAINDER_OPEN" and _truth_rank(existing) >= 3:
            data.at[index, "lifecycle_state"] = LIVE_OPEN_STATE
    numeric_r = pd.to_numeric(data["lifecycle_r"], errors="coerce")
    terminal = data["lifecycle_terminal"].map(_truthy)
    data["lifecycle_terminal"] = terminal
    if "result" not in data.columns:
        data["result"] = "OPEN"
    data.loc[terminal & numeric_r.gt(0), "result"] = "WIN"
    data.loc[terminal & numeric_r.lt(0), "result"] = "LOSS"
    data.loc[terminal & numeric_r.eq(0), "result"] = "BREAKEVEN"
    data.loc[data["lifecycle_state"].eq(LIVE_OPEN_STATE), "result"] = "OPEN"
    data.loc[data["lifecycle_state"].eq(HISTORICAL_UNKNOWN_STATE), "result"] = "UNKNOWN"
    data["lifecycle_r"] = numeric_r
    data["real_rr"] = numeric_r.where(numeric_r.notna(), pd.to_numeric(data.get("real_rr"), errors="coerce"))
    data["remainder_resolution_time_utc"] = data["terminal_event_utc"]
    return data


def load_canonical_reporting_population(
    signals_path: Path | str,
    history_path: Path | str,
    db_path: Path | str,
) -> tuple[pd.DataFrame, dict[str, int]]:
    def read(path: Path | str) -> pd.DataFrame:
        try:
            return pd.read_csv(path)
        except (FileNotFoundError, pd.errors.EmptyDataError, OSError):
            return pd.DataFrame()
    population, counts = canonical_sent_population(read(signals_path), read(history_path), load_db_sent_candidates(db_path))
    return apply_lifecycle_truth(population, load_reconstructions(db_path)), counts


@dataclass(frozen=True)
class Reconstruction:
    canonical_signal_key: str
    timestamp_utc: str
    symbol: str
    side: str
    lifecycle_state: str
    lifecycle_r: float | None
    tp1_touch_utc: str
    terminal_event_utc: str
    tp1_fraction: float
    remainder_fraction: float
    ambiguity_flag: int
    ambiguity_reason: str
    source: str
    reconstruction_version: str
    reconstructed_at_utc: str
    price_source: str
    price_interval: str
    closed_candle_cutoff_utc: str
    geometry_hash: str
    evidence_hash: str


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def reconstruct_one(row: Mapping[str, Any], candles: pd.DataFrame, cutoff_utc: str, *, reconstructed_at_utc: str = "") -> Reconstruction:
    timestamp = normalize_timestamp(_value(row, "timestamp", "timestamp_utc"))
    cutoff = parse_utc_mixed(cutoff_utc)
    signal_time = parse_utc_mixed(timestamp)
    ordered = candles.copy()
    if not ordered.empty and not pd.isna(signal_time):
        ordered["open_time"] = parse_utc_mixed(ordered["open_time"])
        ordered["close_time"] = parse_utc_mixed(ordered["close_time"])
        ordered = ordered[(ordered["open_time"] > signal_time) & (ordered["close_time"] <= cutoff)]
    if pd.isna(signal_time):
        state, lifecycle_r, tp1_at, terminal_at, ambiguous, reason = (
            "UNRESOLVED_REMAINDER", None, "", "", 0, "invalid_signal_timestamp"
        )
    else:
        try:
            result = evaluate_reporting_lifecycle(row, ordered)
            state = result.state
            if result.same_candle_ambiguous:
                state = AMBIGUOUS_STATE
            elif state == "TP1_TOUCHED_REMAINDER_OPEN":
                state = "UNRESOLVED_REMAINDER"
            lifecycle_r = result.lifecycle_r
            tp1_at = result.tp1_touch_time_utc
            terminal_at = result.remainder_resolution_time_utc
            ambiguous = int(result.same_candle_ambiguous)
            reason = result.reason
        except (ValueError, TypeError):
            state, lifecycle_r, tp1_at, terminal_at, ambiguous, reason = (
                "UNRESOLVED_REMAINDER", None, "", "", 0, "invalid_trade_geometry"
            )
    geometry = {
        name: _value(row, name, "stop_loss" if name == "sl" else name)
        for name in ("entry", "sl", "tp1", "tp2")
    }
    evidence = [] if ordered.empty else ordered[["open_time", "high", "low", "close_time"]].astype(str).values.tolist()
    return Reconstruction(
        signal_key(row), timestamp, normalize_symbol(_value(row, "symbol")), normalize_side(_value(row, "side", "direction")),
        state, lifecycle_r, tp1_at, terminal_at, 0.5, 0.5, ambiguous, reason, SOURCE,
        RECONSTRUCTION_VERSION, reconstructed_at_utc or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        PRICE_SOURCE, PRICE_INTERVAL, cutoff.isoformat().replace("+00:00", "Z"), _hash(geometry), _hash(evidence),
    )


def fetch_binance_closed_klines(symbol: str, start_utc: str, cutoff_utc: str, *, session: requests.Session | None = None) -> pd.DataFrame:
    client = session or requests.Session()
    start_ms = int(parse_utc_mixed(start_utc).timestamp() * 1000)
    end_ms = int(parse_utc_mixed(cutoff_utc).timestamp() * 1000)
    rows: list[list[Any]] = []
    while start_ms <= end_ms:
        response = client.get(
            "https://fapi.binance.com/fapi/v1/klines",
            params={"symbol": normalize_symbol(symbol), "interval": PRICE_INTERVAL, "startTime": start_ms, "endTime": end_ms, "limit": 1500},
            timeout=30,
        )
        response.raise_for_status()
        page = response.json()
        if not page:
            break
        rows.extend(page)
        next_start = int(page[-1][6]) + 1
        if next_start <= start_ms:
            break
        start_ms = next_start
    return pd.DataFrame(
        [{"open_time": pd.to_datetime(r[0], unit="ms", utc=True), "high": float(r[2]), "low": float(r[3]),
          "close": float(r[4]), "close_time": pd.to_datetime(r[6], unit="ms", utc=True)} for r in rows]
    )


def reconstruct_population(
    population: pd.DataFrame,
    cutoff_utc: str,
    *,
    candle_provider: Callable[[str, str, str], pd.DataFrame] = fetch_binance_closed_klines,
) -> tuple[list[Reconstruction], list[str]]:
    if population.empty:
        return [], []
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    outputs: list[Reconstruction] = []
    errors: list[str] = []
    for symbol, group in population.groupby(population["symbol"].map(normalize_symbol)):
        start = parse_utc_mixed(group["timestamp"]).min()
        try:
            candles = candle_provider(symbol, start.isoformat(), cutoff_utc)
            outputs.extend(reconstruct_one(row, candles, cutoff_utc, reconstructed_at_utc=now) for row in group.to_dict("records"))
        except Exception as exc:  # retain per-symbol failure without partial source mutation
            errors.append(f"{symbol}: {type(exc).__name__}: {exc}")
    return outputs, errors


def lifecycle_metrics(frame: pd.DataFrame) -> dict[str, Any]:
    data = frame.copy()
    state = data.get("lifecycle_state", pd.Series("", index=data.index)).fillna("").astype(str).str.upper()
    terminal = state.isin(TERMINAL_STATES)
    closed = data[terminal].copy()
    closed["_r"] = pd.to_numeric(closed.get("lifecycle_r"), errors="coerce").fillna(0.0)
    # Preserve the independently certified audit convention: lifecycle PnL is
    # sequenced by canonical signal emission order.  Terminal timestamps remain
    # the close facts; TP1 touch timestamps are never used as final PnL times.
    closed["_signal_order"] = parse_utc_mixed(closed.get("timestamp"))
    closed = closed.sort_values(["_signal_order", "canonical_signal_key"], na_position="last")
    equity = closed["_r"].cumsum()
    peak = equity.cummax()
    drawdown = equity - peak
    longest, run = 0, 0
    for value in closed["_r"]:
        run = run + 1 if value < 0 else 0
        longest = max(longest, run)
    return {
        "canonical_sent": len(data), "closed": int(terminal.sum()), "open": int(state.eq(LIVE_OPEN_STATE).sum()),
        "historical_unknown": int(state.eq(HISTORICAL_UNKNOWN_STATE).sum()),
        "wins": int((closed["_r"] > 0).sum()), "losses": int((closed["_r"] < 0).sum()),
        "net_r": float(closed["_r"].sum()), "peak_r": float(peak.max()) if not peak.empty else 0.0,
        "max_drawdown": float(drawdown.min()) if not drawdown.empty else 0.0, "longest_losing_streak": longest,
        "tp2_win": int(state.eq("TP2_WIN").sum()), "tp1_then_original_sl": int(state.eq("TP1_THEN_ORIGINAL_SL").sum()),
        "original_sl": int(state.eq("ORIGINAL_SL").sum()), "ambiguous": int(state.eq(AMBIGUOUS_STATE).sum()),
        "unresolved": int(state.eq("UNRESOLVED_REMAINDER").sum()),
    }


def records_as_dicts(records: Iterable[Reconstruction]) -> list[dict[str, Any]]:
    return [asdict(record) for record in records]
