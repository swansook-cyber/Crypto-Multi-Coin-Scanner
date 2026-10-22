"""Scanner notification-only routing; every transport is mocked."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd
import pytest

import cornix_agent as scanner
from smoke_test import sample_signal


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("LIVE_PILOT_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "shared-test-token")
    monkeypatch.setenv("LIVE_ENTRY_TELEGRAM_BOT_TOKEN", "shared-test-token")
    monkeypatch.setenv("LIVE_ENTRY_CORNIX_CHAT_ID", "cornix")
    cfg = scanner.ScannerConfig.from_env()
    cfg.dry_run = False
    cfg.send_telegram = True
    cfg.send_daily_summary = True
    cfg.telegram_signals_chat_id = "signals"
    cfg.telegram_reports_chat_id = "reports"
    cfg.session_report_only_sessions = ["NewYork", "London+NewYork"]
    return cfg, scanner.TelegramNotifier(cfg)


@pytest.mark.parametrize("session", ["NewYork", "London+NewYork"])
@pytest.mark.parametrize("via_general_sender", [False, True])
@pytest.mark.parametrize("with_chart", [False, True])
def test_blocked_manual_review_has_no_live_capability(configured, tmp_path, session, via_general_sender, with_chart):
    cfg, notifier = configured
    signal = sample_signal()
    signal.market_session = session
    if with_chart:
        signal.chart_path = tmp_path / "chart.png"
        signal.chart_path.write_bytes(b"mock-photo")
        signal.ai_commentary = "Long analysis " * 150
    original = deepcopy(signal)
    notifier.session = Mock()
    notifier.session.post.return_value.status_code = 200
    with patch.object(scanner, "LiveCommandGateway") as gateway, patch.object(scanner, "EntryRequest") as entry, patch.object(scanner, "canonical_entry_id") as action_id:
        send = notifier.send_signal if via_general_sender else notifier.send_session_risk_report_signal
        assert send(signal)
        gateway.assert_not_called()
        entry.assert_not_called()
        action_id.assert_not_called()
    calls = notifier.session.post.call_args_list
    assert calls
    assert all(call.kwargs["data"]["chat_id"] == "signals" for call in calls)
    assert not any(call.kwargs["data"]["chat_id"] in {"cornix", "reports"} for call in calls)
    full = [call.kwargs["data"]["text"] for call in calls if "text" in call.kwargs["data"]]
    assert len(full) == 1
    assert full[0].startswith("⚠️ MANUAL REVIEW — SESSION/RISK BLOCKED")
    assert "Sent to Signals for manual review." in full[0]
    assert "NOT SENT TO CORNIX." in full[0]
    assert notifier.build_message(signal) in full[0]
    for detail in ["BTCUSDT", "SHORT", "Entry:", "SL:", "TP1:", "TP2:", "RR:", "Setup Strength:", "Score:", "Market:", "Tier:", "Session:", "BTC:", "Risk Mode:"]:
        assert detail in full[0]
    assert signal == original


@pytest.mark.parametrize("missing", ["token", "signals"])
def test_missing_notification_route_does_not_fallback(configured, missing):
    cfg, notifier = configured
    if missing == "token":
        cfg.telegram_bot_token = ""
    else:
        cfg.telegram_signals_chat_id = ""
    notifier.session = Mock()
    with patch.object(scanner, "LiveCommandGateway") as gateway:
        assert not notifier.send_session_risk_report_signal(sample_signal())
        gateway.assert_not_called()
    notifier.session.post.assert_not_called()


def test_approved_signal_keeps_existing_gateway_contract(configured):
    cfg, notifier = configured
    signal = sample_signal()
    original = deepcopy(signal)
    notifier.session = Mock()
    notifier.session.post.return_value.status_code = 200
    with patch.object(scanner, "LiveCommandGateway") as gateway:
        gateway.return_value.send_entry.return_value = "DELIVERY_RECEIPT"
        assert notifier.send_signal(signal)
        request = gateway.return_value.send_entry.call_args.args[0]
        assert request.approved and request.live_intent
        assert request.provenance == "SCANNER_PROSPECTIVE"
        assert request.payload == notifier.build_cornix_message(signal)
        gateway.return_value.send_entry.assert_called_once()
    assert signal == original
    assert [c.kwargs["data"]["chat_id"] for c in notifier.session.post.call_args_list] == ["signals"]


def test_other_reports_keep_their_destination(configured):
    _, notifier = configured
    notifier.session = Mock()
    notifier.session.post.return_value.status_code = 200
    with patch.object(scanner, "LiveCommandGateway") as gateway:
        assert notifier.send_daily_summary({"day": "2026-10-22"})
        assert notifier.send_position_message("Outcome / position review")
        assert notifier.send_tier_c_report_signal(sample_signal())
        assert notifier.send_weak_symbol_report_signal(sample_signal())
        assert notifier.send_london_long_report_signal(sample_signal())
        gateway.assert_not_called()
    assert len(notifier.session.post.call_args_list) == 5
    assert all(c.kwargs["data"]["chat_id"] == "reports" for c in notifier.session.post.call_args_list)


def test_scanner_process_preserves_blocked_journal_and_skips_live_entry(configured, tmp_path):
    cfg, notifier = configured
    cfg.use_daily_risk_guard = False
    cfg.use_losing_streak_protection = False
    runner = scanner.AgentRunner.__new__(scanner.AgentRunner)
    runner.config = cfg
    runner.notifier = notifier
    runner.journal = scanner.TradeJournalLogger(tmp_path / "signals.csv")
    runner.loss_cooldown = Mock()
    runner.loss_cooldown.status.return_value = SimpleNamespace(active=False)
    runner.get_btc_context = Mock(return_value=None)
    runner.apply_btc_regime_filter = Mock(return_value=False)
    runner.select_top_candidates = lambda candidates: candidates
    runner.is_in_cooldown = Mock(return_value=False)
    runner.ai_commentary = Mock()
    runner.ai_commentary.can_summarize.return_value = False
    runner.chart_exporter = Mock()
    runner.indicators = Mock()
    runner.data_client = Mock()
    runner.mark_sent = Mock()
    runner.evaluate_entry_timing_shadow = Mock()
    runner.evaluate_setup_strength_shadow = Mock()
    runner.record_research_telemetry = Mock()
    runner.research_same_scan_counts = {}
    runner.research_same_scan_sent_counts = {}
    notifier.session = Mock()
    notifier.session.post.return_value.status_code = 200
    signal = sample_signal()
    signal.market_session = "NewYork"
    original = deepcopy(signal)
    with patch.object(scanner, "evaluate_new_signal", return_value=SimpleNamespace(should_send_signal=True)), patch.object(scanner, "LiveCommandGateway") as gateway, patch.object(scanner, "EntryRequest") as entry:
        runner.process_candidates([signal])
        gateway.assert_not_called()
        entry.assert_not_called()
    rows = pd.read_csv(runner.journal.path)
    assert rows["signal_status"].tolist() == ["session_risk_report_only"]
    assert rows["skip_reason"].tolist() == ["session_risk_experimental_report_only"]
    assert runner.research_same_scan_sent_counts == {}
    runner.record_research_telemetry.assert_called_once_with(signal, "session_risk_report_only", "session_risk_experimental_report_only")
    runner.evaluate_entry_timing_shadow.assert_called_once_with(signal, "session_risk_report_only")
    # Existing mark_sent records cooldown only, not a journal SENT or Cornix receipt.
    runner.mark_sent.assert_called_once_with(signal)
    assert signal == original
    assert all(c.kwargs["data"]["chat_id"] == "signals" for c in notifier.session.post.call_args_list)
