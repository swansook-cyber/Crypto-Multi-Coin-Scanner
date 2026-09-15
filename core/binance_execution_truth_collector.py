# -*- coding: utf-8 -*-
"""Standalone collector. Durable evidence -> derived CSV -> timestamp/ID state.

Only schema-projected execution/order/income events and SENT signal intent are
retained in the companion .evidence.json; the state contains only scan metadata.
All retained evidence is replayed locally, including closed/pending lifecycles.
Network collection is incremental plus a rotating six-day reconciliation slice
over retained prospective history for fill/order discovery. Income requirements
are retained per lifecycle and reread every run; expiry is explicit UNRECOVERABLE.
Late arrivals are reread while
available within exchange retention; no bound on posting finality is assumed.
Failures/saturated ranges that cannot be proven complete never advance state.
The lock serializes writers; each file is atomically replaced in recovery order.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile

from core.binance_execution_truth import BinanceExecutionTruthClient, BinanceReadOnlyError, redact_error, utc_now
from core.binance_execution_reconstruction import (
    OUTPUT_FIELDS, Signal, Fill, assert_unique_ownership, complete_sum, decimal, fmt,
    from_ms, iso, ms, reconstruct_records, signals_from_mappings, utc, valid_initial_risk,
    AUTHORITATIVE_PROVENANCE, position_checkpoints, valid_accounting_evidence, invalid_accounting_evidence,
    valid_provenance,
)

COLLECTOR_VERSION = "2.2.0"
READABLE_VERSIONS = {"2.0.0", "2.1.0", COLLECTOR_VERSION}
DEFAULT_OUTPUT = Path("logs/binance_execution_truth_v1.csv")
DEFAULT_STATE = Path("state/binance_execution_truth_v1.json")
DEFAULT_SIGNALS = Path("logs/signals.csv")
DEFAULT_CREDENTIALS = Path(".env.binance-readonly")
PAGE_SIZE = 1000
MAX_PAGES = 10000
# Older data is not claimed recoverable beyond Binance retention.
RETENTION = timedelta(days=89)
WINDOW = timedelta(days=6)
STATE_KEYS = {"collector_version", "prospective_start_utc", "endpoint_high_water", "last_successful_collection_utc", "pending_income"}
EVENT_FIELDS = {
    "trades": {"symbol", "id", "orderId", "side", "positionSide", "buyer", "maker", "time",
               "price", "qty", "realizedPnl", "commission", "commissionAsset", "marginAsset"},
    "orders": {"symbol", "orderId", "side", "positionSide", "reduceOnly", "type", "origType",
               "status", "executedQty", "time", "updateTime"},
    "income": {"symbol", "incomeType", "income", "asset", "time", "tranId", "tradeId", "orderId", "positionSide"},
}


class CollectionIncomplete(RuntimeError):
    pass


def _boundary(value):
    parsed = utc(value)
    if parsed is None:
        raise ValueError("An explicit timezone-aware start boundary is required")
    return parsed


def load_sent_signals(path, boundary, *, historical=False):
    if not path.exists():
        raise ValueError("Signal journal is missing")
    with path.open(encoding="utf-8-sig", newline="") as f:
        return signals_from_mappings(list(csv.DictReader(f)), boundary, historical)


def active_signals_for_incremental_run(signals, output, state, now):
    # All candidate identities remain available. Finality of a prior GET cannot
    # exclude late fills/costs or remove temporal conflicts from matching.
    return list(signals)


def _load_credentials(path):
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise ValueError("Credential file must be private")
    values = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        key, sep, value = line.partition("=")
        if sep:
            parsed = shlex.split(value, comments=True)
            if len(parsed) == 1:
                values[key.strip()] = parsed[0]
    key, secret = values.get("BINANCE_RO_API_KEY"), values.get("BINANCE_RO_API_SECRET")
    if not key or not secret:
        raise ValueError("Dedicated credential variables missing")
    return key, secret


def load_state(path, boundary):
    if not path.exists():
        return {"collector_version": COLLECTOR_VERSION, "prospective_start_utc": iso(boundary),
                "endpoint_high_water": {}, "last_successful_collection_utc": "", "pending_income": {}}
    result = json.loads(path.read_text(encoding="utf-8"))
    if result.get("collector_version") in {"2.0.0", "2.1.0"}:
        result.setdefault("pending_income", {})
        result["collector_version"] = COLLECTOR_VERSION
    if set(result) != STATE_KEYS or result.get("collector_version") != COLLECTOR_VERSION:
        raise ValueError("Legacy or invalid state: explicit clean rebootstrap required")
    if utc(result["prospective_start_utc"]) != boundary:
        raise ValueError("Prospective boundary differs from stored state")
    return result


def _atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent, text=True)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def atomic_write_state(path, state):
    if set(state) != STATE_KEYS:
        raise ValueError("Invalid state fields")
    _atomic_text(path, json.dumps(state, sort_keys=True, indent=2) + "\n")


def write_records(path, records):
    """Replace the complete projection, never append orphan and enriched versions."""
    assert_unique_ownership(records)
    result = io.StringIO(newline="")
    writer = csv.DictWriter(result, fieldnames=OUTPUT_FIELDS, lineterminator="\n", extrasaction="raise")
    writer.writeheader()
    writer.writerows(sorted(records, key=lambda r: r["lifecycle_id"]))
    text = result.getvalue()
    # Idempotent retries with unchanged evidence do not alter the file.
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        _atomic_text(path, text)


def evidence_path(output):
    return output.with_suffix(".evidence.json")


def load_evidence(path, boundary):
    if not path.exists():
        return {"collector_version": COLLECTOR_VERSION, "prospective_start_utc": iso(boundary),
                "trades": [], "orders": [], "income": [], "signals": [], "position_snapshots": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("collector_version") not in READABLE_VERSIONS or utc(data.get("prospective_start_utc")) != boundary:
        raise ValueError("Evidence boundary/version mismatch")
    data["collector_version"] = COLLECTOR_VERSION
    data.setdefault("position_snapshots", [])  # Old evidence never implies flatness.
    return data


def retain_signal_evidence(evidence, signals):
    """Retain SENT intent for journal rotation and deterministic crash replay."""
    indexed = {row["key"]: row for row in evidence.get("signals", [])}
    for s in signals:
        row = {"key": s.key, "symbol": s.symbol, "side": s.side, "timestamp": iso(s.timestamp),
               "entry": fmt(s.entry), "stop": fmt(s.stop), "tp1": fmt(s.tp1), "tp2": fmt(s.tp2),
               "order_ids": sorted(s.order_ids), "trade_ids": sorted(s.trade_ids)}
        old = indexed.get(s.key)
        if old:
            if any(old[k] != row[k] for k in row if k not in {"order_ids", "trade_ids"}):
                raise CollectionIncomplete("Previously observed signal intent changed")
            row["order_ids"] = sorted(set(old["order_ids"]) | set(row["order_ids"]))
            row["trade_ids"] = sorted(set(old.get("trade_ids", [])) | set(row["trade_ids"]))
        indexed[s.key] = row
    evidence["signals"] = [indexed[key] for key in sorted(indexed)]
    return [Signal(row["key"], row["symbol"], row["side"], utc(row["timestamp"]),
                   decimal(row["entry"]), decimal(row["stop"]), decimal(row["tp1"]), decimal(row["tp2"]),
                   frozenset(row["order_ids"]), frozenset(row.get("trade_ids", []))) for row in evidence["signals"]]


def _event_key(kind, row):
    if kind == "income":
        return str(row.get("incomeType")), str(row.get("tranId"))
    return str(row.get("symbol")), str(row.get("id" if kind == "trades" else "orderId"))


def merge_evidence(existing, batch):
    result = dict(existing)
    for kind in EVENT_FIELDS:
        if kind == "income":
            # Keep conflicting variants for lifecycle-local INVALID reporting.
            # Never silently overwrite or drop a malformed ledger value.
            variants = {}
            for raw in list(existing[kind]) + list(batch.get(kind, [])):
                row = {k: v for k, v in raw.items() if k in EVENT_FIELDS[kind]}
                variants[json.dumps(row, sort_keys=True)] = row
            result[kind] = [variants[k] for k in sorted(variants)]
            continue
        indexed = {_event_key(kind, r): r for r in existing[kind]}
        for raw in batch.get(kind, []):
            row = {k: v for k, v in raw.items() if k in EVENT_FIELDS[kind]}
            key = _event_key(kind, row)
            if not key[0] or not key[1].isdigit():
                raise CollectionIncomplete("Malformed evidence identity")
            previous = indexed.get(key)
            if previous and kind == "orders":
                # An older overlapping snapshot must not downgrade order evidence.
                if int(previous.get("updateTime", 0)) > int(row.get("updateTime", 0)):
                    continue
            elif previous:
                # Enrichment is allowed, contradictory immutable evidence is not.
                if any(k in previous and previous[k] not in (None, "") and v not in (None, "")
                       and previous[k] != v for k, v in row.items()):
                    raise CollectionIncomplete("Conflicting immutable evidence")
            indexed[key] = {**(previous or {}), **{k: v for k, v in row.items() if v is not None}}
        result[kind] = [indexed[k] for k in sorted(indexed)]
    return result


@contextmanager
def collection_lock(path):
    """Persistent lock file; OS releases lock even after process death."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as f:
        if f.tell() == 0:
            f.write(b"0")
            f.flush()
        f.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            f.seek(0)
            if os.name == "nt":
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)


