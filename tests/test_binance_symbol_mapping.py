from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pandas as pd
import pytest
import requests

import cornix_agent
import position_manager
import position_watcher
import review_signals
from core import moving_sl_prospective_shadow
from core import pullback_retest_outcome_shadow
from core import rejected_outcome_shadow
from core.binance_symbols import BINANCE_USDM_MARKET_SYMBOLS, binance_usdm_market_symbol
from core.research_telemetry import make_candidate_key
from core.signal_identity import canonical_signal_key


EXPECTED_MAPPING = {
    "PEPEUSDT": "1000PEPEUSDT",
    "FLOKIUSDT": "1000FLOKIUSDT",
    "BONKUSDT": "1000BONKUSDT",
}


def candle_rows(count: int = 1) -> list[list[Any]]:
    return [
        [
            1_800_000_000_000 + index * 900_000,
            "10",
            "11",
            "9",
            "10.5",
            "100",
            1_800_000_899_999 + index * 900_000,
            "0",
            1,
            "0",
            "0",
            "0",
        ]
        for index in range(count)
    ]


class FakeResponse:
    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self.payload


class FakeSession:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict[str, Any], int]] = []

    def get(self, url: str, params: dict[str, Any] | None = None, timeout: int = 0) -> FakeResponse:
        self.calls.append((url, params or {}, timeout))
        return FakeResponse(self.payload)


def pepe_signal() -> cornix_agent.TradeSignal:
    return cornix_agent.TradeSignal(
        timestamp=datetime(2026, 10, 8, tzinfo=timezone.utc),
        symbol="PEPEUSDT",
        watchlist_tier="C",
        tradingview_symbol="BINANCE:PEPEUSDT.P",
        direction="LONG",
        entry=0.01,
        tp1=0.011,
        tp2=0.012,
        sl=0.009,
        rr=2.0,
        confidence=85,
        score=85,
        support=0.009,
        resistance=0.012,
        regime="Trending",
        regime_details="fixture",
        market_session="Asia",
        htf_regime="Bullish",
        htf_alignment="Aligned",
        volume_spike=True,
        volume_ratio=1.5,
        atr_pct=1.0,
        mfi=55.0,
        mfi_confirmed=True,
        body_ratio=0.7,
        opposite_wick_ratio=0.1,
        atr_expansion_ratio=1.2,
        quality_flags="fixture",
        reason="fixture",
    )


@pytest.mark.parametrize("canonical,exchange", EXPECTED_MAPPING.items())
def test_confirmed_usdm_market_mapping(canonical: str, exchange: str) -> None:
    assert binance_usdm_market_symbol(canonical) == exchange
    assert binance_usdm_market_symbol(canonical.lower()) == exchange
    assert binance_usdm_market_symbol(f"BINANCE:{canonical}.P") == exchange


def test_mapping_is_centralized_deterministic_and_falls_back_safely() -> None:
    assert isinstance(BINANCE_USDM_MARKET_SYMBOLS, MappingProxyType)
    assert dict(BINANCE_USDM_MARKET_SYMBOLS) == EXPECTED_MAPPING
    assert binance_usdm_market_symbol("BTCUSDT") == "BTCUSDT"
    assert binance_usdm_market_symbol("UNKNOWN/USDT") == "UNKNOWNUSDT"
    assert {binance_usdm_market_symbol("PEPEUSDT") for _ in range(20)} == {"1000PEPEUSDT"}
    for module in (
        cornix_agent,
        position_manager,
        position_watcher,
        review_signals,
        moving_sl_prospective_shadow,
        rejected_outcome_shadow,
        pullback_retest_outcome_shadow,
    ):
        assert module.binance_usdm_market_symbol is binance_usdm_market_symbol


