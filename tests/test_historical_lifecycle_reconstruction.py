from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from core.historical_lifecycle_reconstruction import (
    HISTORICAL_UNKNOWN_STATE,
    LIVE_OPEN_STATE,
    SOURCE,
    apply_lifecycle_truth,
    canonical_sent_population,
    lifecycle_metrics,
    load_canonical_reporting_population,
    parse_utc_mixed,
    reconstruct_one,
    signal_key,
)
from core.performance_analytics_v1 import build_complete_report
from core.performance_stats import summary as performance_summary
from core.research_telemetry import ResearchTelemetryStore
from daily_summary import build_daily_summary, ensure_columns
from dashboard import dashboard_kpis


def signal(index: int, **extra):
    row = {
        "timestamp": (pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(minutes=index)).isoformat(),
        "symbol": f"C{index}USDT", "side": "LONG", "entry": 100 + index / 1000,
        "stop_loss": 90, "sl": 90, "tp1": 112, "tp2": 120,
        "signal_status": "sent", "result": "OPEN", "hit_target": "",
    }
    row.update(extra)
    return row


def candles(*bars, base="2026-01-01T00:15:00Z"):
    base = pd.Timestamp(base)
    return pd.DataFrame([
        {"open_time": base + pd.Timedelta(minutes=15 * i), "high": high, "low": low, "close": close,
         "close_time": base + pd.Timedelta(minutes=15 * (i + 1)) - pd.Timedelta(milliseconds=1)}
        for i, (high, low, close) in enumerate(bars)
    ])


def fact(row, state="TP2_WIN", r=1.6, **extra):
    key = canonical_sent_population(pd.DataFrame([row]), pd.DataFrame())[0].iloc[0]["canonical_signal_key"]
    value = {"canonical_signal_key": key, "lifecycle_state": state, "lifecycle_r": r,
             "tp1_touch_utc": "2026-01-01T00:29:59Z", "terminal_event_utc": "2026-01-01T00:44:59Z",
             "ambiguity_flag": 0, "ambiguity_reason": "", "source": SOURCE}
    value.update(extra)
    return value


def test_canonical_population_232_plus_52_plus_3_is_287():
    current = pd.DataFrame([signal(i) for i in range(232)])
    history = pd.DataFrame([signal(i) for i in range(284)])
    database = pd.DataFrame([signal(i, decision="SENT") for i in range(268, 287)])
    result, counts = canonical_sent_population(current, history, database)
    assert len(result) == 287
    assert (counts["current_csv"], counts["history_only"], counts["db_only"]) == (232, 52, 3)


def test_history_only_sent_is_included():
    result, counts = canonical_sent_population(pd.DataFrame([signal(0)]), pd.DataFrame([signal(0), signal(1)]))
    assert len(result) == 2 and counts["history_only"] == 1


def test_db_only_sent_is_included():
    result, counts = canonical_sent_population(pd.DataFrame([signal(0)]), pd.DataFrame(), pd.DataFrame([signal(1, decision="SENT")]))
    assert len(result) == 2 and counts["db_only"] == 1


def test_duplicate_key_is_suppressed_and_missing_evidence_is_merged():
    current = signal(0, lifecycle_state="")
    history = signal(0, lifecycle_state="TP2_WIN")
    result, counts = canonical_sent_population(pd.DataFrame([current]), pd.DataFrame([history]))
    assert len(result) == 1 and result.iloc[0]["lifecycle_state"] == "TP2_WIN"
    assert counts["duplicates_suppressed"] == 1


def test_historical_tp1_is_unknown_not_live_open():
    row = signal(0, result="WIN", hit_target="TP1")
    population, _ = canonical_sent_population(pd.DataFrame([row]), pd.DataFrame())
    applied = apply_lifecycle_truth(population, pd.DataFrame())
    assert applied.iloc[0]["lifecycle_state"] == HISTORICAL_UNKNOWN_STATE
    assert applied.iloc[0]["result"] == "UNKNOWN"