def _rows(value):
    if not isinstance(value, list) or not all(isinstance(r, dict) for r in value):
        raise CollectionIncomplete("Unexpected endpoint schema")
    return value


def _windows(start_ms, end_ms):
    width = int(WINDOW.total_seconds() * 1000)
    while start_ms <= end_ms:
        stop = min(end_ms, start_ms + width - 1)
        yield start_ms, stop
        start_ms = stop + 1


def fetch_income(client, start, end):
    result, seen_pages = [], set()
    for page in range(1, MAX_PAGES + 1):
        batch = _rows(client.income_history(startTime=start, endTime=end, page=page, limit=PAGE_SIZE))
        fingerprint = tuple(sorted(_event_key("income", row) for row in batch))
        if batch and fingerprint in seen_pages:
            raise CollectionIncomplete("Income pagination made no progress")
        seen_pages.add(fingerprint)
        if any(from_ms(r.get("time")) is not None and not start <= ms(from_ms(r["time"])) <= end for r in batch):
            raise CollectionIncomplete("Income outside requested interval")
        result.extend(batch)
        if len(batch) < PAGE_SIZE:
            return result
    raise CollectionIncomplete("Income page limit reached")


def fetch_trades_at_saturated_ms(client, symbol, stamp, now):
    # Never start at the maximum ID of a recent-first time page.
    # ID zero explicitly asks for forward enumeration from the lowest ID.
    # The documented default no-time window is seven days: older saturation
    # cannot be proven complete and must fail without advancing any state.
    if stamp < ms(now - timedelta(days=7)):
        raise CollectionIncomplete("Older saturated timestamp requires unavailable historical cursor proof")
    result, cursor = [], 0
    for _ in range(MAX_PAGES):
        batch = _rows(client.user_trades(symbol=symbol, fromId=cursor, limit=PAGE_SIZE))
        if not batch:
            return result
        ids = [int(r["id"]) for r in batch]
        if min(ids) < cursor or len(set(ids)) != len(ids):
            raise CollectionIncomplete("Trade ID pagination made no progress")
        if any(r.get("symbol") != symbol for r in batch):
            raise CollectionIncomplete("Trade symbol mismatch")
        result.extend(r for r in batch if int(r.get("time", -1)) == stamp)
        cursor = max(ids) + 1
        if len(batch) < PAGE_SIZE:
            return result
    raise CollectionIncomplete("Trade page limit reached")


