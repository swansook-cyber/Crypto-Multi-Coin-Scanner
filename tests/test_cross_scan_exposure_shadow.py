from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor
import hashlib
import inspect
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import cornix_agent
import core.cross_scan_exposure_shadow as cross_scan_shadow
from core.cross_scan_exposure_shadow import (
    CrossScanExposureShadowLogger,
    ExposureState,
    OpenExposure,
    ShadowPersistenceLock,
    ShadowRule,
    atomic_write_csv,
    atomic_write_text,
    evaluate_shadow_candidate,
    load_open_exposure_state,
    pair_return_correlation,
    refresh_shadow_outcomes,
    timestamped_close_series,
)


NOW = pd.Timestamp("2026-09-29T04:00:00Z")


def candidate(symbol: str = "ETHUSDT", side: str = "LONG", timestamp: str = "2026-09-29T04:00:00Z"):
    return SimpleNamespace(
        symbol=symbol,
        direction=side,
        timestamp=pd.Timestamp(timestamp),
        entry=100.0,
        setup_strength=88,
        confidence=88,
        btc_regime="bullish",
        market_session="Asia",
    )


def exposure(symbol: str, side: str = "LONG", timestamp: str = "2026-09-29T02:00:00Z") -> OpenExposure:
    signal = candidate(symbol, side, timestamp)
    from core.cross_scan_exposure_shadow import signal_key
    return OpenExposure(signal_key(signal), symbol, side, pd.Timestamp(timestamp))


def histories(*symbols: str, uncorrelated: set[str] | None = None) -> dict[str, pd.Series]:
    uncorrelated = uncorrelated or set()
    rng = np.random.default_rng(42)
    base_returns = rng.normal(0.001, 0.01, 90)
    index = pd.date_range("2026-09-20", periods=91, freq="h", tz="UTC")
    result = {}
    for offset, symbol in enumerate(symbols):
        returns = rng.normal(0, 0.012, 90) if symbol in uncorrelated else base_returns + offset * 0.000001
        result[symbol] = pd.Series(np.r_[100.0, 100.0 * np.cumprod(1 + returns)], index=index)
    return result


def journal_row(symbol: str, side: str, timestamp: str, result: str = "OPEN", **extra):
    return {
        "timestamp": timestamp,
        "symbol": symbol,
        "side": side,
        "entry": extra.pop("entry", 100),
        "signal_status": "sent",
        "result": result,
        "closed_at": extra.pop("closed_at", ""),
        "risk_reward": extra.pop("risk_reward", 2),
        "hit_target": extra.pop("hit_target", ""),
        "net_r_estimate": extra.pop("net_r_estimate", ""),
        **extra,
    }


def write_journal(path: Path, rows: list[dict]) -> None:
    pd.DataFrame(rows).to_csv(path, index=False)


def test_no_open_exposure_allows() -> None:
    record = evaluate_shadow_candidate(candidate(), ExposureState((), ()), histories("ETHUSDT"), now=NOW)
    assert record["shadow_decision"] == "ALLOW"
    assert record["shadow_reason"] == "no_open_same_side_exposure"


def test_same_side_uncorrelated_exposure_allows() -> None:
    state = ExposureState((exposure("BTCUSDT"),), (exposure("BTCUSDT"),))
    record = evaluate_shadow_candidate(
        candidate(), state, histories("ETHUSDT", "BTCUSDT", uncorrelated={"BTCUSDT"}), now=NOW
    )
    assert record["shadow_decision"] == "ALLOW"
    assert record["correlated_open_count"] == 0


def test_correlated_exposure_below_configured_threshold_is_caution() -> None:
    item = exposure("BTCUSDT")
    state = ExposureState((item,), (item,))
    rule = ShadowRule(max_correlated_open_positions=2)
    record = evaluate_shadow_candidate(candidate(), state, histories("ETHUSDT", "BTCUSDT"), rule=rule, now=NOW)
    assert record["shadow_decision"] == "CAUTION"


def test_correlated_cluster_threshold_exceeded_would_block() -> None:
    items = (exposure("BTCUSDT"), exposure("SOLUSDT", timestamp="2026-09-29T03:00:00Z"))
    state = ExposureState(items, items)
    record = evaluate_shadow_candidate(candidate(), state, histories("ETHUSDT", "BTCUSDT", "SOLUSDT"), now=NOW)
    assert record["shadow_decision"] == "WOULD_BLOCK"
    assert record["correlated_open_count"] == 2
    assert record["representative_symbol"] == "BTCUSDT"


