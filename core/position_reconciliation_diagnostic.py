# -*- coding: utf-8 -*-
"""Read-only production position reconciliation diagnostic.

This module inspects local scanner journals and runtime state without changing
files, services, Telegram routes, Cornix messages, or trading behavior.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from core.signal_identity import canonical_signal_key, normalize_timestamp


BASE_DIR = Path(__file__).resolve().parents[1]
BINANCE_FUTURES_TIME_URL = "https://fapi.binance.com/fapi/v1/time"
VERSION = "position-reconciliation-diagnostic-v1"
OPEN_WARNING_HOURS = 24
OPEN_STALE_HOURS = 48
FRESH_WARNING_HOURS = 24
FRESH_STALE_HOURS = 48
TERMINAL_RESULTS = {"WIN", "LOSS", "CLOSED", "TP", "SL", "EXPIRED", "BREAKEVEN"}
ACTIVE_SIGNAL_STATUS = {
    "sent",
    "tier_c_report_only",
    "weak_symbol_report_only",
    "session_risk_report_only",
    "london_long_report_only",
}
SEVERITY_ORDER = {"OK": 0, "UNKNOWN": 1, "WARNING": 2, "STALE": 3, "CONFLICT": 4}


@dataclass
class CheckResult:
    name: str
    status: str
    details: dict[str, Any] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


@dataclass
class DiagnosticResult:
    version: str
    checked_at_utc: str
    overall_status: str
    checks: list[CheckResult]
    summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "checked_at_utc": self.checked_at_utc,
            "overall_status": self.overall_status,
            "checks": [
                {
                    "name": check.name,
                    "status": check.status,
                    "details": check.details,
                    "items": check.items,
                    "messages": check.messages,
                }
                for check in self.checks
            ],
            "summary": self.summary,
        }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_text(dt: datetime | None = None) -> str:
    value = dt or utc_now()
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: Any) -> pd.Timestamp | None:
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    return ts


def safe_age_hours(timestamp: Any, now: datetime | None = None) -> float | None:
    ts = parse_timestamp(timestamp)
    if ts is None:
        return None
    current = pd.Timestamp(now or utc_now())
    if current.tzinfo is None:
        current = current.tz_localize("UTC")
    else:
        current = current.tz_convert("UTC")
    return float((current - ts).total_seconds() / 3600.0)


def status_max(*statuses: str) -> str:
    present = [status for status in statuses if status]
    if not present:
        return "UNKNOWN"
    return max(present, key=lambda value: SEVERITY_ORDER.get(value, 1))


def read_csv_safely(path: Path) -> tuple[pd.DataFrame | None, str]:
    try:
        if not path.exists():
            return None, "missing"
        return pd.read_csv(path), "ok"
    except pd.errors.EmptyDataError:
        return pd.DataFrame(), "empty"
    except Exception as exc:  # pragma: no cover - defensive filesystem guard
        return None, f"read_error: {exc}"


def clean_cell(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if text.lower() in {"nan", "nat", "none", "null"}:
        return ""
    return text


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                values[key] = value
    except OSError:
        return values
    return values


def env_value(name: str, env_file_values: dict[str, str] | None = None, default: str = "") -> str:
    value = os.getenv(name)
    if value is not None:
        return value
    return (env_file_values or {}).get(name, default)


def env_bool_value(name: str, env_file_values: dict[str, str] | None = None, default: bool = False) -> bool:
    raw = env_value(name, env_file_values, "true" if default else "false")
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def row_signal_status(row: pd.Series) -> str:
    return str(row.get("signal_status", "") or "").strip().lower()


def row_result(row: pd.Series) -> str:
    return str(row.get("result", "") or "").strip().upper()


def is_terminal_row(row: pd.Series) -> bool:
    return row_result(row) in TERMINAL_RESULTS


def is_open_row(row: pd.Series) -> bool:
    return row_result(row) == "OPEN"


def identity_for_row(row: pd.Series) -> str:
    existing = str(row.get("canonical_signal_key", "") or "").strip()
    if existing.startswith(("sig:v1:", "id:v1:")):
        return existing
    return canonical_signal_key(
        symbol=row.get("symbol", ""),
        side=row.get("side", row.get("direction", "")),
        timestamp=row.get("timestamp", row.get("timestamp_utc", "")),
        entry=row.get("entry", row.get("entry_low", "")),
        signal_id=row.get("signal_id", ""),
        candidate_id=row.get("candidate_id", ""),
    )


def fetch_binance_server_utc(session: requests.Session | None = None) -> datetime:
    requester = session or requests.Session()
    response = requester.get(BINANCE_FUTURES_TIME_URL, timeout=10)
    response.raise_for_status()
    payload = response.json()
    server_time_ms = int(payload["serverTime"])
    return datetime.fromtimestamp(server_time_ms / 1000.0, tz=timezone.utc)


def check_clock_health(session: requests.Session | None = None, now: datetime | None = None) -> CheckResult:
    local_now = now or utc_now()
    try:
        binance_now = fetch_binance_server_utc(session)
    except Exception as exc:
        return CheckResult(
            "clock",
            "UNKNOWN",
            {"local_utc": utc_text(local_now), "binance_server_utc": None, "drift_seconds": None},
            messages=[f"Binance Futures server time unavailable: {exc}"],
        )
    drift = abs((local_now - binance_now).total_seconds())
    if drift <= 5:
        status = "OK"
    elif drift <= 30:
        status = "WARNING"
    else:
        status = "STALE"
    return CheckResult(
        "clock",
        status,
        {
            "local_utc": utc_text(local_now),
            "binance_server_utc": utc_text(binance_now),
            "drift_seconds": round(float(drift), 3),
        },
    )


def check_signals_csv(signals_path: Path, now: datetime | None = None) -> CheckResult:
    df, read_status = read_csv_safely(signals_path)
    if df is None:
        return CheckResult("signals", "UNKNOWN", {"path": str(signals_path), "read_status": read_status})
    if df.empty:
        return CheckResult(
            "signals",
            "OK",
            {
                "path": str(signals_path),
                "total_rows": 0,
                "sent_rows": 0,
                "open_rows": 0,
                "closed_rows": 0,
                "latest_signal_timestamp": "",
                "latest_sent_timestamp": "",
                "duplicate_canonical_signal_keys": 0,
                "duplicate_terminal_outcome_keys": 0,
            },
        )

    working = df.copy()
    statuses = working.get("signal_status", pd.Series([""] * len(working))).fillna("").astype(str).str.lower()
    results = working.get("result", pd.Series([""] * len(working))).fillna("").astype(str).str.upper()
    timestamps = pd.to_datetime(working.get("timestamp", pd.Series([""] * len(working))), utc=True, errors="coerce")
    keys = working.apply(identity_for_row, axis=1)
    nonblank_keys = keys[keys.astype(str).str.strip() != ""]
    duplicate_key_count = int(nonblank_keys.duplicated(keep=False).sum())
    terminal_mask = results.isin(TERMINAL_RESULTS)
    terminal_keys = keys[terminal_mask & (keys.astype(str).str.strip() != "")]
    duplicate_terminal_count = int(terminal_keys.duplicated(keep=False).sum())
    latest_ts = timestamps.max()
    latest_sent_ts = timestamps[statuses == "sent"].max() if (statuses == "sent").any() else pd.NaT
    future_rows = int((timestamps > pd.Timestamp(now or utc_now())).sum())

    status = "OK"
    messages: list[str] = []
    if duplicate_terminal_count:
        status = "CONFLICT"
        messages.append("Multiple terminal outcomes share the same canonical signal key.")
    elif duplicate_key_count:
        status = "WARNING"
        messages.append("Duplicate canonical signal keys detected.")
    if future_rows:
        status = status_max(status, "WARNING")
        messages.append("Future signal timestamps detected.")

    return CheckResult(
        "signals",
        status,
        {
            "path": str(signals_path),
            "total_rows": int(len(working)),
            "sent_rows": int((statuses == "sent").sum()),
            "open_rows": int((results == "OPEN").sum()),
            "closed_rows": int(results.isin(["WIN", "LOSS"]).sum()),
            "latest_signal_timestamp": "" if pd.isna(latest_ts) else latest_ts.isoformat().replace("+00:00", "Z"),
            "latest_sent_timestamp": "" if pd.isna(latest_sent_ts) else latest_sent_ts.isoformat().replace("+00:00", "Z"),
            "duplicate_canonical_signal_keys": duplicate_key_count,
            "duplicate_terminal_outcome_keys": duplicate_terminal_count,
            "future_timestamp_rows": future_rows,
        },
        messages=messages,
    )


def check_stale_open(signals_path: Path, now: datetime | None = None) -> CheckResult:
    df, read_status = read_csv_safely(signals_path)
    if df is None:
        return CheckResult("open_positions", "UNKNOWN", {"path": str(signals_path), "read_status": read_status})
    if df.empty or "result" not in df.columns:
        return CheckResult("open_positions", "OK", {"open_rows": 0, "warning_open_rows": 0, "stale_open_rows": 0})

    current = now or utc_now()
    items: list[dict[str, Any]] = []
    warning_count = 0
    stale_count = 0
    open_df = df[df["result"].fillna("").astype(str).str.upper() == "OPEN"].copy()
    for _, row in open_df.iterrows():
        age = safe_age_hours(row.get("timestamp", ""), current)
        item_status = "UNKNOWN"
        if age is not None:
            if age <= OPEN_WARNING_HOURS:
                item_status = "OK"
            elif age <= OPEN_STALE_HOURS:
                item_status = "WARNING"
                warning_count += 1
            else:
                item_status = "STALE"
                stale_count += 1
        items.append(
            {
                "status": item_status,
                "symbol": str(row.get("symbol", "")),
                "side": str(row.get("side", row.get("direction", ""))),
                "timestamp": str(row.get("timestamp", "")),
                "age_hours": None if age is None else round(age, 2),
                "entry": str(row.get("entry", "")),
                "sl": str(row.get("stop_loss", row.get("sl", ""))),
                "tp1": str(row.get("tp1", "")),
                "tp2": str(row.get("tp2", "")),
            }
        )
    status = "OK"
    if stale_count:
        status = "STALE"
    elif warning_count:
        status = "WARNING"
    elif any(item["status"] == "UNKNOWN" for item in items):
        status = "UNKNOWN"
    return CheckResult(
        "open_positions",
        status,
        {"open_rows": int(len(open_df)), "warning_open_rows": warning_count, "stale_open_rows": stale_count},
        items=items,
    )


def check_outcome_consistency(signals_path: Path, now: datetime | None = None) -> CheckResult:
    df, read_status = read_csv_safely(signals_path)
    if df is None:
        return CheckResult("outcome_consistency", "UNKNOWN", {"path": str(signals_path), "read_status": read_status})
    if df.empty:
        return CheckResult("outcome_consistency", "OK", {"conflicts": 0, "warnings": 0, "stale_unresolved": 0})

    current = now or utc_now()
    items: list[dict[str, Any]] = []
    working = df.copy()
    keys = working.apply(identity_for_row, axis=1)
    working["_canonical_key"] = keys
    for index, row in working.iterrows():
        result = row_result(row)
        hit_target = clean_cell(row.get("hit_target", ""))
        closed_at_raw = clean_cell(row.get("closed_at", ""))
        signal_ts = parse_timestamp(row.get("timestamp", ""))
        close_ts = parse_timestamp(closed_at_raw)
        status = ""
        reason = ""
        if result == "OPEN" and (hit_target or closed_at_raw):
            status = "CONFLICT"
            reason = "outcome fields exist while result remains OPEN"
        elif result in TERMINAL_RESULTS:
            if not closed_at_raw and result in {"WIN", "LOSS"}:
                status = "WARNING"
                reason = "terminal result missing closed_at"
            if signal_ts is not None and close_ts is not None and close_ts < signal_ts:
                status = "CONFLICT"
                reason = "closed_at before signal timestamp"
        elif row_signal_status(row) == "sent":
            age = safe_age_hours(row.get("timestamp", ""), current)
            if age is not None and age > OPEN_STALE_HOURS:
                status = "STALE"
                reason = "unresolved historical sent signal beyond stale threshold"
        if status:
            items.append(
                {
                    "row": int(index),
                    "status": status,
                    "reason": reason,
                    "canonical_signal_key": str(row.get("_canonical_key", "")),
                    "symbol": str(row.get("symbol", "")),
                    "side": str(row.get("side", row.get("direction", ""))),
                    "result": result,
                    "hit_target": hit_target,
                    "timestamp": str(row.get("timestamp", "")),
                    "closed_at": closed_at_raw,
                }
            )

    terminal = working[working.apply(is_terminal_row, axis=1) & (working["_canonical_key"].astype(str).str.strip() != "")]
    for key, group in terminal.groupby("_canonical_key"):
        if len(group) <= 1:
            continue
        distinct = sorted({str(value).upper() for value in group.get("result", pd.Series(dtype=str)).tolist()})
        items.append(
            {
                "status": "CONFLICT",
                "reason": "multiple terminal outcomes for canonical signal",
                "canonical_signal_key": str(key),
                "results": ",".join(distinct),
                "rows": int(len(group)),
            }
        )

    status = "OK"
    for item in items:
        status = status_max(status, str(item.get("status", "")))
    return CheckResult(
        "outcome_consistency",
        status,
        {
            "conflicts": sum(1 for item in items if item.get("status") == "CONFLICT"),
            "warnings": sum(1 for item in items if item.get("status") == "WARNING"),
            "stale_unresolved": sum(1 for item in items if item.get("status") == "STALE"),
        },
        items=items[:50],
    )


def file_freshness_status(path: Path, now: datetime | None = None) -> tuple[str, dict[str, Any]]:
    if not path.exists():
        return "UNKNOWN", {"exists": False, "path": str(path), "last_modified_utc": "", "age_hours": None}
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError as exc:
        return "UNKNOWN", {"exists": True, "path": str(path), "error": str(exc)}
    age = safe_age_hours(modified, now)
    status = "UNKNOWN"
    if age is not None:
        if age <= FRESH_WARNING_HOURS:
            status = "OK"
        elif age <= FRESH_STALE_HOURS:
            status = "WARNING"
        else:
            status = "STALE"
    return status, {
        "exists": True,
        "path": str(path),
        "last_modified_utc": utc_text(modified),
        "age_hours": None if age is None else round(age, 2),
    }


def check_watcher_state(base_dir: Path = BASE_DIR, now: datetime | None = None) -> CheckResult:
    paths = [
        base_dir / "watchdog" / "state.json",
        base_dir / "signal_state.json",
    ]
    details: dict[str, Any] = {}
    statuses: list[str] = []
    for path in paths:
        status, info = file_freshness_status(path, now)
        details[path.name if path.name != "state.json" else str(path.relative_to(base_dir))] = info
        statuses.append(status)
    lock_dir = base_dir / "logs" / "signals_position_watcher_locks"
    lock_count = 0
    if lock_dir.exists():
        try:
            lock_count = len([path for path in lock_dir.glob("*.lock") if path.is_file()])
        except OSError:
            statuses.append("UNKNOWN")
    details["position_watcher_locks"] = {"path": str(lock_dir), "exists": lock_dir.exists(), "lock_count": lock_count}
    return CheckResult("watcher_state", status_max(*statuses), details)


def check_service_freshness(base_dir: Path = BASE_DIR, now: datetime | None = None) -> CheckResult:
    # The portable core diagnostic intentionally does not invoke systemctl.
    # Service truth should be layered in separately on VPS hosts.
    candidates = {
        "scanner": base_dir / "logs" / "signals.csv",
        "outcome_checker": base_dir / "logs" / "signals_history.csv",
        "position_watcher": base_dir / "logs" / "signals.csv",
        "performance_report": base_dir / "logs" / "performance_report.txt",
        "moving_sl_collector": base_dir / "logs" / "moving_sl_prospective_shadow.csv",
    }
    details: dict[str, Any] = {}
    statuses: list[str] = []
    for name, path in candidates.items():
        status, info = file_freshness_status(path, now)
        details[name] = info | {"service_runtime_status": "UNKNOWN", "systemctl_checked": False}
        statuses.append(status)
    return CheckResult(
        "service_freshness",
        status_max(*statuses),
        details,
        messages=["systemctl is not invoked by the portable read-only diagnostic."],
    )


def check_moving_sl_shadow(base_dir: Path = BASE_DIR, env_file_values: dict[str, str] | None = None) -> CheckResult:
    output_path = base_dir / "logs" / "moving_sl_prospective_shadow.csv"
    shadow_enabled = env_bool_value("MOVING_SL_SHADOW_ENABLED", env_file_values, False)
    live_enabled = env_bool_value("MOVING_SL_LIVE_ENABLED", env_file_values, False)
    start_raw = env_value("MOVING_SL_PROSPECTIVE_START_UTC", env_file_values, "")
    start_norm = normalize_timestamp(start_raw)
    details: dict[str, Any] = {
        "shadow_enabled": shadow_enabled,
        "live_enabled": live_enabled,
        "prospective_start_utc": start_norm,
        "output_path": str(output_path),
    }
    items: list[dict[str, Any]] = []
    status = "OK"
    if live_enabled:
        status = "CONFLICT"
        items.append({"status": "CONFLICT", "reason": "MOVING_SL_LIVE_ENABLED must remain false/no-op"})
    if shadow_enabled and not start_norm:
        status = status_max(status, "WARNING")
        items.append({"status": "WARNING", "reason": "MOVING_SL_PROSPECTIVE_START_UTC missing or invalid"})

    df, read_status = read_csv_safely(output_path)
    details["read_status"] = read_status
    if df is None:
        return CheckResult("moving_sl_shadow", status_max(status, "UNKNOWN"), details, items)
    if df.empty:
        details.update({"rows": 0, "rows_before_prospective_start": 0, "duplicate_canonical_lifecycle_rows": 0})
        return CheckResult("moving_sl_shadow", status, details, items)

    timestamps = pd.to_datetime(df.get("timestamp_utc", pd.Series([""] * len(df))), utc=True, errors="coerce")
    start_ts = parse_timestamp(start_norm)
    before_start = 0
    if start_ts is not None:
        before_start = int((timestamps < start_ts).sum())
        if before_start:
            status = "CONFLICT"
            items.append({"status": "CONFLICT", "reason": "moving-sl shadow rows before prospective boundary", "count": before_start})
    keys = df.get("canonical_signal_key", pd.Series([""] * len(df))).fillna("").astype(str)
    duplicate_keys = int(keys[keys.str.strip() != ""].duplicated(keep=False).sum())
    if duplicate_keys:
        status = "CONFLICT"
        items.append({"status": "CONFLICT", "reason": "duplicate canonical moving-sl lifecycle rows", "count": duplicate_keys})
    latest_ts = timestamps.max()
    details.update(
        {
            "rows": int(len(df)),
            "rows_before_prospective_start": before_start,
            "duplicate_canonical_lifecycle_rows": duplicate_keys,
            "latest_row_timestamp_utc": "" if pd.isna(latest_ts) else latest_ts.isoformat().replace("+00:00", "Z"),
        }
    )
    return CheckResult("moving_sl_shadow", status, details, items)


def check_timestamp_sanity(base_dir: Path = BASE_DIR, env_file_values: dict[str, str] | None = None, now: datetime | None = None) -> CheckResult:
    current = pd.Timestamp(now or utc_now())
    if current.tzinfo is None:
        current = current.tz_localize("UTC")
    else:
        current = current.tz_convert("UTC")
    config_names = ["SETUP_STRENGTH_PROSPECTIVE_START_UTC", "MOVING_SL_PROSPECTIVE_START_UTC"]
    items: list[dict[str, Any]] = []
    for name in config_names:
        raw = env_value(name, env_file_values, "")
        if raw and not normalize_timestamp(raw):
            items.append({"status": "WARNING", "source": name, "value": raw, "reason": "invalid timezone/timestamp"})
        parsed = parse_timestamp(raw)
        if parsed is not None and parsed > current + pd.Timedelta(minutes=5):
            items.append({"status": "WARNING", "source": name, "value": raw, "reason": "future timestamp"})

    signals_path = base_dir / "logs" / "signals.csv"
    df, _read_status = read_csv_safely(signals_path)
    if df is not None and not df.empty:
        for column in ["timestamp", "closed_at", "outcome_alert_at", "tp1_alert_at"]:
            if column not in df.columns:
                continue
            parsed = pd.to_datetime(df[column], utc=True, errors="coerce")
            raw_nonblank = df[column].fillna("").astype(str).str.strip() != ""
            invalid = int((raw_nonblank & parsed.isna()).sum())
            future = int((parsed > current + pd.Timedelta(minutes=5)).sum())
            if invalid:
                items.append({"status": "WARNING", "source": f"signals.csv:{column}", "reason": "invalid timestamp rows", "count": invalid})
            if future:
                items.append({"status": "WARNING", "source": f"signals.csv:{column}", "reason": "future timestamp rows", "count": future})
    status = "OK" if not items else status_max(*(str(item["status"]) for item in items))
    return CheckResult("timestamp_sanity", status, {"issues": len(items)}, items=items[:50])


def run_diagnostic(
    base_dir: Path = BASE_DIR,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> DiagnosticResult:
    checked_at = now or utc_now()
    env_file_values = parse_env_file(base_dir / ".env")
    signals_path = base_dir / "logs" / "signals.csv"
    checks = [
        check_clock_health(session=session, now=checked_at),
        check_signals_csv(signals_path, checked_at),
        check_stale_open(signals_path, checked_at),
        check_outcome_consistency(signals_path, checked_at),
        check_watcher_state(base_dir, checked_at),
        check_service_freshness(base_dir, checked_at),
        check_moving_sl_shadow(base_dir, env_file_values),
        check_timestamp_sanity(base_dir, env_file_values, checked_at),
    ]
    visible_statuses = [check.status for check in checks if check.status != "UNKNOWN"]
    overall = status_max(*visible_statuses) if visible_statuses else "UNKNOWN"
    summary = {
        "ok": sum(1 for check in checks if check.status == "OK"),
        "warnings": sum(1 for check in checks if check.status == "WARNING"),
        "stale": sum(1 for check in checks if check.status == "STALE"),
        "conflicts": sum(1 for check in checks if check.status == "CONFLICT"),
        "unknown": sum(1 for check in checks if check.status == "UNKNOWN"),
    }
    return DiagnosticResult(VERSION, utc_text(checked_at), overall, checks, summary)


def _detail_line(value: Any) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        if math.isfinite(value):
            return f"{value:.2f}"
        return "N/A"
    return str(value)


def format_text_report(result: DiagnosticResult, verbose: bool = False) -> str:
    by_name = {check.name: check for check in result.checks}
    lines = [
        "Position Reconciliation Diagnostic V1",
        "=====================================",
        f"Checked UTC: {result.checked_at_utc}",
        "",
        f"Clock: {by_name.get('clock', CheckResult('clock', 'UNKNOWN')).status}",
        f"Signals: {by_name.get('signals', CheckResult('signals', 'UNKNOWN')).status}",
        f"Open positions: {by_name.get('open_positions', CheckResult('open_positions', 'UNKNOWN')).status}",
        f"Outcome consistency: {by_name.get('outcome_consistency', CheckResult('outcome_consistency', 'UNKNOWN')).status}",
        f"Watcher state: {by_name.get('watcher_state', CheckResult('watcher_state', 'UNKNOWN')).status}",
        f"Moving-SL shadow: {by_name.get('moving_sl_shadow', CheckResult('moving_sl_shadow', 'UNKNOWN')).status}",
        "",
        f"Overall: {result.overall_status}",
        "",
    ]
    signals = by_name.get("signals")
    if signals:
        details = signals.details
        lines.extend(
            [
                "Signals CSV",
                f"- Total rows: {details.get('total_rows', 'N/A')}",
                f"- Sent rows: {details.get('sent_rows', 'N/A')}",
                f"- OPEN rows: {details.get('open_rows', 'N/A')}",
                f"- CLOSED rows: {details.get('closed_rows', 'N/A')}",
                f"- Latest signal: {details.get('latest_signal_timestamp') or 'N/A'}",
                f"- Latest sent: {details.get('latest_sent_timestamp') or 'N/A'}",
                f"- Duplicate keys: {details.get('duplicate_canonical_signal_keys', 'N/A')}",
                "",
            ]
        )
    open_check = by_name.get("open_positions")
    if open_check and open_check.items:
        visible_items = [item for item in open_check.items if item.get("status") != "OK"]
        if verbose and not visible_items:
            visible_items = open_check.items
        if visible_items:
            lines.append("OPEN Rows" if verbose else "Suspicious OPEN Rows")
        for item in visible_items[:10]:
            lines.append(
                f"- {item.get('status')} {item.get('symbol')} {item.get('side')} "
                f"{item.get('timestamp')} age={_detail_line(item.get('age_hours'))}h "
                f"entry={item.get('entry')} sl={item.get('sl')} tp1={item.get('tp1')} tp2={item.get('tp2')}"
            )
        if visible_items:
            lines.append("")
    if verbose:
        for check in result.checks:
            if check.messages:
                lines.append(f"{check.name} notes")
                lines.extend(f"- {message}" for message in check.messages)
                lines.append("")
            if check.items and check.name != "open_positions":
                lines.append(f"{check.name} items")
                for item in check.items[:20]:
                    lines.append(f"- {item}")
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only position reconciliation diagnostic.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument("--verbose", action="store_true", help="Show diagnostic detail items.")
    parser.add_argument("--base-dir", type=Path, default=BASE_DIR, help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_diagnostic(base_dir=args.base_dir)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        print(format_text_report(result, verbose=args.verbose), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
