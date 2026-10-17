"""Process-safe CSV transactions and optimistic, field-level journal updates.

Every writer must use the same sidecar lock. Readers see either the old or new
complete CSV. The lock is on a stable inode, never the replaced CSV itself.
"""
from __future__ import annotations

from contextlib import contextmanager
import csv
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import time

import pandas as pd

from core.signal_identity import identity_from_record

LOG = logging.getLogger(__name__)
TERMINAL = {"ORIGINAL_SL", "TP2_WIN", "TP1_THEN_ORIGINAL_SL", "TP1_THEN_PROTECTIVE_STOP"}
LIFECYCLE = {"lifecycle_state", "lifecycle_r", "lifecycle_terminal", "tp1_fraction",
             "remainder_fraction", "tp1_touch_time_utc", "remainder_resolution_time_utc",
             "lifecycle_same_candle_ambiguous", "lifecycle_reason", "lifecycle_source",
             "source", "reconstruction_version", "price_source", "evidence_hash",
             "geometry_hash", "closed_candle_cutoff_utc", "reconstructed_at_utc",
             "ambiguity_flag", "ambiguity_reason", "tp1_touch_utc", "terminal_event_utc"}
IMMUTABLE = {"timestamp", "symbol", "side", "entry", "stop_loss", "sl", "tp1", "tp2"}


@contextmanager
def process_lock(path: Path, timeout: float = 10):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    handle.seek(0, 2)
    if not handle.tell():
        handle.write(b"0")
        handle.flush()
    started = time.monotonic()
    acquired = False
    try:
        while not acquired:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (OSError, BlockingIOError):
                if time.monotonic() - started >= timeout:
                    LOG.error("Journal lock timeout: %s", path)
                    raise TimeoutError(f"Journal lock contention: {path}")
                time.sleep(.02)
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def text(value):
    return "" if value is None or pd.isna(value) else str(value)


def record_key(row):
    key = identity_from_record(row).canonical_key
    if key:
        return key
    # Unidentifiable legacy evidence is preserved rather than merged arbitrarily.
    return "legacy:" + hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()


def _read(path):
    if not path.exists():
        return [], []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle, strict=True)
        rows = list(reader)
        if any(None in row or any(v is None for v in row.values()) for row in rows):
            raise ValueError(f"Malformed journal; refusing to overwrite evidence: {path}")
        return reader.fieldnames or [], rows


def atomic_csv(path, columns, rows):
    """Caller holds process lock. A killed writer cannot truncate the live CSV."""
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows({k: row.get(k, "") for k in columns} for row in rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_frame(path):
    path = Path(path)
    with process_lock(path.with_suffix(path.suffix + ".lock")):
        columns, rows = _read(path)
    frame = pd.DataFrame(rows, columns=columns)
    frame.attrs["journal_baseline"] = {record_key(row): dict(row) for row in rows}
    return frame


def _rank(row):
    if row.get("lifecycle_state") not in TERMINAL:
        return 0
    source = row.get("lifecycle_source") or row.get("source", "")
    if source == "LEGACY_FALLBACK" or row.get("lifecycle_reason") == "migrated_from_terminal_compatibility_outcome":
        return 1
    if "HISTORICAL" in source:
        return 2
    return 3


def _merge(current, incoming, baseline):
    changes = {k: v for k, v in incoming.items() if baseline is None or text(baseline.get(k)) != v}
    old_rank, new_rank = _rank(current), _rank(incoming)
    if old_rank and new_rank == old_rank:
        for field in ("lifecycle_state", "lifecycle_r", "remainder_resolution_time_utc"):
            if field in changes and text(current.get(field)) and text(current.get(field)) != text(incoming.get(field)):
                # Numeric serialization (1 vs 1.000000) is not conflicting truth.
                if field == "lifecycle_r" and float(current[field]) == float(incoming[field]):
                    continue
                raise ValueError(f"Conflicting terminal lifecycle evidence: {field}")
    for field, value in changes.items():
        old = text(current.get(field))
        if field in IMMUTABLE and old:
            continue
        if field in LIFECYCLE | {"result", "hit_target", "closed_at", "real_rr", "outcome", "pnl_percent", "holding_minutes"} and old_rank > new_rank:
            continue
        if old and not value:
            continue
        # Positive delivery evidence is monotone; stale defaults cannot erase it.
        if (field.endswith("_sent") or field == "breakeven_recommended") and old.lower() in {"1", "1.0", "true", "yes"}:
            continue
        if baseline is not None and old != text(baseline.get(field)) and old != value:
            if field not in LIFECYCLE or new_rank <= old_rank:
                # First successful commit wins same-field conflicts; disjoint
                # fields still merge. Never invent an ordering from wall clocks.
                continue
        current[field] = value
    return current


def merge_frame(path, frame, *, key_func=record_key):
    path = Path(path)
    baseline = frame.attrs.get("journal_baseline")
    incoming = [{str(k): text(v) for k, v in row.items()} for row in frame.to_dict("records")]
    try:
        with process_lock(path.with_suffix(path.suffix + ".lock")):
            columns, existing = _read(path)
            columns = list(dict.fromkeys(columns + list(frame.columns)))
            indexed = {}
            for row in existing:
                key = key_func(row)
                indexed[key] = _merge(indexed[key], row, None) if key in indexed else dict(row)
            for row in incoming:
                key = key_func(row)
                if key in indexed:
                    indexed[key] = _merge(indexed[key], row, baseline.get(key, {}) if baseline is not None else None)
                else:
                    indexed[key] = row
            result = list(indexed.values())
            if columns and (result != existing or columns != _read(path)[0]):
                atomic_csv(path, columns, result)
            frame.attrs["journal_baseline"] = {key_func(row): dict(row) for row in incoming}
            LOG.debug("Journal records merged: %s count=%d", path, len(incoming))
            return pd.DataFrame(result, columns=columns)
    except Exception:
        LOG.exception("Journal persistence failed; evidence retained for retry: %s", path)
        raise
