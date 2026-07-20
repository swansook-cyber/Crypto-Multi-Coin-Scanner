# -*- coding: utf-8 -*-
"""Verify and clean stale Position Watcher lock files for confirmed closed rows.

Default mode is dry-run. Apply mode requires --confirm-count and removes only
regular .lock files inside the watcher lock directory after a runtime backup.
CSV history is preserved.
"""

from __future__ import annotations

import argparse
import json
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
DO_NOT_REMOVE_PATH_UNSAFE = "DO_NOT_REMOVE_PATH_UNSAFE"
DO_NOT_REMOVE_IDENTITY_AMBIGUOUS = "DO_NOT_REMOVE_IDENTITY_AMBIGUOUS"
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
    path_safe: bool
    canonical_runtime_active: bool
    identity_confidence: str
    category: str
    verdict: str
    removable: bool
    reason: str = ""


@dataclass
class CleanupClassification:
    confirmed_closed_rows: int = 0
    historical_closed_rows: int = 0
    canonical_active_entries: int = 0
    stale_canonical_active_entries: int = 0
    active_stale_state_entries: int = 0
    existing_stale_lock_files: int = 0
    empty_or_nan_references: int = 0
    invalid_missing_references: int = 0
    safe_to_remove: int = 0
    blocked_open_positions: int = 0
    blocked_active_runtime: int = 0
    blocked_unconfirmed: int = 0
    blocked_unsafe_path: int = 0
    blocked_identity_ambiguous: int = 0
    removable_items: int = 0
    affected_symbols: list[str] | None = None


@dataclass
class WatcherRuntimeIndex:
    open_identity_keys: set[str]
    closed_identity_counts: dict[str, int]
    canonical_lock_keys: dict[str, Path]
    path_cache: dict[str, tuple[bool, str, Path | None]]


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


def canonical_lock_files(journal_path: Path = JOURNAL) -> list[Path]:
    lock_dir = approved_lock_dir(journal_path)
    if not lock_dir.exists() or not lock_dir.is_dir():
        return []
    return sorted(path for path in lock_dir.iterdir() if path.is_file() or path.is_symlink())


def canonical_lock_keys(journal_path: Path = JOURNAL) -> dict[str, Path]:
    keys: dict[str, Path] = {}
    for path in canonical_lock_files(journal_path):
        if path.suffix != ".lock":
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        if len(lines) >= 2 and not is_blank(lines[1]):
            keys[lines[1].strip().upper()] = path
    return keys


def build_runtime_index(df: pd.DataFrame, journal_path: Path = JOURNAL) -> WatcherRuntimeIndex:
    return WatcherRuntimeIndex(
        open_identity_keys=_open_identity_keys(df),
        closed_identity_counts=_closed_identity_counts(df),
        canonical_lock_keys=canonical_lock_keys(journal_path),
        path_cache={},
    )


def cached_path_safety(
    lock_file: Path,
    journal_path: Path,
    runtime_index: WatcherRuntimeIndex | None,
) -> tuple[bool, str, Path | None]:
    if runtime_index is None:
        return path_safety(lock_file, journal_path)
    cache_key = str(lock_file)
    if cache_key not in runtime_index.path_cache:
        runtime_index.path_cache[cache_key] = path_safety(lock_file, journal_path)
    return runtime_index.path_cache[cache_key]


def make_item(
    symbol: str,
    side: str,
    timestamp: str,
    result: str,
    alert_key: str,
    lock_file: Path,
    position_closed: bool,
    active_runtime_state: bool,
    exists: bool,
    path_safe: bool,
    canonical_runtime_active: bool,
    identity_confidence: str,
    category: str,
    verdict: str,
    removable: bool,
    reason: str,
) -> StaleStateItem:
    return StaleStateItem(
        symbol,
        side,
        timestamp,
        result,
        alert_key,
        lock_file,
        position_closed,
        active_runtime_state,
        exists,
        path_safe,
        canonical_runtime_active,
        identity_confidence,
        category,
        verdict,
        removable,
        reason,
    )


