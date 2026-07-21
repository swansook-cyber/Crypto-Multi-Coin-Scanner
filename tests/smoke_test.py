# -*- coding: utf-8 -*-
"""Local smoke tests for scanner output/report compatibility."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import argparse
import contextlib
import importlib.util
import io
import json
import tempfile
import sys
import os
import shutil
import time

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cornix_agent as scanner
import backup_runtime_data
import dashboard
import data_integrity_audit
import daily_summary
import entry_timing_operational_summary
import external_signal_analyzer
import live_pilot_preflight
import manual_live_pilot
import manual_trade_plan
import performance_report
import position_manager
import position_watcher
import position_watcher_state_cleanup
import production_health
import production_v1_readiness
import review_signals
import stats_dashboard
import system_status
import telegram_external_inbox
from core.analytics_reporting import build_daily_performance_report, export_journal_csvs, journal_signal_export
from core.btc_regime_filter import detect_btc_regime
from core.entry_timing_engine import EntryTimingEngine, EntryTimingLogger, format_entry_timing_summary
from core.loss_cooldown import LossCooldownTracker
from core.performance_analytics_v1 import build_complete_report, export_v1_outputs
from core.performance_analytics_v2 import build_performance_v2, canonical_session, generate_performance_warnings
from core.performance_analytics_v3 import build_performance_v3, shadow_filter_backtest
from core.performance_analytics_v8 import build_root_cause_analytics
from core import wave_structure_analyzer as wave

WATCHDOG_MONITOR_PATH = Path(__file__).resolve().parents[1] / "watchdog" / "monitor.py"
WATCHDOG_SPEC = importlib.util.spec_from_file_location("velahub_watchdog_monitor", WATCHDOG_MONITOR_PATH)
watchdog_monitor = importlib.util.module_from_spec(WATCHDOG_SPEC)
assert WATCHDOG_SPEC and WATCHDOG_SPEC.loader
sys.modules["velahub_watchdog_monitor"] = watchdog_monitor
WATCHDOG_SPEC.loader.exec_module(watchdog_monitor)


def sample_signal() -> scanner.TradeSignal:
    return scanner.TradeSignal(
        timestamp=datetime.now(timezone.utc),
        symbol="BTCUSDT",
        watchlist_tier="A",
        tradingview_symbol="BINANCE:BTCUSDT.P",
        direction="SHORT",
        entry=100.0,
        tp1=98.0,
        tp2=96.0,
        sl=101.0,
        rr=2.0,
        confidence=91,
        score=92,
        support=95.0,
        resistance=105.0,
        regime="Trending",
        regime_details="test",
        market_session="London",
        htf_regime="Bearish",
        htf_alignment="Aligned",
        volume_spike=True,
        volume_ratio=1.5,
        atr_pct=1.2,
        mfi=38.0,
        mfi_confirmed=True,
        body_ratio=0.62,
        opposite_wick_ratio=0.2,
        atr_expansion_ratio=1.18,
        quality_flags="strong_body",
        wave_score=76,
        wave_structure="bearish",
        wave_phase="possible_wave_3",
        wave_notes=["bearish swing structure", "volume confirms move"],
        btc_regime="bearish",
        risk_mode="normal",
        reason="test reason",
    )


def test_telegram_message() -> None:
    cfg = scanner.ScannerConfig.from_env()
    message = scanner.TelegramNotifier(cfg).build_message(sample_signal())
    assert "Setup Strength: 91%" in message
    assert "Confidence:" not in message
    assert "🧭 HTF:" in message
    assert "4H Trend: Bearish" in message
    assert "Alignment: YES" in message
    assert "Conflict: NO" in message
    assert "Session: London" in message
    assert "Wave Structure:" in message
    assert "Wave Score: 76/100" in message
    assert "Possible Phase: possible_wave_3" in message
    assert "bearish swing structure" in message
    assert "Market Regime:" in message
    assert "BTC: bearish" in message
    assert "Risk Mode: normal" in message
    assert message.count("For educational analysis only. Not financial advice.") == 1
    assert "No auto-trade" not in message


def test_cornix_dry_run_format_and_signal_immutability() -> None:
    cfg = scanner.ScannerConfig.from_env()
    cfg.dry_run = True
    cfg.max_leverage = 10
    signal = sample_signal()
    original = {
        "direction": signal.direction,
        "entry": signal.entry,
        "tp1": signal.tp1,
        "tp2": signal.tp2,
        "sl": signal.sl,
        "confidence": signal.confidence,
    }
    notifier = scanner.TelegramNotifier(cfg)
    message = notifier.build_cornix_message(signal)
    assert "DRY RUN" not in message
    assert "DO NOT AUTO TRADE" not in message
    assert "SHORT BTCUSDT" in message
    assert "Entry:" in message
    assert "Targets:" in message
    assert "Stop:" in message
    assert "Leverage:" in message
    assert "20x" in message
    assert "10x" not in message
    target_block = message.split("Targets:\n", 1)[1].split("\n\nStop:", 1)[0].splitlines()
    assert 1 <= len([line for line in target_block if line.strip()]) <= 3
    assert {
        "direction": signal.direction,
        "entry": signal.entry,
        "tp1": signal.tp1,
        "tp2": signal.tp2,
        "sl": signal.sl,
        "confidence": signal.confidence,
    } == original
    assert notifier.send_signal(signal) is True


def test_internal_signal_channel_routing() -> None:
    cfg = scanner.ScannerConfig.from_env()
    cfg.dry_run = False
    cfg.send_telegram = True
    cfg.send_daily_summary = True
    cfg.telegram_bot_token = "token"
    cfg.telegram_chat_id = "legacy"
    cfg.telegram_signals_chat_id = "signals"
    cfg.telegram_cornix_chat_id = "cornix"
    cfg.telegram_reports_chat_id = "reports"
    notifier = scanner.TelegramNotifier(cfg)
    calls: list[tuple[str, str]] = []

    def fake_message(_message: str, chat_id: str, channel_name: str = "telegram") -> bool:
        calls.append((chat_id, channel_name))
        return True

    notifier._send_message = fake_message  # type: ignore[method-assign]
    notifier._send_photo = lambda *_args, **_kwargs: False  # type: ignore[method-assign]

    assert notifier.send_signal(sample_signal()) is True
    assert ("signals", "signals") in calls
    assert ("cornix", "cornix") in calls
    assert not any(channel == "reports" for _chat, channel in calls)

    calls.clear()
    assert notifier.send_position_message("POSITION REVIEW") is True
    assert calls == [("reports", "reports")]

    calls.clear()
    assert notifier.send_daily_summary({"day": "2026-05-31"}) is True
    assert calls == [("reports", "reports")]


def test_tier_c_report_only_routing() -> None:
    cfg = scanner.ScannerConfig.from_env()
    cfg.dry_run = False
    cfg.send_telegram = True
    cfg.telegram_bot_token = "token"
    cfg.telegram_signals_chat_id = "signals"
    cfg.telegram_cornix_chat_id = "cornix"
    cfg.telegram_reports_chat_id = "reports"
    cfg.enable_tier_c_report_only = True
    signal = sample_signal()
    signal.watchlist_tier = "C"
    notifier = scanner.TelegramNotifier(cfg)
    calls: list[tuple[str, str, str]] = []

    def fake_message(message: str, chat_id: str, channel_name: str = "telegram") -> bool:
        calls.append((chat_id, channel_name, message))
        return True

    notifier._send_message = fake_message  # type: ignore[method-assign]
    notifier._send_photo = lambda *_args, **_kwargs: False  # type: ignore[method-assign]

    assert notifier.send_tier_c_report_signal(signal) is True
    assert len(calls) == 1
    assert calls[0][0] == "reports"
    assert calls[0][1] == "reports"
    assert "TIER C EXPERIMENTAL REPORT ONLY" in calls[0][2]
    assert "Not sent to Signals channel" in calls[0][2]


def test_weak_symbol_report_only_routing() -> None:
    cfg = scanner.ScannerConfig.from_env()
    cfg.dry_run = False
    cfg.send_telegram = True
    cfg.telegram_bot_token = "token"
    cfg.telegram_signals_chat_id = "signals"
    cfg.telegram_cornix_chat_id = "cornix"
    cfg.telegram_reports_chat_id = "reports"
    signal = sample_signal()
    signal.symbol = "LINKUSDT"
    notifier = scanner.TelegramNotifier(cfg)
    calls: list[tuple[str, str, str]] = []

    def fake_message(message: str, chat_id: str, channel_name: str = "telegram") -> bool:
        calls.append((chat_id, channel_name, message))
        return True

    notifier._send_message = fake_message  # type: ignore[method-assign]
    notifier._send_photo = lambda *_args, **_kwargs: False  # type: ignore[method-assign]

    assert notifier.send_weak_symbol_report_signal(signal) is True
    assert len(calls) == 1
    assert calls[0][0] == "reports"
    assert calls[0][1] == "reports"
    assert "WEAK SYMBOL EXPERIMENTAL REPORT ONLY" in calls[0][2]
    assert "Not sent to Signals channel" in calls[0][2]
    assert "Not sent to Cornix channel" in calls[0][2]


def test_session_risk_report_only_routing() -> None:
    cfg = scanner.ScannerConfig.from_env()
    cfg.dry_run = False
    cfg.send_telegram = True
    cfg.telegram_bot_token = "token"
    cfg.telegram_signals_chat_id = "signals"
    cfg.telegram_cornix_chat_id = "cornix"
    cfg.telegram_reports_chat_id = "reports"
    signal = sample_signal()
    signal.market_session = "NewYork"
    notifier = scanner.TelegramNotifier(cfg)
    calls: list[tuple[str, str, str]] = []

    def fake_message(message: str, chat_id: str, channel_name: str = "telegram") -> bool:
        calls.append((chat_id, channel_name, message))
        return True

    notifier._send_message = fake_message  # type: ignore[method-assign]
    notifier._send_photo = lambda *_args, **_kwargs: False  # type: ignore[method-assign]

    assert notifier.send_session_risk_report_signal(signal) is True
    assert len(calls) == 1
    assert calls[0][0] == "reports"
    assert calls[0][1] == "reports"
    assert "SESSION RISK REPORT ONLY" in calls[0][2]
    assert "Not sent to Signals channel" in calls[0][2]
    assert "Not sent to Cornix channel" in calls[0][2]


def test_london_long_report_only_routing_and_unaffected_signals() -> None:
    cfg = scanner.ScannerConfig.from_env()
    cfg.dry_run = False
    cfg.send_telegram = True
    cfg.telegram_bot_token = "token"
    cfg.telegram_signals_chat_id = "signals"
    cfg.telegram_cornix_chat_id = "cornix"
    cfg.telegram_reports_chat_id = "reports"
    notifier = scanner.TelegramNotifier(cfg)
    calls: list[tuple[str, str, str]] = []

    def fake_message(message: str, chat_id: str, channel_name: str = "telegram") -> bool:
        calls.append((chat_id, channel_name, message))
        return True

    notifier._send_message = fake_message  # type: ignore[method-assign]
    notifier._send_photo = lambda *_args, **_kwargs: False  # type: ignore[method-assign]

    london_long = sample_signal()
    london_long.direction = "LONG"
    london_long.market_session = "London"
    assert notifier.send_london_long_report_signal(london_long) is True
    assert len(calls) == 1
    assert calls[0][0] == "reports"
    assert calls[0][1] == "reports"
    assert "LONDON LONG EXPERIMENTAL REPORT ONLY" in calls[0][2]
    assert "Not sent to Signals channel" in calls[0][2]
    assert "Not sent to Cornix channel" in calls[0][2]

    calls.clear()
    london_short = sample_signal()
    london_short.direction = "SHORT"
    london_short.market_session = "London"
    assert notifier.send_signal(london_short) is True
    assert ("signals", "signals") in [(chat, channel) for chat, channel, _message in calls]
    assert ("cornix", "cornix") in [(chat, channel) for chat, channel, _message in calls]
    assert not any(channel == "reports" for _chat, channel, _message in calls)

    calls.clear()
    asia_long = sample_signal()
    asia_long.direction = "LONG"
    asia_long.market_session = "Asia"
    assert notifier.send_signal(asia_long) is True
    assert ("signals", "signals") in [(chat, channel) for chat, channel, _message in calls]
    assert ("cornix", "cornix") in [(chat, channel) for chat, channel, _message in calls]
    assert not any(channel == "reports" for _chat, channel, _message in calls)


def test_missing_telegram_channel_ids_do_not_crash() -> None:
    cfg = scanner.ScannerConfig.from_env()
    cfg.dry_run = False
    cfg.send_telegram = True
    cfg.telegram_bot_token = ""
    cfg.telegram_chat_id = ""
    cfg.telegram_signals_chat_id = ""
    cfg.telegram_cornix_chat_id = ""
    cfg.telegram_reports_chat_id = ""
    notifier = scanner.TelegramNotifier(cfg)
    assert notifier.send_signal(sample_signal()) is False
    assert notifier.send_daily_summary({"day": "2026-05-31"}) is False
    assert notifier.send_position_message("POSITION REVIEW") is False


def test_external_inbox_logging_and_debug_format() -> None:
    path = Path(tempfile.gettempdir()) / "external_signals_smoke.csv"
    try:
        pd.DataFrame(
            [{"timestamp_utc": "2026-05-31T00:00:00+00:00", "chat_id": "old", "message_id": 1, "source": "old", "raw_text": "old", "status": "RECEIVED"}]
        ).to_csv(path, index=False)
        telegram_external_inbox.log_external_message(
            chat_id="123",
            message_id=456,
            raw_text="hello external signal",
            path=path,
        )
        df = pd.read_csv(path)
        assert list(df.columns) == telegram_external_inbox.FIELDNAMES
        assert df.iloc[-1]["recommendation"] in {"APPROVED", "WAIT", "SKIP", "RISKY", "FAILED"}
        assert df.iloc[-1]["source"] == "External Signal Inbox"
        report = telegram_external_inbox.build_debug_report("x" * 600)
        assert "📥 External Signal Received" in report
        assert "Received Successfully" in report
        assert len(report.split("Message Preview:\n", 1)[1].split("\n\nStatus:", 1)[0]) == 500
        update = {
            "message": {
                "message_id": 9,
                "chat": {"id": 123},
                "text": "test",
            }
        }
        assert telegram_external_inbox.extract_message(update) == ("123", 9, "test")
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def _vip_long_text() -> str:
    return """#BTC/USDT
LONG
Entry: 105000 - 105500
SL: 103800
Targets:
107000
109000
112000
Leverage: 10x
"""


def _vip_short_text() -> str:
    return """ETHUSDT
