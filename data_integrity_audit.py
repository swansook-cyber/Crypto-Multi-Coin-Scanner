# -*- coding: utf-8 -*-
"""Read-only CSV integrity audit for Crypto Multi-Coin Scanner runtime data."""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd


BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
JOURNAL = LOGS_DIR / "signals.csv"
ENTRY_TIMING = LOGS_DIR / "entry_timing_engine.csv"
BACKUP_DIR = BASE_DIR / "backups"
REJECTED_SIGNALS = LOGS_DIR / "rejected_signals.csv"
SIGNALS_HISTORY = LOGS_DIR / "signals_history.csv"
OPTIONAL_CANDIDATE_LOGS = [
    LOGS_DIR / "scanner_candidates.csv",
    LOGS_DIR / "signal_candidates.csv",
    LOGS_DIR / "candidates.csv",
    LOGS_DIR / "candidate_signals.csv",
]
SOURCE_PRIORITY = {
    "sent": 10,
    "report_only": 20,
    "approved_history": 30,
    "journal_rejected": 40,
    "rejected": 50,
    "scanner_candidates.csv": 60,
    "signal_candidates.csv": 70,
    "candidates.csv": 80,
    "candidate_signals.csv": 90,
}

FINAL_STATUSES = {"WIN", "LOSS", "EXPIRED", "BREAKEVEN", "CLOSED", "TP", "SL", "TAKE_PROFIT", "STOP_LOSS"}
OPEN_STATUSES = {"", "OPEN", "0"}
VALID_RESULTS = FINAL_STATUSES | {"OPEN", "SKIPPED", "0"}
VALID_SIGNAL_STATUSES = {
    "open",
    "sent",
    "closed",
    "logged_quality_filter",
    "skipped_quality_filter",
    "logged_quality_filter",
    "skipped_daily_risk_guard",
    "skipped_losing_streak",
    "skipped_btc_regime",
    "skipped_loss_cooldown",
    "skipped_correlation",
    "skipped_not_top_candidate",
    "skipped_not_top",
    "skipped_position_management",
    "tier_c_report_only",
    "weak_symbol_report_only",
    "session_risk_report_only",
    "london_long_report_only",
}
APPROVED_FINAL_STATUSES = {
    "sent",
    "tier_c_report_only",
    "weak_symbol_report_only",
    "session_risk_report_only",
    "london_long_report_only",
}
ENTRY_TIMING_FINAL_CANDIDATE_INTEGRATION_UTC = pd.Timestamp("2026-06-28T02:05:18Z")


@dataclass
class AuditFinding:
    severity: str
    check: str
    detail: str


@dataclass
class EntryTimingClassification:
    total: int
    matched: int
    legacy: int
    orphan: int
    ambiguous: int
    duplicate: int
    samples: list[str] | None = None

    @property
    def coverage_pct(self) -> float:
        if self.total <= 0:
            return 0.0
        return round(self.matched / self.total * 100.0, 1)

    @property
    def explained_coverage_pct(self) -> float:
        if self.total <= 0:
            return 0.0
        explained = self.total - self.orphan - self.ambiguous - self.duplicate
        return round(max(0, explained) / self.total * 100.0, 1)


ENTRY_PROVENANCE_SEVERITY = {
    "MATCHED_APPROVED_SIGNAL": "PASS",
    "MATCHED_SENT_SIGNAL": "PASS",
    "MATCHED_REPORT_ONLY_SIGNAL": "INFO",
    "MATCHED_REJECTED_CANDIDATE": "INFO",
    "MATCHED_PRE_FINAL_CANDIDATE": "INFO",
    "LEGACY_SHADOW_ROW": "INFO",
    "TRUE_ORPHAN": "WARNING",
    "AMBIGUOUS_PROVENANCE": "WARNING",
    "DUPLICATE_ROW": "WARNING",
}


@dataclass
class EntryTimingProvenance:
    index: int
    category: str
    severity: str
    reason: str
    match_source: str = ""
    match_count: int = 0
    search_counts: dict[str, int] | None = None
    identity: dict[str, Any] | None = None


@dataclass
class EntryTimingTruthSummary:
    total: int
    counts: dict[str, int]
    warning_count: int
    explained_coverage_pct: float
    approved_sent_coverage_pct: float
    samples: list[EntryTimingProvenance]


@dataclass(frozen=True)
class CandidateRecord:
    index: int
    source_key: str
    source_name: str
    provenance: str
    ids: frozenset[str]
    symbol: str
    direction: str
    timestamp: pd.Timestamp | pd.NaT
    timestamp_bucket: int | None
    canonical_identity: str
    entry: float | None
    sl: float | None
    tp1: float | None


@dataclass
class SourceIndex:
    source_key: str
    source_name: str
    provenance: str
    records: list[CandidateRecord]
    by_id: dict[str, list[CandidateRecord]]
    by_pair: dict[tuple[str, str], list[CandidateRecord]]
    by_pair_hour: dict[tuple[str, str, int], list[CandidateRecord]]


class AuditProfiler:
    def __init__(self) -> None:
        self.timings: list[tuple[str, float]] = []

    def stage(self, name: str):
        profiler = self

        class _Stage:
            def __enter__(self) -> None:
                self.start = time.perf_counter()

            def __exit__(self, exc_type, exc, tb) -> None:
                profiler.timings.append((name, time.perf_counter() - self.start))

        return _Stage()

    def print(self) -> None:
        total = sum(seconds for _, seconds in self.timings)
        width = max([len(name) for name, _ in self.timings] + [5])
        for name, seconds in self.timings:
            print(f"{name:<{width}} .... {seconds:.3f}s")
        print(f"{'Total':<{width}} .... {total:.3f}s")


@dataclass
class WatcherStateClassification:
    historical_flags: int
    active_stale_state: int
    existing_stale_locks: int
    empty_or_nan_references: int
    invalid_missing_references: int
    removable_items: int
    closed_treated_open: int
    symbols: list[str]


def _load_csv(path: Path) -> tuple[pd.DataFrame, AuditFinding | None]:
    if not path.exists():
        return pd.DataFrame(), AuditFinding("WARNING", "csv exists", f"{path} missing")
    try:
        return pd.read_csv(path), None
    except Exception as exc:
        return pd.DataFrame(), AuditFinding("FAIL", "corrupt or unreadable CSV files", f"{path}: {type(exc).__name__}: {exc}")


def _series(df: pd.DataFrame, column: str, default: Any = "") -> pd.Series:
    if column in df.columns:
        return df[column]
    return pd.Series([default] * len(df), index=df.index)


def is_blank(value: Any) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return str(value).strip().lower() in {"", "nan", "none", "null", "<na>"}


def _cache_key(value: Any) -> str:
    if is_blank(value):
        return ""
    return str(value).strip()


