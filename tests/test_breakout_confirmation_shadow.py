from dataclasses import replace
import json
import sqlite3
from types import SimpleNamespace

import pandas as pd
import pytest

from core import breakout_confirmation_shadow as shadow
from core.research_telemetry import CandidateSnapshot, FailOpenResearchTelemetry
from core.signal_identity import canonical_signal_key


def candles(stamp, side="LONG", confirmed=True):
    end = pd.Timestamp(stamp).floor("h")
    frame = pd.DataFrame([
        {"open_time": end - pd.Timedelta(hours=21-i),
         "close_time": end - pd.Timedelta(hours=20-i, milliseconds=1),
         "high": 101., "low": 99., "close": 100.}
        for i in range(21)
    ])
    if confirmed:
        frame.loc[20, ["high", "low", "close"]] = [103., 97., 102. if side == "LONG" else 98.]
    return frame


@pytest.fixture
def collection(tmp_path):
    telemetry = FailOpenResearchTelemetry(tmp_path / "shadow.db")
    boundary = shadow.activate(telemetry, enabled=True, dry_run=False)
    stamp = (pd.Timestamp(boundary).ceil("h") + pd.Timedelta(seconds=5)).isoformat()
    return telemetry, boundary, stamp


def insert(collection, *, side="LONG", confirmed=True, stamp=None, symbol="BTCUSDT"):
    telemetry, boundary, default_stamp = collection
    stamp = stamp or default_stamp
    telemetry.start_run(stamp, stamp, stamp)
    snap = CandidateSnapshot(stamp, stamp, stamp, symbol, side, "SENT", signal_status="sent",
                             entry=100., sl=99. if side == "LONG" else 101., tp1=101., tp2=102.,
                             score=82, confidence=85, setup_strength=84, session="Asia",
                             market={"btc_regime": "RANGE"})
    source = shadow.capture_source(candles(stamp, side, confirmed), stamp, side, confirmed)
    captured = shadow.attach_observation(snap, source, boundary)
    assert telemetry.record_candidate(captured)
    key = canonical_signal_key(symbol=symbol, side=side, timestamp=stamp, entry=100.)
    return snap, captured, source, key


@pytest.mark.parametrize("side,confirmed", [("LONG", True), ("SHORT", True), ("LONG", False), ("SHORT", False)])
def test_signal_time_exact_definition_and_copy(side, confirmed):
    stamp = "2026-10-23T01:00:05Z"
    frame = candles(stamp, side, confirmed)
    result = shadow.capture_source(frame, stamp, side, confirmed)
    frame.loc[20, "close"] = 123456
    assert result["breakout_confirmed"] is confirmed
    assert len(result["source_candles"]) == 21
    assert result["source_candles"][-1]["close"] != 123456
    assert all(pd.Timestamp(r["close_time"]) <= pd.Timestamp(stamp) for r in result["source_candles"])
    assert result["definition_version"] == "BREAKOUT_CONFIRMATION_V1"


@pytest.mark.parametrize("side,value", [("LONG", 101.), ("SHORT", 99.)])
def test_equality_unconfirmed(side, value):
    stamp = "2026-10-23T01:00:05Z"
    frame = candles(stamp, side, False)
    frame.loc[20, "close"] = value
    assert shadow.capture_source(frame, stamp, side, False)["breakout_confirmed"] is False


@pytest.mark.parametrize("corruption", ["future", "gap", "nan", "timeframe", "stale", "mismatch"])
def test_no_lookahead_or_unreproducible_capture(corruption):
    stamp = "2026-10-23T01:00:05Z"
    frame = candles(stamp)
    timeframe = "1h"
    if corruption == "future":
        stamp = "2026-10-23T00:59:58Z"
    elif corruption == "gap":
        frame.loc[2, "open_time"] += pd.Timedelta(hours=1)
    elif corruption == "nan":
        frame.loc[2, "high"] = float("nan")
    elif corruption == "timeframe":
        timeframe = "4h"
    elif corruption == "stale":
        stamp = "2026-10-23T02:00:05Z"
    with pytest.raises(ValueError):
        shadow.capture_source(frame, stamp, "LONG", corruption != "mismatch", timeframe=timeframe)