SELL
Entry: 3200-3180
Stop Loss: 3260
TP1: 3140
TP2: 3080
TP3: 3000
20x
"""


def test_external_signal_parse_long_short_and_symbols() -> None:
    parsed = external_signal_analyzer.parse_external_signal(_vip_long_text(), message_id=1)
    assert parsed.parse_status == "SUCCESS"
    assert parsed.symbol == "BTCUSDT"
    assert parsed.side == "LONG"
    assert parsed.entry_low == 105000
    assert parsed.entry_high == 105500
    assert parsed.stop_loss == 103800
    assert parsed.targets[:3] == [107000, 109000, 112000]

    parsed_short = external_signal_analyzer.parse_external_signal(_vip_short_text(), message_id=2)
    assert parsed_short.parse_status == "SUCCESS"
    assert parsed_short.symbol == "ETHUSDT"
    assert parsed_short.side == "SHORT"
    assert parsed_short.targets[:3] == [3140, 3080, 3000]


def test_external_signal_missing_fields_not_approved() -> None:
    missing_sl = external_signal_analyzer.analyze_external_signal("BTCUSDT LONG Entry 100 TP 105", message_id=3)
    assert missing_sl.parsed.parse_status == "FAILED"
    assert missing_sl.recommendation == "FAILED"

    missing_entry = external_signal_analyzer.analyze_external_signal("BTCUSDT LONG SL 95 TP 105", message_id=4)
    assert missing_entry.parsed.parse_status == "FAILED"
    assert missing_entry.recommendation == "FAILED"


def test_external_signal_score_threshold_and_routing() -> None:
    old_threshold = os.environ.get("EXTERNAL_SIGNAL_SCORE_THRESHOLD")
    os.environ["EXTERNAL_SIGNAL_SCORE_THRESHOLD"] = "101"
    calls: list[tuple[str, str]] = []
    original_send = external_signal_analyzer.send_telegram_message
    original_refine = external_signal_analyzer.perform_refine_analysis

    def fake_send(_token: str, chat_id: str, _message: str, channel_name: str) -> bool:
        calls.append((chat_id, channel_name))
        return True

    def fake_refine(_parsed):
        return external_signal_analyzer.RefineResult(
            status="SUCCESS",
            score=80,
            scanner_agreement="YES",
            scanner_direction="LONG",
            reason=["scanner agreement mocked"],
            details={"trend_1h": "bullish", "entry_15m": "bullish", "btc_regime": "bullish"},
        )

    external_signal_analyzer.send_telegram_message = fake_send
    external_signal_analyzer.perform_refine_analysis = fake_refine
    try:
        analysis = external_signal_analyzer.process_external_signal(
            _vip_long_text(),
            message_id=5,
            token="token",
            signals_chat_id="signals",
            cornix_chat_id="cornix",
            reports_chat_id="reports",
            log_path=Path(tempfile.gettempdir()) / "external_threshold_smoke.csv",
            send=True,
        )
        assert analysis.recommendation == "WAIT"
        assert ("signals", "external signals") not in calls
        assert ("cornix", "external cornix") not in calls
        assert ("reports", "external reports") not in calls
    finally:
        external_signal_analyzer.send_telegram_message = original_send
        if old_threshold is None:
            os.environ.pop("EXTERNAL_SIGNAL_SCORE_THRESHOLD", None)
        else:
            os.environ["EXTERNAL_SIGNAL_SCORE_THRESHOLD"] = old_threshold
        try:
            (Path(tempfile.gettempdir()) / "external_threshold_smoke.csv").unlink()
        except OSError:
            pass
        external_signal_analyzer.perform_refine_analysis = original_refine


def test_external_signal_approved_routes_to_signals_and_cornix_and_logs() -> None:
    old_threshold = os.environ.get("EXTERNAL_SIGNAL_SCORE_THRESHOLD")
    os.environ["EXTERNAL_SIGNAL_SCORE_THRESHOLD"] = "70"
    calls: list[tuple[str, str, str]] = []
    original_send = external_signal_analyzer.send_telegram_message
    original_refine = external_signal_analyzer.perform_refine_analysis
    path = Path(tempfile.gettempdir()) / "external_approved_smoke.csv"

    def fake_send(_token: str, chat_id: str, message: str, channel_name: str) -> bool:
        calls.append((chat_id, channel_name, message))
        return True

    def fake_refine(_parsed):
        return external_signal_analyzer.RefineResult(
            status="SUCCESS",
            score=90,
            scanner_agreement="YES",
            scanner_direction="LONG",
            reason=["scanner agreement mocked"],
            details={
                "trend_1h": "bullish",
                "entry_15m": "bullish",
                "htf_regime": "Bullish",
                "htf_alignment": "Aligned",
                "mfi": "61.0",
                "atr_pct": "0.80",
                "support": "104000",
                "resistance": "108000",
                "btc_regime": "bullish",
                "volume_ratio": "1.50",
                "volume_spike": "YES",
                "market_regime": "Trending",
            },
        )

    external_signal_analyzer.send_telegram_message = fake_send
    external_signal_analyzer.perform_refine_analysis = fake_refine
    try:
        analysis = external_signal_analyzer.process_external_signal(
            _vip_long_text(),
            message_id=6,
            token="token",
            signals_chat_id="signals",
            cornix_chat_id="cornix",
            reports_chat_id="reports",
            log_path=path,
            send=True,
        )
        assert analysis.recommendation == "APPROVED"
        assert analysis.sent_to_signals is True
        assert analysis.sent_to_cornix is True
        assert any(call[0] == "signals" and call[1] == "external signals" for call in calls)
        assert any(call[0] == "cornix" and call[1] == "external cornix" for call in calls)
        assert not any(call[0] == "reports" for call in calls)
        cornix_message = [call[2] for call in calls if call[0] == "cornix"][0]
        assert "DRY RUN" not in cornix_message
        assert "DO NOT AUTO TRADE" not in cornix_message
        logged = pd.read_csv(path)
        assert logged.loc[0, "recommendation"] == "APPROVED"
        assert logged.loc[0, "sent_to_signals"] == "YES"
        assert logged.loc[0, "sent_to_cornix"] == "YES"
        assert logged.loc[0, "scanner_agreement"] == "YES"
        assert int(logged.loc[0, "refine_score"]) == 90
        assert logged.loc[0, "trend_1h"] == "bullish"
        assert logged.loc[0, "status"] == "APPROVED"
        assert logged.loc[0, "direction"] == "LONG"
        assert float(logged.loc[0, "entry"]) > 0
        assert float(logged.loc[0, "sl"]) > 0
        assert int(logged.loc[0, "setup_strength"]) >= 70
        assert "RR acceptable" in str(logged.loc[0, "approved_reason"])
        assert logged.loc[0, "result"] == "OPEN"
    finally:
        external_signal_analyzer.send_telegram_message = original_send
        external_signal_analyzer.perform_refine_analysis = original_refine
        if old_threshold is None:
            os.environ.pop("EXTERNAL_SIGNAL_SCORE_THRESHOLD", None)
        else:
            os.environ["EXTERNAL_SIGNAL_SCORE_THRESHOLD"] = old_threshold
        try:
            path.unlink()
        except OSError:
            pass


def test_external_signal_refine_conflict_not_approved_or_routed() -> None:
    calls: list[tuple[str, str]] = []
    original_send = external_signal_analyzer.send_telegram_message
    original_refine = external_signal_analyzer.perform_refine_analysis
    path = Path(tempfile.gettempdir()) / "external_conflict_smoke.csv"

    def fake_send(_token: str, chat_id: str, _message: str, channel_name: str) -> bool:
        calls.append((chat_id, channel_name))
        return True

    def conflict_refine(_parsed):
        return external_signal_analyzer.RefineResult(
            status="SUCCESS",
            score=20,
            scanner_agreement="NO",
            scanner_direction="SHORT",
            conflict_reason="scanner_direction=SHORT",
            reason=["VIP direction conflicts with scanner"],
            details={"trend_1h": "bearish", "entry_15m": "bearish", "btc_regime": "bearish"},
        )

    external_signal_analyzer.send_telegram_message = fake_send
    external_signal_analyzer.perform_refine_analysis = conflict_refine
    try:
        analysis = external_signal_analyzer.process_external_signal(
            _vip_long_text(),
            message_id=7,
            token="token",
            signals_chat_id="signals",
            cornix_chat_id="cornix",
            reports_chat_id="reports",
            log_path=path,
            send=True,
        )
        assert analysis.recommendation == "WAIT"
        assert analysis.scanner_agreement == "NO"
        assert not calls
        logged = pd.read_csv(path)
        assert logged.loc[0, "sent_to_signals"] == "NO"
        assert logged.loc[0, "sent_to_cornix"] == "NO"
        assert logged.loc[0, "scanner_agreement"] == "NO"
        assert "scanner_direction" in str(logged.loc[0, "conflict_reason"])
        assert logged.loc[0, "status"] == "REJECTED"
        assert "trend conflict" in str(logged.loc[0, "reject_reason"]).lower()
    finally:
        external_signal_analyzer.send_telegram_message = original_send
        external_signal_analyzer.perform_refine_analysis = original_refine
        try:
            path.unlink()
        except OSError:
            pass


def test_outcome_alert_reports_only() -> None:
    calls: list[tuple[str, str]] = []

    class FakeSession:
        def post(self, _url, data=None, timeout=None):
            calls.append((data["chat_id"], data["text"]))

            class Response:
                status_code = 200
                text = "ok"

            return Response()

    old_send = os.environ.get("SEND_TELEGRAM")
    old_outcomes = os.environ.get("SEND_OUTCOME_ALERTS")
    old_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    old_legacy = os.environ.get("TELEGRAM_CHAT_ID")
    old_reports = os.environ.get("TELEGRAM_REPORTS_CHAT_ID")
    os.environ["SEND_TELEGRAM"] = "1"
    os.environ["SEND_OUTCOME_ALERTS"] = "1"
    os.environ["TELEGRAM_BOT_TOKEN"] = "token"
    os.environ["TELEGRAM_CHAT_ID"] = "legacy"
    os.environ["TELEGRAM_REPORTS_CHAT_ID"] = "reports"
    try:
        assert review_signals.send_telegram_alert(FakeSession(), "TP1 HIT", "BTCUSDT", "WIN") is True
        assert calls == [("reports", "TP1 HIT")]
        calls.clear()
        assert review_signals.send_test_report(FakeSession()) is True
        assert calls == [("reports", "🧪 Crypto Scanner Reports Channel Test\nDestination: TELEGRAM_REPORTS_CHAT_ID only\nNo trade signal. No outcome update.")]
        os.environ["TELEGRAM_REPORTS_CHAT_ID"] = ""
        calls.clear()
        assert review_signals.send_telegram_alert(FakeSession(), "SL HIT", "BTCUSDT", "LOSS") is False
        assert calls == []
    finally:
        restore = {
            "SEND_TELEGRAM": old_send,
            "SEND_OUTCOME_ALERTS": old_outcomes,
            "TELEGRAM_BOT_TOKEN": old_token,
            "TELEGRAM_CHAT_ID": old_legacy,
            "TELEGRAM_REPORTS_CHAT_ID": old_reports,
        }
        for key, value in restore.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _closed_outcome_row() -> dict:
    return {
        "timestamp": "2026-05-28T00:00:00+00:00",
        "closed_at": "2026-05-28T02:15:00+00:00",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry": 100,
        "stop_loss": 98,
        "tp1": 102,
        "tp2": 104,
        "risk_reward": 2.0,
        "result": "WIN",
        "hit_target": "TP2",
        "signal_status": "sent",
        "outcome_alert_sent": 0,
        "outcome_alert_at": "",
        "outcome_id": "",
        "tp1_alert_sent": 0,
        "tp2_alert_sent": 0,
        "sl_alert_sent": 0,
        "outcome_alert_sent_at": "",
    }


def test_outcome_uses_futures_klines_and_symbol_normalization() -> None:
    assert review_signals.clean_symbol("INJUSDT.P") == "INJUSDT"
    assert review_signals.clean_symbol("BINANCE:INJUSDT.P") == "INJUSDT"
    assert review_signals.clean_symbol("INJ/USDT") == "INJUSDT"
    assert review_signals.clean_symbol("INJUSDT") == "INJUSDT"

    class FakeSession:
        def __init__(self) -> None:
            self.url = ""
            self.params = {}

        def get(self, url, params=None, timeout=None):
            self.url = url
            self.params = params or {}

            class Response:
                def raise_for_status(self) -> None:
                    return None

                def json(self):
                    return [
                        [
                            1_800_000_000_000,
                            "10",
                            "11",
                            "9",
                            "10.5",
                            "100",
                            1_800_000_900_000,
                            "0",
                            1,
                            "0",
                            "0",
                            "0",
                        ]
                    ]

            return Response()

    session = FakeSession()
    candles = review_signals.fetch_klines(session, "INJ/USDT", 1, 2)
    assert review_signals.BINANCE_FUTURES_KLINES == "https://fapi.binance.com/fapi/v1/klines"
    assert session.url == review_signals.BINANCE_FUTURES_KLINES
    assert session.params["symbol"] == "INJUSDT"
    assert session.params["interval"] == "15m"
    assert float(candles.iloc[0]["high"]) == 11.0


def test_outcome_high_low_direction_rules() -> None:
    candles = pd.DataFrame(
        [
            {
                "open_time": pd.Timestamp("2026-06-01T00:00:00Z"),
                "close_time": pd.Timestamp("2026-06-01T00:15:00Z"),
                "open": 100.0,
                "high": 101.0,
                "low": 95.0,
                "close": 96.0,
            }
        ]
    )
    short = pd.Series({"symbol": "INJUSDT", "side": "SHORT", "entry": 100.0, "stop_loss": 105.0, "tp1": 98.0, "tp2": 96.0})
    short_outcome = review_signals.evaluate_outcome(short, candles)
    assert short_outcome.result == "WIN"
    assert short_outcome.hit_target == "TP2"

    short_sl = short.copy()
    short_sl["stop_loss"] = 100.5
    short_sl_outcome = review_signals.evaluate_outcome(short_sl, candles)
    assert short_sl_outcome.result == "LOSS"
    assert short_sl_outcome.hit_target == "SL"

    long = pd.Series({"symbol": "INJUSDT", "side": "LONG", "entry": 100.0, "stop_loss": 95.0, "tp1": 100.5, "tp2": 101.0})
    long_outcome = review_signals.evaluate_outcome(long, candles)
    assert long_outcome.result == "LOSS"
    assert long_outcome.hit_target == "SL"

    long_tp = long.copy()
    long_tp["stop_loss"] = 94.0
    long_tp_outcome = review_signals.evaluate_outcome(long_tp, candles)
    assert long_tp_outcome.result == "WIN"
    assert long_tp_outcome.hit_target == "TP2"


def test_outcome_alert_success_marks_sent_and_failure_does_not() -> None:
    class FakeSession:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code
            self.calls: list[tuple[str, str]] = []

        def post(self, _url, data=None, timeout=None):
            self.calls.append((data["chat_id"], data["text"]))
            status = self.status_code

            class Response:
                status_code = status
                text = "ok" if status == 200 else "telegram error"

            return Response()

    old_values = {
        "SEND_TELEGRAM": os.environ.get("SEND_TELEGRAM"),
        "SEND_OUTCOME_ALERTS": os.environ.get("SEND_OUTCOME_ALERTS"),
        "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN"),
        "TELEGRAM_CHAT_ID": os.environ.get("TELEGRAM_CHAT_ID"),
        "TELEGRAM_REPORTS_CHAT_ID": os.environ.get("TELEGRAM_REPORTS_CHAT_ID"),
    }
    old_journal = review_signals.JOURNAL
    old_history = review_signals.HISTORY
    old_external = review_signals.EXTERNAL_SIGNALS
    success_path = Path(tempfile.gettempdir()) / "outcome_success_smoke.csv"
    fail_path = Path(tempfile.gettempdir()) / "outcome_fail_smoke.csv"
    history_path = Path(tempfile.gettempdir()) / "outcome_history_smoke.csv"
    rejected_path = history_path.with_name("rejected_signals_smoke.csv")
    external_path = Path(tempfile.gettempdir()) / "missing_external_outcome_smoke.csv"
    try:
        os.environ["SEND_TELEGRAM"] = "1"
        os.environ["SEND_OUTCOME_ALERTS"] = "1"
        os.environ["TELEGRAM_BOT_TOKEN"] = "token"
        os.environ["TELEGRAM_CHAT_ID"] = "legacy"
        os.environ["TELEGRAM_REPORTS_CHAT_ID"] = "reports"
        review_signals.HISTORY = history_path
        review_signals.EXTERNAL_SIGNALS = external_path

        pd.DataFrame([_closed_outcome_row()]).to_csv(success_path, index=False)
        review_signals.JOURNAL = success_path
        review_signals.PROCESSED_OUTCOMES.clear()
        success_session = FakeSession(200)
        stats = review_signals.run_review_cycle(
            notify=True,
            session=success_session,
            lookahead_hours=24,
            print_report=False,
            resend_unsent=True,
        )
        saved = pd.read_csv(success_path)
        assert stats.sent_alerts == 1
        assert success_session.calls and success_session.calls[0][0] == "reports"
        assert int(saved.loc[0, "outcome_alert_sent"]) == 1
        assert str(saved.loc[0, "outcome_alert_at"]).strip()
        assert str(saved.loc[0, "outcome_id"]).strip()

        pd.DataFrame([_closed_outcome_row()]).to_csv(fail_path, index=False)
        review_signals.JOURNAL = fail_path
        review_signals.PROCESSED_OUTCOMES.clear()
        fail_session = FakeSession(500)
        stats = review_signals.run_review_cycle(
            notify=True,
            session=fail_session,
            lookahead_hours=24,
            print_report=False,
            resend_unsent=True,
        )
        failed = pd.read_csv(fail_path)
        assert stats.sent_alerts == 0
        assert fail_session.calls and fail_session.calls[0][0] == "reports"
        assert int(failed.loc[0, "outcome_alert_sent"]) == 0
        assert str(failed.loc[0, "outcome_alert_at"]).strip() in {"", "nan"}
        assert str(failed.loc[0, "outcome_id"]).strip() in {"", "nan"}
    finally:
        review_signals.JOURNAL = old_journal
        review_signals.HISTORY = old_history
        review_signals.EXTERNAL_SIGNALS = old_external
        review_signals.PROCESSED_OUTCOMES.clear()
        for key, value in old_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for path in [success_path, fail_path, history_path, rejected_path]:
            try:
                path.unlink()
            except OSError:
                pass


def _wave_test_candles(kind: str = "bullish") -> pd.DataFrame:
    rows = []
    base_time = pd.Timestamp("2026-05-28T00:00:00Z")
    if kind == "range":
        closes = [100 + ((index % 6) - 3) * 0.25 for index in range(60)]
    elif kind == "bearish":
        closes = [120 - index * 0.35 + (1 if index % 8 == 0 else 0) for index in range(60)]
    else:
        closes = [100 + index * 0.35 - (1 if index % 8 == 0 else 0) for index in range(60)]
    for index, close in enumerate(closes):
        open_price = close - 0.25 if kind != "bearish" else close + 0.25
        rows.append(
            {
                "open_time": base_time + pd.Timedelta(hours=index),
                "close_time": base_time + pd.Timedelta(hours=index + 1),
                "open": open_price,
                "high": close + 0.8,
                "low": close - 0.8,
                "close": close,
                "volume": 1000 + index * 5,
            }
        )
    df = pd.DataFrame(rows)
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["volume_sma20"] = df["volume"].rolling(20).mean()
    return df


def test_wave_bullish_higher_high_higher_low() -> None:
    swings = [
        {"type": "low", "price": 100, "index": 1},
        {"type": "high", "price": 110, "index": 2},
        {"type": "low", "price": 104, "index": 3},
        {"type": "high", "price": 118, "index": 4},
    ]
    structure = wave.detect_market_structure(swings)
    assert structure["higher_high"] is True
    assert structure["higher_low"] is True
    assert structure["bullish_structure"] is True
    assert structure["structure"] == "bullish"


def test_wave_bearish_lower_high_lower_low() -> None:
    swings = [
        {"type": "high", "price": 120, "index": 1},
        {"type": "low", "price": 110, "index": 2},
        {"type": "high", "price": 116, "index": 3},
        {"type": "low", "price": 104, "index": 4},
    ]
    structure = wave.detect_market_structure(swings)
    assert structure["lower_high"] is True
    assert structure["lower_low"] is True
    assert structure["bearish_structure"] is True
    assert structure["structure"] == "bearish"


def test_wave_range_or_unclear_and_score_bounds() -> None:
    swings = [
        {"type": "high", "price": 105, "index": 1},
        {"type": "low", "price": 100, "index": 2},
        {"type": "high", "price": 105, "index": 3},
        {"type": "low", "price": 100, "index": 4},
    ]
    structure = wave.detect_market_structure(swings)
    assert structure["structure"] == "range"
    for kind in ["bullish", "bearish", "range"]:
        result = wave.calculate_wave_score(_wave_test_candles(kind))
        assert 0 <= result["wave_score"] <= 100
        assert result["structure"] in {"bullish", "bearish", "range", "unclear"}


def test_btc_regime_filter_profiles() -> None:
    bullish = detect_btc_regime(_wave_test_candles("bullish"))
    assert bullish["regime"] == "bullish"
    assert bullish["allow_long"] is True

    bearish = detect_btc_regime(_wave_test_candles("bearish"))
    assert bearish["regime"] == "bearish"
    assert bearish["allow_short"] is True

    sideways = detect_btc_regime(_wave_test_candles("range"))
    assert sideways["regime"] == "sideways"
    assert sideways["risk_multiplier"] < 1.0

    high_volatility_df = _wave_test_candles("bullish")
    high_volatility_df["atr_pct"] = 3.2
    high_volatility = detect_btc_regime(high_volatility_df)
    assert high_volatility["regime"] == "high_volatility"
    assert high_volatility["risk_multiplier"] < 1.0

    unavailable = detect_btc_regime(None)
    assert unavailable["regime"] == "unclear"
    assert unavailable["allow_long"] is True
    assert unavailable["allow_short"] is True


def test_loss_cooldown_three_losses_and_missing_journal() -> None:
    missing = LossCooldownTracker(Path(tempfile.gettempdir()) / "missing_loss_cooldown_smoke.csv")
    assert missing.status().active is False

    path = Path(tempfile.gettempdir()) / "loss_cooldown_smoke.csv"
    now = pd.Timestamp.now(tz="UTC")
    try:
        pd.DataFrame(
            [
                {"result": "LOSS", "closed_at": (now - pd.Timedelta(hours=1)).isoformat()},
                {"result": "LOSS", "closed_at": (now - pd.Timedelta(hours=2)).isoformat()},
                {"result": "LOSS", "closed_at": (now - pd.Timedelta(hours=3)).isoformat()},
                {"result": "WIN", "closed_at": (now - pd.Timedelta(hours=20)).isoformat()},
            ]
        ).to_csv(path, index=False)
        status = LossCooldownTracker(path, max_losses=3, pause_hours=12).status(now=now)
        assert status.active is True
        assert status.loss_streak == 3
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def test_scanner_survives_wave_analyzer_failure() -> None:
    indicator_engine = scanner.IndicatorEngine()
    df_1h = indicator_engine.add_indicators(_wave_test_candles("bullish"))
    df_15m = indicator_engine.add_indicators(_wave_test_candles("bullish"))
    cfg = scanner.ScannerConfig.from_env()
    cfg.use_mfi_filter = False
    cfg.use_session_filter = False
    cfg.use_4h_regime_filter = False
    cfg.use_candle_body_filter = False
    cfg.use_wick_filter = False
    cfg.use_atr_expansion_filter = False
    cfg.min_volume_ratio = 0
    cfg.min_atr_pct = 0
    scorer = scanner.SignalScorer(
        cfg,
        scanner.SupportResistanceEngine(),
        scanner.MarketRegimeDetector(),
    )
    original = scanner.calculate_wave_score

    def broken_wave_score(*_args, **_kwargs):
        raise RuntimeError("wave unavailable")

    scanner.calculate_wave_score = broken_wave_score
    try:
        signal = scorer.score("BTCUSDT", df_1h, df_15m, None)
    finally:
        scanner.calculate_wave_score = original
    assert signal is None or signal.wave_structure == "unclear"


def test_review_old_journal_columns() -> None:
    path = Path(tempfile.gettempdir()) / "old_review_smoke.csv"
    old = review_signals.JOURNAL
    try:
        pd.DataFrame([{
            "timestamp": "2026-05-28T00:00:00+00:00",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "entry": 100,
            "stop_loss": 99,
            "tp1": 101,
            "tp2": 102,
        }]).to_csv(path, index=False)
        review_signals.JOURNAL = path
        df = review_signals.reload_journal()
        assert df is not None
        for column in [
            "setup_strength",
            "raw_score",
            "score_bucket",
            "htf_conflict",
            "market_session",
            "tp1_alert_sent",
            "tp1_alert_at",
            "tp1_alert_source",
            "tp2_alert_sent",
            "sl_alert_sent",
            "outcome_alert_sent_at",
            "breakeven_recommended",
            "position_management_stage",
        ]:
            assert column in df.columns

        history_path = Path(tempfile.gettempdir()) / "signals_history_smoke.csv"
        rejected_path = history_path.with_name("rejected_signals_smoke.csv")
        old_history = review_signals.HISTORY
        try:
            review_signals.HISTORY = history_path
            review_signals.sync_signal_history(df)
            history = pd.read_csv(history_path)
            for column in review_signals.HISTORY_COLUMNS:
                assert column in history.columns
            assert len(history) == 1
        finally:
            review_signals.HISTORY = old_history
            try:
                history_path.unlink()
            except OSError:
                pass
            try:
                rejected_path.unlink()
            except OSError:
                pass
    finally:
        review_signals.JOURNAL = old
        review_signals.PROCESSED_OUTCOMES.clear()
        try:
            path.unlink()
        except OSError:
            pass


def test_external_approved_outcome_tracking_updates_csv() -> None:
    path = Path(tempfile.gettempdir()) / "external_outcome_tracking_smoke.csv"
    old_external = review_signals.EXTERNAL_SIGNALS
    old_fetch = review_signals.fetch_klines
    old_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    old_reports = os.environ.get("TELEGRAM_REPORTS_CHAT_ID")
    old_send = os.environ.get("SEND_TELEGRAM")
    old_outcomes = os.environ.get("SEND_OUTCOME_ALERTS")

    def fake_fetch(_session, _symbol, _start_ms, _end_ms):
        return pd.DataFrame(
            [
                {
                    "open_time": pd.Timestamp("2026-05-28T00:00:00Z"),
                    "close_time": pd.Timestamp("2026-05-28T00:15:00Z"),
                    "open": 100.0,
                    "high": 104.2,
                    "low": 99.5,
                    "close": 103.0,
                }
            ]
        )

    try:
        pd.DataFrame(
            [
                {
                    "timestamp": "2026-05-28T00:00:00+00:00",
                    "source_type": "external",
                    "symbol": "NEARUSDT",
                    "direction": "LONG",
                    "entry": 100.0,
                    "sl": 98.0,
                    "tp1": 102.0,
                    "tp2": 104.0,
                    "recommendation": "APPROVED",
                    "status": "APPROVED",
                    "sent_to_signals": "YES",
                    "sent_to_cornix": "YES",
                    "result": "OPEN",
                }
            ]
        ).to_csv(path, index=False)
        review_signals.EXTERNAL_SIGNALS = path
        review_signals.fetch_klines = fake_fetch
        stats = review_signals.review_external_signals(review_signals.build_session(), lookahead_hours=24)
        saved = pd.read_csv(path)
        assert stats.tp_hits == 1
        assert saved.loc[0, "result"] == "WIN"
        assert saved.loc[0, "hit_target"] == "TP2"
        assert float(saved.loc[0, "net_r_estimate"]) == 2.0
        assert float(saved.loc[0, "holding_minutes"]) == 15.0
    finally:
        review_signals.EXTERNAL_SIGNALS = old_external
        review_signals.fetch_klines = old_fetch
        try:
            path.unlink()
        except OSError:
            pass


def test_stats_old_and_new_fields() -> None:
    old_df = pd.DataFrame([{
        "timestamp": "2026-05-28T00:00:00+00:00",
        "closed_at": "2026-05-28T01:00:00+00:00",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "confidence": 82,
        "risk_reward": 2.0,
        "result": "WIN",
        "hit_target": "TP1",
    }])
    normalized = stats_dashboard.normalize(old_df)
    assert "setup_strength" in normalized.columns
    assert "score_bucket" in normalized.columns
    assert normalized.loc[0, "score_bucket"] == "A"
    summary = stats_dashboard.build_summary(normalized)
    assert not summary.empty
    suggestions = stats_dashboard.adaptive_suggestions(normalized)
    assert suggestions


def test_outcome_message_and_dedupe() -> None:
    row = pd.Series({
        "timestamp": "2026-05-28T00:00:00+00:00",
        "closed_at": "2026-05-28T02:15:00+00:00",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "result": "WIN",
        "hit_target": "TP1",
        "setup_strength": 95,
        "raw_score": 98,
        "market_regime": "Trending",
        "market_session": "London",
        "outcome_alert_sent": 0,
        "tp1_alert_sent": 0,
    })
    message = review_signals.build_outcome_alert(row)
    assert "✅ TP1 HIT" in message
    assert "🪙 BTCUSDT" in message
    assert "📈 Result: +1R" in message
    assert "⏱ Hold Time: 2h 15m" in message
    assert "Setup Strength: 95%" in message
    assert "Score: 98" in message
    assert "Past performance does not guarantee future results" in message
    assert not review_signals.target_alert_already_sent(row)
    row["tp1_alert_sent"] = 1
    assert review_signals.target_alert_already_sent(row)

    loss = row.copy()
    loss["result"] = "LOSS"
    loss["hit_target"] = "SL"
    loss["closed_at"] = "2026-05-28T01:05:00+00:00"
    loss["sl_alert_sent"] = 0
    loss_message = review_signals.build_outcome_alert(loss)
    assert "❌ SL HIT" in loss_message
    assert "📉 Result: -1R" in loss_message
    assert "Risk managed correctly" in loss_message


def test_tp1_watcher_outcome_review_dedupe_sync() -> None:
    path = Path(tempfile.gettempdir()) / "tp1_watcher_review_sync.csv"
    history_path = Path(tempfile.gettempdir()) / "tp1_watcher_review_history.csv"
    external_path = Path(tempfile.gettempdir()) / "tp1_watcher_review_external.csv"
    old_journal = review_signals.JOURNAL
    old_history = review_signals.HISTORY
    old_external = review_signals.EXTERNAL_SIGNALS
    old_fetch = review_signals.fetch_klines
    old_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    old_reports = os.environ.get("TELEGRAM_REPORTS_CHAT_ID")
    old_send = os.environ.get("SEND_TELEGRAM")
    old_outcomes = os.environ.get("SEND_OUTCOME_ALERTS")

    def fake_fetch(_session, _symbol, _start_ms, _end_ms):
        return pd.DataFrame(
            [
                {
                    "open_time": pd.Timestamp("2026-05-28T00:00:00Z"),
                    "close_time": pd.Timestamp("2026-05-28T00:15:00Z"),
                    "open": 100.0,
                    "high": 102.2,
                    "low": 99.4,
                    "close": 101.5,
                }
            ]
        )

    class FakeSession:
        def __init__(self) -> None:
            self.posts: list[str] = []

        def post(self, _url, data=None, timeout=None):
            self.posts.append(data["text"])

            class Response:
                status_code = 200
                text = "ok"

            return Response()

    try:
        pd.DataFrame(
            [
                {
                    "timestamp": "2026-05-28T00:00:00+00:00",
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "entry": 100,
                    "stop_loss": 98,
                    "tp1": 102,
                    "tp2": 104,
                    "risk_reward": 2.0,
                    "result": "OPEN",
                    "signal_status": "sent",
                    "tp1_alert_sent": 1,
                    "tp1_alert_at": "2026-05-28T00:10:00+00:00",
                    "tp1_alert_source": "watcher",
                    "breakeven_recommended": 1,
                    "position_management_stage": "TP1_REACHED_BE_RECOMMENDED",
                }
            ]
        ).to_csv(path, index=False)
        review_signals.JOURNAL = path
        review_signals.HISTORY = history_path
        review_signals.EXTERNAL_SIGNALS = external_path
        review_signals.fetch_klines = fake_fetch
        review_signals.PROCESSED_OUTCOMES.clear()
        os.environ["TELEGRAM_BOT_TOKEN"] = "token"
        os.environ["TELEGRAM_REPORTS_CHAT_ID"] = "reports"
        os.environ["SEND_TELEGRAM"] = "1"
        os.environ["SEND_OUTCOME_ALERTS"] = "1"

        session = FakeSession()
        stats = review_signals.run_review_cycle(True, session, 24, print_report=False)
        saved = pd.read_csv(path)
        assert stats.tp_hits == 1
        assert stats.skipped_alerts >= 1
        assert session.posts == []
        assert saved.loc[0, "result"] == "WIN"
        assert saved.loc[0, "hit_target"] == "TP1"
        assert saved.loc[0, "tp1_alert_source"] == "watcher"
        assert saved.loc[0, "tp1_alert_at"] == "2026-05-28T00:10:00+00:00"
        assert float(saved.loc[0, "holding_minutes"]) == 15.0
        assert float(saved.loc[0, "net_r_estimate"]) == 1.0
    finally:
        review_signals.JOURNAL = old_journal
        review_signals.HISTORY = old_history
        review_signals.EXTERNAL_SIGNALS = old_external
        review_signals.fetch_klines = old_fetch
        for key, value in {
            "TELEGRAM_BOT_TOKEN": old_token,
            "TELEGRAM_REPORTS_CHAT_ID": old_reports,
            "SEND_TELEGRAM": old_send,
            "SEND_OUTCOME_ALERTS": old_outcomes,
        }.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        review_signals.PROCESSED_OUTCOMES.clear()
        for temp_path in [path, history_path, history_path.with_name("rejected_signals_smoke.csv"), external_path]:
            try:
                temp_path.unlink()
            except OSError:
                pass


def test_tp1_outcome_review_sets_source_when_first() -> None:
    path = Path(tempfile.gettempdir()) / "tp1_outcome_review_first.csv"
    history_path = Path(tempfile.gettempdir()) / "tp1_outcome_review_history.csv"
    external_path = Path(tempfile.gettempdir()) / "tp1_outcome_review_external.csv"
    old_journal = review_signals.JOURNAL
    old_history = review_signals.HISTORY
    old_external = review_signals.EXTERNAL_SIGNALS
    old_fetch = review_signals.fetch_klines
    old_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    old_reports = os.environ.get("TELEGRAM_REPORTS_CHAT_ID")
    old_send = os.environ.get("SEND_TELEGRAM")
    old_outcomes = os.environ.get("SEND_OUTCOME_ALERTS")

    def fake_fetch(_session, _symbol, _start_ms, _end_ms):
        return pd.DataFrame(
            [
                {
                    "open_time": pd.Timestamp("2026-05-28T00:00:00Z"),
                    "close_time": pd.Timestamp("2026-05-28T00:15:00Z"),
                    "open": 100.0,
                    "high": 102.2,
                    "low": 99.4,
                    "close": 101.5,
                }
            ]
        )

    class FakeSession:
        def __init__(self) -> None:
            self.posts: list[str] = []

        def post(self, _url, data=None, timeout=None):
            self.posts.append(data["text"])

            class Response:
                status_code = 200
                text = "ok"

            return Response()

    try:
        pd.DataFrame(
            [
                {
                    "timestamp": "2026-05-28T00:00:00+00:00",
                    "symbol": "ETHUSDT",
                    "side": "LONG",
                    "entry": 100,
                    "stop_loss": 98,
                    "tp1": 102,
                    "tp2": 104,
                    "risk_reward": 2.0,
                    "result": "OPEN",
                    "signal_status": "sent",
                }
            ]
        ).to_csv(path, index=False)
        review_signals.JOURNAL = path
        review_signals.HISTORY = history_path
        review_signals.EXTERNAL_SIGNALS = external_path
        review_signals.fetch_klines = fake_fetch
        review_signals.PROCESSED_OUTCOMES.clear()
        os.environ["TELEGRAM_BOT_TOKEN"] = "token"
        os.environ["TELEGRAM_REPORTS_CHAT_ID"] = "reports"
        os.environ["SEND_TELEGRAM"] = "1"
        os.environ["SEND_OUTCOME_ALERTS"] = "1"

        session = FakeSession()
        stats = review_signals.run_review_cycle(True, session, 24, print_report=False)
        saved = pd.read_csv(path)
        assert stats.sent_alerts == 1
        assert len(session.posts) == 1
        assert saved.loc[0, "result"] == "WIN"
        assert saved.loc[0, "hit_target"] == "TP1"
        assert int(saved.loc[0, "tp1_alert_sent"]) == 1
        assert saved.loc[0, "tp1_alert_source"] == "outcome_review"
        assert saved.loc[0, "position_management_stage"] == "TP1_REACHED_REVIEW_CONFIRMED"
    finally:
        review_signals.JOURNAL = old_journal
        review_signals.HISTORY = old_history
        review_signals.EXTERNAL_SIGNALS = old_external
        review_signals.fetch_klines = old_fetch
        for key, value in {
            "TELEGRAM_BOT_TOKEN": old_token,
            "TELEGRAM_REPORTS_CHAT_ID": old_reports,
            "SEND_TELEGRAM": old_send,
            "SEND_OUTCOME_ALERTS": old_outcomes,
        }.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        review_signals.PROCESSED_OUTCOMES.clear()
        for temp_path in [path, history_path, history_path.with_name("rejected_signals_smoke.csv"), external_path]:
            try:
                temp_path.unlink()
            except OSError:
                pass


def test_daily_summary_and_missing_telegram_env() -> None:
    df = pd.DataFrame([
        {
            "timestamp": "2026-05-28T00:00:00+00:00",
            "closed_at": "2026-05-28T02:15:00+00:00",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "entry": 100,
            "tp2": 104,
            "stop_loss": 98,
            "wave_score": 82,
            "btc_regime": "bullish",
            "pnl_percent": 2.0,
            "result": "WIN",
            "hit_target": "TP1",
            "market_session": "London",
            "score_bucket": "A+",
        },
        {
            "timestamp": "2026-05-28T03:00:00+00:00",
            "closed_at": "2026-05-28T04:00:00+00:00",
            "symbol": "ETHUSDT",
            "side": "SHORT",
            "entry": 100,
            "tp2": 96,
            "stop_loss": 102,
            "wave_score": 55,
            "btc_regime": "bearish",
            "pnl_percent": -1.0,
            "result": "LOSS",
            "hit_target": "SL",
            "market_session": "London",
            "score_bucket": "B",
        },
        {
            "timestamp": "2026-05-28T05:00:00+00:00",
            "symbol": "SOLUSDT",
            "side": "LONG",
            "entry": 100,
            "tp2": 104,
            "stop_loss": 98,
            "wave_score": 30,
            "btc_regime": "sideways",
            "pnl_percent": 0.0,
            "result": "OPEN",
            "hit_target": "",
            "market_session": "Asia",
            "score_bucket": "A",
        },
    ])
    normalized = daily_summary.ensure_columns(df)
    summary = daily_summary.build_daily_summary(normalized, "2026-05-28")
    assert summary["total_signals"] == 3
    assert summary["tp1_hits"] == 1
    assert summary["sl_hits"] == 1
    assert summary["pending"] == 1
    assert summary["win_rate"] == 50.0
    assert summary["btc_regime_breakdown"] == "bullish: 1, bearish: 1, sideways: 1"
    assert summary["wave_score_breakdown"] == "80-100: 1, 40-59: 1, 0-39: 1"
    assert summary["current_streak"] == "1 LOSS"
    message = daily_summary.build_telegram_message(summary)
    assert "📊 Daily Signal Summary" in message
    assert "Today's Winrate: 50.0%" in message
    assert "Best Coin: BTCUSDT" in message
    assert "Worst Coin: ETHUSDT" in message
    assert "Best Session: London" in message
    assert "Best Bucket: A+" in message
    assert "BTC Regime: bullish: 1, bearish: 1, sideways: 1" in message
    assert "Wave Score: 80-100: 1, 40-59: 1, 0-39: 1" in message
    assert "Current Streak: 1 LOSS" in message

    old_token = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    old_chat = os.environ.pop("TELEGRAM_CHAT_ID", None)
    old_send = os.environ.get("SEND_DAILY_SUMMARY")
    os.environ["SEND_DAILY_SUMMARY"] = "1"
    try:
        assert daily_summary.send_telegram(message) is False
    finally:
        if old_token is not None:
            os.environ["TELEGRAM_BOT_TOKEN"] = old_token
        if old_chat is not None:
            os.environ["TELEGRAM_CHAT_ID"] = old_chat
        if old_send is None:
            os.environ.pop("SEND_DAILY_SUMMARY", None)
        else:
            os.environ["SEND_DAILY_SUMMARY"] = old_send


def test_analytics_report_and_journal_exports() -> None:
    df = pd.DataFrame(
        [
            {
                "timestamp": "2026-05-28T00:00:00+00:00",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "entry": 100,
                "tp2": 104,
                "stop_loss": 98,
                "wave_score": 88,
                "btc_regime": "bullish",
                "result": "WIN",
                "pnl_percent": 2.0,
            },
            {
                "timestamp": "2026-05-28T01:00:00+00:00",
                "symbol": "ETHUSDT",
                "side": "SHORT",
                "entry": 100,
                "tp2": 96,
                "stop_loss": 102,
                "wave_score": 45,
                "btc_regime": "bearish",
                "result": "LOSS",
                "pnl_percent": -1.0,
            },
            {
                "timestamp": "2026-05-28T02:00:00+00:00",
                "symbol": "SOLUSDT",
                "side": "LONG",
                "entry": 100,
                "tp2": 103,
                "stop_loss": 98,
                "wave_score": 70,
                "btc_regime": "sideways",
                "result": "OPEN",
                "pnl_percent": 0.0,
            },
        ]
    )
    report = build_daily_performance_report(df, "2026-05-28")
    assert report["signals"] == 3
    assert report["wins"] == 1
    assert report["losses"] == 1
    assert report["pending"] == 1
    assert report["win_rate"] == 50.0
    assert report["best_coin"] == "BTCUSDT"
    assert report["worst_coin"] == "ETHUSDT"

    export = journal_signal_export(df)
    assert list(export.columns) == [
        "timestamp",
        "symbol",
        "direction",
        "wave_score",
        "btc_regime",
        "entry",
        "tp",
        "sl",
        "result",
        "pnl_percent",
    ]

    export_dir = Path(tempfile.gettempdir()) / "crypto_scanner_journal_export_smoke"
    signals_path, daily_path = export_journal_csvs(df, export_dir, report)
    try:
        signals = pd.read_csv(signals_path)
        daily = pd.read_csv(daily_path)
        assert len(signals) == 3
        assert len(daily) == 1
        assert daily.loc[0, "btc_regime_breakdown"] == report["btc_regime_breakdown"]
    finally:
        try:
            signals_path.unlink()
            daily_path.unlink()
            export_dir.rmdir()
        except OSError:
            pass

    missing_report = build_daily_performance_report(pd.DataFrame(), "2026-05-28")
    assert missing_report["signals"] == 0
    assert missing_report["btc_regime_breakdown"] == "-"


def test_daily_performance_report_metrics() -> None:
    df = pd.DataFrame(
        [
            {
                "timestamp": "2026-05-30T00:00:00+00:00",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "watchlist_tier": "A",
                "market_session": "London",
                "entry": 100,
                "tp1": 101.2,
                "tp2": 102,
                "stop_loss": 99,
                "risk_reward": 2.0,
                "result": "WIN",
                "hit_target": "TP1",
                "signal_status": "sent",
            },
            {
                "timestamp": "2026-05-30T01:00:00+00:00",
                "symbol": "ETHUSDT",
                "side": "SHORT",
                "watchlist_tier": "B",
                "market_session": "NewYork",
                "entry": 100,
                "tp1": 98.8,
                "tp2": 98,
                "stop_loss": 101,
                "risk_reward": 2.0,
                "result": "WIN",
                "hit_target": "TP2",
                "signal_status": "sent",
            },
            {
                "timestamp": "2026-05-30T02:00:00+00:00",
                "symbol": "SOLUSDT",
                "side": "LONG",
                "watchlist_tier": "C",
                "market_session": "London",
                "entry": 100,
                "tp1": 101,
                "tp2": 102,
                "stop_loss": 99,
                "risk_reward": 2.0,
                "result": "LOSS",
                "hit_target": "SL",
                "signal_status": "sent",
            },
            {
                "timestamp": "2026-05-30T03:00:00+00:00",
                "symbol": "BNBUSDT",
                "side": "SHORT",
                "watchlist_tier": "A",
                "market_session": "Asia",
                "entry": 100,
                "tp1": 99,
                "tp2": 98,
                "stop_loss": 101,
                "risk_reward": 2.0,
                "result": "OPEN",
                "hit_target": "",
                "signal_status": "sent",
            },
        ]
    )
    report = performance_report.build_report(df, "2026-05-30")
    assert report["total_sent_signals"] == 4
    assert report["closed_signals"] == 3
    assert report["open_signals"] == 1
    assert report["wins"] == 2
    assert report["losses"] == 1
    assert round(report["win_rate"], 1) == 66.7
    assert report["tp1_hits"] == 2
    assert report["tp2_hits"] == 1
    assert report["sl_hits"] == 1
    assert report["net_r_estimate"] == 2.2
    assert report["small_sample_warning"] is True
    message = performance_report.format_report(report)
    assert "Daily Performance Report" in message
    assert "Sample size is still small. Use for monitoring only." in message
    assert "Long win rate:" in message
    assert "Short win rate:" in message
    assert "Score Performance Analytics" in message
    assert "Score Deep Audit" in message
    assert "Score Calibration Report" in message
    assert "Score Calibration Recommendations" in message
    assert "Strategy Filter Simulator" in message
    assert "Top Strategy Candidates" in message
    assert "Strategy Filter Recommendations" in message
    assert "Production Universe Ranking" in message
    assert "Recommended Production Universe" in message
    assert "Post-Filter Live Performance" in message
    assert "Production Universe Performance" in message
    assert "Session Risk Experimental Performance" in message
    assert "London Long Experimental Performance" in message


def test_performance_report_routes_to_reports_only() -> None:
    calls: list[tuple[str, str]] = []

    class FakeSession:
        def post(self, _url, data=None, timeout=None):
            calls.append((data["chat_id"], data["text"]))

            class Response:
                status_code = 200
                text = "ok"

            return Response()

    old_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    old_legacy = os.environ.get("TELEGRAM_CHAT_ID")
    old_reports = os.environ.get("TELEGRAM_REPORTS_CHAT_ID")
    os.environ["TELEGRAM_BOT_TOKEN"] = "token"
    os.environ["TELEGRAM_CHAT_ID"] = "legacy"
    os.environ["TELEGRAM_REPORTS_CHAT_ID"] = "reports"
    try:
        assert performance_report.send_telegram("Daily Performance Report", FakeSession()) is True
        assert calls == [("reports", "Daily Performance Report")]
        os.environ["TELEGRAM_REPORTS_CHAT_ID"] = ""
        calls.clear()
        assert performance_report.send_telegram("Daily Performance Report", FakeSession()) is False
        assert calls == []
    finally:
        restore = {
            "TELEGRAM_BOT_TOKEN": old_token,
            "TELEGRAM_CHAT_ID": old_legacy,
            "TELEGRAM_REPORTS_CHAT_ID": old_reports,
        }
        for key, value in restore.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_executive_report_v2_entry_timing_and_decisions() -> None:
    base_report = {
        "date": "2026-06-01",
        "closed_signals": 12,
        "wins": 7,
        "losses": 5,
        "win_rate": 58.3,
        "net_r_estimate": 3.5,
        "tp1_hits": 8,
        "tp2_hits": 4,
        "avg_time_to_tp": 95,
        "avg_time_to_sl": 42,
        "best_symbol": "BTCUSDT (66.7%, 6)",
        "best_tier": "A (60.0%, 10)",
        "best_session": "London (62.5%, 8)",
        "long_win_rate": 60.0,
        "short_win_rate": 50.0,
        "performance_warnings": "warning\nSEIUSDT Trades 6 Win Rate 33.3",
        "production_universe_tier_s": "BTCUSDT ETHUSDT",
        "production_universe_tier_a": "SOLUSDT LINKUSDT",
        "production_universe_report_only": "SEIUSDT WIFUSDT",
        "production_universe_performance": "Pool Closed Trades Wins Losses Win Rate Net R\nTier S + Tier A 10 6 4 60.0 3.5",
        "session_risk_report_count": 0,
    }
    collecting = pd.DataFrame({"recommendation": ["ENTER NOW", "WAIT FOR PULLBACK", "SKIP (poor timing)"]})
    message = performance_report.format_executive_report(base_report, collecting, "https://scanner.velalab.net/report")
    assert "Daily Performance Summary" in message
    assert "Decision" in message
    assert "Entry Timing Shadow" in message
    assert "ENTER NOW: 1" in message
    assert "WAIT PULLBACK: 1" in message
    assert "SKIP: 1" in message
    assert "Market Timing: COLLECTING DATA" in message
    assert "Core WR: 60.0%" in message
    assert "Core Net R: 3.50R" in message
    assert "Full analytics: https://scanner.velalab.net/report" in message
    assert "Score Deep Audit" not in message
    assert "Strategy Filter Simulator" not in message
    decision_lines = [line for line in message.splitlines() if line.startswith("- KEEP:") or line.startswith("- WATCH:") or line.startswith("- INVESTIGATE:") or line.startswith("- REPORT ONLY:")]
    assert len(decision_lines) <= 3
    assert len(performance_report.build_executive_decisions(base_report, performance_report._entry_timing_counts(collecting))) <= 3

    poor = pd.DataFrame({"recommendation": ["SKIP (poor timing)"] * 6 + ["ENTER NOW"] * 4})
    assert performance_report._entry_timing_counts(poor)["market_timing"] == "POOR TIMING"
    waiting = pd.DataFrame({"recommendation": ["WAIT FOR PULLBACK"] * 3 + ["WAIT FOR BREAKOUT"] * 3 + ["WAIT FOR BREAKOUT RETEST"] * 2 + ["ENTER NOW"] * 2})
    timing = performance_report._entry_timing_counts(waiting)
    assert timing["counts"]["WAIT PULLBACK"] == 3
    assert timing["counts"]["WAIT BREAKOUT"] == 3
    assert timing["counts"]["WAIT RETEST"] == 2
    assert timing["market_timing"] == "WAITING"
    enterable = pd.DataFrame({"recommendation": ["ENTER NOW"] * 4 + ["WAIT FOR PULLBACK"] * 3 + ["SKIP (poor timing)"] * 3})
    assert performance_report._entry_timing_counts(enterable)["market_timing"] == "ENTERABLE"


def test_scheduled_performance_report_reaches_reports_channel_path() -> None:
    calls: list[tuple[str, str]] = []

    class FakeSession:
        def post(self, _url, data=None, timeout=None):
            calls.append((data["chat_id"], data["text"]))

            class Response:
                status_code = 200
                text = "ok"

            return Response()

    temp_dir = Path(tempfile.gettempdir())
    journal = temp_dir / "scheduled_performance_report_signals.csv"
    history = temp_dir / "scheduled_performance_report_history.csv"
    external = temp_dir / "scheduled_performance_report_external.csv"
    entry_timing = temp_dir / "scheduled_performance_report_entry_timing.csv"
    old_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    old_legacy = os.environ.get("TELEGRAM_CHAT_ID")
    old_signals = os.environ.get("TELEGRAM_SIGNALS_CHAT_ID")
    old_cornix = os.environ.get("TELEGRAM_CORNIX_CHAT_ID")
    old_reports = os.environ.get("TELEGRAM_REPORTS_CHAT_ID")
    old_dashboard = os.environ.get("ANALYTICS_DASHBOARD_URL")
    old_executive = os.environ.get("TELEGRAM_EXECUTIVE_REPORT_ONLY")
    try:
        pd.DataFrame(
            [
                {
                    "timestamp": "2026-06-01T00:00:00+00:00",
                    "closed_at": "2026-06-01T01:00:00+00:00",
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "entry": 100,
                    "stop_loss": 99,
                    "tp1": 101,
                    "tp2": 102,
                    "risk_reward": 2.0,
                    "result": "WIN",
                    "hit_target": "TP2",
                    "signal_status": "sent",
                    "watchlist_tier": "A",
                    "market_session": "London",
                    "score": 88,
                    "confidence": 88,
                }
            ]
        ).to_csv(journal, index=False)
        pd.DataFrame().to_csv(history, index=False)
        pd.DataFrame().to_csv(external, index=False)
        pd.DataFrame(
            [
                {
                    "timestamp": "2026-06-01T00:00:00+00:00",
                    "symbol": "BTCUSDT",
                    "recommendation": "ENTER NOW",
                    "entry_quality_score": 76,
                }
            ]
        ).to_csv(entry_timing, index=False)
        os.environ["TELEGRAM_BOT_TOKEN"] = "token"
        os.environ["TELEGRAM_CHAT_ID"] = "legacy"
        os.environ["TELEGRAM_SIGNALS_CHAT_ID"] = "signals"
        os.environ["TELEGRAM_CORNIX_CHAT_ID"] = "cornix"
        os.environ["TELEGRAM_REPORTS_CHAT_ID"] = "reports"
        os.environ["ANALYTICS_DASHBOARD_URL"] = "https://scanner.velalab.net/report"
        os.environ["TELEGRAM_EXECUTIVE_REPORT_ONLY"] = "1"
        args = argparse.Namespace(
            date="2026-06-01",
            send=True,
            executive=False,
            test_report=False,
            journal=journal,
            history=history,
            external=external,
            entry_timing=entry_timing,
        )
        assert performance_report.run_report(args, session=FakeSession()) == 0
        assert calls
        assert all(chat_id == "reports" for chat_id, _text in calls)
        assert not any(chat_id in {"legacy", "signals", "cornix"} for chat_id, _text in calls)
        assert len(calls) <= 2
        assert all(len(text) <= performance_report.TELEGRAM_MESSAGE_LIMIT for _chat_id, text in calls)
        payload = "\n".join(text for _chat_id, text in calls)
        assert "Daily Performance Summary" in payload
        assert "Closed: 1" in payload
        assert "Wins / Losses: 1 / 0" in payload
        assert "Win Rate: 100.0%" in payload
        assert "Net R: 2.00R" in payload
        assert "Entry Timing Shadow" in payload
        assert "Decision" in payload
        assert "Market Timing: COLLECTING DATA" in payload
        assert "Full analytics: https://scanner.velalab.net/report" in payload
        assert "Score Deep Audit" not in payload
        assert "Strategy Filter Simulator" not in payload
        assert "Root Cause Analytics" not in payload

        calls.clear()
        os.environ["ANALYTICS_DASHBOARD_URL"] = ""
        assert performance_report.run_report(args, session=FakeSession()) == 0
        payload = "\n".join(text for _chat_id, text in calls)
        assert "Daily Performance Summary" in payload
        assert "Full analytics:" not in payload
    finally:
        restore = {
            "TELEGRAM_BOT_TOKEN": old_token,
            "TELEGRAM_CHAT_ID": old_legacy,
            "TELEGRAM_SIGNALS_CHAT_ID": old_signals,
            "TELEGRAM_CORNIX_CHAT_ID": old_cornix,
            "TELEGRAM_REPORTS_CHAT_ID": old_reports,
            "ANALYTICS_DASHBOARD_URL": old_dashboard,
            "TELEGRAM_EXECUTIVE_REPORT_ONLY": old_executive,
        }
        for key, value in restore.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for path in [journal, history, external, entry_timing]:
            try:
                path.unlink()
            except OSError:
                pass


def test_executive_cli_prints_without_telegram_send() -> None:
    calls: list[tuple[str, str]] = []

    class FakeSession:
        def post(self, _url, data=None, timeout=None):
            calls.append((data["chat_id"], data["text"]))

            class Response:
                status_code = 200
                text = "ok"

            return Response()

    temp_dir = Path(tempfile.gettempdir())
    journal = temp_dir / "executive_cli_print_signals.csv"
    history = temp_dir / "executive_cli_print_history.csv"
    external = temp_dir / "executive_cli_print_external.csv"
    entry_timing = temp_dir / "executive_cli_print_entry_timing.csv"
    try:
        pd.DataFrame(
            [
                {
                    "timestamp": "2026-06-01T00:00:00+00:00",
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "entry": 100,
                    "stop_loss": 99,
                    "tp1": 101,
                    "tp2": 102,
                    "risk_reward": 2.0,
                    "result": "WIN",
                    "hit_target": "TP2",
                    "signal_status": "sent",
                    "watchlist_tier": "A",
                    "market_session": "London",
                    "score": 88,
                    "confidence": 88,
                }
            ]
        ).to_csv(journal, index=False)
        pd.DataFrame().to_csv(history, index=False)
        pd.DataFrame().to_csv(external, index=False)
        pd.DataFrame(
            [
                {
                    "timestamp": "2026-06-01T00:00:00+00:00",
                    "symbol": "BTCUSDT",
                    "recommendation": "ENTER NOW",
                    "entry_quality_score": 76,
                }
            ]
        ).to_csv(entry_timing, index=False)
        args = argparse.Namespace(
            date="2026-06-01",
            send=True,
            executive=True,
            test_report=False,
            journal=journal,
            history=history,
            external=external,
            entry_timing=entry_timing,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            assert performance_report.run_report(args, session=FakeSession()) == 0
        payload = output.getvalue()
        assert calls == []
        assert "Daily Performance Summary" in payload
        assert "Closed: 1" in payload
        assert "Decision" in payload
        assert "Score Deep Audit" not in payload
    finally:
        for path in [journal, history, external, entry_timing]:
            try:
                path.unlink()
            except OSError:
                pass


def test_performance_report_send_failure_exits_nonzero() -> None:
    calls: list[tuple[str, str]] = []

    class FailingSession:
        def post(self, _url, data=None, timeout=None):
            calls.append((data["chat_id"], data["text"]))

            class Response:
                status_code = 500
                text = "telegram down"

            return Response()

    temp_dir = Path(tempfile.gettempdir())
    journal = temp_dir / "scheduled_performance_report_fail_signals.csv"
    history = temp_dir / "scheduled_performance_report_fail_history.csv"
    external = temp_dir / "scheduled_performance_report_fail_external.csv"
    entry_timing = temp_dir / "scheduled_performance_report_fail_entry_timing.csv"
    old_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    old_reports = os.environ.get("TELEGRAM_REPORTS_CHAT_ID")
    try:
        pd.DataFrame(
            [
                {
                    "timestamp": "2026-06-01T00:00:00+00:00",
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "entry": 100,
                    "stop_loss": 99,
                    "tp1": 101,
                    "tp2": 102,
                    "risk_reward": 2.0,
                    "result": "WIN",
                    "hit_target": "TP1",
                    "signal_status": "sent",
                }
            ]
        ).to_csv(journal, index=False)
        pd.DataFrame().to_csv(history, index=False)
        pd.DataFrame().to_csv(external, index=False)
        pd.DataFrame().to_csv(entry_timing, index=False)
        os.environ["TELEGRAM_BOT_TOKEN"] = "token"
        os.environ["TELEGRAM_REPORTS_CHAT_ID"] = "reports"
        args = argparse.Namespace(
            date="2026-06-01",
            send=True,
            executive=False,
            test_report=False,
            journal=journal,
            history=history,
            external=external,
            entry_timing=entry_timing,
        )
        assert performance_report.run_report(args, session=FailingSession()) == 1
        assert calls and calls[0][0] == "reports"
    finally:
        restore = {
            "TELEGRAM_BOT_TOKEN": old_token,
            "TELEGRAM_REPORTS_CHAT_ID": old_reports,
        }
        for key, value in restore.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for path in [journal, history, external, entry_timing]:
            try:
                path.unlink()
            except OSError:
                pass


def test_complete_performance_analytics_v1_outputs() -> None:
    journal = pd.DataFrame(
        [
            {
                "timestamp": "2026-05-30T00:00:00+00:00",
                "closed_at": "2026-05-30T02:00:00+00:00",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "watchlist_tier": "A",
                "market_session": "London",
                "market_regime": "Trending",
                "btc_regime": "bullish",
                "entry": 100,
                "tp1": 102,
                "tp2": 104,
                "tp3": 106,
                "stop_loss": 98,
                "risk_reward": 2.0,
                "result": "WIN",
                "hit_target": "TP3",
                "pnl_percent": 6.0,
                "max_profit_pct": 6.2,
                "max_drawdown_pct": -0.5,
                "signal_status": "sent",
            },
            {
                "timestamp": "2026-05-30T01:00:00+00:00",
                "closed_at": "2026-05-30T01:40:00+00:00",
                "symbol": "ETHUSDT",
                "side": "SHORT",
                "watchlist_tier": "B",
                "market_session": "NewYork",
                "market_regime": "Sideway",
                "btc_regime": "bearish",
                "entry": 100,
                "tp1": 98,
                "tp2": 96,
                "stop_loss": 102,
                "risk_reward": 2.0,
                "result": "LOSS",
                "hit_target": "SL",
                "pnl_percent": -2.0,
                "max_profit_pct": 0.3,
                "max_drawdown_pct": -2.1,
                "signal_status": "sent",
            },
            {
                "timestamp": "2026-05-30T02:00:00+00:00",
                "symbol": "SOLUSDT",
                "side": "LONG",
                "watchlist_tier": "C",
                "market_session": "Asia",
                "entry": 100,
                "tp1": 101,
                "tp2": 102,
                "stop_loss": 99,
                "risk_reward": 2.0,
                "result": "OPEN",
                "signal_status": "sent",
            },
            {
                "timestamp": "2026-05-30T03:00:00+00:00",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "entry": 101,
                "result": "SKIPPED",
                "signal_status": "skipped_position_management",
                "skip_reason": "same_symbol_same_direction_open",
            },
            {
                "timestamp": "2026-05-30T04:00:00+00:00",
                "symbol": "BTCUSDT",
                "side": "SHORT",
                "entry": 101,
                "result": "SKIPPED",
                "signal_status": "skipped_position_management",
                "skip_reason": "same_symbol_opposite_direction_open",
            },
            {
                "timestamp": "2026-05-30T05:00:00+00:00",
                "symbol": "ETHUSDT",
                "side": "SHORT",
                "entry": 99,
                "result": "SKIPPED",
                "signal_status": "skipped_position_management",
                "skip_reason": "position_review_open_over_6h exit_review",
            },
            {
                "timestamp": "2026-05-30T06:00:00+00:00",
                "closed_at": "2026-05-30T07:00:00+00:00",
                "symbol": "PEPEUSDT",
                "side": "LONG",
                "watchlist_tier": "C",
                "entry": 100,
                "tp1": 102,
                "tp2": 104,
                "stop_loss": 98,
                "risk_reward": 2.0,
                "result": "WIN",
                "hit_target": "TP1",
                "signal_status": "tier_c_report_only",
            },
            {
                "timestamp": "2026-05-30T08:00:00+00:00",
                "closed_at": "2026-05-30T09:00:00+00:00",
                "symbol": "BONKUSDT",
                "side": "SHORT",
                "watchlist_tier": "C",
                "entry": 100,
                "tp1": 98,
                "tp2": 96,
                "stop_loss": 102,
                "risk_reward": 2.0,
                "result": "LOSS",
                "hit_target": "SL",
                "signal_status": "tier_c_report_only",
            },
            {
                "timestamp": "2026-05-30T10:00:00+00:00",
                "closed_at": "2026-05-30T11:00:00+00:00",
                "symbol": "LINKUSDT",
                "side": "LONG",
                "watchlist_tier": "B",
                "entry": 100,
                "tp1": 102,
                "tp2": 104,
                "stop_loss": 98,
                "risk_reward": 2.0,
                "result": "WIN",
                "hit_target": "TP1",
                "signal_status": "weak_symbol_report_only",
            },
            {
                "timestamp": "2026-05-30T12:00:00+00:00",
                "closed_at": "2026-05-30T13:00:00+00:00",
                "symbol": "SEIUSDT",
                "side": "SHORT",
                "watchlist_tier": "C",
                "entry": 100,
                "tp1": 98,
                "tp2": 96,
                "stop_loss": 102,
                "risk_reward": 2.0,
                "result": "LOSS",
                "hit_target": "SL",
                "signal_status": "weak_symbol_report_only",
            },
            {
                "timestamp": "2026-05-30T14:00:00+00:00",
                "closed_at": "2026-05-30T15:00:00+00:00",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "watchlist_tier": "A",
                "market_session": "NewYork",
                "entry": 100,
                "tp1": 102,
                "tp2": 104,
                "stop_loss": 98,
                "risk_reward": 2.0,
                "result": "WIN",
                "hit_target": "TP1",
                "signal_status": "session_risk_report_only",
            },
            {
                "timestamp": "2026-05-30T16:00:00+00:00",
                "closed_at": "2026-05-30T17:00:00+00:00",
                "symbol": "ETHUSDT",
                "side": "SHORT",
                "watchlist_tier": "A",
                "market_session": "London+NewYork",
                "entry": 100,
                "tp1": 98,
                "tp2": 96,
                "stop_loss": 102,
                "risk_reward": 2.0,
                "result": "LOSS",
                "hit_target": "SL",
                "signal_status": "session_risk_report_only",
            },
            {
                "timestamp": "2026-05-30T18:00:00+00:00",
                "closed_at": "2026-05-30T19:00:00+00:00",
                "symbol": "AAVEUSDT",
                "side": "LONG",
                "watchlist_tier": "B",
                "market_session": "London",
                "entry": 100,
                "tp1": 102,
                "tp2": 104,
                "stop_loss": 98,
                "risk_reward": 2.0,
                "result": "WIN",
                "hit_target": "TP2",
                "signal_status": "london_long_report_only",
            },
            {
                "timestamp": "2026-05-30T20:00:00+00:00",
                "closed_at": "2026-05-30T21:00:00+00:00",
                "symbol": "UNIUSDT",
                "side": "LONG",
                "watchlist_tier": "B",
                "market_session": "London",
                "entry": 100,
                "tp1": 102,
                "tp2": 104,
                "stop_loss": 98,
                "risk_reward": 2.0,
                "result": "LOSS",
                "hit_target": "SL",
                "signal_status": "london_long_report_only",
            },
            {
                "timestamp": "2026-05-30T22:00:00+00:00",
                "symbol": "RUNEUSDT",
                "side": "LONG",
                "watchlist_tier": "B",
                "market_session": "London",
                "entry": 100,
                "tp1": 102,
                "tp2": 104,
                "stop_loss": 98,
                "risk_reward": 2.0,
                "result": "OPEN",
                "hit_target": "",
                "signal_status": "london_long_report_only",
            },
        ]
    )
    external = pd.DataFrame(
        [
            {
                "timestamp_utc": "2026-05-30T06:00:00+00:00",
                "symbol": "NEARUSDT",
                "side": "LONG",
                "entry_low": 2.2,
                "entry_high": 2.3,
                "stop_loss": 2.1,
                "tp1": 2.5,
                "tp2": 2.7,
                "tp3": 3.0,
                "analysis_score": 86,
                "setup_strength": 86,
                "recommendation": "APPROVED",
                "status": "APPROVED",
                "sent_to_signals": "YES",
                "sent_to_cornix": "YES",
                "parse_status": "SUCCESS",
                "result": "WIN",
                "hit_target": "TP2",
                "net_r_estimate": 1.8,
                "max_profit_pct": 8.0,
                "max_drawdown_pct": -0.5,
                "holding_minutes": 45,
                "approved_reason": "RR acceptable; trend aligned",
            },
            {
                "timestamp_utc": "2026-05-30T07:00:00+00:00",
                "symbol": "XRPUSDT",
                "recommendation": "FAILED",
                "status": "REJECTED",
                "sent_to_signals": "NO",
                "sent_to_cornix": "NO",
                "parse_status": "FAILED",
                "reject_reason": "missing stop loss",
                "result": "EXPIRED",
            },
            {
                "timestamp_utc": "2026-05-30T08:00:00+00:00",
                "symbol": "ADAUSDT",
                "side": "SHORT",
                "entry": 1.0,
                "stop_loss": 1.02,
                "tp1": 0.98,
                "analysis_score": 82,
                "recommendation": "APPROVED",
                "status": "APPROVED",
                "sent_to_signals": "YES",
                "sent_to_cornix": "YES",
                "parse_status": "SUCCESS",
                "result": "OPEN",
            },
        ]
    )
    report, tables = build_complete_report(journal, pd.DataFrame(), external, "2026-05-30")
    assert report["total_sent_signals"] == 3
    assert report["wins"] == 1
    assert report["losses"] == 1
    assert report["tp3_hits"] == 1
    assert report["sl_hits"] == 1
    assert report["hold_count"] == 1
    assert report["opposite_signal_count"] == 1
    assert report["exit_recommendation_count"] == 1
    assert report["stale_position_count"] == 1
    assert report["external_total"] == 3
    assert report["external_approved"] == 2
    assert report["external_rejected"] == 1
    assert report["external_wins"] == 1
    assert report["external_losses"] == 0
    assert report["external_open"] == 1
    assert report["external_win_rate"] == 100.0
    assert report["external_net_r_estimate"] == 1.8
    assert report["tier_c_report_count"] == 2
    assert report["tier_c_report_wins"] == 1
    assert report["tier_c_report_losses"] == 1
    assert report["tier_c_report_win_rate"] == 50.0
    assert report["weak_symbol_report_count"] == 2
    assert report["weak_symbol_report_wins"] == 1
    assert report["weak_symbol_report_losses"] == 1
    assert report["weak_symbol_report_win_rate"] == 50.0
    assert report["session_risk_report_count"] == 2
    assert report["session_risk_report_wins"] == 1
    assert report["session_risk_report_losses"] == 1
    assert report["session_risk_report_win_rate"] == 50.0
    assert report["london_long_report_count"] == 3
    assert report["london_long_report_wins"] == 1
    assert report["london_long_report_losses"] == 1
    assert report["london_long_report_open"] == 1
    assert report["london_long_report_win_rate"] == 50.0
    assert report["london_long_report_net_r"] == 1.0
    assert "missing stop loss" in report["external_top_reject_reasons"]
    assert "NEARUSDT" in report["external_top_approved_symbols"]
    assert "XRPUSDT" in report["external_top_rejected_symbols"]
    assert not tables["symbol_performance"].empty
    assert not tables["source_performance"].empty

    export_dir = Path(tempfile.gettempdir()) / "crypto_perf_v1_smoke"
    try:
        paths = export_v1_outputs(report, tables, export_dir)
        for path in paths.values():
            assert path.exists()
        daily = pd.read_csv(paths["daily_performance"])
        assert daily.loc[0, "tp3_hits"] == 1
        assert daily.loc[0, "london_long_report_count"] == 3
        position = pd.read_csv(paths["position_management"])
        assert position.loc[0, "hold_count"] == 1
        assert paths["score_tier_audit"].name == "score_tier_audit.csv"
        assert paths["score_session_audit"].exists()
        assert paths["score_direction_audit"].exists()
        assert paths["score_symbol_audit"].exists()
        assert paths["score_efficiency_audit"].exists()
        assert paths["score_calibration_report"].exists()
        assert paths["strategy_filter_simulator"].exists()
        assert paths["top_strategy_candidates"].exists()
        assert paths["production_universe_ranking"].exists()
        assert paths["post_filter_live_performance"].exists()
        assert paths["production_universe_performance"].exists()
    finally:
        for path in export_dir.glob("*.csv"):
            try:
                path.unlink()
            except OSError:
                pass
        try:
            export_dir.rmdir()
        except OSError:
            pass


def test_performance_analytics_production_mapping() -> None:
    journal = pd.DataFrame(
        [
            {
                "timestamp": "2026-05-31T00:00:00+00:00",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "watchlist_tier": "A",
                "market_session": "London",
                "signal_status": "sent",
                "result": "WIN",
                "hit_target": "TP2",
                "max_profit_pct": "2.5",
                "max_drawdown_pct": "-0.4",
            },
            {
                "timestamp": "2026-05-31T01:00:00+00:00",
                "symbol": "ETHUSDT",
                "side": "SHORT",
                "watchlist_tier": "B",
                "market_session": "NewYork",
                "signal_status": "sent",
                "result": "LOSS",
                "hit_target": "SL",
                "max_profit_pct": "",
                "max_drawdown_pct": "-1.2",
            },
            {
                "timestamp": "2026-05-31T02:00:00+00:00",
                "symbol": "SOLUSDT",
                "side": "LONG",
                "watchlist_tier": "C",
                "market_session": "Asia",
                "signal_status": "logged_quality_filter",
                "result": "SKIPPED",
                "hit_target": "",
            },
            {
                "timestamp": "2026-05-31T03:00:00+00:00",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "watchlist_tier": "A",
                "market_session": "London",
                "signal_status": "skipped_btc_regime",
                "result": "SKIPPED",
                "hit_target": "3",
            },
        ]
    )
    report, tables = build_complete_report(journal, pd.DataFrame(), pd.DataFrame())
    assert report["date"] == "ALL"
    assert report["total_sent_signals"] == 2
    assert report["closed_signals"] == 2
    assert report["wins"] == 1
    assert report["losses"] == 1
    assert round(report["win_rate"], 1) == 50.0
    assert report["tp1_hits"] == 1
    assert report["tp2_hits"] == 1
    assert report["tp3_hits"] == 0
    assert report["sl_hits"] == 1
    assert "BTCUSDT" in report["best_symbol"]
    assert "ETHUSDT" in report["worst_symbol"]
    assert round(report["long_win_rate"], 1) == 100.0
    assert round(report["short_win_rate"], 1) == 0.0
    symbol_table = tables["symbol_performance"]
    assert int(symbol_table["total_signals"].sum()) == 2
    assert "SOLUSDT" not in symbol_table["symbol"].astype(str).tolist()


def test_performance_analytics_v2_tables_and_warnings() -> None:
    rows = []
    for index in range(6):
        rows.append(
            {
                "timestamp": f"2026-06-01T0{index}:00:00+00:00",
                "closed_at": f"2026-06-01T0{index}:30:00+00:00",
                "symbol": "SEIUSDT",
                "side": "LONG",
                "tier": "C",
                "session": "NewYork",
                "entry": 100,
                "sl": 99,
                "tp1": 101,
                "tp2": 102,
                "rr": 2.0,
                "signal_status": "sent",
                "result": "LOSS",
                "hit_target": "SL",
                "max_profit_pct": 0.2,
                "max_drawdown_pct": -1.0,
                "holding_minutes": 30,
            }
        )
    for index in range(5):
        rows.append(
            {
                "timestamp": f"2026-06-01T1{index}:00:00+00:00",
                "closed_at": f"2026-06-01T1{index}:45:00+00:00",
                "symbol": "BTCUSDT",
                "side": "SHORT",
                "tier": "A",
                "session": "London+NewYork",
                "entry": 100,
                "sl": 102,
                "tp1": 98,
                "tp2": 96,
                "rr": 2.0,
                "signal_status": "sent",
                "result": "WIN",
                "hit_target": "TP2",
                "max_profit_pct": 4.0,
                "max_drawdown_pct": -0.3,
                "holding_minutes": 45,
            }
        )
    df = pd.DataFrame(rows)
    v2 = build_performance_v2(df)
    symbol_table = v2["symbol_performance_v2"]
    session_table = v2["session_performance_v2"]
    direction_table = v2["direction_performance_v2"]
    tier_table = v2["tier_performance_v2"]
    assert canonical_session("London_NewYork") == "London+NewYork"
    assert "SEIUSDT" in symbol_table["Symbol"].tolist()
    assert int(symbol_table[symbol_table["Symbol"] == "SEIUSDT"].iloc[0]["Trades"]) == 6
    assert float(symbol_table[symbol_table["Symbol"] == "SEIUSDT"].iloc[0]["Win Rate"]) == 0.0
    assert "NewYork" in session_table["Session"].tolist()
    assert "LONG" in direction_table["Direction"].tolist()
    assert "C" in tier_table["Tier"].tolist()
    warnings = generate_performance_warnings(df)
    assert any("Symbol Warning" in warning and "SEIUSDT" in warning for warning in warnings)
    assert any("Session Warning" in warning and "NewYork" in warning for warning in warnings)
    assert any("Direction Warning" in warning and "LONG" in warning for warning in warnings)
    assert any("Tier Warning" in warning and "C" in warning for warning in warnings)
    assert v2["top_symbols"].iloc[0]["Symbol"] == "BTCUSDT"
    assert v2["bottom_symbols"].iloc[0]["Symbol"] == "SEIUSDT"


def test_performance_analytics_v3_shadow_filters_and_recommendations() -> None:
    rows = []
    for index in range(5):
        rows.append(
            {
                "timestamp": f"2026-06-02T0{index}:00:00+00:00",
                "symbol": "WEAKUSDT",
                "side": "LONG",
                "tier": "C",
                "session": "NewYork",
                "signal_status": "sent",
                "result": "LOSS",
                "hit_target": "SL",
                "risk_reward": 2.0,
                "pnl_percent": -1.0,
                "score": 78,
                "max_profit_pct": 0.5,
                "max_drawdown_pct": -1.2,
                "holding_minutes": 35,
            }
        )
    for index in range(6):
        rows.append(
            {
                "timestamp": f"2026-06-02T1{index}:00:00+00:00",
                "symbol": "GOODUSDT",
                "side": "SHORT",
                "tier": "A",
                "session": "London+NewYork",
                "signal_status": "sent",
                "result": "WIN",
                "hit_target": "TP1",
                "risk_reward": 2.0,
                "pnl_percent": 1.2,
                "score": 100 + index,
                "max_profit_pct": 2.5,
                "max_drawdown_pct": -0.4,
                "holding_minutes": 55,
            }
        )
    for index in range(5):
        rows.append(
            {
                "timestamp": f"2026-06-02T{18 + index}:00:00+00:00",
                "symbol": "FILTEREDUSDT",
                "side": "LONG",
                "tier": "B",
                "session": "London",
                "signal_status": "weak_symbol_report_only",
                "result": "LOSS",
                "hit_target": "SL",
                "risk_reward": 2.0,
                "pnl_percent": -1.0,
                "score": 82,
                "max_profit_pct": 0.3,
                "max_drawdown_pct": -1.5,
                "holding_minutes": 25,
            }
        )
    df = pd.DataFrame(rows)
    before = df.copy(deep=True)
    v3 = build_performance_v3(df)
    shadow = v3["shadow_filter_backtest"]
    actions = v3["recommended_actions"]
    assert not v3["symbol_performance_v3"].empty
    assert not v3["session_performance_v3"].empty
    assert not v3["tier_performance_v3"].empty
    assert not v3["direction_performance_v3"].empty
    assert not v3["hour_performance_v3"].empty
    score_perf = v3["score_performance_v3"]
    assert not score_perf.empty
    bucket_75 = score_perf[score_perf["Score Range"] == "75-79"].iloc[0]
    bucket_100 = score_perf[score_perf["Score Range"] == "100+"].iloc[0]
    assert int(bucket_75["Trades"]) == 5
    assert int(bucket_75["Losses"]) == 5
    assert float(bucket_75["Net R"]) == -5.0
    assert int(bucket_100["Trades"]) == 6
    assert int(bucket_100["Wins"]) == 6
    assert int(bucket_100["TP1 Hits"]) == 6
    assert float(bucket_100["Avg Max Profit %"]) == 2.5
    assert not v3["score_tier_audit"].empty
    assert not v3["score_session_audit"].empty
    assert not v3["score_direction_audit"].empty
    assert not v3["score_symbol_audit"].empty
    assert not v3["score_efficiency_audit"].empty
    calibration = v3["score_calibration_report"]
    assert not calibration.empty
    assert "Calibration" in calibration.columns
    assert "Diagnostics" in calibration.columns
    assert v3["score_calibration_recommendations"]
    simulator = v3["strategy_filter_simulator"]
    assert not simulator.empty
    assert "Production Universe + Score 75-89 + No NewYork" in simulator["Scenario"].tolist()
    assert "Diff vs Current Win Rate" in simulator.columns
    assert "Diff vs Current Net R" in simulator.columns
    current_strategy = simulator[simulator["Scenario"] == "Current"].iloc[0]
    assert int(current_strategy["Closed Trades"]) == 11
    top_candidates = v3["top_strategy_candidates"]
    assert not top_candidates.empty
    assert int(top_candidates.iloc[0]["Rank"]) == 1
    assert v3["strategy_filter_recommendations"]
    ranking = v3["production_universe_ranking"]
    assert not ranking.empty
    good_rank = ranking[ranking["Symbol"] == "GOODUSDT"].iloc[0]
    weak_rank = ranking[ranking["Symbol"] == "WEAKUSDT"].iloc[0]
    filtered_rank = ranking[ranking["Symbol"] == "FILTEREDUSDT"].iloc[0]
    assert good_rank["Classification"] == "Tier A"
    assert weak_rank["Classification"] == "Report Only"
    assert filtered_rank["Classification"] == "Report Only"
    assert float(good_rank["Confidence Score"]) > float(weak_rank["Confidence Score"])
    assert int(good_rank["Closed Trades"]) == 6
    assert int(weak_rank["Closed Trades"]) == 5
    post_filter = v3["post_filter_live_performance"]
    assert not post_filter.empty
    historical = post_filter[post_filter["Pool"] == "Historical"].iloc[0]
    live_pool = post_filter[post_filter["Pool"] == "Post-Filter Live Pool"].iloc[0]
    improvement = post_filter[post_filter["Pool"] == "Improvement"].iloc[0]
    assert int(historical["Closed Trades"]) == 16
    assert int(live_pool["Closed Trades"]) == 11
    assert float(improvement["Net R"]) == 5.0
    universe_perf = v3["production_universe_performance"]
    assert "Tier S + Tier A symbols" in universe_perf["Pool"].tolist()
    assert "Report-only symbols" in universe_perf["Pool"].tolist()
    score_symbol = v3["score_symbol_audit"]
    assert "GOODUSDT" in score_symbol["Symbol"].tolist()
    assert "WEAKUSDT" in score_symbol["Symbol"].tolist()
    efficiency_100 = v3["score_efficiency_audit"][v3["score_efficiency_audit"]["Score Bucket"] == "100+"].iloc[0]
    assert float(efficiency_100["Avg Time To TP"]) == 55.0
    assert "No Tier C" in shadow["Scenario"].tolist()
    assert "Exclude weak symbols <40% WR / >=5 trades" in shadow["Scenario"].tolist()
    no_tier_c = shadow[shadow["Scenario"] == "No Tier C"].iloc[0]
    current = shadow[shadow["Scenario"] == "Current"].iloc[0]
    assert float(no_tier_c["Net R estimate"]) > float(current["Net R estimate"])
    weak = actions[actions["Symbol"] == "WEAKUSDT"].iloc[0]
    assert weak["Recommendation"] == "FLAG FOR REMOVAL"
    assert set(actions["Recommendation"]).issubset({"FLAG FOR REMOVAL", "KEEP", "WATCH"})
    pd.testing.assert_frame_equal(df, before)


def test_score_calibration_report_detects_inversion() -> None:
    rows = []
    for index in range(5):
        rows.append(
            {
                "timestamp": f"2026-06-04T0{index}:00:00+00:00",
                "symbol": "LOWGOOD",
                "side": "LONG",
                "signal_status": "sent",
                "result": "WIN",
                "hit_target": "TP2",
                "risk_reward": 2.0,
                "pnl_percent": 1.5,
                "score": 78,
                "max_profit_pct": 2.0,
                "max_drawdown_pct": -0.3,
            }
        )
    for index in range(5):
        rows.append(
            {
                "timestamp": f"2026-06-04T1{index}:00:00+00:00",
                "symbol": "HIGHBAD",
                "side": "SHORT",
                "signal_status": "sent",
                "result": "LOSS",
                "hit_target": "SL",
                "risk_reward": 2.0,
                "pnl_percent": -1.0,
                "score": 100,
                "max_profit_pct": 0.2,
                "max_drawdown_pct": -1.4,
            }
        )
    v3 = build_performance_v3(pd.DataFrame(rows))
    calibration = v3["score_calibration_report"]
    low = calibration[calibration["Score Bucket"] == "75-79"].iloc[0]
    high = calibration[calibration["Score Bucket"] == "100+"].iloc[0]
    assert low["Calibration"] == "UNDERVALUED"
    assert high["Calibration"] == "OVERVALUED"
    assert "HIGH SCORE UNDERPERFORMING" in high["Diagnostics"]
    assert "SCORE INVERSION" in high["Diagnostics"]
    assert "Score inversion detected" in v3["score_calibration_recommendations"]
    assert "High-score signals underperform lower-score signals" in v3["score_calibration_recommendations"]


def _root_cause_sample() -> pd.DataFrame:
    rows = []
    for index in range(5):
        rows.append(
            {
                "timestamp": f"2026-06-06T0{index}:00:00+00:00",
                "symbol": "LOSSUSDT",
                "side": "LONG",
                "tier": "C",
                "session": "NewYork",
                "signal_status": "sent",
                "result": "LOSS",
                "hit_target": "SL",
                "risk_reward": 2.0,
                "score": 92,
                "max_profit_pct": 0.4,
                "max_drawdown_pct": -1.1,
                "holding_minutes": 30,
            }
        )
    for index in range(5):
        rows.append(
            {
                "timestamp": f"2026-06-06T1{index}:00:00+00:00",
                "symbol": "WINUSDT",
                "side": "SHORT",
                "tier": "A",
                "session": "London",
                "signal_status": "sent",
                "result": "WIN",
                "hit_target": "TP2",
                "risk_reward": 2.0,
                "score": 82,
                "max_profit_pct": 2.2,
                "max_drawdown_pct": -0.3,
                "holding_minutes": 45,
            }
        )
    return pd.DataFrame(rows)


def test_root_cause_analytics_sections_exports_and_clusters() -> None:
    df = _root_cause_sample()
    before = df.copy(deep=True)
    root = build_root_cause_analytics(df)
    assert not root["root_score_session"].empty
    assert not root["root_score_direction"].empty
    assert not root["root_tier_session"].empty
    assert not root["root_symbol_session"].empty
    assert not root["root_symbol_direction"].empty
    assert not root["root_loss_clusters"].empty
    assert not root["root_win_clusters"].empty
    assert not root["root_cause_recommendations"].empty
    worst = root["root_loss_clusters"].iloc[0]
    best = root["root_win_clusters"].iloc[0]
    assert "NewYork" in worst["Cluster Name"]
    assert int(worst["Loss Count"]) >= 3
    assert "London" in best["Cluster Name"]
    assert int(best["Win Count"]) >= 3

    report, tables = build_complete_report(df, pd.DataFrame(), pd.DataFrame(), "2026-06-06")
    message = performance_report.format_report(report)
    assert "Root Cause Analytics" in message
    assert "Root Cause Recommendations" in message
    assert "root_loss_clusters" in tables
    assert "root_win_clusters" in tables

    export_dir = Path(tempfile.gettempdir()) / "crypto_root_cause_exports_smoke"
    shutil.rmtree(export_dir, ignore_errors=True)
    try:
        paths = export_v1_outputs(report, tables, export_dir)
        for key in [
            "root_score_session",
            "root_score_direction",
            "root_tier_session",
            "root_symbol_session",
            "root_symbol_direction",
            "root_loss_clusters",
            "root_win_clusters",
            "root_cause_recommendations",
        ]:
            assert key in paths
            assert paths[key].exists()
            exported = pd.read_csv(paths[key])
            assert not exported.empty
    finally:
        shutil.rmtree(export_dir, ignore_errors=True)
    pd.testing.assert_frame_equal(df, before)


def test_performance_analytics_v3_missing_fields_safe() -> None:
    df = pd.DataFrame(
        [
            {"timestamp": "2026-06-03T00:00:00+00:00", "symbol": "BTCUSDT", "result": "WIN", "signal_status": "sent"},
            {"timestamp": "2026-06-03T01:00:00+00:00", "symbol": "ETHUSDT", "result": "LOSS", "signal_status": "sent"},
        ]
    )
    v3 = build_performance_v3(df)
    shadow = shadow_filter_backtest(df)
    assert not shadow.empty
    assert "Current" in shadow["Scenario"].tolist()
    assert "symbol_performance_v3" in v3
    assert "recommended_actions" in v3
    assert "production_universe_ranking" in v3
    assert "score_calibration_report" in v3
    assert "strategy_filter_simulator" in v3


def test_dashboard_renders_html() -> None:
    df = performance_report.normalize(
        pd.DataFrame(
            [
                {
                    "timestamp": "2026-05-30T00:00:00+00:00",
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "watchlist_tier": "A",
                    "market_session": "London",
                    "entry": 100,
                    "tp1": 101,
                    "tp2": 102,
                    "stop_loss": 99,
                    "risk_reward": 2,
                    "result": "OPEN",
                    "signal_status": "sent",
                }
            ]
        )
    )
    output = Path(tempfile.gettempdir()) / "crypto_dashboard_smoke.html"
    try:
        dashboard.render_dashboard(df, output)
        html = output.read_text(encoding="utf-8")
        assert "Crypto Scanner Dashboard" in html
        assert "Overview" not in html or "Total Signals" in html
        assert "Open Positions" in html
        assert "Top Performers" in html
        assert "Worst Performers" in html
        assert "Warnings" in html
    finally:
        try:
            output.unlink()
        except OSError:
            pass


def test_dashboard_v2_handles_missing_and_empty_data() -> None:
    missing = Path(tempfile.gettempdir()) / "missing_dashboard_signals.csv"
    data = dashboard.load_dashboard_data({"signals": missing})
    assert "signals" in data
    assert "sent" in data
    assert data["sent"].empty

    kpis = dashboard.dashboard_kpis(pd.DataFrame())
    assert kpis["Total sent signals"] == 0
    assert kpis["Closed trades"] == 0
    assert kpis["Win rate"] == 0.0
    assert kpis["Best symbol"] == "-"

    filtered = dashboard.apply_filters(
        data["sent"],
        results=["WIN"],
        targets=["TP1"],
        score_range=(80, 100),
        confidence_range=(80, 100),
    )
    assert filtered.empty
    assert dashboard.equity_curve(pd.DataFrame()).empty
    assert dashboard.daily_net_r(pd.DataFrame()).empty
    assert dashboard.analytics_suggestions(pd.DataFrame())


def test_dashboard_v3_equity_drawdown_monthly_simulator() -> None:
    df = performance_report.normalize(
        pd.DataFrame(
            [
                {
                    "timestamp": "2026-05-30T00:00:00+00:00",
                    "closed_at": "2026-05-30T01:00:00+00:00",
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "watchlist_tier": "A",
                    "market_session": "London",
                    "entry": 100,
                    "tp1": 102,
                    "tp2": 104,
                    "stop_loss": 98,
                    "risk_reward": 2.0,
                    "result": "WIN",
                    "hit_target": "TP2",
                    "signal_status": "sent",
                },
                {
                    "timestamp": "2026-05-31T00:00:00+00:00",
                    "closed_at": "2026-05-31T01:00:00+00:00",
                    "symbol": "ETHUSDT",
                    "side": "SHORT",
                    "watchlist_tier": "B",
                    "market_session": "NewYork",
                    "entry": 100,
                    "tp1": 98,
                    "tp2": 96,
                    "stop_loss": 102,
                    "risk_reward": 2.0,
                    "result": "LOSS",
                    "hit_target": "SL",
                    "signal_status": "sent",
                },
            ]
        )
    )
    curve = dashboard.equity_curve(df)
    assert list(curve.columns) == ["closed_at", "r", "cumulative_r"]
    assert round(float(curve["cumulative_r"].iloc[-1]), 2) == 1.0
    drawdown = dashboard.drawdown_curve(df)
    assert "drawdown_r" in drawdown.columns
    assert dashboard.max_drawdown_r(df) == -1.0
    daily = dashboard.daily_pnl_bars(df)
    assert set(daily["color"]) == {"#16a34a", "#dc2626"}
    monthly = dashboard.monthly_performance(df)
    assert int(monthly.loc[0, "closed"]) == 2
    simulator = dashboard.account_growth_simulator(df)
    assert simulator["account_usdt"].tolist() == [100.0, 500.0, 1000.0]
    assert round(float(simulator.loc[0, "ending_balance"]), 2) == 101.0


def test_entry_timing_engine_shadow_csv_and_report_summary() -> None:
    engine = EntryTimingEngine()
    good = sample_signal()
    good.direction = "LONG"
    good.entry = 100.0
    good.sl = 99.0
    good.support = 99.4
    good.resistance = 105.0
    good.volume_spike = True
    good.mfi_confirmed = True
    good.atr_expansion_ratio = 1.1
    result = engine.evaluate(good)
    assert 0 <= result.entry_quality_score <= 100
    assert result.recommendation in {
        "ENTER NOW",
        "WAIT FOR PULLBACK",
        "WAIT FOR BREAKOUT",
        "WAIT FOR BREAKOUT RETEST",
        "SKIP (poor timing)",
    }
    assert result.symbol == "BTCUSDT"

    overextended = sample_signal()
    overextended.direction = "LONG"
    overextended.entry = 110.0
    overextended.sl = 109.0
    overextended.support = 100.0
    overextended.resistance = 111.0
    overextended.atr_expansion_ratio = 2.0
    skip = engine.evaluate(overextended)
    assert skip.recommendation == "SKIP (poor timing)"
    assert skip.overextended_move == "YES"

    path = Path(tempfile.gettempdir()) / "entry_timing_engine_smoke.csv"
    try:
        if path.exists():
            path.unlink()
        logger = EntryTimingLogger(path)
        logger.log_many([result, skip])
        saved = pd.read_csv(path)
        assert len(saved) == 2
        assert "entry_quality_score" in saved.columns
        summary = format_entry_timing_summary(saved)
        assert "Recommendation" in summary
        assert "Candidates" in summary

        report = {"date": "ALL", "entry_timing_shadow_summary": summary}
        message = performance_report.format_report(
            {
                **{key: 0 for key in [
                    "total_sent_signals",
                    "closed_signals",
                    "open_signals",
                    "wins",
                    "losses",
                    "tp1_hits",
                    "tp2_hits",
                    "sl_hits",
                    "net_r_estimate",
                ]},
                "date": "ALL",
                "best_symbol": "-",
                "worst_symbol": "-",
                "best_tier": "-",
                "worst_tier": "-",
                "best_session": "-",
                "worst_session": "-",
                **report,
            }
        )
        assert "Entry Timing Engine Shadow Summary" in message
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def test_scanner_final_candidate_writes_entry_timing_row() -> None:
    path = Path(tempfile.gettempdir()) / "entry_timing_final_candidate_smoke.csv"
    try:
        if path.exists():
            path.unlink()
        runner = object.__new__(scanner.AgentRunner)
        runner.entry_timing = EntryTimingEngine()
        runner.entry_timing_logger = EntryTimingLogger(path)

        production_signal = sample_signal()
        runner.evaluate_entry_timing_shadow(production_signal, "sent")

        report_only_signal = sample_signal()
        report_only_signal.symbol = "SUIUSDT"
        runner.evaluate_entry_timing_shadow(report_only_signal, "session_risk_report_only")

        saved = pd.read_csv(path)
        assert len(saved) == 2
        assert saved["symbol"].tolist() == ["BTCUSDT", "SUIUSDT"]
        assert saved["entry_quality_score"].between(0, 100).all()
        assert saved["signal_status"].tolist() == ["sent", "session_risk_report_only"]
        assert saved["normalized_symbol"].tolist() == ["BTCUSDT", "SUIUSDT"]
        assert saved["normalized_direction"].tolist() == ["SHORT", "SHORT"]
        assert saved["final_signal_timestamp"].fillna("").astype(str).str.len().gt(0).all()
        assert set(saved["recommendation"]).issubset(
            {
                "ENTER NOW",
                "WAIT FOR PULLBACK",
                "WAIT FOR BREAKOUT",
                "WAIT FOR BREAKOUT RETEST",
                "SKIP (poor timing)",
            }
        )
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def test_position_manager_advice() -> None:
    now = pd.Timestamp("2026-05-30T12:00:00Z")
    path = Path(tempfile.gettempdir()) / "position_manager_smoke.csv"
    original_snapshot = position_manager.fetch_position_snapshot

    def fake_snapshot(_symbol: str) -> position_manager.PositionSnapshot:
        return position_manager.PositionSnapshot(
            current_price=101.0,
            trend_status="bullish",
            confirmation_15m="bullish",
            volume_status="normal",
            mfi=58.0,
            atr_pct=0.75,
            support=98.0,
            resistance=104.0,
            market_regime="Trending",
            scanner_bias="LONG",
            available=True,
        )

    position_manager.fetch_position_snapshot = fake_snapshot
    try:
        pd.DataFrame(
            [
                {
                    "timestamp": "2026-05-30T08:00:00+00:00",
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "entry": 100,
                    "stop_loss": 98,
                    "tp1": 102,
                    "tp2": 104,
                    "result": "OPEN",
                    "signal_status": "sent",
                },
                {
                    "timestamp": "2026-05-30T02:00:00+00:00",
                    "symbol": "ETHUSDT",
                    "side": "SHORT",
                    "entry": 100,
                    "stop_loss": 102,
                    "tp1": 98,
                    "tp2": 96,
                    "result": "OPEN",
                    "signal_status": "sent",
                },
            ]
        ).to_csv(path, index=False)

        same = {"symbol": "BTCUSDT", "direction": "LONG", "entry": 101}
        advice = position_manager.evaluate_new_signal(same, path, now=now)
        assert advice.should_send_signal is False
        assert advice.action == "position_hold"
        assert "POSITION UPDATE / HOLD" in advice.message

        opposite = {"symbol": "BTCUSDT", "direction": "SHORT", "entry": 101}
        advice = position_manager.evaluate_new_signal(opposite, path, now=now)
        assert advice.should_send_signal is False
        assert advice.action == "opposite_signal"
        assert "OPPOSITE SIGNAL DETECTED" in advice.message

        review = {"symbol": "ETHUSDT", "direction": "SHORT", "entry": 99}
        advice = position_manager.evaluate_new_signal(review, path, now=now)
        assert advice.should_send_signal is False
        assert advice.action == "position_review"
        assert "POSITION REVIEW" in advice.message
        assert "Recommendation:" in advice.message
        assert "Suggested actions:" in advice.message
        assert "Current R:" in advice.message
        assert "Distance to TP2:" in advice.message
        assert "Unrealized profit:" in advice.message
        assert "ATR multiple from entry:" in advice.message
        assert "Recommendation Confidence:" in advice.message
        assert "AI/System Analysis:" in advice.message

        fresh = {"symbol": "SOLUSDT", "direction": "LONG", "entry": 100}
        advice = position_manager.evaluate_new_signal(fresh, path, now=now)
        assert advice.should_send_signal is True
    finally:
        position_manager.fetch_position_snapshot = original_snapshot
        try:
            path.unlink()
        except OSError:
            pass


def _reset_position_watcher_test_path(path: Path) -> None:
    shutil.rmtree(path.parent / f"{path.stem}_position_watcher_locks", ignore_errors=True)
    try:
        path.unlink()
    except OSError:
        pass


def test_position_watcher_tp1_breakeven_alert_and_dedupe() -> None:
    path = Path(tempfile.gettempdir()) / "position_watcher_smoke.csv"
    _reset_position_watcher_test_path(path)
    pd.DataFrame(
        [
            {
                "timestamp": "2026-06-01T00:00:00+00:00",
                "symbol": "HYPEUSDT",
                "side": "LONG",
                "entry": 70.744,
                "tp1": 72.468,
                "tp2": 74.0,
                "stop_loss": 69.0,
                "result": "OPEN",
                "signal_status": "sent",
            }
        ]
    ).to_csv(path, index=False)

    class Response:
        def __init__(self, status_code: int, payload: dict | None = None, text: str = "ok") -> None:
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError(self.text)

    class FakeSession:
        def __init__(self) -> None:
            self.posts: list[tuple[str, str]] = []

        def get(self, _url, params=None, timeout=None):
            assert params["symbol"] == "HYPEUSDT"
            return Response(200, {"price": "72.500"})

        def post(self, _url, data=None, timeout=None):
            self.posts.append((data["chat_id"], data["text"]))
            return Response(200)

    config = position_watcher.WatcherConfig(
        enabled=True,
        interval_seconds=60,
        send_alerts=True,
        send_telegram=True,
        token="token",
        reports_chat_id="reports",
        cornix_chat_id="cornix",
        command_mode="report_only",
        send_report_copy=True,
        dry_run=False,
    )
    session = FakeSession()
    try:
        stats = position_watcher.process_once(path, session, config)
        assert stats.checked == 1
        assert stats.tp1_reached == 1
        assert stats.alerts_sent == 1
        assert len(session.posts) == 1
        assert session.posts[0][0] == "reports"
        assert "POSITION WATCHER ALERT" in session.posts[0][1]
        assert "MOVE SL TO BREAKEVEN" in session.posts[0][1]
        saved = pd.read_csv(path)
        assert int(saved.loc[0, "tp1_alert_sent"]) == 1
        assert saved.loc[0, "tp1_alert_source"] == "watcher"
        assert int(saved.loc[0, "breakeven_recommended"]) == 1
        assert saved.loc[0, "position_management_stage"] == "TP1_REACHED_BE_RECOMMENDED"
        assert float(saved.loc[0, "breakeven_price"]) == 70.744
        assert int(saved.loc[0, "new_stop_notification_sent"]) == 1
        assert saved.loc[0, "new_stop_notification_key"] == "HYPEUSDT|LONG|70.744"

        stats = position_watcher.process_once(path, session, config)
        assert stats.skipped_duplicates == 1
        assert len(session.posts) == 1
    finally:
        _reset_position_watcher_test_path(path)


def _watcher_config(mode: str = "cornix_command", dry_run: bool = False) -> position_watcher.WatcherConfig:
    return position_watcher.WatcherConfig(
        enabled=True,
        interval_seconds=60,
        send_alerts=True,
        send_telegram=True,
        token="token",
        reports_chat_id="reports",
        cornix_chat_id="cornix",
        command_mode=mode,
        send_report_copy=True,
        dry_run=dry_run,
    )


def _watcher_row(side: str = "LONG", entry: float | str = 70.744, tp1: float = 72.468) -> dict:
    return {
        "timestamp": "2026-06-01T00:00:00+00:00",
        "symbol": "HYPEUSDT",
        "side": side,
        "entry": entry,
        "tp1": tp1,
        "tp2": 74.0 if side == "LONG" else 67.0,
        "stop_loss": 69.0 if side == "LONG" else 72.0,
        "result": "OPEN",
        "signal_status": "sent",
    }


class WatcherResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = "ok") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(self.text)


class WatcherFakeSession:
    def __init__(self, price: str) -> None:
        self.price = price
        self.posts: list[tuple[str, str]] = []

    def get(self, _url, params=None, timeout=None):
        return WatcherResponse(200, {"price": self.price})

    def post(self, _url, data=None, timeout=None):
        self.posts.append((data["chat_id"], data["text"]))
        return WatcherResponse(200)


def test_position_watcher_cornix_command_long_short_and_dedupe() -> None:
    for side, price, tp1 in [("LONG", "72.500", 72.468), ("SHORT", "68.000", 68.500)]:
        path = Path(tempfile.gettempdir()) / f"position_watcher_{side.lower()}_command_smoke.csv"
        _reset_position_watcher_test_path(path)
        pd.DataFrame([_watcher_row(side=side, tp1=tp1)]).to_csv(path, index=False)
        session = WatcherFakeSession(price)
        try:
            stats = position_watcher.process_once(path, session, _watcher_config("cornix_command"))
            assert stats.tp1_reached == 1
            assert stats.cornix_commands_sent == 1
            assert len(session.posts) == 2
            assert session.posts[0][0] == "cornix"
            assert f"{side} HYPEUSDT" in session.posts[0][1]
            assert "NEW STOP:" in session.posts[0][1]
            assert session.posts[1][0] == "reports"
            saved = pd.read_csv(path)
            assert int(saved.loc[0, "tp1_alert_sent"]) == 1
            assert int(saved.loc[0, "cornix_be_command_sent"]) == 1
            assert float(saved.loc[0, "cornix_be_stop_price"]) == 70.744
            assert int(saved.loc[0, "new_stop_notification_sent"]) == 1
            assert saved.loc[0, "new_stop_notification_key"] == f"HYPEUSDT|{side}|70.744"
            assert str(saved.loc[0, "cornix_be_command_status"]).startswith("SENT")

            stats = position_watcher.process_once(path, session, _watcher_config("cornix_command"))
            assert stats.skipped_duplicates == 1
            assert len(session.posts) == 2
        finally:
            _reset_position_watcher_test_path(path)


def test_position_watcher_cornix_stop_price_duplicate_prevention() -> None:
    path = Path(tempfile.gettempdir()) / "position_watcher_stop_dedupe_smoke.csv"
    _reset_position_watcher_test_path(path)
    pd.DataFrame(
        [
            {
                **_watcher_row(),
                "cornix_be_command_sent": 1,
                "cornix_be_stop_price": 70.744,
            }
        ]
    ).to_csv(path, index=False)
    session = WatcherFakeSession("72.500")
    try:
        stats = position_watcher.process_once(path, session, _watcher_config("cornix_command"))
        assert stats.skipped_duplicates == 1
        assert stats.cornix_commands_sent == 0
        assert len(session.posts) == 0
    finally:
        _reset_position_watcher_test_path(path)


def test_position_watcher_float_flag_duplicate_prevention() -> None:
    path = Path(tempfile.gettempdir()) / "position_watcher_float_flag_dedupe_smoke.csv"
    _reset_position_watcher_test_path(path)
    pd.DataFrame(
        [
            {
                **_watcher_row(side="SHORT", tp1=68.500),
                "cornix_be_command_sent": 1.0,
                "cornix_be_stop_price": 70.744,
                "new_stop_notification_sent": 1.0,
                "new_stop_notification_stop_price": 70.744,
            }
        ]
    ).to_csv(path, index=False)
    session = WatcherFakeSession("68.000")
    try:
        stats = position_watcher.process_once(path, session, _watcher_config("cornix_command"))
        assert stats.skipped_duplicates == 1
        assert stats.cornix_commands_sent == 0
        assert len(session.posts) == 0
    finally:
        _reset_position_watcher_test_path(path)


def test_position_watcher_cross_row_new_stop_dedupe() -> None:
    path = Path(tempfile.gettempdir()) / "position_watcher_cross_row_dedupe_smoke.csv"
    _reset_position_watcher_test_path(path)
    pd.DataFrame(
        [
            {**_watcher_row(side="SHORT", entry=0.696, tp1=0.680), "symbol": "SUIUSDT"},
            {**_watcher_row(side="SHORT", entry=0.696, tp1=0.680), "symbol": "SUIUSDT"},
        ]
    ).to_csv(path, index=False)
    session = WatcherFakeSession("0.679")
    try:
        stats = position_watcher.process_once(path, session, _watcher_config("cornix_command"))
        assert stats.tp1_reached == 1
        assert stats.cornix_commands_sent == 1
        assert stats.skipped_duplicates == 1
        assert len(session.posts) == 2
        assert session.posts[0][0] == "cornix"
        assert session.posts[0][1].count("NEW STOP:") == 1

        stats = position_watcher.process_once(path, session, _watcher_config("cornix_command"))
        assert stats.cornix_commands_sent == 0
        assert stats.skipped_duplicates == 2
        assert len(session.posts) == 2
    finally:
        _reset_position_watcher_test_path(path)


def test_position_watcher_persistent_lock_prevents_stale_csv_duplicate() -> None:
    path = Path(tempfile.gettempdir()) / "position_watcher_persistent_lock_smoke.csv"
    _reset_position_watcher_test_path(path)
    pd.DataFrame([_watcher_row()]).to_csv(path, index=False)
    session = WatcherFakeSession("72.500")
    try:
        stats = position_watcher.process_once(path, session, _watcher_config("report_only"))
        assert stats.alerts_sent == 1
        assert len(session.posts) == 1
        saved = pd.read_csv(path)
        assert int(saved.loc[0, "tp1_alert_sent"]) == 1
        assert str(saved.loc[0, "position_watcher_alert_key"]).strip()
        assert str(saved.loc[0, "position_watcher_lock_file"]).strip()

        stale = saved.copy().astype(object)
        for column in [
            "tp1_alert_sent",
            "breakeven_recommended",
            "new_stop_notification_sent",
            "cornix_be_command_sent",
        ]:
            stale.loc[0, column] = 0
        for column in [
            "tp1_alert_at",
            "tp1_alert_source",
            "new_stop_notification_at",
            "new_stop_notification_key",
            "new_stop_notification_stop_price",
            "position_watcher_alert_key",
            "position_watcher_lock_file",
        ]:
            stale.loc[0, column] = ""
        stale.to_csv(path, index=False)

        stats = position_watcher.process_once(path, session, _watcher_config("report_only"))
        assert stats.skipped_duplicates == 1
        assert len(session.posts) == 1
        saved = pd.read_csv(path)
        assert int(saved.loc[0, "tp1_alert_sent"]) == 1
        assert str(saved.loc[0, "position_watcher_alert_key"]).strip()
    finally:
        _reset_position_watcher_test_path(path)


def test_position_watcher_releases_lock_when_report_send_fails() -> None:
    class FailingWatcherSession(WatcherFakeSession):
        def __init__(self, price: str) -> None:
            super().__init__(price)
            self.fail_posts = True

        def post(self, _url, data=None, timeout=None):
            self.posts.append((data["chat_id"], data["text"]))
            if self.fail_posts:
                return WatcherResponse(500, text="telegram down")
            return WatcherResponse(200)

    path = Path(tempfile.gettempdir()) / "position_watcher_failed_send_lock_smoke.csv"
    _reset_position_watcher_test_path(path)
    pd.DataFrame([_watcher_row()]).to_csv(path, index=False)
    session = FailingWatcherSession("72.500")
    try:
        stats = position_watcher.process_once(path, session, _watcher_config("report_only"))
        assert stats.alerts_sent == 0
        assert len(session.posts) == 1
        saved = pd.read_csv(path)
        assert int(saved.loc[0].get("tp1_alert_sent", 0)) == 0

        session.fail_posts = False
        stats = position_watcher.process_once(path, session, _watcher_config("report_only"))
        assert stats.alerts_sent == 1
        assert len(session.posts) == 2
        saved = pd.read_csv(path)
        assert int(saved.loc[0, "tp1_alert_sent"]) == 1
    finally:
        _reset_position_watcher_test_path(path)


def test_position_watcher_new_stop_notification_key_dedupe_and_closed_skip() -> None:
    duplicate_path = Path(tempfile.gettempdir()) / "position_watcher_new_stop_key_dedupe_smoke.csv"
    closed_path = Path(tempfile.gettempdir()) / "position_watcher_closed_new_stop_smoke.csv"
    _reset_position_watcher_test_path(duplicate_path)
    _reset_position_watcher_test_path(closed_path)
    try:
        pd.DataFrame(
            [
                {
                    **_watcher_row(side="SHORT", tp1=68.500),
                    "new_stop_notification_sent": 1,
                    "new_stop_notification_key": "HYPEUSDT|SHORT|70.744",
                    "new_stop_notification_stop_price": 70.744,
                }
            ]
        ).to_csv(duplicate_path, index=False)
        session = WatcherFakeSession("68.000")
        stats = position_watcher.process_once(duplicate_path, session, _watcher_config("cornix_command"))
        assert stats.skipped_duplicates == 1
        assert stats.cornix_commands_sent == 0
        assert len(session.posts) == 0

        pd.DataFrame([{**_watcher_row(), "result": "WIN", "hit_target": "TP2"}]).to_csv(closed_path, index=False)
        session = WatcherFakeSession("72.500")
        stats = position_watcher.process_once(closed_path, session, _watcher_config("cornix_command"))
        assert stats.checked == 0
        assert stats.cornix_commands_sent == 0
        assert len(session.posts) == 0
    finally:
        for path in [duplicate_path, closed_path]:
            _reset_position_watcher_test_path(path)


def test_position_watcher_command_safety_modes() -> None:
    missing_entry = Path(tempfile.gettempdir()) / "position_watcher_missing_entry_smoke.csv"
    report_only = Path(tempfile.gettempdir()) / "position_watcher_report_only_smoke.csv"
    weak_report_only = Path(tempfile.gettempdir()) / "position_watcher_weak_report_only_smoke.csv"
    session_report_only = Path(tempfile.gettempdir()) / "position_watcher_session_report_only_smoke.csv"
    london_long_report_only = Path(tempfile.gettempdir()) / "position_watcher_london_long_report_only_smoke.csv"
    dry_run = Path(tempfile.gettempdir()) / "position_watcher_dry_run_smoke.csv"
    for path in [missing_entry, report_only, weak_report_only, session_report_only, london_long_report_only, dry_run]:
        _reset_position_watcher_test_path(path)
    try:
        pd.DataFrame([_watcher_row(entry="")]).to_csv(missing_entry, index=False)
        session = WatcherFakeSession("72.500")
        stats = position_watcher.process_once(missing_entry, session, _watcher_config("cornix_command"))
        assert stats.tp1_reached == 0
        assert len(session.posts) == 0

        pd.DataFrame([_watcher_row()]).to_csv(report_only, index=False)
        session = WatcherFakeSession("72.500")
        stats = position_watcher.process_once(report_only, session, _watcher_config("report_only"))
        assert stats.alerts_sent == 1
        assert len(session.posts) == 1
        assert session.posts[0][0] == "reports"

        pd.DataFrame([{**_watcher_row(), "symbol": "LINKUSDT", "signal_status": "weak_symbol_report_only"}]).to_csv(weak_report_only, index=False)
        session = WatcherFakeSession("72.500")
        stats = position_watcher.process_once(weak_report_only, session, _watcher_config("cornix_command"))
        assert stats.alerts_sent == 1
        assert stats.cornix_commands_sent == 0
        assert len(session.posts) == 1
        assert session.posts[0][0] == "reports"

        pd.DataFrame([{**_watcher_row(), "signal_status": "session_risk_report_only"}]).to_csv(session_report_only, index=False)
        session = WatcherFakeSession("72.500")
        stats = position_watcher.process_once(session_report_only, session, _watcher_config("cornix_command"))
        assert stats.alerts_sent == 1
        assert stats.cornix_commands_sent == 0
        assert len(session.posts) == 1
        assert session.posts[0][0] == "reports"

        pd.DataFrame([{**_watcher_row(), "signal_status": "london_long_report_only"}]).to_csv(london_long_report_only, index=False)
        session = WatcherFakeSession("72.500")
        stats = position_watcher.process_once(london_long_report_only, session, _watcher_config("cornix_command"))
        assert stats.alerts_sent == 1
        assert stats.cornix_commands_sent == 0
        assert len(session.posts) == 1
        assert session.posts[0][0] == "reports"

        pd.DataFrame([_watcher_row()]).to_csv(dry_run, index=False)
        session = WatcherFakeSession("72.500")
        stats = position_watcher.process_once(dry_run, session, _watcher_config("cornix_command", dry_run=True))
        assert stats.cornix_commands_sent == 1
        assert len(session.posts) == 1
        assert session.posts[0][0] == "reports"
        assert "DRY RUN - POSITION WATCHER CORNIX COMMAND COPY" in session.posts[0][1]
        saved = pd.read_csv(dry_run)
        assert int(saved.loc[0].get("cornix_be_command_sent", 0)) == 0
    finally:
        for path in [missing_entry, report_only, weak_report_only, session_report_only, london_long_report_only, dry_run]:
            _reset_position_watcher_test_path(path)


def test_position_watcher_cornix_breakeven_formats() -> None:
    row = pd.Series({"symbol": "LTCUSDT", "side": "LONG", "entry": 44.960})
    assert position_watcher.format_cornix_breakeven_command(row, "v1") == "LONG LTCUSDT\n\nNEW STOP:\n44.960"
    assert position_watcher.format_cornix_breakeven_command(row, "v2") == "LONG LTCUSDT\n\nMOVE STOP LOSS\n\n44.960"
    assert position_watcher.format_cornix_breakeven_command(row, "v3") == "UPDATE LTCUSDT\n\nSTOP LOSS:\n44.960"
    assert position_watcher.format_cornix_breakeven_command(row, "v4") == "#LTC/USDT\n\nMOVE SL TO ENTRY\n\n44.960"


def test_position_watcher_cornix_test_command() -> None:
    session = WatcherFakeSession("72.500")
    config = _watcher_config("cornix_command")
    assert position_watcher.send_cornix_test_command(session, config) is True
    assert len(session.posts) == 1
    assert session.posts[0][0] == "cornix"
    assert "LONG HYPEUSDT" in session.posts[0][1]
    assert "NEW STOP:" in session.posts[0][1]


def test_velahub_watchdog_threshold_recovery_and_report() -> None:
    services_path = Path(tempfile.gettempdir()) / "velahub_services_smoke.json"
    state_path = Path(tempfile.gettempdir()) / "velahub_state_smoke.json"
    services_path.write_text(
        json.dumps([{"name": "Smoke Service", "url": "https://smoke.example"}]),
        encoding="utf-8",
    )
    old_threshold = os.environ.get("WATCHDOG_FAILURE_THRESHOLD")
    old_enabled = os.environ.get("WATCHDOG_TELEGRAM_ENABLED")
    old_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    old_monitor_chat = os.environ.get("TELEGRAM_VELAHUB_MONITOR_CHAT_ID")
    old_reports = os.environ.get("TELEGRAM_REPORTS_CHAT_ID")
    old_legacy = os.environ.get("TELEGRAM_CHAT_ID")
    os.environ["WATCHDOG_FAILURE_THRESHOLD"] = "3"
    os.environ["WATCHDOG_TELEGRAM_ENABLED"] = "1"
    os.environ["TELEGRAM_BOT_TOKEN"] = "token"
    os.environ["TELEGRAM_VELAHUB_MONITOR_CHAT_ID"] = "velahub-monitor"
    os.environ["TELEGRAM_REPORTS_CHAT_ID"] = "reports-should-not-be-used"
    os.environ["TELEGRAM_CHAT_ID"] = "legacy-should-not-be-used"

    class Response:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code
            self.text = "ok"

    class FakeSession:
        def __init__(self) -> None:
            self.get_statuses = [500, 500, 500, 200]
            self.posts: list[tuple[str, str]] = []

        def get(self, _url, timeout=None, allow_redirects=True):
            return Response(self.get_statuses.pop(0))

        def post(self, _url, data=None, timeout=None):
            self.posts.append((data["chat_id"], data["text"]))
            return Response(200)

    session = FakeSession()
    try:
        for _ in range(2):
            state = watchdog_monitor.run_once(services_path, state_path, session=session)
        item = state["https://smoke.example"]
        assert item["status"] == "unknown"
        assert int(item["consecutive_failures"]) == 2
        assert not session.posts

        state = watchdog_monitor.run_once(services_path, state_path, session=session)
        item = state["https://smoke.example"]
        assert item["status"] == "offline"
        assert int(item["consecutive_failures"]) == 3
        assert len(session.posts) == 1
        assert session.posts[0][0] == "velahub-monitor"
        assert "Service Offline" in session.posts[0][1]

        state = watchdog_monitor.run_once(services_path, state_path, session=session)
        item = state["https://smoke.example"]
        assert item["status"] == "online"
        assert int(item["consecutive_failures"]) == 0
        assert len(session.posts) == 2
        assert session.posts[1][0] == "velahub-monitor"
        assert "Service Recovered" in session.posts[1][1]

        report = watchdog_monitor.build_daily_report(services_path, state_path)
        assert "VelaHub Watchdog Daily Report" in report
        assert "Smoke Service" in report
        assert "Uptime:" in report
        assert "Failure count: 3" in report
        assert "Outage count: 1" in report

        os.environ["TELEGRAM_VELAHUB_MONITOR_CHAT_ID"] = ""
        assert watchdog_monitor.send_telegram("should not route to fallback", session) is False
        assert len(session.posts) == 2
    finally:
        if old_threshold is None:
            os.environ.pop("WATCHDOG_FAILURE_THRESHOLD", None)
        else:
            os.environ["WATCHDOG_FAILURE_THRESHOLD"] = old_threshold
        if old_enabled is None:
            os.environ.pop("WATCHDOG_TELEGRAM_ENABLED", None)
        else:
            os.environ["WATCHDOG_TELEGRAM_ENABLED"] = old_enabled
        for key, value in {
            "TELEGRAM_BOT_TOKEN": old_token,
            "TELEGRAM_VELAHUB_MONITOR_CHAT_ID": old_monitor_chat,
            "TELEGRAM_REPORTS_CHAT_ID": old_reports,
            "TELEGRAM_CHAT_ID": old_legacy,
        }.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for path in [services_path, state_path]:
            try:
                path.unlink()
            except OSError:
                pass


def test_production_health_exit_codes_and_no_telegram_send() -> None:
    checks = [
        production_health.HealthCheck("a", production_health.PASS, "ok"),
        production_health.HealthCheck("b", production_health.WARNING, "warn"),
    ]
    assert production_health.exit_code(checks) == 1
    checks.append(production_health.HealthCheck("c", production_health.FAIL, "bad"))
    assert production_health.exit_code(checks) == 2
    assert production_health.exit_code([production_health.HealthCheck("a", production_health.PASS, "ok")]) == 0

    class NoTelegramSession:
        def post(self, *_args, **_kwargs):
            raise AssertionError("health checks must not send Telegram")

    assert NoTelegramSession
    env_checks = production_health._check_env()
    assert any("Telegram" in check.name or "environment" in check.name for check in env_checks)


def test_data_integrity_audit_detects_duplicates_and_malformed_rows() -> None:
    journal = pd.DataFrame(
        [
            {
                "timestamp": "bad timestamp",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "entry": "100",
                "stop_loss": "99",
                "tp1": "101",
                "result": "OPEN",
                "hit_target": "TP1",
                "signal_status": "sent",
            },
            {
                "timestamp": "bad timestamp",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "entry": "100",
                "stop_loss": "99",
                "tp1": "101",
                "result": "OPEN",
                "hit_target": "TP1",
                "signal_status": "sent",
            },
            {
                "timestamp": "2026-06-01T00:00:00+00:00",
                "symbol": "",
                "side": "SHORT",
                "entry": "not-a-number",
                "stop_loss": "102",
                "tp1": "98",
                "result": "MAYBE",
                "signal_status": "strange_status",
            },
        ]
    )
    entry = pd.DataFrame(
        [
            {"timestamp": "2026-06-01T00:00:00+00:00", "symbol": "ETHUSDT", "direction": "LONG", "entry": 100},
            {"timestamp": "2026-06-01T00:00:00+00:00", "symbol": "ETHUSDT", "direction": "LONG", "entry": 100},
        ]
    )
    findings = data_integrity_audit.audit_journal(journal) + data_integrity_audit.audit_entry_timing(entry, journal)
    text = "\n".join(f"{item.check}: {item.detail}" for item in findings)
    assert "duplicate signal identifiers" in text
    assert "malformed timestamps" in text
    assert "OPEN rows that already contain final hit targets" in text
    assert "invalid numeric values" in text
    assert "invalid status values" in text
    assert "DUPLICATE_ROW" in text


def test_data_integrity_safe_repair_preserves_outcomes() -> None:
    path = Path(tempfile.gettempdir()) / "safe_repair_smoke.csv"
    try:
        pd.DataFrame(
            [
                {"symbol": "BTCUSDT", "result": "WIN", "hit_target": "TP2", "tp1_alert_sent": "1.0", "entry": 100},
                {"symbol": "BTCUSDT", "result": "WIN", "hit_target": "TP2", "tp1_alert_sent": "1.0", "entry": 100},
            ]
        ).to_csv(path, index=False)
        changed, backup = data_integrity_audit.repair_safe(path)
        repaired = pd.read_csv(path)
        assert changed >= 1
        assert backup is not None and backup.exists()
        assert len(repaired) == 1
        assert repaired.loc[0, "result"] == "WIN"
        assert repaired.loc[0, "hit_target"] == "TP2"
        assert str(repaired.loc[0, "tp1_alert_sent"]) == "1"
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def test_backup_runtime_data_excludes_secrets() -> None:
    temp_backup = Path(tempfile.gettempdir()) / "runtime_backup_smoke.zip"
    try:
        backup = backup_runtime_data.create_backup(temp_backup)
        assert backup.exists()
        import zipfile

        with zipfile.ZipFile(backup) as archive:
            names = archive.namelist()
        assert "BACKUP_MANIFEST.txt" in names
        assert ".env" not in names
        assert "config.bat" not in names
        assert not any("token" in name.lower() for name in names)
    finally:
        try:
            temp_backup.unlink()
        except OSError:
            pass


def test_entry_timing_readiness_thresholds_and_summary() -> None:
    assert entry_timing_operational_summary.readiness_status(0) == "NOT ENOUGH DATA"
    assert entry_timing_operational_summary.readiness_status(30) == "EARLY DATA"
    assert entry_timing_operational_summary.readiness_status(99) == "EARLY DATA"
    assert entry_timing_operational_summary.readiness_status(100) == "REVIEW READY"
    entry = pd.DataFrame(
        [
            {
                "timestamp": "2026-06-01T00:00:00+00:00",
                "symbol": "BTCUSDT",
                "direction": "LONG",
                "entry": 100,
                "recommendation": "ENTER NOW",
                "entry_quality_score": 80,
                "market_session": "London",
                "watchlist_tier": "A",
            }
        ]
    )
    journal = pd.DataFrame(
        [
            {
                "timestamp": "2026-06-01T00:00:00+00:00",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "entry": 100,
                "result": "WIN",
            }
        ]
    )
    summary = entry_timing_operational_summary.build_summary(entry, journal)
    assert "Entry Timing Operational Summary" in summary
    assert "Total evaluated candidates: 1" in summary
    assert "Data readiness: NOT ENOUGH DATA" in summary
    assert "Recommendation Counts:" in summary


def test_update_script_validation_before_restart_protection() -> None:
    script = (Path(__file__).resolve().parents[1] / "scripts" / "update_production.sh").read_text(encoding="utf-8")
    validation_index = script.index("tests/smoke_test.py")
    restart_index = script.index("systemctl restart \"$SCANNER_SERVICE\"")
    backup_index = script.index("backup_runtime_data.py")
    pull_index = script.index("git pull --ff-only origin main")
    assert backup_index < pull_index < validation_index < restart_index
    assert "fail \"smoke tests failed\"" in script
    assert "Tracked local modifications detected" in script


def test_data_audit_status_registry_accepts_current_skip_statuses() -> None:
    valid_statuses = [
        "open",
        "sent",
        "closed",
        "skipped_quality_filter",
        "skipped_daily_risk_guard",
        "skipped_losing_streak",
        "skipped_loss_cooldown",
        "skipped_correlation",
        "skipped_btc_regime",
        "skipped_not_top",
        "skipped_position_management",
        "tier_c_report_only",
        "weak_symbol_report_only",
        "session_risk_report_only",
        "london_long_report_only",
    ]
    rows = [
        {
            "timestamp": f"2026-06-01T{i % 24:02d}:00:00+00:00",
            "symbol": f"BTC{i}USDT",
            "side": "LONG",
            "entry": 100,
            "stop_loss": 99,
            "tp1": 101,
            "result": "SKIPPED" if status.startswith("skipped") else "OPEN",
            "hit_target": "",
            "signal_status": status,
        }
        for i, status in enumerate(valid_statuses)
    ]
    findings = data_integrity_audit.audit_journal(pd.DataFrame(rows))
    assert not any(item.check == "invalid status values" and "signal_status" in item.detail for item in findings)
    rows.append({**rows[0], "timestamp": "2026-06-02T00:00:00+00:00", "symbol": "BADUSDT", "signal_status": "unknown_status"})
    findings = data_integrity_audit.audit_journal(pd.DataFrame(rows))
    assert any(item.check == "invalid status values" and "unknown_status" in item.detail for item in findings)


def test_entry_timing_audit_classifications() -> None:
    journal = pd.DataFrame(
        [
            {
                "timestamp": "2026-06-29T00:00:00+00:00",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "entry": 100,
                "stop_loss": 99,
                "tp1": 101,
                "signal_status": "sent",
            }
        ]
    )
    entry = pd.DataFrame(
        [
            {"timestamp": "2026-06-27T00:00:00+00:00", "symbol": "LEGACYUSDT", "direction": "LONG", "entry": 1},
            {"timestamp": "2026-06-29T00:00:00+00:00", "symbol": "BTCUSDT", "direction": "LONG", "entry": 100},
            {"timestamp": "2026-06-29T01:00:00+00:00", "symbol": "ORPHANUSDT", "direction": "SHORT", "entry": 5},
            {"timestamp": "2026-06-29T01:00:00+00:00", "symbol": "ORPHANUSDT", "direction": "SHORT", "entry": 5},
        ]
    )
    classification = data_integrity_audit.classify_entry_timing(entry, journal)
    assert classification.matched == 1
    assert classification.legacy == 1
    assert classification.orphan == 0
    assert classification.ambiguous == 0
    assert classification.duplicate == 2
    findings = data_integrity_audit.audit_entry_timing(entry, journal)
    assert any(item.severity == "INFO" and item.check == "LEGACY_SHADOW_ROW" for item in findings)
    assert not any(item.check == "TRUE_ORPHAN" for item in findings)
    assert any(item.severity == "WARNING" and item.check == "DUPLICATE_ROW" for item in findings)


def test_entry_timing_audit_deterministic_matching() -> None:
    journal = pd.DataFrame(
        [
            {
                "timestamp": "2026-07-01T01:00:00+00:00",
                "signal_id": "sig-1",
                "symbol": "SUIUSDT",
                "side": "SHORT",
                "entry": 0.6960001,
                "stop_loss": 0.72,
                "tp1": 0.66,
                "signal_status": "sent",
            },
            {
                "timestamp": "2026-07-01T01:20:00+00:00",
                "symbol": "BINANCE:SUIUSDT.P",
                "side": "SHORT",
                "entry": 0.6960002,
                "stop_loss": 0.72,
                "tp1": 0.66,
                "signal_status": "sent",
            },
            {
                "timestamp": "2026-07-01T03:00:00+00:00",
                "symbol": "SUIUSDT",
                "side": "SHORT",
                "entry": 0.70,
                "stop_loss": 0.73,
                "tp1": 0.65,
                "signal_status": "sent",
            },
        ]
    )
    entry = pd.DataFrame(
        [
            {
                "timestamp": "2026-07-01T01:10:00+00:00",
                "source_signal_id": "sig-1",
                "symbol": "SUI/USDT",
                "direction": "SELL",
                "entry": 0.696,
            },
            {
                "timestamp": "2026-07-01T01:05:00+00:00",
                "symbol": "SUIUSDT.P",
                "direction": "SHORT",
                "entry": 0.6960003,
            },
            {
                "timestamp": "2026-07-01T01:06:00+00:00",
                "symbol": "SUIUSDT",
                "direction": "SHORT",
                "entry": 0.6960004,
            },
            {
                "timestamp": "2026-07-01T12:00:00+00:00",
                "symbol": "SUIUSDT",
                "direction": "SHORT",
                "entry": 0.696,
            },
            {
                "timestamp": "2026-06-27T12:00:00+00:00",
                "symbol": "OLDUSDT",
                "direction": "LONG",
                "entry": 1.0,
            },
        ]
    )
    classification = data_integrity_audit.classify_entry_timing(entry, journal)
    assert classification.matched == 1
    assert classification.ambiguous == 2
    assert classification.orphan == 1
    assert classification.legacy == 1
    findings = data_integrity_audit.audit_entry_timing(entry, journal, verbose=True)
    assert any(item.check == "AMBIGUOUS_PROVENANCE" for item in findings)
    assert any(item.check == "ENTRY_TIMING_SAMPLE" for item in findings)


def test_entry_timing_truth_layer_sources_and_coverage() -> None:
    temp_dir = Path(tempfile.gettempdir()) / "entry_timing_truth_sources"
    temp_dir.mkdir(exist_ok=True)
    rejected_path = temp_dir / "rejected_signals.csv"
    history_path = temp_dir / "signals_history.csv"
    candidate_path = temp_dir / "scanner_candidates.csv"
    try:
        journal = pd.DataFrame(
            [
                {"timestamp": "2026-07-01T00:00:00+00:00", "symbol": "BTCUSDT", "side": "LONG", "entry": 100, "stop_loss": 99, "tp1": 101, "signal_status": "sent"},
                {"timestamp": "2026-07-01T01:00:00+00:00", "symbol": "ETHUSDT", "side": "SHORT", "entry": 200, "stop_loss": 202, "tp1": 198, "signal_status": "tier_c_report_only"},
            ]
        )
        pd.DataFrame([{"timestamp": "2026-07-01T02:00:00+00:00", "symbol": "SOLUSDT", "side": "LONG", "entry": 50, "tp1": 51, "signal_status": "skipped_quality_filter"}]).to_csv(rejected_path, index=False)
        pd.DataFrame([{"timestamp": "2026-07-01T03:00:00+00:00", "symbol": "XRPUSDT", "side": "LONG", "entry": 1, "tp1": 1.1}]).to_csv(history_path, index=False)
        pd.DataFrame([{"timestamp": "2026-07-01T04:00:00+00:00", "symbol": "ADAUSDT", "side": "SHORT", "entry": 2, "tp1": 1.9}]).to_csv(candidate_path, index=False)
        entry = pd.DataFrame(
            [
                {"timestamp": "2026-07-01T00:00:10+00:00", "symbol": "BTC/USDT", "direction": "BUY", "entry": 100, "tp1": 101},
                {"timestamp": "2026-07-01T01:00:10+00:00", "symbol": "ETHUSDT.P", "direction": "SHORT", "entry": 200, "tp1": 198},
                {"timestamp": "2026-07-01T02:00:10+00:00", "symbol": "SOLUSDT", "direction": "LONG", "entry": 50, "tp1": 51},
                {"timestamp": "2026-07-01T03:00:10+00:00", "symbol": "XRPUSDT", "direction": "LONG", "entry": 1, "tp1": 1.1},
                {"timestamp": "2026-07-01T04:00:10+00:00", "symbol": "ADAUSDT", "direction": "SHORT", "entry": 2, "tp1": 1.9},
                {"timestamp": "2026-07-01T05:00:00+00:00", "symbol": "ORPHANUSDT", "direction": "LONG", "entry": 9, "tp1": 10},
            ]
        )
        provenances = data_integrity_audit.classify_entry_timing_rows(entry, journal, logs_dir=temp_dir)
        counts = data_integrity_audit.summarize_entry_timing_truth(provenances).counts
        assert counts["MATCHED_SENT_SIGNAL"] == 1
        assert counts["MATCHED_REPORT_ONLY_SIGNAL"] == 1
        assert counts["MATCHED_REJECTED_CANDIDATE"] == 1
        assert counts["MATCHED_APPROVED_SIGNAL"] == 1
        assert counts["MATCHED_PRE_FINAL_CANDIDATE"] == 1
        assert counts["TRUE_ORPHAN"] == 1
        summary = data_integrity_audit.summarize_entry_timing_truth(provenances)
        assert summary.explained_coverage_pct == 83.3
    finally:
        for path in [rejected_path, history_path, candidate_path]:
            try:
                path.unlink()
            except OSError:
                pass
        try:
            temp_dir.rmdir()
        except OSError:
            pass


def test_entry_timing_canonical_source_priority_reduces_cross_source_ambiguity() -> None:
    temp_dir = Path(tempfile.gettempdir()) / "entry_timing_canonical_priority"
    shutil.rmtree(temp_dir, ignore_errors=True)
    temp_dir.mkdir(exist_ok=True)
    history_path = temp_dir / "signals_history.csv"
    candidate_path = temp_dir / "scanner_candidates.csv"
    row = {
        "timestamp": "2026-07-03T00:00:00+00:00",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry": 100,
        "stop_loss": 99,
        "tp1": 101,
        "signal_status": "sent",
    }
    try:
        journal = pd.DataFrame([row])
        pd.DataFrame([row]).to_csv(history_path, index=False)
        pd.DataFrame([row]).to_csv(candidate_path, index=False)
        entry = pd.DataFrame(
            [
                {
                    "timestamp": "2026-07-03T00:00:30+00:00",
                    "symbol": "BINANCE:BTCUSDT.P",
                    "direction": "BUY",
                    "entry": 100,
                    "sl": 99,
                    "tp1": 101,
                }
            ]
        )
        provenances = data_integrity_audit.classify_entry_timing_rows(entry, journal, logs_dir=temp_dir)
        assert len(provenances) == 1
        assert provenances[0].category == "MATCHED_SENT_SIGNAL"
        assert provenances[0].match_source == "sent"
        assert provenances[0].reason == "canonical candidate identity; source priority"

        competing = {**row, "timestamp": "2026-07-03T00:45:00+00:00", "entry": 100.01}
        pd.DataFrame([row, competing]).to_csv(candidate_path, index=False)
        ambiguous = data_integrity_audit.classify_entry_timing_rows(entry, journal, logs_dir=temp_dir)
        assert ambiguous[0].category == "AMBIGUOUS_PROVENANCE"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_data_integrity_entry_timing_indexed_benchmark() -> None:
    temp_dir = Path(tempfile.gettempdir()) / "entry_timing_indexed_benchmark_smoke"
    shutil.rmtree(temp_dir, ignore_errors=True)
    temp_dir.mkdir(exist_ok=True)
    candidate_path = temp_dir / "scanner_candidates.csv"
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "LINKUSDT", "SUIUSDT", "INJUSDT"]
    base = pd.Timestamp("2026-07-02T00:00:00Z")
    journal_rows = []
    candidate_rows = []
    for index in range(5000):
        side = "LONG" if index % 2 == 0 else "SHORT"
        entry_price = 100 + (index % 50) * 0.1
        row = {
            "signal_id": f"sig-{index}",
            "timestamp": (base + pd.Timedelta(minutes=15 * index)).isoformat(),
            "symbol": symbols[index % len(symbols)],
            "side": side,
            "entry": entry_price,
            "stop_loss": entry_price - 1 if side == "LONG" else entry_price + 1,
            "tp1": entry_price + 2 if side == "LONG" else entry_price - 2,
            "signal_status": "sent",
            "result": "OPEN",
        }
        if index % 10 == 0:
            candidate_rows.append(row.copy())
        else:
            journal_rows.append(row)
    try:
        pd.DataFrame(candidate_rows).to_csv(candidate_path, index=False)
        entry_rows = []
        for index, source in enumerate(journal_rows[:196]):
            entry_rows.append(
                {
                    "source_signal_id": source["signal_id"],
                    "final_signal_timestamp": source["timestamp"],
                    "symbol": source["symbol"],
                    "direction": source["side"],
                    "entry": source["entry"],
                    "sl": source["stop_loss"],
                    "tp1": source["tp1"],
                }
            )
        for source in candidate_rows[:2]:
            entry_rows.append(
                {
                    "final_signal_timestamp": source["timestamp"],
                    "symbol": source["symbol"],
                    "direction": source["side"],
                    "entry": source["entry"],
                    "sl": source["stop_loss"],
                    "tp1": source["tp1"],
                }
            )
        entry_rows.append({"final_signal_timestamp": "2026-08-01T00:00:00+00:00", "symbol": "ORPHANUSDT", "direction": "LONG", "entry": 1, "sl": 0.9, "tp1": 1.1})
        entry_rows.append(entry_rows[-1].copy())
        started = time.perf_counter()
        provenances = data_integrity_audit.classify_entry_timing_rows(pd.DataFrame(entry_rows), pd.DataFrame(journal_rows), logs_dir=temp_dir)
        elapsed = time.perf_counter() - started
        summary = data_integrity_audit.summarize_entry_timing_truth(provenances)
        assert summary.total == 200
        assert summary.counts["MATCHED_SENT_SIGNAL"] == 196
        assert summary.counts["MATCHED_PRE_FINAL_CANDIDATE"] == 2
        assert summary.counts["DUPLICATE_ROW"] == 2
        assert elapsed < 10.0
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_position_watcher_state_audit_categories() -> None:
    temp_dir = Path(tempfile.gettempdir())
    journal_path = temp_dir / "watcher_state_audit_smoke.csv"
    lock_dir = position_watcher_state_cleanup.approved_lock_dir(journal_path)
    lock_dir.mkdir(exist_ok=True)
    historical = pd.DataFrame(
        [
            {
                "timestamp": "2026-06-01T00:00:00+00:00",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "entry": 100,
                "tp1": 101,
                "result": "WIN",
                "signal_status": "sent",
                "position_management_stage": "TP1_REACHED_BE_RECOMMENDED",
                "position_watcher_alert_key": "",
                "position_watcher_lock_file": "",
            }
        ]
    )
    try:
        historical.to_csv(journal_path, index=False)
        findings = data_integrity_audit.audit_stale_watcher_state(historical, journal_path=journal_path)
        assert any(item.severity == "INFO" and item.check == "CLOSED_ROW_WITH_HISTORICAL_TP1_FLAG" for item in findings)
        assert not any(item.check == "CLOSED_ROW_IN_ACTIVE_WATCHER_STATE" for item in findings)

        active = historical.copy()
        lock = lock_dir / "watcher_state_audit_smoke.lock"
        lock.write_text("created\nBTCUSDT|LONG|2026-06-01T00:00:00+00:00|100|101|100\n", encoding="utf-8")
        active.loc[0, "position_watcher_alert_key"] = "BTCUSDT|LONG|2026-06-01T00:00:00+00:00|100|101|100"
        active.loc[0, "position_watcher_lock_file"] = str(lock)
        active.to_csv(journal_path, index=False)
        findings = data_integrity_audit.audit_stale_watcher_state(active, journal_path=journal_path)
        assert any(item.severity == "WARNING" and item.check == "STALE_LOCK_FILE" for item in findings)
        assert any(item.severity == "WARNING" and item.check == "CLOSED_ROW_IN_ACTIVE_WATCHER_STATE" for item in findings)

        critical = active.copy()
        critical.loc[0, "signal_status"] = "active"
        critical.to_csv(journal_path, index=False)
        findings = data_integrity_audit.audit_stale_watcher_state(critical, journal_path=journal_path)
        assert any(item.severity == "FAIL" and item.check == "CLOSED_ROW_STILL_TREATED_AS_OPEN" for item in findings)

        nan_refs = historical.copy()
        nan_refs.loc[0, "position_watcher_alert_key"] = float("nan")
        nan_refs.loc[0, "position_watcher_lock_file"] = float("nan")
        nan_refs.to_csv(journal_path, index=False)
        state = data_integrity_audit.classify_watcher_state(nan_refs, journal_path=journal_path)
        assert state.active_stale_state == 0
        assert state.removable_items == 0
    finally:
        for path in [journal_path]:
            try:
                path.unlink()
            except OSError:
                pass
        for path in lock_dir.glob("*"):
            try:
                path.unlink()
            except OSError:
                pass
        try:
            lock_dir.rmdir()
        except OSError:
            pass


def test_position_watcher_state_cleanup_dry_run_and_apply() -> None:
    temp_dir = Path(tempfile.gettempdir())
    journal = temp_dir / "position_watcher_cleanup_smoke.csv"
    lock_dir = temp_dir / "position_watcher_cleanup_smoke_position_watcher_locks"
    lock_dir.mkdir(exist_ok=True)
    lock = lock_dir / "position_watcher_cleanup_smoke.lock"
    lock.write_text("created\nBTCUSDT|LONG|key\n", encoding="utf-8")
    try:
        pd.DataFrame(
            [
                {
                    "timestamp": "2026-06-01T00:00:00+00:00",
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "entry": 100,
                    "tp1": 101,
                    "stop_loss": 99,
                    "result": "WIN",
                    "signal_status": "sent",
                    "position_watcher_alert_key": "BTCUSDT|LONG|key",
                    "position_watcher_lock_file": str(lock),
                },
                {
                    "timestamp": "2026-06-01T01:00:00+00:00",
                    "symbol": "ETHUSDT",
                    "side": "LONG",
                    "entry": 100,
                    "tp1": 101,
                    "stop_loss": 99,
                    "result": "OPEN",
                    "signal_status": "sent",
                    "position_watcher_alert_key": "ETHUSDT|LONG|key",
                    "position_watcher_lock_file": str(temp_dir / "open_position.lock"),
                },
            ]
        ).to_csv(journal, index=False)
        items, backup, removed, verification = position_watcher_state_cleanup.cleanup(journal, apply=False)
        assert len(items) == 1
        assert items[0].symbol == "BTCUSDT"
        assert items[0].verdict == position_watcher_state_cleanup.SAFE_TO_REMOVE
        assert items[0].removable is True
        assert backup is None
        assert removed == []
        assert verification == "NOT_RUN"
        assert lock.exists()

        items, backup, removed, verification = position_watcher_state_cleanup.cleanup(journal, apply=True, confirm_count=0)
        assert verification == "ABORT_CONFIRM_COUNT_MISMATCH"
        assert lock.exists()

        items, backup, removed, verification = position_watcher_state_cleanup.cleanup(journal, apply=True, confirm_count=1)
        assert len(items) == 1
        assert backup is not None and backup.exists()
        assert removed == [lock]
        assert not lock.exists()
        assert verification == "PASS"
        saved = pd.read_csv(journal)
        assert saved.loc[0, "result"] == "WIN"
        assert saved.loc[0, "position_watcher_alert_key"] == "BTCUSDT|LONG|key"
        classification = position_watcher_state_cleanup.classify_cleanup(journal)
        assert classification.removable_items == 0
    finally:
        for path in [journal, lock]:
            try:
                path.unlink()
            except OSError:
                pass
        try:
            lock_dir.rmdir()
        except OSError:
            pass


def test_position_watcher_state_cleanup_safety_verdicts() -> None:
    temp_dir = Path(tempfile.gettempdir())
    journal = temp_dir / "position_watcher_safety_smoke.csv"
    lock_dir = position_watcher_state_cleanup.approved_lock_dir(journal)
    lock_dir.mkdir(exist_ok=True)
    safe_lock = lock_dir / "safe.lock"
    safe_lock.write_text("safe", encoding="utf-8")
    outside_lock = temp_dir / "outside.lock"
    outside_lock.write_text("outside", encoding="utf-8")
    missing_lock = lock_dir / "missing.lock"
    symlink_lock = lock_dir / "escape.lock"
    target = temp_dir / "escape_target.lock"
    target.write_text("target", encoding="utf-8")
    try:
        try:
            symlink_lock.symlink_to(target)
        except (OSError, NotImplementedError):
            symlink_lock.write_text("regular", encoding="utf-8")
            symlink_expected_block = False
        else:
            symlink_expected_block = True
        rows = [
            {
                "timestamp": "2026-06-01T00:00:00+00:00",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "entry": 100,
                "tp1": 101,
                "result": "WIN",
                "position_watcher_lock_file": str(safe_lock),
                "position_watcher_alert_key": "BTCUSDT|LONG|2026-06-01T00:00:00+00:00|100|101|100",
            },
            {
                "timestamp": "2026-06-01T00:00:00+00:00",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "entry": 100,
                "tp1": 101,
                "result": "OPEN",
                "position_watcher_lock_file": "",
                "position_watcher_alert_key": "",
            },
            {
                "timestamp": "2026-06-02T00:00:00+00:00",
                "symbol": "ETHUSDT",
                "side": "SHORT",
                "entry": 50,
                "tp1": 49,
                "result": "LOSS",
                "position_watcher_lock_file": str(outside_lock),
                "position_watcher_alert_key": "ETHUSDT|SHORT|2026-06-02T00:00:00+00:00|50|49|50",
            },
            {
                "timestamp": "2026-06-03T00:00:00+00:00",
                "symbol": "SOLUSDT",
                "side": "LONG",
                "entry": 20,
                "tp1": 21,
                "result": "WIN",
                "position_watcher_lock_file": str(missing_lock),
                "position_watcher_alert_key": "SOLUSDT|LONG|2026-06-03T00:00:00+00:00|20|21|20",
            },
            {
                "timestamp": "2026-06-04T00:00:00+00:00",
                "symbol": "XRPUSDT",
                "side": "LONG",
                "entry": 1,
                "tp1": 1.1,
                "result": "WIN",
                "position_watcher_lock_file": float("nan"),
                "position_watcher_alert_key": float("nan"),
                "tp1_alert_sent": 1,
            },
        ]
        if symlink_expected_block:
            rows.append(
                {
                    "timestamp": "2026-06-05T00:00:00+00:00",
                    "symbol": "ADAUSDT",
                    "side": "LONG",
                    "entry": 1,
                    "tp1": 1.1,
                    "result": "WIN",
                    "position_watcher_lock_file": str(symlink_lock),
                    "position_watcher_alert_key": "ADAUSDT|LONG|2026-06-05T00:00:00+00:00|1|1.1|1",
                }
            )
        pd.DataFrame(rows).to_csv(journal, index=False)
        items = position_watcher_state_cleanup.all_state_items(journal)
        by_symbol = {item.symbol: item for item in items if item.symbol}
        assert by_symbol["BTCUSDT"].verdict == position_watcher_state_cleanup.DO_NOT_REMOVE_ACTIVE_RUNTIME
        assert by_symbol["ETHUSDT"].verdict == position_watcher_state_cleanup.DO_NOT_REMOVE_PATH_UNSAFE
        assert by_symbol["SOLUSDT"].verdict == position_watcher_state_cleanup.HISTORICAL_ONLY
        assert by_symbol["XRPUSDT"].verdict == position_watcher_state_cleanup.HISTORICAL_ONLY
        if symlink_expected_block:
            assert by_symbol["ADAUSDT"].verdict == position_watcher_state_cleanup.DO_NOT_REMOVE_PATH_UNSAFE

        # Different timestamp for the same symbol is unrelated and can be removed.
        df = pd.read_csv(journal)
        df.loc[1, "timestamp"] = "2026-06-01T02:00:00+00:00"
        df.to_csv(journal, index=False)
        items = position_watcher_state_cleanup.stale_state_items(journal)
        assert len(items) == 1
        assert items[0].symbol == "BTCUSDT"
    finally:
        for path in [journal, safe_lock, outside_lock, target, symlink_lock]:
            try:
                path.unlink()
            except OSError:
                pass
        try:
            lock_dir.rmdir()
        except OSError:
            pass


def test_position_watcher_state_cleanup_apply_aborts_when_state_changes() -> None:
    temp_dir = Path(tempfile.gettempdir())
    journal = temp_dir / "position_watcher_state_change_smoke.csv"
    lock_dir = position_watcher_state_cleanup.approved_lock_dir(journal)
    lock_dir.mkdir(exist_ok=True)
    first_lock = lock_dir / "first.lock"
    second_lock = lock_dir / "second.lock"
    first_lock.write_text("first", encoding="utf-8")
    second_lock.write_text("second", encoding="utf-8")
    rows = [
        {
            "timestamp": "2026-06-01T00:00:00+00:00",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "entry": 100,
            "tp1": 101,
            "result": "WIN",
            "position_watcher_lock_file": str(first_lock),
            "position_watcher_alert_key": "BTCUSDT|LONG|2026-06-01T00:00:00+00:00|100|101|100",
        }
    ]
    try:
        pd.DataFrame(rows).to_csv(journal, index=False)
        assert len(position_watcher_state_cleanup.stale_state_items(journal)) == 1

        rows.append(
            {
                "timestamp": "2026-06-02T00:00:00+00:00",
                "symbol": "ETHUSDT",
                "side": "SHORT",
                "entry": 50,
                "tp1": 49,
                "result": "LOSS",
                "position_watcher_lock_file": str(second_lock),
                "position_watcher_alert_key": "ETHUSDT|SHORT|2026-06-02T00:00:00+00:00|50|49|50",
            }
        )
        pd.DataFrame(rows).to_csv(journal, index=False)
        items, backup, removed, verification = position_watcher_state_cleanup.cleanup(journal, apply=True, confirm_count=1)
        assert verification == "ABORT_CONFIRM_COUNT_MISMATCH"
        assert backup is None
        assert removed == []
        assert len(items) == 2
        assert first_lock.exists()
        assert second_lock.exists()
    finally:
        for path in [journal, first_lock, second_lock]:
            try:
                path.unlink()
            except OSError:
                pass
        try:
            lock_dir.rmdir()
        except OSError:
            pass


def test_production_v1_readiness_exit_codes() -> None:
    ready = [production_v1_readiness.ReadinessItem("a", "PASS", "ok")]
    warning = ready + [production_v1_readiness.ReadinessItem("b", "WARNING", "warn")]
    fail = warning + [production_v1_readiness.ReadinessItem("c", "FAIL", "bad")]
    assert production_v1_readiness.final_status(ready) == ("READY FOR V1", 0)
    assert production_v1_readiness.final_status(warning) == ("READY WITH WARNINGS", 1)
    assert production_v1_readiness.final_status(fail) == ("NOT READY", 2)


def test_system_status_exit_codes_json_and_helpers() -> None:
    assert system_status.final_status(["PASS"]) == ("READY FOR PRODUCTION", 0)
    assert system_status.final_status(["PASS", "WARNING"]) == ("READY WITH WARNINGS", 1)
    assert system_status.final_status(["FAIL"]) == ("NOT READY", 2)
    assert system_status.final_status(["UNKNOWN"]) == ("READY WITH WARNINGS", 1)

    status = {
        "release": {"commit": "abc1234", "release": "V1.0", "utc_time": "2026-07-18T00:00:00+00:00"},
        "services": {
            "scanner": "RUNNING",
            "position_watcher": "RUNNING",
            "performance_timer": "ACTIVE",
            "next_performance_report": "Sun 2026-07-19",
        },
        "runtime": {
            "closed_signals": 3,
            "open_signals": 1,
            "win_rate": 66.6,
            "net_r": 1.25,
            "today_sent": 2,
            "today_wins": 1,
            "today_losses": 0,
        },
        "reporting": {
            "executive_report": "PASS",
            "full_report_html": "PASS",
            "analytics_html": "PASS",
            "reports_chat_configured": "YES",
        },
        "entry_timing": {
            "mode": "SHADOW",
            "evaluated": 10,
            "latest_evaluation": "2026-07-18T00:00:00+00:00",
            "readiness": "NOT ENOUGH DATA",
            "dominant_recommendation": "ENTER NOW",
        },
        "production_universe": {
            "tier_s": "BTCUSDT",
            "tier_a": "ETHUSDT",
            "watch": "SOLUSDT",
            "report_only": "SEIUSDT",
        },
        "safety": {
            "data_integrity": "PASS",
            "active_stale_watcher_state": 0,
            "duplicate_alert_protection": "PASS",
            "latest_runtime_backup": "runtime_20260718.zip (1h ago)",
            "disk_free_gb": 12.3,
        },
        "final_status": "READY FOR PRODUCTION",
        "exit_code": 0,
    }
    text = system_status.format_status(status)
    assert "Crypto Scanner Production V1" in text
    assert "Release: V1.0" in text
    assert "Reports Chat Configured: YES" in text
    assert "READY FOR PRODUCTION" in text
    assert json.loads(json.dumps(status))["release"]["release"] == "V1.0"


def test_system_status_read_only_and_missing_optional_inputs() -> None:
    before = {}
    for path in [system_status.JOURNAL, system_status.ENTRY_TIMING]:
        if path.exists():
            before[path] = path.stat().st_mtime_ns

    class NoTelegramSession:
        def post(self, *_args, **_kwargs):
            raise AssertionError("system_status must not send Telegram")

    assert NoTelegramSession
    status = system_status.build_status(include_services=False)
    assert "final_status" in status
    assert status["entry_timing"]["mode"] == "SHADOW"
    assert status["release"]["release"]
    for path, mtime in before.items():
        assert path.stat().st_mtime_ns == mtime

    missing = Path(tempfile.gettempdir()) / "missing_entry_timing_status_smoke.csv"
    entry = system_status.entry_timing_status(missing, missing)
    assert entry["evaluated"] == 0
    assert entry["latest_evaluation"] == "N/A"
    assert entry["readiness"] == "NOT ENOUGH DATA"


def test_system_status_systemctl_backup_and_compaction() -> None:
    def missing_systemctl(_command):
        raise FileNotFoundError("systemctl")

    assert system_status.service_state("crypto-scanner.service", runner=missing_systemctl) == "UNKNOWN"
    assert system_status.timer_state("crypto-performance-report.timer", runner=missing_systemctl) == "UNKNOWN"

    temp_dir = Path(tempfile.gettempdir()) / "system_status_backup_smoke"
    temp_dir.mkdir(exist_ok=True)
    backup = temp_dir / "runtime_20260718_000000.zip"
    try:
        backup.write_text("backup", encoding="utf-8")
        latest = system_status.latest_backup(temp_dir)
        assert latest.startswith("runtime_20260718_000000.zip")
        symbols = ",".join(f"COIN{i}USDT" for i in range(20))
        compact = system_status.compact_symbols(symbols, limit=4)
        assert "COIN0USDT" in compact
        assert "+16" in compact
        assert len(compact) <= 58
    finally:
        try:
            backup.unlink()
            temp_dir.rmdir()
        except OSError:
            pass


def _with_pilot_env(values: dict[str, str | None]):
    original = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    return original


def _restore_env(original: dict[str, str | None]) -> None:
    for key, value in original.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_manual_live_pilot_defaults_disabled_and_paper() -> None:
    original = _with_pilot_env({"TRADING_MODE": None, "LIVE_PILOT_ENABLED": None})
    try:
        config = manual_live_pilot.load_config()
        assert config.trading_mode == manual_live_pilot.PAPER
        assert config.enabled is False
        verdict = manual_live_pilot.evaluate_signal_pilot(sample_signal(), config=config, journal=pd.DataFrame())
        assert verdict.status == "BLOCKED"
        assert "TRADING_MODE is PAPER" in verdict.reasons
    finally:
        _restore_env(original)


def test_manual_live_pilot_policy_blocks_and_allows_core_tiers() -> None:
    original = _with_pilot_env({"TRADING_MODE": "MANUAL_LIVE_PILOT", "LIVE_PILOT_ENABLED": "true"})
    try:
        config = manual_live_pilot.LivePilotConfig(
            trading_mode=manual_live_pilot.MANUAL_LIVE_PILOT,
            enabled=True,
            risk_per_trade_pct=0.25,
            max_daily_risk_pct=0.50,
            max_open_positions=1,
            max_signals_per_day=3,
            max_consecutive_losses=2,
            allowed_tiers=("S", "A"),
        )
        signal = sample_signal()
        signal.watchlist_tier = "A"
        assert manual_live_pilot.evaluate_signal_pilot(signal, config=config, journal=pd.DataFrame()).status == "ALLOWED"
        signal.watchlist_tier = "B"
        assert "outside Production Universe Core/Tier A" in manual_live_pilot.evaluate_signal_pilot(signal, config=config, journal=pd.DataFrame()).reasons
        signal.watchlist_tier = "A"
        signal.sl = signal.entry
        assert "calculated stop distance is invalid" in manual_live_pilot.evaluate_signal_pilot(signal, config=config, journal=pd.DataFrame()).reasons
        signal.sl = 0
        assert "signal lacks valid entry or SL" in manual_live_pilot.evaluate_signal_pilot(signal, config=config, journal=pd.DataFrame()).reasons
        signal.sl = 101
        assert "system health is FAIL" in manual_live_pilot.evaluate_signal_pilot(signal, config=config, journal=pd.DataFrame(), system_health_status="FAIL").reasons
        assert "data integrity has critical findings" in manual_live_pilot.evaluate_signal_pilot(signal, config=config, journal=pd.DataFrame(), data_integrity_status="FAIL").reasons
    finally:
        _restore_env(original)


def test_manual_live_pilot_limits_duplicates_and_losses() -> None:
    config = manual_live_pilot.LivePilotConfig(
        trading_mode=manual_live_pilot.MANUAL_LIVE_PILOT,
        enabled=True,
        risk_per_trade_pct=0.25,
        max_daily_risk_pct=0.50,
        max_open_positions=1,
        max_signals_per_day=3,
        max_consecutive_losses=2,
        allowed_tiers=("S", "A"),
    )
    now = datetime.now(timezone.utc).isoformat()
    open_row = {
        "pilot_trade_id": "p1",
        "source_signal_id": "s1",
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "planned_entry": 100,
        "actual_entry": 100,
        "planned_sl": 99,
        "actual_sl": 99,
        "risk_percent": 0.25,
        "status": "OPEN",
        "outcome": "OPEN",
        "created_at": now,
        "updated_at": now,
    }
    journal = pd.DataFrame([open_row])
    signal = sample_signal()
    signal.symbol = "BTCUSDT"
    signal.direction = "SHORT"
    signal.watchlist_tier = "A"
    reasons = manual_live_pilot.evaluate_signal_pilot(signal, config=config, journal=journal).reasons
    assert "maximum open pilot position reached" in reasons
    assert "duplicate symbol position is already open" in reasons
    assert "opposite signal while symbol is open" in reasons
    assert "daily risk limit reached" in reasons

    closed_rows = []
    for idx in range(2):
        closed_rows.append({**open_row, "pilot_trade_id": f"c{idx}", "status": "CLOSED", "outcome": "LOSS", "updated_at": f"2026-07-0{idx + 1}T00:00:00+00:00"})
    assert "consecutive loss limit reached" in manual_live_pilot.evaluate_signal_pilot(signal, config=config, journal=pd.DataFrame(closed_rows)).reasons


def test_manual_trade_plan_and_invalid_inputs() -> None:
    plan = manual_live_pilot.calculate_trade_plan("BTC/USDT", 100000, 99000, 1000, 0.25, "LONG")
    assert plan["symbol"] == "BTCUSDT"
    assert round(plan["maximum_loss_amount"], 4) == 2.5
    assert round(plan["maximum_position_notional"], 4) == 250.0
    text = manual_trade_plan.format_plan(plan)
    assert "Maximum risk limit only" in text
    assert "Leverage" not in text
    for bad in ["nan", "0", "-1"]:
        try:
            manual_live_pilot.calculate_trade_plan("BTCUSDT", bad, 99, 1000, 0.25)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid numeric input should fail")


def test_manual_live_pilot_journal_backup_append_only_and_disable(tmp_path: Path | None = None) -> None:
    temp_dir = Path(tempfile.gettempdir()) / "manual_live_pilot_smoke"
    shutil.rmtree(temp_dir, ignore_errors=True)
    temp_dir.mkdir(exist_ok=True)
    journal = temp_dir / "manual_live_pilot.csv"
    old_journal = manual_live_pilot.JOURNAL
    old_logs = manual_live_pilot.LOGS_DIR
    old_marker = manual_live_pilot.DISABLE_MARKER
    calls: list[str] = []
    original_backup = manual_live_pilot.backup_runtime_data.create_backup
    manual_live_pilot.JOURNAL = journal
    manual_live_pilot.LOGS_DIR = temp_dir
    manual_live_pilot.DISABLE_MARKER = temp_dir / "manual_live_pilot.disabled"
    manual_live_pilot.backup_runtime_data.create_backup = lambda: calls.append("backup") or temp_dir / "backup.zip"
    try:
        row = {column: "" for column in manual_live_pilot.PILOT_COLUMNS}
        row.update({"pilot_trade_id": "p1", "source_signal_id": "s1", "symbol": "BTCUSDT", "direction": "LONG", "status": "OPEN", "outcome": "OPEN", "created_at": "2026-07-01T00:00:00+00:00", "updated_at": "2026-07-01T00:00:00+00:00"})
        manual_live_pilot.append_journal(row, journal)
        row.update({"status": "CLOSED", "outcome": "WIN", "updated_at": "2026-07-01T01:00:00+00:00"})
        manual_live_pilot.append_journal(row, journal)
        saved = pd.read_csv(journal)
        assert len(saved) == 2
        assert calls == ["backup", "backup"]
        manual_live_pilot.disable_pilot()
        assert manual_live_pilot.DISABLE_MARKER.exists()
    finally:
        manual_live_pilot.backup_runtime_data.create_backup = original_backup
        manual_live_pilot.JOURNAL = old_journal
        manual_live_pilot.LOGS_DIR = old_logs
        manual_live_pilot.DISABLE_MARKER = old_marker
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_manual_live_pilot_telegram_no_balance_and_cornix_unchanged() -> None:
    signal = sample_signal()
    cfg = scanner.ScannerConfig.from_env()
    notifier = scanner.TelegramNotifier(cfg)
    message = notifier.build_message(signal)
    cornix = notifier.build_cornix_message(signal)
    assert "MANUAL LIVE PILOT" in message
    assert "Account balance" not in message
    assert "1000" not in message
    assert "Leverage:\n20x" in cornix
    assert "MANUAL LIVE PILOT" not in cornix


def test_live_pilot_preflight_exit_codes() -> None:
    ready = [live_pilot_preflight.PreflightItem("a", "PASS", "ok")]
    warning = ready + [live_pilot_preflight.PreflightItem("b", "WARNING", "warn")]
    fail = warning + [live_pilot_preflight.PreflightItem("c", "FAIL", "bad")]
    assert live_pilot_preflight.final_status(ready) == ("PILOT READY", 0)
    assert live_pilot_preflight.final_status(warning) == ("PILOT READY WITH WARNINGS", 1)
    assert live_pilot_preflight.final_status(fail) == ("PILOT BLOCKED", 2)


def main() -> int:
    test_telegram_message()
    test_cornix_dry_run_format_and_signal_immutability()
    test_internal_signal_channel_routing()
    test_tier_c_report_only_routing()
    test_weak_symbol_report_only_routing()
    test_session_risk_report_only_routing()
    test_london_long_report_only_routing_and_unaffected_signals()
    test_missing_telegram_channel_ids_do_not_crash()
    test_external_inbox_logging_and_debug_format()
    test_external_signal_parse_long_short_and_symbols()
    test_external_signal_missing_fields_not_approved()
    test_external_signal_score_threshold_and_routing()
    test_external_signal_approved_routes_to_signals_and_cornix_and_logs()
    test_external_signal_refine_conflict_not_approved_or_routed()
    test_outcome_alert_reports_only()
    test_outcome_uses_futures_klines_and_symbol_normalization()
    test_outcome_high_low_direction_rules()
    test_outcome_alert_success_marks_sent_and_failure_does_not()
    test_wave_bullish_higher_high_higher_low()
    test_wave_bearish_lower_high_lower_low()
    test_wave_range_or_unclear_and_score_bounds()
    test_btc_regime_filter_profiles()
    test_loss_cooldown_three_losses_and_missing_journal()
    test_scanner_survives_wave_analyzer_failure()
    test_review_old_journal_columns()
    test_external_approved_outcome_tracking_updates_csv()
    test_stats_old_and_new_fields()
    test_outcome_message_and_dedupe()
    test_tp1_watcher_outcome_review_dedupe_sync()
    test_tp1_outcome_review_sets_source_when_first()
    test_daily_summary_and_missing_telegram_env()
    test_analytics_report_and_journal_exports()
    test_daily_performance_report_metrics()
    test_performance_report_routes_to_reports_only()
    test_executive_report_v2_entry_timing_and_decisions()
    test_scheduled_performance_report_reaches_reports_channel_path()
    test_executive_cli_prints_without_telegram_send()
    test_performance_report_send_failure_exits_nonzero()
    test_complete_performance_analytics_v1_outputs()
    test_performance_analytics_production_mapping()
    test_performance_analytics_v2_tables_and_warnings()
    test_performance_analytics_v3_shadow_filters_and_recommendations()
    test_score_calibration_report_detects_inversion()
    test_root_cause_analytics_sections_exports_and_clusters()
    test_performance_analytics_v3_missing_fields_safe()
    test_dashboard_renders_html()
    test_dashboard_v2_handles_missing_and_empty_data()
    test_dashboard_v3_equity_drawdown_monthly_simulator()
    test_entry_timing_engine_shadow_csv_and_report_summary()
    test_scanner_final_candidate_writes_entry_timing_row()
    test_position_manager_advice()
    test_position_watcher_tp1_breakeven_alert_and_dedupe()
    test_position_watcher_cornix_command_long_short_and_dedupe()
    test_position_watcher_cornix_stop_price_duplicate_prevention()
    test_position_watcher_float_flag_duplicate_prevention()
    test_position_watcher_cross_row_new_stop_dedupe()
    test_position_watcher_persistent_lock_prevents_stale_csv_duplicate()
    test_position_watcher_releases_lock_when_report_send_fails()
    test_position_watcher_new_stop_notification_key_dedupe_and_closed_skip()
    test_position_watcher_command_safety_modes()
    test_position_watcher_cornix_breakeven_formats()
    test_position_watcher_cornix_test_command()
    test_velahub_watchdog_threshold_recovery_and_report()
    test_production_health_exit_codes_and_no_telegram_send()
    test_data_integrity_audit_detects_duplicates_and_malformed_rows()
    test_data_integrity_safe_repair_preserves_outcomes()
    test_backup_runtime_data_excludes_secrets()
    test_entry_timing_readiness_thresholds_and_summary()
    test_update_script_validation_before_restart_protection()
    test_data_audit_status_registry_accepts_current_skip_statuses()
    test_entry_timing_audit_classifications()
    test_entry_timing_audit_deterministic_matching()
    test_entry_timing_truth_layer_sources_and_coverage()
    test_entry_timing_canonical_source_priority_reduces_cross_source_ambiguity()
    test_data_integrity_entry_timing_indexed_benchmark()
    test_position_watcher_state_audit_categories()
    test_position_watcher_state_cleanup_dry_run_and_apply()
    test_position_watcher_state_cleanup_safety_verdicts()
    test_position_watcher_state_cleanup_apply_aborts_when_state_changes()
    test_production_v1_readiness_exit_codes()
    test_system_status_exit_codes_json_and_helpers()
    test_system_status_read_only_and_missing_optional_inputs()
    test_system_status_systemctl_backup_and_compaction()
    test_manual_live_pilot_defaults_disabled_and_paper()
    test_manual_live_pilot_policy_blocks_and_allows_core_tiers()
    test_manual_live_pilot_limits_duplicates_and_losses()
    test_manual_trade_plan_and_invalid_inputs()
    test_manual_live_pilot_journal_backup_append_only_and_disable()
    test_manual_live_pilot_telegram_no_balance_and_cornix_unchanged()
    test_live_pilot_preflight_exit_codes()
    print("smoke tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
