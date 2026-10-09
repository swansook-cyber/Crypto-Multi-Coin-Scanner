from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest

from core.performance_analytics_v1 import build_complete_report, normalize_scanner_data
from core.performance_stats import summary as performance_summary
from daily_summary import ensure_columns as normalize_daily_summary
from core.post_tp1_lifecycle import (
    SHADOW_NAME,
    SHADOW_VERSION,
    evaluate_delayed_quarter_r_shadow,
    evaluate_reporting_lifecycle,
)
from core.research_telemetry import (
    CandidateSnapshot,
    FailOpenResearchTelemetry,
    ResearchTelemetryStore,
    ShadowDecision,
    post_tp1_shadow_metrics,
)
from core.outcome_tracker import journal_to_history
from review_signals import record_post_tp1_shadow


def trade(side: str = "LONG") -> dict[str, object]:
    if side == "LONG":
        return {"timestamp": "2026-10-10T00:00:00Z", "symbol": "BTCUSDT", "side": side, "entry": 100, "stop_loss": 90, "tp1": 110, "tp2": 120, "signal_status": "sent"}
    return {"timestamp": "2026-10-10T00:00:00Z", "symbol": "BTCUSDT", "side": side, "entry": 100, "stop_loss": 110, "tp1": 90, "tp2": 80, "signal_status": "sent"}


def candles(*ohlc: tuple[float, float, float]) -> pd.DataFrame:
    start = pd.Timestamp("2026-10-10T00:00:00Z")
    return pd.DataFrame(
        [
            {
                "open_time": start + pd.Timedelta(minutes=15 * i),
                "high": high,
                "low": low,
                "close": close,
                "close_time": start + pd.Timedelta(minutes=15 * (i + 1)),
            }
            for i, (high, low, close) in enumerate(ohlc)
        ]
    )


def test_tp1_touch_is_not_a_final_win_or_full_trade_r() -> None:
    row = trade()
    lifecycle = evaluate_reporting_lifecycle(row, candles((111, 99, 108)))
    assert lifecycle.state == "TP1_TOUCHED_REMAINDER_OPEN"
    assert lifecycle.lifecycle_r is None
    normalized = normalize_scanner_data(pd.DataFrame([{**row, "result": "WIN", "hit_target": "TP1"}]))
    assert normalized.iloc[0]["result"] == "OPEN"
    assert pd.isna(normalized.iloc[0]["real_rr"])


@pytest.mark.parametrize("side,path", [
    ("LONG", ((111, 99, 108), (109, 89, 92))),
    ("SHORT", ((101, 89, 92), (111, 91, 108))),
])
def test_tp1_then_original_sl_is_position_weighted(side: str, path: tuple[tuple[float, float, float], ...]) -> None:
    result = evaluate_reporting_lifecycle(trade(side), candles(*path))
    assert result.state == "TP1_THEN_ORIGINAL_SL"
    assert result.lifecycle_r == pytest.approx(0.0)


@pytest.mark.parametrize("side,path", [
    ("LONG", ((111, 99, 108), (121, 105, 119))),
    ("SHORT", ((101, 89, 92), (95, 79, 81))),
])
def test_tp1_then_tp2_is_position_weighted_and_symmetric(side: str, path: tuple[tuple[float, float, float], ...]) -> None:
    result = evaluate_reporting_lifecycle(trade(side), candles(*path))
    assert result.state == "TP2_WIN"
    assert result.lifecycle_r == pytest.approx(1.5)


def test_same_candle_ambiguity_is_excluded() -> None:
    result = evaluate_reporting_lifecycle(trade(), candles((111, 89, 100)))
    assert result.same_candle_ambiguous is True
    assert result.lifecycle_r is None


def test_shadow_moves_only_after_qualifying_decision_close_and_activates_next_candle() -> None:
    result = evaluate_delayed_quarter_r_shadow(
        trade(),
        candles((111, 99, 108), (114, 105, 112), (113, 101, 106), (121, 100, 120)),
    )
    assert result.state == "SHADOW_PROTECTIVE_STOP"
    assert result.move_stop_to_quarter_r is True
    assert result.shadow_stop_price == pytest.approx(102.5)
    assert result.shadow_r == pytest.approx(0.625)
    # Baseline survives the protective-stop candle and later reaches TP2.
    assert result.baseline_r == pytest.approx(1.5)
    assert result.delta_r == pytest.approx(-0.875)


def test_shadow_no_move_branch_keeps_original_stop() -> None:
    result = evaluate_delayed_quarter_r_shadow(
        trade(), candles((111, 99, 108), (109, 101, 109), (108, 89, 91))
    )
    assert result.state == "BASELINE_ORIGINAL_SL"
    assert result.move_stop_to_quarter_r is False
    assert result.shadow_r == pytest.approx(0.0)


