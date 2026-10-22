"""Offline capability tests. Every HTTP client here is a fake."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pandas as pd
import pytest

from live_command_gateway import (ControlRequest, EntryRequest, ExposureEvidence,
                                  LiveCommandGateway, authorize, canonical_entry_id)
import position_watcher


NOW = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)
SUI_ID = canonical_entry_id("2026-10-17T01:00:33Z", "SUIUSDT", "SHORT", "1.0075", "0.99064")


def request(**changes):
    base = ControlRequest(SUI_ID, "trade-1", "order-1", "SUIUSDT", "SHORT", "10",
                          "1.0075", "STOP_UPDATE", 1, "TRUSTED_CURRENT_LIVE", True,
                          "SHORT SUIUSDT\n\nNEW STOP:\n1.0075")
    return replace(base, **changes)


def exposure(**changes):
    base = ExposureEvidence(NOW, "AUTHORITATIVE_LIVE_EXPOSURE", "TRUSTED_CURRENT_LIVE",
                            SUI_ID, "trade-1", "order-1", "SUIUSDT", "SHORT", "10", True)
    return replace(base, **changes)


@pytest.mark.parametrize("provenance", ["RESEARCH", "AUDIT", "RECONSTRUCTION", "RECOVERY",
                                          "BACKFILL", "SHADOW", "HISTORICAL_REPLAY", "REPORT_ONLY"])
def test_nonlive_provenance_denied(provenance):
    assert authorize(request(provenance=provenance), exposure(), NOW) == "UNTRUSTED_PROVENANCE"


@pytest.mark.parametrize("change,reason", [
    ({"symbol": "BTCUSDT"}, "SYMBOL_MISMATCH"),
    ({"side": "LONG"}, "SIDE_MISMATCH"),
    ({"quantity": "11"}, "QUANTITY_MISMATCH"),
    ({"trade_id": "wrong"}, "LINKAGE_MISMATCH"),
    ({"live_intent": False}, "NO_EXPLICIT_LIVE_INTENT_OR_ACTION"),
    ({"canonical_signal_id": "journal-open"}, "INVALID_CANONICAL_SIGNAL_ID"),
])
def test_authorization_mismatches_denied(change, reason):
    assert authorize(request(**change), exposure(), NOW) == reason


def test_fresh_owned_exposure_required():
    assert authorize(request(), None, NOW) == "NO_CURRENT_OWNED_EXPOSURE"
    assert authorize(request(), exposure(open_quantity="0"), NOW) == "NO_CURRENT_OWNED_EXPOSURE"
    assert authorize(request(), exposure(ownership_verified=False), NOW) == "NO_CURRENT_OWNED_EXPOSURE"
    assert authorize(request(), exposure(observed_at=NOW - timedelta(seconds=61)), NOW) == "EXPIRED_EXPOSURE_LEASE"
    assert authorize(request(), exposure(), NOW) == "AUTHORIZED"


def test_action_identity_normalizes_price_and_changes_with_version():
    assert request().action_id == request(target_price="1.007500").action_id
    assert request().action_id != request(action_version=2).action_id


class FakeResponse:
    status_code = 200

    def json(self):
        return {"ok": True, "result": {"message_id": 2158}}


class FakeSession:
    def __init__(self, uncertain=False):
        self.posts = []
        self.uncertain = uncertain

    def post(self, url, data, timeout):
        self.posts.append((url, data))
        if self.uncertain:
            raise TimeoutError("response lost")
        return FakeResponse()


def test_durable_idempotency_receipt_is_not_cornix_ack(tmp_path):
    session = FakeSession()
    path = tmp_path / "audit.sqlite3"
    gateway = LiveCommandGateway(path, session=session, exposure_provider=lambda _: exposure(observed_at=datetime.now(timezone.utc)),
                                 token="test-token", chat_id="test-chat")
    assert gateway.send_position_control(request()) == "DELIVERY_RECEIPT"
    assert len(session.posts) == 1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT state,receipt_id FROM commands").fetchone() == ("DELIVERY_RECEIPT", "2158")
        assert [row[0] for row in db.execute("SELECT state FROM events")] == [
            "REQUESTED", "AUTHORIZED", "SEND_ATTEMPTED", "DELIVERY_RECEIPT"]
    restarted = LiveCommandGateway(path, session=session, exposure_provider=lambda _: exposure(observed_at=datetime.now(timezone.utc)),
                                   token="test-token", chat_id="test-chat")
    assert restarted.send_position_control(request()) == "DUPLICATE_ACTION_ID"
    assert len(session.posts) == 1


def test_uncertain_send_requires_reconciliation_and_never_retries(tmp_path):
    session = FakeSession(uncertain=True)
    gateway = LiveCommandGateway(tmp_path / "audit.sqlite3", session=session,
                                 exposure_provider=lambda _: exposure(observed_at=datetime.now(timezone.utc)),
                                 token="test-token", chat_id="test-chat")
    assert gateway.send_position_control(request()) == "RECONCILIATION_REQUIRED"
    assert gateway.send_position_control(request()) == "RECONCILIATION_REQUIRED"
    assert gateway.send_position_control(request(action_version=2)) == "RECONCILIATION_REQUIRED"
    assert len(session.posts) == 1
    assert gateway.record_external_resolution(request(), acknowledged=False, evidence_reference="operator-case-1") == "RECONCILED"
    assert gateway.send_position_control(request()) == "DUPLICATE_ACTION_ID"


def test_sui_incident_stale_open_flat_sends_zero_commands(tmp_path):
    class PriceSession(FakeSession):
        def get(self, url, params, timeout):
            class PriceResponse:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"price": "0.99000"}
            return PriceResponse()

    path = tmp_path / "signals.csv"
    pd.DataFrame([{"timestamp": "2026-10-17T01:00:33Z", "symbol": "SUIUSDT",
                   "side": "SHORT", "entry": 1.0075, "tp1": 0.99064,
                   "result": "OPEN", "signal_status": "sent",
                   "canonical_signal_key": SUI_ID, "binance_trade_id": "trade-1",
                   "binance_order_id": "order-1", "current_remainder_qty": "10",
                   "live_action_provenance": "TRUSTED_CURRENT_LIVE",
                   "explicit_live_intent": "true"}]).to_csv(path, index=False)
    session = PriceSession()
    config = position_watcher.WatcherConfig(True, 60, True, True, "report-token", "reports",
                                             "cornix", "cornix_command", False, False)
    stats = position_watcher.process_once(path, session, config)
    assert stats.cornix_commands_sent == 0
    assert not any(data["chat_id"] == "cornix" for _, data in session.posts)
    with sqlite3.connect(tmp_path / "live_command_audit.sqlite3") as db:
        assert db.execute("SELECT state FROM commands").fetchone()[0] == "DENIED"
        assert db.execute("SELECT detail FROM events WHERE state='DENIED'").fetchone()[0] == "NO_CURRENT_OWNED_EXPOSURE"


def test_research_journal_mutation_never_grants_live_command(tmp_path):
    gateway = LiveCommandGateway(tmp_path / "audit.sqlite3", session=FakeSession(),
                                 exposure_provider=None, token="test-token", chat_id="test-chat")
    for provenance in ("RESEARCH", "RECONSTRUCTION", "RECOVERY"):
        candidate = request(action_version=len(provenance), provenance=provenance)
        assert gateway.send_position_control(candidate) == "UNTRUSTED_PROVENANCE"
    assert gateway.session.posts == []


def test_intended_entry_notification_and_no_binance_write(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_ENTRY_TELEGRAM_BOT_TOKEN", "test-entry-token")
    monkeypatch.setenv("LIVE_ENTRY_CORNIX_CHAT_ID", "test-entry-chat")
    session = FakeSession()
    gateway = LiveCommandGateway(tmp_path / "audit.sqlite3", session=session)
    entry = EntryRequest(SUI_ID, "SUIUSDT", "SHORT", "SCANNER_PROSPECTIVE", True, True, "SHORT SUIUSDT\nEntry: 1.0075")
    assert gateway.send_entry(entry) == "DELIVERY_RECEIPT"
    assert len(session.posts) == 1
    assert all("binance.com" not in url for url, _ in session.posts)


def test_shared_notification_credential_cannot_send_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "shared-token")
    monkeypatch.setenv("LIVE_ENTRY_TELEGRAM_BOT_TOKEN", "shared-token")
    monkeypatch.setenv("LIVE_ENTRY_CORNIX_CHAT_ID", "test-entry-chat")
    session = FakeSession()
    gateway = LiveCommandGateway(tmp_path / "audit.sqlite3", session=session)
    entry = EntryRequest(SUI_ID, "SUIUSDT", "SHORT", "SCANNER_PROSPECTIVE", True, True, "SHORT SUIUSDT")
    assert gateway.send_entry(entry) == "SHARED_LIVE_CREDENTIAL"
    assert session.posts == []


def test_watcher_and_research_have_no_direct_control_sender():
    root = Path(__file__).resolve().parents[1]
    watcher = (root / "position_watcher.py").read_text(encoding="utf-8")
    assert "api.telegram.org" not in watcher
    assert "LIVE_CONTROL_TELEGRAM_BOT_TOKEN" not in watcher
    for source in (root / "research").rglob("*.py"):
        text = source.read_text(encoding="utf-8")
        assert "live_command_gateway" not in text
        assert "LIVE_CONTROL_TELEGRAM_BOT_TOKEN" not in text