def test_immutable_persistence_boundary_and_identity(collection):
    telemetry, boundary, _ = collection
    snap, captured, source, key = insert(collection)
    assert captured.decision == snap.decision == "SENT"
    assert (captured.score, captured.confidence, captured.setup_strength) == (82, 85, 84)
    assert not telemetry.record_candidate(replace(captured, shadows=()))
    assert shadow.activate(telemetry, enabled=True, dry_run=False) == boundary
    with telemetry.store.connect() as conn:
        for table in ("shadow_decisions", "shadow_boundaries"):
            for op in (f"DELETE FROM {table}", f"UPDATE {table} SET shadow_name='changed'"):
                with pytest.raises(sqlite3.IntegrityError):
                    conn.execute(op + " WHERE shadow_name=?", (shadow.NAME,))
        conn.execute("INSERT OR REPLACE INTO shadow_boundaries VALUES (?,?,?)", (shadow.NAME, "2000-01-01T00:00:00Z", boundary))
        conn.execute("INSERT OR REPLACE INTO shadow_decisions SELECT candidate_key,shadow_name,shadow_version,'UNCONFIRMED',reason,'{}',evaluated_at_utc FROM shadow_decisions WHERE shadow_name=?", (shadow.NAME,))
    assert shadow.activate(telemetry, enabled=True, dry_run=False) == boundary
    before = shadow.report(telemetry.path)
    source["source_candles"][0]["high"] = 99999
    after = shadow.report(telemetry.path)
    assert before == after
    assert after["records"][0]["canonical_signal_key"] == key
    assert after["gaps"] == []
    with pytest.raises(ValueError):
        shadow.attach_observation(replace(snap, canonical_signal_key="wrong"), source, boundary)


@pytest.mark.parametrize("kind", ["before", "backfill", "report", "disabled"])
def test_boundary_prevents_old_or_non_sent_population(collection, kind):
    telemetry, boundary, stamp = collection
    snapshot = CandidateSnapshot(stamp, stamp, stamp, "BTCUSDT", "LONG", "SENT", signal_status="sent", entry=100.)
    source = shadow.capture_source(candles(stamp), stamp, "LONG", True)
    if kind == "before":
        snapshot = replace(snapshot, timestamp_utc=(pd.Timestamp(boundary)-pd.Timedelta(seconds=1)).isoformat())
    elif kind == "backfill":
        snapshot = replace(snapshot, source_mode="BACKFILL")
    elif kind == "report":
        snapshot = replace(snapshot, decision="REPORT_ONLY", signal_status="report_only")
    else:
        boundary = ""
    assert shadow.attach_observation(snapshot, source, boundary) is snapshot


def test_no_activation_on_report_disabled_dry_run_or_missing_store(tmp_path):
    path = tmp_path / "not-created.db"
    assert shadow.report(path)["status"] == "NOT_ACTIVATED"
    assert not path.exists()
    telemetry = FailOpenResearchTelemetry(tmp_path / "empty.db")
    for enabled, dry_run in [(False, False), (True, True)]:
        assert shadow.activate(telemetry, enabled=enabled, dry_run=dry_run) == ""
        assert shadow.report(telemetry.path)["status"] == "NOT_ACTIVATED"
    assert shadow.activate(SimpleNamespace(available=False), enabled=True, dry_run=False) == ""


def test_later_canonical_terminal_link_and_ambiguous_execution(collection):
    telemetry, _, _ = collection
    _, _, _, key = insert(collection)
    initial = shadow.report(telemetry.path)
    assert initial["records"][0]["modeled_r"] is None
    outcomes = [{"canonical_signal_key": key, "lifecycle_state": "TP2_WIN", "lifecycle_r": 1.6}]
    later = shadow.report(telemetry.path, outcomes, [{"match_status": "AMBIGUOUS", "candidate_signal_keys": json.dumps([key])}])
    record = later["records"][0]
    assert record["modeled_r"] == 1.6
    assert record["execution_r"] is None
    assert record["execution_owner_state"] == "AMBIGUOUS"
    assert later["comparison"]["CONFIRMED"]["terminal_N"] == 1
    assert shadow.report(telemetry.path) == initial  # outcome join never rewrites observations


