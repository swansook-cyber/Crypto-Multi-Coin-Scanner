# -*- coding: utf-8 -*-
"""Pure lifecycle reconstruction and conservative accounting; no I/O.

Amounts use signed cash flows: net = realized PnL + commission + funding
+ other costs. Per-fill commission is negative Binance's charged commission;
income commission retains its sign. Non-USDT assets are preserved, never
converted. Missing components are unknown, not zero. Position closure does
not prove funding/other-cost finality, so V1 cannot automatically finalize
all-in accounting. Reconciled terminal fills can emit authoritative execution
PnL (gross plus commissions), explicitly excluding funding and other adjustments. A 48-hour candidate horizon
is not proof: temporal competitors survive price filtering.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Any, Mapping, Sequence

from core.signal_identity import identity_from_record, normalize_side, normalize_symbol

MATCH_WINDOW = timedelta(hours=48)  # Candidate search horizon, never proof of ownership.
ENTRY_PROXIMITY = Decimal("0.02")
ZERO = Decimal(0)
AUTHORITATIVE_PROVENANCE = {"POST_BOUNDARY_AUTHORITATIVE", "POST_BOUNDARY_AFTER_FLAT_RESET"}


def invalid_accounting_evidence(row):
    issues = str(row.get("accounting_issues", "")).split("|")
    return row.get("accounting_evidence_status") == "ACCOUNTING_EVIDENCE_INVALID" or any(
        issue.startswith(("INVALID_", "CONTRADICTORY_", "CONFLICTING_"))
        or issue in {"REALIZED_PNL_DISAGREEMENT", "COMMISSION_DISAGREEMENT"} for issue in issues)


def valid_accounting_evidence(row):
    return row.get("accounting_evidence_status") == "ACCOUNTING_EVIDENCE_VALID" and not invalid_accounting_evidence(row)


OUTPUT_FIELDS = [
    "record_version", "lifecycle_id", "canonical_signal_key", "candidate_signal_keys",
    "symbol", "side", "position_side", "signal_timestamp_utc", "signal_entry",
    "signal_sl", "signal_tp1", "signal_tp2", "binance_trade_id", "binance_order_id",
    "binance_trade_ids", "binance_order_ids", "entry_fill_time_utc", "entry_fill_count",
    "entry_fill_price", "entry_fill_qty", "exit_fill_count", "exit_fill_qty", "exit_vwap",
    "position_close_time_utc", "remaining_qty", "gross_realized_pnl_usdt",
    "commission_usdt", "commission_by_asset", "funding_usdt", "other_execution_cost_usdt",
    "net_realized_pnl_usdt", "provisional_net_realized_pnl_usdt", "initial_risk_usdt",
    "gross_realized_r", "net_realized_r", "maker_taker_if_available",
    "partial_tp_detected", "stop_fill_detected", "match_status", "data_quality",
    "execution_completeness", "commission_completeness", "cost_completeness",
    "position_terminal", "accounting_finalized", "reconciliation_status",
    "accounting_issues", "data_source", "prospective_start_utc", "collected_at_utc",
    "SCANNER_ATTRIBUTED_REALIZED_PNL", "REALIZED_EXECUTION_COST", "REALIZED_NET_PNL",
    "REALIZED_NET_R", "MONTHLY_INFRA_BREAK_EVEN",
    "execution_finality", "realized_pnl_finality", "commission_finality",
    "funding_finality", "accounting_finality", "execution_pnl_usdt", "execution_r",
    "income_reconciliation_start_utc", "income_reconciliation_status", "funding_adjusted_pnl_usdt",
    "boundary_exposure_state", "lifecycle_provenance", "flat_provenance_utc",
    "accounting_evidence_status",
]


def decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def fmt(value: Decimal | None) -> str:
    if value is None:
        return ""
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def utc(value: Any) -> datetime | None:
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None or dt.utcoffset() is None:
            return None
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def iso(value: datetime | None) -> str:
    if value is None:
        return ""
    parsed = utc(value)
    if parsed is None:
        raise ValueError("Timezone-aware timestamp required")
    return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")


def ms(value: datetime) -> int:
    dt = utc(value)
    if dt is None:
        raise ValueError("Timezone-aware timestamp required")
    delta = dt - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def from_ms(value: Any) -> datetime | None:
    try:
        if type(value) is not int and not (isinstance(value, str) and re.fullmatch(r"-?\d+", value)):
            return None
        return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=int(value))
    except (ValueError, TypeError, OverflowError):
        return None


def complete_sum(values) -> Decimal | None:
    items = list(values)
    return sum(items, ZERO) if items and all(v is not None for v in items) else None


def boolean(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in ("true", "True"):
        return True
    if value in ("false", "False"):
        return False
    return None


@dataclass(frozen=True)
class Signal:
    key: str
    symbol: str
    side: str
    timestamp: datetime
    entry: Decimal | None
    stop: Decimal | None
    tp1: Decimal | None
    tp2: Decimal | None
    order_ids: frozenset[str] = frozenset()
    trade_ids: frozenset[str] = frozenset()


def signals_from_mappings(rows, boundary, historical=False):
    selected = {}
    for row in rows:
        if str(row.get("signal_status", "")).strip().lower() != "sent":
            continue
        stamp = utc(row.get("timestamp") or row.get("signal_timestamp") or row.get("final_signal_timestamp"))
        if stamp is None or (stamp < boundary and not historical):
            continue
        key = identity_from_record(row).canonical_key
        symbol = normalize_symbol(row.get("symbol"))
        side = normalize_side(row.get("side") or row.get("direction"))
        if not key or not symbol or side not in {"LONG", "SHORT"}:
            continue
        refs = str(row.get("binance_order_ids") or row.get("binance_order_id") or "")
        item = Signal(key, symbol, side, stamp, decimal(row.get("entry") or row.get("entry_low")),
                      decimal(row.get("stop_loss") or row.get("sl")), decimal(row.get("tp1")),
                      decimal(row.get("tp2")), frozenset(refs.split("|")) - {""},
                      frozenset(str(row.get("binance_trade_ids") or row.get("binance_trade_id") or "").split("|")) - {""})
        if key in selected and selected[key] != item:
            raise ValueError("Conflicting canonical signal identity")
        selected[key] = item
    return sorted(selected.values(), key=lambda s: (s.timestamp, s.key))


@dataclass(frozen=True)
class Fill:
    symbol: str
    side: str
    time: datetime
    price: Decimal
    qty: Decimal
    trade_id: str
    order_id: str
    realized_pnl: Decimal | None
    commission: Decimal | None
    commission_asset: str
    margin_asset: str
    position_side: str
    reduce_only: bool | None
    maker: bool | None
    order_type: str
    valid_semantics: bool = True


def normalize_fills(rows, boundary, historical=False, orders=()):
    order_map = {(normalize_symbol(r.get("symbol")), str(r.get("orderId"))): r for r in orders}
    unique = {}
    for row in rows:
        stamp, price, qty = from_ms(row.get("time")), decimal(row.get("price")), decimal(row.get("qty"))
        symbol, tid = normalize_symbol(row.get("symbol")), str(row.get("id", ""))
        if stamp is None or price is None or qty is None or price <= 0 or qty <= 0 or not tid.isdigit() or not symbol:
            raise ValueError("Invalid execution evidence")
        if stamp < boundary and not historical:
            continue
        oid = str(row.get("orderId", ""))
        order = order_map.get((symbol, oid), {})
        side = str(row.get("side", "")).upper()
        pos = str(row.get("positionSide") or order.get("positionSide") or "").upper()
        buyer = boolean(row.get("buyer"))
        valid = side in {"BUY", "SELL"} and pos in {"LONG", "SHORT", "BOTH"}
        valid &= buyer is None or buyer == (side == "BUY")
        valid &= not order.get("side") or order["side"] == side
        valid &= not order.get("positionSide") or order["positionSide"] == pos
        reduce_only = boolean(order.get("reduceOnly", row.get("reduceOnly")))
        item = Fill(symbol, side, stamp, price, qty, tid, oid, decimal(row.get("realizedPnl")),
                    decimal(row.get("commission")), str(row.get("commissionAsset", "")).upper(),
                    str(row.get("marginAsset", "")).upper(), pos, reduce_only,
                    boolean(row.get("maker")), str(order.get("origType") or order.get("type") or ""), valid)
        key = (symbol, tid)
        if key in unique and unique[key] != item:
            raise ValueError("Conflicting duplicate trade ID")
        unique[key] = item
    return sorted(unique.values(), key=lambda f: (f.time, f.symbol, int(f.trade_id)))


@dataclass
class Lifecycle:
    symbol: str
    position_side: str
    direction: str
    fills: list[Fill] = field(default_factory=list)
    entries: list[Fill] = field(default_factory=list)
    exits: list[Fill] = field(default_factory=list)
    qty: Decimal = ZERO
    close_time: datetime | None = None
    issues: set[str] = field(default_factory=set)
    orphan: bool = False
    boundary_state: str = "BOUNDARY_EXPOSURE_UNKNOWN"
    provenance: str = "BOUNDARY_EXPOSURE_UNKNOWN"
    flat_provenance: datetime | None = None

    @property
    def key(self):
        return f"life:2:{self.symbol}:{self.position_side}:{self.fills[0].trade_id}"

    @property
    def start(self):
        return self.fills[0].time

    def exposed(self, when):
        # A fill and a funding event with the same timestamp have unknown ordering.
        if when <= self.start or (self.close_time and when >= self.close_time):
            return False
        return not any(f.time == when for f in self.fills)


def position_checkpoints(rows, boundary, collected, fills):
    """Explicit observed rows only; absence is never a flat position snapshot.

    A read is an interval, not an atomic timestamp. If any fill overlaps its
    millisecond-rounded interval, it cannot anchor inventory. Late evidence
    therefore revokes an earlier ambiguous anchor on replay.
    """
    indexed = {}
    for row in rows:
        start, end = utc(row.get("observed_from_utc")), utc(row.get("observed_through_utc"))
        qty = decimal(row.get("positionAmt"))
        symbol, side = normalize_symbol(row.get("symbol")), row.get("positionSide")
        if (start is None or end is None or not boundary <= start <= end <= collected
                or qty is None or not symbol or side not in {"LONG", "SHORT", "BOTH"}
                or (side == "LONG" and qty < ZERO) or (side == "SHORT" and qty > ZERO)):
            raise ValueError("Invalid position checkpoint")
        key = (symbol, side, start, end)
        if key in indexed and indexed[key] != qty:
            raise ValueError("Conflicting position checkpoint")
        indexed[key] = qty
    result = []
    for (symbol, side, start, end), qty in indexed.items():
        if any(f.symbol == symbol and f.position_side == side
               and ms(start) <= ms(f.time) <= ms(end) for f in fills):
            continue
        result.append((end, symbol, side, qty, start == end == boundary))
    # Disagreeing overlapping reads have no unique inventory anchor.
    for i, (end, symbol, side, qty, _) in enumerate(result):
        for other in result[i+1:]:
            if other[:3] == (end, symbol, side) and other[3] != qty:
                raise ValueError("Conflicting simultaneous position observations")
    return result


def segment_lifecycles(fills, boundary=None, position_snapshots=(), collected=None):
    active, result, uncertain_groups = {}, [], set()
    inventory, baseline, flat_at, last_anchor = {}, {}, {}, {}
    checkpoints = position_checkpoints(position_snapshots, boundary, collected, fills) if position_snapshots else []
    events = [(f.time, 1, int(f.trade_id), f) for f in fills]
    events += [(p[0], 0, index, p) for index, p in enumerate(checkpoints)]
    for _, kind, _, event in sorted(events, key=lambda e: e[:3]):
        if kind == 0:
            when, symbol, side, qty, at_boundary = event
            key = (symbol, side)
            if at_boundary:
                baseline[key] = "BOUNDARY_FLAT_CONFIRMED" if qty == ZERO else "BOUNDARY_PREEXISTING_POSITION"
            life = active.get(key)
            previous = inventory.get(key)
            if previous is not None and previous != qty:
                # A later snapshot contradicts replay since the prior anchor.
                # Revoke even already-closed projections from that interval.
                for prior_life in result:
                    if ((prior_life.symbol, prior_life.position_side) == key and prior_life.fills
                            and (not prior_life.close_time or prior_life.close_time >= last_anchor[key])):
                        prior_life.issues.add("POSITION_SNAPSHOT_MISMATCH")
            if life and (previous is None or previous != qty):
                life.issues.add("POSITION_SNAPSHOT_RESET")
                active.pop(key)
            inventory[key] = qty
            last_anchor[key] = when
            if qty == ZERO:
                flat_at[key] = when
                uncertain_groups.discard(key)
            elif previous != qty:
                flat_at.pop(key, None)
            continue
        f = event
        key = (f.symbol, f.position_side)
        life = active.get(key)
        before = inventory.get(key)
        if not f.valid_semantics:
            orphan = Lifecycle(f.symbol, f.position_side, "", [f], issues={"POSITION_SEMANTICS_UNKNOWN"}, orphan=True)
            result.append(orphan)
            if life:
                life.issues.add("POSITION_SEQUENCE_INCOMPLETE")
            uncertain_groups.add(key)
            inventory[key] = None
            flat_at.pop(key, None)
            continue
        if life is None and before is not None and before != ZERO:
            # Snapshot-established inventory belongs to a legacy/unknown span,
            # not a fabricated post-boundary entry. Only its later zero resets.
            life = Lifecycle(f.symbol, f.position_side, "LONG" if before > ZERO else "SHORT",
                             qty=abs(before), boundary_state=baseline.get(key, "BOUNDARY_EXPOSURE_UNKNOWN"),
                             provenance="PRE_BOUNDARY" if baseline.get(key) == "BOUNDARY_PREEXISTING_POSITION"
                             else "BOUNDARY_EXPOSURE_UNKNOWN")
            active[key] = life
            result.append(life)
        entry_side = "BUY" if f.position_side == "LONG" else "SELL" if f.position_side == "SHORT" else None
        if life is not None:
            entry_side = "BUY" if life.direction == "LONG" else "SELL"
        if f.position_side == "BOTH" and life is None:
            # One-way opening requires explicit order evidence and no realized close.
            opening = f.reduce_only is False and f.realized_pnl == ZERO
        else:
            opening = f.side == entry_side and f.reduce_only is not True and f.realized_pnl == ZERO
        if life is None:
            if not opening:
                result.append(Lifecycle(f.symbol, f.position_side, "", [f],
                                        issues={"ENTRY_OR_BASELINE_MISSING"}, orphan=True))
                uncertain_groups.add(key)
                inventory[key] = None
                flat_at.pop(key, None)
                continue
            direction = "LONG" if f.side == "BUY" else "SHORT"
            life = Lifecycle(f.symbol, f.position_side, direction)
            life.boundary_state = baseline.get(key, "BOUNDARY_EXPOSURE_UNKNOWN")
            reset = flat_at.get(key)
            if before == ZERO and reset is not None and f.time > reset:
                life.flat_provenance = reset
                life.provenance = ("POST_BOUNDARY_AUTHORITATIVE" if reset == boundary
                                   else "POST_BOUNDARY_AFTER_FLAT_RESET")
            if key in uncertain_groups:
                life.issues.add("POSITION_SEQUENCE_INCOMPLETE")
            active[key] = life
            result.append(life)
        if opening:
            life.fills.append(f)
            life.entries.append(f)
            if life.exits:
                life.issues.add("SCALE_IN_AFTER_REDUCTION_R_UNSAFE")
            life.qty += f.qty
            if before is not None:
                inventory[key] = before + (f.qty if f.side == "BUY" else -f.qty)
            continue
        closing_evidence = (f.position_side in {"LONG", "SHORT"} or f.reduce_only is True
                            or (f.realized_pnl is not None and f.realized_pnl != ZERO))
        if f.side == entry_side or not closing_evidence or f.qty > life.qty:
            # Reversals cannot be split without assigning one trade ID to two owners.
            life.issues.add("POSITION_SEQUENCE_INCOMPLETE")
            uncertain_groups.add(key)
            inventory[key] = None
            flat_at.pop(key, None)
            if not life.fills:
                result.remove(life)
                active.pop(key, None)
            result.append(Lifecycle(f.symbol, f.position_side, "", [f],
                                    issues={"REVERSAL_OR_UNPROVEN_REDUCTION"}, orphan=True))
            continue
        life.fills.append(f)
        life.exits.append(f)
        life.qty -= f.qty
        if before is not None:
            inventory[key] = before + (f.qty if f.side == "BUY" else -f.qty)
        if life.qty == ZERO:
            life.close_time = f.time
            active.pop(key)
            if inventory.get(key) == ZERO:
                flat_at[key] = f.time
                uncertain_groups.discard(key)
    return result


def vwap(fills):
    qty = sum((f.qty for f in fills), ZERO)
    return sum((f.price * f.qty for f in fills), ZERO) / qty if qty else None


def _ownership(life, signals, consumed_signals):
    # Symbol, direction, temporal eligibility and explicit references are constraints.
    # Missing evidence cannot satisfy a supplied constraint; contradiction cannot
    # be repaired by price. Only a unique hard link may override price proximity.
    temporal = [s for s in signals if s.symbol == life.symbol and s.side == life.direction
                and s.key not in consumed_signals and (
                    ((s.order_ids or s.trade_ids) and s.timestamp <= life.start)
                    or any(s.timestamp <= f.time <= s.timestamp + MATCH_WINDOW for f in life.entries))]
    refs = {f.order_id for f in life.entries if f.order_id and f.order_id.isdigit()}
    tids = {f.trade_id for f in life.entries}
    candidates = [s for s in temporal
                  if not (s.order_ids and refs - s.order_ids)
                  and not (s.trade_ids and tids - s.trade_ids)]
    def hard_link(s):
        return bool(s.order_ids or s.trade_ids) and (
            not s.order_ids or (len(refs) > 0 and all(f.order_id in s.order_ids for f in life.entries))
        ) and (not s.trade_ids or tids <= s.trade_ids)
    exact = [s for s in candidates if s.timestamp <= life.start and hard_link(s)]
    if len(exact) == 1:
        status = "PARTIAL" if life.issues else "MATCHED"
        return exact[0], status, candidates
    if len(candidates) > 1:
        return None, "AMBIGUOUS", candidates
    if not candidates:
        return None, "UNMATCHED", []
    s = candidates[0]
    if s.timestamp > life.start or (s.order_ids and not hard_link(s)):
        return s, "PARTIAL", candidates
    if s.entry is None or s.entry <= 0:
        return s, "PARTIAL", candidates
    if abs(life.entries[0].price - s.entry) / s.entry > ENTRY_PROXIMITY:
        return None, "UNMATCHED", candidates
    if life.issues:
        return s, "PARTIAL", candidates
    return s, "MATCHED", candidates


def valid_initial_risk(row):
    """Recompute risk from owned entry evidence; never trust serialized R."""
    qty, price, stop = (decimal(row.get(k)) for k in
                        ("entry_fill_qty", "entry_fill_price", "signal_sl"))
    stored = decimal(row.get("initial_risk_usdt"))
    if (not valid_provenance(row)
            or not valid_accounting_evidence(row)):
        return None
    if row.get("match_status") != "MATCHED" or not row.get("canonical_signal_key"):
        return None
    if row.get("execution_completeness") != "EXECUTION_COMPLETE":
        return None
    if row.get("position_terminal") != "POSITION_TERMINAL" or decimal(row.get("remaining_qty")) != ZERO:
        return None
    if decimal(row.get("exit_fill_qty")) != qty:
        return None
    if any(v is None or v <= 0 for v in (qty, price, stop, stored)):
        return None
    side = row.get("side")
    if not ((side == "LONG" and stop < price) or (side == "SHORT" and stop > price)):
        return None
    expected = abs(price - stop) * qty
    return expected if expected.is_finite() and expected > 0 and expected == stored else None


def valid_provenance(row):
    boundary, reset, entry = (utc(row.get(k)) for k in
                              ("prospective_start_utc", "flat_provenance_utc", "entry_fill_time_utc"))
    if boundary is None or reset is None or entry is None or not boundary <= reset < entry:
        return False
    state = row.get("boundary_exposure_state")
    if row.get("lifecycle_provenance") == "POST_BOUNDARY_AUTHORITATIVE":
        return state == "BOUNDARY_FLAT_CONFIRMED" and reset == boundary
    return (row.get("lifecycle_provenance") == "POST_BOUNDARY_AFTER_FLAT_RESET" and reset > boundary
            and state in {"BOUNDARY_FLAT_CONFIRMED", "BOUNDARY_PREEXISTING_POSITION", "BOUNDARY_EXPOSURE_UNKNOWN"})


def _dedupe_income(rows):
    groups = {}
    for row in rows:
        key = (str(row.get("incomeType")), str(row.get("tranId", "")))
        groups.setdefault(key, {})[json.dumps(row, sort_keys=True)] = row
    return [{**row, "_income_conflict": len(variants) > 1}
            for variants in groups.values() for row in variants.values()]


def _income_link(row, life, lives):
    """Return related/contradictory without discarding malformed linked evidence.

    Numeric IDs are symbol-scoped. A complete identity belonging to another
    observed symbol/lifecycle proves nonownership; a partial contradictory link
    does not. No-ref funding is linked by exposure, not by guessed trade IDs.
    """
    symbol = normalize_symbol(row.get("symbol"))
    tid, oid = str(row.get("tradeId") or ""), str(row.get("orderId") or "")
    own = [f for f in life.fills if (tid and f.trade_id == tid) or (oid and f.order_id == oid)]
    exact_elsewhere = [f for item in lives if item is not life for f in item.fills
                       if f.symbol == symbol and (tid or oid)
                       and (not tid or f.trade_id == tid) and (not oid or f.order_id == oid)]
    if exact_elsewhere and not (symbol == life.symbol and own):
        return False, False
    if own:
        contradiction = symbol != life.symbol or bool(row.get("positionSide") and row["positionSide"] != life.position_side)
        contradiction |= not any((not tid or f.trade_id == tid) and (not oid or f.order_id == oid) for f in own)
        return True, contradiction
    if symbol != life.symbol:
        return False, False
    if row.get("positionSide") in {"LONG", "SHORT", "BOTH"} and row["positionSide"] != life.position_side:
        return False, False
    if tid or oid:
        return False, False  # Both supplied references exclude every owned fill.
    stamp = from_ms(row.get("time"))
    if stamp is not None and (stamp < life.start or (life.close_time and stamp > life.close_time)):
        return False, False
    return True, bool(row.get("positionSide") and row["positionSide"] != life.position_side)


def _account(life, lives, incomes, boundary, collected, order_map):
    issues = set(life.issues)
    eligible = []
    invalid = False
    incomplete = False
    for r in incomes:
        related, contradiction = _income_link(r, life, lives)
        if not related:
            continue
        stamp = from_ms(r.get("time"))
        bad = set()
        if contradiction:
            bad.add("CONTRADICTORY_INCOME_LINK")
        if stamp is None:
            bad.add("INVALID_INCOME_TIMESTAMP")
        if decimal(r.get("income")) is None:
            bad.add("INVALID_INCOME_VALUE")
        if not re.fullmatch(r"[A-Z][A-Z0-9]{0,19}", str(r.get("asset", ""))):
            bad.add("INVALID_INCOME_ASSET")
        if not str(r.get("tranId", "")).isdigit():
            bad.add("INVALID_INCOME_IDENTITY")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", str(r.get("incomeType", ""))):
            bad.add("INVALID_INCOME_TYPE")
        if r.get("incomeType") == "REALIZED_PNL":
            settlements = {f.margin_asset for f in life.fills if f.trade_id == str(r.get("tradeId")) and f.margin_asset}
            if settlements and r.get("asset") not in settlements:
                bad.add("INVALID_INCOME_SETTLEMENT_ASSET")
        if r.get("_income_conflict"):
            bad.add("CONFLICTING_INCOME_TRANSACTION")
        if bad:
            issues.update(bad)
            invalid = True
            continue
        if not boundary <= stamp <= collected:
            continue
        # Event timestamp, not arrival time, is used. Costs posted after close
        # can reconcile by exact trade ID up to this successful observation time.
        if stamp < life.start:
            continue
        if r.get("incomeType") in {"REALIZED_PNL", "COMMISSION"} and not r.get("tradeId"):
            incomplete = True
            issues.add("INCOME_TRADE_LINK_INCOMPLETE")
        eligible.append(r)
    pnl_parts, fees, assets, fee_complete = [], [], {}, True
    for f in life.fills:
        per_fill = [r for r in eligible if str(r.get("tradeId", "")) == f.trade_id
                    and from_ms(r["time"]) >= f.time
                    and (not r.get("orderId") or str(r["orderId"]) == f.order_id)]
        realized = [r for r in per_fill if r.get("incomeType") == "REALIZED_PNL" and r.get("asset") == "USDT"]
        value = complete_sum(decimal(r["income"]) for r in realized) if realized else f.realized_pnl if f.margin_asset == "USDT" else None
        if realized and f.margin_asset == "USDT" and f.realized_pnl is not None and value != f.realized_pnl:
            issues.add("REALIZED_PNL_DISAGREEMENT")
            invalid = True
        pnl_parts.append(value)
        charged = [r for r in per_fill if r.get("incomeType") == "COMMISSION"]
        if charged:
            part_assets = {}
            for r in charged:
                asset, val = str(r.get("asset", "")), decimal(r.get("income"))
                part_assets[asset] = part_assets.get(asset, ZERO) + val
            if f.commission is not None and (set(part_assets) != {f.commission_asset}
                                            or part_assets.get(f.commission_asset) != -f.commission):
                issues.add("COMMISSION_DISAGREEMENT")
                fee_complete = False
                invalid = True
        elif f.commission is not None and f.commission_asset:
            part_assets = {f.commission_asset: -f.commission}
        else:
            part_assets = {}
            fee_complete = False
        for asset, val in part_assets.items():
            assets[asset] = assets.get(asset, ZERO) + val
        if set(part_assets) != {"USDT"}:
            fees.append(None)
            fee_complete = False
        else:
            fees.append(part_assets["USDT"])
    funding_parts = []
    funding_ids = []
    owned_ids = {f.trade_id for f in life.fills}
    owned_orders = {f.order_id for f in life.fills}
    for r in eligible:
        if r.get("incomeType") != "FUNDING_FEE":
            continue
        stamp = from_ms(r["time"])
        if r.get("tradeId") and str(r["tradeId"]) not in owned_ids:
            continue
        if r.get("orderId") and str(r["orderId"]) not in owned_orders:
            continue
        pos = str(r.get("positionSide") or "")
        if pos and pos != life.position_side:
            continue
        holders = [item for item in lives if item.symbol == life.symbol and not item.orphan
                   and item.exposed(stamp) and (not pos or item.position_side == pos)]
        unknown_exposure = any(item.symbol == life.symbol and item.orphan and item.start <= stamp for item in lives)
        if life.exposed(stamp) and len(holders) == 1 and not life.issues and not unknown_exposure:
            if r.get("asset") == "USDT":
                funding_parts.append(decimal(r["income"]))
                funding_ids.append(str(r["tranId"]))
            else:
                issues.add("NON_USDT_FUNDING")
        elif life.exposed(stamp) and len(holders) > 1:
            issues.add("FUNDING_OWNERSHIP_AMBIGUOUS")
    # A GET page/clock watermark does not certify no future late postings.
    # No zero funding, arbitrary grace time or automatic finality is invented.
    gross, commission = complete_sum(pnl_parts), complete_sum(fees)
    funding = complete_sum(funding_parts)
    if gross is None:
        issues.add("REALIZED_PNL_UNKNOWN")
    if not fee_complete:
        issues.add("COMMISSION_INCOMPLETE_OR_UNCONVERTED")
    issues.add("FUNDING_FINALITY_UNPROVEN")
    issues.add("OTHER_COST_FINALITY_UNPROVEN")
    order_sums = {}
    for f in life.fills:
        order_sums[f.order_id] = order_sums.get(f.order_id, ZERO) + f.qty
    orders_complete = bool(order_sums) and all(
        (life.symbol, oid) in order_map
        and order_map[(life.symbol, oid)].get("status") in {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"}
        and decimal(order_map[(life.symbol, oid)].get("executedQty")) == qty
        for oid, qty in order_sums.items()
    )
    execution_complete = orders_complete and not life.orphan and not life.issues
    evidence_status = ("ACCOUNTING_EVIDENCE_INVALID" if invalid else
                       "ACCOUNTING_EVIDENCE_INCOMPLETE" if incomplete or gross is None or not fee_complete
                       or any(f.realized_pnl is None or f.commission is None or not f.margin_asset for f in life.fills) else
                       "ACCOUNTING_EVIDENCE_VALID")
    return {
        "gross": gross, "commission": commission, "funding": funding,
        "assets": json.dumps({k: fmt(v) for k, v in sorted(assets.items())}, sort_keys=True),
        "commission_complete": fee_complete, "execution_complete": execution_complete,
        "issues": sorted(issues),
        "evidence_status": evidence_status,
        "pnl_complete": gross is not None and "REALIZED_PNL_DISAGREEMENT" not in issues
                        and all(f.realized_pnl is not None and f.margin_asset == "USDT" for f in life.fills),
    }


def reconstruct_records(signal_rows, trade_rows, income_rows, boundary, *, historical=False,
                        collected_at=None, order_rows=(), position_snapshots=()):
    boundary = utc(boundary)
    if boundary is None:
        raise ValueError("Timezone-aware boundary required")
    collected = utc(collected_at) if collected_at else datetime.now(timezone.utc)
    if collected is None:
        raise ValueError("Timezone-aware collection time required")
    if signal_rows and isinstance(signal_rows[0], Signal):
        signals = [s for s in signal_rows if utc(s.timestamp) is not None and (historical or s.timestamp >= boundary)]
    else:
        signals = signals_from_mappings(signal_rows, boundary, historical)
    if trade_rows and isinstance(trade_rows[0], Fill):
        fills = [f for f in trade_rows if historical or f.time >= boundary]
    else:
        fills = normalize_fills(trade_rows, boundary, historical, order_rows)
    fills = [f for f in fills if f.time <= collected]
    lives = segment_lifecycles(fills, boundary, position_snapshots, collected)
    incomes = _dedupe_income(income_rows)
    order_map = {(normalize_symbol(o.get("symbol")), str(o.get("orderId"))): o for o in order_rows}
    consumed, represented, records = set(), set(), []
    for life in lives:
        sig, status, candidates = (None, "PARTIAL", []) if life.orphan else _ownership(life, signals, consumed)
        represented.update(s.key for s in candidates)
        # Same signal cannot claim a later re-entry after an already owned close.
        if sig and status == "MATCHED" and life.close_time:
            consumed.add(sig.key)
        amounts = _account(life, lives, incomes, boundary, collected, order_map)
        row = {name: "" for name in OUTPUT_FIELDS}
        entry_qty = sum((f.qty for f in life.entries), ZERO)
        entry_price = vwap(life.entries)
        risk = None
        if (sig and status == "MATCHED" and sig.stop is not None and entry_price is not None
                and life.provenance in AUTHORITATIVE_PROVENANCE
                and amounts["evidence_status"] == "ACCOUNTING_EVIDENCE_VALID"):
            correct_side = sig.stop < entry_price if sig.side == "LONG" else sig.stop > entry_price
            if correct_side and sig.stop > 0 and all(f.margin_asset == "USDT" for f in life.fills) and not life.issues:
                risk = abs(entry_price - sig.stop) * entry_qty
        gross_r = amounts["gross"] / risk if risk and amounts["gross"] is not None else None
        kinds = {f.maker for f in life.fills if f.maker is not None}
        reductions = life.exits if life.close_time is None else [
            f for f in life.exits if f.order_id != life.exits[-1].order_id
        ]
        partial_tp = any(f.order_type.startswith("TAKE_PROFIT") for f in reductions)
        stop = any(f.order_type in {"STOP", "STOP_MARKET", "TRAILING_STOP_MARKET"} for f in life.exits)
        row.update({
            "record_version": "2", "lifecycle_id": life.key,
            "canonical_signal_key": sig.key if sig else "",
            "candidate_signal_keys": json.dumps(sorted(s.key for s in candidates)),
            "symbol": life.symbol, "side": life.direction, "position_side": life.position_side,
            "boundary_exposure_state": life.boundary_state,
            "lifecycle_provenance": life.provenance,
            "flat_provenance_utc": iso(life.flat_provenance),
            "accounting_evidence_status": amounts["evidence_status"],
            "binance_trade_id": life.fills[0].trade_id, "binance_order_id": life.fills[0].order_id,
            "binance_trade_ids": "|".join(f.trade_id for f in life.fills),
            "binance_order_ids": "|".join(sorted({f.order_id for f in life.fills})),
            "entry_fill_time_utc": iso(life.start), "entry_fill_count": str(len(life.entries)),
            "entry_fill_price": fmt(entry_price), "entry_fill_qty": fmt(entry_qty),
            "exit_fill_count": str(len(life.exits)), "exit_fill_qty": fmt(sum((f.qty for f in life.exits), ZERO)),
            "exit_vwap": fmt(vwap(life.exits)), "position_close_time_utc": iso(life.close_time),
            "remaining_qty": fmt(life.qty) if not life.orphan else "",
            "gross_realized_pnl_usdt": fmt(amounts["gross"]),
            "commission_usdt": fmt(amounts["commission"]), "commission_by_asset": amounts["assets"],
            "funding_usdt": fmt(amounts["funding"]), "initial_risk_usdt": fmt(risk),
            "gross_realized_r": fmt(gross_r),
            "maker_taker_if_available": "MAKER" if kinds == {True} else "TAKER" if kinds == {False} else "MIXED" if kinds else "",
            "partial_tp_detected": "true" if partial_tp else "UNKNOWN",
            "stop_fill_detected": "true" if stop else "UNKNOWN",
            "match_status": status, "data_quality": "RECONCILIATION_PENDING",
            "execution_completeness": "EXECUTION_COMPLETE" if amounts["execution_complete"] else "PARTIAL",
            "commission_completeness": "COMPLETE" if amounts["commission_complete"] else "PARTIAL",
            "cost_completeness": "PARTIAL",
            "position_terminal": "POSITION_TERMINAL" if life.close_time and not life.issues else "UNKNOWN" if life.issues else "OPEN",
            "accounting_finalized": "false", "reconciliation_status": "RECONCILIATION_PENDING",
            "accounting_issues": "|".join(amounts["issues"]),
            "data_source": "BINANCE_USDM_RESEARCH_ONLY" if historical else "BINANCE_USDM_PROSPECTIVE",
            "prospective_start_utc": iso(boundary), "collected_at_utc": iso(collected),
        })
        if sig:
            row.update({"signal_timestamp_utc": iso(sig.timestamp), "signal_entry": fmt(sig.entry),
                        "signal_sl": fmt(sig.stop), "signal_tp1": fmt(sig.tp1), "signal_tp2": fmt(sig.tp2)})
        # FINAL means reconciled for this finite observed execution set, not an
        # exchange guarantee against corrections. Funding is a separate ledger.
        provenance_valid = life.provenance in AUTHORITATIVE_PROVENANCE and not historical
        terminal = amounts["execution_complete"] and life.close_time is not None and provenance_valid
        evidence_valid = amounts["evidence_status"] == "ACCOUNTING_EVIDENCE_VALID"
        pnl_final = terminal and amounts["pnl_complete"] and evidence_valid
        fee_final = terminal and evidence_valid and amounts["commission_complete"] and all(
            f.commission is not None and f.commission_asset == "USDT" for f in life.fills)
        execution_pnl = (amounts["gross"] + amounts["commission"]
                         if status == "MATCHED" and pnl_final and fee_final else None)
        row.update({
            "execution_finality": "EXECUTION_FINAL" if terminal else "EXECUTION_PENDING",
            "realized_pnl_finality": "REALIZED_PNL_FINAL" if pnl_final else "REALIZED_PNL_PENDING",
            "commission_finality": "COMMISSION_FINAL" if fee_final else "COMMISSION_PENDING",
            "funding_finality": "FUNDING_PENDING",
            "accounting_finality": "ACCOUNTING_PENDING",
            "execution_pnl_usdt": fmt(execution_pnl),
            "income_reconciliation_start_utc": iso(life.start),
            "income_reconciliation_status": "PENDING",
        })
        if not provenance_valid:
            row["accounting_issues"] += "|PROSPECTIVE_PROVENANCE_UNPROVEN"
            if life.provenance == "BOUNDARY_EXPOSURE_UNKNOWN":
                row["position_terminal"] = "UNKNOWN"
        if amounts["evidence_status"] == "ACCOUNTING_EVIDENCE_INVALID":
            row["data_quality"] = "INVALID_REQUIRES_RECONCILIATION"
            row["reconciliation_status"] = "INVALID_REQUIRES_RECONCILIATION"
        safe_risk = valid_initial_risk(row)
        row["execution_r"] = fmt(execution_pnl / safe_risk if execution_pnl is not None and safe_risk else None)
        row["provisional_net_realized_pnl_usdt"] = fmt(
            execution_pnl + amounts["funding"] if execution_pnl is not None and amounts["funding"] is not None else None)
        # All-in net remains unknown: no funding/other-adjustment finality claim.
        records.append(row)
    for s in signals:
        if s.key not in represented:
            row = {name: "" for name in OUTPUT_FIELDS}
            row.update({"record_version": "2", "lifecycle_id": "signal:" + s.key, "canonical_signal_key": s.key,
                        "symbol": s.symbol, "side": s.side, "signal_timestamp_utc": iso(s.timestamp),
                        "match_status": "UNMATCHED", "data_quality": "EXECUTION_NOT_OBSERVED",
                        "prospective_start_utc": iso(boundary), "collected_at_utc": iso(collected),
                        "data_source": "BINANCE_USDM_RESEARCH_ONLY" if historical else "BINANCE_USDM_PROSPECTIVE"})
            records.append(row)
    assert_unique_ownership(records)
    return sorted(records, key=lambda r: r["lifecycle_id"])


def assert_unique_ownership(records):
    owners = {}
    for row in records:
        ids = (row.get("binance_trade_ids") or row.get("binance_trade_id") or "").split("|")
        for tid in filter(None, ids):
            key = (row["symbol"], tid)
            if key in owners:
                raise ValueError("Duplicate trade ownership")
            owners[key] = row["lifecycle_id"]
