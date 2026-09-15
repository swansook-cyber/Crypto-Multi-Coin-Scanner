"""Fixture-only regression coverage for every reproduced precommit defect."""

from __future__ import annotations

import ast
from contextlib import redirect_stdout, redirect_stderr
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import core.binance_execution_truth_collector as c
import core.binance_execution_reconstruction as r
from core.binance_execution_truth import BinanceExecutionTruthClient, BinanceReadOnlyError, GET_ALLOWLIST

BOUNDARY = datetime(2026, 9, 6, tzinfo=timezone.utc)
NOW = BOUNDARY + timedelta(hours=12)


def stamp(h, minute=0, micros=0):
    return BOUNDARY + timedelta(hours=h, minutes=minute, microseconds=micros)


def signal(key="s1", h=1, entry="100", side="LONG", **kw):
    return {"canonical_signal_key": "id:v1:" + key, "timestamp": r.iso(stamp(h)),
            "symbol": "BTCUSDT", "side": side, "entry": entry, "stop_loss": "90",
            "tp1": "110", "tp2": "120", "signal_status": "sent", **kw}


def fill(tid, h=1, minute=5, side="BUY", price="100", qty="1", pnl="0", pos="LONG", **kw):
    return {"symbol": "BTCUSDT", "id": tid, "orderId": tid, "side": side,
            "positionSide": pos, "buyer": side == "BUY", "time": r.ms(stamp(h, minute)),
            "price": price, "qty": qty, "realizedPnl": pnl, "commission": "0.04",
            "commissionAsset": "USDT", "marginAsset": "USDT", "maker": False, **kw}


def income(tid=2, kind="REALIZED_PNL", amount="10", h=2, minute=0, **kw):
    return {"symbol": "BTCUSDT", "incomeType": kind, "income": amount, "asset": "USDT",
            "tradeId": str(tid) if tid else "", "tranId": str(10000 + tid),
            "time": r.ms(stamp(h, minute)), **kw}


def orders_for(fills):
    orders = {}
    for f in fills:
        oid = str(f["orderId"])
        opening = (f["positionSide"] == "LONG" and f["side"] == "BUY"
                   or f["positionSide"] == "SHORT" and f["side"] == "SELL")
        if oid not in orders:
            orders[oid] = {"symbol": f["symbol"], "orderId": f["orderId"], "side": f["side"],
                           "positionSide": f["positionSide"], "reduceOnly": not opening,
                           "type": "MARKET" if opening else "TAKE_PROFIT_MARKET",
                           "status": "FILLED", "executedQty": "0", "time": f["time"], "updateTime": f["time"]}
        orders[oid]["executedQty"] = str(Decimal(orders[oid]["executedQty"]) + Decimal(f["qty"]))
    return list(orders.values())


def pair():
    return [fill(1), fill(2, h=2, minute=0, side="SELL", price="110", pnl="10")]


def flat_snapshots(symbols=("BTCUSDT",), boundary=BOUNDARY):
    # Explicit fixture precondition: every tested side was observed flat at T0.
    return [{"symbol": symbol, "positionSide": side, "positionAmt": "0",
             "observed_from_utc": r.iso(boundary), "observed_through_utc": r.iso(boundary)}
            for symbol in symbols for side in ("LONG", "SHORT", "BOTH")]


def fixture_collection(*args, **kwargs):
    kwargs.setdefault("position_snapshots", flat_snapshots())
    return c.run_collection(*args, **kwargs)


def reconstruct(fills=None, signals=None, incomes=(), orders=None):
    fills = pair() if fills is None else fills
    return r.reconstruct_records([signal()] if signals is None else signals, fills, incomes, BOUNDARY,
                                 collected_at=NOW, order_rows=orders_for(fills) if orders is None else orders,
                                 position_snapshots=flat_snapshots({f["symbol"] for f in fills}))


def lives(rows):
    return [row for row in rows if row["binance_trade_ids"]]


class FixtureClient:
    def __init__(self, trades=(), incomes=(), orders=None):
        self.trades = list(trades)
        self.incomes = list(incomes)
        self.orders = orders_for(trades) if orders is None else list(orders)
        self.calls = []
        self.fail = False

    def positions(self):
        return []  # No explicit position observation: never infer flatness from absence.

    def server_now(self):
        return NOW

    def income_history(self, **p):
        self.calls.append(("income", p))
        if self.fail:
            raise RuntimeError("fixture fetch failure")
        rows = [r for r in self.incomes if p["startTime"] <= r["time"] <= p["endTime"]]
        start = (p["page"] - 1) * p["limit"]
        return list(reversed(rows[start:start + p["limit"]]))

    def order_history(self, **p):
        self.calls.append(("orders", p))
        rows = [r for r in self.orders if p["startTime"] <= r["time"] <= p["endTime"]
                and (not p.get("symbol") or r["symbol"] == p["symbol"])]
        return list(reversed(sorted(rows, key=lambda x: int(x["orderId"]))[-p["limit"]:]))

    def user_trades(self, **p):
        self.calls.append(("trades", p))
        rows = [r for r in self.trades if r["symbol"] == p["symbol"]]
        if "fromId" in p:
            rows = sorted([r for r in rows if int(r["id"]) >= p["fromId"]], key=lambda x: int(x["id"]))[:p["limit"]]
        else:
            rows = sorted([r for r in rows if p["startTime"] <= r["time"] <= p["endTime"]],
                          key=lambda x: (x["time"], int(x["id"])))[-p["limit"]:]
        return list(reversed(rows))


class OfflineOnly(unittest.TestCase):
    def setUp(self):
        self.network = patch("socket.socket.connect", side_effect=AssertionError("Live network forbidden"))
        self.network.start()

    def tearDown(self):
        self.network.stop()


class AttributionTests(OfflineOnly):
    def test_late_signal_cannot_own_earlier_entry_via_scale_in(self):
        ff = [fill(1), fill(2, h=3), fill(3, h=4, side="SELL", qty="2", pnl="20")]
        row = lives(reconstruct(ff, [signal(h=2)]))[0]
        self.assertEqual(row["match_status"], "PARTIAL")

    def test_older_price_newer_time_conflict_is_ambiguous(self):
        f = [fill(1, h=5, minute=1), fill(2, h=6, minute=0, side="SELL", price="110", pnl="10")]
        row = lives(reconstruct(f, [signal(), signal("s2", h=5, entry="104")]))[0]
        self.assertEqual(row["match_status"], "AMBIGUOUS")
        self.assertEqual(row["canonical_signal_key"], "")
        self.assertEqual(len(json.loads(row["candidate_signal_keys"])), 2)

    def test_same_side_signals_inside_48h_are_not_nearest_matched(self):
        f = [fill(1, h=5, minute=1)]
        row = lives(reconstruct(f, [signal(), signal("s2", h=5, entry="101")]))[0]
        self.assertEqual(row["match_status"], "AMBIGUOUS")

    def test_overlapping_windows_one_logical_owner(self):
        rows = reconstruct([fill(1)], [signal(), signal("s2", timestamp=r.iso(stamp(1, 1)))])
        r.assert_unique_ownership(rows)
        self.assertEqual(len(lives(rows)), 1)
        self.assertEqual(lives(rows)[0]["match_status"], "AMBIGUOUS")

    def test_explicit_order_reference_can_resolve_conflict(self):
        ss = [signal(binance_order_id="1"), signal("s2", timestamp=r.iso(stamp(1, 1)))]
        row = lives(reconstruct(signals=ss))[0]
        self.assertEqual(row["canonical_signal_key"], "id:v1:s1")
        self.assertEqual(row["match_status"], "MATCHED")

    def test_repeat_after_closed_lifecycle_uses_new_signal(self):
        ff = pair() + [fill(3, h=3), fill(4, h=4, minute=0, side="SELL", price="110", pnl="10")]
        rows = lives(reconstruct(ff, [signal(), signal("s2", h=3)]))
        self.assertEqual(len(rows), 2)
        self.assertEqual([x["canonical_signal_key"] for x in rows], ["id:v1:s1", "id:v1:s2"])

    def test_one_signal_cannot_claim_later_reentry(self):
        ff = pair() + [fill(3, h=3)]
        rows = lives(reconstruct(ff))
        self.assertEqual(rows[1]["match_status"], "UNMATCHED")

    def test_report_only_never_owns_execution(self):
        row = lives(reconstruct(signals=[signal(signal_status="tier_c_report_only")]))[0]
        self.assertEqual(row["match_status"], "UNMATCHED")

    def test_no_sent_signal_unmatched(self):
        self.assertEqual(lives(reconstruct(signals=[]))[0]["match_status"], "UNMATCHED")

    def test_missing_price_cannot_prove_match(self):
        self.assertEqual(lives(reconstruct(signals=[signal(entry="")]))[0]["match_status"], "PARTIAL")