def fetch_time_range(client, kind, start, end, *, symbol=None, now=None):
    """Bisect saturated time ranges, independent of ascending/recent-first order."""
    if kind == "trades":
        batch = _rows(client.user_trades(symbol=symbol, startTime=start, endTime=end, limit=PAGE_SIZE))
    else:
        batch = _rows(client.order_history(symbol=symbol, startTime=start, endTime=end, limit=PAGE_SIZE))
    if any(from_ms(r.get("time")) is None or not start <= ms(from_ms(r["time"])) <= end for r in batch):
        raise CollectionIncomplete("Record outside requested interval")
    if symbol and any(r.get("symbol") != symbol for r in batch):
        raise CollectionIncomplete("Symbol mismatch")
    if len(batch) < PAGE_SIZE:
        return batch
    if start < end:
        middle = (start + end) // 2
        return (fetch_time_range(client, kind, start, middle, symbol=symbol, now=now)
                + fetch_time_range(client, kind, middle + 1, end, symbol=symbol, now=now))
    if kind == "trades":
        fetched = fetch_trades_at_saturated_ms(client, symbol, start, now)
        if not {str(r["id"]) for r in batch} <= {str(r["id"]) for r in fetched}:
            raise CollectionIncomplete("Saturated trade enumeration did not cover observed page")
        return fetched
    # Account-wide order IDs are not used as a global cross-symbol cursor.
    raise CollectionIncomplete("Saturated order timestamp cannot be proven complete")


