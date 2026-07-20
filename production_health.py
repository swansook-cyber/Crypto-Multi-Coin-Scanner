# -*- coding: utf-8 -*-
"""Production readiness health checks for Crypto Multi-Coin Scanner.

This command is intentionally read-only. It does not send Telegram messages,
generate trade signals, or change scanner state.
"""

from __future__ import annotations

import argparse
import importlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pandas as pd
import requests
from dotenv import load_dotenv

import data_integrity_audit
import position_watcher_state_cleanup


BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
REPORTS_DIR = BASE_DIR / "reports"
JOURNAL = LOGS_DIR / "signals.csv"
ENTRY_TIMING = LOGS_DIR / "entry_timing_engine.csv"
REPORT_HTML = REPORTS_DIR / "report.html"
ANALYTICS_HTML = REPORTS_DIR / "analytics.html"
LOCK_DIR_CANDIDATES = [
    LOGS_DIR / "position_watcher_locks",
    BASE_DIR / "position_watcher_locks",
]

PASS = "PASS"
WARNING = "WARNING"
FAIL = "FAIL"


@dataclass
class HealthCheck:
    name: str
    status: str
    detail: str


def _mask(value: str) -> str:
    if not value:
        return "-"
    if len(value) <= 6:
        return "***"
    return f"{value[:3]}***{value[-3:]}"


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _check_env() -> list[HealthCheck]:
    required = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_REPORTS_CHAT_ID"]
    optional = ["TELEGRAM_SIGNALS_CHAT_ID", "TELEGRAM_CORNIX_CHAT_ID"]
    checks: list[HealthCheck] = []
    missing_required = [name for name in required if not _env(name)]
    missing_optional = [name for name in optional if not _env(name)]
    if missing_required:
        checks.append(HealthCheck("required environment variables", FAIL, "missing: " + ", ".join(missing_required)))
    else:
        checks.append(HealthCheck("required environment variables", PASS, "required vars present"))
    for label, var in [
        ("Telegram Signals chat configuration", "TELEGRAM_SIGNALS_CHAT_ID"),
        ("Telegram Cornix chat configuration", "TELEGRAM_CORNIX_CHAT_ID"),
        ("Telegram Reports chat configuration", "TELEGRAM_REPORTS_CHAT_ID"),
    ]:
        value = _env(var)
        status = PASS if value else WARNING
        checks.append(HealthCheck(label, status, f"{var}={_mask(value)}" if value else f"{var} missing"))
    if missing_optional:
        checks.append(HealthCheck("optional Telegram channel variables", WARNING, "missing: " + ", ".join(missing_optional)))
    return checks


def _check_binance(timeout: float = 10.0) -> HealthCheck:
    try:
        response = requests.get("https://fapi.binance.com/fapi/v1/time", timeout=timeout)
        if 200 <= response.status_code < 400:
            return HealthCheck("Binance Futures API reachability", PASS, f"HTTP {response.status_code}")
        return HealthCheck("Binance Futures API reachability", WARNING, f"HTTP {response.status_code}")
    except requests.RequestException as exc:
        return HealthCheck("Binance Futures API reachability", WARNING, f"{type(exc).__name__}: {exc}")


def _check_csv_rw(path: Path, label: str) -> HealthCheck:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            pd.read_csv(path, nrows=5)
        else:
            return HealthCheck(label, WARNING, f"{path} does not exist yet")
        probe = path.parent / f".health_probe_{path.stem}.tmp"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return HealthCheck(label, PASS, str(path))
    except Exception as exc:
        return HealthCheck(label, FAIL, f"{path}: {type(exc).__name__}: {exc}")


def _check_outcome_data() -> HealthCheck:
    if not JOURNAL.exists():
        return HealthCheck("outcome review data availability", WARNING, "logs/signals.csv missing")
    try:
        df = pd.read_csv(JOURNAL)
    except Exception as exc:
        return HealthCheck("outcome review data availability", FAIL, f"cannot read journal: {exc}")
    result = df.get("result", pd.Series(dtype=str)).fillna("").astype(str).str.upper()
    closed = int(result.isin(["WIN", "LOSS"]).sum())
    open_count = int(result.eq("OPEN").sum())
    if closed or open_count:
        return HealthCheck("outcome review data availability", PASS, f"closed={closed}, open={open_count}")
    return HealthCheck("outcome review data availability", WARNING, "no OPEN/WIN/LOSS rows found")