class LifecycleTests(OfflineOnly):
    def test_hedge_long_short_openings_do_not_cross_close(self):
        ff = [fill(1), fill(2, side="SELL", pos="SHORT")]
        rows = lives(reconstruct(ff, [signal(), signal("short", side="SHORT", stop_loss="110")]))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(x["exit_fill_count"] == "0" for x in rows))
        self.assertTrue(all(x["position_terminal"] == "OPEN" for x in rows))

    def test_missing_position_side_is_not_inferred(self):
        row = lives(reconstruct([fill(1, pos="")]))[0]
        self.assertEqual(row["match_status"], "PARTIAL")
        self.assertEqual(row["entry_fill_count"], "0")

    def test_buyer_side_conflict_is_partial(self):
        row = lives(reconstruct([fill(1, buyer=False)]))[0]
        self.assertEqual(row["match_status"], "PARTIAL")

    def test_partial_reduce_long(self):
        ff = [fill(1, qty="2"), pair()[1]]
        row = lives(reconstruct(ff))[0]
        self.assertEqual(row["remaining_qty"], "1")
        self.assertEqual(row["position_terminal"], "OPEN")
        self.assertEqual(row["partial_tp_detected"], "true")

    def test_multiple_fills_of_single_close_order_not_partial_tp(self):
        ff = [fill(1, qty="2"), fill(2,h=2,side="SELL",price="110",pnl="10",orderId=10),
              fill(3,h=2,minute=6,side="SELL",price="110",pnl="10",orderId=10)]
        row = lives(reconstruct(ff))[0]
        self.assertEqual(row["partial_tp_detected"],"UNKNOWN")

    def test_full_close_long(self):
        row = lives(reconstruct())[0]
        self.assertEqual(row["position_terminal"], "POSITION_TERMINAL")
        self.assertEqual(row["execution_completeness"], "EXECUTION_COMPLETE")
        self.assertEqual(row["match_status"], "MATCHED")

    def test_multiple_entry_fills_same_order_vwap(self):
        ff = [fill(1, price="99", orderId=10), fill(2, minute=6, price="101", orderId=10),
              fill(3, h=2, minute=0, side="SELL", price="110", qty="2", pnl="20")]
        row = lives(reconstruct(ff))[0]
        self.assertEqual(row["entry_fill_price"], "100")
        self.assertEqual(row["entry_fill_count"], "2")
        self.assertEqual(row["execution_completeness"], "EXECUTION_COMPLETE")

    def test_scale_in_after_45_minutes_is_not_lost(self):
        ff = [fill(1), fill(2, h=2), fill(3, h=3, minute=0, side="SELL", price="110", qty="2", pnl="20")]
        row = lives(reconstruct(ff))[0]
        self.assertEqual(row["entry_fill_qty"], "2")
        self.assertEqual(row["position_terminal"], "POSITION_TERMINAL")

    def test_tp1_tp2(self):
        ff = [fill(1, qty="2"), pair()[1],
              fill(3, h=3, minute=0, side="SELL", price="120", pnl="20")]
        row = lives(reconstruct(ff))[0]
        self.assertEqual(row["partial_tp_detected"], "true")
        self.assertEqual(row["exit_vwap"], "115")

    def test_tp1_then_stop_uses_order_type(self):
        ff = [fill(1, qty="2"), pair()[1],
              fill(3, h=3, minute=0, side="SELL", price="90", pnl="-10")]
        oo = orders_for(ff)
        oo[-1]["type"] = "STOP_MARKET"
        row = lives(reconstruct(ff, orders=oo))[0]
        self.assertEqual(row["partial_tp_detected"], "true")
        self.assertEqual(row["stop_fill_detected"], "true")

    def test_immediate_stop(self):
        ff = [fill(1), fill(2, minute=6, side="SELL", price="90", pnl="-10")]
        oo = orders_for(ff)
        oo[1]["type"] = "STOP_MARKET"
        self.assertEqual(lives(reconstruct(ff, orders=oo))[0]["stop_fill_detected"], "true")

    def test_close_reentry_inside_45m_are_separate(self):
        ff = [fill(1), fill(2, minute=10, side="SELL", price="110", pnl="10"),
              fill(3, minute=20), fill(4, minute=30, side="SELL", price="110", pnl="10")]
        self.assertEqual(len(lives(reconstruct(ff))), 2)

    def test_one_way_opposite_side_without_close_evidence_not_exit(self):
        ff = [fill(1, pos="BOTH"), fill(2, h=2, minute=0, side="SELL", pos="BOTH", pnl="0")]
        oo = orders_for(ff)
        for order in oo:
            order["reduceOnly"] = False
        rows = lives(reconstruct(ff, orders=oo))
        self.assertEqual(rows[0]["exit_fill_count"], "0")

    def test_one_way_reduce_only_close(self):
        ff = [fill(1, pos="BOTH"), fill(2, h=2, minute=0, side="SELL", pos="BOTH", pnl="0")]
        oo = orders_for(ff)
        oo[0]["reduceOnly"], oo[1]["reduceOnly"] = False, True
        self.assertEqual(lives(reconstruct(ff, orders=oo))[0]["position_terminal"], "POSITION_TERMINAL")

    def test_numeric_trade_ordering_same_millisecond(self):
        ff = [fill(10, side="SELL", pnl="1"), fill(9)]
        row = lives(reconstruct(ff))[0]
        self.assertEqual(row["binance_trade_ids"], "9|10")


class AccountingTests(OfflineOnly):
    def test_other_symbol_income_ignored(self):
        row = lives(reconstruct(incomes=[income(amount="999", symbol="ETHUSDT")]))[0]
        self.assertEqual(row["gross_realized_pnl_usdt"], "10")

    def test_pre_boundary_income_excluded(self):
        row = lives(reconstruct(incomes=[income(amount="888", time=r.ms(BOUNDARY)-1)]))[0]
        self.assertEqual(row["gross_realized_pnl_usdt"], "10")

    def test_income_before_fill_ignored(self):
        row = lives(reconstruct(incomes=[income(amount="888", h=0)]))[0]
        self.assertEqual(row["gross_realized_pnl_usdt"], "10")

    def test_income_for_other_trade_ignored(self):
        self.assertEqual(lives(reconstruct(incomes=[income(tid=999, amount="888")]))[0]["gross_realized_pnl_usdt"], "10")

    def test_non_usdt_commission_preserved_unknown_net(self):
        ff = [{**f, "commissionAsset": "BNB", "commission": "0.001"} for f in pair()]
        row = lives(reconstruct(ff))[0]
        self.assertEqual(row["commission_usdt"], "")
        self.assertEqual(json.loads(row["commission_by_asset"]), {"BNB": "-0.002"})
        self.assertEqual(row["commission_completeness"], "PARTIAL")
        self.assertEqual(row["net_realized_pnl_usdt"], "")

    def test_one_income_fee_does_not_replace_all_fill_fees(self):
        row = lives(reconstruct(incomes=[income(kind="COMMISSION", amount="-0.04")]))[0]
        self.assertEqual(row["commission_usdt"], "-0.08")

    def test_all_income_and_fill_fees_not_double_counted(self):
        ii = [income(1, kind="COMMISSION", amount="-0.04"), income(kind="COMMISSION", amount="-0.04")]
        row = lives(reconstruct(incomes=ii))[0]
        self.assertEqual(row["commission_usdt"], "-0.08")

    def test_rebate_sign_preserved(self):
        ff = [{**f, "commission": "-0.01"} for f in pair()]
        self.assertEqual(lives(reconstruct(ff))[0]["commission_usdt"], "0.02")

    def test_duplicate_income_deduped(self):
        val = income(kind="COMMISSION", amount="-0.04")
        self.assertEqual(lives(reconstruct(incomes=[val, dict(val)]))[0]["commission_usdt"], "-0.08")

    def test_flat_period_funding_excluded(self):
        ff = [fill(1), fill(2, minute=10, side="SELL", price="110", pnl="10"),
              fill(3, minute=20), fill(4, minute=30, side="SELL", price="110", pnl="10")]
        ii = [income(0, kind="FUNDING_FEE", amount="-1", h=1, minute=15)]
        self.assertTrue(all(row["funding_usdt"] == "" for row in lives(reconstruct(ff, incomes=ii))))

    def test_funding_only_open_interval(self):
        ii = [income(0, kind="FUNDING_FEE", amount="-0.25", h=1, minute=30)]
        row = lives(reconstruct(incomes=ii))[0]
        self.assertEqual(row["funding_usdt"], "-0.25")
        self.assertEqual(row["accounting_finalized"], "false")
        self.assertEqual(row["cost_completeness"], "PARTIAL")

    def test_shared_hedge_funding_not_allocated(self):
        ff = [fill(1), fill(2, pos="SHORT", side="SELL")]
        ii = [income(0, kind="FUNDING_FEE", amount="-1", h=1, minute=30)]
        self.assertTrue(all(row["funding_usdt"] == "" for row in lives(reconstruct(ff, incomes=ii))))

    def test_same_timestamp_funding_ordering_unknown(self):
        ii = [income(0, kind="FUNDING_FEE", amount="-1", h=1, minute=5)]
        self.assertEqual(lives(reconstruct(incomes=ii))[0]["funding_usdt"], "")

    def test_delayed_commission_keeps_pending(self):
        ff = [{k:v for k,v in f.items() if k != "commission"} for f in pair()]
        row = lives(reconstruct(ff))[0]
        self.assertEqual(row["commission_usdt"], "")
        self.assertEqual(row["reconciliation_status"], "RECONCILIATION_PENDING")
        ii = [income(1, kind="COMMISSION", amount="-0.04", h=4),
              income(2, kind="COMMISSION", amount="-0.04", h=4)]
        later = lives(reconstruct(ff, incomes=ii))[0]
        self.assertEqual(later["commission_usdt"], "-0.08")
        self.assertEqual(later["accounting_finalized"], "false")

    def test_missing_funding_not_zero_or_final(self):
        row = lives(reconstruct())[0]
        self.assertEqual(row["funding_usdt"], "")
        self.assertEqual(row["other_execution_cost_usdt"], "")
        self.assertEqual(row["net_realized_pnl_usdt"], "")
        self.assertEqual(row["accounting_finalized"], "false")

    def test_r_uses_actual_qty_and_entry_stop(self):
        ff = [fill(1, qty="2"), fill(2, h=2, minute=0, qty="2", side="SELL", price="110", pnl="20")]
        row = lives(reconstruct(ff))[0]
        self.assertEqual(row["initial_risk_usdt"], "20")
        self.assertEqual(row["gross_realized_r"], "1")
        self.assertEqual(row["net_realized_r"], "")

    def test_missing_or_wrong_side_stop_r_unknown(self):
        for stop in ("", "100", "110"):
            self.assertEqual(lives(reconstruct(signals=[signal(stop_loss=stop)]))[0]["initial_risk_usdt"], "")

    def test_non_usdt_settlement_not_mislabeled(self):
        ff = [{**f, "marginAsset": "USDC"} for f in pair()]
        self.assertEqual(lives(reconstruct(ff))[0]["gross_realized_pnl_usdt"], "")