def collect(client, signals, boundary, state, *, existing=None, now=None):
    now = utc(now) if now else client.server_now()
    if now is None or boundary > now:
        raise ValueError("Invalid collection interval")
    existing = existing or {"trades": [], "orders": [], "income": []}
    mark = state.get("endpoint_high_water", {})
    last = utc(mark.get("scanned_through_utc")) or boundary
    if last < now - RETENTION:
        raise CollectionIncomplete("Uncollected interval exceeds retention")
    # Timestamps mark scans, never accounting finality. A rotating historical
    # sweep revisits ALL retained prospective intervals, regardless of row status.
    floor = max(boundary, now - RETENTION)
    cursor = utc(mark.get("reconciliation_cursor_utc")) or floor
    cursor = max(cursor, floor)
    if cursor > now:
        cursor = floor
    historical_end = min(now, cursor + WINDOW - timedelta(milliseconds=1))
    ranges = list(_windows(ms(max(last, floor)), ms(now)))
    ranges += list(_windows(ms(cursor), ms(historical_end)))
    ranges = sorted(set(ranges))
    batch = {"trades": [], "orders": [], "income": []}
    for start, end in ranges:
        batch["income"].extend(fetch_income(client, start, end))
        batch["orders"].extend(fetch_time_range(client, "orders", start, end, now=now))
    symbols = {s.symbol for s in signals}
    for source in (existing["trades"], existing["orders"], existing["income"], batch["orders"], batch["income"]):
        symbols.update(r["symbol"] for r in source if r.get("symbol"))
    prior_symbols = set(mark.get("trade_scanned_through_utc", {}))
    symbols.update(prior_symbols)
    trade_marks = dict(mark.get("trade_scanned_through_utc", {}))
    for symbol in sorted(symbols):
        trade_ranges = ranges
        if symbol not in prior_symbols:
            if boundary < now - RETENTION:
                raise CollectionIncomplete("New symbol requires evidence outside retention")
            trade_ranges = sorted(set(ranges + list(_windows(ms(boundary), ms(now)))))
        for start, end in trade_ranges:
            batch["trades"].extend(fetch_time_range(client, "trades", start, end, symbol=symbol, now=now))
        trade_marks[symbol] = iso(now)
    # Discard only out-of-primary-boundary evidence, retaining sub-second precision.
    for kind in batch:
        batch[kind] = [r for r in batch[kind] if
                       (kind == "income" and from_ms(r.get("time")) is None)
                       or (from_ms(r.get("time")) is not None and boundary <= from_ms(r["time"]) <= now)]
    next_cursor = floor if historical_end >= now else historical_end + timedelta(milliseconds=1)
    next_state = {
        "collector_version": COLLECTOR_VERSION, "prospective_start_utc": iso(boundary),
        "last_successful_collection_utc": iso(now),
        "pending_income": dict(state.get("pending_income", {})),
        "endpoint_high_water": {
            "scanned_through_utc": iso(now), "reconciliation_cursor_utc": iso(next_cursor),
            "trade_scanned_through_utc": trade_marks,
        },
    }
    return batch, next_state