def test_canonical_identity_messages_and_journal_remain_unchanged(tmp_path: Path) -> None:
    signal = pepe_signal()
    key = canonical_signal_key(
        symbol=signal.symbol,
        side=signal.direction,
        timestamp=signal.timestamp,
        entry=signal.entry,
    )
    candidate_key = make_candidate_key(
        run_id="run-v1-fixture",
        symbol=signal.symbol,
        side=signal.direction,
        closed_candle_time_utc=signal.timestamp,
    )
    assert key == "sig:v1:PEPEUSDT|LONG|2026-10-08T00:00:00Z|0.010000"
    assert "1000PEPEUSDT" not in key
    assert candidate_key != make_candidate_key(
        run_id="run-v1-fixture",
        symbol=binance_usdm_market_symbol(signal.symbol),
        side=signal.direction,
        closed_candle_time_utc=signal.timestamp,
    )

    config = cornix_agent.ScannerConfig.from_env()
    notifier = cornix_agent.TelegramNotifier(config)
    telegram = notifier.build_message(signal)
    cornix = notifier.build_cornix_message(signal)
    assert "PEPEUSDT.P" in telegram
    assert "1000PEPEUSDT" not in telegram
    assert cornix.startswith("LONG PEPEUSDT\n")
    assert "1000PEPEUSDT" not in cornix

    journal_path = tmp_path / "signals.csv"
    cornix_agent.TradeJournalLogger(journal_path).log_signal(signal)
    journal = pd.read_csv(journal_path)
    assert journal.loc[0, "symbol"] == "PEPEUSDT"
    assert signal.symbol == "PEPEUSDT"
    assert signal.score == 85
    assert signal.confidence == 85
    assert signal.rr == 2.0


def test_all_public_market_data_paths_use_one_mapped_request(monkeypatch: pytest.MonkeyPatch) -> None:
    scanner_session = FakeSession(candle_rows(60))
    client = cornix_agent.MarketDataClient(0, session=scanner_session)
    client.fetch_klines("PEPEUSDT", "1h", 60)
    assert len(scanner_session.calls) == 1
    assert scanner_session.calls[0][1]["symbol"] == "1000PEPEUSDT"

    fetches = [
        lambda session: review_signals.fetch_klines(session, "FLOKIUSDT", 1, 2),
        lambda session: moving_sl_prospective_shadow.fetch_futures_klines(
            session,
            "FLOKIUSDT",
            pd.Timestamp("2026-10-08T00:00:00Z"),
            pd.Timestamp("2026-10-08T01:00:00Z"),
        ),
        lambda session: rejected_outcome_shadow.fetch_futures_klines(
            session, "FLOKIUSDT", 1, 2
        ),
        lambda session: pullback_retest_outcome_shadow.fetch_futures_klines(
            session, "FLOKIUSDT", 1, 2
        ),
    ]
    for fetch in fetches:
        session = FakeSession(candle_rows())
        fetch(session)
        assert len(session.calls) == 1
        assert session.calls[0][1]["symbol"] == "1000FLOKIUSDT"

    manager_calls: list[tuple[str, dict[str, Any], int]] = []

    def fake_get(url: str, params: dict[str, Any], timeout: int) -> FakeResponse:
        manager_calls.append((url, params, timeout))
        return FakeResponse(candle_rows())

    monkeypatch.setattr(position_manager.requests, "get", fake_get)
    position_manager.fetch_klines("BONKUSDT", "15m", 1)
    assert len(manager_calls) == 1
    assert manager_calls[0][1]["symbol"] == "1000BONKUSDT"

    watcher_session = FakeSession({"price": "0.0123"})
    assert position_watcher.fetch_current_price(watcher_session, "BONKUSDT") == 0.0123
    assert len(watcher_session.calls) == 1
    assert watcher_session.calls[0][1]["symbol"] == "1000BONKUSDT"


def test_mapped_market_failure_stays_observable_and_fail_open_compatible(caplog: pytest.LogCaptureFixture) -> None:
    class FailingResponse:
        def raise_for_status(self) -> None:
            raise requests.HTTPError("HTTP 400")

    class FailingSession:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def get(self, _url: str, params: dict[str, Any], timeout: int) -> FailingResponse:
            self.calls.append({"params": params, "timeout": timeout})
            return FailingResponse()

    session = FailingSession()
    client = cornix_agent.MarketDataClient(0, session=session)  # type: ignore[arg-type]
    with caplog.at_level("INFO"), pytest.raises(requests.HTTPError, match="HTTP 400"):
        client.fetch_klines("PEPEUSDT", "1h", 200)

    assert len(session.calls) == 1
    assert session.calls[0]["params"]["symbol"] == "1000PEPEUSDT"
    assert "canonical=PEPEUSDT exchange=1000PEPEUSDT" in caplog.text
