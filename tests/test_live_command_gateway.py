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


@pytest.fixture(autouse=True)
def isolated_routes(monkeypatch):
    for key in ("LIVE_ENTRY_TELEGRAM_BOT_TOKEN", "LIVE_ENTRY_CORNIX_CHAT_ID",
                "LIVE_CONTROL_TELEGRAM_BOT_TOKEN", "LIVE_CONTROL_CORNIX_CHAT_ID",
                "TELEGRAM_CORNIX_CHAT_ID", "TELEGRAM_CHAT_ID", "TELEGRAM_EXTERNAL_INBOX_CHAT_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "notification-token")
    monkeypatch.setenv("TELEGRAM_SIGNALS_CHAT_ID", "signals")
    monkeypatch.setenv("TELEGRAM_REPORTS_CHAT_ID", "reports")


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
    gateway = LiveCommandGateway(tmp_path / "audit.sqlite3", session=session, entry_role="crypto-scanner")
    entry = EntryRequest(SUI_ID, "SUIUSDT", "SHORT", "SCANNER_PROSPECTIVE", True, True, "SHORT SUIUSDT\nEntry: 1.0075")
    assert gateway.send_entry(entry) == "DELIVERY_RECEIPT"
    assert len(session.posts) == 1
    assert all("binance.com" not in url for url, _ in session.posts)


@pytest.mark.parametrize("role,provenance", [("crypto-scanner", "SCANNER_PROSPECTIVE"),
                                            ("crypto-external-inbox", "EXTERNAL_APPROVED")])
def test_shared_notification_credential_can_send_entry(tmp_path, monkeypatch, role, provenance):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "shared-token")
    monkeypatch.setenv("LIVE_ENTRY_TELEGRAM_BOT_TOKEN", "shared-token")
    monkeypatch.setenv("LIVE_ENTRY_CORNIX_CHAT_ID", "test-entry-chat")
    session = FakeSession()
    monkeypatch.setenv("TELEGRAM_CORNIX_CHAT_ID", "test-entry-chat")
    gateway = LiveCommandGateway(tmp_path / "audit.sqlite3", session=session, entry_role=role)
    entry = EntryRequest(SUI_ID, "SUIUSDT", "SHORT", provenance, True, True, "SHORT SUIUSDT\n\nEntry:\n1.0-1.01\n\nTargets:\n0.99\n0.98\n\nStop:\n1.02\n\nLeverage:\n20x")
    assert gateway.send_entry(entry) == "DELIVERY_RECEIPT"
    assert session.posts == [("https://api.telegram.org/botshared-token/sendMessage",
                              {"chat_id": "test-entry-chat", "text": entry.payload})]
    assert gateway.send_entry(entry) == "DUPLICATE_ACTION_ID"
    assert len(session.posts) == 1


def shared_entry_gateway(tmp_path, monkeypatch, *, role="crypto-scanner", uncertain=False):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "shared-token")
    monkeypatch.setenv("LIVE_ENTRY_TELEGRAM_BOT_TOKEN", "shared-token")
    monkeypatch.setenv("LIVE_ENTRY_CORNIX_CHAT_ID", "cornix")
    monkeypatch.setenv("TELEGRAM_CORNIX_CHAT_ID", "cornix")
    session = FakeSession(uncertain=uncertain)
    gateway = LiveCommandGateway(tmp_path / "audit.sqlite3", session=session, entry_role=role)
    entry = EntryRequest(SUI_ID, "SUIUSDT", "SHORT", "SCANNER_PROSPECTIVE", True, True, "SHORT SUIUSDT")
    return gateway, entry, session


@pytest.mark.parametrize("destination", ["", " ", "signals", "reports", "wrong-cornix"])
def test_shared_entry_requires_correct_explicit_destination(tmp_path, monkeypatch, destination):
    _, entry, session = shared_entry_gateway(tmp_path, monkeypatch)
    monkeypatch.setenv("LIVE_ENTRY_CORNIX_CHAT_ID", destination)
    gateway = LiveCommandGateway(tmp_path / "audit.sqlite3", session=session, entry_role="crypto-scanner")
    assert gateway.send_entry(entry) in {"MISSING_DEDICATED_ENTRY_ROUTE", "INVALID_ENTRY_DESTINATION"}
    assert session.posts == []