def test_opposite_side_exposure_allows() -> None:
    item = exposure("BTCUSDT", "SHORT")
    record = evaluate_shadow_candidate(
        candidate(), ExposureState((item,), (item,)), histories("ETHUSDT", "BTCUSDT"), now=NOW
    )
    assert record["shadow_decision"] == "ALLOW"


def test_closed_exposure_disappears_between_scans(tmp_path: Path) -> None:
    journal = tmp_path / "signals.csv"
    row = journal_row("BTCUSDT", "LONG", "2026-09-29T02:00:00Z")
    write_journal(journal, [row])
    assert len(load_open_exposure_state(journal, now=NOW).positions) == 1
    row.update(result="LOSS", closed_at="2026-09-29T03:00:00Z")
    write_journal(journal, [row])
    assert len(load_open_exposure_state(journal, now=NOW).positions) == 0


def test_stale_open_exposure_is_bounded(tmp_path: Path) -> None:
    journal = tmp_path / "signals.csv"
    write_journal(journal, [journal_row("BTCUSDT", "LONG", "2026-09-27T00:00:00Z")])
    state = load_open_exposure_state(journal, now=NOW, stale_after_hours=24)
    assert not state.positions
    assert state.stale_excluded_count == 1


def test_open_state_requires_sent_open_and_deduplicates(tmp_path: Path) -> None:
    journal = tmp_path / "signals.csv"
    active = journal_row("BTCUSDT", "LONG", "2026-09-29T02:00:00Z")
    report_only = {**journal_row("ETHUSDT", "LONG", "2026-09-29T02:30:00Z"), "signal_status": "tier_c_report_only"}
    closed = journal_row("SOLUSDT", "LONG", "2026-09-29T03:00:00Z", "WIN")
    write_journal(journal, [active, active.copy(), report_only, closed])
    state = load_open_exposure_state(journal, now=NOW)
    assert len(state.positions) == 1
    assert state.positions[0].symbol == "BTCUSDT"


def test_multiple_correlated_open_symbols_are_recorded() -> None:
    items = (exposure("BTCUSDT"), exposure("SOLUSDT", timestamp="2026-09-29T03:00:00Z"))
    record = evaluate_shadow_candidate(
        candidate(), ExposureState(items, items), histories("ETHUSDT", "BTCUSDT", "SOLUSDT"), now=NOW
    )
    assert record["correlated_symbols"] == "BTCUSDT,SOLUSDT"
    assert record["cluster_exposure_count"] == 3


def test_restart_persists_boundary_and_deduplicates(tmp_path: Path) -> None:
    journal = tmp_path / "signals.csv"
    write_journal(journal, [journal_row("BTCUSDT", "SHORT", "2026-09-29T03:00:00Z")])
    shadow, state = tmp_path / "shadow.csv", tmp_path / "state.json"
    first = CrossScanExposureShadowLogger(
        shadow, state, journal, prospective_start_timestamp_utc="2026-09-29T00:00:00Z"
    )
    first.log_candidate(candidate(), histories("ETHUSDT", "BTCUSDT"), now=NOW)
    second = CrossScanExposureShadowLogger(
        shadow, state, journal, prospective_start_timestamp_utc="2099-01-01T00:00:00Z"
    )
    second.log_candidate(candidate(), histories("ETHUSDT", "BTCUSDT"), now=NOW)
    saved = pd.read_csv(shadow)
    assert len(saved) == 1
    assert second.prospective_start_timestamp_utc == "2026-09-29T00:00:00Z"


def test_shadow_never_mutates_signals_or_candidate(tmp_path: Path) -> None:
    journal = tmp_path / "signals.csv"
    write_journal(journal, [journal_row("BTCUSDT", "LONG", "2026-09-29T02:00:00Z")])
    before_file = hashlib.sha256(journal.read_bytes()).hexdigest()
    signal = candidate()
    before_signal = vars(signal).copy()
    logger = CrossScanExposureShadowLogger(
        tmp_path / "shadow.csv", tmp_path / "state.json", journal,
        prospective_start_timestamp_utc="2026-09-29T00:00:00Z",
    )
    record = logger.log_candidate(signal, histories("ETHUSDT", "BTCUSDT"), now=NOW)
    assert record["shadow_decision"] == "WOULD_BLOCK"
    assert record["live_result"] == "SENT_UNCHANGED"
    assert vars(signal) == before_signal
    assert hashlib.sha256(journal.read_bytes()).hexdigest() == before_file