def test_authoritative_owned_execution_and_duplicate_quarantine(collection):
    telemetry, boundary, stamp = collection
    _, _, _, key = insert(collection)
    execution = dict(canonical_signal_key=key, symbol="BTCUSDT", side="LONG", match_status="MATCHED",
        entry_fill_qty="2", exit_fill_qty="2", entry_fill_price="100", signal_sl="99", initial_risk_usdt="2",
        execution_completeness="EXECUTION_COMPLETE", position_terminal="POSITION_TERMINAL", remaining_qty="0",
        prospective_start_utc=boundary, flat_provenance_utc=boundary, entry_fill_time_utc=stamp,
        lifecycle_provenance="POST_BOUNDARY_AUTHORITATIVE", boundary_exposure_state="BOUNDARY_FLAT_CONFIRMED",
        accounting_evidence_status="ACCOUNTING_EVIDENCE_VALID", execution_finality="EXECUTION_FINAL",
        realized_pnl_finality="REALIZED_PNL_FINAL", commission_finality="COMMISSION_FINAL",
        execution_pnl_usdt="3", execution_r="1.5", commission_usdt="-0.1", binance_trade_ids="1|2")
    owned = shadow.report(telemetry.path, execution_rows=[execution])["records"][0]
    assert owned["execution_r"] == 1.5
    assert owned["execution_only_net_usdt"] == 3
    assert owned["commission_usdt"] == -0.1
    for invalid in [replace_dict(execution, match_status="UNMATCHED"), replace_dict(execution, initial_risk_usdt="1"),
                    replace_dict(execution, execution_r="999"), replace_dict(execution, commission_finality="PENDING")]:
        assert shadow.report(telemetry.path, execution_rows=[invalid])["records"][0]["execution_r"] is None
    assert shadow.report(telemetry.path, execution_rows=[execution, execution])["records"][0]["execution_r"] is None


def replace_dict(value, **changes):
    return {**value, **changes}


def test_missing_capture_is_visible_and_frozen_baselines_reference_only(collection):
    telemetry, _, stamp = collection
    telemetry.start_run(stamp, stamp, stamp)
    telemetry.record_candidate(CandidateSnapshot(stamp, stamp, stamp, "BTCUSDT", "LONG", "SENT", signal_status="sent", entry=100.))
    result = shadow.report(telemetry.path)
    assert len(result["missing_observation_keys"]) == 1
    assert result["classification"] == "INSUFFICIENT_EVIDENCE"
    assert result["references_only"]["historical"] == {"N": 287, "net_r": 49.685052548339}
    assert result["references_only"]["frozen_prospective"] == {"N": 25, "net_r": 5.799125634998}
    assert result["records"] == []


def test_multiple_days_directions_and_winner_sacrifice_gates(collection):
    telemetry, _, start = collection
    outcomes = []
    for day in range(2):
        for side in ("LONG", "SHORT"):
            for flag in (True, False):
                stamp = (pd.Timestamp(start)+pd.Timedelta(days=day, hours=int(side == "SHORT"), minutes=int(not flag))).isoformat()
                _, _, _, key = insert(collection, side=side, confirmed=flag, stamp=stamp)
                outcomes.append(dict(canonical_signal_key=key, lifecycle_state="TP2_WIN" if flag else "ORIGINAL_SL", lifecycle_r=1.6 if flag else -1.))
    result = shadow.report(telemetry.path, outcomes)
    assert result["classification"] == "PROMISING"
    assert result["episode_awareness"]["benefit_distribution"] == "MULTIPLE_DAYS"
    assert "NOT_ESTABLISHED" in result["episode_awareness"]["independence"]
    assert result["comparison"]["delta_avg_r"] == pytest.approx(2.6)
    for row in outcomes:
        row["lifecycle_r"] *= -1
    assert shadow.report(telemetry.path, outcomes)["classification"] == "CONTRADICTORY"


def scanner_signal(stamp, flag=True):
    from cornix_agent import TradeSignal
    return TradeSignal(timestamp=pd.Timestamp(stamp).to_pydatetime(), symbol="BTCUSDT", watchlist_tier="A",
        tradingview_symbol="BINANCE:BTCUSDT.P", direction="LONG", entry=100., tp1=101.2, tp2=102., sl=99.,
        rr=2., confidence=85, score=82, support=98., resistance=103., regime="Trending", regime_details="fixture",
        market_session="Asia", htf_regime="Bullish", htf_alignment="Aligned", volume_spike=True, volume_ratio=1.5,
        atr_pct=1., mfi=55., mfi_confirmed=True, body_ratio=.7, opposite_wick_ratio=.1, atr_expansion_ratio=1.2,
        quality_flags="fixture", reason="fixture", breakout_confirmed=flag)