@pytest.mark.parametrize("changes", [{"provenance": "RESEARCH"}, {"provenance": "SHADOW"},
    {"provenance": "REPORT_ONLY"}, {"canonical_signal_id": "bad"}, {"approved": False},
    {"live_intent": False}, {"side": "BOTH"}, {"symbol": "invalid/symbol"}, {"payload": ""}])
def test_shared_entry_does_not_weaken_authorization(tmp_path, monkeypatch, changes):
    gateway, entry, session = shared_entry_gateway(tmp_path, monkeypatch)
    assert gateway.send_entry(replace(entry, **changes)) == "INVALID_ENTRY_AUTHORIZATION"
    assert session.posts == []


@pytest.mark.parametrize("role", ["", "crypto-position-watcher", "crypto-outcome-checker", "research", "shadow", "crypto-external-inbox"])
def test_entry_role_is_explicit_and_matches_provenance(tmp_path, monkeypatch, role):
    gateway, entry, session = shared_entry_gateway(tmp_path, monkeypatch, role=role)
    assert gateway.send_entry(entry) == "INVALID_ENTRY_SERVICE_ROLE"
    assert session.posts == []


def test_shared_entry_uncertainty_survives_restart_without_resend(tmp_path, monkeypatch):
    gateway, entry, session = shared_entry_gateway(tmp_path, monkeypatch, uncertain=True)
    assert gateway.send_entry(entry) == "RECONCILIATION_REQUIRED"
    restarted = LiveCommandGateway(gateway.audit_path, session=session, entry_role="crypto-scanner")
    assert restarted.send_entry(entry) == "RECONCILIATION_REQUIRED"
    assert len(session.posts) == 1


@pytest.mark.parametrize("control_route", ["absent", "shared"])
def test_shared_entry_token_never_enables_position_control(tmp_path, monkeypatch, control_route):
    _, _, session = shared_entry_gateway(tmp_path, monkeypatch)
    if control_route == "shared":
        monkeypatch.setenv("LIVE_CONTROL_TELEGRAM_BOT_TOKEN", "shared-token")
        monkeypatch.setenv("LIVE_CONTROL_CORNIX_CHAT_ID", "cornix")
    gateway = LiveCommandGateway(tmp_path / "audit.sqlite3", session=session,
        exposure_provider=lambda _: exposure(observed_at=datetime.now(timezone.utc)))
    expected = "SHARED_LIVE_CREDENTIAL" if control_route == "shared" else "MISSING_DEDICATED_CONTROL_ROUTE"
    assert gateway.send_position_control(request()) == expected
    assert session.posts == []


def test_only_supported_entry_callers_are_scanner_and_external_analyzer():
    import ast
    root = Path(__file__).resolve().parents[1]
    callers = set()
    # Application sources only; historical backup scripts and tests are not service entrypoints.
    for source in [*root.glob("*.py"), *root.joinpath("core").rglob("*.py"), *root.joinpath("research").rglob("*.py")]:
        if source.name.startswith("test_") or ".backup_" in source.name:
            continue
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        if any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr == "send_entry" for node in ast.walk(tree)):
            callers.add(source.name)
    assert callers == {"cornix_agent.py", "external_signal_analyzer.py"}


def test_watcher_and_research_have_no_direct_control_sender():
    root = Path(__file__).resolve().parents[1]
    watcher = (root / "position_watcher.py").read_text(encoding="utf-8")
    assert "api.telegram.org" not in watcher
    assert "LIVE_CONTROL_TELEGRAM_BOT_TOKEN" not in watcher
    for source in (root / "research").rglob("*.py"):
        text = source.read_text(encoding="utf-8")
        assert "live_command_gateway" not in text
        assert "LIVE_CONTROL_TELEGRAM_BOT_TOKEN" not in text