def test_scanner_routing_ignores_shadow_decision() -> None:
    source = inspect.getsource(cornix_agent.AgentRunner.process_candidates)
    shadow_call = source.index("self.evaluate_cross_scan_exposure_shadow(signal)")
    journal_call = source.index('self.log_signal_status(signal, "sent", "")')
    telegram_call = source.index("self.notifier.send_signal(signal)")
    assert shadow_call < journal_call < telegram_call
    assert "if self.evaluate_cross_scan_exposure_shadow" not in source


def test_outcome_linkage_enriches_shadow_only(tmp_path: Path) -> None:
    journal = tmp_path / "signals.csv"
    signal = candidate()
    write_journal(
        journal,
        [journal_row("ETHUSDT", "LONG", signal.timestamp.isoformat(), "WIN", hit_target="TP1", net_r_estimate=1.2)],
    )
    shadow, state = tmp_path / "shadow.csv", tmp_path / "state.json"
    logger = CrossScanExposureShadowLogger(
        shadow, state, journal, prospective_start_timestamp_utc="2026-09-29T00:00:00Z"
    )
    logger.log_candidate(signal, histories("ETHUSDT"), now=NOW)
    original = pd.read_csv(shadow).iloc[0][
        ["shadow_decision", "representative_signal_key", "prospective_start_timestamp_utc"]
    ].to_dict()
    assert refresh_shadow_outcomes(shadow, journal) == 1
    saved = pd.read_csv(shadow)
    assert saved.loc[0, "final_outcome"] == "WIN"
    assert saved.loc[0, "final_r"] == 1.2
    assert saved.iloc[0][list(original)].to_dict() == original


def test_first_in_cluster_counterfactual_state_machine(tmp_path: Path) -> None:
    events = [
        ("DOGEUSDT", "2026-09-23T03:00:39Z", "2026-09-23T05:59:59Z"),
        ("ARBUSDT", "2026-09-23T03:00:56Z", "2026-09-23T04:29:59Z"),
        ("XRPUSDT", "2026-09-23T04:00:29Z", "2026-09-23T04:29:59Z"),
        ("LTCUSDT", "2026-09-23T05:00:57Z", "2026-09-23T08:14:59Z"),
        ("APTUSDT", "2026-09-23T06:00:57Z", "2026-09-23T08:14:59Z"),
        ("ADAUSDT", "2026-09-23T07:00:39Z", "2026-09-23T08:14:59Z"),
    ]
    symbols = [item[0] for item in events]
    history = histories(*symbols)
    journal, shadow, state = tmp_path / "signals.csv", tmp_path / "shadow.csv", tmp_path / "state.json"
    rows: list[dict] = []
    logger = CrossScanExposureShadowLogger(
        shadow, state, journal, prospective_start_timestamp_utc="2026-09-23T00:00:00Z"
    )
    decisions = {}
    for symbol, opened, closed in events:
        current = pd.Timestamp(opened)
        for row in rows:
            if pd.Timestamp(row["_closed"]) <= current:
                row["result"] = "WIN" if row["symbol"] == "XRPUSDT" else "LOSS"
                row["closed_at"] = row["_closed"]
        write_journal(journal, [{key: value for key, value in row.items() if key != "_closed"} for row in rows])
        item = candidate(symbol, "LONG", opened)
        decisions[symbol] = logger.log_candidate(item, history, now=current)["shadow_decision"]
        rows.append({**journal_row(symbol, "LONG", opened), "_closed": closed})
    assert decisions == {
        "DOGEUSDT": "ALLOW",
        "ARBUSDT": "WOULD_BLOCK",
        "XRPUSDT": "WOULD_BLOCK",
        "LTCUSDT": "WOULD_BLOCK",
        "APTUSDT": "ALLOW",
        "ADAUSDT": "WOULD_BLOCK",
    }


