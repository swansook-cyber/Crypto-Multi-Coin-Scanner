"""Fail-closed, durable boundary for live position-control commands.

Only this module knows the dedicated control Telegram route. Journal rows and
price ticks are never exposure evidence. Production has no exposure provider
until an independently authenticated ownership check is deployed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Callable

import requests


DENIED_PROVENANCE = frozenset({
    "RESEARCH", "AUDIT", "RECONSTRUCTION", "RECOVERY", "BACKFILL",
    "SHADOW", "HISTORICAL_REPLAY", "REPORT_ONLY",
})
MAX_EXPOSURE_AGE_SECONDS = 60
ALLOWED_ACTIONS = frozenset({"STOP_UPDATE"})


@dataclass(frozen=True)
class ControlRequest:
    canonical_signal_id: str
    trade_id: str
    order_id: str
    symbol: str
    side: str
    quantity: str
    target_price: str
    action: str
    action_version: int
    provenance: str
    live_intent: bool
    payload: str

    @property
    def action_id(self) -> str:
        target = _positive(self.target_price)
        identity = [self.canonical_signal_id, self.trade_id, self.action,
                    format(target.normalize(), "f") if target is not None else self.target_price,
                    self.action_version]
        return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class ExposureEvidence:
    observed_at: datetime
    source: str
    provenance: str
    canonical_signal_id: str
    trade_id: str
    order_id: str
    symbol: str
    side: str
    open_quantity: str
    ownership_verified: bool


@dataclass(frozen=True)
class EntryRequest:
    canonical_signal_id: str
    symbol: str
    side: str
    provenance: str
    approved: bool
    live_intent: bool
    payload: str

    @property
    def action_id(self) -> str:
        return hashlib.sha256(f"ENTRY|{self.canonical_signal_id}|{self.symbol}|{self.side}".encode()).hexdigest()


def canonical_entry_id(*parts: object) -> str:
    return "sig:" + hashlib.sha256(json.dumps([str(part) for part in parts], separators=(",", ":")).encode()).hexdigest()


def _positive(value: str) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number > 0 else None


def authorize(request: ControlRequest, evidence: ExposureEvidence | None,
              now: datetime | None = None) -> str:
    """Return a denial reason or AUTHORIZED; no caller can authorize by CSV state."""
    now = now or datetime.now(timezone.utc)
    provenance = request.provenance.strip().upper()
    if provenance in DENIED_PROVENANCE or provenance != "TRUSTED_CURRENT_LIVE":
        return "UNTRUSTED_PROVENANCE"
    if not request.live_intent or request.action not in ALLOWED_ACTIONS:
        return "NO_EXPLICIT_LIVE_INTENT_OR_ACTION"
    if not re.fullmatch(r"sig:[0-9a-f]{64}", request.canonical_signal_id):
        return "INVALID_CANONICAL_SIGNAL_ID"
    if not request.trade_id or not request.order_id or request.action_version < 1:
        return "MISSING_AUTHORITATIVE_LINKAGE"
    if not re.fullmatch(r"[A-Z0-9]{3,24}", request.symbol) or request.side not in {"LONG", "SHORT"}:
        return "INVALID_SYMBOL_OR_SIDE"
    quantity, target = _positive(request.quantity), _positive(request.target_price)
    if quantity is None or target is None or not request.payload.strip():
        return "INVALID_QUANTITY_TARGET_OR_PAYLOAD"
    if evidence is None:
        return "NO_CURRENT_OWNED_EXPOSURE"
    if not evidence.ownership_verified or evidence.source != "AUTHORITATIVE_LIVE_EXPOSURE":
        return "NO_CURRENT_OWNED_EXPOSURE"
    if evidence.provenance != "TRUSTED_CURRENT_LIVE":
        return "UNTRUSTED_EXPOSURE_PROVENANCE"
    if evidence.observed_at.tzinfo is None:
        return "EXPIRED_EXPOSURE_LEASE"
    age = (now - evidence.observed_at).total_seconds()
    if age < 0 or age > MAX_EXPOSURE_AGE_SECONDS:
        return "EXPIRED_EXPOSURE_LEASE"
    if evidence.canonical_signal_id != request.canonical_signal_id or evidence.trade_id != request.trade_id or evidence.order_id != request.order_id:
        return "LINKAGE_MISMATCH"
    if evidence.symbol != request.symbol:
        return "SYMBOL_MISMATCH"
    if evidence.side != request.side:
        return "SIDE_MISMATCH"
    open_quantity = _positive(evidence.open_quantity)
    if open_quantity is None:
        return "NO_CURRENT_OWNED_EXPOSURE"
    if open_quantity != quantity:
        return "QUANTITY_MISMATCH"
    return "AUTHORIZED"


class LiveCommandGateway:
    def __init__(self, audit_path: Path, *, session: requests.Session | None = None,
                 exposure_provider: Callable[[ControlRequest], ExposureEvidence | None] | None = None,
                 token: str | None = None, chat_id: str | None = None):
        self.audit_path = Path(audit_path)
        self.session = session or requests.Session()
        self.exposure_provider = exposure_provider
        self._token = token if token is not None else os.getenv("LIVE_CONTROL_TELEGRAM_BOT_TOKEN", "")
        self._chat_id = chat_id if chat_id is not None else os.getenv("LIVE_CONTROL_CORNIX_CHAT_ID", "")
        self._entry_token = os.getenv("LIVE_ENTRY_TELEGRAM_BOT_TOKEN", "")
        self._entry_chat_id = os.getenv("LIVE_ENTRY_CORNIX_CHAT_ID", "")
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS commands (
                action_id TEXT PRIMARY KEY, state TEXT NOT NULL, request_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '{}',
                receipt_id TEXT NOT NULL DEFAULT '', updated_utc TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, action_id TEXT NOT NULL, state TEXT NOT NULL,
                utc TEXT NOT NULL, detail TEXT NOT NULL)""")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.audit_path, timeout=30)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def _state(self, db, request: ControlRequest | EntryRequest, state: str, detail: str,
               evidence: ExposureEvidence | None = None, receipt_id: str = "") -> None:
        utc = datetime.now(timezone.utc).isoformat()
        request_data = {k: v for k, v in vars(request).items() if k != "payload"}
        request_data["sender_process"] = os.getpid()
        evidence_data = ({**vars(evidence), "observed_at": evidence.observed_at.isoformat()}
                         if evidence else {})
        db.execute("""INSERT INTO commands VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(action_id) DO UPDATE SET state=excluded.state,
            evidence_json=excluded.evidence_json, receipt_id=excluded.receipt_id,
            updated_utc=excluded.updated_utc""",
                   (request.action_id, state, json.dumps(request_data, sort_keys=True),
                    hashlib.sha256(request.payload.encode()).hexdigest(),
                    json.dumps(evidence_data, sort_keys=True), receipt_id, utc))
        db.execute("INSERT INTO events(action_id,state,utc,detail) VALUES (?,?,?,?)",
                   (request.action_id, state, utc, detail))

    def send_position_control(self, request: ControlRequest) -> str:
        """One attempt maximum per action ID, including across restarts."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT state FROM commands WHERE action_id=?", (request.action_id,)).fetchone()
            if prior and prior[0] in {"SEND_ATTEMPTED", "DELIVERY_RECEIPT", "ACKNOWLEDGED", "RECONCILIATION_REQUIRED", "RECONCILED"}:
                return "RECONCILIATION_REQUIRED" if prior[0] in {"SEND_ATTEMPTED", "RECONCILIATION_REQUIRED"} else "DUPLICATE_ACTION_ID"
            self._state(db, request, "REQUESTED", "position control requested")
            for old_id, old_state, old_json in list(db.execute(
                    "SELECT action_id,state,request_json FROM commands WHERE state IN "
                    "('SEND_ATTEMPTED','RECONCILIATION_REQUIRED','DELIVERY_RECEIPT')")):
                old = json.loads(old_json)
                if (old_id != request.action_id and
                        old.get("canonical_signal_id") == request.canonical_signal_id and
                        old.get("trade_id") == request.trade_id and
                        old.get("action") == request.action):
                    self._state(db, request, "DENIED", "PREVIOUS_COMMAND_RECONCILIATION_REQUIRED")
                    return "RECONCILIATION_REQUIRED"
            try:
                evidence = self.exposure_provider(request) if self.exposure_provider else None
            except Exception:
                evidence = None
            decision = authorize(request, evidence)
            if decision != "AUTHORIZED":
                self._state(db, request, "DENIED", decision, evidence)
                return decision
            if not self._token or not self._chat_id:
                self._state(db, request, "DENIED", "MISSING_DEDICATED_CONTROL_ROUTE", evidence)
                return "MISSING_DEDICATED_CONTROL_ROUTE"
            if self._token == os.getenv("TELEGRAM_BOT_TOKEN", ""):
                self._state(db, request, "DENIED", "SHARED_LIVE_CREDENTIAL", evidence)
                return "SHARED_LIVE_CREDENTIAL"
            self._state(db, request, "AUTHORIZED", "all authorization checks passed", evidence)
            self._state(db, request, "SEND_ATTEMPTED", "durable pre-send record", evidence)
        try:
            response = self.session.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                data={"chat_id": self._chat_id, "text": request.payload}, timeout=20)
            body = response.json()
            if response.status_code != 200 or not isinstance(body, dict) or body.get("ok") is not True:
                raise ValueError("delivery not confirmed")
            receipt = str(body.get("result", {}).get("message_id", ""))
            if not receipt:
                raise ValueError("Telegram receipt missing")
        except Exception:
            with self._connect() as db:
                self._state(db, request, "RECONCILIATION_REQUIRED", "uncertain delivery; no automatic retry", evidence)
            return "RECONCILIATION_REQUIRED"
        with self._connect() as db:
            self._state(db, request, "DELIVERY_RECEIPT", "Telegram delivered; Cornix acknowledgement unknown", evidence, receipt)
        return "DELIVERY_RECEIPT"

    def send_entry(self, request: EntryRequest) -> str:
        """Route approved prospective entry signals through a distinct credential."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT state FROM commands WHERE action_id=?", (request.action_id,)).fetchone()
            if prior and prior[0] in {"SEND_ATTEMPTED", "DELIVERY_RECEIPT", "RECONCILIATION_REQUIRED", "ACKNOWLEDGED", "RECONCILED"}:
                return "DUPLICATE_ACTION_ID"
            self._state(db, request, "REQUESTED", "entry notification requested")
            valid = (request.provenance in {"SCANNER_PROSPECTIVE", "EXTERNAL_APPROVED"}
                     and request.approved and request.live_intent
                     and re.fullmatch(r"sig:[0-9a-f]{64}", request.canonical_signal_id)
                     and re.fullmatch(r"[A-Z0-9]{3,24}", request.symbol)
                     and request.side in {"LONG", "SHORT"} and bool(request.payload.strip()))
            if not valid:
                self._state(db, request, "DENIED", "INVALID_ENTRY_AUTHORIZATION")
                return "INVALID_ENTRY_AUTHORIZATION"
            if not self._entry_token or not self._entry_chat_id:
                self._state(db, request, "DENIED", "MISSING_DEDICATED_ENTRY_ROUTE")
                return "MISSING_DEDICATED_ENTRY_ROUTE"
            if self._entry_token == os.getenv("TELEGRAM_BOT_TOKEN", ""):
                self._state(db, request, "DENIED", "SHARED_LIVE_CREDENTIAL")
                return "SHARED_LIVE_CREDENTIAL"
            self._state(db, request, "AUTHORIZED", "prospective entry authorized")
            self._state(db, request, "SEND_ATTEMPTED", "durable pre-send record")
        try:
            response = self.session.post(
                f"https://api.telegram.org/bot{self._entry_token}/sendMessage",
                data={"chat_id": self._entry_chat_id, "text": request.payload}, timeout=20)
            body = response.json()
            if response.status_code != 200 or not isinstance(body, dict) or body.get("ok") is not True:
                raise ValueError("delivery not confirmed")
            receipt = str(body.get("result", {}).get("message_id", ""))
            if not receipt:
                raise ValueError("Telegram receipt missing")
        except Exception:
            with self._connect() as db:
                self._state(db, request, "RECONCILIATION_REQUIRED", "uncertain delivery; no automatic retry")
            return "RECONCILIATION_REQUIRED"
        with self._connect() as db:
            self._state(db, request, "DELIVERY_RECEIPT", "Telegram delivered; Cornix acknowledgement unknown", receipt_id=receipt)
        return "DELIVERY_RECEIPT"

    def record_external_resolution(self, request: ControlRequest, *, acknowledged: bool,
                                   evidence_reference: str) -> str:
        """Record an operator-provided Cornix reconciliation without resending."""
        if not evidence_reference.strip():
            return "MISSING_RECONCILIATION_EVIDENCE"
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT state FROM commands WHERE action_id=?", (request.action_id,)).fetchone()
            if not prior or prior[0] not in {"DELIVERY_RECEIPT", "RECONCILIATION_REQUIRED", "SEND_ATTEMPTED"}:
                return "INVALID_RECONCILIATION_STATE"
            state = "ACKNOWLEDGED" if acknowledged else "RECONCILED"
            self._state(db, request, state, "external evidence reference: " + evidence_reference)
            return state
