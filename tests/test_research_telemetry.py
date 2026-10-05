from __future__ import annotations

import csv
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import core.research_telemetry as telemetry_module

from core.research_telemetry import (
    CandidateSnapshot,
    FailOpenResearchTelemetry,
    PreCandidateObservation,
    ResearchTelemetryStore,
    ShadowDecision,
    backfill_signals,
    health,
    make_candidate_key,
    make_observation_key,
    make_run_id,
    summary,
    utc_now,
)


def make_store(tmp_path: Path) -> ResearchTelemetryStore:
    return ResearchTelemetryStore(tmp_path / "research.db", busy_timeout_ms=75)


def make_snapshot(store: ResearchTelemetryStore, **overrides) -> CandidateSnapshot:
    stamp = utc_now()
    defaults = {
        "run_id": make_run_id(stamp, stamp),
        "timestamp_utc": stamp,
        "closed_candle_time_utc": stamp,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "decision": "SENT",
        "signal_status": "sent",
        "entry": 100.0,
        "sl": 99.0,
        "tp1": 101.2,
        "tp2": 102.0,
        "rr": 2.0,
        "score": 82,
        "confidence": 84,
        "setup_strength": 84,
        "features": {"rsi": 55, "mfi": 61, "atr": 1.0, "atr_expansion": 1.2},
        "market": {"btc_regime": "sideways", "session": "Asia"},
        "exposure": {"actual_open_same_side": 1},
    }
    defaults.update(overrides)
    snapshot = CandidateSnapshot(**defaults)
    store.start_run(snapshot.run_id, snapshot.timestamp_utc, snapshot.closed_candle_time_utc,
                    source_mode=snapshot.source_mode, source_name=snapshot.source_name,
                    source_version=snapshot.source_version)
    return snapshot


def make_observation(store: ResearchTelemetryStore, **overrides) -> PreCandidateObservation:
    stamp = store.boundary()
    defaults = {
        "run_id": make_run_id(stamp, stamp),
        "timestamp_utc": stamp,
        "scan_candle_utc": stamp,
        "symbol": "BTCUSDT",
        "stage": "SCORER",
        "category": "WAIT",
        "reason": "no_candidate",
    }
    defaults.update(overrides)
    observation = PreCandidateObservation(**defaults)
    store.start_run(observation.run_id, stamp, observation.scan_candle_utc)
    return observation


def test_db_unavailable_fails_open_and_live_path_continues(tmp_path: Path) -> None:
    bad_path = tmp_path / "directory-not-db"
    bad_path.mkdir()
    telemetry = FailOpenResearchTelemetry(bad_path)
    live_actions = []
    live_actions.append("sent")
    assert telemetry.record_candidate(CandidateSnapshot("r", utc_now(), utc_now(), "BTCUSDT", "LONG", "SENT")) is False
    assert live_actions == ["sent"]


def test_scanner_status_path_continues_when_snapshot_builder_throws() -> None:
    from cornix_agent import AgentRunner

    calls: list[str] = []

    class Journal:
        @staticmethod
        def log_signal(signal, status, reason):
            calls.append(status)

    runner = AgentRunner.__new__(AgentRunner)
    runner.journal = Journal()
    runner.evaluate_setup_strength_shadow = lambda *args: None
    runner.record_research_telemetry = lambda *args: (_ for _ in ()).throw(RuntimeError("broken research"))
    runner.research_same_scan_counts = {"LONG": 0, "SHORT": 0}
    runner.research_same_scan_sent_counts = {"LONG": 0, "SHORT": 0}
    signal = type("Signal", (), {"symbol": "BTCUSDT", "direction": "LONG"})()
    runner.log_signal_status(signal, "sent", "")
    assert calls == ["sent"]
    assert runner.research_same_scan_sent_counts["LONG"] == 1