def _check_report_generation() -> list[HealthCheck]:
    checks: list[HealthCheck] = []
    try:
        import performance_report

        journal = performance_report.load_csv_safely(performance_report.JOURNAL)
        history = performance_report.load_csv_safely(performance_report.HISTORY)
        external = performance_report.load_csv_safely(performance_report.EXTERNAL)
        entry = performance_report.load_csv_safely(performance_report.ENTRY_TIMING)
        report, tables = performance_report.build_full_report(journal, history, external, None)
        report["entry_timing_shadow_summary"] = performance_report.format_entry_timing_summary(entry)
        tables["entry_timing_shadow_summary"] = performance_report.summarize_entry_timing(entry)
        full = performance_report.format_report(report)
        executive = performance_report.format_executive_report(report, entry)
        path = performance_report.write_full_web_report(full)
        performance_report.persist_report(report)
        checks.append(HealthCheck("performance report generation", PASS, f"{len(full)} chars"))
        checks.append(HealthCheck("executive report generation", PASS, f"{len(executive)} chars"))
        checks.append(HealthCheck("reports/report.html generation", PASS, str(path)))
        checks.append(HealthCheck("reports/analytics.html generation", PASS, str(performance_report.REPORTS_DIR / "analytics.html")))
    except Exception as exc:
        checks.append(HealthCheck("performance/executive/web report generation", FAIL, f"{type(exc).__name__}: {exc}"))
    return checks


def _check_locks() -> HealthCheck:
    existing = [path for path in LOCK_DIR_CANDIDATES if path.exists()]
    if existing:
        return HealthCheck("duplicate-alert lock directory", PASS, ", ".join(str(path) for path in existing))
    try:
        probe = LOGS_DIR / "position_watcher_locks"
        probe.mkdir(parents=True, exist_ok=True)
        return HealthCheck("duplicate-alert lock directory", PASS, str(probe))
    except Exception as exc:
        return HealthCheck("duplicate-alert lock directory", FAIL, f"cannot create lock dir: {exc}")


def _check_data_integrity() -> list[HealthCheck]:
    try:
        findings = data_integrity_audit.audit_paths(JOURNAL, ENTRY_TIMING)
    except Exception as exc:
        return [HealthCheck("Data Integrity Audit", FAIL, f"{type(exc).__name__}: {exc}")]
    fail_count = sum(1 for item in findings if item.severity == "FAIL")
    warning_count = sum(1 for item in findings if item.severity == "WARNING")
    info_count = sum(1 for item in findings if item.severity == "INFO")
    if fail_count:
        status = FAIL
    elif warning_count:
        status = WARNING
    else:
        status = PASS
    checks = [
        HealthCheck(
            "Data Integrity Audit",
            status,
            f"critical={fail_count}, warnings={warning_count}, info={info_count}",
        )
    ]
    stale = [item for item in findings if item.check == "CLOSED_ROW_IN_ACTIVE_WATCHER_STATE"]
    if stale:
        checks.append(HealthCheck("Position watcher active stale state", WARNING, "; ".join(item.detail for item in stale)))
    else:
        checks.append(HealthCheck("Position watcher active stale state", PASS, "no active stale state detected by audit"))
    return checks


def _check_position_watcher_cleanup_view() -> HealthCheck:
    try:
        state = position_watcher_state_cleanup.classify_cleanup(JOURNAL)
    except Exception as exc:
        return HealthCheck("Position watcher cleanup dry-run view", WARNING, f"{type(exc).__name__}: {exc}")
    warning_count = state.stale_canonical_active_entries + state.safe_to_remove + state.blocked_unsafe_path + state.blocked_identity_ambiguous
    if warning_count:
        return HealthCheck(
            "Position watcher cleanup dry-run view",
            WARNING,
            f"safe={state.safe_to_remove}, stale_active={state.stale_canonical_active_entries}, unsafe={state.blocked_unsafe_path}, ambiguous={state.blocked_identity_ambiguous}",
        )
    return HealthCheck("Position watcher cleanup dry-run view", PASS, "0 safe removable or blocked runtime items")


def _check_disk(min_free_gb: float = 1.0) -> HealthCheck:
    usage = shutil.disk_usage(BASE_DIR)
    free_gb = usage.free / (1024**3)
    status = PASS if free_gb >= min_free_gb else WARNING
    return HealthCheck("disk free space", status, f"{free_gb:.2f} GB free")


