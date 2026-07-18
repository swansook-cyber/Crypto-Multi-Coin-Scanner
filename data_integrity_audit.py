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

FINAL_STATUSES = {"WIN", "LOSS", "EXPIRED", "BREAKEVEN", "CLOSED"}
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
    duplicate: int

    @property
    def coverage_pct(self) -> float:
        if self.total <= 0:
            return 0.0
        return round(self.matched / self.total * 100.0, 1)


@dataclass
class WatcherStateClassification:
    historical_flags: int
    active_stale_state: int
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
    symbol = _series(df, "symbol").fillna("").astype(str).str.upper()
    side = _series(df, side_column).fillna("").astype(str).str.upper()
    entry = pd.to_numeric(_series(df, "entry"), errors="coerce").round(8).astype(str)
    return timestamp + "|" + symbol + "|" + side + "|" + entry


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
        return EntryTimingClassification(0, 0, 0, 0, 0)
    if journal.empty:
        timestamps = pd.to_datetime(_series(entry, "timestamp"), utc=True, errors="coerce")
        legacy = int(timestamps.lt(ENTRY_TIMING_FINAL_CANDIDATE_INTEGRATION_UTC).fillna(True).sum())
        orphan = int(len(entry) - legacy)
        duplicate = int(signal_prefix_df(entry, "direction").duplicated(keep=False).sum())
        return EntryTimingClassification(len(entry), 0, legacy, orphan, duplicate)

    approved = journal[_series(journal, "signal_status").fillna("").astype(str).str.lower().isin(APPROVED_FINAL_STATUSES)].copy()
    approved_prefixes = set(signal_prefix_df(approved, "side").astype(str))
    entry_key = signal_prefix_df(entry, "direction").astype(str)
    timestamps = pd.to_datetime(_series(entry, "timestamp"), utc=True, errors="coerce")
    matched_mask = entry_key.isin(approved_prefixes)
    legacy_mask = ~matched_mask & timestamps.lt(ENTRY_TIMING_FINAL_CANDIDATE_INTEGRATION_UTC).fillna(True)
    orphan_mask = ~matched_mask & ~legacy_mask
    duplicate_mask = entry_key.duplicated(keep=False)
    return EntryTimingClassification(
        total=int(len(entry)),
        matched=int(matched_mask.sum()),
        legacy=int(legacy_mask.sum()),
        orphan=int(orphan_mask.sum()),
        duplicate=int(duplicate_mask.sum()),
    )


def audit_entry_timing(entry: pd.DataFrame, journal: pd.DataFrame) -> list[AuditFinding]:
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
                f"duplicate={classification.duplicate}, coverage={classification.coverage_pct:.1f}%"
            ),
        )
    )
    if classification.legacy:
        findings.append(AuditFinding("INFO", "LEGACY_SHADOW_ROW", f"{classification.legacy} historical rows before final-candidate integration"))
    if classification.orphan:
        findings.append(AuditFinding("WARNING", "ORPHAN_ROW", f"{classification.orphan} post-integration Entry Timing rows without final candidate match"))
    if classification.duplicate:
        findings.append(AuditFinding("WARNING", "DUPLICATE_ROW", f"{classification.duplicate} duplicate Entry Timing rows"))
    return findings


def classify_watcher_state(journal: pd.DataFrame) -> WatcherStateClassification:
    if journal.empty:
        return WatcherStateClassification(0, 0, 0, [])
    result = _series(journal, "result", "OPEN").fillna("OPEN").astype(str).str.upper()
    stage = _series(journal, "position_management_stage").fillna("").astype(str)
    lock_file = _series(journal, "position_watcher_lock_file").fillna("").astype(str).str.strip()
    alert_key = _series(journal, "position_watcher_alert_key").fillna("").astype(str).str.strip()
    status = _series(journal, "signal_status").fillna("").astype(str).str.lower()
    historical = result.isin(FINAL_STATUSES) & stage.str.contains("TP1_REACHED", case=False, na=False)
    active_stale = result.isin(FINAL_STATUSES) & (lock_file.ne("") | alert_key.ne(""))
    closed_open = result.isin(FINAL_STATUSES) & status.isin(["active"])
    symbols = sorted(_series(journal.loc[active_stale], "symbol").fillna("").astype(str).str.upper().unique().tolist())
    return WatcherStateClassification(
        historical_flags=int(historical.sum()),
        active_stale_state=int(active_stale.sum()),
        closed_treated_open=int(closed_open.sum()),
        symbols=symbols,
    )


def audit_stale_watcher_state(journal: pd.DataFrame) -> list[AuditFinding]:
    state = classify_watcher_state(journal)
    findings: list[AuditFinding] = []
    if state.historical_flags:
        findings.append(AuditFinding("INFO", "CLOSED_ROW_WITH_HISTORICAL_TP1_FLAG", f"{state.historical_flags} closed rows keep TP1 audit fields"))
    if state.active_stale_state:
        findings.append(AuditFinding("WARNING", "CLOSED_ROW_IN_ACTIVE_WATCHER_STATE", f"{state.active_stale_state} rows; symbols={','.join(state.symbols) or '-'}"))
    if state.closed_treated_open:
        findings.append(AuditFinding("FAIL", "CLOSED_ROW_STILL_TREATED_AS_OPEN", f"{state.closed_treated_open} rows"))
    if not findings:
        findings.append(AuditFinding("PASS", "position watcher active state", "no stale active watcher state"))
    return findings


def audit_paths(journal_path: Path = JOURNAL, entry_path: Path = ENTRY_TIMING) -> list[AuditFinding]:
    journal, journal_error = _load_csv(journal_path)
    entry, entry_error = _load_csv(entry_path)
    findings = [item for item in [journal_error] if item is not None]
    if entry_error is not None:
        findings.append(AuditFinding("INFO", "Entry Timing rows", f"{entry_path} missing; no shadow rows yet"))
    if journal_error is None:
        findings.extend(audit_journal(journal))
    if entry_error is None:
        findings.extend(audit_entry_timing(entry, journal))
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.repair_safe:
        for path in [args.journal, args.entry_timing]:
            changed, backup = repair_safe(path)
            if changed:
                print(f"SAFE_REPAIR | {path} | changes={changed} | backup={backup}")
    findings = audit_paths(args.journal, args.entry_timing)
    print_findings(findings)
    return exit_code(findings)


if __name__ == "__main__":
    raise SystemExit(main())