def test_reconstruct_tp2():
    result = reconstruct_one(signal(0), candles((121, 99, 118)), "2026-01-01T01:00:00Z")
    assert result.lifecycle_state == "TP2_WIN" and result.lifecycle_r == pytest.approx(1.6)


def test_reconstruct_tp1_then_original_sl():
    result = reconstruct_one(signal(0), candles((113, 99, 110), (111, 89, 90)), "2026-01-01T01:00:00Z")
    assert result.lifecycle_state == "TP1_THEN_ORIGINAL_SL" and result.lifecycle_r == pytest.approx(0.1)


def test_reconstruct_original_sl():
    result = reconstruct_one(signal(0), candles((105, 89, 91)), "2026-01-01T01:00:00Z")
    assert result.lifecycle_state == "ORIGINAL_SL" and result.lifecycle_r == -1


def test_reconstruct_ambiguity_is_preserved():
    result = reconstruct_one(signal(0), candles((121, 89, 100)), "2026-01-01T01:00:00Z")
    assert result.lifecycle_state == "SAME_CANDLE_AMBIGUOUS" and result.ambiguity_flag == 1


def test_reconstruct_unresolved_is_preserved():
    result = reconstruct_one(signal(0), candles((105, 95, 100)), "2026-01-01T01:00:00Z")
    assert result.lifecycle_state == "UNRESOLVED_REMAINDER" and result.lifecycle_r is None


def test_source_priority_prospective_beats_reconstruction():
    row = signal(0, lifecycle_state="TP1_THEN_PROTECTIVE_STOP", lifecycle_r=0.6,
                 lifecycle_source="PROSPECTIVE_LIVE", lifecycle_terminal=1)
    population, _ = canonical_sent_population(pd.DataFrame([row]), pd.DataFrame())
    result = apply_lifecycle_truth(population, pd.DataFrame([fact(row)]))
    assert result.iloc[0]["lifecycle_state"] == "TP1_THEN_PROTECTIVE_STOP"
    assert result.iloc[0]["lifecycle_r"] == pytest.approx(0.6)


def test_unproven_lifecycle_state_does_not_outrank_reconstruction():
    row = signal(0, lifecycle_state="TP1_THEN_PROTECTIVE_STOP", lifecycle_r=0.6, lifecycle_terminal=1)
    population, _ = canonical_sent_population(pd.DataFrame([row]), pd.DataFrame())
    result = apply_lifecycle_truth(population, pd.DataFrame([fact(row)]))
    assert result.iloc[0]["lifecycle_state"] == "TP2_WIN"


def test_reconstruction_beats_legacy():
    row = signal(0, result="LOSS", hit_target="SL")
    population, _ = canonical_sent_population(pd.DataFrame([row]), pd.DataFrame())
    result = apply_lifecycle_truth(population, pd.DataFrame([fact(row)]))
    assert result.iloc[0]["lifecycle_state"] == "TP2_WIN"


def test_float_terminal_representation_remains_closed():
    row = signal(0, lifecycle_terminal=0.0)
    population, _ = canonical_sent_population(pd.DataFrame([row]), pd.DataFrame())
    result = apply_lifecycle_truth(population, pd.DataFrame([fact(row)]))
    assert bool(result.iloc[0]["lifecycle_terminal"]) is True
    assert result.iloc[0]["result"] == "WIN"


def test_legacy_tp2_fallback_uses_weighted_geometry():
    row = signal(0, result="WIN", hit_target="TP2")
    population, _ = canonical_sent_population(pd.DataFrame([row]), pd.DataFrame())
    result = apply_lifecycle_truth(population, pd.DataFrame())
    assert result.iloc[0]["lifecycle_state"] == "TP2_WIN"
    assert result.iloc[0]["lifecycle_r"] == pytest.approx(1.6)


def test_live_open_requires_prospective_evidence():
    row = signal(0, lifecycle_state="TP1_TOUCHED_REMAINDER_OPEN", lifecycle_source="PROSPECTIVE_LIVE")
    population, _ = canonical_sent_population(pd.DataFrame([row]), pd.DataFrame())
    result = apply_lifecycle_truth(population, pd.DataFrame())
    assert result.iloc[0]["lifecycle_state"] == LIVE_OPEN_STATE