def reconcile_pending_income(client, records, state, boundary, now):
    """Every lifecycle retains its required interval; wall clock never deletes it.

    Re-read each unresolved interval every run, coalescing overlapping account-wide
    queries. A successful scan is observation coverage, never absence/finality.
    At the conservative local 89-day limit, persist an irreversible explicit gap.
    API failure aborts the transaction before any cursor or projection advances.
    """
    pending = {k: dict(v) for k, v in state.get("pending_income", {}).items()}
    for row in records:
        if not row.get("binance_trade_ids"):
            continue
        key = row["lifecycle_id"]
        start = utc(row["entry_fill_time_utc"])
        old = pending.get(key, {})
        prior = utc(old.get("required_start_utc"))
        pending[key] = {
            **old, "symbol": row["symbol"], "position_side": row["position_side"],
            "required_start_utc": iso(min(start, prior) if prior else start),
            "status": old.get("status", "PENDING"),
        }
    floor = max(boundary, now - RETENTION)
    intervals = []
    for req in pending.values():
        start = utc(req["required_start_utc"])
        if start is None:
            raise CollectionIncomplete("Malformed pending interval")
        if start < floor:
            req["status"] = "UNRECOVERABLE"
            req["reason"] = "RETENTION_COVERAGE_UNPROVEN"
            req["unrecoverable_before_utc"] = iso(floor)
        if max(start, floor) <= now:
            intervals.append((ms(max(start, floor)), ms(now)))
    # All upper bounds are now, so their account-wide union is one interval.
    income = []
    if intervals:
        for start, end in _windows(min(a for a, _ in intervals), ms(now)):
            income.extend(fetch_income(client, start, end))
    for req in pending.values():
        req["observed_from_utc"] = iso(max(utc(req["required_start_utc"]), floor))
        req["observed_through_utc"] = iso(now)
    state["pending_income"] = pending
    return income


def apply_income_coverage(records, state):
    for row in records:
        req = state.get("pending_income", {}).get(row["lifecycle_id"])
        if not req:
            continue
        row["income_reconciliation_start_utc"] = req["required_start_utc"]
        row["income_reconciliation_status"] = req["status"]
        if req["status"] == "UNRECOVERABLE":
            row["accounting_finality"] = "ACCOUNTING_INCOMPLETE"
            row["funding_finality"] = "FUNDING_UNRECOVERABLE"
            row["reconciliation_status"] = "ACCOUNTING_INCOMPLETE"
            if row.get("accounting_evidence_status") != "ACCOUNTING_EVIDENCE_INVALID":
                row["data_quality"] = "ACCOUNTING_INCOMPLETE"
            row["accounting_issues"] += "|RETENTION_COVERAGE_UNPROVEN"
    return records


def preserve_observation_times(records, output):
    if not output.exists():
        return records
    with output.open(encoding="utf-8-sig", newline="") as f:
        prior = {r.get("lifecycle_id"): r for r in csv.DictReader(f)}
    for r in records:
        old = prior.get(r["lifecycle_id"])
        if old and all(old.get(k, "") == r.get(k, "") for k in OUTPUT_FIELDS if k != "collected_at_utc"):
            r["collected_at_utc"] = old.get("collected_at_utc", "")
    return records


def capture_position_snapshots(client):
    """Bracket actual position reads; updateTime is NOT a boundary timestamp.

    Missing positionRisk rows do not establish zero. Only explicit signed
    position quantities are retained; full account payloads are never stored.
    """
    start = utc(client.server_now())
    rows = _rows(client.positions())
    end = utc(client.server_now())
    if start is None or end is None or end < start:
        raise CollectionIncomplete("Invalid position observation interval")
    return [{"symbol": row.get("symbol"), "positionSide": row.get("positionSide"),
             "positionAmt": row.get("positionAmt"), "observed_from_utc": iso(start),
             "observed_through_utc": iso(end)} for row in rows]


