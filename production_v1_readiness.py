# -*- coding: utf-8 -*-
"""Production V1 readiness summary.

This command is read-only. It does not send Telegram and does not repair data.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

import backup_runtime_data
import data_integrity_audit
import entry_timing_operational_summary
import performance_report
import position_watcher_state_cleanup
import production_health


BASE_DIR = Path(__file__).resolve().parent
BACKUPS_DIR = BASE_DIR / "backups"
ENTRY_TIMING = BASE_DIR / "logs" / "entry_timing_engine.csv"
JOURNAL = BASE_DIR / "logs" / "signals.csv"


@dataclass
class ReadinessItem:
    name: str
    status: str
    detail: str


def _status_from_health(checks: list[production_health.HealthCheck]) -> str:
    if any(check.status == production_health.FAIL for check in checks):
        return "FAIL"
    if any(check.status == production_health.WARNING for check in checks):
        return "WARNING"
    return "PASS"


def _load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def build_readiness(include_services: bool = True) -> list[ReadinessItem]:
    items: list[ReadinessItem] = []
    health_checks = production_health.run_checks(include_services=include_services)
    health_status = _status_from_health(health_checks)
    items.append(ReadinessItem("health", health_status, f"{len(health_checks)} checks"))

    service_checks = [check for check in health_checks if ".service" in check.name or ".timer" in check.name]
    service_status = _status_from_health(service_checks) if service_checks else "WARNING"
    items.append(ReadinessItem("services", service_status, f"{len(service_checks)} service/timer checks"))
    timer_checks = [check for check in service_checks if "crypto-performance-report.timer" in check.name]
    timer_status = _status_from_health(timer_checks) if timer_checks else "WARNING"
    items.append(ReadinessItem("timer", timer_status, f"{len(timer_checks)} performance timer checks"))

    audit_findings = data_integrity_audit.audit_paths(JOURNAL, ENTRY_TIMING)
    audit_status = "FAIL" if any(item.severity == "FAIL" for item in audit_findings) else "WARNING" if any(item.severity == "WARNING" for item in audit_findings) else "PASS"
    items.append(ReadinessItem("data integrity", audit_status, f"{len(audit_findings)} findings"))

    backups = sorted(BACKUPS_DIR.glob("runtime_*.zip")) if BACKUPS_DIR.exists() else []
    items.append(ReadinessItem("backup availability", "PASS" if backups else "WARNING", backups[-1].name if backups else "no runtime backup zip found"))

    try:
        journal = performance_report.load_csv_safely(performance_report.JOURNAL)
        history = performance_report.load_csv_safely(performance_report.HISTORY)
        external = performance_report.load_csv_safely(performance_report.EXTERNAL)
        entry = performance_report.load_csv_safely(performance_report.ENTRY_TIMING)
        report, _tables = performance_report.build_full_report(journal, history, external, None)
        executive = performance_report.format_executive_report(report, entry)
        items.append(ReadinessItem("Executive Report", "PASS", f"{len(executive)} chars"))
    except Exception as exc:
        items.append(ReadinessItem("Executive Report", "FAIL", f"{type(exc).__name__}: {exc}"))

    html_ok = performance_report.FULL_WEB_REPORT.exists() and (performance_report.REPORTS_DIR / "analytics.html").exists()
    items.append(ReadinessItem("Full Analytics HTML", "PASS" if html_ok else "WARNING", "report.html and analytics.html present" if html_ok else "missing report.html or analytics.html"))

    entry_df = _load_csv(ENTRY_TIMING)
    journal_df = _load_csv(JOURNAL)
    linked = entry_timing_operational_summary.linked_closed_outcomes(entry_df, journal_df)
    readiness = entry_timing_operational_summary.readiness_status(linked)
    items.append(ReadinessItem("Entry Timing data status", "PASS" if readiness == "REVIEW READY" else "WARNING", f"{readiness}; linked_closed={linked}"))

    stale_items = position_watcher_state_cleanup.stale_state_items(JOURNAL)
    items.append(ReadinessItem("active stale state count", "PASS" if not stale_items else "WARNING", str(len(stale_items))))
    lock_check = [check for check in health_checks if check.name == "duplicate-alert lock directory"]
    items.append(ReadinessItem("duplicate-alert protection", lock_check[0].status if lock_check else "WARNING", lock_check[0].detail if lock_check else "not checked"))
    return items


def final_status(items: list[ReadinessItem]) -> tuple[str, int]:
    if any(item.status == "FAIL" for item in items):
        return "NOT READY", 2
    if any(item.status == "WARNING" for item in items):
        return "READY WITH WARNINGS", 1
    return "READY FOR V1", 0


def print_readiness(items: list[ReadinessItem]) -> None:
    status, _code = final_status(items)
    print("Crypto Scanner Production V1 Readiness")
    print("")
    for item in items:
        print(f"{item.status:7} | {item.name} | {item.detail}")
    print("")
    print(f"Final status: {status}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Print read-only Production V1 readiness summary.")
    parser.add_argument("--no-services", action="store_true", help="Skip systemd checks.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    items = build_readiness(include_services=not args.no_services)
    print_readiness(items)
    return final_status(items)[1]


if __name__ == "__main__":
    raise SystemExit(main())