def test_live_open_accepts_prospective_mode_with_runtime_touch_evidence():
    row = signal(0, lifecycle_state="TP1_TOUCHED_REMAINDER_OPEN", source_mode="PROSPECTIVE",
                 tp1_touch_time_utc="2026-01-01T00:29:59Z")
    population, _ = canonical_sent_population(pd.DataFrame([row]), pd.DataFrame())
    result = apply_lifecycle_truth(population, pd.DataFrame())
    assert result.iloc[0]["lifecycle_state"] == LIVE_OPEN_STATE


def test_legacy_tp1_state_is_overridden_by_reconstruction():
    row = signal(0, result="OPEN", hit_target="TP1", lifecycle_state="TP1_TOUCHED_REMAINDER_OPEN")
    population, _ = canonical_sent_population(pd.DataFrame([row]), pd.DataFrame())
    result = apply_lifecycle_truth(population, pd.DataFrame([fact(row)]))
    assert result.iloc[0]["lifecycle_state"] == "TP2_WIN"


def test_upsert_is_idempotent(tmp_path: Path):
    store = ResearchTelemetryStore(tmp_path / "research.db")
    record = fact(signal(0)) | {
        "timestamp_utc": "2026-01-01T00:00:00Z", "symbol": "C0USDT", "side": "LONG",
        "tp1_fraction": .5, "remainder_fraction": .5, "reconstruction_version": "v1",
        "reconstructed_at_utc": "2026-01-02T00:00:00Z", "price_source": "BINANCE_USDM_FUTURES",
        "price_interval": "15m", "closed_candle_cutoff_utc": "2026-01-01T01:00:00Z",
        "geometry_hash": "g", "evidence_hash": "e",
    }
    assert store.upsert_historical_lifecycle([record]) == {"inserted": 1, "updated": 0, "unchanged": 0}
    assert store.upsert_historical_lifecycle([record]) == {"inserted": 0, "updated": 0, "unchanged": 1}


def test_reconstruction_does_not_mutate_source_frame():
    source = pd.DataFrame([signal(0)])
    before = source.copy(deep=True)
    population, _ = canonical_sent_population(source, pd.DataFrame())
    apply_lifecycle_truth(population, pd.DataFrame([fact(signal(0))]))
    pd.testing.assert_frame_equal(source, before)


def canonical_lifecycle_frame():
    rows = [
        signal(0, lifecycle_state="TP2_WIN", lifecycle_r=1.4, lifecycle_terminal=1, terminal_event_utc="2026-01-01T01:00:00Z"),
        signal(1, lifecycle_state="TP1_THEN_ORIGINAL_SL", lifecycle_r=0.1, lifecycle_terminal=1, terminal_event_utc="2026-01-01T02:00:00Z"),
        signal(2, lifecycle_state="ORIGINAL_SL", lifecycle_r=-1.0, lifecycle_terminal=1, terminal_event_utc="2026-01-01T03:00:00Z"),
    ]
    frame, _ = canonical_sent_population(pd.DataFrame(rows), pd.DataFrame())
    return frame


def test_dashboard_lifecycle_metrics_are_correct():
    metrics = dashboard_kpis(canonical_lifecycle_frame())
    assert metrics["Closed trades"] == 3 and metrics["Net R"] == pytest.approx(.5)
    assert metrics["TP2 wins"] == 1 and metrics["TP1->SL partial outcomes"] == 1 and metrics["Original SL"] == 1


def test_daily_summary_lifecycle_metrics_are_correct():
    report = build_daily_summary(ensure_columns(canonical_lifecycle_frame()), "ALL")
    assert report["wins"] == 2 and report["losses"] == 1 and report["net_rr"] == pytest.approx(.5)


def test_performance_report_lifecycle_metrics_are_correct():
    report, _ = build_complete_report(canonical_lifecycle_frame(), pd.DataFrame(), pd.DataFrame(), "ALL")
    assert report["closed_signals"] == 3 and report["open_signals"] == 0 and report["net_r_estimate"] == pytest.approx(.5)


