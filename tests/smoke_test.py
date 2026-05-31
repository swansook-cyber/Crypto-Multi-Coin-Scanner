# -*- coding: utf-8 -*-
"""Local smoke tests for scanner output/report compatibility."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
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
from core import wave_structure_analyzer as wave


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
    os.environ["EXTERNAL_SIGNAL_SCORE_THRESHOLD"] = "99"
    calls: list[tuple[str, str]] = []
    original_send = external_signal_analyzer.send_telegram_message

    def fake_send(_token: str, chat_id: str, _message: str, channel_name: str) -> bool:
        calls.append((chat_id, channel_name))
        return True

    external_signal_analyzer.send_telegram_message = fake_send
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
        assert ("reports", "external reports") in calls
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


def test_external_signal_approved_routes_to_signals_and_cornix_and_logs() -> None:
    old_threshold = os.environ.get("EXTERNAL_SIGNAL_SCORE_THRESHOLD")
    os.environ["EXTERNAL_SIGNAL_SCORE_THRESHOLD"] = "70"
    calls: list[tuple[str, str, str]] = []
    original_send = external_signal_analyzer.send_telegram_message
    path = Path(tempfile.gettempdir()) / "external_approved_smoke.csv"

    def fake_send(_token: str, chat_id: str, message: str, channel_name: str) -> bool:
        calls.append((chat_id, channel_name, message))
        return True

    external_signal_analyzer.send_telegram_message = fake_send
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
        assert "DRY RUN - EXTERNAL SIGNAL CORNIX FORMAT" in [call[2] for call in calls if call[0] == "cornix"][0]
        logged = pd.read_csv(path)
        assert logged.loc[0, "recommendation"] == "APPROVED"
        assert logged.loc[0, "sent_to_signals"] == "YES"
        assert logged.loc[0, "sent_to_cornix"] == "YES"
    finally:
        external_signal_analyzer.send_telegram_message = original_send
        if old_threshold is None:
            os.environ.pop("EXTERNAL_SIGNAL_SCORE_THRESHOLD", None)
        else:
            os.environ["EXTERNAL_SIGNAL_SCORE_THRESHOLD"] = old_threshold
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
    assert report["tp1_hits"] == 1
    assert report["tp2_hits"] == 1
    assert report["sl_hits"] == 1
    assert report["net_r_estimate"] == 2.2
    assert report["small_sample_warning"] is True
    message = performance_report.format_report(report)
    assert "Daily Performance Report" in message
    assert "Sample size is still small. Use for monitoring only." in message
    assert "Long win rate:" in message
    assert "Short win rate:" in message


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
    finally:
        try:
            output.unlink()
        except OSError:
            pass


def test_position_manager_advice() -> None:
    now = pd.Timestamp("2026-05-30T12:00:00Z")
    path = Path(tempfile.gettempdir()) / "position_manager_smoke.csv"
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

        fresh = {"symbol": "SOLUSDT", "direction": "LONG", "entry": 100}
        advice = position_manager.evaluate_new_signal(fresh, path, now=now)
        assert advice.should_send_signal is True
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def main() -> int:
    test_telegram_message()
    test_cornix_dry_run_format_and_signal_immutability()
    test_missing_telegram_channel_ids_do_not_crash()
    test_external_inbox_logging_and_debug_format()
    test_external_signal_parse_long_short_and_symbols()
    test_external_signal_missing_fields_not_approved()
    test_external_signal_score_threshold_and_routing()
    test_external_signal_approved_routes_to_signals_and_cornix_and_logs()
    test_wave_bullish_higher_high_higher_low()
    test_wave_bearish_lower_high_lower_low()
    test_wave_range_or_unclear_and_score_bounds()
    test_btc_regime_filter_profiles()
    test_loss_cooldown_three_losses_and_missing_journal()
    test_scanner_survives_wave_analyzer_failure()
    test_review_old_journal_columns()
    test_stats_old_and_new_fields()
    test_outcome_message_and_dedupe()
    test_daily_summary_and_missing_telegram_env()
    test_analytics_report_and_journal_exports()
    test_daily_performance_report_metrics()
    test_dashboard_renders_html()
    test_position_manager_advice()
    print("smoke tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