@pytest.mark.parametrize("flag", [True, False])
@pytest.mark.parametrize("broken_source", [False, True])
def test_scanner_sent_ranking_payload_and_fail_open(collection, tmp_path, monkeypatch, flag, broken_source):
    import cornix_agent as scanner
    telemetry, boundary, stamp = collection
    monkeypatch.setattr(scanner, "evaluate_new_signal", lambda *_a: SimpleNamespace(should_send_signal=True))
    outcomes = []
    for enabled in (False, True):
        signal = scanner_signal(stamp, flag)
        runner = scanner.AgentRunner.__new__(scanner.AgentRunner)
        runner.config = SimpleNamespace(dry_run=False, trend_timeframe="1h", entry_timeframe="15m", use_4h_regime_filter=False,
            use_daily_risk_guard=False, use_losing_streak_protection=False, score_threshold=70, min_confidence=70,
            min_rr=1.2, loss_cooldown_minutes=60, max_signals_per_direction_per_candle=10,
            max_major_correlated_signals=10, max_signals_per_scan=10, session_report_only_sessions=[],
            weak_symbol_report_only_symbols=[], enable_tier_c_report_only=False, london_long_report_only=False)
        runner.breakout_shadow_enabled = enabled
        runner.breakout_shadow_boundary = boundary
        runner.research_telemetry = FailOpenResearchTelemetry(tmp_path / f"scanner-{enabled}.db")
        if enabled:
            runner.breakout_shadow_boundary = shadow.activate(runner.research_telemetry, enabled=True, dry_run=False)
        runner.research_run_id = ""
        runner.research_same_scan_counts = {"LONG": 0, "SHORT": 0}
        runner.research_same_scan_sent_counts = {"LONG": 0, "SHORT": 0}
        runner.research_same_scan_sent_signals = []
        runner.research_scan_candle_utc = stamp
        runner.evaluate_research_candidate_correlation = lambda *_a: None
        runner.research_exposure_snapshot = lambda *_a: {}
        runner.entry_timing = SimpleNamespace()
        runner.cross_scan_price_history = {}
        runner.fear_greed_value = None
        frame = candles(stamp, confirmed=flag)
        if broken_source:
            frame.loc[0, "high"] = float("nan")
        runner.indicators = SimpleNamespace(add_indicators=lambda data: data)
        runner.data_client = SimpleNamespace(fetch_closed_klines=lambda *_a: frame)
        runner.scorer = SimpleNamespace(score=lambda *_a, **_k: signal)
        runner.risk_manager = SimpleNamespace(apply=lambda value: value)
        runner.evaluate_sr_trade_weight_shadow = lambda *_a: ""
        runner.evaluate_market_exhaustion_shadow = lambda *_a: None
        runner.evaluate_setup_strength_shadow = lambda *_a: None
        runner.evaluate_entry_timing_shadow = lambda *_a: None
        runner.evaluate_cross_scan_exposure_shadow = lambda *_a: None
        runner.get_btc_context = lambda: None
        runner.apply_btc_regime_filter = lambda *_a: False
        runner.is_in_cooldown = lambda *_a: False
        runner.loss_cooldown = SimpleNamespace(status=lambda: SimpleNamespace(active=False))
        runner.ai_commentary = SimpleNamespace(can_summarize=lambda *_a: False)
        runner.chart_exporter = SimpleNamespace(export=lambda *_a: None)
        calls = []
        runner.journal = SimpleNamespace(path=tmp_path / "absent.csv", is_recent_loss=lambda *_a: False,
            log_signal=lambda _signal, status, _reason: calls.append(("journal", status)))
        notifier = scanner.TelegramNotifier.__new__(scanner.TelegramNotifier)
        runner.notifier = SimpleNamespace(send_signal=lambda value: calls.append(("Cornix", notifier.build_cornix_message(value))) or True)
        runner.mark_sent = lambda value: calls.append(("marked", value.symbol))
        scanned = runner.scan_symbol(signal.symbol)
        competitor = replace(signal, symbol="ETHUSDT", score=80)
        ranking = [s.symbol for s in runner.select_top_candidates([competitor, scanned])]
        runner.process_candidates([scanned])
        fields = {k: v for k, v in vars(signal).items() if not k.startswith("research_")}
        outcomes.append((calls, ranking, fields))
        if enabled and not broken_source:
            assert "_breakout_source_v1" in signal.research_features
            recorded = shadow.report(runner.research_telemetry.path)
            assert len(recorded["records"]) == 1
            assert recorded["records"][0]["breakout_confirmed"] is flag
        if enabled and broken_source:
            assert len(shadow.report(runner.research_telemetry.path)["missing_observation_keys"]) == 1
    assert outcomes[0] == outcomes[1]
    assert outcomes[1][0][0] == ("journal", "sent")
    assert outcomes[1][1] == ["BTCUSDT", "ETHUSDT"]


def test_cli_read_only_no_database_creation_or_live_import(tmp_path, capsys):
    import sys
    import breakout_shadow_report
    path = tmp_path / "absent.db"
    assert breakout_shadow_report.main(["--db", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "NOT_ACTIVATED"
    assert not path.exists()
    assert result["references_only"]["frozen_prospective"]["N"] == 25
