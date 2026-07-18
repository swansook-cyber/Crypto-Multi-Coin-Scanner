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


def _series(df: pd.DataFrame, column: str, default: str = "") -> pd.Series:
    if column in df.columns:
        return df[column]
    return pd.Series([default] * len(df), index=df.index)


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
        lock_text = str(row.get("position_watcher_lock_file", "") or "").strip()
        alert_key = str(row.get("position_watcher_alert_key", "") or "").strip()
        if not lock_text and not alert_key:
            continue
        lock_path = Path(lock_text) if lock_text else Path()
        items.append(
            StaleStateItem(
                symbol=str(row.get("symbol", "") or "").upper(),
                side=str(row.get("side", row.get("direction", "")) or "").upper(),
                result=str(row.get("result", "") or "").upper(),
                alert_key=alert_key,
                lock_file=lock_path,
                exists=bool(lock_text and lock_path.exists()),
            )
        )
    return items


def cleanup(journal_path: Path = JOURNAL, apply: bool = False) -> tuple[list[StaleStateItem], Path | None, list[Path]]:
    items = stale_state_items(journal_path)
    removed: list[Path] = []
    backup: Path | None = None
    if apply and items:
        backup = backup_runtime_data.create_backup()
        for item in items:
            if item.exists and item.lock_file.is_file():
                item.lock_file.unlink()
                removed.append(item.lock_file)
    return items, backup, removed


def print_result(items: list[StaleStateItem], backup: Path | None = None, removed: list[Path] | None = None, apply: bool = False) -> None:
    removed = removed or []
    mode = "APPLY" if apply else "DRY RUN"
    print("Position Watcher State Cleanup")
    print(f"Mode: {mode}")
    print(f"Checked at UTC: {datetime.now(timezone.utc).isoformat()}")
    print(f"Stale active state keys: {len(items)}")
    symbols = sorted({item.symbol for item in items if item.symbol})
    print(f"Affected symbols: {', '.join(symbols) if symbols else '-'}")
    if backup:
        print(f"Backup: {backup}")
    print("")
    for item in items:
        print(
            f"{item.symbol or '-'} {item.side or '-'} {item.result or '-'} | "
            f"key={item.alert_key or '-'} | lock={item.lock_file or '-'} | exists={int(item.exists)}"
        )
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
    print_result(items, backup, removed, apply=args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