def run_collection(client, signals, boundary, output, state_path, *, dry_run=False,
                   historical=False, now=None, checkpoint=None, position_snapshots=()):
    """Caller holds collection_lock; checkpoint is fixture-only failure injection."""
    checkpoint = checkpoint or (lambda stage: None)
    ep = evidence_path(output)
    if output.exists() and not ep.exists():
        raise ValueError("CSV without durable evidence cannot be resumed")
    if state_path.exists() and not ep.exists():
        raise ValueError("State without durable evidence cannot be resumed")
    state = load_state(state_path, boundary)
    existing = load_evidence(ep, boundary)
    captured = capture_position_snapshots(client)
    observations = existing["position_snapshots"] + list(position_snapshots) + captured
    existing["position_snapshots"] = list({json.dumps(p, sort_keys=True): p for p in observations}.values())
    signals = retain_signal_evidence(existing, signals)
    batch, next_state = collect(client, signals, boundary, state, existing=existing, now=now)
    checkpoint("after_fetch")
    merged = merge_evidence(existing, batch)
    observed_now = utc(next_state["last_successful_collection_utc"])
    position_checkpoints(merged["position_snapshots"], boundary, observed_now, [])
    records = reconstruct_records(signals, merged["trades"], merged["income"], boundary,
                                  order_rows=merged["orders"], historical=historical,
                                  collected_at=observed_now, position_snapshots=merged["position_snapshots"])
    income = reconcile_pending_income(client, records, next_state, boundary, observed_now)
    merged = merge_evidence(merged, {"income": income})
    records = reconstruct_records(signals, merged["trades"], merged["income"], boundary,
                                  order_rows=merged["orders"], historical=historical,
                                  collected_at=observed_now, position_snapshots=merged["position_snapshots"])
    apply_income_coverage(records, next_state)
    preserve_observation_times(records, output)
    if dry_run:
        return records
    if historical:
        raise ValueError("Research results cannot be persisted")
    # Requirements derive from retained events on recovery, including a new retention gap.
    # Raw events make a crash at either following step replayable.
    _atomic_text(ep, json.dumps(merged, sort_keys=True, indent=2) + "\n")
    checkpoint("after_evidence")
    write_records(output, records)
    checkpoint("after_csv")
    atomic_write_state(state_path, next_state)
    checkpoint("after_state")
    return records


def report_records(rows, boundary="UNKNOWN"):
    if any(row.get("record_version") != "2" for row in rows):
        raise ValueError("Legacy report schema requires explicit rebootstrap")
    assert_unique_ownership(rows)
    primary = [dict(r) for r in rows if r.get("data_source") == "BINANCE_USDM_PROSPECTIVE"]
    for row in primary:
        risk = valid_initial_risk(row)
        for field, amount in (("gross_realized_r", "gross_realized_pnl_usdt"),
                              ("net_realized_r", "net_realized_pnl_usdt"),
                              ("execution_r", "execution_pnl_usdt")):
            value = decimal(row.get(amount))
            row[field] = fmt(value / risk if risk and value is not None else None)
    execution = [r for r in primary
                 if r.get("match_status") == "MATCHED" and r.get("canonical_signal_key")
                 and valid_provenance(r)
                 and valid_accounting_evidence(r)
                 and r.get("execution_completeness") == "EXECUTION_COMPLETE"
                 and r.get("position_terminal") == "POSITION_TERMINAL"
                 and r.get("execution_finality") == "EXECUTION_FINAL"
                 and r.get("realized_pnl_finality") == "REALIZED_PNL_FINAL"
                 and r.get("commission_finality") == "COMMISSION_FINAL"
                 and r.get("commission_completeness") == "COMPLETE"
                 and all(decimal(r.get(k)) is not None for k in
                         ("gross_realized_pnl_usdt", "commission_usdt", "execution_pnl_usdt"))
                 and decimal(r["execution_pnl_usdt"]) == decimal(r["gross_realized_pnl_usdt"]) + decimal(r["commission_usdt"])]
    funding_reconciled = [r for r in execution if r.get("funding_finality") == "FUNDING_FINAL"
                          and r.get("income_reconciliation_status") == "FINAL"
                          and decimal(r.get("funding_usdt")) is not None]
    for row in primary:
        row["funding_adjusted_pnl_usdt"] = fmt(
            decimal(row["execution_pnl_usdt"]) + decimal(row["funding_usdt"])
            if row in funding_reconciled else None)
    authoritative = [r for r in funding_reconciled if r.get("match_status") == "MATCHED"
                     and r.get("execution_completeness") == "EXECUTION_COMPLETE"
                     and r.get("cost_completeness") == "COST_COMPLETE"
                     and r.get("position_terminal") == "POSITION_TERMINAL"
                     and r.get("accounting_finalized") == "true"
                     and r.get("accounting_finality") == "ACCOUNTING_FINAL"
                     and r.get("reconciliation_status") == "ACCOUNTING_FINALIZED"
                     and all(decimal(r.get(k)) is not None for k in
                             ("gross_realized_pnl_usdt", "commission_usdt", "funding_usdt",
                              "other_execution_cost_usdt", "net_realized_pnl_usdt"))
                     and decimal(r["net_realized_pnl_usdt"]) == sum(decimal(r[k]) for k in
                         ("gross_realized_pnl_usdt", "commission_usdt", "funding_usdt", "other_execution_cost_usdt"))]
    invalid = [r for r in primary if invalid_accounting_evidence(r)]
    pending = [r for r in primary if r not in authoritative and r not in invalid
               and r.get("match_status") in {"MATCHED", "PARTIAL"}]
    def totals(population):
        result = {"rows": len(population)}
        for name in ("gross_realized_pnl_usdt", "commission_usdt", "funding_usdt",
                     "other_execution_cost_usdt", "net_realized_pnl_usdt", "gross_realized_r", "net_realized_r",
                     "execution_pnl_usdt", "execution_r", "provisional_net_realized_pnl_usdt", "funding_adjusted_pnl_usdt"):
            vals = [decimal(r.get(name)) for r in population]
            result[name] = fmt(complete_sum(vals)) or None
            result[name + "_known_rows"] = sum(v is not None for v in vals)
        return result
    ids = {(r["symbol"], tid) for r in primary for tid in r.get("binance_trade_ids", "").split("|") if tid}
    return {
        "prospective_start": boundary, "observed_binance_fills": len(ids),
        "matched_scanner_lifecycles": sum(r.get("match_status") == "MATCHED" for r in primary),
        "partial": sum(r.get("match_status") == "PARTIAL" for r in primary),
        "ambiguous": sum(r.get("match_status") == "AMBIGUOUS" for r in primary),
        "unmatched": sum(r.get("match_status") == "UNMATCHED" for r in primary),
        "execution_complete_matched": totals(execution),
        "pending_execution_accounting": totals([r for r in pending if r not in execution]),
        "invalid_accounting_evidence": {"rows": len(invalid)},
        "funding_pending": {"rows": sum(r.get("funding_finality") in {"FUNDING_PENDING", "FUNDING_UNRECOVERABLE"} for r in primary)},
        "funding_reconciled_matched": totals(funding_reconciled),
        "incomplete_unrecoverable": sum(r.get("accounting_finality") == "ACCOUNTING_INCOMPLETE" for r in primary),
        "complete_cost_matched": totals(authoritative),
        "provisional_pending": totals(pending),
        "linkage_coverage_pct": round(100 * len({(r["symbol"], t) for r in primary
                                               if r.get("match_status") == "MATCHED"
                                               for t in r.get("binance_trade_ids", "").split("|") if t}) / len(ids), 2) if ids else None,
    }