def evaluate_row(
    row: pd.Series,
    df: pd.DataFrame,
    journal_path: Path = JOURNAL,
    runtime_index: WatcherRuntimeIndex | None = None,
) -> StaleStateItem:
    runtime_index = runtime_index or build_runtime_index(df, journal_path)
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
    active_runtime_state = bool(row_identity and row_identity in runtime_index.open_identity_keys)
    closed_counts = runtime_index.closed_identity_counts
    lock_exists = bool(not lock_blank and lock_file.exists())
    path_ok, path_reason, safe_path = cached_path_safety(lock_file, journal_path, runtime_index) if not lock_blank else (False, "blank lock path", None)
    canonical_active = bool(alert_key and alert_key.upper() in runtime_index.canonical_lock_keys)
    identity_confidence = "deterministic" if row_identity and closed_counts.get(row_identity, 0) == 1 else "ambiguous" if row_identity else "missing"

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
        return make_item(symbol, side, timestamp, result, alert_key, lock_file, position_closed, active_runtime_state, False, False, False, identity_confidence, category, verdict, removable, reason)

    if not position_closed:
        return make_item(symbol, side, timestamp, result, alert_key, lock_file, False, active_runtime_state, lock_exists, path_ok, canonical_active, identity_confidence, "OPEN_OR_UNCONFIRMED", DO_NOT_REMOVE_OPEN_POSITION, False, "position not terminal")
    if not deterministic_identity(row):
        return make_item(symbol, side, timestamp, result, alert_key, lock_file, position_closed, active_runtime_state, lock_exists, path_ok, canonical_active, identity_confidence, "UNCONFIRMED_IDENTITY", DO_NOT_REMOVE_UNCONFIRMED, False, "identity missing")
    if closed_counts.get(row_identity, 0) != 1:
        return make_item(symbol, side, timestamp, result, alert_key, lock_file, position_closed, active_runtime_state, lock_exists, path_ok, canonical_active, identity_confidence, "AMBIGUOUS_IDENTITY", DO_NOT_REMOVE_IDENTITY_AMBIGUOUS, False, "identity ambiguous")
    if active_runtime_state:
        return make_item(symbol, side, timestamp, result, alert_key, lock_file, position_closed, True, lock_exists, path_ok, canonical_active, identity_confidence, "ACTIVE_RUNTIME_STATE", DO_NOT_REMOVE_ACTIVE_RUNTIME, False, "same identity still open")
    if lock_blank:
        return make_item(symbol, side, timestamp, result, alert_key, lock_file, position_closed, False, False, False, False, identity_confidence, "HISTORICAL_CLOSED_ROW", HISTORICAL_ONLY, False, "key without lock path")
    if not lock_exists:
        return make_item(symbol, side, timestamp, result, alert_key, lock_file, position_closed, False, False, path_ok, False, identity_confidence, "INVALID_LOCK_REFERENCE", HISTORICAL_ONLY, False, "lock reference does not exist")
    if not path_ok or safe_path is None:
        return make_item(symbol, side, timestamp, result, alert_key, lock_file, position_closed, False, lock_exists, False, canonical_active, identity_confidence, "UNSAFE_LOCK_PATH", DO_NOT_REMOVE_PATH_UNSAFE, False, path_reason)
    return make_item(symbol, side, timestamp, result, alert_key, safe_path, True, False, True, True, canonical_active, identity_confidence, "STALE_LOCK_FILE", SAFE_TO_REMOVE, True, "confirmed closed stale lock")


def all_state_items(journal_path: Path = JOURNAL) -> list[StaleStateItem]:
    df = load_journal(journal_path)
    if df.empty:
        return []
    runtime_index = build_runtime_index(df, journal_path)
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
            rows.append(evaluate_row(row, df, journal_path, runtime_index))
    return rows


def stale_state_items(journal_path: Path = JOURNAL) -> list[StaleStateItem]:
    return [item for item in all_state_items(journal_path) if item.verdict == SAFE_TO_REMOVE]


def classify_cleanup(journal_path: Path = JOURNAL) -> CleanupClassification:
    items = all_state_items(journal_path)
    classification = CleanupClassification(affected_symbols=[])
    symbols: set[str] = set()
    classification.canonical_active_entries = len(canonical_lock_files(journal_path))
    for item in items:
        if item.position_closed:
            classification.confirmed_closed_rows += 1
        if item.verdict == HISTORICAL_ONLY:
            classification.historical_closed_rows += 1
        if item.category == "STALE_LOCK_FILE":
            classification.existing_stale_lock_files += 1
        if item.category == "STALE_LOCK_FILE":
            classification.stale_canonical_active_entries += 1
        if item.category == "ACTIVE_RUNTIME_STATE":
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
        elif item.verdict == DO_NOT_REMOVE_PATH_UNSAFE:
            classification.blocked_unsafe_path += 1
        elif item.verdict == DO_NOT_REMOVE_IDENTITY_AMBIGUOUS:
            classification.blocked_identity_ambiguous += 1
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
    print(f"Historical closed rows: {classification.confirmed_closed_rows}")
    print(f"Historical-only rows: {classification.historical_closed_rows}")
    print(f"Canonical active entries: {classification.canonical_active_entries}")
    print(f"Stale canonical active entries: {classification.stale_canonical_active_entries}")
    print(f"Existing lock files: {classification.existing_stale_lock_files}")
    print(f"Safe removable locks: {classification.safe_to_remove}")
    print(f"Blocked open positions: {classification.blocked_open_positions}")
    print(f"Blocked active runtime: {classification.blocked_active_runtime}")
    print(f"Blocked ambiguous: {classification.blocked_identity_ambiguous}")
    print(f"Blocked unsafe path: {classification.blocked_unsafe_path}")
    print(f"Blocked unconfirmed: {classification.blocked_unconfirmed}")


