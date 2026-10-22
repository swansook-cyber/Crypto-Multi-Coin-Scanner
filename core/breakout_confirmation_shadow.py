"""One prospective hypothesis, no execution capability and no lifecycle engine.

Capture is opt-in in the scanner. Reporting opens SQLite read-only and joins
existing canonical modeled/owned-execution evidence. Importing does no I/O.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import sqlite3

import pandas as pd

from core.research_telemetry import ShadowDecision
from core.signal_identity import canonical_signal_key

NAME = "breakout_confirmation_prospective_v1"
VERSION = "BREAKOUT_CONFIRMATION_V1"
DEFINITION = (
    "Latest closed 1h candle close strictly > max(high) of the immediately "
    "preceding 20 closed 1h candles for LONG; strictly < min(low) for SHORT. "
    "Current candle excluded from lookback. Equality is unconfirmed. "
    "21 finite contiguous closed 1h candles required for auditable capture."
)
DEFINITION_HASH = hashlib.sha256(DEFINITION.encode()).hexdigest()
TERMINAL = {"TP2_WIN", "TP1_THEN_ORIGINAL_SL", "ORIGINAL_SL"}
REFERENCES = {"historical": {"N": 287, "net_r": 49.685052548339},
              "frozen_prospective": {"N": 25, "net_r": 5.799125634998}}


def _utc(value):
    if value is None or isinstance(value, (int, float)) or not str(value).strip():
        raise ValueError("Explicit UTC timestamp required")
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError("Timezone-aware timestamp required")
    return stamp.tz_convert("UTC")


def _iso(value):
    return _utc(value).isoformat().replace("+00:00", "Z")


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def capture_source(candles, signal_time, side, expected, *, timeframe="1h"):
    """Validate a copy of the scorer's inputs; never replace its decision."""
    if timeframe != "1h" or side not in {"LONG", "SHORT"} or type(expected) not in {bool}:
        raise ValueError("V1 requires 1h, LONG/SHORT and a real Boolean")
    if len(candles) < 21:
        raise ValueError("21 source candles required")
    signal_at = _utc(signal_time)
    raw = []
    previous_open = None
    for row in candles.tail(21).to_dict("records"):
        opened, closed = _utc(row["open_time"]), _utc(row["close_time"])
        if closed != opened + pd.Timedelta(hours=1) - pd.Timedelta(milliseconds=1):
            raise ValueError("Unexpected 1h candle interval")
        if closed > signal_at or (previous_open is not None and opened != previous_open + pd.Timedelta(hours=1)):
            raise ValueError("Future, duplicate, unordered or missing source candle")
        numbers = {name: _number(row[name]) for name in ("high", "low", "close")}
        if any(v is None or v <= 0 for v in numbers.values()) or not numbers["low"] <= numbers["close"] <= numbers["high"]:
            raise ValueError("Invalid source OHLC")
        raw.append({"open_time": _iso(opened), "close_time": _iso(closed), **numbers})
        previous_open = opened
    if signal_at - _utc(raw[-1]["close_time"]) >= pd.Timedelta(hours=1):
        raise ValueError("Stale source candle; no retrospective capture")
    high = max(r["high"] for r in raw[:-1])
    low = min(r["low"] for r in raw[:-1])
    actual = raw[-1]["close"] > high if side == "LONG" else raw[-1]["close"] < low
    if actual != expected:
        raise ValueError("Captured definition contradicts scorer state")
    payload = {"definition_version": VERSION, "definition": DEFINITION,
               "definition_hash": DEFINITION_HASH, "source_timeframe": "1h",
               "source_provenance": "BINANCE_USDM_FETCH_CLOSED_KLINES:scorer_input",
               "source_candle_timestamp_utc": raw[-1]["close_time"],
               "signal_timestamp_utc": _iso(signal_at), "side": side,
               "source_candles": raw, "lookback_high": high, "lookback_low": low,
               "breakout_confirmed": actual}
    payload["source_hash"] = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
    return payload


