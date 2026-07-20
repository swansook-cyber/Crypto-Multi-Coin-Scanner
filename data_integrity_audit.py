# -*- coding: utf-8 -*-
"""Read-only CSV integrity audit for Crypto Multi-Coin Scanner runtime data."""

from __future__ import annotations

import argparse
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
JOURNAL = LOGS_DIR / "signals.csv"
ENTRY_TIMING = LOGS_DIR / "entry_timing_engine.csv"
BACKUP_DIR = BASE_DIR / "backups"

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


def normalize_symbol(value: Any) -> str:
    if is_blank(value):
        return ""
    text = str(value).strip().upper()
    if ":" in text:
        text = text.split(":")[-1]
    text = text.replace("#", "").replace(".P", "").replace("PERP", "")
    text = text.replace("/", "").replace("-", "").replace("_", "")
    return "".join(ch for ch in text if ch.isalnum())


def normalize_direction(value: Any) -> str:
    text = "" if is_blank(value) else str(value).strip().upper()
    if text == "BUY":
        return "LONG"
    if text == "SELL":
        return "SHORT"
    return text if text in {"LONG", "SHORT"} else ""


def parse_utc(value: Any) -> pd.Timestamp | pd.NaT:
    if is_blank(value):
        return pd.NaT
    return pd.to_datetime(value, utc=True, errors="coerce")


def numeric_value(value: Any) -> float | None:
    if is_blank(value):
        return None
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric):
        return None
    return float(numeric)


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
    for column in ["source_signal_id", "candidate_id", "signal_id", "outcome_id"]:
        if column in row.index and not is_blank(row.get(column)):
            values.add(str(row.get(column)).strip())
    return values


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
    duplicate_mask = _entry_duplicate_mask(entry)
    timestamp_source = _series(entry, "final_signal_timestamp") if "final_signal_timestamp" in entry.columns else _series(entry, "timestamp")
    if journal.empty:
        timestamps = pd.to_datetime(timestamp_source, utc=True, errors="coerce")
        legacy = int(timestamps.lt(ENTRY_TIMING_FINAL_CANDIDATE_INTEGRATION_UTC).fillna(True).sum())
        orphan = int(len(entry) - legacy)
        duplicate = int(duplicate_mask.sum())
        samples = [_sample_entry(row, "journal missing") for _, row in entry[~timestamps.lt(ENTRY_TIMING_FINAL_CANDIDATE_INTEGRATION_UTC).fillna(False)].head(5).iterrows()]
        return EntryTimingClassification(len(entry), 0, legacy, orphan, 0, duplicate, samples)

    approved = journal[_series(journal, "signal_status").fillna("").astype(str).str.lower().isin(APPROVED_FINAL_STATUSES)].copy()
    timestamps = pd.to_datetime(timestamp_source, utc=True, errors="coerce")
    matched = 0
    orphan = 0
    ambiguous = 0
    legacy = 0
    samples: list[str] = []
    for index, row in entry.iterrows():
        matches, reason = _candidate_matches(row, approved)
        if len(matches) == 1:
            matched += 1
            continue
        row_timestamp = timestamps.loc[index]
        if (pd.isna(row_timestamp) or row_timestamp < ENTRY_TIMING_FINAL_CANDIDATE_INTEGRATION_UTC) and len(matches) == 0:
            legacy += 1
            continue
        if len(matches) > 1:
            ambiguous += 1
            if len(samples) < 5:
                samples.append(_sample_entry(row, f"ambiguous {len(matches)} matches via {reason}"))
            continue
        orphan += 1
        if len(samples) < 5:
            samples.append(_sample_entry(row, "no deterministic final candidate match"))
    return EntryTimingClassification(
        total=int(len(entry)),
        matched=int(matched),
        legacy=int(legacy),
        orphan=int(orphan),
        ambiguous=int(ambiguous),
        duplicate=int(duplicate_mask.sum()),
        samples=samples,
    )


def audit_entry_timing(entry: pd.DataFrame, journal: pd.DataFrame, verbose: bool = False) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    if entry.empty:
        findings.append(AuditFinding("INFO", "Entry Timing rows", "logs/entry_timing_engine.csv has no rows"))
        return findings
    classification = classify_entry_timing(entry, journal)
    findings.append(
        AuditFinding(
            "PASS",
            "Entry Timing classification summary",
            (
                f"total={classification.total}, matched={classification.matched}, "
                f"legacy={classification.legacy}, orphan={classification.orphan}, "
                f"ambiguous={classification.ambiguous}, duplicate={classification.duplicate}, "
                f"coverage={classification.coverage_pct:.1f}%"
            ),
        )
    )
    if classification.legacy:
        findings.append(AuditFinding("INFO", "LEGACY_SHADOW_ROW", f"{classification.legacy} historical rows before final-candidate integration"))
    if classification.orphan:
        findings.append(AuditFinding("WARNING", "ORPHAN_ROW", f"{classification.orphan} post-integration Entry Timing rows without final candidate match"))
    if classification.ambiguous:
        findings.append(AuditFinding("WARNING", "AMBIGUOUS_MATCH", f"{classification.ambiguous} Entry Timing rows matched multiple possible final candidates"))
    if classification.duplicate:
        findings.append(AuditFinding("WARNING", "DUPLICATE_ROW", f"{classification.duplicate} duplicate Entry Timing rows"))
    if verbose and classification.samples:
        for sample in classification.samples:
            findings.append(AuditFinding("INFO", "ENTRY_TIMING_SAMPLE", sample))
    return findings