def test_db_locked_fails_open(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    snapshot = make_snapshot(store)
    blocker = sqlite3.connect(store.path, timeout=.05)
    blocker.execute("BEGIN IMMEDIATE")
    telemetry = FailOpenResearchTelemetry(enabled=False)
    telemetry.store = store
    try:
        assert telemetry.record_candidate(snapshot) is False
    finally:
        blocker.rollback()
        blocker.close()


def test_duplicate_candidate_and_restart_are_idempotent(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    snapshot = make_snapshot(store)
    assert store.record_candidate(snapshot) is True
    assert store.record_candidate(snapshot) is False
    restarted = ResearchTelemetryStore(store.path)
    assert restarted.record_candidate(snapshot) is False
    assert summary(restarted)["candidates"] == 1


@pytest.mark.parametrize("decision", ["SENT", "REJECTED", "SKIPPED"])
def test_decision_populations_are_captured(tmp_path: Path, decision: str) -> None:
    store = make_store(tmp_path)
    snapshot = make_snapshot(store, decision=decision, signal_status=decision.lower())
    assert store.record_candidate(snapshot)
    assert summary(store)[decision.lower()] == 1


def test_feature_snapshot_is_immutable(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = make_snapshot(store, features={"rsi": 41})
    assert store.record_candidate(first)
    changed = CandidateSnapshot(**{**first.__dict__, "features": {"rsi": 99}})
    assert store.record_candidate(changed) is False
    with store.connect(read_only=True) as connection:
        assert connection.execute("SELECT rsi FROM candidate_features").fetchone()[0] == 41


def test_prospective_boundary_enforced_and_backfill_labeled(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    old = make_snapshot(store, timestamp_utc="2020-01-01T00:00:00Z", closed_candle_time_utc="2020-01-01T00:00:00Z")
    with pytest.raises(ValueError, match="boundary"):
        store.record_candidate(old)
    path = tmp_path / "signals.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["timestamp", "symbol", "side", "signal_status", "entry"])
        writer.writeheader()
        writer.writerow({"timestamp": "2020-01-01T00:00:00Z", "symbol": "ETHUSDT", "side": "SHORT", "signal_status": "sent", "entry": "200"})
    assert backfill_signals(store, path)["inserted"] == 1
    with store.connect(read_only=True) as connection:
        assert connection.execute("SELECT source_mode FROM candidates").fetchone()[0] == "HISTORICAL_BACKFILL"


def test_outcome_enrichment_idempotent_and_does_not_mutate_source(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    snapshot = make_snapshot(store)
    store.record_candidate(snapshot)
    with store.connect(read_only=True) as connection:
        key = connection.execute("SELECT canonical_signal_key FROM candidates").fetchone()[0]
    path = tmp_path / "signals.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["canonical_signal_key", "timestamp", "symbol", "side", "result", "result_r", "closed_at"])
        writer.writeheader()
        writer.writerow({"canonical_signal_key": key, "timestamp": snapshot.timestamp_utc, "symbol": "BTCUSDT", "side": "LONG", "result": "WIN", "result_r": "2", "closed_at": snapshot.timestamp_utc})
    before = path.read_bytes()
    first = store.enrich_outcomes(path)
    second = store.enrich_outcomes(path)
    assert (first["inserted"], first["updated"], first["unchanged"]) == (1, 0, 0)
    assert (second["inserted"], second["updated"], second["unchanged"]) == (0, 0, 1)
    assert path.read_bytes() == before
    with store.connect(read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM signal_outcomes").fetchone()[0] == 1


def test_execution_enrichment_idempotent(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.record_candidate(make_snapshot(store, canonical_signal_key="k"))
    path = tmp_path / "execution.csv"
    fields = ["canonical_signal_key", "match_status", "execution_finality", "accounting_evidence_status",
              "entry_fill_price", "exit_vwap", "gross_realized_pnl_usdt", "commission_usdt",
              "execution_pnl_usdt", "gross_realized_r", "execution_r", "commission_finality",
              "funding_finality", "accounting_finality"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({"canonical_signal_key": "k", "match_status": "MATCHED", "execution_finality": "EXECUTION_FINAL",
                         "accounting_evidence_status": "ACCOUNTING_EVIDENCE_VALID", "entry_fill_price": 100,
                         "exit_vwap": 102, "gross_realized_pnl_usdt": 2, "commission_usdt": -.1,
                         "execution_pnl_usdt": 1.9, "gross_realized_r": 2, "execution_r": 1.9,
                         "commission_finality": "COMMISSION_FINAL", "funding_finality": "FUNDING_FINAL",
                         "accounting_finality": "ACCOUNTING_FINAL"})
    first = store.enrich_execution(path)
    second = store.enrich_execution(path)
    assert (first["inserted"], first["updated"], first["unchanged"]) == (1, 0, 0)
    assert (second["inserted"], second["updated"], second["unchanged"]) == (0, 0, 1)
    with store.connect(read_only=True) as connection:
        row = connection.execute("SELECT COUNT(*),authoritative_eligible FROM execution_truth_links").fetchone()
        assert tuple(row) == (1, 1)


def test_wal_and_concurrent_read_write(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    snapshot = make_snapshot(store)
    errors: list[Exception] = []
    reader = store.connect(read_only=True)
    thread = threading.Thread(target=lambda: _record_thread(store, snapshot, errors))
    thread.start()
    reader.execute("SELECT COUNT(*) FROM candidates").fetchone()
    thread.join()
    reader.close()
    assert not errors
    assert health(store)["journal_mode"].lower() == "wal"


def _record_thread(store: ResearchTelemetryStore, snapshot: CandidateSnapshot, errors: list[Exception]) -> None:
    try:
        store.record_candidate(snapshot)
    except Exception as exc:  # pragma: no cover - assertion reports the exception
        errors.append(exc)


def test_schema_migration_is_safe(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    boundary = store.boundary()
    ResearchTelemetryStore(store.path).migrate()
    assert ResearchTelemetryStore(store.path).boundary() == boundary


def test_schema_migrates_v1_to_current_atomically(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    connection = sqlite3.connect(path)
    for statement in telemetry_module._sql_statements(telemetry_module.MIGRATION_1):
        connection.execute(statement)
    boundary = "2026-01-01T00:00:00Z"
    connection.execute("INSERT INTO research_meta VALUES (1,1,?,?,?)", (boundary, boundary, boundary))
    connection.commit()
    connection.close()
    store = ResearchTelemetryStore(path)
    with store.connect(read_only=True) as connection:
        assert connection.execute("SELECT schema_version FROM research_meta").fetchone()[0] == telemetry_module.SCHEMA_VERSION
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pre_candidate_observations'"
        ).fetchone()
    assert store.boundary() == boundary


def test_schema_v2_to_v3_preserves_enrichment_status(tmp_path: Path) -> None:
    path = tmp_path / "v2.db"
    connection = sqlite3.connect(path)
    for version in (1, 2):
        for statement in telemetry_module._sql_statements(telemetry_module.MIGRATIONS[version]):
            connection.execute(statement)
    boundary = "2026-01-01T00:00:00Z"
    connection.execute("INSERT INTO research_meta VALUES (1,2,?,?,?)", (boundary, boundary, boundary))
    connection.execute(
        "INSERT INTO enrichment_status VALUES (?,?,?,?)",
        ("outcomes:signals.csv", boundary, "100", ""),
    )
    connection.commit()
    connection.close()
    store = ResearchTelemetryStore(path)
    with store.connect(read_only=True) as connection:
        row = connection.execute("SELECT * FROM enrichment_status").fetchone()
        assert row["last_success_utc"] == boundary
        assert row["high_water"] == "100"
        assert row["last_attempt_utc"] is None
        assert row["source_rows_seen"] == 0
        assert connection.execute("SELECT schema_version FROM research_meta").fetchone()[0] == 3


def test_corrupt_db_fails_open(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"not sqlite")
    telemetry = FailOpenResearchTelemetry(path)
    assert telemetry.available is False
    observation = PreCandidateObservation("run", utc_now(), utc_now(), "BTCUSDT", "SCORER", "WAIT")
    assert telemetry.record_observation(observation) is False


def test_multiple_shadow_rows_per_candidate(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    snapshot = make_snapshot(store, shadows=(
        ShadowDecision("one", "1", "ALLOW"),
        ShadowDecision("two", "1", "CAUTION"),
    ))
    store.record_candidate(snapshot)
    with store.connect(read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM shadow_decisions").fetchone()[0] >= 2


def test_candidate_identity_preserves_run_and_candle_uniqueness() -> None:
    base = {"symbol": "BTCUSDT", "side": "LONG", "candidate_stage": "FINAL_CANDIDATE"}
    first = make_candidate_key(run_id="run1", closed_candle_time_utc="2026-01-01T00:00:00Z", **base)
    repeat = make_candidate_key(run_id="run1", closed_candle_time_utc="2026-01-01T00:00:00Z", **base)
    later = make_candidate_key(run_id="run2", closed_candle_time_utc="2026-01-01T01:00:00Z", **base)
    assert first == repeat
    assert first != later


def test_candidate_identity_canonicalizes_timezone_precision_and_stage() -> None:
    common = {"run_id": "run", "symbol": "BTCUSDT", "side": "LONG"}
    utc = make_candidate_key(closed_candle_time_utc="2026-01-01T00:00:00.999999Z", candidate_stage="FINAL", **common)
    offset = make_candidate_key(closed_candle_time_utc="2026-01-01T07:00:00+07:00", candidate_stage="FINAL", **common)
    rejected_stage = make_candidate_key(closed_candle_time_utc="2026-01-01T00:00:00Z", candidate_stage="QUALITY_FILTER", **common)
    short = make_candidate_key(closed_candle_time_utc="2026-01-01T00:00:00Z", candidate_stage="FINAL", **{**common, "side": "SHORT"})
    other_symbol = make_candidate_key(closed_candle_time_utc="2026-01-01T00:00:00Z", candidate_stage="FINAL", **{**common, "symbol": "ETHUSDT"})
    assert utc == offset
    assert rejected_stage != utc
    assert short != utc
    assert other_symbol != utc


def test_boundary_exact_and_after_are_valid_and_historical_cannot_overwrite(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    boundary = store.boundary()
    exact = make_snapshot(store, timestamp_utc=boundary, closed_candle_time_utc=boundary)
    assert store.record_candidate(exact)
    historical = CandidateSnapshot(**{
        **exact.__dict__,
        "decision": "REJECTED",
        "source_mode": "HISTORICAL_BACKFILL",
        "source_name": "old.csv",
    })
    assert store.record_candidate(historical) is False
    with store.connect(read_only=True) as connection:
        row = connection.execute("SELECT decision,source_mode FROM candidates").fetchone()
        assert tuple(row) == ("SENT", "PROSPECTIVE")


def test_historical_backfill_is_idempotent_when_source_rows_reorder(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    path = tmp_path / "signals.csv"
    fields = ["timestamp", "symbol", "side", "signal_status", "entry"]
    rows = [
        {"timestamp": "2020-01-01T00:00:00Z", "symbol": "BTCUSDT", "side": "LONG", "signal_status": "sent", "entry": "100"},
        {"timestamp": "2020-01-01T01:00:00Z", "symbol": "ETHUSDT", "side": "SHORT", "signal_status": "sent", "entry": "200"},
    ]
    for source_rows in (rows, list(reversed(rows))):
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(source_rows)
        backfill_signals(store, path)
    with store.connect(read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 2


def test_module_has_no_telegram_cornix_or_binance_calls() -> None:
    source = Path("core/research_telemetry.py").read_text(encoding="utf-8").lower()
    assert "api.telegram.org" not in source
    assert "send_signal(" not in source
    assert "fapi.binance.com" not in source


def test_duplicate_open_row_cannot_regress_resolved_outcome(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.record_candidate(make_snapshot(store, canonical_signal_key="signal"))
    path = tmp_path / "outcomes.csv"
    fields = ["canonical_signal_key", "timestamp", "symbol", "side", "result", "result_r", "closed_at"]
    stamp = utc_now()
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({"canonical_signal_key": "signal", "timestamp": stamp, "symbol": "BTCUSDT", "side": "LONG", "result": "WIN", "result_r": "2", "closed_at": stamp})
        writer.writerow({"canonical_signal_key": "signal", "timestamp": stamp, "symbol": "BTCUSDT", "side": "LONG", "result": "OPEN"})
    store.enrich_outcomes(path)
    with store.connect(read_only=True) as connection:
        row = connection.execute("SELECT result,modeled_r FROM signal_outcomes").fetchone()
        assert tuple(row) == ("WIN", 2.0)


def test_pending_execution_cannot_regress_authoritative_execution(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.record_candidate(make_snapshot(store, canonical_signal_key="signal"))
    path = tmp_path / "execution.csv"
    fields = ["canonical_signal_key", "match_status", "execution_finality", "accounting_evidence_status",
              "commission_finality", "funding_finality", "accounting_finality", "execution_r"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({"canonical_signal_key": "signal", "match_status": "MATCHED", "execution_finality": "EXECUTION_FINAL",
                         "accounting_evidence_status": "ACCOUNTING_EVIDENCE_VALID", "commission_finality": "COMMISSION_FINAL",
                         "funding_finality": "FUNDING_FINAL", "accounting_finality": "ACCOUNTING_FINAL", "execution_r": "1.8"})
        writer.writerow({"canonical_signal_key": "signal", "match_status": "AMBIGUOUS", "execution_finality": "EXECUTION_PENDING"})
    store.enrich_execution(path)
    with store.connect(read_only=True) as connection:
        row = connection.execute("SELECT execution_status,authoritative_eligible,execution_net_r FROM execution_truth_links").fetchone()
        assert tuple(row) == ("MATCHED", 1, 1.8)


def test_future_schema_rejected_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    connection = sqlite3.connect(path)
    connection.execute("""CREATE TABLE research_meta (
        singleton INTEGER PRIMARY KEY, schema_version INTEGER, prospective_start_utc TEXT,
        created_at_utc TEXT, last_migration_utc TEXT)""")
    connection.execute("INSERT INTO research_meta VALUES (1,99,'2026-01-01T00:00:00Z','x','x')")
    connection.commit()
    before = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    connection.close()
    with pytest.raises(RuntimeError, match="newer"):
        ResearchTelemetryStore(path)
    connection = sqlite3.connect(path)
    after = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    connection.close()
    assert after == before


def test_pre_candidate_wait_restart_replay_and_next_candle(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = make_observation(store)
    assert store.record_observation(first) is True
    assert ResearchTelemetryStore(store.path).record_observation(first) is False
    next_candle = "2099-01-01T01:00:00Z"
    later = make_observation(store, scan_candle_utc=next_candle)
    assert later.resolved_key() != first.resolved_key()
    assert store.record_observation(later) is True
    assert summary(store)["pre_candidate_observations"] == 2


def test_pre_candidate_observation_identity_normalizes_time() -> None:
    common = {"run_id": "run", "symbol": "btcusdt", "stage": "scorer", "category": "wait"}
    utc = make_observation_key(scan_candle_utc="2026-01-01T00:00:00.999Z", **common)
    offset = make_observation_key(scan_candle_utc="2026-01-01T07:00:00+07:00", **common)
    assert utc == offset


@pytest.mark.parametrize(
    "category",
    ["INVALID_ATR", "REGIME_NO_TRADE", "BELOW_DIRECTIONAL_THRESHOLD", "MARKET_DATA_FAILURE"],
)
def test_pre_candidate_categories_and_health(tmp_path: Path, category: str) -> None:
    store = make_store(tmp_path)
    observation = make_observation(store, category=category, stage="TEST", reason=category.lower())
    assert store.record_observation(observation)
    status = health(store)
    assert status["pre_candidate_observations"] == 1
    assert status["latest_pre_candidate_timestamp"] == telemetry_module.normalize_utc(store.boundary())
    assert status["pre_candidate_by_reason"] == {category: 1}
    with store.connect(read_only=True) as connection:
        row = connection.execute(
            "SELECT outcome,category,score_long,score_short FROM pre_candidate_observations"
        ).fetchone()
        assert tuple(row) == ("WAIT", category, None, None)


def test_pre_candidate_boundary_and_locked_db_fail_open(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    old = make_observation(store, timestamp_utc="2020-01-01T00:00:00Z")
    with pytest.raises(ValueError, match="boundary"):
        store.record_observation(old)
    current = make_observation(store)
    blocker = sqlite3.connect(store.path, timeout=.05)
    blocker.execute("BEGIN IMMEDIATE")
    adapter = FailOpenResearchTelemetry(enabled=False)
    adapter.store = store
    try:
        assert adapter.record_observation(current) is False
    finally:
        blocker.rollback()
        blocker.close()


@pytest.mark.parametrize("decision", ["SENT", "REJECTED", "REPORT_ONLY"])
def test_correlation_snapshot_for_all_constructed_decisions(tmp_path: Path, decision: str) -> None:
    from cornix_agent import AgentRunner
    from core.cross_scan_exposure_shadow import ExposureState, OpenExposure, ShadowRule

    runner = AgentRunner.__new__(AgentRunner)
    runner.research_correlation_rule = ShadowRule(
        correlation_threshold=.75, correlation_lookback_bars=10, correlation_min_observations=4
    )
    index = pd.date_range("2026-01-01", periods=8, freq="h", tz="UTC")
    runner.cross_scan_price_history = {
        "BTCUSDT": pd.Series([100, 101, 103, 102, 105, 108, 107, 110], index=index),
        "ETHUSDT": pd.Series([200, 202, 206, 204, 210, 216, 214, 220], index=index),
    }
    exposure = OpenExposure("open-key", "ETHUSDT", "LONG", index[0])
    runner.research_cross_scan_state = ExposureState((exposure,), (exposure,))
    runner.research_correlation_state_error = ""
    signal = SimpleNamespace(symbol="BTCUSDT", direction="LONG", research_exposure={})
    runner.evaluate_research_candidate_correlation(signal)
    assert signal.research_exposure["correlation_evaluated"] is True
    assert signal.research_exposure["correlated_open_count"] == 1
    assert signal.research_exposure["max_pair_correlation"] == pytest.approx(1.0)
    assert signal.research_exposure["correlated_signal_keys"] == ["open-key"]
    store = make_store(tmp_path)
    snapshot = make_snapshot(store, decision=decision, signal_status=decision.lower(), exposure=signal.research_exposure)
    assert store.record_candidate(snapshot)
    with store.connect(read_only=True) as connection:
        row = connection.execute(
            "SELECT max_pair_correlation,correlated_open_count,cluster_id,representative_signal_key,exposure_extras_json "
            "FROM exposure_snapshots"
        ).fetchone()
        assert row[0] == pytest.approx(1.0)
        assert tuple(row[1:4]) == (1, signal.research_exposure["cluster_id"], "open-key")
        assert '"correlation_evaluated":true' in row[4]


def test_unavailable_correlation_is_null_and_does_not_mutate_signal_csv(tmp_path: Path) -> None:
    from cornix_agent import AgentRunner
    from core.cross_scan_exposure_shadow import ExposureState, OpenExposure, ShadowRule

    journal = tmp_path / "signals.csv"
    journal.write_text("timestamp,symbol\n", encoding="utf-8")
    before = journal.read_bytes()
    runner = AgentRunner.__new__(AgentRunner)
    runner.research_correlation_rule = ShadowRule(correlation_min_observations=48)
    exposure = OpenExposure("open-key", "ETHUSDT", "LONG", pd.Timestamp.now(tz="UTC"))
    runner.research_cross_scan_state = ExposureState((exposure,), (exposure,))
    runner.research_correlation_state_error = ""
    runner.cross_scan_price_history = {"BTCUSDT": pd.Series(dtype=float)}
    signal = SimpleNamespace(symbol="BTCUSDT", direction="LONG", research_exposure={})
    runner.evaluate_research_candidate_correlation(signal)
    assert signal.research_exposure["correlation_evaluated"] is False
    assert signal.research_exposure["correlated_open_count"] is None
    assert signal.research_exposure["max_pair_correlation"] is None
    assert signal.research_exposure["unavailable_reason"]
    assert journal.read_bytes() == before


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("invalid_atr", "INVALID_ATR"),
        ("regime", "REGIME_NO_TRADE"),
        ("scores", "BELOW_DIRECTIONAL_THRESHOLD"),
    ],
)
def test_scorer_pre_candidate_exits_emit_observation_without_changing_decision(
    mode: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cornix_agent as scanner

    config = SimpleNamespace(
        watchlist_tiers={}, volume_spike_multiplier=2.0,
        fear_greed_greed_threshold=80, fear_greed_fear_threshold=20, fear_greed_score_adjustment=5,
        use_candle_body_filter=False, use_wick_filter=False, use_atr_expansion_filter=False,
        use_mfi_filter=False, min_volume_ratio=0.0, min_atr_pct=0.0,
    )
    scorer = scanner.SignalScorer.__new__(scanner.SignalScorer)
    scorer.config = config
    scorer.support_resistance = SimpleNamespace(calculate=lambda _df: (90.0, 110.0))
    regime_name = "Sideway" if mode == "regime" else "Trending"
    scorer.regime_detector = SimpleNamespace(
        detect=lambda _df: scanner.MarketRegime(regime_name, "test")
    )
    if mode == "scores":
        scorer._direction_score = lambda *_args: 0
    rows = []
    for index in range(22):
        rows.append(
            {
                "close_time": pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(hours=index),
                "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                "atr14": float("nan") if mode == "invalid_atr" else 1.0,
                "atr_pct": 1.0, "mfi": 50.0, "volume_sma20": 100.0, "volume": 100.0,
                "ema20": 100.0, "ema50": 100.0, "rsi14": 50.0,
            }
        )
    df_1h = pd.DataFrame(rows)
    df_15m = pd.DataFrame([{"close": 100.0, "ema9": 100.0, "ema21": 100.0, "rsi14": 50.0}])
    monkeypatch.setattr(
        scanner,
        "calculate_wave_score",
        lambda *_args, **_kwargs: {"wave_score": 0, "structure": "unclear", "possible_phase": "unknown", "notes": []},
    )
    captured: list[tuple[str, dict[str, object]]] = []
    result = scorer.score("BTCUSDT", df_1h, df_15m, observation_callback=lambda category, values: captured.append((category, values)))
    assert result is None
    assert captured and captured[0][0] == expected
    # Callback failure is independently fail-open and cannot create a candidate.
    assert scorer.score(
        "BTCUSDT",
        df_1h,
        df_15m,
        observation_callback=lambda *_args: (_ for _ in ()).throw(RuntimeError("db unavailable")),
    ) is None


def test_scan_market_data_failure_emits_pre_candidate_observation(tmp_path: Path) -> None:
    import requests
    from cornix_agent import AgentRunner
    from core.cross_scan_exposure_shadow import ShadowRule

    captured: list[tuple[str, str]] = []
    runner = AgentRunner.__new__(AgentRunner)
    runner.maybe_send_daily_summary = lambda: None
    runner.ai_commentary = SimpleNamespace(reset_run_budget=lambda: None)
    runner.research_telemetry = SimpleNamespace(start_run=lambda *_args: True)
    runner.research_correlation_rule = ShadowRule()
    journal = tmp_path / "signals.csv"
    runner.journal = SimpleNamespace(path=journal)
    runner.cross_scan_exposure_shadow_logger = None
    runner.config = SimpleNamespace(use_fear_greed=False, watchlist=["BTCUSDT"], request_delay_seconds=0)
    runner.scan_symbol = lambda _symbol: (_ for _ in ()).throw(requests.HTTPError("503"))
    runner.record_pre_candidate_observation = lambda category, _values, symbol="": captured.append((category, symbol))
    runner.process_candidates = lambda candidates: captured.append(("PROCESSED", str(len(candidates))))
    runner.scan_once()
    assert captured == [("MARKET_DATA_FAILURE", "BTCUSDT"), ("PROCESSED", "0")]


def test_failed_migration_rolls_back_all_schema_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "failed.db"
    monkeypatch.setitem(
        telemetry_module.MIGRATIONS,
        1,
        telemetry_module.MIGRATION_1 + "\nCREATE TABL broken;",
    )
    with pytest.raises(sqlite3.OperationalError):
        ResearchTelemetryStore(path)
    connection = sqlite3.connect(path)
    tables = list(connection.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    connection.close()
    assert tables == []


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_outcome_open_resolves_and_terminal_result_cannot_regress(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    snapshot = make_snapshot(store, canonical_signal_key="outcome-key")
    store.record_candidate(snapshot)
    path = tmp_path / "signals.csv"
    fields = ["canonical_signal_key", "timestamp", "result", "result_r", "closed_at", "hit_target"]
    _write_csv(path, fields, [{"canonical_signal_key": "outcome-key", "timestamp": snapshot.timestamp_utc, "result": "OPEN"}])
    assert store.enrich_outcomes(path)["inserted"] == 1
    _write_csv(path, fields, [{
        "canonical_signal_key": "outcome-key", "timestamp": snapshot.timestamp_utc,
        "result": "WIN", "result_r": 2, "closed_at": snapshot.timestamp_utc, "hit_target": "TP2",
    }])
    assert store.enrich_outcomes(path)["updated"] == 1
    _write_csv(path, fields, [{"canonical_signal_key": "outcome-key", "timestamp": snapshot.timestamp_utc, "result": "OPEN"}])
    assert store.enrich_outcomes(path)["unchanged"] == 1
    with store.connect(read_only=True) as connection:
        row = connection.execute(
            "SELECT result,modeled_r,tp1_hit,tp2_hit,sl_hit FROM signal_outcomes"
        ).fetchone()
        assert tuple(row) == ("WIN", 2.0, 1, 1, None)


def test_report_only_and_rejected_outcomes_require_real_source_evidence(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    report = make_snapshot(
        store, canonical_signal_key="report-key", decision="REPORT_ONLY", signal_status="session_risk_report_only"
    )
    rejected = make_snapshot(
        store, canonical_signal_key="rejected-key", symbol="ETHUSDT", decision="REJECTED",
        signal_status="logged_quality_filter"
    )
    store.record_candidate(report)
    store.record_candidate(rejected)
    signals = tmp_path / "signals.csv"
    fields = ["canonical_signal_key", "timestamp", "result", "net_r_estimate", "closed_at"]
    _write_csv(signals, fields, [
        {"canonical_signal_key": "report-key", "timestamp": report.timestamp_utc, "result": "WIN", "net_r_estimate": 1, "closed_at": report.timestamp_utc},
        {"canonical_signal_key": "rejected-key", "timestamp": rejected.timestamp_utc, "result": "SKIPPED"},
    ])
    result = store.enrich_outcomes(signals)
    assert result["matched_report_only"] == 1
    assert result["matched_rejected"] == 0
    with store.connect(read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM signal_outcomes").fetchone()[0] == 1
    rejected_shadow = tmp_path / "rejected_outcome_shadow.csv"
    _write_csv(
        rejected_shadow,
        ["canonical_signal_key", "timestamp_utc", "hypothetical_outcome", "hypothetical_r", "close_timestamp"],
        [{
            "canonical_signal_key": "rejected-key", "timestamp_utc": rejected.timestamp_utc,
            "hypothetical_outcome": "LOSS", "hypothetical_r": -1, "close_timestamp": rejected.timestamp_utc,
        }],
    )
    result = store.enrich_outcomes(rejected_shadow)
    assert result["matched_rejected"] == 1
    with store.connect(read_only=True) as connection:
        assert tuple(connection.execute(
            "SELECT result,modeled_r FROM signal_outcomes WHERE canonical_signal_key='rejected-key'"
        ).fetchone()) == ("LOSS", -1.0)


def test_execution_unknowns_stay_null_and_authoritative_never_regresses(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.record_candidate(make_snapshot(store, canonical_signal_key="execution-key"))
    path = tmp_path / "execution.csv"
    fields = [
        "canonical_signal_key", "match_status", "execution_finality", "accounting_evidence_status",
        "entry_fill_price", "exit_vwap", "gross_realized_pnl_usdt", "commission_usdt",
        "execution_pnl_usdt", "gross_realized_r", "execution_r", "commission_finality",
        "funding_finality", "accounting_finality",
    ]
    _write_csv(path, fields, [{
        "canonical_signal_key": "execution-key", "match_status": "PARTIAL",
        "execution_finality": "EXECUTION_PENDING", "entry_fill_price": 100,
    }])
    assert store.enrich_execution(path)["inserted"] == 1
    with store.connect(read_only=True) as connection:
        row = connection.execute(
            "SELECT actual_entry_vwap,actual_exit_vwap,execution_pnl,execution_net_r FROM execution_truth_links"
        ).fetchone()
        assert tuple(row) == (100.0, None, None, None)
    _write_csv(path, fields, [{
        "canonical_signal_key": "execution-key", "match_status": "MATCHED",
        "execution_finality": "EXECUTION_FINAL", "accounting_evidence_status": "ACCOUNTING_EVIDENCE_VALID",
        "entry_fill_price": 100, "exit_vwap": 102, "gross_realized_pnl_usdt": 2,
        "commission_usdt": -.1, "execution_pnl_usdt": 1.9, "gross_realized_r": 2,
        "execution_r": 1.9, "commission_finality": "COMMISSION_FINAL",
        "funding_finality": "FUNDING_FINAL", "accounting_finality": "ACCOUNTING_FINAL",
    }])
    assert store.enrich_execution(path)["updated"] == 1
    _write_csv(path, fields, [{
        "canonical_signal_key": "execution-key", "match_status": "PARTIAL",
        "execution_finality": "EXECUTION_PENDING",
    }])
    assert store.enrich_execution(path)["unchanged"] == 1
    with store.connect(read_only=True) as connection:
        row = connection.execute(
            "SELECT execution_status,authoritative_eligible,execution_net_r FROM execution_truth_links"
        ).fetchone()
        assert tuple(row) == ("MATCHED", 1, 1.9)


def test_enrichment_status_distinguishes_no_data_healthy_and_error(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    empty = tmp_path / "empty.csv"
    _write_csv(empty, ["canonical_signal_key", "result"], [])
    assert store.enrich_outcomes(empty)["source_rows"] == 0
    assert health(store)["enrichment_sources"]["outcomes:empty.csv"]["status"] == "NO_DATA"
    store.record_candidate(make_snapshot(store, canonical_signal_key="healthy-key"))
    populated = tmp_path / "populated.csv"
    _write_csv(populated, ["canonical_signal_key", "result"], [{"canonical_signal_key": "healthy-key", "result": "OPEN"}])
    store.enrich_outcomes(populated)
    assert health(store)["enrichment_sources"]["outcomes:populated.csv"]["status"] == "HEALTHY"
    missing = tmp_path / "missing.csv"
    with pytest.raises(FileNotFoundError):
        store.enrich_outcomes(missing)
    error = health(store)["enrichment_sources"]["outcomes:missing.csv"]
    assert error["status"] == "ERROR"
    assert "FileNotFoundError" in error["last_error"]


def test_locked_enrichment_fails_without_source_or_candidate_mutation(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    snapshot = make_snapshot(store, canonical_signal_key="locked-key")
    store.record_candidate(snapshot)
    path = tmp_path / "signals.csv"
    _write_csv(path, ["canonical_signal_key", "result"], [{"canonical_signal_key": "locked-key", "result": "OPEN"}])
    before = path.read_bytes()
    blocker = sqlite3.connect(store.path, timeout=.05)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            store.enrich_outcomes(path)
    finally:
        blocker.rollback()
        blocker.close()
    assert path.read_bytes() == before
    with store.connect(read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM signal_outcomes").fetchone()[0] == 0
        assert connection.execute("SELECT decision FROM candidates").fetchone()[0] == "SENT"


def test_corrupt_db_enrichment_cli_fails_nonzero(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not sqlite")
    source = tmp_path / "signals.csv"
    _write_csv(source, ["canonical_signal_key", "result"], [])
    before = source.read_bytes()
    assert telemetry_module.main(["--db", str(corrupt), "enrich-outcomes", "--signals", str(source)]) == 1
    assert source.read_bytes() == before


def test_enrichment_uses_no_network_and_does_not_change_candidate_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    store = make_store(tmp_path)
    snapshot = make_snapshot(store, canonical_signal_key="offline-key", score=91)
    store.record_candidate(snapshot)
    path = tmp_path / "signals.csv"
    _write_csv(path, ["canonical_signal_key", "result"], [{"canonical_signal_key": "offline-key", "result": "OPEN"}])
    monkeypatch.setattr(socket, "socket", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network used")))
    store.enrich_outcomes(path)
    with store.connect(read_only=True) as connection:
        row = connection.execute("SELECT decision,score FROM candidates").fetchone()
        assert tuple(row) == ("SENT", 91.0)


def test_research_enrichment_systemd_units_are_isolated_oneshot() -> None:
    service = Path("deploy/systemd/crypto-research-enrichment.service").read_text(encoding="utf-8")
    timer = Path("deploy/systemd/crypto-research-enrichment.timer").read_text(encoding="utf-8")
    assert "Type=oneshot" in service
    assert "research_telemetry enrich-all" in service
    assert "network-online.target" not in service
    assert "crypto-scanner.service" not in service
    assert "OnCalendar=*:0/15" in timer