def test_short_shadow_trigger_is_directionally_correct() -> None:
    row = trade("SHORT")
    result = evaluate_delayed_quarter_r_shadow(
        row, candles((101, 89, 92), (95, 85, 88), (98, 96, 97))
    )
    assert result.move_stop_to_quarter_r is True
    assert result.shadow_stop_price == pytest.approx(97.5)
    assert result.state == "SHADOW_PROTECTIVE_STOP"
    assert row["tp2"] == 80


def test_future_path_cannot_change_the_frozen_decision() -> None:
    prefix = ((111, 99, 108), (114, 105, 112))
    stops_later = evaluate_delayed_quarter_r_shadow(trade(), candles(*prefix, (113, 101, 106)))
    wins_later = evaluate_delayed_quarter_r_shadow(trade(), candles(*prefix, (121, 104, 120)))
    assert stops_later.decision_candle_time_utc == wins_later.decision_candle_time_utc
    assert stops_later.decision_close == wins_later.decision_close == 112
    assert stops_later.move_stop_to_quarter_r is wins_later.move_stop_to_quarter_r is True


def test_shadow_does_not_activate_on_the_decision_candle() -> None:
    result = evaluate_delayed_quarter_r_shadow(
        trade(), candles((111, 99, 108), (113, 101, 112), (121, 103, 120))
    )
    assert result.state == "SHADOW_TP2"
    assert result.shadow_r == pytest.approx(1.5)


def test_shadow_records_exact_following_candle_activation_time() -> None:
    result = evaluate_delayed_quarter_r_shadow(
        trade(), candles((111, 99, 108), (114, 105, 112), (113, 103, 106))
    )
    assert result.activation_time_utc == "2026-10-10T00:30:00+00:00"


def test_shadow_direct_tp2_and_same_candle_ambiguity() -> None:
    direct = evaluate_delayed_quarter_r_shadow(trade(), candles((121, 99, 119)))
    assert direct.state == "SHADOW_TP2"
    assert direct.shadow_r == pytest.approx(1.5)
    ambiguous = evaluate_delayed_quarter_r_shadow(trade(), candles((121, 89, 100)))
    assert ambiguous.state == "SAME_CANDLE_AMBIGUOUS"
    assert ambiguous.same_candle_ambiguous is True


def test_shadow_boundary_excludes_pre_activation_signals() -> None:
    class FakeTelemetry:
        called = False

        def upsert_shadow_for_signal(self, *args, **kwargs):
            self.called = True
            return True

    telemetry = FakeTelemetry()
    row = trade()
    row["timestamp"] = "2026-10-09T12:38:25Z"
    assert record_post_tp1_shadow(telemetry, pd.Series(row), candles((111, 99, 108))) is False
    assert telemetry.called is False


def test_shadow_recording_does_not_mutate_signal_or_call_live_paths() -> None:
    class FakeTelemetry:
        def __init__(self):
            self.calls = 0

        def upsert_shadow_for_signal(self, *args, **kwargs):
            self.calls += 1
            return True

    telemetry = FakeTelemetry()
    source = pd.Series(trade())
    before = source.copy(deep=True)
    assert record_post_tp1_shadow(
        telemetry, source, candles((111, 99, 108), (114, 105, 112), (121, 104, 120))
    ) is True
    pd.testing.assert_series_equal(source, before)
    assert telemetry.calls == 1


def test_legacy_history_remains_readable_but_tp1_is_open() -> None:
    history = journal_to_history(pd.DataFrame([
        {**trade(), "result": "WIN", "hit_target": "TP1"},
        {**trade(), "symbol": "ETHUSDT", "result": "LOSS"},
    ]))
    assert history.iloc[0]["result"] == "OPEN"
    assert history.iloc[0]["outcome"] == "TP1_TOUCHED_REMAINDER_OPEN"
    assert float(history.iloc[0]["real_rr"]) == 0.0
    assert history.iloc[1]["result"] == "LOSS"
    assert history.iloc[1]["outcome"] == "ORIGINAL_SL"
    assert history.iloc[1]["lifecycle_state"] == "ORIGINAL_SL"
    assert float(history.iloc[1]["real_rr"]) == pytest.approx(-1.0)


def test_reporting_pipeline_uses_lifecycle_r() -> None:
    row = {
        **trade(),
        "result": "WIN",
        "hit_target": "TP1",
        "lifecycle_state": "TP1_THEN_ORIGINAL_SL",
        "lifecycle_r": 0.0,
        "lifecycle_terminal": 1,
        "closed_at": "2026-10-10T01:00:00Z",
    }
    report, _ = build_complete_report(pd.DataFrame([row]), pd.DataFrame(), pd.DataFrame(), "ALL")
    assert report["net_r_estimate"] == pytest.approx(0.0)
    assert report["wins"] == 0
    assert report["losses"] == 0