def classify_watcher_state(journal: pd.DataFrame) -> WatcherStateClassification:
    if journal.empty:
        return WatcherStateClassification(0, 0, 0, 0, 0, 0, 0, [])
    result = _series(journal, "result", "OPEN").fillna("OPEN").astype(str).str.upper()
    stage = _series(journal, "position_management_stage").fillna("").astype(str)
    status = _series(journal, "signal_status").fillna("").astype(str).str.lower()
    historical = result.isin(FINAL_STATUSES) & stage.str.contains("TP1_REACHED", case=False, na=False)
    closed_open = result.isin(FINAL_STATUSES) & status.isin(["active"])
    closed = journal[result.isin(FINAL_STATUSES)].copy()
    active_stale = 0
    existing_locks = 0
    empty_refs = 0
    invalid_missing = 0
    symbols: set[str] = set()
    for _, row in closed.iterrows():
        lock_raw = row.get("position_watcher_lock_file", "")
        key_raw = row.get("position_watcher_alert_key", "")
        lock_blank = is_blank(lock_raw)
        key_blank = is_blank(key_raw)
        if lock_blank and key_blank:
            if any(not is_blank(row.get(column, "")) for column in ["tp1_alert_sent", "breakeven_recommended", "breakeven_price", "position_management_stage"]):
                empty_refs += 1
            continue
        lock_path = Path(str(lock_raw).strip()) if not lock_blank else None
        if lock_path is not None and lock_path.exists():
            existing_locks += 1
            active_stale += 1
            symbols.add(normalize_symbol(row.get("symbol", "")))
        elif lock_path is not None:
            invalid_missing += 1
        else:
            historical = historical.copy()
    symbols_list = sorted(symbol for symbol in symbols if symbol)
    return WatcherStateClassification(
        historical_flags=int(historical.sum()),
        active_stale_state=int(active_stale),
        existing_stale_locks=int(existing_locks),
        empty_or_nan_references=int(empty_refs),
        invalid_missing_references=int(invalid_missing),
        removable_items=int(existing_locks),
        closed_treated_open=int(closed_open.sum()),
        symbols=symbols_list,
    )


def audit_stale_watcher_state(journal: pd.DataFrame) -> list[AuditFinding]:
    state = classify_watcher_state(journal)
    findings: list[AuditFinding] = []
    if state.historical_flags:
        findings.append(AuditFinding("INFO", "CLOSED_ROW_WITH_HISTORICAL_TP1_FLAG", f"{state.historical_flags} closed rows keep TP1 audit fields"))
    if state.empty_or_nan_references:
        findings.append(AuditFinding("INFO", "EMPTY_OR_NAN_REFERENCE", f"{state.empty_or_nan_references} closed rows have blank/NaN watcher references"))
    if state.invalid_missing_references:
        findings.append(AuditFinding("INFO", "INVALID_LOCK_REFERENCE", f"{state.invalid_missing_references} nonblank lock references do not exist"))
    if state.existing_stale_locks:
        findings.append(AuditFinding("WARNING", "STALE_LOCK_FILE", f"{state.existing_stale_locks} existing stale lock files; symbols={','.join(state.symbols) or '-'}"))
    if state.active_stale_state:
        findings.append(AuditFinding("WARNING", "CLOSED_ROW_IN_ACTIVE_WATCHER_STATE", f"{state.active_stale_state} active state entries; removable={state.removable_items}; symbols={','.join(state.symbols) or '-'}"))
    if state.closed_treated_open:
        findings.append(AuditFinding("FAIL", "CLOSED_ROW_STILL_TREATED_AS_OPEN", f"{state.closed_treated_open} rows"))
    if not findings:
        findings.append(AuditFinding("PASS", "position watcher active state", "no stale active watcher state"))
    return findings


def audit_paths(journal_path: Path = JOURNAL, entry_path: Path = ENTRY_TIMING, verbose: bool = False) -> list[AuditFinding]:
    journal, journal_error = _load_csv(journal_path)
    entry, entry_error = _load_csv(entry_path)
    findings = [item for item in [journal_error] if item is not None]
    if entry_error is not None:
        findings.append(AuditFinding("INFO", "Entry Timing rows", f"{entry_path} missing; no shadow rows yet"))
    if journal_error is None:
        findings.extend(audit_journal(journal))
    if entry_error is None:
        findings.extend(audit_entry_timing(entry, journal, verbose=verbose))
    findings.extend(audit_stale_watcher_state(journal))
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.repair_safe:
        for path in [args.journal, args.entry_timing]:
            changed, backup = repair_safe(path)
            if changed:
                print(f"SAFE_REPAIR | {path} | changes={changed} | backup={backup}")
    findings = audit_paths(args.journal, args.entry_timing, verbose=args.verbose)
    print_findings(findings)
    return exit_code(findings)


if __name__ == "__main__":
    raise SystemExit(main())