def test_analytics_and_stats_are_consistent():
    frame = canonical_lifecycle_frame()
    analytics, _ = build_complete_report(frame, pd.DataFrame(), pd.DataFrame(), "ALL")
    stats = performance_summary(frame)
    assert (analytics["closed_signals"], analytics["wins"], analytics["losses"]) == (stats["closed_trades"], stats["wins"], stats["losses"])
    assert analytics["net_r_estimate"] == pytest.approx(stats["net_rr"])


def test_terminal_order_drawdown_is_consistent():
    frame = canonical_lifecycle_frame()
    metrics = lifecycle_metrics(frame)
    analytics, _ = build_complete_report(frame, pd.DataFrame(), pd.DataFrame(), "ALL")
    stats = performance_summary(frame)
    assert metrics["max_drawdown"] == pytest.approx(-1.0)
    assert analytics["max_drawdown_r"] == pytest.approx(metrics["max_drawdown"])
    assert stats["max_drawdown"] == pytest.approx(metrics["max_drawdown"])


def test_drawdown_uses_certified_signal_order_not_reversed_terminal_order():
    frame = pd.DataFrame([
        signal(0, lifecycle_state="TP2_WIN", lifecycle_r=2.0, lifecycle_terminal=1, terminal_event_utc="2026-01-01T03:00:00Z"),
        signal(1, lifecycle_state="ORIGINAL_SL", lifecycle_r=-1.0, lifecycle_terminal=1, terminal_event_utc="2026-01-01T02:00:00Z"),
        signal(2, lifecycle_state="ORIGINAL_SL", lifecycle_r=-1.0, lifecycle_terminal=1, terminal_event_utc="2026-01-01T01:00:00Z"),
    ])
    population, _ = canonical_sent_population(frame, pd.DataFrame())
    assert lifecycle_metrics(population)["max_drawdown"] == pytest.approx(-2.0)


def test_shadow_boundary_is_unchanged_by_lifecycle_upsert(tmp_path: Path):
    store = ResearchTelemetryStore(tmp_path / "research.db")
    before = store.shadow_boundary("post_tp1_delayed_quarter_r_v1")
    test_upsert_is_idempotent(tmp_path)
    after = ResearchTelemetryStore(tmp_path / "research.db").shadow_boundary("post_tp1_delayed_quarter_r_v1")
    assert after == before


def test_schema_migration_preserves_existing_table(tmp_path: Path):
    store = ResearchTelemetryStore(tmp_path / "research.db")
    with store.connect(read_only=True) as connection:
        assert connection.execute("SELECT schema_version FROM research_meta").fetchone()[0] == 5
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='historical_lifecycle_reconstructions'").fetchone()


def test_corrupt_db_reporting_fallback_is_safe(tmp_path: Path):
    signals = tmp_path / "signals.csv"
    history = tmp_path / "history.csv"
    db = tmp_path / "broken.db"
    pd.DataFrame([signal(0, result="WIN", hit_target="TP1")]).to_csv(signals, index=False)
    pd.DataFrame().to_csv(history, index=False)
    db.write_bytes(b"not sqlite")
    result, _ = load_canonical_reporting_population(signals, history, db)
    assert len(result) == 1 and result.iloc[0]["lifecycle_state"] == HISTORICAL_UNKNOWN_STATE


def test_legacy_result_and_hit_target_are_not_modified():
    row = signal(0, result="WIN", hit_target="TP1")
    population, _ = canonical_sent_population(pd.DataFrame([row]), pd.DataFrame())
    result = apply_lifecycle_truth(population, pd.DataFrame([fact(row)]))
    assert result.iloc[0]["result"] == "WIN"
    assert result.iloc[0]["hit_target"] == "TP1"


def test_microsecond_csv_timestamp_parses():
    parsed = parse_utc_mixed(pd.Series(["2026-10-05T04:00:47.123456+00:00"]))
    assert parsed.notna().all()
    assert parsed.iloc[0].isoformat() == "2026-10-05T04:00:47.123456+00:00"


