# -*- coding: utf-8 -*-
"""Verify and clean stale Position Watcher lock files for confirmed closed rows.

Default mode is dry-run. Apply mode requires --confirm-count and removes only
regular .lock files inside the watcher lock directory after a runtime backup.
CSV history is preserved.
"""

from __future__ import annotations

import argparse
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

import backup_runtime_data


BASE_DIR = Path(__file__).resolve().parent
JOURNAL = BASE_DIR / "logs" / "signals.csv"
FINAL_RESULTS = {"WIN", "LOSS", "EXPIRED", "BREAKEVEN", "CLOSED", "TP", "SL", "TAKE_PROFIT", "STOP_LOSS"}
OPEN_RESULTS = {"", "OPEN", "0"}
SAFE_TO_REMOVE = "SAFE_TO_REMOVE"
DO_NOT_REMOVE_OPEN_POSITION = "DO_NOT_REMOVE_OPEN_POSITION"
DO_NOT_REMOVE_ACTIVE_RUNTIME = "DO_NOT_REMOVE_ACTIVE_RUNTIME"
DO_NOT_REMOVE_UNCONFIRMED = "DO_NOT_REMOVE_UNCONFIRMED"
HISTORICAL_ONLY = "HISTORICAL_ONLY"


@dataclass
class StaleStateItem:
    symbol: str
    side: str
    timestamp: str
    result: str
    alert_key: str
    lock_file: Path
    position_closed: bool
    active_runtime_state: bool
    exists: bool
    category: str
    verdict: str
    removable: bool
    reason: str = ""


@dataclass
class CleanupClassification:
    confirmed_closed_rows: int = 0
    historical_closed_rows: int = 0
    active_stale_state_entries: int = 0
    existing_stale_lock_files: int = 0
    empty_or_nan_references: int = 0
    invalid_missing_references: int = 0
    safe_to_remove: int = 0
    blocked_open_positions: int = 0
    blocked_active_runtime: int = 0
    blocked_unconfirmed: int = 0
    removable_items: int = 0
    affected_symbols: list[str] | None = None


def _series(df: pd.DataFrame, column: str, default: str = "") -> pd.Series:
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


def audit_field_present(value: Any) -> bool:
    if is_blank(value):
        return False
    return str(value).strip().lower() not in {"0", "0.0", "false", "no"}


def normalize_symbol(value: Any) -> str:
    if is_blank(value):
        return ""
    text = str(value).strip().upper()
    if ":" in text:
        text = text.split(":")[-1]
    return text.replace("#", "").replace(".P", "").replace("/", "").replace("-", "").replace("_", "")


def normalize_side(value: Any) -> str:
    text = "" if is_blank(value) else str(value).strip().upper()
    if text == "BUY":
        return "LONG"
    if text == "SELL":
        return "SHORT"
    return text if text in {"LONG", "SHORT"} else ""


def numeric_text(value: Any) -> str:
    if is_blank(value):
        return ""
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric):
        return ""
    return f"{float(numeric):.8f}".rstrip("0").rstrip(".")


def load_journal(path: Path = JOURNAL) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def approved_lock_dir(journal_path: Path = JOURNAL) -> Path:
    return journal_path.parent / f"{journal_path.stem}_position_watcher_locks"


def identity_key(row: pd.Series) -> str:
    explicit = row.get("signal_id", row.get("candidate_id", ""))
    if not is_blank(explicit):
        return f"id:{str(explicit).strip()}"
    symbol = normalize_symbol(row.get("symbol", ""))
    side = normalize_side(row.get("side", row.get("direction", "")))
    timestamp = "" if is_blank(row.get("timestamp", "")) else str(row.get("timestamp", "")).strip()
    entry = numeric_text(row.get("entry", ""))
    tp1 = numeric_text(row.get("tp1", ""))
    stop = numeric_text(row.get("breakeven_price", row.get("entry", ""))) or entry
    if not all([symbol, side, timestamp, entry, tp1, stop]):
        return ""
    return f"{symbol}|{side}|{timestamp}|{entry}|{tp1}|{stop}"


def deterministic_identity(row: pd.Series) -> bool:
    return bool(identity_key(row))


def terminal_result(value: Any) -> bool:
    return str(value if not is_blank(value) else "").strip().upper() in FINAL_RESULTS


def open_result(value: Any) -> bool:
    return str(value if not is_blank(value) else "").strip().upper() in OPEN_RESULTS


def _open_identity_keys(df: pd.DataFrame) -> set[str]:
    if df.empty:
        return set()
    result = _series(df, "result", "OPEN").fillna("OPEN").astype(str).str.upper()
    open_rows = df[result.isin(OPEN_RESULTS)].copy()
    return {key for _, row in open_rows.iterrows() if (key := identity_key(row))}


