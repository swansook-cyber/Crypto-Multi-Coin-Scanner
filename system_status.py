# -*- coding: utf-8 -*-
"""Compact read-only production status console for Crypto Scanner V1."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd
from dotenv import load_dotenv

import data_integrity_audit
import entry_timing_operational_summary
import performance_report
import position_watcher_state_cleanup


BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
REPORTS_DIR = BASE_DIR / "reports"
BACKUPS_DIR = BASE_DIR / "backups"
JOURNAL = LOGS_DIR / "signals.csv"
ENTRY_TIMING = LOGS_DIR / "entry_timing_engine.csv"
DEFAULT_RELEASE = "V1.0"


StatusRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _run(command: list[str], timeout: int = 5) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)


def _safe_run(command: list[str], runner: StatusRunner | None = None) -> tuple[int, str, str]:
    try:
        result = (runner or _run)(command)
        return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"


def git_text(args: list[str]) -> str:
    code, stdout, _stderr = _safe_run(["git", *args])
    return stdout if code == 0 and stdout else "N/A"


def short_commit() -> str:
    return git_text(["rev-parse", "--short", "HEAD"])


def release_marker() -> str:
    load_dotenv(BASE_DIR / ".env")
    return os.getenv("SCANNER_RELEASE", DEFAULT_RELEASE).strip() or DEFAULT_RELEASE


def service_state(unit: str, runner: StatusRunner | None = None) -> str:
    code, stdout, stderr = _safe_run(["systemctl", "is-active", unit], runner=runner)
    if code == 127 or "FileNotFoundError" in stderr:
        return "UNKNOWN"
    return "RUNNING" if stdout == "active" else "STOPPED"


def timer_state(unit: str, runner: StatusRunner | None = None) -> str:
    state = service_state(unit, runner=runner)
    if state == "RUNNING":
        return "ACTIVE"
    if state == "STOPPED":
        return "INACTIVE"
    return "UNKNOWN"


def next_timer_run(unit: str = "crypto-performance-report.timer", runner: StatusRunner | None = None) -> str:
    code, stdout, _stderr = _safe_run(
        ["systemctl", "list-timers", unit, "--no-pager", "--no-legend"],
        runner=runner,
    )
    if code != 0 or not stdout:
        return "N/A"
    first = stdout.splitlines()[0].strip()
    return " ".join(first.split()[:2]) if first else "N/A"


def load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def _series(df: pd.DataFrame, column: str, default: str = "") -> pd.Series:
    if column in df.columns:
        return df[column].fillna(default)
    return pd.Series([default] * len(df))


def _today_mask(df: pd.DataFrame) -> pd.Series:
    if df.empty or "timestamp" not in df.columns:
        return pd.Series([False] * len(df))
    ts = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    today = datetime.now(timezone.utc).date()
    return ts.dt.date == today


def runtime_metrics(report: dict[str, Any], journal: pd.DataFrame) -> dict[str, Any]:
    status = _series(journal, "signal_status").astype(str).str.lower()
    result = _series(journal, "result").astype(str).str.upper()
    today = _today_mask(journal)
    today_sent = int((today & (status == "sent")).sum()) if len(journal) else 0
    today_wins = int((today & (result == "WIN")).sum()) if len(journal) else 0
    today_losses = int((today & (result == "LOSS")).sum()) if len(journal) else 0
    return {
        "closed_signals": int(report.get("closed_signals", 0) or 0),
        "open_signals": int(report.get("open_signals", 0) or 0),
        "win_rate": float(report.get("win_rate", 0.0) or 0.0),
        "net_r": float(report.get("net_r_estimate", 0.0) or 0.0),
        "today_sent": today_sent,
        "today_wins": today_wins,
        "today_losses": today_losses,
    }


def build_report_snapshot() -> tuple[dict[str, Any], pd.DataFrame, str | None]:
    try:
        journal = performance_report.load_csv_safely(performance_report.JOURNAL)
        history = performance_report.load_csv_safely(performance_report.HISTORY)
        external = performance_report.load_csv_safely(performance_report.EXTERNAL)
        report, _tables = performance_report.build_full_report(journal, history, external, None)
        return report, journal, None
    except Exception as exc:
        return {}, load_csv(JOURNAL), f"{type(exc).__name__}: {exc}"


def reporting_status(report: dict[str, Any], report_error: str | None) -> dict[str, str]:
    executive = "FAIL"
    try:
        entry = load_csv(ENTRY_TIMING)
        performance_report.format_executive_report(report, entry)
        executive = "PASS" if not report_error else "FAIL"
    except Exception:
        executive = "FAIL"
    return {
        "executive_report": executive,
        "full_report_html": "PASS" if (REPORTS_DIR / "report.html").exists() else "FAIL",
        "analytics_html": "PASS" if (REPORTS_DIR / "analytics.html").exists() else "FAIL",
        "reports_chat_configured": "YES" if os.getenv("TELEGRAM_REPORTS_CHAT_ID", "").strip() else "NO",
    }


def entry_timing_status(entry_path: Path = ENTRY_TIMING, journal_path: Path = JOURNAL) -> dict[str, Any]:
    entry = load_csv(entry_path)
    journal = load_csv(journal_path)
    total = int(len(entry))
    latest = "N/A"
    if total and "timestamp" in entry.columns:
        parsed = pd.to_datetime(entry["timestamp"], errors="coerce", utc=True).dropna()
        if not parsed.empty:
            latest = parsed.max().isoformat()
    recommendation = "N/A"
    if total and "recommendation" in entry.columns:
        counts = entry["recommendation"].fillna("").astype(str).replace("", "N/A").value_counts()
        if not counts.empty:
            recommendation = str(counts.index[0])
    linked = entry_timing_operational_summary.linked_closed_outcomes(entry, journal)
    return {
        "mode": "SHADOW",
        "evaluated": total,
        "latest_evaluation": latest,
        "readiness": entry_timing_operational_summary.readiness_status(linked),
        "dominant_recommendation": recommendation,
    }


def compact_symbols(value: Any, limit: int = 8, max_chars: int = 58) -> str:
    text = str(value or "").strip()
    if not text or text.upper() == "N/A":
        return "N/A"
    symbols: list[str] = []
    for token in re.split(r"[\s,|:()]+", text.upper()):
        token = token.strip()
        if token.endswith("USDT") and token not in symbols:
            symbols.append(token)
    if not symbols:
        return text[: max_chars - 3] + "..." if len(text) > max_chars else text
    extra = max(0, len(symbols) - limit)
    compact = ", ".join(symbols[:limit])
    if extra:
        compact = f"{compact}, +{extra}"
    return compact[: max_chars - 3] + "..." if len(compact) > max_chars else compact


def production_universe(report: dict[str, Any]) -> dict[str, str]:
    return {
        "tier_s": compact_symbols(report.get("production_universe_tier_s")),
        "tier_a": compact_symbols(report.get("production_universe_tier_a")),
        "watch": compact_symbols(report.get("production_universe_watch")),
        "report_only": compact_symbols(report.get("production_universe_report_only")),
    }


def audit_status() -> str:
    findings = data_integrity_audit.audit_paths(JOURNAL, ENTRY_TIMING)
    if any(item.severity == "FAIL" for item in findings):
        return "FAIL"
    if any(item.severity == "WARNING" for item in findings):
        return "WARNING"
    return "PASS"


def duplicate_protection_status() -> str:
    candidates = [
        LOGS_DIR / "signals_position_watcher_locks",
        LOGS_DIR / "position_watcher_locks",
        BASE_DIR / "position_watcher_locks",
    ]
    return "PASS" if any(path.exists() and path.is_dir() for path in candidates) else "FAIL"


def age_text(path: Path, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    seconds = max(0, int((now - modified).total_seconds()))
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def latest_backup(backups_dir: Path = BACKUPS_DIR) -> str:
    backups = sorted(backups_dir.glob("runtime_*.zip")) if backups_dir.exists() else []
    if not backups:
        return "N/A"
    latest = max(backups, key=lambda path: path.stat().st_mtime)
    return f"{latest.name} ({age_text(latest)})"


def disk_free_gb(path: Path = BASE_DIR) -> float:
    usage = shutil.disk_usage(path)
    return round(usage.free / (1024**3), 1)


def safety_status() -> dict[str, Any]:
    stale = position_watcher_state_cleanup.stale_state_items(JOURNAL)
    return {
        "data_integrity": audit_status(),
        "active_stale_watcher_state": len(stale),
        "duplicate_alert_protection": duplicate_protection_status(),
        "latest_runtime_backup": latest_backup(),
        "disk_free_gb": disk_free_gb(),
    }


def final_status(statuses: list[str]) -> tuple[str, int]:
    normalized = [str(status).upper() for status in statuses]
    if "FAIL" in normalized:
        return "NOT READY", 2
    if any(status in {"WARNING", "UNKNOWN", "NO"} for status in normalized):
        return "READY WITH WARNINGS", 1
    return "READY FOR PRODUCTION", 0


def build_status(include_services: bool = True) -> dict[str, Any]:
    load_dotenv(BASE_DIR / ".env")
    report, journal, report_error = build_report_snapshot()
    services = {
        "scanner": service_state("crypto-scanner.service") if include_services else "UNKNOWN",
        "position_watcher": service_state("crypto-position-watcher.service") if include_services else "UNKNOWN",
        "performance_timer": timer_state("crypto-performance-report.timer") if include_services else "UNKNOWN",
        "next_performance_report": next_timer_run() if include_services else "N/A",
    }
    reporting = reporting_status(report, report_error)
    entry = entry_timing_status()
    universe = production_universe(report)
    safety = safety_status()
    status_inputs = [
        safety["data_integrity"],
        safety["duplicate_alert_protection"],
        reporting["executive_report"],
        reporting["full_report_html"],
        reporting["analytics_html"],
    ]
    if reporting["reports_chat_configured"] == "NO":
        status_inputs.append("WARNING")
    if include_services and any(value == "UNKNOWN" for key, value in services.items() if key != "next_performance_report"):
        status_inputs.append("WARNING")
    final, code = final_status(status_inputs)
    return {
        "release": {
            "commit": short_commit(),
            "release": release_marker(),
            "utc_time": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        },
        "services": services,
        "runtime": runtime_metrics(report, journal),
        "reporting": reporting,
        "entry_timing": entry,
        "production_universe": universe,
        "safety": safety,
        "final_status": final,
        "exit_code": code,
    }


def _pct(value: Any) -> str:
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return "N/A"


def _r(value: Any) -> str:
    try:
        return f"{float(value):.2f}R"
    except (TypeError, ValueError):
        return "N/A"


def format_status(status: dict[str, Any]) -> str:
    release = status["release"]
    services = status["services"]
    runtime = status["runtime"]
    reporting = status["reporting"]
    entry = status["entry_timing"]
    universe = status["production_universe"]
    safety = status["safety"]
    lines = [
        "Crypto Scanner Production V1",
        "============================",
        "",
        "Release",
        f"- Commit: {release['commit']}",
        f"- Release: {release['release']}",
        f"- UTC time: {release['utc_time']}",
        "",
        "Services",
        f"- Scanner: {services['scanner']}",
        f"- Position Watcher: {services['position_watcher']}",
        f"- Performance Timer: {services['performance_timer']}",
        f"- Next Performance Report: {services['next_performance_report']}",
        "",
        "Runtime",
        f"- Closed Signals: {runtime['closed_signals']}",
        f"- Open Signals: {runtime['open_signals']}",
        f"- Win Rate: {_pct(runtime['win_rate'])}",
        f"- Net R: {_r(runtime['net_r'])}",
        f"- Today Sent: {runtime['today_sent']}",
        f"- Today Wins/Losses: {runtime['today_wins']}/{runtime['today_losses']}",
        "",
        "Reporting",
        f"- Executive Report: {reporting['executive_report']}",
        f"- Full Report HTML: {reporting['full_report_html']}",
        f"- Analytics HTML: {reporting['analytics_html']}",
        f"- Reports Chat Configured: {reporting['reports_chat_configured']}",
        "",
        "Entry Timing",
        f"- Mode: {entry['mode']}",
        f"- Evaluated: {entry['evaluated']}",
        f"- Latest Evaluation: {entry['latest_evaluation']}",
        f"- Readiness: {entry['readiness']}",
        f"- Dominant Recommendation: {entry['dominant_recommendation']}",
        "",
        "Production Universe",
        f"- Tier S: {universe['tier_s']}",
        f"- Tier A: {universe['tier_a']}",
        f"- Watch: {universe['watch']}",
        f"- Report Only: {universe['report_only']}",
        "",
        "Safety",
        f"- Data Integrity: {safety['data_integrity']}",
        f"- Active Stale Watcher State: {safety['active_stale_watcher_state']}",
        f"- Duplicate Alert Protection: {safety['duplicate_alert_protection']}",
        f"- Latest Runtime Backup: {safety['latest_runtime_backup']}",
        f"- Disk Free: {safety['disk_free_gb']:.1f} GB",
        "",
        "Final Status",
        f"- {status['final_status']}",
    ]
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Print compact read-only Crypto Scanner V1 status.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--no-services", action="store_true", help="Skip systemd checks.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    status = build_status(include_services=not args.no_services)
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
    else:
        print(format_status(status))
    return int(status["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