def test_second_precision_db_timestamp_parses():
    parsed = parse_utc_mixed(pd.Series(["2026-10-05T04:00:47Z"]))
    assert parsed.notna().all()
    assert parsed.iloc[0].isoformat() == "2026-10-05T04:00:47+00:00"


def test_mixed_timestamp_column_parses_every_row():
    values = pd.Series(["2026-10-05T04:00:47.123456+00:00", "2026-10-07T04:00:45Z", "2026-10-07T13:00:33Z"])
    assert parse_utc_mixed(values).notna().all()


def test_timezone_equivalent_timestamps_canonicalize_without_key_change():
    first = signal(0, timestamp="2026-10-05T04:00:47Z", symbol="DOTUSDT", entry=1.2173)
    second = signal(0, timestamp="2026-10-05T11:00:47+07:00", symbol="DOTUSDT", entry=1.2173)
    assert signal_key(first) == signal_key(second)


def test_invalid_timestamp_remains_explicit_unresolved():
    row = signal(0, timestamp="not-a-time")
    result = reconstruct_one(row, candles((121, 99, 118)), "2026-10-09T06:44:59.999Z")
    assert result.lifecycle_state == "UNRESOLVED_REMAINDER"
    assert result.ambiguity_reason == "invalid_signal_timestamp"


PRODUCTION_DB_ONLY = [
    {
        "timestamp": "2026-10-05T04:00:47Z", "symbol": "DOTUSDT", "side": "LONG",
        "entry": 1.2173, "sl": 1.2021561534117018, "tp1": 1.235472615905958,
        "tp2": 1.2475876931765963, "signal_status": "sent",
    },
    {
        "timestamp": "2026-10-07T04:00:45Z", "symbol": "DOTUSDT", "side": "SHORT",
        "entry": 1.1223, "sl": 1.1425165280362575, "tp1": 1.0980401663564914,
        "tp2": 1.0818669439274853, "signal_status": "sent",
    },
    {
        "timestamp": "2026-10-07T13:00:33Z", "symbol": "SOLUSDT", "side": "SHORT",
        "entry": 116.33, "sl": 117.18604835874878, "tp1": 115.30274196950145,
        "tp2": 114.61790328250243, "signal_status": "sent",
    },
]


def _production_db_only_results():
    cutoff = "2026-10-09T06:44:59.999Z"
    evidence = [
        candles((1.236, 1.21, 1.23), (1.23, 1.20, 1.205), base="2026-10-05T04:15:00Z"),
        candles((1.13, 1.08, 1.085), base="2026-10-07T04:15:00Z"),
        candles((116.5, 114.5, 115.0), base="2026-10-07T13:15:00Z"),
    ]
    return [reconstruct_one(row, path, cutoff) for row, path in zip(PRODUCTION_DB_ONLY, evidence)]


def test_production_dot_long_reconstructs_tp1_then_original_sl():
    assert _production_db_only_results()[0].lifecycle_state == "TP1_THEN_ORIGINAL_SL"


def test_production_dot_short_reconstructs_tp2():
    assert _production_db_only_results()[1].lifecycle_state == "TP2_WIN"


def test_production_sol_short_reconstructs_tp2():
    assert _production_db_only_results()[2].lifecycle_state == "TP2_WIN"


def test_three_production_db_only_trades_contribute_3_3r():
    assert sum(result.lifecycle_r or 0 for result in _production_db_only_results()) == pytest.approx(3.3)


def test_reporting_read_does_not_mutate_research_db(tmp_path: Path):
    db = tmp_path / "research.db"
    ResearchTelemetryStore(db)
    before = db.read_bytes()
    signals = tmp_path / "signals.csv"
    history = tmp_path / "history.csv"
    pd.DataFrame([signal(0)]).to_csv(signals, index=False)
    pd.DataFrame([signal(1)]).to_csv(history, index=False)
    load_canonical_reporting_population(signals, history, db)
    assert db.read_bytes() == before
