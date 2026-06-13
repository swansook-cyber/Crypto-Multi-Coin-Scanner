# -*- coding: utf-8 -*-
"""Local smoke tests for scanner output/report compatibility."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import importlib.util
import json
import tempfile
import sys
import os

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cornix_agent as scanner
import dashboard
import daily_summary
import external_signal_analyzer
import performance_report
import position_manager
import review_signals
import stats_dashboard
import telegram_external_inbox
from core.analytics_reporting import build_daily_performance_report, export_journal_csvs, journal_signal_export
from core.btc_regime_filter import detect_btc_regime
from core.loss_cooldown import LossCooldownTracker
from core.performance_analytics_v1 import build_complete_report, export_v1_outputs
from core.performance_analytics_v2 import build_performance_v2, canonical_session, generate_performance_warnings
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
    assert "🧪 DRY RUN - CORNIX FORMAT TEST" in message
    assert "DO NOT AUTO TRADE" in message
    assert "SHORT BTCUSDT" in message
    assert "Entry:" in message
    assert "Targets:" in message
    assert "Stop:" in message
    assert "Leverage:" in message
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
        assert "DRY RUN - EXTERNAL SIGNAL CORNIX FORMAT" in [call[2] for call in calls if call[0] == "cornix"][0]
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
            "tp2_alert_sent",
            "sl_alert_sent",
            "outcome_alert_sent_at",
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
        position = pd.read_csv(paths["position_management"])
        assert position.loc[0, "hold_count"] == 1
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


def main() -> int:
    test_telegram_message()
    test_cornix_dry_run_format_and_signal_immutability()
    test_internal_signal_channel_routing()
    test_missing_telegram_channel_ids_do_not_crash()
    test_external_inbox_logging_and_debug_format()
    test_external_signal_parse_long_short_and_symbols()
    test_external_signal_missing_fields_not_approved()
    test_external_signal_score_threshold_and_routing()
    test_external_signal_approved_routes_to_signals_and_cornix_and_logs()
    test_external_signal_refine_conflict_not_approved_or_routed()
    test_outcome_alert_reports_only()
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
    test_daily_summary_and_missing_telegram_env()
    test_analytics_report_and_journal_exports()
    test_daily_performance_report_metrics()
    test_performance_report_routes_to_reports_only()
    test_complete_performance_analytics_v1_outputs()
    test_performance_analytics_production_mapping()
    test_performance_analytics_v2_tables_and_warnings()
    test_dashboard_renders_html()
    test_dashboard_v2_handles_missing_and_empty_data()
    test_position_manager_advice()
    test_velahub_watchdog_threshold_recovery_and_report()
    print("smoke tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
