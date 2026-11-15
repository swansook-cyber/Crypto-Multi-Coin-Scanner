from __future__ import annotations

import json
from pathlib import Path
import time

import pandas as pd
import pytest

import review_signals
from core.historical_lifecycle_reconstruction import SOURCE, reconstruct_one, records_as_dicts
from core.research_telemetry import ResearchTelemetryStore
from tools.reconcile_pending_outcomes import (
    ambiguous_execution_keys,
    classify,
    update_current_csv,
)


def row(timestamp: str, *, result: str = "OPEN", key: str = "") -> dict:
    return {
        "timestamp": timestamp,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry": 100.0,
        "stop_loss": 90.0,
        "tp1": 110.0,
        "tp2": 120.0,
        "result": result,
        "hit_target": "TP2" if result == "WIN" else "SL" if result == "LOSS" else "",
        "closed_at": timestamp if result in {"WIN", "LOSS"} else "",
        "outcome_alert_sent": 1 if result in {"WIN", "LOSS"} else 0,
        "outcome_id": f"done-{timestamp}" if result in {"WIN", "LOSS"} else "",
        "signal_status": "sent",
        "canonical_signal_key": key,
        "lifecycle_state": "TP2_WIN" if result == "WIN" else "ORIGINAL_SL" if result == "LOSS" else "",
        "lifecycle_terminal": 1 if result in {"WIN", "LOSS"} else 0,
    }


def candles(*values: tuple[float, float]) -> pd.DataFrame:
    base = pd.Timestamp("2026-01-01T00:00:00Z")
    return pd.DataFrame([
        {
            "open_time": base + pd.Timedelta(minutes=15 * (index + 1)),
            "close_time": base + pd.Timedelta(minutes=15 * (index + 2)) - pd.Timedelta(milliseconds=1),
            "open": 100.0,
            "high": high,
            "low": low,
            "close": 100.0,
        }
        for index, (high, low) in enumerate(values)
    ])