def _closed_identity_counts(df: pd.DataFrame) -> dict[str, int]:
    if df.empty:
        return {}
    result = _series(df, "result", "OPEN").fillna("OPEN").astype(str).str.upper()
    closed = df[result.isin(FINAL_RESULTS)].copy()
    counts: dict[str, int] = {}
    for _, row in closed.iterrows():
        key = identity_key(row)
        if key:
            counts[key] = counts.get(key, 0) + 1
    return counts


def path_safety(lock_file: Path, journal_path: Path = JOURNAL) -> tuple[bool, str, Path | None]:
    if not lock_file or is_blank(str(lock_file)):
        return False, "blank lock path", None
    try:
        base = approved_lock_dir(journal_path).resolve(strict=False)
        resolved = lock_file.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        return False, f"malformed lock path: {exc}", None
    try:
        resolved.relative_to(base)
    except ValueError:
        return False, "lock path outside approved watcher lock directory", resolved
    if resolved.suffix != ".lock":
        return False, "lock path is not a .lock file", resolved
    if resolved.exists():
        try:
            strict_resolved = lock_file.resolve(strict=True)
            strict_resolved.relative_to(base)
        except (OSError, RuntimeError, ValueError):
            return False, "lock symlink escapes approved directory", resolved
        if not strict_resolved.is_file() or strict_resolved.is_dir():
            return False, "lock path is not a regular file", strict_resolved
    return True, "path ok", resolved


def _service_state(unit: str) -> str:
    try:
        completed = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=8, check=False)
        return (completed.stdout or completed.stderr or f"exit={completed.returncode}").strip()
    except FileNotFoundError:
        return "systemctl unavailable"
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def service_snapshot() -> dict[str, str]:
    return {
        "crypto-scanner.service": _service_state("crypto-scanner.service"),
        "crypto-position-watcher.service": _service_state("crypto-position-watcher.service"),
    }


def evaluate_row(row: pd.Series, df: pd.DataFrame, journal_path: Path = JOURNAL) -> StaleStateItem:
    symbol = normalize_symbol(row.get("symbol", ""))
    side = normalize_side(row.get("side", row.get("direction", "")))
    timestamp = "" if is_blank(row.get("timestamp", "")) else str(row.get("timestamp", "")).strip()
    result = "" if is_blank(row.get("result", "")) else str(row.get("result", "")).strip().upper()
    lock_raw = row.get("position_watcher_lock_file", "")
    key_raw = row.get("position_watcher_alert_key", "")
    lock_blank = is_blank(lock_raw)
    key_blank = is_blank(key_raw)
    lock_file = Path() if lock_blank else Path(str(lock_raw).strip())
    alert_key = "" if key_blank else str(key_raw).strip()
    historical_fields = any(
        audit_field_present(row.get(column, ""))
        for column in ["tp1_alert_sent", "breakeven_recommended", "breakeven_price", "position_management_stage"]
    )
    position_closed = terminal_result(result)
    row_identity = identity_key(row)
    active_runtime_state = bool(row_identity and row_identity in _open_identity_keys(df))
    closed_counts = _closed_identity_counts(df)
    lock_exists = bool(not lock_blank and lock_file.exists())
    path_ok, path_reason, safe_path = path_safety(lock_file, journal_path) if not lock_blank else (False, "blank lock path", None)

    category = "HISTORICAL_CLOSED_ROW"
    verdict = HISTORICAL_ONLY
    removable = False
    reason = "historical audit fields only"
    if lock_blank and key_blank:
        if not historical_fields:
            reason = "blank watcher fields"
        elif not position_closed:
            verdict = DO_NOT_REMOVE_OPEN_POSITION
            reason = "position not terminal"
        return StaleStateItem(symbol, side, timestamp, result, alert_key, lock_file, position_closed, active_runtime_state, False, category, verdict, removable, reason)

    if not position_closed:
        return StaleStateItem(symbol, side, timestamp, result, alert_key, lock_file, False, active_runtime_state, lock_exists, "OPEN_OR_UNCONFIRMED", DO_NOT_REMOVE_OPEN_POSITION, False, "position not terminal")
    if not deterministic_identity(row) or closed_counts.get(row_identity, 0) != 1:
        return StaleStateItem(symbol, side, timestamp, result, alert_key, lock_file, position_closed, active_runtime_state, lock_exists, "UNCONFIRMED_IDENTITY", DO_NOT_REMOVE_UNCONFIRMED, False, "identity missing or ambiguous")
    if active_runtime_state:
        return StaleStateItem(symbol, side, timestamp, result, alert_key, lock_file, position_closed, True, lock_exists, "ACTIVE_RUNTIME_STATE", DO_NOT_REMOVE_ACTIVE_RUNTIME, False, "same identity still open")
    if lock_blank:
        return StaleStateItem(symbol, side, timestamp, result, alert_key, lock_file, position_closed, False, False, "HISTORICAL_CLOSED_ROW", HISTORICAL_ONLY, False, "key without lock path")
    if not lock_exists:
        return StaleStateItem(symbol, side, timestamp, result, alert_key, lock_file, position_closed, False, False, "INVALID_LOCK_REFERENCE", HISTORICAL_ONLY, False, "lock reference does not exist")
    if not path_ok or safe_path is None:
        return StaleStateItem(symbol, side, timestamp, result, alert_key, lock_file, position_closed, False, lock_exists, "INVALID_LOCK_REFERENCE", DO_NOT_REMOVE_UNCONFIRMED, False, path_reason)
    return StaleStateItem(symbol, side, timestamp, result, alert_key, safe_path, True, False, True, "STALE_LOCK_FILE", SAFE_TO_REMOVE, True, "confirmed closed stale lock")