def report(path, boundary="UNKNOWN", *, as_json=False):
    rows = []
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    if boundary == "UNKNOWN" and rows:
        boundaries = {r.get("prospective_start_utc") for r in rows}
        if len(boundaries) == 1:
            boundary = boundaries.pop()
    data = report_records(rows, boundary)
    if as_json:
        return json.dumps(data, sort_keys=True)
    lines = ["Binance Execution Truth V1", "==========================",
             f"Prospective start: {boundary}", f"Observed Binance fills: {data['observed_binance_fills']}",
             f"Matched scanner lifecycles: {data['matched_scanner_lifecycles']}",
             f"Partial: {data['partial']}", f"Ambiguous: {data['ambiguous']}", f"Unmatched: {data['unmatched']}"]
    lines += ["", "AUTHORITATIVE EXECUTION PNL (EXCLUDES FUNDING AND OTHER ADJUSTMENTS)",
              f"Rows: {data['execution_complete_matched']['rows']}",
              "Gross realized PnL + signed execution commissions: " + (data["execution_complete_matched"]["execution_pnl_usdt"] or "UNKNOWN"),
              "Execution R: " + (data["execution_complete_matched"]["execution_r"] or "UNKNOWN"),
              f"Pending Execution Accounting: {data['pending_execution_accounting']['rows']}",
              f"Invalid Accounting Evidence / REQUIRES_RECONCILIATION: {data['invalid_accounting_evidence']['rows']}",
              f"Funding Pending: {data['funding_pending']['rows']}",
              f"Funding-reconciled rows: {data['funding_reconciled_matched']['rows']}",
              "Authoritative execution + funding (excludes other adjustments): " + (data["funding_reconciled_matched"]["funding_adjusted_pnl_usdt"] or "UNKNOWN"),
              "PROVISIONAL execution + observed funding (excludes other adjustments): " + (data["provisional_pending"]["provisional_net_realized_pnl_usdt"] or "UNKNOWN"),
              f"Accounting incomplete/unrecoverable: {data['incomplete_unrecoverable']}"]
    for title, key in [("ALL-IN ACCOUNTING FINAL POPULATION", "complete_cost_matched"),
                       ("PROVISIONAL / RECONCILIATION PENDING", "provisional_pending")]:
        pop = data[key]
        lines += ["", title, f"Rows: {pop['rows']}"]
        for name, label in [("gross_realized_pnl_usdt", "Gross realized PnL"),
                            ("commission_usdt", "Commission"), ("funding_usdt", "Funding"),
                            ("net_realized_pnl_usdt", "Net realized PnL"),
                            ("gross_realized_r", "Gross realized R"), ("net_realized_r", "Net realized R")]:
            value = pop[name] if pop[name] is not None else "UNKNOWN"
            lines.append(f"{label}: {value} ({pop[name + '_known_rows']}/{pop['rows']} known)")
    lines += ["", f"Fill linkage coverage: {data['linkage_coverage_pct']}",
              "Pending amounts are provisional. No project or economic profitability claim."]
    return "\n".join(lines)


