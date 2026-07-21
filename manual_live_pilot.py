# -*- coding: utf-8 -*-
"""Manual Live Pilot controls and journal.

This module is advisory only. It never connects to an exchange and never places,
modifies, or closes orders.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

import backup_runtime_data


BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
JOURNAL = LOGS_DIR / "manual_live_pilot.csv"
DISABLE_MARKER = LOGS_DIR / "manual_live_pilot.disabled"

PAPER = "PAPER"
MANUAL_LIVE_PILOT = "MANUAL_LIVE_PILOT"
VALID_MODES = {PAPER, MANUAL_LIVE_PILOT}

PILOT_COLUMNS = [
    "pilot_trade_id",
    "source_signal_id",
    "symbol",
    "direction",
    "signal_timestamp",
    "manual_entry_timestamp",
    "planned_entry",
    "actual_entry",
    "planned_sl",
    "actual_sl",
    "planned_tp1",
    "risk_percent",
    "risk_amount",
    "status",
    "outcome",
    "realized_r",
    "notes",
    "created_at",
    "updated_at",
]

TERMINAL_STATUSES = {"CLOSED", "CANCELLED"}
OPEN_STATUSES = {"OPEN"}


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_mode(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text if text in VALID_MODES else PAPER


def trading_mode() -> str:
    load_dotenv(BASE_DIR / ".env")
    return normalize_mode(os.getenv("TRADING_MODE", PAPER))


@dataclass(frozen=True)
class LivePilotConfig:
    trading_mode: str = PAPER
    enabled: bool = False
    risk_per_trade_pct: float = 0.25
    max_daily_risk_pct: float = 0.50
    max_open_positions: int = 1
    max_signals_per_day: int = 3
    max_consecutive_losses: int = 2
    allowed_tiers: tuple[str, ...] = ("S", "A")


@dataclass
class PilotVerdict:
    status: str
    risk_budget_pct: float
    open_positions: int
    max_open_positions: int
    daily_risk_used_pct: float
    reasons: list[str]

    @property
    def allowed(self) -> bool:
        return self.status == "ALLOWED"

    def reason_text(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "Pilot policy checks passed"


def load_config() -> LivePilotConfig:
    load_dotenv(BASE_DIR / ".env")
    allowed = tuple(
        item.strip().upper()
        for item in os.getenv("LIVE_PILOT_ALLOWED_TIERS", "S,A").split(",")
        if item.strip()
    ) or ("S", "A")
    return LivePilotConfig(
        trading_mode=trading_mode(),
        enabled=_env_bool("LIVE_PILOT_ENABLED", False),
        risk_per_trade_pct=_env_float("LIVE_PILOT_RISK_PER_TRADE_PCT", 0.25),
        max_daily_risk_pct=_env_float("LIVE_PILOT_MAX_DAILY_RISK_PCT", 0.50),
        max_open_positions=_env_int("LIVE_PILOT_MAX_OPEN_POSITIONS", 1),
        max_signals_per_day=_env_int("LIVE_PILOT_MAX_SIGNALS_PER_DAY", 3),
        max_consecutive_losses=_env_int("LIVE_PILOT_MAX_CONSECUTIVE_LOSSES", 2),
        allowed_tiers=allowed,
    )


def pilot_disabled(config: LivePilotConfig | None = None) -> bool:
    config = config or load_config()
    return config.trading_mode != MANUAL_LIVE_PILOT or not config.enabled or DISABLE_MARKER.exists()


def _safe_float(value: Any, name: str, *, positive: bool = True) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be numeric")
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    if positive and number <= 0:
        raise ValueError(f"{name} must be positive")
    if abs(number) > 1_000_000_000:
        raise ValueError(f"{name} is outside safe bounds")
    return number


def calculate_trade_plan(
    symbol: str,
    entry: Any,
    stop: Any,
    account_balance: Any,
    risk_percent: Any,
    direction: str = "",
) -> dict[str, Any]:
    clean_symbol = str(symbol or "").strip().upper().replace("/", "").replace(".P", "")
    if not clean_symbol:
        raise ValueError("symbol is required")
    entry_value = _safe_float(entry, "entry")
    stop_value = _safe_float(stop, "stop")
    balance_value = _safe_float(account_balance, "account_balance")
    risk_value = _safe_float(risk_percent, "risk_percent")
    if risk_value > 10:
        raise ValueError("risk_percent is outside safe bounds")
    stop_distance = abs(entry_value - stop_value)
    if stop_distance <= 0:
        raise ValueError("stop distance must be greater than zero")
    max_loss = balance_value * risk_value / 100.0
    quantity = max_loss / stop_distance
    max_notional = quantity * entry_value
    return {
        "symbol": clean_symbol,
        "direction": str(direction or "").strip().upper(),
        "account_balance": balance_value,
        "risk_percent": risk_value,
        "maximum_loss_amount": max_loss,
        "entry": entry_value,
        "stop": stop_value,
        "stop_distance": stop_distance,
        "maximum_position_notional": max_notional,
        "pilot_policy_result": "RISK_LIMIT_ONLY",
        "blocking_reasons": [],
    }


def load_journal(path: Path = JOURNAL) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=PILOT_COLUMNS)
    try:
        df = pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=PILOT_COLUMNS)
    for column in PILOT_COLUMNS:
        if column not in df.columns:
            df[column] = ""
    return df[PILOT_COLUMNS]


def latest_trade_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    sortable = df.copy()
    sortable["updated_at"] = pd.to_datetime(sortable["updated_at"], errors="coerce", utc=True)
    sortable = sortable.sort_values("updated_at")
    return sortable.groupby("pilot_trade_id", as_index=False).tail(1)


def open_positions(df: pd.DataFrame | None = None) -> pd.DataFrame:
    latest = latest_trade_rows(load_journal() if df is None else df)
    if latest.empty:
        return latest
    return latest[latest["status"].fillna("").astype(str).str.upper().isin(OPEN_STATUSES)].copy()


def daily_risk_used_pct(df: pd.DataFrame | None = None, date: datetime | None = None) -> float:
    latest = latest_trade_rows(load_journal() if df is None else df)
    if latest.empty:
        return 0.0
    date = date or datetime.now(timezone.utc)
    created = pd.to_datetime(latest["created_at"], errors="coerce", utc=True)
    today = created.dt.date == date.date()
    opened = latest["status"].fillna("").astype(str).str.upper().isin({"OPEN", "CLOSED"})
    risk = pd.to_numeric(latest.loc[today & opened, "risk_percent"], errors="coerce").fillna(0)
    return float(risk.sum())


def todays_open_count(df: pd.DataFrame | None = None, date: datetime | None = None) -> int:
    latest = latest_trade_rows(load_journal() if df is None else df)
    if latest.empty:
        return 0
    date = date or datetime.now(timezone.utc)
    created = pd.to_datetime(latest["created_at"], errors="coerce", utc=True)
    return int((created.dt.date == date.date()).sum())


def consecutive_losses(df: pd.DataFrame | None = None) -> int:
    latest = latest_trade_rows(load_journal() if df is None else df)
    if latest.empty:
        return 0
    closed = latest[latest["status"].fillna("").astype(str).str.upper().isin(TERMINAL_STATUSES)].copy()
    if closed.empty:
        return 0
    closed["updated_at"] = pd.to_datetime(closed["updated_at"], errors="coerce", utc=True)
    closed = closed.sort_values("updated_at")
    count = 0
    for outcome in reversed(closed["outcome"].fillna("").astype(str).str.upper().tolist()):
        if outcome != "LOSS":
            break
        count += 1
    return count


def _signal_value(signal: Any, name: str, default: Any = "") -> Any:
    if isinstance(signal, dict):
        return signal.get(name, default)
    return getattr(signal, name, default)


def evaluate_signal_pilot(
    signal: Any,
    signal_status: str = "sent",
    config: LivePilotConfig | None = None,
    journal: pd.DataFrame | None = None,
    system_health_status: str = "PASS",
    data_integrity_status: str = "PASS",
) -> PilotVerdict:
    config = config or load_config()
    df = load_journal() if journal is None else journal
    reasons: list[str] = []
    open_df = open_positions(df)
    used = daily_risk_used_pct(df)

    if config.trading_mode != MANUAL_LIVE_PILOT:
        reasons.append("TRADING_MODE is PAPER")
    if not config.enabled:
        reasons.append("LIVE_PILOT_ENABLED is false")
    if DISABLE_MARKER.exists():
        reasons.append("PILOT DISABLED")
    if str(signal_status or "").lower() != "sent":
        reasons.append("signal is not production-routed")
    if str(system_health_status).upper() == "FAIL":
        reasons.append("system health is FAIL")
    if str(data_integrity_status).upper() == "FAIL":
        reasons.append("data integrity has critical findings")

    tier = str(_signal_value(signal, "watchlist_tier", _signal_value(signal, "tier", ""))).upper()
    if tier not in config.allowed_tiers:
        reasons.append("outside Production Universe Core/Tier A")
    symbol = str(_signal_value(signal, "symbol", "")).upper()
    direction = str(_signal_value(signal, "direction", _signal_value(signal, "side", ""))).upper()
    if not open_df.empty:
        if len(open_df) >= config.max_open_positions:
            reasons.append("maximum open pilot position reached")
        same_symbol = open_df[open_df["symbol"].fillna("").astype(str).str.upper() == symbol]
        if not same_symbol.empty:
            reasons.append("duplicate symbol position is already open")
            if not (same_symbol["direction"].fillna("").astype(str).str.upper() == direction).all():
                reasons.append("opposite signal while symbol is open")
    if used + config.risk_per_trade_pct >= config.max_daily_risk_pct:
        reasons.append("daily risk limit reached")
    if todays_open_count(df) >= config.max_signals_per_day:
        reasons.append("maximum pilot signals per day reached")
    if consecutive_losses(df) >= config.max_consecutive_losses:
        reasons.append("consecutive loss limit reached")
    try:
        entry = _safe_float(_signal_value(signal, "entry", ""), "entry")
        stop = _safe_float(_signal_value(signal, "sl", _signal_value(signal, "stop_loss", "")), "stop")
        if abs(entry - stop) <= 0:
            reasons.append("calculated stop distance is invalid")
    except ValueError:
        reasons.append("signal lacks valid entry or SL")
    return PilotVerdict(
        status="BLOCKED" if reasons else "ALLOWED",
        risk_budget_pct=config.risk_per_trade_pct,
        open_positions=int(len(open_df)),
        max_open_positions=config.max_open_positions,
        daily_risk_used_pct=used,
        reasons=reasons,
    )


def format_pilot_telegram_section(verdict: PilotVerdict) -> str:
    disabled = "PILOT DISABLED" if any("PILOT DISABLED" in reason for reason in verdict.reasons) else ""
    lines = [
        "MANUAL LIVE PILOT",
        f"Status: {verdict.status}",
        f"Risk budget: {verdict.risk_budget_pct:.2f}%",
        f"Open pilot positions: {verdict.open_positions} / {verdict.max_open_positions}",
        f"Daily risk used: {verdict.daily_risk_used_pct:.2f}%",
        f"Reason: {disabled or verdict.reason_text()}",
    ]
    return "\n".join(lines)


def append_journal(row: dict[str, Any], path: Path = JOURNAL) -> None:
    LOGS_DIR.mkdir(exist_ok=True)
    backup_runtime_data.create_backup()
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PILOT_COLUMNS)
        if not exists or path.stat().st_size == 0:
            writer.writeheader()
        writer.writerow({column: row.get(column, "") for column in PILOT_COLUMNS})


def open_trade(args: argparse.Namespace) -> int:
    df = load_journal()
    config = load_config()
    source_id = str(args.source_signal_id).strip()
    if not source_id:
        print("source_signal_id is required")
        return 2
    latest = latest_trade_rows(df)
    if not latest.empty:
        duplicate = latest[
            (latest["source_signal_id"].fillna("").astype(str) == source_id)
            & latest["status"].fillna("").astype(str).str.upper().isin(OPEN_STATUSES)
        ]
        if not duplicate.empty:
            print("Pilot open blocked: duplicate open pilot trade for source signal")
            return 2
    signal = {
        "symbol": args.symbol,
        "direction": args.direction,
        "entry": args.planned_entry,
        "sl": args.planned_sl,
        "watchlist_tier": args.tier,
    }
    verdict = evaluate_signal_pilot(signal, config=config, journal=df)
    if not verdict.allowed:
        print(f"Pilot open blocked: {verdict.reason_text()}")
        return 2
    now = utc_now()
    row = {
        "pilot_trade_id": str(uuid.uuid4()),
        "source_signal_id": source_id,
        "symbol": str(args.symbol).upper(),
        "direction": str(args.direction).upper(),
        "signal_timestamp": args.signal_timestamp or now,
        "manual_entry_timestamp": args.manual_entry_timestamp or now,
        "planned_entry": args.planned_entry,
        "actual_entry": args.actual_entry or args.planned_entry,
        "planned_sl": args.planned_sl,
        "actual_sl": args.actual_sl or args.planned_sl,
        "planned_tp1": args.planned_tp1,
        "risk_percent": config.risk_per_trade_pct,
        "risk_amount": args.risk_amount or "",
        "status": "OPEN",
        "outcome": "OPEN",
        "realized_r": "",
        "notes": args.notes or "",
        "created_at": now,
        "updated_at": now,
    }
    append_journal(row)
    print(f"Pilot trade opened: {row['pilot_trade_id']}")
    return 0


def close_trade(args: argparse.Namespace) -> int:
    df = load_journal()
    latest = latest_trade_rows(df)
    trade_id = str(args.pilot_trade_id).strip()
    match = latest[latest["pilot_trade_id"].fillna("").astype(str) == trade_id] if not latest.empty else pd.DataFrame()
    if match.empty:
        print("Pilot close blocked: trade id not found")
        return 2
    row = match.iloc[-1].to_dict()
    if str(row.get("status", "")).upper() not in OPEN_STATUSES:
        print("Pilot close blocked: trade is not open")
        return 2
    now = utc_now()
    row.update(
        {
            "status": "CLOSED",
            "outcome": str(args.outcome).upper(),
            "realized_r": args.realized_r,
            "notes": args.notes or row.get("notes", ""),
            "updated_at": now,
        }
    )
    append_journal(row)
    print(f"Pilot trade closed: {trade_id}")
    return 0


def print_open() -> int:
    open_df = open_positions()
    if open_df.empty:
        print("No open manual pilot trades")
        return 0
    for _, row in open_df.iterrows():
        print(f"{row['pilot_trade_id']} | {row['symbol']} {row['direction']} | entry={row['actual_entry']} sl={row['actual_sl']}")
    return 0


def status_text() -> str:
    config = load_config()
    df = load_journal()
    verdict = "DISABLED" if pilot_disabled(config) else "ENABLED"
    return (
        "Manual Live Pilot Status\n"
        f"- Trading mode: {config.trading_mode}\n"
        f"- Pilot enabled: {config.enabled}\n"
        f"- Runtime marker: {'DISABLED' if DISABLE_MARKER.exists() else 'CLEAR'}\n"
        f"- Effective status: {verdict}\n"
        f"- Risk per trade: {config.risk_per_trade_pct:.2f}%\n"
        f"- Max daily risk: {config.max_daily_risk_pct:.2f}%\n"
        f"- Open positions: {len(open_positions(df))} / {config.max_open_positions}\n"
        f"- Daily risk used: {daily_risk_used_pct(df):.2f}%\n"
        f"- Consecutive losses: {consecutive_losses(df)}"
    )


def disable_pilot() -> int:
    LOGS_DIR.mkdir(exist_ok=True)
    DISABLE_MARKER.write_text(f"disabled_at={utc_now()}\n", encoding="utf-8")
    print("Manual live pilot disabled")
    return 0


def daily_summary() -> int:
    df = load_journal()
    print("Manual Live Pilot Daily Summary")
    print(f"- Open positions: {len(open_positions(df))}")
    print(f"- Daily risk used: {daily_risk_used_pct(df):.2f}%")
    print(f"- Consecutive losses: {consecutive_losses(df)}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manual live pilot journal. No exchange orders are placed.")
    sub = parser.add_subparsers(dest="command", required=True)
    open_p = sub.add_parser("open")
    open_p.add_argument("--source-signal-id", required=True)
    open_p.add_argument("--symbol", required=True)
    open_p.add_argument("--direction", required=True, choices=["LONG", "SHORT"])
    open_p.add_argument("--tier", default="A")
    open_p.add_argument("--signal-timestamp", default="")
    open_p.add_argument("--manual-entry-timestamp", default="")
    open_p.add_argument("--planned-entry", required=True)
    open_p.add_argument("--actual-entry", default="")
    open_p.add_argument("--planned-sl", required=True)
    open_p.add_argument("--actual-sl", default="")
    open_p.add_argument("--planned-tp1", default="")
    open_p.add_argument("--risk-amount", default="")
    open_p.add_argument("--notes", default="")
    close_p = sub.add_parser("close")
    close_p.add_argument("--pilot-trade-id", required=True)
    close_p.add_argument("--outcome", required=True, choices=["WIN", "LOSS", "BREAKEVEN", "CANCELLED"])
    close_p.add_argument("--realized-r", default="")
    close_p.add_argument("--notes", default="")
    sub.add_parser("list-open")
    sub.add_parser("daily-summary")
    sub.add_parser("disable")
    sub.add_parser("status")
    return parser.parse_args()


def main() -> int:
    load_dotenv(BASE_DIR / ".env")
    args = parse_args()
    if args.command == "open":
        return open_trade(args)
    if args.command == "close":
        return close_trade(args)
    if args.command == "list-open":
        return print_open()
    if args.command == "daily-summary":
        return daily_summary()
    if args.command == "disable":
        return disable_pilot()
    if args.command == "status":
        print(status_text())
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