class CollectionTests(OfflineOnly):
    def test_2000_same_ms_full_collection_state_and_durable_evidence(self):
        ff = [fill(i, orderId=1) for i in range(1,2001)]
        client = FixtureClient(ff)
        with tempfile.TemporaryDirectory() as folder:
            out, state = Path(folder)/"truth.csv", Path(folder)/"state.json"
            rows = fixture_collection(client, r.signals_from_mappings([signal()], BOUNDARY),
                                    BOUNDARY, out, state, now=NOW)
            self.assertEqual(len(lives(rows)[0]["binance_trade_ids"].split("|")),2000)
            self.assertEqual(len(json.loads(c.evidence_path(out).read_text())["trades"]),2000)
            self.assertTrue(state.exists())

    def test_2000_same_ms_recent_first_page_all_preserved(self):
        ff = [fill(i) for i in range(1, 2001)]
        client = FixtureClient(ff, orders=[])
        point = ff[0]["time"]
        got = c.fetch_time_range(client, "trades", point, point, symbol="BTCUSDT", now=NOW)
        self.assertEqual({f["id"] for f in got}, set(range(1, 2001)))
        starts = [p["fromId"] for kind,p in client.calls if kind == "trades" and "fromId" in p]
        self.assertEqual(starts, [0, 1001, 2001])

    def test_time_bisection_handles_out_of_order_and_partial_pages(self):
        ff = [fill(i, minute=5 + i % 3) for i in range(1, 1501)]
        client = FixtureClient(ff, orders=[])
        got = c.fetch_time_range(client, "trades", r.ms(stamp(1)), r.ms(stamp(2)), symbol="BTCUSDT", now=NOW)
        self.assertEqual(len({x["id"] for x in got}), 1500)

    def test_income_pages_out_of_order_same_ms(self):
        ii = [income(i, kind="COMMISSION", amount="-0.01") for i in range(2000)]
        client = FixtureClient(incomes=ii, orders=[])
        self.assertEqual(len(c.fetch_income(client, r.ms(stamp(1)), r.ms(stamp(3)))), 2000)

    def test_repeated_income_page_fails_without_false_success(self):
        client = Mock()
        client.income_history.return_value = [income(i) for i in range(1000)]
        with self.assertRaises(c.CollectionIncomplete):
            c.fetch_income(client, r.ms(stamp(1)), r.ms(stamp(3)))

    def test_older_saturated_timestamp_fails_closed(self):
        client = FixtureClient([fill(i) for i in range(1,1001)], orders=[])
        t = r.ms(stamp(1,5))
        with self.assertRaises(c.CollectionIncomplete):
            c.fetch_time_range(client, "trades", t, t, symbol="BTCUSDT", now=NOW + timedelta(days=10))

    def test_saturated_accountwide_order_timestamp_fails_closed(self):
        client = FixtureClient([fill(i) for i in range(1,1001)])
        t = r.ms(stamp(1,5))
        with self.assertRaises(c.CollectionIncomplete):
            c.fetch_time_range(client, "orders", t, t, now=NOW)

    def test_late_income_over_five_minutes_is_revisited(self):
        state = {"endpoint_high_water":{"scanned_through_utc":r.iso(NOW), "reconciliation_cursor_utc":r.iso(BOUNDARY)}}
        client = FixtureClient(incomes=[income(0, kind="FUNDING_FEE", amount="-1", h=1, minute=30)])
        batch,_ = c.collect(client, [], BOUNDARY, state, now=NOW)
        self.assertTrue(any(x["incomeType"] == "FUNDING_FEE" for x in batch["income"]))

    def test_reconciliation_cursor_rotates_and_preserves_marks(self):
        now = NOW + timedelta(days=20)
        state = {"endpoint_high_water":{"scanned_through_utc":r.iso(now), "reconciliation_cursor_utc":r.iso(BOUNDARY),
                                      "trade_scanned_through_utc":{"BTCUSDT":r.iso(now)}}}
        batch, updated = c.collect(FixtureClient(), [], BOUNDARY, state, now=now)
        self.assertGreater(r.utc(updated["endpoint_high_water"]["reconciliation_cursor_utc"]), BOUNDARY)
        self.assertIn("BTCUSDT", updated["endpoint_high_water"]["trade_scanned_through_utc"])

    def test_retention_gap_fails_no_watermark(self):
        with self.assertRaises(c.CollectionIncomplete):
            c.collect(FixtureClient(), [], BOUNDARY, {"endpoint_high_water":{}}, now=NOW+timedelta(days=100))


class PersistenceTests(OfflineOnly):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)/"truth.csv"
        self.state = Path(self.temp.name)/"state.json"
        self.signals = r.signals_from_mappings([signal()], BOUNDARY)
        self.client = FixtureClient(pair())

    def run_once(self, **kw):
        return fixture_collection(self.client, self.signals, BOUNDARY, self.output, self.state, now=NOW, **kw)

    def test_duplicate_fill_id_deduped(self):
        self.client.trades += [dict(self.client.trades[0])]
        rows = self.run_once()
        r.assert_unique_ownership(rows)
        self.assertEqual(lives(rows)[0]["entry_fill_qty"], "1")

    def test_rerun_byte_idempotent(self):
        self.run_once()
        before = self.output.read_bytes()
        self.run_once()
        self.assertEqual(before, self.output.read_bytes())

    def test_signal_snapshot_retained_after_journal_rotation(self):
        self.run_once()
        rows = fixture_collection(self.client, [], BOUNDARY, self.output, self.state, now=NOW)
        self.assertEqual(lives(rows)[0]["canonical_signal_key"],"id:v1:s1")

    def test_closed_rows_never_removed_from_reconciliation_by_status(self):
        self.run_once()
        active = c.active_signals_for_incremental_run(self.signals, self.output, {}, NOW)
        self.assertEqual(active,self.signals)

    def test_durable_state_write_failure_is_replayable(self):
        with patch.object(c,"atomic_write_state",side_effect=OSError("fixture state failure")):
            with self.assertRaises(OSError):
                self.run_once()
        self.assertTrue(self.output.exists())
        self.assertFalse(self.state.exists())
        self.run_once()
        self.assertTrue(self.state.exists())

    def test_clock_only_change_does_not_rewrite_identical_projection(self):
        self.run_once()
        before = self.output.read_bytes()
        fixture_collection(self.client, self.signals, BOUNDARY, self.output, self.state, now=NOW+timedelta(hours=1))
        self.assertEqual(before, self.output.read_bytes())

    def test_unmatched_to_matched_supersedes_old_representation(self):
        fixture_collection(self.client, [], BOUNDARY, self.output, self.state, now=NOW)
        rows = self.run_once()
        self.assertEqual(len(lives(rows)), 1)
        self.assertEqual(lives(rows)[0]["match_status"], "MATCHED")
        with self.output.open(encoding="utf-8", newline="") as handle:
            saved = list(csv.DictReader(handle))
        r.assert_unique_ownership(saved)

    def test_duplicate_owner_rejected_before_write(self):
        rows = reconstruct()
        with self.assertRaises(ValueError):
            c.write_records(self.output, rows + rows)
        self.assertFalse(self.output.exists())

    def test_crash_after_fetch_before_persist(self):
        def crash(stage):
            if stage == "after_fetch":
                raise RuntimeError("fixture crash")
        with self.assertRaises(RuntimeError):
            self.run_once(checkpoint=crash)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.output.exists())
        self.run_once()
        with self.output.open(encoding="utf-8", newline="") as handle:
            self.assertEqual(len(lives(list(csv.DictReader(handle)))),1)

    def test_crash_after_csv_before_state_replays_without_loss(self):
        def crash(stage):
            if stage == "after_csv":
                raise RuntimeError("fixture crash")
        with self.assertRaises(RuntimeError):
            self.run_once(checkpoint=crash)
        before = self.output.read_bytes()
        self.assertFalse(self.state.exists())
        self.assertTrue(c.evidence_path(self.output).exists())
        rows = self.run_once()
        self.assertEqual(before,self.output.read_bytes())
        r.assert_unique_ownership(rows)
        self.assertTrue(self.state.exists())

    def test_crash_after_evidence_before_csv_replays(self):
        def crash(stage):
            if stage == "after_evidence":
                raise RuntimeError("fixture crash")
        with self.assertRaises(RuntimeError):
            self.run_once(checkpoint=crash)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.output.exists())
        rows = self.run_once()
        self.assertEqual(len(lives(rows)),1)

    def test_partial_fetch_failure_does_not_advance_state(self):
        self.run_once()
        before = self.state.read_bytes()
        self.client.fail=True
        with self.assertRaises(RuntimeError):
            self.run_once()
        self.assertEqual(before,self.state.read_bytes())
        self.client.fail=False
        self.run_once()

    def test_failed_csv_replace_preserves_prior_bytes(self):
        self.run_once()
        before = self.output.read_bytes()
        modified = reconstruct()
        modified[0]["data_quality"]="fixture changed"
        with patch.object(c.os,"replace",side_effect=OSError("fixture failure")):
            with self.assertRaises(OSError):
                c.write_records(self.output,modified)
        self.assertEqual(before,self.output.read_bytes())

    def test_closed_row_receives_delayed_funding(self):
        self.run_once()
        self.client.incomes.append(income(0,kind="FUNDING_FEE",amount="-0.25",h=1,minute=30))
        row = lives(self.run_once())[0]
        self.assertEqual(row["funding_usdt"],"-0.25")
        self.assertEqual(row["accounting_finalized"],"false")

    def test_closed_row_receives_delayed_commission(self):
        self.client.trades=[{k:v for k,v in f.items() if k!="commission"} for f in pair()]
        self.run_once()
        self.client.incomes.extend([income(1,kind="COMMISSION",amount="-0.04",h=4),
                                    income(2,kind="COMMISSION",amount="-0.04",h=4)])
        self.assertEqual(lives(self.run_once())[0]["commission_usdt"],"-0.08")

    def test_late_fill_enriches_persisted_lifecycle(self):
        self.client=FixtureClient([fill(1,qty="2"),pair()[1]])
        self.run_once()
        extra=fill(3,h=3,minute=0,side="SELL",price="90",pnl="-10")
        self.client.trades.append(extra)
        self.client.orders=orders_for(self.client.trades)
        self.assertEqual(lives(self.run_once())[0]["position_terminal"],"POSITION_TERMINAL")

    def test_state_contains_only_approved_metadata(self):
        self.run_once()
        state=json.loads(self.state.read_text())
        self.assertEqual(set(state),c.STATE_KEYS)
        self.assertNotIn("balance",self.state.read_text().lower())
        evidence=json.loads(c.evidence_path(self.output).read_text())
        self.assertEqual(set(evidence),{"collector_version","prospective_start_utc","trades","orders","income","signals","position_snapshots"})

    def test_dry_run_writes_nothing(self):
        self.run_once(dry_run=True)
        self.assertEqual(list(Path(self.temp.name).iterdir()),[])

    def test_legacy_csv_without_evidence_refuses_resume(self):
        self.output.write_text("old data")
        with self.assertRaises(ValueError):
            self.run_once()