def _validate_paths(signals, credentials, output, state):
    protected = {signals.resolve(), credentials.resolve(), Path(".env").resolve(), DEFAULT_SIGNALS.resolve()}
    writable = [output, state, evidence_path(output), output.with_suffix(".lock")]
    resolved = [p.resolve() for p in writable]
    if len(set(resolved)) != len(resolved) or any(p in protected for p in resolved):
        raise ValueError("Collector paths overlap protected inputs")
    for p in writable:
        if p.is_symlink() or p.suffix.lower() not in {".csv", ".json", ".lock"}:
            raise ValueError("Invalid collector output path")
        if p.exists() and any(q.exists() and os.path.samefile(p, q) for q in (signals, credentials, Path(".env"))):
            raise ValueError("Output aliases a protected input")
    if output.suffix.lower() != ".csv" or state.suffix.lower() != ".json":
        raise ValueError("Unexpected output extensions")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Standalone GET-only prospective execution evidence collector")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run", action="store_true")
    mode.add_argument("--report", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Signed reads only; no output/state writes")
    parser.add_argument("--historical", action="store_true", help="Research-only; requires --dry-run")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--signals", type=Path, default=DEFAULT_SIGNALS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS)
    parser.add_argument("--start-utc", default=os.getenv("BINANCE_EXECUTION_TRUTH_START_UTC"))
    args = parser.parse_args(argv)
    try:
        if args.historical and not args.dry_run:
            raise ValueError("Historical mode requires dry-run")
        if args.report:
            print(report(args.output, args.start_utc or "UNKNOWN", as_json=args.json))
            return 0
        boundary = _boundary(args.start_utc)
        _validate_paths(args.signals, args.credentials, args.output, args.state)
        before = hashlib.sha256(args.signals.read_bytes()).digest()
        signals = load_sent_signals(args.signals, boundary, historical=args.historical)
        key, secret = _load_credentials(args.credentials)
        client = BinanceExecutionTruthClient(key, secret)
        client.sync_server_time()
        if args.dry_run:
            records = run_collection(client, signals, boundary, args.output, args.state, dry_run=True, historical=args.historical)
        else:
            with collection_lock(args.output.with_suffix(".lock")):
                def unchanged(_stage):
                    if hashlib.sha256(args.signals.read_bytes()).digest() != before:
                        raise ValueError("Signal journal changed during collection")
                records = run_collection(client, signals, boundary, args.output, args.state, checkpoint=unchanged)
        if args.dry_run:
            print(json.dumps(report_records(records, iso(boundary)), sort_keys=True))
        else:
            print(report(args.output, iso(boundary), as_json=args.json))
        return 0
    except BinanceReadOnlyError as exc:
        print(str(exc), file=sys.stderr)
    except Exception:
        print("Collector failed safely: " + redact_error(None), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
