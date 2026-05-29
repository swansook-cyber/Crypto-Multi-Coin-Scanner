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
import daily_summary
import review_signals
import stats_dashboard
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
            "result": "WIN",
            "hit_target": "TP1",
            "market_session": "London",
            "score_bucket": "A+",
        },
        {
            "timestamp": "2026-05-28T03:00:00+00:00",
            "closed_at": "2026-05-28T04:00:00+00:00",
            "symbol": "ETHUSDT",
            "result": "LOSS",
            "hit_target": "SL",
            "market_session": "London",
            "score_bucket": "B",
        },
        {
            "timestamp": "2026-05-28T05:00:00+00:00",
            "symbol": "SOLUSDT",
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
    assert summary["current_streak"] == "1 LOSS"
    message = daily_summary.build_telegram_message(summary)
    assert "📊 Daily Signal Summary" in message
    assert "Today's Winrate: 50.0%" in message
    assert "Best Session: London" in message
    assert "Best Bucket: A+" in message
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


def main() -> int:
    test_telegram_message()
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
    print("smoke tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