def classification_to_dict(classification: CleanupClassification) -> dict[str, Any]:
    return {
        "historical_closed_rows": classification.confirmed_closed_rows,
        "historical_only_rows": classification.historical_closed_rows,
        "canonical_active_entries": classification.canonical_active_entries,
        "stale_canonical_active_entries": classification.stale_canonical_active_entries,
        "existing_lock_files": classification.existing_stale_lock_files,
        "safe_removable_locks": classification.safe_to_remove,
        "blocked_open_positions": classification.blocked_open_positions,
        "blocked_active_runtime": classification.blocked_active_runtime,
        "blocked_ambiguous": classification.blocked_identity_ambiguous,
        "blocked_unsafe_path": classification.blocked_unsafe_path,
        "blocked_unconfirmed": classification.blocked_unconfirmed,
        "warning_reasons": [
            reason
            for reason, count in [
                ("stale canonical active entries", classification.stale_canonical_active_entries),
                ("safe removable existing stale lock", classification.safe_to_remove),
                ("blocked unsafe path", classification.blocked_unsafe_path),
                ("blocked ambiguous identity", classification.blocked_identity_ambiguous),
            ]
            if count
        ],
    }


def item_to_dict(item: StaleStateItem) -> dict[str, Any]:
    return {
        "symbol": item.symbol,
        "direction": item.side,
        "timestamp": item.timestamp,
        "signal_status": item.result,
        "identity_confidence": item.identity_confidence,
        "open_identity_match": item.active_runtime_state,
        "canonical_runtime_active": item.canonical_runtime_active,
        "lock_exists": item.exists,
        "path_safe": item.path_safe,
        "verdict": item.verdict,
        "reason": item.reason,
        "lock_file": str(item.lock_file) if item.lock_file else "",
    }


def print_diagnostics(journal_path: Path = JOURNAL) -> None:
    print("Position Watcher Diagnostics")
    print(f"Checked at UTC: {datetime.now(timezone.utc).isoformat()}")
    print("")
    items = all_state_items(journal_path)
    for item in items:
        print(f"{item.symbol or '-'} {item.side or '-'} {item.timestamp or '-'}")
        print(f"Signal status: {item.result or '-'}")
        print(f"Signal identity confidence: {item.identity_confidence}")
        print(f"Open identity match: {'YES' if item.active_runtime_state else 'NO'}")
        print(f"Canonical runtime active: {'YES' if item.canonical_runtime_active else 'NO'}")
        print(f"Lock exists: {'YES' if item.exists else 'NO'}")
        print(f"Path safe: {'YES' if item.path_safe else 'NO'}")
        print(f"Verdict: {item.verdict}")
        print(f"Reason: {item.reason}")
        print("")
    if not items:
        print("No watcher state items found.")


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
    print(f"Canonical active entries: {classification.canonical_active_entries}")
    print(f"Stale canonical active entries: {classification.stale_canonical_active_entries}")
    print(f"Existing lock files: {classification.existing_stale_lock_files}")
    print(f"Safe removable locks: {classification.safe_to_remove}")
    print(f"Blocked open positions: {classification.blocked_open_positions}")
    print(f"Blocked active runtime: {classification.blocked_active_runtime}")
    print(f"Blocked ambiguous: {classification.blocked_identity_ambiguous}")
    print(f"Blocked unsafe path: {classification.blocked_unsafe_path}")
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
            f"canonical_runtime={'YES' if item.canonical_runtime_active else 'NO'} | "
            f"lock_exists={'YES' if item.exists else 'NO'} | path_safe={'YES' if item.path_safe else 'NO'} | category={item.category} | "
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
    parser.add_argument("--diagnostics", action="store_true", help="Print per-item watcher truth diagnostics.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON for summary/diagnostics.")
    parser.add_argument("--apply", action="store_true", help="Remove SAFE_TO_REMOVE lock files after backup.")
    parser.add_argument("--confirm-count", type=int, default=None, help="Required SAFE_TO_REMOVE count confirmation for --apply.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.summary:
        classification = classify_cleanup(args.journal)
        if args.json:
            print(json.dumps(classification_to_dict(classification), indent=2))
        else:
            print_summary(classification)
        return 0
    if args.diagnostics:
        if args.json:
            items = [item_to_dict(item) for item in all_state_items(args.journal)]
            print(json.dumps({"summary": classification_to_dict(classify_cleanup(args.journal)), "items": items}, indent=2))
        else:
            print_diagnostics(args.journal)
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
