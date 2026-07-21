# -*- coding: utf-8 -*-
"""Manual Live Pilot preflight checks.

Read-only. Does not send Telegram and does not place or modify orders.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

import data_integrity_audit
import manual_live_pilot
import position_watcher_state_cleanup
import system_status


BASE_DIR = Path(__file__).resolve().parent
JOURNAL = BASE_DIR / "logs" / "signals.csv"
PILOT_JOURNAL = manual_live_pilot.JOURNAL


@dataclass
class PreflightItem:
    name: str
    status: str
    detail: str


def _service(name: str) -> str:
    try:
        result = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True, timeout=5, check=False)
        return "PASS" if result.stdout.strip() == "active" else "WARNING"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return "WARNING"


def _timer(name: str) -> str:
    return _service(name)


def build_preflight() -> list[PreflightItem]:
    load_dotenv(BASE_DIR / ".env")
    config = manual_live_pilot.load_config()
    items: list[PreflightItem] = []
    items.append(PreflightItem("TRADING_MODE", "PASS" if config.trading_mode == manual_live_pilot.MANUAL_LIVE_PILOT else "FAIL", config.trading_mode))
    items.append(PreflightItem("LIVE_PILOT_ENABLED", "PASS" if config.enabled else "FAIL", str(config.enabled)))
    items.append(PreflightItem("disable marker", "FAIL" if manual_live_pilot.DISABLE_MARKER.exists() else "PASS", "present" if manual_live_pilot.DISABLE_MARKER.exists() else "clear"))

    items.append(PreflightItem("scanner service", _service("crypto-scanner.service"), "crypto-scanner.service"))
    items.append(PreflightItem("position watcher", _service("crypto-position-watcher.service"), "crypto-position-watcher.service"))
    items.append(PreflightItem("performance timer", _timer("crypto-performance-report.timer"), "crypto-performance-report.timer"))

    telegram_ok = bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip() and os.getenv("TELEGRAM_REPORTS_CHAT_ID", "").strip())
    items.append(PreflightItem("Telegram reports config", "PASS" if telegram_ok else "FAIL", "configured" if telegram_ok else "missing token/reports chat"))
    backup = system_status.latest_backup()
    items.append(PreflightItem("latest backup", "PASS" if backup != "N/A" else "WARNING", backup))

    findings = data_integrity_audit.audit_paths(data_integrity_audit.JOURNAL, data_integrity_audit.ENTRY_TIMING)
    critical = [item for item in findings if item.severity == "FAIL"]
    items.append(PreflightItem("data integrity critical findings", "FAIL" if critical else "PASS", f"{len(critical)} critical findings"))

    watcher = position_watcher_state_cleanup.classify_cleanup(JOURNAL)
    stale = watcher.safe_to_remove + watcher.blocked_unsafe_path + watcher.blocked_identity_ambiguous
    items.append(PreflightItem("stale watcher state", "WARNING" if stale else "PASS", f"stale_or_blocked={stale}"))

    pilot_df = manual_live_pilot.load_journal(PILOT_JOURNAL)
    open_count = len(manual_live_pilot.open_positions(pilot_df))
    daily_risk = manual_live_pilot.daily_risk_used_pct(pilot_df)
    losses = manual_live_pilot.consecutive_losses(pilot_df)
    items.append(PreflightItem("open pilot positions", "FAIL" if open_count >= config.max_open_positions else "PASS", f"{open_count}/{config.max_open_positions}"))
    items.append(PreflightItem("daily pilot risk usage", "FAIL" if daily_risk >= config.max_daily_risk_pct else "PASS", f"{daily_risk:.2f}/{config.max_daily_risk_pct:.2f}%"))
    items.append(PreflightItem("consecutive pilot losses", "FAIL" if losses >= config.max_consecutive_losses else "PASS", f"{losses}/{config.max_consecutive_losses}"))

    universe = pd.DataFrame()
    if JOURNAL.exists():
        try:
            universe = pd.read_csv(JOURNAL)
        except Exception:
            universe = pd.DataFrame()
    tier_ok = "watchlist_tier" in universe.columns and universe["watchlist_tier"].fillna("").astype(str).str.upper().isin(["A", "S"]).any()
    items.append(PreflightItem("Production Universe availability", "PASS" if tier_ok or universe.empty else "WARNING", "Tier S/A available" if tier_ok else "not enough local data"))

    try:
        manual_live_pilot.LOGS_DIR.mkdir(exist_ok=True)
        test_path = manual_live_pilot.LOGS_DIR / ".pilot_write_test"
        test_path.write_text("ok", encoding="utf-8")
        test_path.unlink()
        writable = True
    except OSError as exc:
        writable = False
        items.append(PreflightItem("journal writable", "FAIL", str(exc)))
    if writable:
        items.append(PreflightItem("journal writable", "PASS", str(PILOT_JOURNAL)))

    now = datetime.now(timezone.utc)
    items.append(PreflightItem("UTC clock", "PASS", now.isoformat(timespec="seconds")))
    items.append(PreflightItem("Entry Timing Shadow", "WARNING", "shadow only; does not block pilot"))
    return items


def final_status(items: list[PreflightItem]) -> tuple[str, int]:
    if any(item.status == "FAIL" for item in items):
        return "PILOT BLOCKED", 2
    if any(item.status == "WARNING" for item in items):
        return "PILOT READY WITH WARNINGS", 1
    return "PILOT READY", 0


def print_preflight(items: list[PreflightItem]) -> None:
    print("Manual Live Pilot Preflight")
    print("")
    for item in items:
        print(f"{item.status:7} | {item.name} | {item.detail}")
    print("")
    print(f"Final result: {final_status(items)[0]}")


def parse_args() -> argparse.Namespace:
    return argparse.ArgumentParser(description="Manual live pilot preflight. Read-only.").parse_args()


def main() -> int:
    parse_args()
    items = build_preflight()
    print_preflight(items)
    return final_status(items)[1]


if __name__ == "__main__":
    raise SystemExit(main())
