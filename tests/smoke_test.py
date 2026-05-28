# -*- coding: utf-8 -*-
"""Local smoke tests for scanner output/report compatibility."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cornix_agent as scanner
import review_signals
import stats_dashboard


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
    assert "สัญญาณนี้ใช้เพื่อเก็บสถิติ/ทดสอบระบบเท่านั้น" in message
    assert "No auto-trade" in message


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
        for column in ["setup_strength", "raw_score", "score_bucket", "htf_conflict", "market_session"]:
            assert column in df.columns
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


def main() -> int:
    test_telegram_message()
    test_review_old_journal_columns()
    test_stats_old_and_new_fields()
    print("smoke tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