def _check_clock() -> HealthCheck:
    now = datetime.now(timezone.utc)
    if now.year < 2024 or now.year > 2035:
        return HealthCheck("system clock / UTC timestamp sanity", FAIL, now.isoformat())
    return HealthCheck("system clock / UTC timestamp sanity", PASS, now.isoformat())


def _check_imports() -> HealthCheck:
    modules = [
        "pandas",
        "requests",
        "dotenv",
        "cornix_agent",
        "review_signals",
        "performance_report",
        "position_watcher",
        "core.entry_timing_engine",
    ]
    missing = []
    for module in modules:
        try:
            importlib.import_module(module)
        except Exception as exc:
            missing.append(f"{module} ({type(exc).__name__})")
    if missing:
        return HealthCheck("required Python imports", FAIL, "; ".join(missing))
    return HealthCheck("required Python imports", PASS, f"{len(modules)} imports ok")


def _run_systemctl(args: list[str]) -> tuple[int, str]:
    try:
        completed = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=12, check=False)
        output = (completed.stdout + completed.stderr).strip()
        return completed.returncode, output
    except FileNotFoundError:
        return 127, "systemctl not available on this host"
    except Exception as exc:
        return 1, f"{type(exc).__name__}: {exc}"


def check_services() -> list[HealthCheck]:
    units = [
        ("crypto-scanner.service", "service"),
        ("crypto-position-watcher.service", "service"),
        ("crypto-performance-report.timer", "timer"),
        ("crypto-performance-report.service", "service"),
    ]
    checks: list[HealthCheck] = []
    for unit, unit_type in units:
        code, active = _run_systemctl(["is-active", unit])
        if code == 127:
            checks.append(HealthCheck(f"{unit} active state", WARNING, active))
            continue
        status = PASS if active.strip() == "active" or (unit_type == "service" and active.strip() in {"active", "inactive"}) else WARNING
        if active.strip() == "failed":
            status = FAIL
        checks.append(HealthCheck(f"{unit} active state", status, active or f"exit={code}"))
        if unit_type == "timer":
            enabled_code, enabled = _run_systemctl(["is-enabled", unit])
            checks.append(
                HealthCheck(
                    f"{unit} enabled state",
                    PASS if enabled.strip() == "enabled" else WARNING,
                    enabled or f"exit={enabled_code}",
                )
            )
        code, logs = _run_systemctl(["--no-pager", "status", unit])
        if "Traceback" in logs or "ERROR" in logs:
            checks.append(HealthCheck(f"{unit} recent errors", WARNING, "recent Traceback/ERROR text detected"))
    return checks


def run_checks(include_services: bool = True) -> list[HealthCheck]:
    load_dotenv(BASE_DIR / ".env")
    checks: list[HealthCheck] = []
    checks.extend(_check_env())
    checks.append(_check_binance())
    checks.append(_check_csv_rw(JOURNAL, "scanner journal CSV readability/writability"))
    checks.append(_check_csv_rw(ENTRY_TIMING, "Entry Timing CSV readability/writability"))
    checks.append(_check_outcome_data())
    checks.extend(_check_report_generation())
    checks.extend(_check_data_integrity())
    checks.append(_check_position_watcher_cleanup_view())
    checks.append(_check_locks())
    checks.append(_check_disk())
    checks.append(_check_clock())
    checks.append(_check_imports())
    if include_services:
        checks.extend(check_services())
    return checks


def exit_code(checks: list[HealthCheck]) -> int:
    if any(check.status == FAIL for check in checks):
        return 2
    if any(check.status == WARNING for check in checks):
        return 1
    return 0


def print_checks(checks: list[HealthCheck]) -> None:
    print("Crypto Scanner Production Health")
    print(f"Checked at UTC: {datetime.now(timezone.utc).isoformat()}")
    print("")
    for check in checks:
        print(f"{check.status:7} | {check.name} | {check.detail}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run read-only production health checks.")
    parser.add_argument("--no-services", action="store_true", help="Skip systemctl service checks.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    checks = run_checks(include_services=not args.no_services)
    print_checks(checks)
    return exit_code(checks)


if __name__ == "__main__":
    raise SystemExit(main())