def activate(telemetry, *, enabled, dry_run):
    """Called only by an explicitly enabled non-dry-run scan, before scoring.

    Existing v5 tables are reused; no migration/version marker change. Scoped
    triggers protect this hypothesis's boundary and immutable observation rows.
    No caller-supplied/backdated activation time is accepted.
    """
    if not enabled or dry_run or not telemetry.available:
        return ""
    store = telemetry.store
    with closing(store.connect()) as connection, connection:
        for table in ("shadow_decisions", "shadow_boundaries"):
            for operation in ("UPDATE", "DELETE"):
                connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS breakout_v1_{table}_{operation.lower()} "
                    f"BEFORE {operation} ON {table} WHEN OLD.shadow_name='{NAME}' "
                    "BEGIN SELECT RAISE(ABORT,'Breakout V1 evidence is immutable'); END"
                )
            # REPLACE deletes need not fire delete triggers in SQLite. Ignore
            # duplicate insertions before conflict resolution can replace V1.
            identity = " AND candidate_key=NEW.candidate_key AND shadow_version=NEW.shadow_version" if table == "shadow_decisions" else ""
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS breakout_v1_{table}_insert BEFORE INSERT ON {table} "
                f"WHEN NEW.shadow_name='{NAME}' AND EXISTS(SELECT 1 FROM {table} "
                f"WHERE shadow_name=NEW.shadow_name{identity}) BEGIN SELECT RAISE(IGNORE); END"
            )
        rows = connection.execute(
            "SELECT metrics_json FROM shadow_decisions WHERE shadow_name=?", (NAME,)
        ).fetchall()
        if any(json.loads(row[0]).get("definition_hash") != DEFINITION_HASH for row in rows):
            raise ValueError("Definition changed: create a V2, never reuse V1")
    return telemetry.shadow_boundary(NAME)


def attach_observation(snapshot, source, boundary):
    """Return a new snapshot only; no score, ranking, routing or signal writes."""
    if not boundary or snapshot.source_mode != "PROSPECTIVE" or snapshot.decision != "SENT" or snapshot.signal_status != "sent":
        return snapshot
    if _utc(snapshot.timestamp_utc) < _utc(boundary):
        return snapshot
    if not source or source.get("definition_hash") != DEFINITION_HASH:
        raise ValueError("Missing/version-mismatched signal-time source")
    canonical = canonical_signal_key(symbol=snapshot.symbol, side=snapshot.side,
                                     timestamp=snapshot.timestamp_utc, entry=snapshot.entry)
    if not canonical or not canonical.startswith("sig:v1:") or (snapshot.canonical_signal_key and snapshot.canonical_signal_key != canonical):
        raise ValueError("Invalid or conflicting canonical identity")
    # Revalidate the serialized raw components, rather than trusting a mutable flag.
    rebuilt = capture_source(pd.DataFrame(source["source_candles"]), snapshot.timestamp_utc,
                             snapshot.side, source["breakout_confirmed"])
    if rebuilt != source:
        raise ValueError("Source payload was altered after capture")
    metrics = {**source, "canonical_signal_key": canonical, "symbol": snapshot.symbol,
               "activation_boundary_utc": boundary, "source_mode": "PROSPECTIVE",
               "session": snapshot.session, "btc_regime": snapshot.market.get("btc_regime", "UNKNOWN"),
               "utc_day": _utc(snapshot.timestamp_utc).strftime("%Y-%m-%d")}
    metrics["episode_id"] = "|".join((metrics["utc_day"], metrics["btc_regime"] or "UNKNOWN", snapshot.side))
    decision = ShadowDecision(NAME, VERSION, "CONFIRMED" if source["breakout_confirmed"] else "UNCONFIRMED",
                              "Observation only; SENT decision unchanged", metrics)
    return replace(snapshot, shadows=(*snapshot.shadows, decision))