class SafetyAndReportTests(OfflineOnly):
    def test_exact_get_allowlist_static_all_modules(self):
        self.assertEqual(GET_ALLOWLIST,{
            "/fapi/v1/time","/fapi/v3/account","/fapi/v1/userTrades","/fapi/v1/allOrders",
            "/fapi/v1/income","/fapi/v1/commissionRate","/fapi/v3/positionRisk"})
        for module in ("binance_execution_truth","binance_execution_truth_collector","binance_execution_reconstruction"):
            source=Path("core",module+".py").read_text(encoding="utf-8")
            tree=ast.parse(source)
            self.assertFalse(any(isinstance(n,ast.ImportFrom) and any(s in (n.module or "") for s in ("telegram","cornix","ccxt")) for n in ast.walk(tree)))
            for n in ast.walk(tree):
                if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute):
                    self.assertNotIn(n.func.attr,{"post","put","delete","patch","request"})
            routes={n.value for n in ast.walk(tree) if isinstance(n,ast.Constant) and isinstance(n.value,str) and n.value.startswith("/fapi/")}
            self.assertTrue(routes<=GET_ALLOWLIST)
        client_tree=ast.parse(Path("core/binance_execution_truth.py").read_text())
        calls=[n for n in ast.walk(client_tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute)
               and isinstance(n.func.value,ast.Attribute) and n.func.value.attr=="_session"]
        self.assertEqual({n.func.attr for n in calls},{"get"})

    def test_synthetic_secret_marker_error_is_suppressed(self):
        error=Mock(status_code=400,headers={})
        error.json.return_value={"code":-2015,"msg":"SYNTHETIC_SECRET_MARKER Authorization: fixture-key signature=fixture-signature"}
        session=Mock();session.get.return_value=error
        client=BinanceExecutionTruthClient("fixture-key","fixture-secret",session=session)
        with self.assertRaises(BinanceReadOnlyError) as ctx:
            client.account()
        for marker in ("SYNTHETIC_SECRET_MARKER","fixture-key","fixture-secret","fixture-signature","Authorization"):
            self.assertNotIn(marker,str(ctx.exception))
        self.assertEqual(ctx.exception.code,-2015)

    def test_network_exception_redacted_including_cause(self):
        session=Mock();session.get.side_effect=RuntimeError("SYNTHETIC_SECRET_MARKER")
        client=BinanceExecutionTruthClient("fixture","fixture",session=session,max_attempts=1)
        with self.assertRaises(BinanceReadOnlyError) as ctx:client.account()
        self.assertTrue(ctx.exception.__suppress_context__)
        self.assertNotIn("SYNTHETIC_SECRET_MARKER",str(ctx.exception))

    def test_cli_raw_exception_redacted(self):
        err=io.StringIO()
        with patch.object(c,"load_sent_signals",side_effect=RuntimeError("SYNTHETIC_SECRET_MARKER")),patch.object(Path,"read_bytes",return_value=b"fixture"),redirect_stderr(err):
            rc=c.main(["--run","--start-utc",r.iso(BOUNDARY)])
        self.assertEqual(rc,1)
        self.assertNotIn("SYNTHETIC_SECRET_MARKER",err.getvalue())

    def test_retry_after_and_auth_stop(self):
        for http,expected in ((429,2),(401,1)):
            bad=Mock(status_code=http,headers={"Retry-After":"2"});bad.json.return_value={"code":-2015}
            ok=Mock(status_code=200,headers={});ok.json.return_value=[]
            session=Mock();session.get.side_effect=[bad,ok];sleeps=[]
            client=BinanceExecutionTruthClient("fixture","fixture",session=session,sleep=sleeps.append)
            try:client.account()
            except BinanceReadOnlyError:pass
            self.assertEqual(session.get.call_count,expected)

    def test_missing_boundary_refused_before_credentials(self):
        with patch.dict(c.os.environ,{},clear=True),patch.object(c,"_load_credentials") as credentials,redirect_stderr(io.StringIO()):
            self.assertEqual(c.main(["--run"]),1)
            credentials.assert_not_called()

    def test_historical_write_refused(self):
        with patch.object(c,"_load_credentials") as credentials,redirect_stderr(io.StringIO()):
            self.assertEqual(c.main(["--run","--historical","--start-utc",r.iso(BOUNDARY)]),1)
            credentials.assert_not_called()

    def test_subsecond_boundary_preserved(self):
        boundary=BOUNDARY+timedelta(microseconds=900001)
        self.assertEqual(c._boundary(r.iso(boundary)),boundary)
        ff=[fill(1,time=r.ms(BOUNDARY)+900),fill(2,time=r.ms(BOUNDARY)+901)]
        rr=r.reconstruct_records([],ff,[],boundary,collected_at=NOW)
        self.assertEqual([x["binance_trade_ids"] for x in lives(rr)],["2"])

    def test_naive_boundary_rejected_and_offset_normalized(self):
        with self.assertRaises(ValueError):c._boundary("2026-09-06T00:00:00.900")
        self.assertEqual(c._boundary("2026-09-06T07:00:00.900+07:00"),BOUNDARY+timedelta(microseconds=900000))

    def test_pre_boundary_signal_and_execution_excluded(self):
        rr=r.reconstruct_records([signal(timestamp=r.iso(BOUNDARY-timedelta(seconds=1)))],
                                 [fill(1,time=r.ms(BOUNDARY)-1)],[],BOUNDARY,collected_at=NOW)
        self.assertEqual(rr,[])

    def test_partial_rows_do_not_enter_authoritative_net(self):
        rows=reconstruct([fill(1,qty="2"),pair()[1]])
        data=c.report_records(rows)
        self.assertEqual(data["complete_cost_matched"]["rows"],0)
        self.assertIsNone(data["complete_cost_matched"]["net_realized_pnl_usdt"])
        self.assertIsNone(data["provisional_pending"]["net_realized_pnl_usdt"])

    def test_missing_values_not_summed_as_zero(self):
        rows=reconstruct()
        rows[0]["commission_usdt"]=""
        data=c.report_records(rows)
        self.assertIsNone(data["provisional_pending"]["commission_usdt"])
        self.assertEqual(data["provisional_pending"]["commission_usdt_known_rows"],0)

    def test_complete_cost_report_population_requires_all_flags(self):
        row=reconstruct()[0]
        row.update({"cost_completeness":"COST_COMPLETE","accounting_finalized":"true",
                    "funding_usdt":"0","other_execution_cost_usdt":"0",
                    "net_realized_pnl_usdt":"9.92","net_realized_r":"0.992",
                    "funding_finality":"FUNDING_FINAL","income_reconciliation_status":"FINAL",
                    "accounting_finality":"ACCOUNTING_FINAL","reconciliation_status":"ACCOUNTING_FINALIZED"})
        data=c.report_records([row])
        self.assertEqual(data["complete_cost_matched"]["net_realized_pnl_usdt"],"9.92")
        row["commission_usdt"]=""
        self.assertEqual(c.report_records([row])["complete_cost_matched"]["rows"],0)

    def test_historical_not_in_primary_report(self):
        rows=reconstruct()
        rows[0]["data_source"]="BINANCE_USDM_RESEARCH_ONLY"
        self.assertEqual(c.report_records(rows)["observed_binance_fills"],0)

    def test_protected_signal_path_rejected(self):
        with self.assertRaises(ValueError):
            c._validate_paths(Path("logs/signals.csv"),Path(".env.binance-readonly"),Path("logs/signals.csv"),Path("state/x.json"))

    def test_signal_bytes_unchanged_during_fixture_cli(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);journal=root/"signals.csv";out=root/"truth.csv";state=root/"state.json"
            with journal.open("w",newline="") as f:
                writer=csv.DictWriter(f,fieldnames=list(signal()));writer.writeheader();writer.writerow(signal())
            before=hashlib.sha256(journal.read_bytes()).digest()
            client=FixtureClient(pair());client.sync_server_time=lambda:0
            with patch.object(c,"_load_credentials",return_value=("fixture","fixture")),patch.object(c,"BinanceExecutionTruthClient",return_value=client),redirect_stdout(io.StringIO()):
                rc=c.main(["--run","--signals",str(journal),"--output",str(out),"--state",str(state),"--start-utc",r.iso(BOUNDARY)])
            self.assertEqual(rc,0)
            self.assertEqual(before,hashlib.sha256(journal.read_bytes()).digest())