def test_timestamp_preserving_runtime_cache_is_utc_sorted_unique() -> None:
    frame = pd.DataFrame(
        {
            "close_time": [
                "2026-09-29T02:00:00+07:00",
                "2026-09-28T18:00:00Z",
                "2026-09-28T18:00:00Z",
                "2026-09-28T20:00:00Z",
            ],
            "close": [103, 100, 101, 104],
        }
    )
    cached = timestamped_close_series(frame, now="2026-09-28T19:30:00Z")
    assert isinstance(cached.index, pd.DatetimeIndex)
    assert str(cached.index.tz) == "UTC"
    assert cached.index.is_monotonic_increasing
    assert cached.index.is_unique
    assert cached.to_dict() == {
        pd.Timestamp("2026-09-28T18:00:00Z"): 101,
        pd.Timestamp("2026-09-28T19:00:00Z"): 103,
    }
    assert "timestamped_close_series(df_1h)" in inspect.getsource(cornix_agent.AgentRunner.scan_symbol)


def test_returns_align_prices_before_missing_candle_calculation() -> None:
    index = pd.date_range("2026-09-20", periods=90, freq="h", tz="UTC")
    prices = pd.Series(np.linspace(100, 160, len(index)), index=index)
    missing_middle = prices.drop(index[[10, 11, 42]])
    correlation, observations = pair_return_correlation(prices, missing_middle)
    assert observations == 72
    assert correlation == pytest.approx(1.0, abs=1e-12)


def test_returns_align_unequal_out_of_order_histories() -> None:
    index = pd.date_range("2026-09-20", periods=100, freq="h", tz="UTC")
    returns = np.sin(np.arange(99) / 7) * 0.005
    prices = pd.Series(np.r_[100, 100 * np.cumprod(1 + returns)], index=index)
    unequal = prices.iloc[13:91].sample(frac=1, random_state=9)
    correlation, observations = pair_return_correlation(prices, unequal)
    assert observations == 72
    assert correlation == pytest.approx(1.0, abs=1e-12)


def test_correlation_edge_cases_inverse_flat_nan_and_insufficient() -> None:
    index = pd.date_range("2026-09-20", periods=90, freq="h", tz="UTC")
    returns = np.sin(np.arange(89) / 5) * 0.004
    first = pd.Series(np.r_[100, 100 * np.cumprod(1 + returns)], index=index)
    inverse = pd.Series(np.r_[100, 100 * np.cumprod(1 - returns)], index=index)
    flat = pd.Series(100.0, index=index)
    with_nan = first.copy()
    with_nan.iloc[12:16] = np.nan
    assert pair_return_correlation(first, inverse)[0] == pytest.approx(-1.0, abs=2e-4)
    assert pair_return_correlation(first, flat)[0] is None
    assert pair_return_correlation(first, with_nan)[0] == pytest.approx(1.0, abs=1e-12)
    assert pair_return_correlation(first.iloc[:30], inverse.iloc[:30]) == (None, 29)
    assert pair_return_correlation(first.reset_index(drop=True), inverse.reset_index(drop=True))[0] is None


def test_boundary_before_excluded_exact_and_after_included(tmp_path: Path) -> None:
    journal = tmp_path / "signals.csv"
    write_journal(journal, [])
    logger = CrossScanExposureShadowLogger(
        tmp_path / "shadow.csv",
        tmp_path / "state.json",
        journal,
        prospective_start_timestamp_utc="2026-09-29T04:00:00Z",
    )
    before = logger.log_candidate(candidate("BEFOREUSDT", timestamp="2026-09-29T03:59:59.999999Z"), {})
    exact = logger.log_candidate(candidate("EXACTUSDT", timestamp="2026-09-29T04:00:00Z"), {})
    after = logger.log_candidate(candidate("AFTERUSDT", timestamp="2026-09-29T04:00:00.000001Z"), {})
    saved = pd.read_csv(logger.path)
    assert before["persistence_status"] == "EXCLUDED_PRE_BOUNDARY"
    assert exact["persistence_status"] == "PERSISTED"
    assert after["persistence_status"] == "PERSISTED"
    assert set(saved["symbol"]) == {"EXACTUSDT", "AFTERUSDT"}