def _stats(rows):
    terminal = [r for r in rows if r.get("modeled_r") is not None]
    positives = [r["modeled_r"] for r in terminal if r["modeled_r"] > 0]
    negatives = [r["modeled_r"] for r in terminal if r["modeled_r"] < 0]
    equity = peak = 0.0
    dd = 0.0
    for r in sorted(terminal, key=lambda r: (r["signal_timestamp_utc"], r["canonical_signal_key"])):
        equity += r["modeled_r"]
        peak = max(peak, equity)
        dd = min(dd, equity - peak)
    total = sum(positives) + sum(negatives)
    return {"N": len(rows), "terminal_N": len(terminal), "pending_or_unresolved_N": len(rows)-len(terminal),
            "positive": len(positives), "negative": len(negatives), "net_r": total,
            "avg_r": total/len(terminal) if terminal else None,
            "win_rate": len(positives)/len(terminal) if terminal else None,
            "PF": sum(positives)/-sum(negatives) if negatives else None,
            "PF_status": "FINITE" if negatives else "NO_LOSSES" if positives else "NO_TERMINAL_DATA",
            "max_dd_r": dd, "dd_order": "SIGNAL_TIME_TERMINAL_SUBSET"}


def _comparison(rows):
    a, b = ([r for r in rows if r["breakout_confirmed"] == flag] for flag in (True, False))
    confirmed, unconfirmed = _stats(a), _stats(b)
    delta = confirmed["avg_r"]-unconfirmed["avg_r"] if confirmed["avg_r"] is not None and unconfirmed["avg_r"] is not None else None
    return {"CONFIRMED": confirmed, "UNCONFIRMED": unconfirmed, "delta_avg_r": delta,
            "delta_net_r_per_trade": delta,
            "win_rate_difference": confirmed["win_rate"]-unconfirmed["win_rate"] if delta is not None else None,
            "loss_r_avoided": -sum(r["modeled_r"] for r in b if r.get("modeled_r") is not None and r["modeled_r"] < 0),
            "winner_r_sacrificed": sum(r["modeled_r"] for r in b if r.get("modeled_r") is not None and r["modeled_r"] > 0),
            "hypothetical_exclusion_delta_r": -sum(r["modeled_r"] for r in b if r.get("modeled_r") is not None)}


def _execution_links(execution_rows):
    from core.binance_execution_reconstruction import valid_initial_risk
    owners = defaultdict(list)
    bad_keys = set()
    fill_owners = defaultdict(set)
    for index, row in enumerate(execution_rows):
        key = str(row.get("canonical_signal_key", ""))
        if row.get("match_status") == "AMBIGUOUS":
            try:
                candidates = json.loads(row.get("candidate_signal_keys") or "[]")
                if isinstance(candidates, list):
                    bad_keys.update(k for k in candidates if isinstance(k, str))
            except (ValueError, TypeError):
                pass
        if key:
            owners[key].append(row)
            for trade in (row.get("binance_trade_ids") or "").split("|"):
                if trade:
                    fill_owners[(row.get("symbol"), trade)].add((key, index))
    for values in fill_owners.values():
        if len(values) > 1:
            bad_keys.update(key for key, _ in values)
    result = {}
    for key, values in owners.items():
        if key in bad_keys or len(values) != 1:
            continue
        row = values[0]
        risk = valid_initial_risk(row)
        if risk is None or any(row.get(name) != expected for name, expected in (
            ("execution_finality", "EXECUTION_FINAL"), ("realized_pnl_finality", "REALIZED_PNL_FINAL"),
            ("commission_finality", "COMMISSION_FINAL"))):
            continue
        pnl, ratio, commission = (_number(row.get(k)) for k in ("execution_pnl_usdt", "execution_r", "commission_usdt"))
        if any(v is None for v in (pnl, ratio, commission)) or abs(Decimal(str(pnl))/risk-Decimal(str(ratio))) > Decimal("1e-10"):
            continue
        result[key] = {"execution_r": ratio, "execution_only_net_usdt": pnl,
                       "commission_usdt": commission, "execution_lifecycle_id": row.get("lifecycle_id", "")}
    return result, bad_keys


def normalize_report_timestamp(value):
    """An aware UTC instant, preserving pandas microsecond/nanosecond precision."""
    return _utc(value)