class Round2RemediationTests(OfflineOnly):
    def test_explicit_order_conflict_is_hard_rejection(self):
        self.assertEqual(lives(reconstruct(signals=[signal(binance_order_id="999")]))[0]["match_status"], "UNMATCHED")

    def test_missing_order_evidence_is_partial_not_contradiction(self):
        ff=[fill(1,orderId="")]
        row=lives(reconstruct(ff,[signal(binance_order_id="999")],orders=[]))[0]
        self.assertEqual(row["match_status"],"PARTIAL")
        self.assertEqual(row["canonical_signal_key"],"id:v1:s1")

    def test_conflicting_candidate_does_not_block_compatible_candidate(self):
        ss=[signal(binance_order_id="999"),signal("b")]
        row=lives(reconstruct(signals=ss))[0]
        self.assertEqual(row["canonical_signal_key"],"id:v1:b")
        self.assertEqual(row["match_status"],"MATCHED")

    def test_unique_hard_link_dominates_soft_price(self):
        ss=[signal(entry="500",binance_order_id="1"),signal("b")]
        self.assertEqual(lives(reconstruct(signals=ss))[0]["canonical_signal_key"],"id:v1:s1")

    def test_hard_link_can_exceed_soft_48h_horizon(self):
        ff=[fill(1,time=r.ms(BOUNDARY+timedelta(days=4)))]
        rows=r.reconstruct_records([signal(binance_order_id="1")],ff,[],BOUNDARY,
             order_rows=orders_for(ff),collected_at=BOUNDARY+timedelta(days=5))
        self.assertEqual(lives(rows)[0]["match_status"],"MATCHED")

    def test_hard_link_cannot_own_execution_before_signal(self):
        self.assertEqual(lives(reconstruct(signals=[signal(h=3,binance_order_id="1")]))[0]["match_status"],"UNMATCHED")

    def test_hard_link_cannot_cross_symbol_or_side(self):
        for kw in ({"symbol":"ETHUSDT"},{"side":"SHORT"}):
            self.assertEqual(lives(reconstruct(signals=[signal(binance_order_id="1",**kw)]))[0]["match_status"],"UNMATCHED")

    def test_order_id_collision_keeps_symbols_independent(self):
        ff=pair()+[{**f,"symbol":"ETHUSDT"} for f in pair()]
        orders=orders_for(pair())+[{**o,"symbol":"ETHUSDT"} for o in orders_for(pair())]
        ss=[signal(binance_order_id="1"),signal("eth",symbol="ETHUSDT",binance_order_id="1")]
        rows=lives(reconstruct(ff,ss,orders=orders))
        self.assertEqual(len(rows),2)
        self.assertTrue(all(x["match_status"]=="MATCHED" for x in rows))
        r.assert_unique_ownership(rows)

    def test_explicit_trade_reference_conflict_rejected(self):
        self.assertEqual(lives(reconstruct(signals=[signal(binance_trade_id="999")]))[0]["match_status"],"UNMATCHED")
        self.assertEqual(lives(reconstruct(signals=[signal(binance_trade_id="1")]))[0]["match_status"],"MATCHED")

    def test_two_hard_claims_stay_ambiguous(self):
        ss=[signal(binance_order_id="1"),signal("b",binance_order_id="1")]
        self.assertEqual(lives(reconstruct(signals=ss))[0]["match_status"],"AMBIGUOUS")

    def test_terminal_fills_enable_execution_pnl_not_funding_finality(self):
        row=lives(reconstruct())[0]
        self.assertEqual(row["execution_finality"],"EXECUTION_FINAL")
        self.assertEqual(row["realized_pnl_finality"],"REALIZED_PNL_FINAL")
        self.assertEqual(row["commission_finality"],"COMMISSION_FINAL")
        self.assertEqual(row["execution_pnl_usdt"],"9.92")
        self.assertEqual(row["execution_r"],"0.992")
        self.assertEqual(row["funding_finality"],"FUNDING_PENDING")
        self.assertEqual(row["accounting_finalized"],"false")
        self.assertEqual(row["net_realized_pnl_usdt"],"")
        self.assertEqual(c.report_records([row])["execution_complete_matched"]["execution_pnl_usdt"],"9.92")

    def test_missing_fill_or_nonterminal_order_cannot_finalize(self):
        for status,qty in (("NEW","1"),("PARTIALLY_FILLED","1"),("FILLED","2")):
            orders=orders_for(pair());orders[0].update(status=status,executedQty=qty)
            row=lives(reconstruct(orders=orders))[0]
            self.assertEqual(row["execution_finality"],"EXECUTION_PENDING")
            self.assertEqual(row["execution_pnl_usdt"],"")

    def test_income_fee_without_fill_fee_is_provisional_only(self):
        ff=[{k:v for k,v in f.items() if k!="commission"} for f in pair()]
        ii=[income(1,kind="COMMISSION",amount="-0.04"),income(2,kind="COMMISSION",amount="-0.04")]
        row=lives(reconstruct(ff,incomes=ii))[0]
        self.assertEqual(row["commission_usdt"],"-0.08")
        self.assertEqual(row["commission_finality"],"COMMISSION_PENDING")
        self.assertEqual(row["execution_pnl_usdt"],"")

    def test_disagreement_and_non_usdt_do_not_finalize(self):
        cases=[(pair(),[income(amount="999")]),(pair(),[income(kind="COMMISSION",amount="-999")]),
               ([{**f,"commissionAsset":"BNB"} for f in pair()],[]),
               ([{**f,"marginAsset":"USDC"} for f in pair()],[])]
        for ff,ii in cases:
            row=lives(reconstruct(ff,incomes=ii))[0]
            self.assertEqual(row["execution_pnl_usdt"],"")
            self.assertEqual(c.report_records([row])["execution_complete_matched"]["rows"],0)

    def test_observed_funding_is_only_provisional_adjustment(self):
        row=lives(reconstruct(incomes=[income(0,kind="FUNDING_FEE",amount="-1",h=1,minute=30)]))[0]
        self.assertEqual(row["execution_pnl_usdt"],"9.92")
        self.assertEqual(row["provisional_net_realized_pnl_usdt"],"8.92")
        self.assertEqual(row["funding_finality"],"FUNDING_PENDING")
        self.assertEqual(c.report_records([row])["funding_reconciled_matched"]["rows"],0)

    def test_funding_rejects_contradictory_references(self):
        for kw in ({"orderId":999},{"tradeId":"999"},{"positionSide":"SHORT"},{"symbol":"ETHUSDT"}):
            row=lives(reconstruct(incomes=[income(0,kind="FUNDING_FEE",amount="-7",h=1,minute=30,**kw)]))[0]
            self.assertEqual(row["funding_usdt"],"")

    def test_funding_missing_order_reference_is_allowed(self):
        row=lives(reconstruct(incomes=[income(0,kind="FUNDING_FEE",amount="-1",h=1,minute=30)]))[0]
        self.assertEqual(row["funding_usdt"],"-1")

    def test_funding_hedge_position_side_when_supplied(self):
        ff=[fill(1),fill(2,pos="SHORT",side="SELL")]
        ii=[income(0,kind="FUNDING_FEE",amount="-1",h=1,minute=30,positionSide="SHORT")]
        rows=lives(reconstruct(ff,[signal(),signal("sh",side="SHORT")],ii))
        by_side={x["position_side"]:x["funding_usdt"] for x in rows}
        self.assertEqual(by_side,{"LONG":"","SHORT":"-1"})

    def test_terminal_zero_fill_orders_do_not_create_lifecycles(self):
        for status in ("CANCELED","EXPIRED"):
            orders=orders_for([fill(1)]);orders[0].update(status=status,executedQty="0")
            self.assertEqual(lives(reconstruct([],[],orders=orders)),[])

    def test_terminal_partial_entry_and_exit_use_executed_qty(self):
        for status in ("CANCELED","EXPIRED","EXPIRED_IN_MATCH"):
            for index in (0,1):
                orders=orders_for(pair());orders[index].update(status=status,origQty="50")
                row=lives(reconstruct(orders=orders))[0]
                self.assertEqual(row["entry_fill_qty"],"1")
                self.assertEqual(row["exit_fill_qty"],"1")
                self.assertEqual(row["execution_finality"],"EXECUTION_FINAL")
                self.assertEqual(row["execution_pnl_usdt"],"9.92")

    def test_canceled_partial_exit_does_not_close_remaining_position(self):
        ff=[fill(1,qty="2"),pair()[1]]
        orders=orders_for(ff);orders[1].update(status="CANCELED",origQty="2")
        row=lives(reconstruct(ff,orders=orders))[0]
        self.assertEqual(row["remaining_qty"],"1")
        self.assertEqual(row["execution_finality"],"EXECUTION_PENDING")
        self.assertEqual(row["position_terminal"],"OPEN")

    def test_invalid_long_and_short_stops_have_unknown_r(self):
        for side in ("LONG","SHORT"):
            ff=pair() if side=="LONG" else [fill(1,pos="SHORT",side="SELL"),fill(2,h=2,pos="SHORT",side="BUY",pnl="10")]
            for stop in ("0","-1","NaN","Infinity","bad","100","110" if side=="LONG" else "90"):
                row=lives(reconstruct(ff,[signal(side=side,stop_loss=stop)]))[0]
                self.assertEqual(row["execution_r"],"")
                self.assertIsNone(c.report_records([row])["execution_complete_matched"]["execution_r"])

    def test_report_invalid_risk_and_tampered_r_never_authoritative(self):
        row=lives(reconstruct())[0]
        for risk in ("","0","-1","NaN","Infinity","oops","20"):
            changed={**row,"initial_risk_usdt":risk,"execution_r":"999","net_realized_r":"999"}
            self.assertIsNone(c.report_records([changed])["execution_complete_matched"]["execution_r"])
        row["execution_r"]="999"
        self.assertEqual(c.report_records([row])["execution_complete_matched"]["execution_r"],"0.992")

    def test_report_requires_consistent_execution_dimensions(self):
        row=lives(reconstruct())[0]
        for key,val in (("commission_completeness","PARTIAL"),("execution_finality","EXECUTION_PENDING"),
                        ("realized_pnl_finality","REALIZED_PNL_PENDING"),("commission_finality","COMMISSION_PENDING"),
                        ("position_terminal","OPEN"),("match_status","PARTIAL"),("execution_pnl_usdt","999")):
            self.assertEqual(c.report_records([{**row,key:val}])["execution_complete_matched"]["rows"],0)

    def test_report_quantity_mismatch_has_no_r(self):
        row=lives(reconstruct())[0]
        for changed in ({"exit_fill_qty":"0.5"},{"remaining_qty":"1"},
                        {"entry_fill_qty":"2","initial_risk_usdt":"20"}):
            self.assertIsNone(c.report_records([{**row,**changed}])["execution_complete_matched"]["execution_r"])

    def test_report_labels_execution_and_funding_separately(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/"truth.csv"
            c.write_records(out,reconstruct())
            text=c.report(out)
            self.assertIn("EXCLUDES FUNDING AND OTHER ADJUSTMENTS",text)
            self.assertIn("Funding-reconciled rows: 0",text)
            self.assertIn("PROVISIONAL",text)
            self.assertIn("9.92",text)

    def test_late_income_retention_frontier_and_permanent_gap(self):
        with tempfile.TemporaryDirectory() as td:
            out,state=Path(td)/"truth.csv",Path(td)/"state.json"
            client=FixtureClient(pair());ss=r.signals_from_mappings([signal()],BOUNDARY)
            def go(day): return fixture_collection(client,ss,BOUNDARY,out,state,now=BOUNDARY+timedelta(days=day))
            go(88)
            client.incomes=[income(0,kind="FUNDING_FEE",amount="-1",h=1,minute=30)]
            self.assertEqual(lives(go(89))[0]["funding_usdt"],"-1")
            row=lives(go(90))[0]
            self.assertEqual(row["accounting_finality"],"ACCOUNTING_INCOMPLETE")
            self.assertEqual(row["income_reconciliation_status"],"UNRECOVERABLE")
            saved=json.loads(state.read_text())["pending_income"][row["lifecycle_id"]]
            self.assertEqual(saved["required_start_utc"],r.iso(stamp(1,5)))
            self.assertEqual(saved["status"],"UNRECOVERABLE")
            self.assertEqual(lives(go(91))[0]["income_reconciliation_status"],"UNRECOVERABLE")
            self.assertEqual(row["execution_pnl_usdt"],"9.92")

    def test_old_pending_and_new_records_advance_without_erasing_requirements(self):
        with tempfile.TemporaryDirectory() as td:
            out,state=Path(td)/"truth.csv",Path(td)/"state.json"
            client=FixtureClient(pair());ss=r.signals_from_mappings([signal()],BOUNDARY)
            fixture_collection(client,ss,BOUNDARY,out,state,now=BOUNDARY+timedelta(days=88))
            ff=[fill(3,time=r.ms(BOUNDARY+timedelta(days=90))),
                fill(4,side="SELL",pnl="5",time=r.ms(BOUNDARY+timedelta(days=90,hours=1)))]
            client.trades+=ff;client.orders=orders_for(client.trades)
            ss+=r.signals_from_mappings([signal("new",timestamp=r.iso(BOUNDARY+timedelta(days=90)))],BOUNDARY)
            rows=fixture_collection(client,ss,BOUNDARY,out,state,now=BOUNDARY+timedelta(days=90,hours=2))
            by_id={x["binance_trade_ids"]:x for x in lives(rows)}
            self.assertEqual(by_id["1|2"]["income_reconciliation_status"],"UNRECOVERABLE")
            self.assertEqual(by_id["3|4"]["income_reconciliation_status"],"PENDING")
            self.assertEqual(len(json.loads(state.read_text())["pending_income"]),2)
            self.assertEqual(c.report_records(rows)["incomplete_unrecoverable"],1)

    def test_pending_reconciliation_failure_preserves_all_durable_files(self):
        with tempfile.TemporaryDirectory() as td:
            out,state=Path(td)/"truth.csv",Path(td)/"state.json"
            client=FixtureClient(pair());ss=r.signals_from_mappings([signal()],BOUNDARY)
            def go(**kw):return fixture_collection(client,ss,BOUNDARY,out,state,now=NOW,**kw)
            go();paths=(out,state,c.evidence_path(out));before=[p.read_bytes() for p in paths]
            with patch.object(c,"reconcile_pending_income",side_effect=c.CollectionIncomplete("saturated")):
                with self.assertRaises(c.CollectionIncomplete):go()
            self.assertEqual([p.read_bytes() for p in paths],before)
            go()

    def test_income_old_pending_same_ms_saturation_pages_complete(self):
        with tempfile.TemporaryDirectory() as td:
            out,state=Path(td)/"truth.csv",Path(td)/"state.json"
            client=FixtureClient(pair());ss=r.signals_from_mappings([signal()],BOUNDARY)
            now=BOUNDARY+timedelta(days=20)
            fixture_collection(client,ss,BOUNDARY,out,state,now=now)
            client.incomes=[income(0,kind="FUNDING_FEE",amount="-0.001",h=1,minute=30,tranId=str(i)) for i in range(3001)]
            rows=fixture_collection(client,ss,BOUNDARY,out,state,now=now+timedelta(minutes=1))
            self.assertEqual(lives(rows)[0]["funding_usdt"],"-3.001")
            self.assertEqual(len(json.loads(c.evidence_path(out).read_text())["income"]),3001)

    def test_restart_keeps_unrecoverable_marker_after_csv_before_state(self):
        with tempfile.TemporaryDirectory() as td:
            out,state=Path(td)/"truth.csv",Path(td)/"state.json"
            client=FixtureClient(pair());ss=r.signals_from_mappings([signal()],BOUNDARY)
            fixture_collection(client,ss,BOUNDARY,out,state,now=BOUNDARY+timedelta(days=88))
            before=state.read_bytes()
            def crash(stage):
                if stage=="after_csv":raise RuntimeError("fixture")
            with self.assertRaises(RuntimeError):
                fixture_collection(client,ss,BOUNDARY,out,state,now=BOUNDARY+timedelta(days=90),checkpoint=crash)
            self.assertEqual(before,state.read_bytes())
            rows=fixture_collection(client,ss,BOUNDARY,out,state,now=BOUNDARY+timedelta(days=90))
            self.assertEqual(lives(rows)[0]["income_reconciliation_status"],"UNRECOVERABLE")

    def test_legacy_v2_state_migrates_from_durable_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            out,state=Path(td)/"truth.csv",Path(td)/"state.json"
            client=FixtureClient(pair());ss=r.signals_from_mappings([signal(binance_trade_id="1")],BOUNDARY)
            fixture_collection(client,ss,BOUNDARY,out,state,now=NOW)
            prior=json.loads(state.read_text());prior.pop("pending_income");prior["collector_version"]="2.0.0"
            state.write_text(json.dumps(prior))
            rows=fixture_collection(client,ss,BOUNDARY,out,state,now=NOW)
            self.assertEqual(lives(rows)[0]["match_status"],"MATCHED")
            self.assertTrue(json.loads(state.read_text())["pending_income"])

def position_snapshot(qty="0", side="LONG", when=BOUNDARY, end=None, symbol="BTCUSDT"):
    return {"symbol": symbol, "positionSide": side, "positionAmt": qty,
            "observed_from_utc": r.iso(when), "observed_through_utc": r.iso(end or when)}


class BoundaryProvenanceTests(OfflineOnly):
    def build(self, ff=None, snapshots=(), signals=None):
        ff = pair() if ff is None else ff
        return lives(r.reconstruct_records([signal()] if signals is None else signals,
                     ff, [], BOUNDARY, order_rows=orders_for(ff), collected_at=NOW,
                     position_snapshots=snapshots))

    def test_missing_boundary_snapshot_is_unknown_never_flat(self):
        row = self.build()[0]
        self.assertEqual(row["boundary_exposure_state"], "BOUNDARY_EXPOSURE_UNKNOWN")
        self.assertEqual(row["lifecycle_provenance"], "BOUNDARY_EXPOSURE_UNKNOWN")
        self.assertEqual(row["execution_finality"], "EXECUTION_PENDING")
        self.assertEqual(row["execution_pnl_usdt"], "")

    def test_original_carry_in_add_reduce_cannot_finalize(self):
        ff = [fill(1,time=r.ms(BOUNDARY)-1000),fill(2),fill(3,h=2,side="SELL",pnl="10")]
        row = self.build(ff)[0]
        self.assertEqual(row["binance_trade_ids"], "2|3")
        self.assertEqual(row["match_status"], "MATCHED")  # Provenance is a separate gate.
        self.assertEqual(row["execution_finality"], "EXECUTION_PENDING")
        self.assertEqual(c.report_records([row])["execution_complete_matched"]["rows"], 0)

    def test_preexisting_long_close_is_not_a_new_entry(self):
        row = self.build([fill(1,side="SELL",pnl="5")],[position_snapshot("1")])[0]
        self.assertEqual(row["entry_fill_count"], "0")
        self.assertEqual(row["exit_fill_count"], "1")
        self.assertEqual(row["remaining_qty"], "0")
        self.assertEqual(row["lifecycle_provenance"], "PRE_BOUNDARY")
        self.assertEqual(row["boundary_exposure_state"], "BOUNDARY_PREEXISTING_POSITION")
        self.assertEqual(row["execution_pnl_usdt"], "")

    def test_legacy_close_then_new_lifecycle_is_authoritative(self):
        ff = [fill(1,side="SELL",pnl="5"),fill(2,h=2),fill(3,h=3,side="SELL",pnl="10")]
        rows = self.build(ff,[position_snapshot("1")],[signal(h=2)])
        old,new = rows
        self.assertEqual(old["lifecycle_provenance"], "PRE_BOUNDARY")
        self.assertEqual(new["lifecycle_provenance"], "POST_BOUNDARY_AFTER_FLAT_RESET")
        self.assertEqual(new["flat_provenance_utc"], r.iso(stamp(1,5)))
        self.assertEqual(new["execution_finality"], "EXECUTION_FINAL")
        totals=c.report_records(rows)["execution_complete_matched"]
        self.assertEqual(totals["rows"],1)
        self.assertEqual(totals["execution_pnl_usdt"],"9.92")

    def test_unknown_first_sell_never_infers_entry_or_zero(self):
        ff=[fill(1,side="SELL",pnl="5"),fill(2,h=2),fill(3,h=3,side="SELL",pnl="10")]
        rows=self.build(ff,signals=[signal(h=2)])
        self.assertEqual(rows[0]["entry_fill_count"],"0")
        self.assertTrue(all(x["execution_finality"]=="EXECUTION_PENDING" for x in rows))

    def test_known_flat_boundary_allows_normal_execution(self):
        row=self.build(snapshots=[position_snapshot()])[0]
        self.assertEqual(row["boundary_exposure_state"],"BOUNDARY_FLAT_CONFIRMED")
        self.assertEqual(row["lifecycle_provenance"],"POST_BOUNDARY_AUTHORITATIVE")
        self.assertEqual(row["execution_pnl_usdt"],"9.92")

    def test_current_zero_does_not_retroactively_prove_boundary_flat(self):
        row=self.build(snapshots=[position_snapshot(when=NOW)])[0]
        self.assertEqual(row["boundary_exposure_state"],"BOUNDARY_EXPOSURE_UNKNOWN")
        self.assertEqual(row["execution_pnl_usdt"],"")

    def test_unknown_then_observed_zero_allows_only_later_entry(self):
        ff=pair()+[fill(3,h=4),fill(4,h=5,side="SELL",pnl="10")]
        rows=self.build(ff,[position_snapshot(when=stamp(3))],[signal(),signal("new",h=4)])
        self.assertEqual(rows[0]["execution_pnl_usdt"],"")
        self.assertEqual(rows[1]["execution_pnl_usdt"],"9.92")
        self.assertEqual(rows[1]["boundary_exposure_state"],"BOUNDARY_EXPOSURE_UNKNOWN")
        self.assertEqual(rows[1]["lifecycle_provenance"],"POST_BOUNDARY_AFTER_FLAT_RESET")

    def test_later_nonzero_snapshot_then_close_establishes_reset(self):
        ff=[fill(1,h=2,side="SELL",pnl="5"),fill(2,h=3),fill(3,h=4,side="SELL",pnl="10")]
        rows=self.build(ff,[position_snapshot("1",when=stamp(1))],[signal(h=3)])
        self.assertEqual(rows[0]["lifecycle_provenance"],"BOUNDARY_EXPOSURE_UNKNOWN")
        self.assertEqual(rows[1]["execution_pnl_usdt"],"9.92")

    def test_hedge_boundary_inventory_is_independent(self):
        ff=[fill(1,side="SELL",pnl="5"),fill(2,pos="SHORT",side="BUY",pnl="5"),
            fill(3,h=2),fill(4,h=3,side="SELL",pnl="10"),
            fill(5,h=2,pos="SHORT",side="SELL"),fill(6,h=3,pos="SHORT",side="BUY",pnl="10")]
        rows=self.build(ff,[position_snapshot("1"),position_snapshot("-2",side="SHORT")],
                        [signal(h=2),signal("sh",h=2,side="SHORT",stop_loss="110")])
        good=[x for x in rows if x["execution_pnl_usdt"]]
        self.assertEqual([(x["position_side"],x["binance_trade_ids"]) for x in good],[("LONG","3|4")])

    def test_one_way_short_reset_preserves_direction(self):
        ff=[fill(1,pos="BOTH",side="BUY",pnl="5"),fill(2,h=2,pos="BOTH",side="SELL"),
            fill(3,h=3,pos="BOTH",side="BUY",pnl="10")]
        oo=orders_for(ff)
        for o in oo:o["reduceOnly"]=o["side"]=="BUY"
        rows=lives(r.reconstruct_records([signal(h=2,side="SHORT",stop_loss="110")],ff,[],BOUNDARY,
                   collected_at=NOW,order_rows=oo,position_snapshots=[position_snapshot("-1",side="BOTH")]))
        self.assertEqual(rows[0]["entry_fill_count"],"0")
        self.assertEqual(rows[1]["execution_pnl_usdt"],"9.92")

    def test_snapshot_interval_with_fill_cannot_anchor(self):
        for start,end in [(stamp(1),stamp(1,10)),(stamp(1,5),stamp(1,5))]:
            row=self.build(snapshots=[position_snapshot(when=start,end=end)])[0]
            self.assertEqual(row["execution_pnl_usdt"],"")

    def test_reset_same_millisecond_as_new_entry_is_not_proven(self):
        ff=[fill(1,side="SELL",pnl="5"),fill(2),fill(3,h=2,side="SELL",pnl="10")]
        rows=self.build(ff,[position_snapshot("1")])
        self.assertTrue(all(x["execution_pnl_usdt"]=="" for x in rows))

    def test_restart_preserves_inventory_and_reset_provenance(self):
        with tempfile.TemporaryDirectory() as td:
            out,state=Path(td)/"truth.csv",Path(td)/"state.json"
            ff=[fill(1,side="SELL",pnl="5")]
            client=FixtureClient(ff)
            ss=r.signals_from_mappings([signal(h=2)],BOUNDARY)
            c.run_collection(client,ss,BOUNDARY,out,state,now=NOW,position_snapshots=[position_snapshot("1")])
            client.trades += [fill(2,h=2),fill(3,h=3,side="SELL",pnl="10")]
            client.orders=orders_for(client.trades)
            rows=c.run_collection(client,ss,BOUNDARY,out,state,now=NOW)
            self.assertEqual(lives(rows)[1]["execution_pnl_usdt"],"9.92")
            persisted=list(csv.DictReader(out.open(newline="",encoding="utf-8")))
            self.assertEqual(persisted,rows)
            self.assertEqual(json.loads(c.evidence_path(out).read_text())["position_snapshots"],[position_snapshot("1")])

    def test_preboundary_fills_never_enter_primary_pnl(self):
        ff=[fill(1,time=r.ms(BOUNDARY)-2000),fill(2,time=r.ms(BOUNDARY)-1000,side="SELL",pnl="900")]
        ff += [fill(3),fill(4,h=2,side="SELL",pnl="10")]
        rows=self.build(ff,[position_snapshot()])
        self.assertEqual(rows[0]["binance_trade_ids"],"3|4")
        self.assertEqual(c.report_records(rows)["execution_complete_matched"]["execution_pnl_usdt"],"9.92")

    def test_snapshot_mismatch_revokes_prior_lifecycle(self):
        row=self.build(snapshots=[position_snapshot(),position_snapshot("2",when=stamp(1,30))])[0]
        self.assertEqual(row["execution_pnl_usdt"],"")
        self.assertIn("POSITION_SNAPSHOT_RESET",row["accounting_issues"])

    def test_later_snapshot_mismatch_revokes_already_closed_projection(self):
        row=self.build(snapshots=[position_snapshot(),position_snapshot("1",when=stamp(3))])[0]
        self.assertEqual(row["execution_pnl_usdt"],"")
        self.assertIn("POSITION_SNAPSHOT_MISMATCH",row["accounting_issues"])

    def test_missing_side_and_conflicting_snapshots_rejected(self):
        for snaps in ([position_snapshot(side="")],[position_snapshot(),position_snapshot("1")]):
            with self.assertRaises(ValueError):self.build(snapshots=snaps)

    def test_collector_captures_current_read_without_retroactive_flatness(self):
        with tempfile.TemporaryDirectory() as td:
            client=FixtureClient(pair())
            client.positions=lambda:[{"symbol":"BTCUSDT","positionSide":"LONG","positionAmt":"0","updateTime":r.ms(BOUNDARY)}]
            out,state=Path(td)/"truth.csv",Path(td)/"state.json"
            rows=c.run_collection(client,r.signals_from_mappings([signal()],BOUNDARY),BOUNDARY,out,state,now=NOW)
            self.assertEqual(lives(rows)[0]["execution_pnl_usdt"],"")
            snapshot=json.loads(c.evidence_path(out).read_text())["position_snapshots"][0]
            self.assertEqual(snapshot["observed_from_utc"],r.iso(NOW))
            self.assertNotIn("updateTime",snapshot)


class InvalidAccountingEvidenceTests(OfflineOnly):
    def assert_invalid(self, ii):
        row=lives(reconstruct(incomes=ii))[0]
        self.assertEqual(row["accounting_evidence_status"],"ACCOUNTING_EVIDENCE_INVALID")
        self.assertEqual(row["execution_pnl_usdt"],"")
        self.assertEqual(row["realized_pnl_finality"],"REALIZED_PNL_PENDING")
        self.assertEqual(row["commission_finality"],"COMMISSION_PENDING")
        report=c.report_records([row])
        self.assertEqual(report["invalid_accounting_evidence"]["rows"],1)
        self.assertEqual(report["execution_complete_matched"]["rows"],0)
        return row

    def test_malformed_decimal(self):self.assert_invalid([income(amount="bad")])
    def test_nan(self):self.assert_invalid([income(amount="NaN")])
    def test_positive_infinity(self):self.assert_invalid([income(amount="+inf")])
    def test_negative_infinity(self):self.assert_invalid([income(amount="-inf")])
    def test_malformed_timestamp(self):self.assert_invalid([income(time="bad")])
    def test_fractional_or_boolean_timestamp(self):
        for value in (True,123.5,"123.5"):
            with self.subTest(value=value):self.assert_invalid([income(time=value)])
    def test_contradictory_symbol(self):self.assert_invalid([income(symbol="ETHUSDT")])
    def test_contradictory_order(self):self.assert_invalid([income(orderId=999)])
    def test_contradictory_trade_with_matching_order(self):self.assert_invalid([income(tid=999,orderId=2)])
    def test_conflicting_income_identity(self):self.assert_invalid([income(),income(amount="11")])
    def test_invalid_commission_asset(self):
        for asset in ("",None,"!bad","usdt"):
            with self.subTest(asset=asset):self.assert_invalid([income(kind="COMMISSION",amount="-0.04",asset=asset)])
    def test_invalid_commission_amount(self):self.assert_invalid([income(kind="COMMISSION",amount="NaN")])
    def test_contradictory_position_side(self):self.assert_invalid([income(positionSide="SHORT")])
    def test_contradictory_settlement_asset(self):self.assert_invalid([income(asset="USDC")])
    def test_invalid_income_type(self):self.assert_invalid([income(kind="")])

    def test_conclusively_unrelated_invalid_income_does_not_poison(self):
        for kw in ({"tradeId":"999"},{"symbol":"ETHUSDT","tradeId":"999"}):
            row=lives(reconstruct(incomes=[income(amount="bad",time="bad",**kw)]))[0]
            self.assertEqual(row["accounting_evidence_status"],"ACCOUNTING_EVIDENCE_VALID")
            self.assertEqual(row["execution_pnl_usdt"],"9.92")

    def test_cross_symbol_numeric_id_collision_is_not_contradiction(self):
        ff=pair()+[{**f,"symbol":"ETHUSDT"} for f in pair()]
        oo=orders_for(pair())+[{**o,"symbol":"ETHUSDT"} for o in orders_for(pair())]
        rows=lives(reconstruct(ff,[signal(),signal("eth",symbol="ETHUSDT")],
                              [income(symbol="ETHUSDT",amount="bad")],oo))
        by_symbol={x["symbol"]:x for x in rows}
        self.assertEqual(by_symbol["BTCUSDT"]["execution_pnl_usdt"],"9.92")
        self.assertEqual(by_symbol["ETHUSDT"]["accounting_evidence_status"],"ACCOUNTING_EVIDENCE_INVALID")

    def test_missing_fill_evidence_is_incomplete_not_invalid(self):
        ff=[{k:v for k,v in f.items() if k!="commission"} for f in pair()]
        row=lives(reconstruct(ff))[0]
        self.assertEqual(row["accounting_evidence_status"],"ACCOUNTING_EVIDENCE_INCOMPLETE")

    def test_non_usdt_commission_remains_incomplete(self):
        row=lives(reconstruct([{**f,"commissionAsset":"BNB"} for f in pair()]))[0]
        self.assertEqual(row["accounting_evidence_status"],"ACCOUNTING_EVIDENCE_INCOMPLETE")
        self.assertEqual(row["execution_pnl_usdt"],"")

    def test_report_rejects_invalid_marker_even_if_status_tampered(self):
        row=lives(reconstruct())[0]
        row["accounting_issues"] += "|INVALID_INCOME_VALUE"
        self.assertEqual(c.report_records([row])["execution_complete_matched"]["rows"],0)
        self.assertEqual(c.report_records([row])["invalid_accounting_evidence"]["rows"],1)

    def test_persisted_malformed_income_visible_on_restart(self):
        class MalformedClient(FixtureClient):
            def income_history(self, **p):
                return self.incomes if p["page"]==1 else []
        with tempfile.TemporaryDirectory() as td:
            out,state=Path(td)/"truth.csv",Path(td)/"state.json"
            client=MalformedClient(pair(),[income(time="bad",amount="NaN")])
            ss=r.signals_from_mappings([signal()],BOUNDARY)
            first=fixture_collection(client,ss,BOUNDARY,out,state,now=NOW)
            second=c.run_collection(client,ss,BOUNDARY,out,state,now=NOW)
            self.assertEqual(first,second)
            self.assertEqual(lives(second)[0]["accounting_evidence_status"],"ACCOUNTING_EVIDENCE_INVALID")
            self.assertIn("Invalid Accounting Evidence / REQUIRES_RECONCILIATION: 1",c.report(out))
            self.assertEqual(json.loads(c.evidence_path(out).read_text())["income"][0]["income"],"NaN")

    def test_conflicting_duplicate_retained_and_reported(self):
        with tempfile.TemporaryDirectory() as td:
            out,state=Path(td)/"truth.csv",Path(td)/"state.json"
            client=FixtureClient(pair(),[income()])
            ss=r.signals_from_mappings([signal()],BOUNDARY)
            fixture_collection(client,ss,BOUNDARY,out,state,now=NOW)
            client.incomes=[income(amount="11")]
            rows=c.run_collection(client,ss,BOUNDARY,out,state,now=NOW)
            self.assertEqual(lives(rows)[0]["accounting_evidence_status"],"ACCOUNTING_EVIDENCE_INVALID")
            self.assertEqual(len(json.loads(c.evidence_path(out).read_text())["income"]),2)


if __name__=="__main__":
    unittest.main()