def test_preboundary_historical_candidate_never_enters_primary_or_enrichment(tmp_path: Path) -> None:
    journal = tmp_path / "signals.csv"
    write_journal(
        journal,
        [journal_row("DOGEUSDT", "LONG", "2026-09-23T03:00:39Z", "LOSS", net_r_estimate=-1)],
    )
    logger = CrossScanExposureShadowLogger(
        tmp_path / "shadow.csv", tmp_path / "state.json", journal,
        prospective_start_timestamp_utc="2026-09-29T00:00:00Z",
    )
    result = logger.log_candidate(candidate("DOGEUSDT", timestamp="2026-09-23T03:00:39Z"), {})
    assert result["persistence_status"] == "EXCLUDED_PRE_BOUNDARY"
    assert pd.read_csv(logger.path).empty
    assert logger.refresh_outcomes() == 0
    assert pd.read_csv(logger.path).empty


def test_atomic_state_failure_preserves_old_valid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "state.json"
    target.write_text('{"old": true}', encoding="utf-8")
    monkeypatch.setattr(cross_scan_shadow.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError, match="boom"):
        atomic_write_text(target, '{"new": true}')
    assert target.read_text(encoding="utf-8") == '{"old": true}'
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_atomic_csv_failure_preserves_old_valid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "shadow.csv"
    atomic_write_csv(target, [{"canonical_signal_key": "old", "signal_key": "old"}])
    before = target.read_bytes()
    monkeypatch.setattr(cross_scan_shadow.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError, match="boom"):
        atomic_write_csv(target, [{"canonical_signal_key": "new", "signal_key": "new"}])
    assert target.read_bytes() == before
    assert not list(tmp_path.glob(".shadow.csv.*.tmp"))


def test_concurrent_duplicate_prevention(tmp_path: Path) -> None:
    journal = tmp_path / "signals.csv"
    write_journal(journal, [])
    path, state = tmp_path / "shadow.csv", tmp_path / "state.json"
    first = CrossScanExposureShadowLogger(
        path, state, journal, prospective_start_timestamp_utc="2026-09-29T00:00:00Z", lock_timeout_seconds=2
    )
    second = CrossScanExposureShadowLogger(path, state, journal, lock_timeout_seconds=2)
    signal = candidate()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda logger: logger.log_candidate(signal, {}), [first, second]))
    saved = pd.read_csv(path)
    assert len(saved) == 1
    assert sorted(result["persistence_status"] for result in results) == [
        "DUPLICATE_NOT_WRITTEN", "PERSISTED"
    ]


def test_lock_contention_fails_open_without_uncertain_write(tmp_path: Path) -> None:
    journal = tmp_path / "signals.csv"
    write_journal(journal, [])
    logger = CrossScanExposureShadowLogger(
        tmp_path / "shadow.csv", tmp_path / "state.json", journal,
        prospective_start_timestamp_utc="2026-09-29T00:00:00Z", lock_timeout_seconds=0.02,
    )
    with ShadowPersistenceLock(logger.lock_path, timeout_seconds=1):
        result = logger.log_candidate(candidate(), {})
    assert result["persistence_status"] == "LOCK_UNAVAILABLE_NOT_WRITTEN"
    assert pd.read_csv(logger.path).empty
    assert result["live_result"] == "SENT_UNCHANGED"


def test_noop_enrichment_does_not_rewrite_csv(tmp_path: Path) -> None:
    journal = tmp_path / "signals.csv"
    signal = candidate()
    write_journal(
        journal,
        [journal_row("ETHUSDT", "LONG", signal.timestamp.isoformat(), "WIN", hit_target="TP1", net_r_estimate=1.2)],
    )
    logger = CrossScanExposureShadowLogger(
        tmp_path / "shadow.csv", tmp_path / "state.json", journal,
        prospective_start_timestamp_utc="2026-09-29T00:00:00Z",
    )
    logger.log_candidate(signal, histories("ETHUSDT"), now=NOW)
    assert logger.refresh_outcomes() == 1
    before_bytes = logger.path.read_bytes()
    before_mtime = logger.path.stat().st_mtime_ns
    assert logger.refresh_outcomes() == 0
    assert logger.path.read_bytes() == before_bytes
    assert logger.path.stat().st_mtime_ns == before_mtime
