"""Plan or apply conservative post-boundary lifecycle reconciliation.

Dry-run is the default.  Apply writes only derived lifecycle facts and exact
canonical rows whose closed-candle path is deterministic.  Ambiguous execution
ownership and same-candle ordering are always preserved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.historical_lifecycle_reconstruction import (  # noqa: E402
    AMBIGUOUS_STATE,
    LIVE_OPEN_STATE,
    TERMINAL_STATES,
    load_canonical_reporting_population,
    reconstruct_population,
    records_as_dicts,
    signal_key,
)
from core.research_telemetry import ResearchTelemetryStore  # noqa: E402


DEFAULT_BOUNDARY = "2026-10-22T13:00:20.087460Z"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ambiguous_execution_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    data = pd.read_csv(path, dtype=str).fillna("")
    keys: set[str] = set()
    for row in data[data.get("match_status", "").str.upper().eq("AMBIGUOUS")].to_dict("records"):
        raw = row.get("candidate_signal_keys", "")
        try:
            candidates = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            candidates = []
        keys.update(str(value) for value in candidates if str(value).startswith(("sig:v1:", "id:v1:")))
    return keys


def classify(record: dict[str, Any], current_state: str, execution_ambiguous: bool) -> str:
    if execution_ambiguous:
        return "EXECUTION_AMBIGUOUS"
    state = str(record["lifecycle_state"]).upper()
    if state == AMBIGUOUS_STATE:
        return "LIFECYCLE_AMBIGUOUS"
    if state in TERMINAL_STATES:
        return "DETERMINISTIC_RECOVERABLE"
    if current_state == LIVE_OPEN_STATE and state == "UNRESOLVED_REMAINDER":
        return "GENUINELY_OPEN"
    return "INSUFFICIENT_EVIDENCE"


def build_plan(
    signals: Path,
    history: Path,
    db: Path,
    execution: Path,
    boundary: str,
    cutoff: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    population, _ = load_canonical_reporting_population(signals, history, db)
    timestamp = pd.to_datetime(population.get("timestamp"), utc=True, errors="coerce")
    state = population.get("lifecycle_state", pd.Series("", index=population.index)).fillna("").astype(str).str.upper()
    pending = population[(timestamp >= pd.Timestamp(boundary)) & ~state.isin(TERMINAL_STATES)].copy()
    records, errors = reconstruct_population(pending, cutoff)
    ambiguous_keys = ambiguous_execution_keys(execution)
    by_key = {str(row["canonical_signal_key"]): row for row in pending.to_dict("records")}
    plan: list[dict[str, Any]] = []
    recoverable: list[dict[str, Any]] = []
    for record in records_as_dicts(records):
        key = record["canonical_signal_key"]
        source = by_key[key]
        classification = classify(record, str(source.get("lifecycle_state", "")).upper(), key in ambiguous_keys)
        item = {
            "canonical_signal_key": key,
            "timestamp": record["timestamp_utc"],
            "symbol": record["symbol"],
            "side": record["side"],
            "classification": classification,
            "current_lifecycle_state": source.get("lifecycle_state", ""),
            "population_provenance": source.get("population_provenance", ""),
            "geometry": {name: source.get(name, source.get("stop_loss", "") if name == "sl" else "") for name in ("entry", "sl", "tp1", "tp2")},
            "candle_evidence": {
                "price_source": record["price_source"],
                "interval": record["price_interval"],
                "tp1_touch_utc": record["tp1_touch_utc"],
                "terminal_event_utc": record["terminal_event_utc"],
                "evidence_hash": record["evidence_hash"],
            },
            "proposed_lifecycle_state": record["lifecycle_state"],
            "proposed_lifecycle_r": record["lifecycle_r"],
            "deterministic": classification == "DETERMINISTIC_RECOVERABLE",
        }
        plan.append(item)
        if item["deterministic"]:
            recoverable.append(record)
    return plan, recoverable, errors


def backup_files(paths: list[Path], backup_root: Path) -> list[dict[str, Any]]:
    backup_root.mkdir(parents=True, exist_ok=False)
    result = []
    for source in paths:
        target = backup_root / source.name
        shutil.copy2(source, target)
        result.append({"source": str(source), "backup": str(target), "size": target.stat().st_size, "sha256": file_sha256(target)})
    return result


def update_current_csv(path: Path, records: list[dict[str, Any]]) -> int:
    data = pd.read_csv(path)
    original_hash = file_sha256(path)
    by_key = {record["canonical_signal_key"]: record for record in records}
    changed = 0
    numeric_columns = {"lifecycle_r", "lifecycle_terminal", "tp1_fraction", "remainder_fraction", "lifecycle_same_candle_ambiguous"}

    def equal(column: str, left: Any, right: Any) -> bool:
        if column in numeric_columns:
            left_number = pd.to_numeric(pd.Series([left]), errors="coerce").iloc[0]
            right_number = pd.to_numeric(pd.Series([right]), errors="coerce").iloc[0]
            return (pd.isna(left_number) and pd.isna(right_number)) or (
                pd.notna(left_number) and pd.notna(right_number) and float(left_number) == float(right_number)
            )
        left_text = "" if pd.isna(left) else str(left).strip()
        right_text = "" if pd.isna(right) else str(right).strip()
        return left_text == right_text

    for index, row in data.iterrows():
        key = signal_key(row.to_dict())
        record = by_key.get(key)
        if not record:
            continue
        values = {
            "lifecycle_state": record["lifecycle_state"],
            "lifecycle_r": record["lifecycle_r"],
            "lifecycle_terminal": 1,
            "tp1_fraction": record["tp1_fraction"],
            "remainder_fraction": record["remainder_fraction"],
            "tp1_touch_time_utc": record["tp1_touch_utc"],
            "remainder_resolution_time_utc": record["terminal_event_utc"],
            "lifecycle_same_candle_ambiguous": record["ambiguity_flag"],
            "lifecycle_reason": record["ambiguity_reason"],
        }
        if any(not equal(column, row.get(column, ""), value) for column, value in values.items()):
            for column, value in values.items():
                if column not in data.columns:
                    data[column] = pd.Series([""] * len(data), dtype="object")
                elif column not in numeric_columns:
                    data[column] = data[column].astype("object")
                data.at[index, column] = value
            changed += 1
    if not changed:
        return 0
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        data.to_csv(temporary, index=False)
        if file_sha256(path) != original_hash:
            raise RuntimeError("signals.csv changed during reconciliation; refusing replacement")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--signals", type=Path, default=ROOT / "logs" / "signals.csv")
    parser.add_argument("--history", type=Path, default=ROOT / "logs" / "signals_history.csv")
    parser.add_argument("--db", type=Path, default=ROOT / "research" / "scanner_research_v1.db")
    parser.add_argument("--execution", type=Path, default=ROOT / "logs" / "binance_execution_truth_v1.csv")
    parser.add_argument("--boundary", default=DEFAULT_BOUNDARY)
    parser.add_argument("--cutoff", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args()

    plan, recoverable, errors = build_plan(args.signals, args.history, args.db, args.execution, args.boundary, args.cutoff)
    output: dict[str, Any] = {
        "mode": "apply" if args.apply else "dry-run",
        "original_unresolved": len(plan),
        "deterministic_recoverable": len(recoverable),
        "remaining_unresolved": len(plan) - len(recoverable),
        "errors": errors,
        "rows": plan,
    }
    if args.apply:
        if errors:
            raise RuntimeError("candle collection errors prevent transactional apply")
        with sqlite3.connect(args.db) as connection:
            quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        if quick_check != "ok":
            raise RuntimeError(f"database quick_check failed: {quick_check}")
        if args.backup_dir is None:
            raise RuntimeError("--backup-dir is required with --apply")
        output["backups"] = backup_files([args.db, args.signals], args.backup_dir)
        store = ResearchTelemetryStore(args.db)
        output["db_changes"] = store.upsert_historical_lifecycle(recoverable)
        output["csv_rows_changed"] = update_current_csv(args.signals, recoverable)
    print(json.dumps(output, indent=2, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
