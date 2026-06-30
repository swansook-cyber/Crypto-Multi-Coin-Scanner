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

FINAL_STATUSES = {"WIN", "LOSS", "EXPIRED", "BREAKEVEN"}
OPEN_STATUSES = {"", "OPEN"}
VALID_RESULTS = FINAL_STATUSES | {"OPEN", "SKIPPED"}
VALID_SIGNAL_STATUSES = {
    "sent",
    "logged_quality_filter",
    "skipped_daily_risk_guard",
    "skipped_btc_regime",
    "skipped_loss_cooldown",
    "skipped_correlation",
    "skipped_not_top_candidate",
    "tier_c_report_only",
    "weak_symbol_report_only",
    "session_risk_report_only",
    "london_long_report_only",
}


@dataclass
class AuditFinding:
    severity: str
    check: str
    detail: str


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
    closed_active = result.isin(FINAL_STATUSES) & active_status.isin(["open", "active"])
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


def audit_entry_timing(entry: pd.DataFrame, journal: pd.DataFrame) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    if entry.empty:
        findings.append(AuditFinding("WARNING", "Entry Timing rows", "logs/entry_timing_engine.csv has no rows"))
        return findings
    if journal.empty:
        findings.append(AuditFinding("WARNING", "Entry Timing rows without a matching approved final candidate", "journal unavailable"))
        return findings

    approved = journal[_series(journal, "signal_status").fillna("").astype(str).str.lower().isin(["sent", "tier_c_report_only", "weak_symbol_report_only", "session_risk_report_only", "london_long_report_only"])].copy()
    approved_keys = set(signal_key_df(approved).astype(str))
    entry_key = (
        _series(entry, "timestamp").fillna("").astype(str)
        + "|"
        + _series(entry, "symbol").fillna("").astype(str).str.upper()
        + "|"
        + _series(entry, "direction").fillna("").astype(str).str.upper()
        + "|"
        + _series(entry, "entry").fillna("").astype(str)
    )
    candidate_prefixes = {key.rsplit("|", 2)[0] for key in approved_keys}
    unmatched = [key for key in entry_key.astype(str) if key.rsplit("|", 1)[0] not in candidate_prefixes]
    if unmatched:
        findings.append(AuditFinding("WARNING", "Entry Timing rows without a matching approved final candidate", f"{len(unmatched)} rows"))
    duplicates = entry_key[entry_key.duplicated(keep=False)]
    if not duplicates.empty:
        findings.append(AuditFinding("WARNING", "multiple Entry Timing rows for one approved candidate", f"{duplicates.nunique()} duplicate keys"))
    return findings


def audit_stale_watcher_state(journal: pd.DataFrame) -> list[AuditFinding]:
    if journal.empty:
        return []
    result = _series(journal, "result", "OPEN").fillna("OPEN").astype(str).str.upper()
    stage = _series(journal, "position_management_stage").fillna("").astype(str)
    stale = result.isin(FINAL_STATUSES) & stage.str.contains("TP1_REACHED", case=False, na=False)
    if stale.any():
        return [AuditFinding("WARNING", "stale position watcher state", f"{int(stale.sum())} closed rows still have TP1 management stage")]
    return []


def audit_paths(journal_path: Path = JOURNAL, entry_path: Path = ENTRY_TIMING) -> list[AuditFinding]:
    journal, journal_error = _load_csv(journal_path)
    entry, entry_error = _load_csv(entry_path)
    findings = [item for item in [journal_error, entry_error] if item is not None]
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