def test_legacy_reporting_outputs_share_lifecycle_compatibility_semantics() -> None:
    rows = pd.DataFrame([
        {**trade(), "result": "WIN", "hit_target": "TP1"},
        {**trade(), "symbol": "ETHUSDT", "result": "LOSS"},
    ])

    analytics = normalize_scanner_data(rows)
    assert analytics["lifecycle_state"].tolist() == ["TP1_TOUCHED_REMAINDER_OPEN", "ORIGINAL_SL"]
    report, _ = build_complete_report(rows, pd.DataFrame(), pd.DataFrame(), "ALL")
    stats = performance_summary(rows)
    daily = normalize_daily_summary(rows)

    assert report["closed_signals"] == 1
    assert report["net_r_estimate"] == pytest.approx(-1.0)
    assert stats["closed_trades"] == 1
    assert stats["net_rr"] == pytest.approx(-1.0)
    assert daily["result"].tolist() == ["OPEN", "LOSS"]
    assert daily["real_rr"].fillna(0).sum() == pytest.approx(-1.0)


def test_unified_shadow_upsert_is_idempotent_and_survives_restart(tmp_path: Path) -> None:
    store = ResearchTelemetryStore(tmp_path / "research.db")
    boundary = pd.Timestamp(store.boundary()) + pd.Timedelta(seconds=1)
    timestamp = boundary.isoformat()
    run_id = "post_tp1_test_run"
    store.start_run(run_id, timestamp, timestamp)
    snapshot = CandidateSnapshot(
        run_id=run_id,
        timestamp_utc=timestamp,
        closed_candle_time_utc=timestamp,
        symbol="BTCUSDT",
        side="LONG",
        decision="SENT",
        signal_status="sent",
        entry=100,
        sl=90,
        tp1=110,
        tp2=120,
    )
    assert store.record_candidate(snapshot) is True
    signal_key = snapshot.canonical_signal_key or ""
    with store.connect(read_only=True) as connection:
        signal_key = connection.execute("SELECT canonical_signal_key FROM candidates").fetchone()[0]
    first = ShadowDecision(SHADOW_NAME, SHADOW_VERSION, "UNRESOLVED", metrics={"state": 1})
    final = ShadowDecision(
        SHADOW_NAME,
        SHADOW_VERSION,
        "SHADOW_PROTECTIVE_STOP",
        metrics={
            "state": 2,
            "tp1_touch_utc": timestamp,
            "shadow_action": "MOVE_REMAINDER_STOP_TO_PLUS_0.25R",
            "baseline_lifecycle_state": "TP2_WIN",
            "baseline_r": 1.5,
            "shadow_r": 0.625,
        },
    )
    assert store.upsert_shadow_for_signal(signal_key, first) is True
    restarted = ResearchTelemetryStore(store.path)
    assert restarted.upsert_shadow_for_signal(signal_key, final) is True
    with restarted.connect(read_only=True) as connection:
        rows = connection.execute(
            "SELECT decision,metrics_json FROM shadow_decisions WHERE shadow_name=?", (SHADOW_NAME,)
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["decision"] == "SHADOW_PROTECTIVE_STOP"
    assert '"state":2' in rows[0]["metrics_json"]
    metrics = post_tp1_shadow_metrics(restarted)
    assert metrics["eligible_tp1_n"] == 1
    assert metrics["resolved_eligible_n"] == 1
    assert metrics["shadow_action_n"] == 1
    assert metrics["tp2_winners_sacrificed"] == 1
    assert metrics["classification"] == "INSUFFICIENT EVIDENCE"


def test_database_unavailable_fails_open(tmp_path: Path) -> None:
    invalid_db_path = tmp_path / "directory-not-db"
    invalid_db_path.mkdir()
    telemetry = FailOpenResearchTelemetry(invalid_db_path)
    assert telemetry.available is False
    assert telemetry.upsert_shadow_for_signal("missing", ShadowDecision(SHADOW_NAME, SHADOW_VERSION, "UNRESOLVED")) is False


def test_shadow_boundary_is_first_activation_non_backdated_and_immutable(tmp_path: Path) -> None:
    store = ResearchTelemetryStore(tmp_path / "boundary.db")
    before = pd.Timestamp.now(tz="UTC")
    boundary = store.shadow_boundary(SHADOW_NAME, requested_start_utc="2020-01-01T00:00:00Z")
    assert pd.Timestamp(boundary) >= before
    assert store.shadow_boundary(SHADOW_NAME, requested_start_utc="2099-01-01T00:00:00Z") == boundary