@lru_cache(maxsize=20000)
def _normalize_symbol_cached(text: str) -> str:
    if not text:
        return ""
    text = text.strip().upper()
    if ":" in text:
        text = text.split(":")[-1]
    text = text.replace("#", "").replace(".P", "").replace("PERP", "")
    text = text.replace("/", "").replace("-", "").replace("_", "")
    return "".join(ch for ch in text if ch.isalnum())


def normalize_symbol(value: Any) -> str:
    return _normalize_symbol_cached(_cache_key(value))


@lru_cache(maxsize=1000)
def _normalize_direction_cached(text: str) -> str:
    text = text.strip().upper()
    if text == "BUY":
        return "LONG"
    if text == "SELL":
        return "SHORT"
    return text if text in {"LONG", "SHORT"} else ""


def normalize_direction(value: Any) -> str:
    return _normalize_direction_cached(_cache_key(value))


@lru_cache(maxsize=50000)
def _parse_utc_cached(text: str) -> pd.Timestamp | pd.NaT:
    if not text:
        return pd.NaT
    return pd.to_datetime(text, utc=True, errors="coerce")


def parse_utc(value: Any) -> pd.Timestamp | pd.NaT:
    return _parse_utc_cached(_cache_key(value))


@lru_cache(maxsize=50000)
def _numeric_value_cached(text: str) -> float | None:
    if not text:
        return None
    numeric = pd.to_numeric(pd.Series([text]), errors="coerce").iloc[0]
    if pd.isna(numeric):
        return None
    return float(numeric)


def numeric_value(value: Any) -> float | None:
    return _numeric_value_cached(_cache_key(value))


def canonical_numeric(value: Any) -> str:
    number = numeric_value(value)
    if number is None:
        return ""
    return f"{number:.8f}".rstrip("0").rstrip(".")


