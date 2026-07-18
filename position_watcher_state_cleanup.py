# -*- coding: utf-8 -*-
"""Clean stale active Position Watcher runtime state for confirmed closed rows.

Default mode is dry-run. Apply mode backs up runtime state first and removes
only lock files tied to confirmed closed positions. CSV history is preserved.
"""

from __future__ import annotations

import argparse
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

import backup_runtime_data


BASE_DIR = Path(__file__).resolve().parent
JOURNAL = BASE_DIR / "logs" / "signals.csv"
FINAL_RESULTS = {"WIN", "LOSS", "EXPIRED", "BREAKEVEN", "CLOSED"}


@dataclass
class StaleStateItem:
    symbol: str
    side: str
    result: str
    alert_key: str
    lock_file: Path
    exists: bool
    category: str
    removable: bool


@dataclass
class CleanupClassification:
    historical_closed_rows: int = 0
    active_stale_state_entries: int = 0
    existing_stale_lock_files: int = 0
    empty_or_nan_references: int = 0
    invalid_missing_references: int = 0
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


def load_journal(path: Path = JOURNAL) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def stale_state_items(journal_path: Path = JOURNAL) -> list[StaleStateItem]:
    df = load_journal(journal_path)
    if df.empty:
        return []
    result = _series(df, "result", "OPEN").fillna("OPEN").astype(str).str.upper()
    closed = df[result.isin(FINAL_RESULTS)].copy()
    items: list[StaleStateItem] = []
    for _, row in closed.iterrows():
        lock_raw = row.get("position_watcher_lock_file", "")
        key_raw = row.get("position_watcher_alert_key", "")
        lock_blank = is_blank(lock_raw)
        key_blank = is_blank(key_raw)
        if lock_blank and key_blank:
            continue
        lock_text = "" if lock_blank else str(lock_raw).strip()
        alert_key = "" if key_blank else str(key_raw).strip()
        lock_path = Path(lock_text) if lock_text else Path()
        exists = bool(lock_text and lock_path.exists())
        category = "STALE_LOCK_FILE" if exists else "INVALID_LOCK_REFERENCE" if lock_text else "HISTORICAL_CLOSED_ROW"
        items.append(
            StaleStateItem(
                symbol=str(row.get("symbol", "") or "").upper(),
                side=str(row.get("side", row.get("direction", "")) or "").upper(),
                result=str(row.get("result", "") or "").upper(),
                alert_key=alert_key,
                lock_file=lock_path,
                exists=exists,
                category=category,
                removable=exists,
            )
        )
    return items


def classify_cleanup(journal_path: Path = JOURNAL) -> CleanupClassification:
    df = load_journal(journal_path)
    if df.empty:
        return CleanupClassification(affected_symbols=[])
    result = _series(df, "result", "OPEN").fillna("OPEN").astype(str).str.upper()
    closed = df[result.isin(FINAL_RESULTS)].copy()
    classification = CleanupClassification(affected_symbols=[])
    symbols: set[str] = set()
    for _, row in closed.iterrows():
        lock_raw = row.get("position_watcher_lock_file", "")
        key_raw = row.get("position_watcher_alert_key", "")
        lock_blank = is_blank(lock_raw)
        key_blank = is_blank(key_raw)
        historical_fields = any(
            not is_blank(row.get(column, ""))
            for column in ["tp1_alert_sent", "breakeven_recommended", "breakeven_price", "position_management_stage"]
        )
        if lock_blank and key_blank:
            if historical_fields:
                classification.historical_closed_rows += 1
                classification.empty_or_nan_references += 1
            continue
        if historical_fields:
            classification.historical_closed_rows += 1
        if not lock_blank:
            lock_path = Path(str(lock_raw).strip())
            if lock_path.exists():
                classification.existing_stale_lock_files += 1
                classification.active_stale_state_entries += 1
                classification.removable_items += 1
                symbol = str(row.get("symbol", "") or "").upper()
                if symbol:
                    symbols.add(symbol)
            else:
                classification.invalid_missing_references += 1
        elif not key_blank:
            classification.historical_closed_rows += 1
    classification.affected_symbols = sorted(symbols)
    return classification


def cleanup(journal_path: Path = JOURNAL, apply: bool = False) -> tuple[list[StaleStateItem], Path | None, list[Path]]:
    items = [item for item in stale_state_items(journal_path) if item.removable]
    removed: list[Path] = []
    backup: Path | None = None
    if apply and items:
        backup = backup_runtime_data.create_backup()
        for item in items:
            if item.exists and item.lock_file.is_file():
                item.lock_file.unlink()
                removed.append(item.lock_file)
    return items, backup, removed


def print_result(
    items: list[StaleStateItem],
    backup: Path | None = None,
    removed: list[Path] | None = None,
    apply: bool = False,
    journal_path: Path = JOURNAL,
) -> None:
    removed = removed or []
    mode = "APPLY" if apply else "DRY RUN"
    classification = classify_cleanup(journal_path)
    print("Position Watcher State Cleanup")
    print(f"Mode: {mode}")
    print(f"Checked at UTC: {datetime.now(timezone.utc).isoformat()}")
    print(f"Historical closed rows: {classification.historical_closed_rows}")
    print(f"Active stale state entries: {classification.active_stale_state_entries}")
    print(f"Existing stale lock files: {classification.existing_stale_lock_files}")
    print(f"Empty/NaN references: {classification.empty_or_nan_references}")
    print(f"Invalid missing references: {classification.invalid_missing_references}")
    print(f"Removable items: {classification.removable_items}")
    print(f"Affected symbols: {', '.join(classification.affected_symbols or []) if classification.affected_symbols else '-'}")
    if backup:
        print(f"Backup: {backup}")
    print("")
    print("Removable Items:")
    for item in items:
        print(
            f"{item.symbol or '-'} {item.side or '-'} {item.result or '-'} | "
            f"key={item.alert_key or '-'} | lock={item.lock_file or '-'} | "
            f"exists={int(item.exists)} | category={item.category}"
        )
    if not items:
        print("-")
    if apply:
        print("")
        print(f"Removed lock files: {len(removed)}")
        for path in removed:
            print(f"REMOVED {path}")
    else:
        print("")
        print("No changes made. Re-run with --apply only after reviewing the listed keys.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run cleanup for stale Position Watcher runtime state.")
    parser.add_argument("--journal", type=Path, default=JOURNAL)
    parser.add_argument("--apply", action="store_true", help="Remove confirmed stale active lock files after backup.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    items, backup, removed = cleanup(args.journal, apply=args.apply)
    print_result(items, backup, removed, apply=args.apply, journal_path=args.journal)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