def all_state_items(journal_path: Path = JOURNAL) -> list[StaleStateItem]:
    df = load_journal(journal_path)
    if df.empty:
        return []
    rows = []
    for _, row in df.iterrows():
        watcher_columns = [
            "position_watcher_lock_file",
            "position_watcher_alert_key",
            "tp1_alert_sent",
            "breakeven_recommended",
            "breakeven_price",
            "position_management_stage",
        ]
        has_watcher_columns = any(column in row.index for column in watcher_columns)
        has_active_watcher_values = any(column in row.index and audit_field_present(row.get(column)) for column in watcher_columns)
        has_watcher_fields = has_active_watcher_values or (terminal_result(row.get("result", "")) and has_watcher_columns)
        if has_watcher_fields:
            rows.append(evaluate_row(row, df, journal_path))
    return rows


def stale_state_items(journal_path: Path = JOURNAL) -> list[StaleStateItem]:
    return [item for item in all_state_items(journal_path) if item.verdict == SAFE_TO_REMOVE]


def classify_cleanup(journal_path: Path = JOURNAL) -> CleanupClassification:
    items = all_state_items(journal_path)
    classification = CleanupClassification(affected_symbols=[])
    symbols: set[str] = set()
    for item in items:
        if item.position_closed:
            classification.confirmed_closed_rows += 1
        if item.verdict == HISTORICAL_ONLY:
            classification.historical_closed_rows += 1
        if item.category == "STALE_LOCK_FILE":
            classification.existing_stale_lock_files += 1
        if item.category in {"STALE_LOCK_FILE", "ACTIVE_RUNTIME_STATE"}:
            classification.active_stale_state_entries += 1
        if item.reason in {"blank watcher fields", "historical audit fields only"} or item.category == "HISTORICAL_CLOSED_ROW":
            classification.empty_or_nan_references += 1
        if item.category == "INVALID_LOCK_REFERENCE":
            classification.invalid_missing_references += 1
        if item.verdict == SAFE_TO_REMOVE:
            classification.safe_to_remove += 1
            classification.removable_items += 1
            if item.symbol:
                symbols.add(item.symbol)
        elif item.verdict == DO_NOT_REMOVE_OPEN_POSITION:
            classification.blocked_open_positions += 1
        elif item.verdict == DO_NOT_REMOVE_ACTIVE_RUNTIME:
            classification.blocked_active_runtime += 1
        elif item.verdict == DO_NOT_REMOVE_UNCONFIRMED:
            classification.blocked_unconfirmed += 1
    classification.affected_symbols = sorted(symbols)
    return classification


def cleanup(
    journal_path: Path = JOURNAL,
    apply: bool = False,
    confirm_count: int | None = None,
) -> tuple[list[StaleStateItem], Path | None, list[Path], str]:
    items = stale_state_items(journal_path)
    removed: list[Path] = []
    backup: Path | None = None
    verification = "NOT_RUN"
    if not apply:
        return items, backup, removed, verification
    if confirm_count is None or confirm_count != len(items):
        return items, backup, removed, "ABORT_CONFIRM_COUNT_MISMATCH"
    before = stale_state_items(journal_path)
    if len(before) != confirm_count:
        return before, backup, removed, "ABORT_STATE_CHANGED"
    if not before:
        return before, backup, removed, "PASS"
    backup = backup_runtime_data.create_backup()
    deleted: list[Path] = []
    for item in before:
        safe, reason, resolved = path_safety(item.lock_file, journal_path)
        if not safe or resolved is None or not resolved.exists() or not resolved.is_file():
            return before, backup, removed, f"FAIL:{reason}"
        resolved.unlink()
        removed.append(resolved)
        deleted.append(resolved)
    after = stale_state_items(journal_path)
    if after:
        verification = "WARNING"
    elif any(path.exists() for path in deleted):
        verification = "FAIL"
    else:
        verification = "PASS"
    return before, backup, removed, verification