def test_incremental_cycle_dedupes_and_ignores_finalized_history(monkeypatch: pytest.MonkeyPatch) -> None:
    now = pd.Timestamp.now(tz="UTC").floor("s")
    old = (now - pd.Timedelta(days=20)).isoformat()
    current = (now - pd.Timedelta(minutes=10)).isoformat()
    rows = [row(old, result="WIN") for _ in range(400)]
    rows += [{**row(old), "result": "SKIPPED", "signal_status": "logged_quality_filter"} for _ in range(8_000)]
    duplicate_key = "sig:v1:BTCUSDT|LONG|" + now.strftime("%Y-%m-%dT%H:%M:%SZ") + "|100.000000"
    rows += [row(current, key=duplicate_key), row(current, key=duplicate_key)]
    frame = review_signals.ensure_columns(pd.DataFrame(rows))
    fetches: list[str] = []
    writes: list[str] = []

    class Telemetry:
        def __init__(self, *_args, **_kwargs):
            pass

        def shadow_boundary(self, *_args, **_kwargs):
            return (now - pd.Timedelta(days=1)).isoformat()

    monkeypatch.setattr(review_signals, "reload_journal", lambda: frame.copy())
    monkeypatch.setattr(review_signals, "FailOpenResearchTelemetry", Telemetry)
    monkeypatch.setattr(review_signals, "candles_for_signal", lambda *_args: fetches.append("fetch") or candles((121, 99)))
    monkeypatch.setattr(review_signals, "record_post_tp1_shadow", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(review_signals, "persist_journal", lambda _df: writes.append("journal"))
    monkeypatch.setattr(review_signals, "sync_signal_history", lambda _df: writes.append("history"))
    monkeypatch.setattr(review_signals, "review_external_signals", lambda *_args: review_signals.ReviewStats())

    stats = review_signals.run_review_cycle(False, review_signals.build_session(), 24, print_report=False)
    assert stats.scanned_rows == 8_402
    assert stats.eligible_rows == 1
    assert stats.duplicate_rows == 1
    assert fetches == ["fetch"]
    assert writes == ["journal", "history"]


def test_noop_production_scale_cycle_is_bounded_and_does_not_rewrite(monkeypatch: pytest.MonkeyPatch) -> None:
    old = "2026-01-01T00:00:00Z"
    frame = review_signals.ensure_columns(pd.DataFrame([row(old, result="WIN") for _ in range(10_000)]))
    writes: list[str] = []

    class Telemetry:
        def __init__(self, *_args, **_kwargs):
            pass

        def shadow_boundary(self, *_args, **_kwargs):
            return "2026-01-01T00:00:00Z"

    monkeypatch.setattr(review_signals, "reload_journal", lambda: frame.copy())
    monkeypatch.setattr(review_signals, "FailOpenResearchTelemetry", Telemetry)
    monkeypatch.setattr(review_signals, "persist_journal", lambda _df: writes.append("journal"))
    monkeypatch.setattr(review_signals, "sync_signal_history", lambda _df: writes.append("history"))
    monkeypatch.setattr(review_signals, "review_external_signals", lambda *_args: review_signals.ReviewStats())
    started = time.perf_counter()
    stats = review_signals.run_review_cycle(False, review_signals.build_session(), 24, print_report=False)
    elapsed = time.perf_counter() - started
    assert stats.eligible_rows == 0
    assert writes == []
    assert elapsed < 12.0


def test_deterministic_recovery_and_ambiguities_are_conservative(tmp_path: Path) -> None:
    candidate = row("2026-01-01T00:00:00Z")
    terminal = records_as_dicts([reconstruct_one(candidate, candles((121, 99)), "2026-01-01T02:00:00Z")])[0]
    ambiguous = records_as_dicts([reconstruct_one(candidate, candles((121, 89)), "2026-01-01T02:00:00Z")])[0]
    assert classify(terminal, "HISTORICAL_REMAINDER_UNKNOWN", False) == "DETERMINISTIC_RECOVERABLE"
    assert classify(terminal, "LIVE_OPEN_REMAINDER", True) == "EXECUTION_AMBIGUOUS"
    assert classify(ambiguous, "UNRESOLVED_REMAINDER", False) == "LIFECYCLE_AMBIGUOUS"

    execution = tmp_path / "execution.csv"
    pd.DataFrame([{"match_status": "AMBIGUOUS", "candidate_signal_keys": json.dumps(["sig:v1:exact"])}]).to_csv(execution, index=False)
    assert ambiguous_execution_keys(execution) == {"sig:v1:exact"}


def test_reconciliation_is_idempotent_and_preserves_shadow(tmp_path: Path) -> None:
    db = tmp_path / "research.db"
    store = ResearchTelemetryStore(db)
    candidate_key = "candidate:v1:test"
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO scanner_runs(run_id,scan_started_at_utc,source_mode,source_name) VALUES(?,?,?,?)",
            ("run", "2026-01-01T00:00:00Z", "PROSPECTIVE", "test"),
        )
        connection.execute(
            """INSERT INTO candidates(candidate_key,run_id,timestamp_utc,closed_candle_time_utc,symbol,side,
            candidate_stage,decision,source_mode,source_name,recorded_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (candidate_key, "run", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "BTCUSDT", "LONG",
             "sent", "SENT", "PROSPECTIVE", "test", "2026-01-01T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO shadow_decisions VALUES(?,?,?,?,?,?,?)",
            (candidate_key, "breakout_confirmation", "v1", "PASS", "fixture", "{}", "2026-01-01T00:00:00Z"),
        )
    record = records_as_dicts([reconstruct_one(row("2026-01-01T00:00:00Z"), candles((121, 99)), "2026-01-01T02:00:00Z")])[0]
    assert store.upsert_historical_lifecycle([record]) == {"inserted": 1, "updated": 0, "unchanged": 0}
    assert store.upsert_historical_lifecycle([record]) == {"inserted": 0, "updated": 0, "unchanged": 1}
    with store.connect(read_only=True) as connection:
        shadow = connection.execute("SELECT decision,reason FROM shadow_decisions WHERE candidate_key=?", (candidate_key,)).fetchone()
    assert tuple(shadow) == ("PASS", "fixture")


def test_current_csv_updates_only_exact_recoverable_row_and_second_pass_is_noop(tmp_path: Path) -> None:
    path = tmp_path / "signals.csv"
    recoverable = row("2026-01-01T00:00:00Z", key="sig:v1:BTCUSDT|LONG|2026-01-01T00:00:00Z|100.000000")
    untouched = {**row("2026-01-01T01:00:00Z"), "symbol": "ETHUSDT"}
    pd.DataFrame([recoverable, untouched]).to_csv(path, index=False)
    record = records_as_dicts([reconstruct_one(recoverable, candles((121, 99)), "2026-01-01T02:00:00Z")])[0]
    assert record["source"] == SOURCE
    assert update_current_csv(path, [record]) == 1
    first = path.read_bytes()
    assert update_current_csv(path, [record]) == 0
    assert path.read_bytes() == first
    saved = pd.read_csv(path)
    assert saved.loc[0, "lifecycle_state"] == "TP2_WIN"
    assert str(saved.loc[1, "lifecycle_state"]) in {"", "nan"}


def test_overlap_lock_rejects_second_instance(tmp_path: Path) -> None:
    lock = tmp_path / "outcome.lock"
    with review_signals.outcome_checker_lock(lock):
        with pytest.raises(review_signals.OutcomeCheckerAlreadyRunning):
            with review_signals.outcome_checker_lock(lock):
                pass