def timestamp_bucket(timestamp: pd.Timestamp | pd.NaT, seconds: int = 3600) -> int | None:
    if pd.isna(timestamp):
        return None
    return int(timestamp.timestamp() // seconds)


def canonical_timestamp(timestamp: pd.Timestamp | pd.NaT) -> str:
    if pd.isna(timestamp):
        return ""
    return timestamp.floor("min").isoformat()


def price_close(left: Any, right: Any, rel_tol: float = 0.0005) -> bool:
    left_num = numeric_value(left)
    right_num = numeric_value(right)
    if left_num is None or right_num is None:
        return True
    tolerance = max(1e-8, abs(right_num) * rel_tol)
    return abs(left_num - right_num) <= tolerance


def signal_key_df(df: pd.DataFrame) -> pd.Series:
    parts = [
        _series(df, "timestamp").fillna("").astype(str),
        _series(df, "symbol").fillna("").astype(str).str.upper(),
        _series(df, "side").fillna("").astype(str).str.upper(),
        _series(df, "entry").fillna("").astype(str),
        _series(df, "stop_loss").fillna("").astype(str),
        _series(df, "tp1").fillna("").astype(str),
    ]
    key = parts[0]
    for part in parts[1:]:
        key = key + "|" + part
    return key


def signal_prefix_df(df: pd.DataFrame, side_column: str = "side") -> pd.Series:
    timestamp = pd.to_datetime(_series(df, "timestamp"), utc=True, errors="coerce").dt.strftime("%Y-%m-%dT%H:%M")
    timestamp = timestamp.fillna(_series(df, "timestamp").fillna("").astype(str).str.slice(0, 16))
    symbol = _series(df, "symbol").map(normalize_symbol)
    side = _series(df, side_column).map(normalize_direction)
    entry = pd.to_numeric(_series(df, "entry"), errors="coerce").round(8).astype(str)
    return timestamp + "|" + symbol + "|" + side + "|" + entry


def _identity_values(row: pd.Series) -> set[str]:
    values: set[str] = set()
    for column in ["source_signal_id", "candidate_id", "final_candidate_id", "signal_id", "outcome_id"]:
        if column in row.index and not is_blank(row.get(column)):
            values.add(str(row.get(column)).strip())
    return values


def _row_identity(row: pd.Series) -> dict[str, Any]:
    return {
        "source_signal_id": row.get("source_signal_id", ""),
        "candidate_id": row.get("candidate_id", ""),
        "final_candidate_id": row.get("final_candidate_id", ""),
        "timestamp": str(_entry_timestamp(row)) if not pd.isna(_entry_timestamp(row)) else str(row.get("timestamp", row.get("final_signal_timestamp", ""))),
        "symbol": normalize_symbol(row.get("normalized_symbol", row.get("symbol", ""))),
        "direction": normalize_direction(row.get("normalized_direction", row.get("side", row.get("direction", "")))),
        "entry": numeric_value(row.get("entry", "")),
        "sl": numeric_value(row.get("sl", row.get("stop_loss", ""))),
        "tp1": numeric_value(row.get("tp1", "")),
    }


def _identity_complete(identity: dict[str, Any]) -> bool:
    return bool(
        identity.get("symbol")
        and identity.get("direction")
        and identity.get("timestamp")
        and identity.get("entry") is not None
    )


def _source_frame(df: pd.DataFrame, source_name: str, provenance: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    frame = df.copy()
    frame["_source_name"] = source_name
    frame["_provenance"] = provenance
    return frame


def load_entry_timing_sources(journal: pd.DataFrame, logs_dir: Path = LOGS_DIR, profiler: AuditProfiler | None = None) -> dict[str, pd.DataFrame]:
    stage = profiler.stage("Load sources") if profiler else None
    if stage:
        stage.__enter__()
    try:
        return _load_entry_timing_sources_impl(journal, logs_dir)
    finally:
        if stage:
            stage.__exit__(None, None, None)


def _load_entry_timing_sources_impl(journal: pd.DataFrame, logs_dir: Path = LOGS_DIR) -> dict[str, pd.DataFrame]:
    sources: dict[str, pd.DataFrame] = {}
    if not journal.empty:
        status = _series(journal, "signal_status").fillna("").astype(str).str.lower()
        sent = journal[status.eq("sent")]
        report_only = journal[status.isin({"tier_c_report_only", "weak_symbol_report_only", "session_risk_report_only", "london_long_report_only"})]
        rejected = journal[status.str.startswith("skipped_") | status.str.startswith("logged_")]
        sources["sent"] = _source_frame(sent, "logs/signals.csv", "MATCHED_SENT_SIGNAL")
        sources["report_only"] = _source_frame(report_only, "logs/signals.csv", "MATCHED_REPORT_ONLY_SIGNAL")
        sources["journal_rejected"] = _source_frame(rejected, "logs/signals.csv", "MATCHED_REJECTED_CANDIDATE")

    rejected_path = logs_dir / "rejected_signals.csv"
    rejected_df, _ = _load_csv(rejected_path)
    sources["rejected"] = _source_frame(rejected_df, str(rejected_path), "MATCHED_REJECTED_CANDIDATE")

    history_path = logs_dir / "signals_history.csv"
    history_df, _ = _load_csv(history_path)
    sources["approved_history"] = _source_frame(history_df, str(history_path), "MATCHED_APPROVED_SIGNAL")

    for candidate_path in [
        logs_dir / "scanner_candidates.csv",
        logs_dir / "signal_candidates.csv",
        logs_dir / "candidates.csv",
        logs_dir / "candidate_signals.csv",
    ]:
        candidate_df, _ = _load_csv(candidate_path)
        if not candidate_df.empty:
            sources[candidate_path.name] = _source_frame(candidate_df, str(candidate_path), "MATCHED_PRE_FINAL_CANDIDATE")
    return sources


def _candidate_timestamp(row: pd.Series) -> pd.Timestamp | pd.NaT:
    return _journal_timestamp(row)


def _record_canonical_identity(
    symbol: str,
    direction: str,
    timestamp: pd.Timestamp | pd.NaT,
    entry: float | None,
    sl: float | None,
    tp1: float | None,
) -> str:
    parts = [
        symbol,
        direction,
        canonical_timestamp(timestamp),
        canonical_numeric(entry),
        canonical_numeric(sl),
        canonical_numeric(tp1),
    ]
    return "|".join(parts)


def _source_priority(source_key: str) -> int:
    return SOURCE_PRIORITY.get(source_key, 100)


def _best_record(records: list[CandidateRecord]) -> CandidateRecord:
    return sorted(records, key=lambda record: (_source_priority(record.source_key), record.index))[0]


def build_source_indexes(sources: dict[str, pd.DataFrame], profiler: AuditProfiler | None = None) -> dict[str, SourceIndex]:
    with profiler.stage("Build indexes") if profiler else _nullcontext():
        indexes: dict[str, SourceIndex] = {}
        for source_key, source_df in sources.items():
            if source_df.empty:
                indexes[source_key] = SourceIndex(source_key, "", "", [], {}, {}, {})
                continue
            source_name = str(source_df["_source_name"].iloc[0]) if "_source_name" in source_df.columns else source_key
            provenance = str(source_df["_provenance"].iloc[0]) if "_provenance" in source_df.columns else ""
            records: list[CandidateRecord] = []
            by_id: dict[str, list[CandidateRecord]] = {}
            by_pair: dict[tuple[str, str], list[CandidateRecord]] = {}
            by_pair_hour: dict[tuple[str, str, int], list[CandidateRecord]] = {}
            symbol_column = "normalized_symbol" if "normalized_symbol" in source_df.columns else "symbol"
            direction_column = "normalized_direction" if "normalized_direction" in source_df.columns else "side" if "side" in source_df.columns else "direction"
            stop_column = "stop_loss" if "stop_loss" in source_df.columns else "sl"
            timestamp_column = "final_signal_timestamp" if "final_signal_timestamp" in source_df.columns else "timestamp"
            symbols = _series(source_df, symbol_column).map(normalize_symbol).tolist()
            directions = _series(source_df, direction_column).map(normalize_direction).tolist()
            timestamps = [_parse_utc_cached(value) for value in _series(source_df, timestamp_column).fillna("").astype(str).str.strip().tolist()]
            entries = [numeric_value(value) for value in _series(source_df, "entry").tolist()]
            stops = [numeric_value(value) for value in _series(source_df, stop_column).tolist()]
            tp1_values = [numeric_value(value) for value in _series(source_df, "tp1").tolist()]
            rows = list(source_df.iterrows())
            for position, (index, row) in enumerate(rows):
                symbol = symbols[position]
                direction = directions[position]
                ids = frozenset(_identity_values(row))
                candidate_timestamp = timestamps[position]
                entry = entries[position]
                sl = stops[position]
                tp1 = tp1_values[position]
                bucket = timestamp_bucket(candidate_timestamp)
                record = CandidateRecord(
                    int(index),
                    source_key,
                    source_name,
                    provenance,
                    ids,
                    symbol,
                    direction,
                    candidate_timestamp,
                    bucket,
                    _record_canonical_identity(symbol, direction, candidate_timestamp, entry, sl, tp1),
                    entry,
                    sl,
                    tp1,
                )
                records.append(record)
                for identity in ids:
                    by_id.setdefault(identity, []).append(record)
                if symbol and direction:
                    by_pair.setdefault((symbol, direction), []).append(record)
                    if bucket is not None:
                        by_pair_hour.setdefault((symbol, direction, bucket), []).append(record)
            indexes[source_key] = SourceIndex(source_key, source_name, provenance, records, by_id, by_pair, by_pair_hour)
        return indexes


class _nullcontext:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def _record_matches(entry_identity: dict[str, Any], timestamp: pd.Timestamp | pd.NaT, record: CandidateRecord) -> tuple[bool, str]:
    if not _timestamp_close(timestamp, record.timestamp):
        return False, "timestamp outside tolerance"
    if not price_close(entry_identity.get("entry"), record.entry):
        return False, "price mismatch"
    entry_tp1 = entry_identity.get("tp1")
    entry_sl = entry_identity.get("sl")
    if entry_tp1 is not None and not price_close(entry_tp1, record.tp1):
        return False, "price mismatch"
    if entry_sl is not None and not price_close(entry_sl, record.sl):
        return False, "price mismatch"
    return True, "symbol/direction/time/price"


def _source_matches_indexed(entry_row: pd.Series, source_index: SourceIndex, entry_identity: dict[str, Any] | None = None) -> tuple[list[CandidateRecord], str]:
    if not source_index.records:
        return [], "source empty"
    entry_ids = _identity_values(entry_row)
    if entry_ids:
        id_matches: list[CandidateRecord] = []
        seen: set[tuple[str, int]] = set()
        for identity in entry_ids:
            for record in source_index.by_id.get(identity, []):
                key = (record.source_key, record.index)
                if key not in seen:
                    id_matches.append(record)
                    seen.add(key)
        if id_matches:
            return id_matches, "explicit id"

    identity = entry_identity or _row_identity(entry_row)
    if not _identity_complete(identity):
        return [], "missing identity fields"

    timestamp = _entry_timestamp(entry_row)
    bucket = timestamp_bucket(timestamp)
    if bucket is None:
        candidates = source_index.by_pair.get((identity["symbol"], identity["direction"]), [])
    else:
        candidates = []
        for nearby_bucket in range(bucket - 2, bucket + 3):
            candidates.extend(source_index.by_pair_hour.get((identity["symbol"], identity["direction"], nearby_bucket), []))
    possible: list[CandidateRecord] = []
    rejected_by_time = 0
    rejected_by_price = 0
    for record in candidates:
        matched, reason = _record_matches(identity, timestamp, record)
        if matched:
            possible.append(record)
        elif reason == "timestamp outside tolerance":
            rejected_by_time += 1
        elif reason == "price mismatch":
            rejected_by_price += 1
    if possible:
        return possible, "symbol/direction/time/price"
    if rejected_by_time:
        return [], "timestamp outside tolerance"
    if rejected_by_price:
        return [], "price mismatch"
    return [], "no candidate in source"


def resolve_canonical_source(
    source_results: list[tuple[str, str, list[CandidateRecord], str]]
) -> tuple[str, str, list[CandidateRecord], str] | None:
    if not source_results:
        return None
    records: list[CandidateRecord] = []
    reasons: list[str] = []
    for _source_key, _provenance, matches, reason in source_results:
        records.extend(matches)
        reasons.append(reason)
    canonical_groups: dict[str, list[CandidateRecord]] = {}
    for record in records:
        key = record.canonical_identity or f"{record.source_key}:{record.index}"
        canonical_groups.setdefault(key, []).append(record)
    if len(canonical_groups) != 1:
        return None
    best = _best_record(canonical_groups[next(iter(canonical_groups))])
    reason = "canonical candidate identity; source priority" if len(records) > 1 else reasons[0]
    return best.source_key, best.provenance, [best], reason


def classify_entry_timing_rows(
    entry: pd.DataFrame,
    journal: pd.DataFrame,
    logs_dir: Path = LOGS_DIR,
    profiler: AuditProfiler | None = None,
) -> list[EntryTimingProvenance]:
    if entry.empty:
        return []
    with profiler.stage("Normalize records") if profiler else _nullcontext():
        duplicate_mask = _entry_duplicate_mask(entry)
        timestamp_source = _series(entry, "final_signal_timestamp") if "final_signal_timestamp" in entry.columns else _series(entry, "timestamp")
        timestamps = pd.to_datetime(timestamp_source, utc=True, errors="coerce")
        entry_records = [(index, row, _row_identity(row)) for index, row in entry.iterrows()]
    sources = load_entry_timing_sources(journal, logs_dir=logs_dir, profiler=profiler)
    indexes = build_source_indexes(sources, profiler=profiler)
    with profiler.stage("Entry Timing match") if profiler else _nullcontext():
        provenances: list[EntryTimingProvenance] = []
        for index, row, identity in entry_records:
            if bool(duplicate_mask.loc[index]):
                category = "DUPLICATE_ROW"
                provenances.append(EntryTimingProvenance(int(index), category, ENTRY_PROVENANCE_SEVERITY[category], "duplicate Entry Timing identity", identity=identity))
                continue

            source_results: list[tuple[str, str, list[CandidateRecord], str]] = []
            search_counts: dict[str, int] = {}
            for source_key, source_index in indexes.items():
                matches, reason = _source_matches_indexed(row, source_index, identity)
                search_counts[source_key] = len(matches)
                if matches and source_index.provenance:
                    source_results.append((source_key, source_index.provenance, matches, reason))

            resolved = resolve_canonical_source(source_results)
            if resolved is not None:
                source_key, category, matches, reason = resolved
                provenances.append(
                    EntryTimingProvenance(int(index), category, ENTRY_PROVENANCE_SEVERITY[category], reason, source_key, len(matches), search_counts, identity)
                )
                continue
            if source_results:
                category = "AMBIGUOUS_PROVENANCE"
                match_count = sum(len(item[2]) for item in source_results)
                provenances.append(
                    EntryTimingProvenance(int(index), category, ENTRY_PROVENANCE_SEVERITY[category], "duplicate candidates across sources", ",".join(item[0] for item in source_results), match_count, search_counts, identity)
                )
                continue

            row_timestamp = timestamps.loc[index]
            if pd.isna(row_timestamp) or row_timestamp < ENTRY_TIMING_FINAL_CANDIDATE_INTEGRATION_UTC:
                category = "LEGACY_SHADOW_ROW"
                reason = "unsupported legacy schema" if pd.isna(row_timestamp) else "row before final-candidate integration"
            elif not _identity_complete(identity):
                category = "TRUE_ORPHAN"
                reason = "missing identity fields"
            else:
                category = "TRUE_ORPHAN"
                reason = "no candidate in any source"
            provenances.append(EntryTimingProvenance(int(index), category, ENTRY_PROVENANCE_SEVERITY[category], reason, "", 0, search_counts, identity))
        return provenances


def summarize_entry_timing_truth(provenances: list[EntryTimingProvenance]) -> EntryTimingTruthSummary:
    total = len(provenances)
    counts = {category: 0 for category in ENTRY_PROVENANCE_SEVERITY}
    for item in provenances:
        counts[item.category] = counts.get(item.category, 0) + 1
    warning_count = sum(count for category, count in counts.items() if ENTRY_PROVENANCE_SEVERITY.get(category) == "WARNING")
    explained = total - counts.get("TRUE_ORPHAN", 0) - counts.get("AMBIGUOUS_PROVENANCE", 0) - counts.get("DUPLICATE_ROW", 0)
    approved_sent = counts.get("MATCHED_APPROVED_SIGNAL", 0) + counts.get("MATCHED_SENT_SIGNAL", 0)
    explained_pct = round(explained / total * 100.0, 1) if total else 0.0
    approved_sent_pct = round(approved_sent / total * 100.0, 1) if total else 0.0
    samples = [item for item in provenances if item.category in {"TRUE_ORPHAN", "AMBIGUOUS_PROVENANCE"}]
    return EntryTimingTruthSummary(total, counts, warning_count, explained_pct, approved_sent_pct, samples)


def _entry_timestamp(row: pd.Series) -> pd.Timestamp | pd.NaT:
    for column in ["final_signal_timestamp", "timestamp"]:
        if column in row.index:
            timestamp = parse_utc(row.get(column))
            if not pd.isna(timestamp):
                return timestamp
    return pd.NaT


def _journal_timestamp(row: pd.Series) -> pd.Timestamp | pd.NaT:
    for column in ["final_signal_timestamp", "timestamp"]:
        if column in row.index:
            timestamp = parse_utc(row.get(column))
            if not pd.isna(timestamp):
                return timestamp
    return pd.NaT


def _timestamp_close(left: pd.Timestamp | pd.NaT, right: pd.Timestamp | pd.NaT, minutes: int = 90) -> bool:
    if pd.isna(left) or pd.isna(right):
        return True
    return abs((left - right).total_seconds()) <= minutes * 60


def _entry_duplicate_mask(entry: pd.DataFrame) -> pd.Series:
    identity_columns = [
        "source_signal_id",
        "candidate_id",
        "final_signal_timestamp",
        "timestamp",
        "symbol",
        "normalized_symbol",
        "direction",
        "normalized_direction",
        "entry",
        "tp1",
        "sl",
        "stop_loss",
        "signal_status",
    ]
    usable = [column for column in identity_columns if column in entry.columns]
    if not usable:
        return signal_prefix_df(entry, "direction").duplicated(keep=False)
    normalized = pd.DataFrame(index=entry.index)
    for column in usable:
        if column in {"symbol", "normalized_symbol"}:
            normalized[column] = entry[column].map(normalize_symbol)
        elif column in {"direction", "normalized_direction"}:
            normalized[column] = entry[column].map(normalize_direction)
        elif column in {"entry", "tp1", "sl", "stop_loss"}:
            normalized[column] = pd.to_numeric(entry[column], errors="coerce").round(8).astype(str)
        elif column in {"timestamp", "final_signal_timestamp"}:
            normalized[column] = pd.to_datetime(entry[column], utc=True, errors="coerce").dt.strftime("%Y-%m-%dT%H:%M")
        else:
            normalized[column] = entry[column].fillna("").astype(str).str.strip()
    return normalized.duplicated(keep=False)


def _candidate_matches(entry_row: pd.Series, approved: pd.DataFrame) -> tuple[list[int], str]:
    entry_ids = _identity_values(entry_row)
    if entry_ids:
        id_matches = [
            int(index)
            for index, row in approved.iterrows()
            if entry_ids & _identity_values(row)
        ]
        if id_matches:
            return id_matches, "explicit id"

    symbol = normalize_symbol(entry_row.get("normalized_symbol", entry_row.get("symbol", "")))
    direction = normalize_direction(entry_row.get("normalized_direction", entry_row.get("direction", "")))
    timestamp = _entry_timestamp(entry_row)
    entry_price = entry_row.get("entry")
    tp1 = entry_row.get("tp1")
    sl = entry_row.get("sl", entry_row.get("stop_loss"))

    possible: list[int] = []
    for index, signal_row in approved.iterrows():
        signal_symbol = normalize_symbol(signal_row.get("normalized_symbol", signal_row.get("symbol", "")))
        signal_direction = normalize_direction(signal_row.get("normalized_direction", signal_row.get("side", signal_row.get("direction", ""))))
        if symbol and signal_symbol and symbol != signal_symbol:
            continue
        if direction and signal_direction and direction != signal_direction:
            continue
        if not _timestamp_close(timestamp, _journal_timestamp(signal_row)):
            continue
        if not price_close(entry_price, signal_row.get("entry")):
            continue
        if not price_close(tp1, signal_row.get("tp1")):
            continue
        if not price_close(sl, signal_row.get("stop_loss", signal_row.get("sl"))):
            continue
        possible.append(int(index))
    return possible, "symbol/direction/time/price"


def _sample_entry(row: pd.Series, reason: str) -> str:
    timestamp = row.get("final_signal_timestamp", row.get("timestamp", ""))
    symbol = normalize_symbol(row.get("normalized_symbol", row.get("symbol", ""))) or "-"
    direction = normalize_direction(row.get("normalized_direction", row.get("direction", ""))) or "-"
    entry = row.get("entry", "")
    return f"timestamp={timestamp}, symbol={symbol}, direction={direction}, entry={entry}, reason={reason}"


def audit_journal(df: pd.DataFrame) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    if df.empty:
        findings.append(AuditFinding("WARNING", "journal rows", "logs/signals.csv has no rows"))
        return findings

    key = signal_key_df(df)
    duplicate_keys = key[key.duplicated(keep=False) & key.ne("|||||")]
    if not duplicate_keys.empty:
        findings.append(AuditFinding("WARNING", "duplicate signal identifiers", f"{duplicate_keys.nunique()} duplicate keys"))

    final_candidate_cols = ["timestamp", "symbol", "side", "entry", "tp1", "signal_status"]
    existing_final_cols = [column for column in final_candidate_cols if column in df.columns]
    if existing_final_cols:
        duplicates = df[df.duplicated(existing_final_cols, keep=False)]
        if not duplicates.empty:
            findings.append(AuditFinding("WARNING", "duplicate final candidates", f"{len(duplicates)} rows"))

    for column in ["telegram_sent", "sent_to_signals", "sent_to_cornix", "tp1_alert_sent", "outcome_alert_sent", "cornix_be_command_sent"]:
        if column in df.columns:
            values = _series(df, column).fillna("").astype(str).str.strip().str.lower()
            invalid = values[~values.isin(["", "0", "1", "0.0", "1.0", "true", "false", "yes", "no"])]
            if not invalid.empty:
                findings.append(AuditFinding("WARNING", "duplicate Telegram/Cornix send flags", f"{column} has non-boolean values"))

    timestamps = pd.to_datetime(_series(df, "timestamp"), utc=True, errors="coerce")
    if timestamps.isna().any():
        findings.append(AuditFinding("WARNING", "malformed timestamps", f"{int(timestamps.isna().sum())} malformed timestamp rows"))

    required = ["symbol", "side", "entry", "stop_loss", "tp1"]
    for column in required:
        if column not in df.columns:
            findings.append(AuditFinding("FAIL", "missing symbol/direction/entry/SL/TP fields", f"missing column {column}"))
        else:
            blank = _series(df, column).fillna("").astype(str).str.strip().eq("")
            if blank.any():
                findings.append(AuditFinding("WARNING", "missing symbol/direction/entry/SL/TP fields", f"{column}: {int(blank.sum())} blanks"))

    result = _series(df, "result", "OPEN").fillna("OPEN").astype(str).str.upper()
    hit = _series(df, "hit_target").fillna("").astype(str).str.upper()
    open_with_final = result.isin(OPEN_STATUSES) & hit.isin(["TP1", "TP2", "TP3", "SL"])
    if open_with_final.any():
        findings.append(AuditFinding("WARNING", "OPEN rows that already contain final hit targets", f"{int(open_with_final.sum())} rows"))

    active_status = _series(df, "signal_status").fillna("").astype(str).str.lower()
    closed_active = result.isin(FINAL_STATUSES) & active_status.isin(["active"])
    if closed_active.any():
        findings.append(AuditFinding("WARNING", "closed rows incorrectly treated as active", f"{int(closed_active.sum())} rows"))

    for column in ["entry", "stop_loss", "tp1", "tp2", "risk_reward", "score", "confidence"]:
        if column in df.columns:
            numeric = pd.to_numeric(df[column], errors="coerce")
            bad = df[column].fillna("").astype(str).str.strip().ne("") & numeric.isna()
            if bad.any():
                findings.append(AuditFinding("WARNING", "invalid numeric values", f"{column}: {int(bad.sum())} invalid"))

    invalid_result = result[~result.isin(VALID_RESULTS)]
    if not invalid_result.empty:
        findings.append(AuditFinding("WARNING", "invalid status values", f"result: {sorted(invalid_result.unique())}"))
    if "signal_status" in df.columns:
        status = active_status
        invalid_status = status[status.ne("") & ~status.isin(VALID_SIGNAL_STATUSES)]
        if not invalid_status.empty:
            findings.append(AuditFinding("WARNING", "invalid status values", f"signal_status: {sorted(invalid_status.unique())}"))

    return findings


def classify_entry_timing(entry: pd.DataFrame, journal: pd.DataFrame) -> EntryTimingClassification:
    if entry.empty:
        return EntryTimingClassification(0, 0, 0, 0, 0, 0, [])
    provenances = classify_entry_timing_rows(entry, journal)
    truth = summarize_entry_timing_truth(provenances)
    matched = truth.counts.get("MATCHED_APPROVED_SIGNAL", 0) + truth.counts.get("MATCHED_SENT_SIGNAL", 0)
    legacy = truth.counts.get("LEGACY_SHADOW_ROW", 0)
    orphan = truth.counts.get("TRUE_ORPHAN", 0)
    ambiguous = truth.counts.get("AMBIGUOUS_PROVENANCE", 0)
    duplicate = truth.counts.get("DUPLICATE_ROW", 0)
    samples = [
        _sample_entry(pd.Series(item.identity or {}), item.reason)
        for item in truth.samples[:5]
    ]
    return EntryTimingClassification(
        total=int(len(entry)),
        matched=int(matched),
        legacy=int(legacy),
        orphan=int(orphan),
        ambiguous=int(ambiguous),
        duplicate=int(duplicate),
        samples=samples,
    )


def audit_entry_timing(entry: pd.DataFrame, journal: pd.DataFrame, verbose: bool = False, profiler: AuditProfiler | None = None) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    if entry.empty:
        findings.append(AuditFinding("INFO", "Entry Timing rows", "logs/entry_timing_engine.csv has no rows"))
        return findings
    provenances = classify_entry_timing_rows(entry, journal, profiler=profiler)
    truth = summarize_entry_timing_truth(provenances)
    findings.append(
        AuditFinding(
            "PASS",
            "Entry Timing classification summary",
            (
                f"total={truth.total}, approved={truth.counts.get('MATCHED_APPROVED_SIGNAL', 0)}, "
                f"sent={truth.counts.get('MATCHED_SENT_SIGNAL', 0)}, "
                f"report_only={truth.counts.get('MATCHED_REPORT_ONLY_SIGNAL', 0)}, "
                f"rejected={truth.counts.get('MATCHED_REJECTED_CANDIDATE', 0)}, "
                f"pre_final={truth.counts.get('MATCHED_PRE_FINAL_CANDIDATE', 0)}, "
                f"legacy={truth.counts.get('LEGACY_SHADOW_ROW', 0)}, "
                f"true_orphan={truth.counts.get('TRUE_ORPHAN', 0)}, "
                f"ambiguous={truth.counts.get('AMBIGUOUS_PROVENANCE', 0)}, "
                f"duplicate={truth.counts.get('DUPLICATE_ROW', 0)}, "
                f"explained_coverage={truth.explained_coverage_pct:.1f}%, "
                f"approved_sent_coverage={truth.approved_sent_coverage_pct:.1f}%"
            ),
        )
    )
    for category, count in truth.counts.items():
        if count and category != "MATCHED_SENT_SIGNAL":
            findings.append(AuditFinding(ENTRY_PROVENANCE_SEVERITY[category], category, f"{count} Entry Timing rows"))
    if verbose and truth.samples:
        for item in truth.samples[:5]:
            findings.append(AuditFinding("INFO", "ENTRY_TIMING_SAMPLE", _sample_entry(pd.Series(item.identity or {}), item.reason)))
    return findings


def classify_watcher_state(journal: pd.DataFrame, journal_path: Path = JOURNAL) -> WatcherStateClassification:
    result = _series(journal, "result", "OPEN").fillna("OPEN").astype(str).str.upper() if not journal.empty else pd.Series(dtype=str)
    status = _series(journal, "signal_status").fillna("").astype(str).str.lower() if not journal.empty else pd.Series(dtype=str)
    closed_open = result.isin(FINAL_STATUSES) & status.isin(["active"]) if not journal.empty else pd.Series(dtype=bool)
    try:
        import position_watcher_state_cleanup

        cleanup_state = position_watcher_state_cleanup.classify_cleanup(journal_path)
        historical = cleanup_state.historical_closed_rows
        active_stale = cleanup_state.stale_canonical_active_entries
        existing_locks = cleanup_state.existing_stale_lock_files
        empty_refs = cleanup_state.empty_or_nan_references
        invalid_missing = cleanup_state.invalid_missing_references + cleanup_state.blocked_unsafe_path + cleanup_state.blocked_identity_ambiguous
        removable = cleanup_state.safe_to_remove
        symbols_list = cleanup_state.affected_symbols or []
    except Exception:
        historical = 0
        active_stale = 0
        existing_locks = 0
        empty_refs = 0
        invalid_missing = 0
        removable = 0
        symbols_list = []
    return WatcherStateClassification(
        historical_flags=int(historical),
        active_stale_state=int(active_stale),
        existing_stale_locks=int(existing_locks),
        empty_or_nan_references=int(empty_refs),
        invalid_missing_references=int(invalid_missing),
        removable_items=int(removable),
        closed_treated_open=int(closed_open.sum()),
        symbols=symbols_list,
    )


def audit_stale_watcher_state(journal: pd.DataFrame, journal_path: Path = JOURNAL) -> list[AuditFinding]:
    state = classify_watcher_state(journal, journal_path=journal_path)
    findings: list[AuditFinding] = []
    if state.historical_flags:
        findings.append(AuditFinding("INFO", "CLOSED_ROW_WITH_HISTORICAL_TP1_FLAG", f"{state.historical_flags} closed rows keep TP1 audit fields"))
    if state.empty_or_nan_references:
        findings.append(AuditFinding("INFO", "EMPTY_OR_NAN_REFERENCE", f"{state.empty_or_nan_references} closed rows have blank/NaN watcher references"))
    if state.invalid_missing_references:
        findings.append(AuditFinding("INFO", "INVALID_LOCK_REFERENCE", f"{state.invalid_missing_references} nonblank lock references do not exist"))
    if state.existing_stale_locks:
        findings.append(AuditFinding("WARNING", "STALE_LOCK_FILE", f"{state.existing_stale_locks} existing stale lock files; safe_to_remove={state.removable_items}; symbols={','.join(state.symbols) or '-'}"))
    if state.active_stale_state:
        findings.append(AuditFinding("WARNING", "CLOSED_ROW_IN_ACTIVE_WATCHER_STATE", f"{state.active_stale_state} canonical active state entries; symbols={','.join(state.symbols) or '-'}"))
    if state.closed_treated_open:
        findings.append(AuditFinding("FAIL", "CLOSED_ROW_STILL_TREATED_AS_OPEN", f"{state.closed_treated_open} rows"))
    if not findings:
        findings.append(AuditFinding("PASS", "position watcher active state", "no stale active watcher state"))
    return findings


def audit_paths(journal_path: Path = JOURNAL, entry_path: Path = ENTRY_TIMING, verbose: bool = False, profiler: AuditProfiler | None = None) -> list[AuditFinding]:
    with profiler.stage("Load runtime CSV") if profiler else _nullcontext():
        journal, journal_error = _load_csv(journal_path)
        entry, entry_error = _load_csv(entry_path)
    findings = [item for item in [journal_error] if item is not None]
    if entry_error is not None:
        findings.append(AuditFinding("INFO", "Entry Timing rows", f"{entry_path} missing; no shadow rows yet"))
    if journal_error is None:
        with profiler.stage("Journal audit") if profiler else _nullcontext():
            findings.extend(audit_journal(journal))
    if entry_error is None:
        findings.extend(audit_entry_timing(entry, journal, verbose=verbose, profiler=profiler))
    with profiler.stage("Watcher audit") if profiler else _nullcontext():
        findings.extend(audit_stale_watcher_state(journal, journal_path=journal_path))
    if not findings:
        findings.append(AuditFinding("PASS", "data integrity audit", "no issues detected"))
    return findings


def _normalize_bool(value: Any) -> Any:
    text = str(value).strip().lower()
    if text in {"1.0", "1", "true", "yes"}:
        return "1"
    if text in {"0.0", "0", "false", "no"}:
        return "0"
    return value


def repair_safe(path: Path) -> tuple[int, Path | None]:
    if not path.exists():
        return 0, None
    df = pd.read_csv(path)
    original = df.copy()
    for column in ["telegram_sent", "sent_to_signals", "sent_to_cornix", "tp1_alert_sent", "outcome_alert_sent", "cornix_be_command_sent"]:
        if column in df.columns:
            df[column] = df[column].map(_normalize_bool)
    before = len(df)
    df = df.drop_duplicates()
    changed = int(before - len(df)) + int(not df.equals(original))
    if changed:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup = BACKUP_DIR / f"{path.stem}_safe_repair_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.csv.bak"
        shutil.copy2(path, backup)
        df.to_csv(path, index=False)
        return changed, backup
    return 0, None


def print_findings(findings: list[AuditFinding]) -> None:
    print("Crypto Scanner Data Integrity Audit")
    print(f"Checked at UTC: {datetime.now(timezone.utc).isoformat()}")
    print("")
    for finding in findings:
        print(f"{finding.severity:7} | {finding.check} | {finding.detail}")


def print_entry_timing_summary(summary: EntryTimingTruthSummary) -> None:
    print("Entry Timing Provenance Summary")
    print(f"Total rows: {summary.total}")
    print(f"Approved matches: {summary.counts.get('MATCHED_APPROVED_SIGNAL', 0)}")
    print(f"Sent matches: {summary.counts.get('MATCHED_SENT_SIGNAL', 0)}")
    print(f"Report-only matches: {summary.counts.get('MATCHED_REPORT_ONLY_SIGNAL', 0)}")
    print(f"Rejected matches: {summary.counts.get('MATCHED_REJECTED_CANDIDATE', 0)}")
    print(f"Pre-final matches: {summary.counts.get('MATCHED_PRE_FINAL_CANDIDATE', 0)}")
    print(f"Legacy rows: {summary.counts.get('LEGACY_SHADOW_ROW', 0)}")
    print(f"True orphans: {summary.counts.get('TRUE_ORPHAN', 0)}")
    print(f"Ambiguous rows: {summary.counts.get('AMBIGUOUS_PROVENANCE', 0)}")
    print(f"Duplicates: {summary.counts.get('DUPLICATE_ROW', 0)}")
    print(f"Explained coverage: {summary.explained_coverage_pct:.1f}%")
    print(f"Approved/sent coverage: {summary.approved_sent_coverage_pct:.1f}%")
    print(f"Warning count: {summary.warning_count}")


def print_entry_timing_diagnostics(entry: pd.DataFrame, journal: pd.DataFrame, limit: int, logs_dir: Path = LOGS_DIR) -> None:
    provenances = classify_entry_timing_rows(entry, journal, logs_dir=logs_dir)
    summary = summarize_entry_timing_truth(provenances)
    print_entry_timing_summary(summary)
    print("")
    print("Entry Timing Diagnostic Samples")
    samples = [item for item in provenances if item.category in {"TRUE_ORPHAN", "AMBIGUOUS_PROVENANCE"}][:limit]
    if not samples:
        print("-")
        return
    for item in samples:
        identity = item.identity or {}
        counts = item.search_counts or {}
        print(f"Index: {item.index}")
        print("Entry Timing identity")
        print(f"- timestamp: {identity.get('timestamp') or '-'}")
        print(f"- symbol: {identity.get('symbol') or '-'}")
        print(f"- direction: {identity.get('direction') or '-'}")
        print(f"- entry: {identity.get('entry') if identity.get('entry') is not None else '-'}")
        print(f"- SL: {identity.get('sl') if identity.get('sl') is not None else '-'}")
        print(f"- TP1: {identity.get('tp1') if identity.get('tp1') is not None else '-'}")
        print(f"- source ID: {identity.get('source_signal_id') or identity.get('candidate_id') or identity.get('final_candidate_id') or '-'}")
        print("Search result")
        print(f"- approved matches: {counts.get('approved_history', 0)}")
        print(f"- sent matches: {counts.get('sent', 0)}")
        print(f"- report-only matches: {counts.get('report_only', 0)}")
        print(f"- rejected matches: {counts.get('rejected', 0) + counts.get('journal_rejected', 0)}")
        raw_candidates = sum(count for key, count in counts.items() if key.endswith('.csv') or 'candidate' in key)
        print(f"- raw candidate matches: {raw_candidates}")
        print("Reason")
        print(f"- {item.reason}")
        print("")


def entry_timing_summary_dict(summary: EntryTimingTruthSummary) -> dict[str, Any]:
    return {
        "total_rows": summary.total,
        "counts": summary.counts,
        "explained_coverage_pct": summary.explained_coverage_pct,
        "approved_sent_coverage_pct": summary.approved_sent_coverage_pct,
        "warning_count": summary.warning_count,
        "warning_reasons": [
            category
            for category in ["TRUE_ORPHAN", "AMBIGUOUS_PROVENANCE", "DUPLICATE_ROW"]
            if summary.counts.get(category, 0)
        ],
    }


def _benchmark_frames(rows: int = 5000, entry_rows: int = 200) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "LINKUSDT", "AVAXUSDT", "SUIUSDT", "INJUSDT"]
    journal_rows: list[dict[str, Any]] = []
    candidate_rows: list[dict[str, Any]] = []
    base = pd.Timestamp("2026-07-01T00:00:00Z")
    for index in range(rows):
        symbol = symbols[index % len(symbols)]
        side = "LONG" if index % 2 == 0 else "SHORT"
        timestamp = (base + pd.Timedelta(minutes=15 * index)).isoformat()
        entry = round(100 + (index % 100) * 0.25, 6)
        tp1 = round(entry + 1.0 if side == "LONG" else entry - 1.0, 6)
        stop = round(entry - 0.75 if side == "LONG" else entry + 0.75, 6)
        row = {
            "signal_id": f"sig-{index}",
            "timestamp": timestamp,
            "symbol": symbol,
            "side": side,
            "entry": entry,
            "stop_loss": stop,
            "tp1": tp1,
            "signal_status": "sent" if index % 3 else "skipped_quality_filter",
            "result": "OPEN",
        }
        if index % 5 == 0:
            candidate_rows.append(row.copy())
        else:
            journal_rows.append(row)

    entry_data: list[dict[str, Any]] = []
    sent_rows = [row for row in journal_rows if row["signal_status"] == "sent"]
    for index in range(entry_rows):
        if index < entry_rows - 4:
            source = sent_rows[index % len(sent_rows)]
            entry_data.append(
                {
                    "source_signal_id": source["signal_id"],
                    "final_signal_timestamp": source["timestamp"],
                    "symbol": source["symbol"],
                    "direction": source["side"],
                    "entry": source["entry"],
                    "sl": source["stop_loss"],
                    "tp1": source["tp1"],
                }
            )
        elif index < entry_rows - 2:
            source = candidate_rows[index % len(candidate_rows)]
            entry_data.append(
                {
                    "final_signal_timestamp": source["timestamp"],
                    "symbol": source["symbol"],
                    "direction": source["side"],
                    "entry": source["entry"],
                    "sl": source["stop_loss"],
                    "tp1": source["tp1"],
                }
            )
        else:
            entry_data.append(
                {
                    "final_signal_timestamp": (base + pd.Timedelta(days=30, minutes=index)).isoformat(),
                    "symbol": f"ORPHAN{index}USDT",
                    "direction": "LONG",
                    "entry": 1.0,
                    "sl": 0.9,
                    "tp1": 1.1,
                }
            )
    return pd.DataFrame(journal_rows), pd.DataFrame(candidate_rows), pd.DataFrame(entry_data)


def run_benchmark(rows: int = 5000, entry_rows: int = 200) -> int:
    journal, candidates, entry = _benchmark_frames(rows=rows, entry_rows=entry_rows)
    with tempfile.TemporaryDirectory(prefix="crypto_audit_benchmark_") as tmp:
        logs_dir = Path(tmp)
        candidates.to_csv(logs_dir / "scanner_candidates.csv", index=False)
        profiler = AuditProfiler()
        started = time.perf_counter()
        provenances = classify_entry_timing_rows(entry, journal, logs_dir=logs_dir, profiler=profiler)
        elapsed = time.perf_counter() - started
        summary = summarize_entry_timing_truth(provenances)
    print("Data Integrity Audit Benchmark")
    print(f"Source rows: {rows}")
    print(f"Entry Timing rows: {entry_rows}")
    print(f"Elapsed: {elapsed:.3f}s")
    print(f"Matched approved/sent: {summary.counts.get('MATCHED_SENT_SIGNAL', 0) + summary.counts.get('MATCHED_APPROVED_SIGNAL', 0)}")
    print(f"Pre-final matches: {summary.counts.get('MATCHED_PRE_FINAL_CANDIDATE', 0)}")
    print(f"True orphans: {summary.counts.get('TRUE_ORPHAN', 0)}")
    print("")
    profiler.print()
    return 0


def exit_code(findings: list[AuditFinding]) -> int:
    if any(item.severity == "FAIL" for item in findings):
        return 2
    if any(item.severity == "WARNING" for item in findings):
        return 1
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit runtime CSV data without changing trading outcomes.")
    parser.add_argument("--journal", type=Path, default=JOURNAL)
    parser.add_argument("--entry-timing", type=Path, default=ENTRY_TIMING)
    parser.add_argument("--repair-safe", action="store_true", help="Apply safe normalization only after creating backups.")
    parser.add_argument("--verbose", action="store_true", help="Show sample Entry Timing mismatch reasons.")
    parser.add_argument("--entry-timing-diagnostics", action="store_true", help="Explain TRUE_ORPHAN and AMBIGUOUS Entry Timing rows.")
    parser.add_argument("--limit", type=int, default=20, help="Limit diagnostic samples.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON summary.")
    parser.add_argument("--profile", action="store_true", help="Print compact stage timings for the read-only audit.")
    parser.add_argument("--benchmark", action="store_true", help="Run a synthetic Entry Timing provenance benchmark without touching runtime logs.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.benchmark:
        return run_benchmark()
    if args.repair_safe:
        for path in [args.journal, args.entry_timing]:
            changed, backup = repair_safe(path)
            if changed:
                print(f"SAFE_REPAIR | {path} | changes={changed} | backup={backup}")
    if args.profile:
        profiler = AuditProfiler()
        findings = audit_paths(args.journal, args.entry_timing, verbose=args.verbose, profiler=profiler)
        print_findings(findings)
        print("")
        print("Audit Profile")
        profiler.print()
        return exit_code(findings)
    journal, journal_error = _load_csv(args.journal)
    entry, entry_error = _load_csv(args.entry_timing)
    if args.entry_timing_diagnostics:
        if entry_error is not None:
            print(f"Entry Timing diagnostics unavailable: {entry_error.detail}")
            return 0
        print_entry_timing_diagnostics(entry, journal, max(0, args.limit))
        return 0
    if args.json:
        findings = audit_paths(args.journal, args.entry_timing, verbose=args.verbose)
        provenances = classify_entry_timing_rows(entry, journal) if entry_error is None else []
        summary = summarize_entry_timing_truth(provenances)
        print(
            json.dumps(
                {
                    "findings": [finding.__dict__ for finding in findings],
                    "entry_timing": entry_timing_summary_dict(summary),
                    "exit_code": exit_code(findings),
                },
                indent=2,
            )
        )
        return exit_code(findings)
    findings = audit_paths(args.journal, args.entry_timing, verbose=args.verbose)
    print_findings(findings)
    return exit_code(findings)


if __name__ == "__main__":
    raise SystemExit(main())