def report(db_path, lifecycle_rows=(), execution_rows=()):
    """Read-only snapshot join; no enrichment writes and no market evaluation."""
    result = {"shadow_name": NAME, "definition_version": VERSION, "definition": DEFINITION,
              "activation_boundary_utc": "", "classification": "INSUFFICIENT_EVIDENCE",
              "references_only": REFERENCES, "records": [], "gaps": [],
              "comparison": _comparison([]), "breakdowns": {}}
    path = Path(db_path)
    if not path.exists():
        result["status"] = "NOT_ACTIVATED"
        return result
    with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='shadow_boundaries'").fetchone():
            result["status"] = "NOT_ACTIVATED"
            return result
        boundary = connection.execute("SELECT prospective_start_utc FROM shadow_boundaries WHERE shadow_name=?", (NAME,)).fetchone()
        if not boundary:
            result["status"] = "NOT_ACTIVATED"
            return result
        result["activation_boundary_utc"] = boundary[0]
        observations = connection.execute(
            "SELECT c.candidate_key,c.canonical_signal_key,c.timestamp_utc,c.symbol,c.side,c.entry,s.decision,s.metrics_json FROM candidates c "
            "JOIN shadow_decisions s USING(candidate_key) WHERE s.shadow_name=? AND s.shadow_version=? "
            "AND c.source_mode='PROSPECTIVE' AND c.decision='SENT'", (NAME, VERSION)).fetchall()
        candidate_counts = dict(connection.execute(
            "SELECT canonical_signal_key,COUNT(*) FROM candidates GROUP BY canonical_signal_key"))
        eligible = {r[0] for r in connection.execute(
            "SELECT canonical_signal_key,timestamp_utc FROM candidates WHERE source_mode='PROSPECTIVE' AND decision='SENT'")
            if normalize_report_timestamp(r[1]) >= normalize_report_timestamp(boundary[0])}
    outcomes = defaultdict(list)
    for row in lifecycle_rows:
        outcomes[str(row.get("canonical_signal_key", ""))].append(row)
    executions, ambiguous = _execution_links(execution_rows)
    seen = set()
    key_counts = defaultdict(int)
    for row in observations:
        key_counts[row["canonical_signal_key"]] += 1
    for row in observations:
        key = row["canonical_signal_key"]
        try:
            record = json.loads(row["metrics_json"])
            if (not key or not row["candidate_key"] or candidate_counts.get(key) != 1
                    or key_counts[key] != 1 or record.get("canonical_signal_key") != key
                    or record.get("definition_hash") != DEFINITION_HASH):
                raise ValueError("Conflicting identity/definition")
            candidate_time = normalize_report_timestamp(row["timestamp_utc"])
            source_time = normalize_report_timestamp(record["signal_timestamp_utc"])
            # The telemetry writer stores whole seconds. Only that known lossy
            # representation may reconcile to a finer source time, after the
            # canonical key and unique candidate FK have already matched.
            timestamp_matches = (candidate_time == source_time or (
                candidate_time == candidate_time.floor("s")
                and candidate_time == source_time.floor("s")))
            if source_time < normalize_report_timestamp(boundary[0]):
                raise ValueError("Observation precedes activation")
            if (record.get("symbol") != row["symbol"] or record.get("side") != row["side"]
                    or not timestamp_matches
                    or normalize_report_timestamp(record.get("activation_boundary_utc")) != normalize_report_timestamp(boundary[0])
                    or record.get("source_mode") != "PROSPECTIVE"
                    or row["decision"] != ("CONFIRMED" if record["breakout_confirmed"] else "UNCONFIRMED")
                    or canonical_signal_key(symbol=row["symbol"], side=row["side"], timestamp=row["timestamp_utc"], entry=row["entry"]) != key
                    or canonical_signal_key(symbol=row["symbol"], side=row["side"], timestamp=record["signal_timestamp_utc"], entry=row["entry"]) != key):
                raise ValueError("Candidate/source identity or boundary mismatch")
            rebuilt = capture_source(pd.DataFrame(record["source_candles"]), record["signal_timestamp_utc"], record["side"], record["breakout_confirmed"])
            if any(record.get(k) != v for k, v in rebuilt.items()):
                raise ValueError("Observation source mismatch")
        except (ValueError, KeyError, TypeError) as exc:
            result["gaps"].append({"key": key, "reason": str(exc)})
            continue
        seen.add(key)
        record.update(modeled_state="PENDING", modeled_r=None, execution_r=None,
                      execution_only_net_usdt=None, commission_usdt=None,
                      execution_owner_state="AMBIGUOUS" if key in ambiguous else "UNOWNED_OR_UNQUALIFIED")
        matches = outcomes.get(key, [])
        if len(matches) == 1:
            state = str(matches[0].get("lifecycle_state", ""))
            value = _number(matches[0].get("lifecycle_r"))
            if state in TERMINAL and value is not None and not str(matches[0].get("ambiguity_flag", "0")).lower() in {"1", "true"}:
                record.update(modeled_state=state, modeled_r=value)
        elif len(matches) > 1:
            result["gaps"].append({"key": key, "reason": "Nonunique canonical lifecycle truth"})
        if key in executions:
            record.update(executions[key], execution_owner_state="AUTHORITATIVE_OWNED_EXECUTION_ONLY")
        result["records"].append(record)
    result["missing_observation_keys"] = sorted(eligible-seen)
    rows = result["records"]
    result.update(status="ACTIVE", comparison=_comparison(rows), breakdowns={})
    for field in ("side", "session", "btc_regime", "utc_day", "episode_id"):
        result["breakdowns"][field] = {str(value): _comparison([r for r in rows if r.get(field) == value]) for value in sorted({r.get(field, "UNKNOWN") for r in rows}, key=str)}
    execution_sample = [{**r, "modeled_r": r["execution_r"]} for r in rows if r["execution_r"] is not None]
    result["execution_comparison"] = _comparison(execution_sample)
    result["execution_totals"] = {"N": len(execution_sample), "net_usdt": sum(r["execution_only_net_usdt"] for r in execution_sample),
                                  "commission_usdt": sum(r["commission_usdt"] for r in execution_sample), "accounting_basis": "EXECUTION_ONLY_NOT_FUNDING_FINAL"}
    comp = result["comparison"]
    daily = result["breakdowns"]["utc_day"]
    supporting = [day for day, info in daily.items() if info["delta_avg_r"] is not None and info["delta_avg_r"] > 0]
    result["episode_awareness"] = {
        "method": "UTC day | signal-time BTC regime | side; descriptive proxy, not independent trials",
        "observed_utc_days": len(daily), "supporting_utc_days": supporting,
        "benefit_distribution": "MULTIPLE_DAYS" if len(supporting) >= 2 else "ONE_DAY_OR_UNESTABLISHED",
        "independence": "NOT_ESTABLISHED: inspect overlapping market moves across days and symbols",
        "largest_episode_terminal_share": max((sum(r.get("modeled_r") is not None for r in rows if r["episode_id"] == episode)
            for episode in result["breakdowns"]["episode_id"]), default=0) / max(1, sum(r.get("modeled_r") is not None for r in rows)),
    }
    side_info = result["breakdowns"]["side"]
    sides_observed = all(side in side_info and side_info[side]["delta_avg_r"] is not None for side in ("LONG", "SHORT"))
    side_consistent = sides_observed and all(info["delta_avg_r"] > 0 for info in side_info.values())
    exec_comp = result["execution_comparison"]
    execution_consistent = not execution_sample or (exec_comp["delta_avg_r"] is not None and exec_comp["delta_avg_r"] >= 0 and exec_comp["hypothetical_exclusion_delta_r"] >= 0)
    result["decision_gates"] = {"new_untouched_population": not result["missing_observation_keys"] and not result["gaps"],
        "modeled_avg_benefit": comp["delta_avg_r"] is not None and comp["delta_avg_r"] > 0,
        "multiple_supporting_utc_days": len(supporting) >= 2, "supporting_days": supporting,
        "both_directions_observed_and_consistent": side_consistent,
        "loss_avoidance_exceeds_winner_sacrifice": comp["hypothetical_exclusion_delta_r"] > 0,
        "execution_consistent_where_available": execution_consistent}
    if comp["delta_avg_r"] is not None and (comp["delta_avg_r"] <= 0 or comp["hypothetical_exclusion_delta_r"] <= 0):
        result["classification"] = "CONTRADICTORY"
    elif all(v for k, v in result["decision_gates"].items() if k != "supporting_days"):
        result["classification"] = "PROMISING"
    result["readiness_policy"] = (
        "READY_FOR_CONTROLLED_LIVE_CANDIDATE is independent-review-only: inspect episode independence, "
        "directional dependency, winner sacrifice and execution coverage. Automatic reporting never "
        "promotes a live filter and uses no fitted/fixed trade-count target."
    )
    return result