def print_summary(classification: CleanupClassification) -> None:
    print("Position Watcher Cleanup Summary")
    print(f"Confirmed closed rows: {classification.confirmed_closed_rows}")
    print(f"Historical-only rows: {classification.historical_closed_rows}")
    print(f"Existing stale locks: {classification.existing_stale_lock_files}")
    print(f"Active stale state entries: {classification.active_stale_state_entries}")
    print(f"Safe to remove: {classification.safe_to_remove}")
    print(f"Blocked open positions: {classification.blocked_open_positions}")
    print(f"Blocked active runtime: {classification.blocked_active_runtime}")
    print(f"Blocked unconfirmed: {classification.blocked_unconfirmed}")


def print_result(
    items: list[StaleStateItem],
    backup: Path | None = None,
    removed: list[Path] | None = None,
    apply: bool = False,
    journal_path: Path = JOURNAL,
    verification: str = "NOT_RUN",
) -> None:
    removed = removed or []
    mode = "APPLY" if apply else "DRY RUN"
    classification = classify_cleanup(journal_path)
    print("Position Watcher State Cleanup")
    print(f"Mode: {mode}")
    print(f"Checked at UTC: {datetime.now(timezone.utc).isoformat()}")
    print(f"Confirmed closed rows: {classification.confirmed_closed_rows}")
    print(f"Historical-only rows: {classification.historical_closed_rows}")
    print(f"Existing stale locks: {classification.existing_stale_lock_files}")
    print(f"Active stale state entries: {classification.active_stale_state_entries}")
    print(f"Safe to remove: {classification.safe_to_remove}")
    print(f"Blocked open positions: {classification.blocked_open_positions}")
    print(f"Blocked active runtime: {classification.blocked_active_runtime}")
    print(f"Blocked unconfirmed: {classification.blocked_unconfirmed}")
    print(f"Empty/NaN references: {classification.empty_or_nan_references}")
    print(f"Invalid missing references: {classification.invalid_missing_references}")
    print(f"Affected symbols: {', '.join(classification.affected_symbols or []) if classification.affected_symbols else '-'}")
    if apply:
        services = service_snapshot()
        print(f"Scanner service: {services['crypto-scanner.service']}")
        print(f"Position watcher service: {services['crypto-position-watcher.service']}")
    if backup:
        print(f"Backup: {backup}")
    print("")
    print("SAFE_TO_REMOVE Items:")
    for item in items:
        print(
            f"{item.symbol or '-'} {item.side or '-'} | timestamp={item.timestamp or '-'} | "
            f"final={item.result or '-'} | position_closed={'YES' if item.position_closed else 'NO'} | "
            f"active_runtime={'YES' if item.active_runtime_state else 'NO'} | "
            f"lock_exists={'YES' if item.exists else 'NO'} | category={item.category} | "
            f"verdict={item.verdict} | lock={item.lock_file}"
        )
    if not items:
        print("-")
    if apply:
        print("")
        print(f"Removed lock files: {len(removed)}")
        for path in removed:
            print(f"DELETED {path}")
        print(f"Cleanup verification: {verification}")
    else:
        print("")
        print("No changes made. Use --apply --confirm-count X only after reviewing SAFE_TO_REMOVE.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run cleanup for stale Position Watcher runtime state.")
    parser.add_argument("--journal", type=Path, default=JOURNAL)
    parser.add_argument("--summary", action="store_true", help="Print compact safety counts only.")
    parser.add_argument("--apply", action="store_true", help="Remove SAFE_TO_REMOVE lock files after backup.")
    parser.add_argument("--confirm-count", type=int, default=None, help="Required SAFE_TO_REMOVE count confirmation for --apply.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.summary:
        print_summary(classify_cleanup(args.journal))
        return 0
    items, backup, removed, verification = cleanup(args.journal, apply=args.apply, confirm_count=args.confirm_count)
    print_result(items, backup, removed, apply=args.apply, journal_path=args.journal, verification=verification)
    if args.apply and verification.startswith("ABORT"):
        return 2
    if args.apply and verification.startswith("FAIL"):
        return 2
    if args.apply and verification == "WARNING":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
