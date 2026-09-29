# -*- coding: utf-8 -*-
"""Streamlit Dashboard V2/V3 for Crypto Multi-Coin Scanner production monitoring.

The dashboard is read-only: it reads local CSV logs and never sends Telegram,
places trades, calls exchange APIs, or mutates log files.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any
import csv
import json
import os
import re
import shutil
import subprocess
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv

from core.analytics_reporting import load_csv_safely
from core.binance_execution_truth_collector import report_records
from core.binance_execution_truth_health import HealthConfig, evaluate_health, probe_systemd
from core.performance_analytics_v2 import build_performance_v2, generate_performance_warnings
from core.signal_identity import identity_from_record
from performance_report import build_report, estimate_r, normalize, sent_signals

BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
REPORTS_DIR = BASE_DIR / "reports"
STATE_DIR = BASE_DIR / "state"
DASHBOARD_HTML = REPORTS_DIR / "dashboard.html"
STALE_DATA_THRESHOLD_MINUTES = 90
DISPLAY_TZ = ZoneInfo("Asia/Bangkok")
DISPLAY_TZ_LABEL = "ICT"

DATA_PATHS = {
    "signals": LOGS_DIR / "signals.csv",
    "daily_performance": LOGS_DIR / "daily_performance.csv",
    "symbol_performance": LOGS_DIR / "symbol_performance.csv",
    "source_performance": LOGS_DIR / "source_performance.csv",
    "position_management": LOGS_DIR / "position_management.csv",
    "external_signals": LOGS_DIR / "external_signals.csv",
    "symbol_performance_v2": LOGS_DIR / "symbol_performance_v2.csv",
    "session_performance_v2": LOGS_DIR / "session_performance_v2.csv",
    "direction_performance_v2": LOGS_DIR / "direction_performance_v2.csv",
    "tier_performance_v2": LOGS_DIR / "tier_performance_v2.csv",
    "performance_warnings": LOGS_DIR / "performance_warnings.csv",
}

LOG_PATHS = [
    LOGS_DIR / "cornix_agent.log",
    BASE_DIR / "cornix_agent.log",
]

EXECUTION_PATHS = {
    "csv": LOGS_DIR / "binance_execution_truth_v1.csv",
    "state": STATE_DIR / "binance_execution_truth_v1.json",
    "evidence": LOGS_DIR / "binance_execution_truth_v1.evidence.json",
    "lock": LOGS_DIR / "binance_execution_truth_v1.lock",
}


def _series(df: pd.DataFrame, column: str, default: Any = "") -> pd.Series:
    if column in df.columns:
        return df[column].fillna(default)
    return pd.Series([default] * len(df), index=df.index)


def _numeric_series(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series([float("nan")] * len(df), index=df.index)
    values = pd.to_numeric(df[column], errors="coerce")
    return values.replace([float("inf"), float("-inf")], pd.NA)


def _timestamp_series(df: pd.DataFrame, column: str = "timestamp") -> pd.Series:
    if column not in df.columns:
        return pd.Series(pd.NaT, index=df.index)
    return pd.to_datetime(df[column], utc=True, errors="coerce")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_dt(value: Any) -> str:
    parsed = pd.to_datetime(pd.Series([value]), utc=True, errors="coerce").iloc[0]
    if pd.isna(parsed):
        return "N/A"
    local = parsed.tz_convert(DISPLAY_TZ)
    return f"{local.strftime('%d %b %Y')}<br>{local.strftime('%H:%M')} {DISPLAY_TZ_LABEL}"


def _fmt_dt_plain(value: Any) -> str:
    return _fmt_dt(value).replace("<br>", " ")


def _fmt_time(value: Any) -> str:
    parsed = pd.to_datetime(pd.Series([value]), utc=True, errors="coerce").iloc[0]
    if pd.isna(parsed):
        return "Unavailable"
    return parsed.tz_convert(DISPLAY_TZ).strftime("%H:%M")


def _fmt_date(value: Any) -> str:
    parsed = pd.to_datetime(pd.Series([value]), utc=True, errors="coerce").iloc[0]
    if pd.isna(parsed):
        return "Unavailable"
    return parsed.tz_convert(DISPLAY_TZ).strftime("%d %b %Y")


def _fmt_utc_compact(value: Any) -> str:
    parsed = pd.to_datetime(pd.Series([value]), utc=True, errors="coerce").iloc[0]
    if pd.isna(parsed):
        return "Unavailable"
    return f"{parsed.strftime('%Y-%m-%d')}<br>{parsed.strftime('%H:%M:%SZ')}"


def _relative_time(value: Any, now: datetime | None = None) -> str:
    parsed = pd.to_datetime(pd.Series([value]), utc=True, errors="coerce").iloc[0]
    if pd.isna(parsed):
        return "Unavailable"
    reference = pd.Timestamp(now or _now_utc()).tz_convert("UTC")
    seconds = int((reference - parsed).total_seconds())
    future = seconds < 0
    seconds = abs(seconds)
    if seconds < 60:
        text = "just now"
    elif seconds < 3600:
        value_minutes = seconds // 60
        text = f"{value_minutes} min"
    elif seconds < 86400:
        value_hours = seconds // 3600
        text = f"{value_hours}h"
    else:
        value_days = seconds // 86400
        text = f"{value_days}d"
    if text == "just now":
        return text
    return f"in {text}" if future else f"{text} ago"


def _signed_number(value: Any, suffix: str = "", decimals: int = 2) -> str:
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric) or numeric in [float("inf"), float("-inf")]:
        return "Waiting for evidence"
    return f"{float(numeric):+,.{decimals}f}{suffix}"


def _local_day_frame(df: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    timestamps = _timestamp_series(df)
    local_dates = timestamps.dt.tz_convert(DISPLAY_TZ).dt.date
    reference = (now or _now_utc()).astimezone(DISPLAY_TZ).date()
    return df[local_dates == reference].copy()


def _fmt_duration_hours(value: Any) -> str:
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric) or numeric in [float("inf"), float("-inf")]:
        return "N/A"
    minutes = int(round(float(numeric) * 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins:02d}m" if mins else f"{hours}h"


def _fmt_percent(value: Any) -> str:
    if value is None:
        return "N/A"
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric) or numeric in [float("inf"), float("-inf")]:
        return "N/A"
    return f"{float(numeric):.1f}%"


def _fmt_price(value: Any) -> str:
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric) or numeric in [float("inf"), float("-inf")]:
        return "N/A"
    return f"{float(numeric):.6g}"


def _latest_file_time(paths: list[Path]) -> datetime | None:
    existing = [path for path in paths if path.exists()]
    if not existing:
        return None
    latest = max(path.stat().st_mtime for path in existing)
    return datetime.fromtimestamp(latest, tz=timezone.utc)


def _systemctl_state(service: str = "crypto-scanner.service") -> str | None:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    state = (result.stdout or result.stderr or "").strip().lower()
    if state == "active":
        return "RUNNING"
    if state in {"inactive", "failed", "deactivating"}:
        return "STOPPED"
    if state:
        return state.upper()
    return None


def _dashboard_systemd_runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Bound collector health probes so a broken systemctl cannot stall HOME."""
    kwargs["timeout"] = min(float(kwargs.get("timeout", 2)), 2.0)
    return subprocess.run(args, **kwargs)


def _ensure_columns(df: pd.DataFrame, defaults: dict[str, Any]) -> pd.DataFrame:
    data = df.copy()
    for column, default in defaults.items():
        if column not in data.columns:
            data[column] = default
    return data


def _report_safe_signals(df: pd.DataFrame) -> pd.DataFrame:
    """Return a normalized signals frame that is safe for report builders."""
    raw = df.copy()
    source_missing = "source" not in raw.columns
    defaults = {
        "timestamp": "",
        "symbol": "",
        "side": "",
        "source": "Unknown",
        "watchlist_tier": "-",
        "market_session": "Other",
        "entry": "",
        "stop_loss": "",
        "tp1": "",
        "tp2": "",
        "tp3": "",
        "risk_reward": "",
        "result": "OPEN",
        "hit_target": "",
        "signal_status": "sent",
        "pnl_percent": "",
        "closed_at": "",
        "max_profit_pct": "",
        "max_drawdown_pct": "",
        "raw_score": "",
        "setup_strength": "",
        "score_bucket": "-",
        "current_price": "",
        "tp1_alert_sent": 0,
        "breakeven_recommended": 0,
        "position_management_stage": "",
        "position_recommendation": "",
        "position_reason": "",
        "signal_id": "",
    }
    data = _ensure_columns(raw, defaults)
    data = normalize(data)
    for column, default in defaults.items():
        if column not in data.columns:
            data[column] = default
    if "source" in data.columns:
        fallback = "Unknown" if source_missing else "Unknown"
        data["source"] = data["source"].fillna(fallback).replace("", fallback).astype(str)
        data.loc[data["source"].str.strip().eq(""), "source"] = fallback
        data["source"] = data["source"].replace({"external": "External", "scanner": "Scanner", "refiner": "Refiner"})
    return data


def load_dashboard_data(paths: dict[str, Path] | None = None) -> dict[str, pd.DataFrame]:
    source_paths = paths or DATA_PATHS
    data = {name: load_csv_safely(path) for name, path in source_paths.items()}
    signals = _report_safe_signals(data.get("signals", pd.DataFrame()))
    if "source" not in signals.columns:
        signals["source"] = "Unknown"
    signals["source"] = signals["source"].fillna("Unknown").replace("", "Unknown").astype(str)
    if "score_bucket" not in signals.columns:
        signals["score_bucket"] = "-"
    if "setup_strength" not in signals.columns:
        signals["setup_strength"] = signals["confidence"] if "confidence" in signals.columns else ""
    if "raw_score" not in signals.columns:
        signals["raw_score"] = signals["score"] if "score" in signals.columns else ""
    for column in ["raw_score", "setup_strength", "max_profit_pct", "max_drawdown_pct"]:
        if column in signals.columns:
            signals[column] = pd.to_numeric(signals[column], errors="coerce")
    data["signals"] = signals
    data["sent"] = sent_signals(signals)
    return data


def _empty_execution_snapshot(paths: dict[str, Path], message: str = "Execution artifacts unavailable") -> dict[str, Any]:
    return {
        "available": False,
        "collector_health": "UNAVAILABLE",
        "message": message,
        "last_success_utc": None,
        "last_success_age_seconds": None,
        "matched": None,
        "authoritative_n": None,
        "gross_realized_pnl_usdt": None,
        "commission_usdt": None,
        "execution_pnl_usdt": None,
        "gross_r": None,
        "execution_net_r": None,
        "invalid": None,
        "ambiguous": None,
        "funding_rows": None,
        "funding_usdt": None,
        "funding_adjusted_pnl_usdt": None,
        "pending_reconciliation": None,
        "boundary": None,
        "high_water_utc": None,
        "state_age_seconds": None,
        "provenance": {},
        "rows": [],
        "checks": {},
        "artifacts": {name: path.exists() for name, path in paths.items()},
    }


def load_execution_truth_snapshot(
    paths: dict[str, Path] | None = None,
    *,
    now: datetime | None = None,
    health_result: Any | None = None,
) -> dict[str, Any]:
    """Read collector artifacts without calling Binance or mutating runtime state."""
    selected = {**EXECUTION_PATHS, **(paths or {})}
    snapshot = _empty_execution_snapshot(selected)
    core_artifacts = [selected["csv"], selected["state"], selected["evidence"]]
    if not any(path.exists() for path in core_artifacts):
        return snapshot

    rows: list[dict[str, str]] = []
    state: dict[str, Any] = {}
    errors: list[str] = []
    try:
        with selected["csv"].open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error) as exc:
        errors.append(f"CSV: {type(exc).__name__}: {exc}")
    try:
        loaded_state = json.loads(selected["state"].read_text(encoding="utf-8"))
        if isinstance(loaded_state, dict):
            state = loaded_state
        else:
            errors.append("State: root is not an object")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"State: {type(exc).__name__}: {exc}")

    report: dict[str, Any] | None = None
    if not errors:
        try:
            report = report_records(rows, str(state.get("prospective_start_utc") or "UNKNOWN"))
        except (AssertionError, KeyError, TypeError, ValueError) as exc:
            errors.append(f"Accounting report: {type(exc).__name__}: {exc}")

    if health_result is None:
        try:
            health_result = evaluate_health(
                HealthConfig(
                    state_path=selected["state"],
                    csv_path=selected["csv"],
                    evidence_path=selected["evidence"],
                    lock_path=selected["lock"],
                ),
                now=now,
                systemd_probe=lambda timer, service: probe_systemd(
                    timer,
                    service,
                    runner=_dashboard_systemd_runner,
                ),
            )
        except Exception as exc:  # Diagnostic failure must never take down the dashboard.
            errors.append(f"Health: {type(exc).__name__}: {exc}")

    checks: dict[str, Any] = {}
    health_metrics: dict[str, Any] = {}
    health_status = "FAILED"
    if health_result is not None:
        raw_health = health_result.as_dict() if hasattr(health_result, "as_dict") else dict(health_result)
        checks = raw_health.get("checks", {})
        health_metrics = raw_health.get("metrics", {})
        health_status = {"OK": "HEALTHY", "WARNING": "WARNING", "FAIL": "FAILED"}.get(
            str(raw_health.get("overall", "FAIL")).upper(), "FAILED"
        )
    artifact_check = checks.get("artifacts", {})
    artifact_status = str(artifact_check.get("status", "")).upper() if isinstance(artifact_check, dict) else ""
    if artifact_status == "FAIL":
        detail = str(artifact_check.get("detail") or "collector artifacts failed validation")
        errors.append(f"Artifacts: {detail}")
        report = None

    snapshot.update(
        {
            "available": report is not None,
            "collector_health": health_status,
            "message": "; ".join(errors) if errors else "Collector artifacts loaded",
            "last_success_utc": health_metrics.get("last_success_utc") or state.get("last_successful_collection_utc"),
            "last_success_age_seconds": health_metrics.get("last_success_age_seconds"),
            "invalid": health_metrics.get("invalid_count"),
            "ambiguous": health_metrics.get("ambiguous_count"),
            "boundary": state.get("prospective_start_utc"),
            "high_water_utc": (state.get("endpoint_high_water") or {}).get("scanned_through_utc")
            if isinstance(state.get("endpoint_high_water"), dict)
            else None,
            "state_age_seconds": health_metrics.get("state_age_seconds"),
            "provenance": pd.Series(
                [
                    row.get("lifecycle_provenance")
                    for row in rows
                    if row.get("data_source") == "BINANCE_USDM_PROSPECTIVE"
                    and row.get("lifecycle_provenance")
                ],
                dtype="object",
            ).value_counts().to_dict(),
            "rows": rows,
            "checks": checks,
            "artifacts": {name: path.exists() for name, path in selected.items()},
        }
    )
    if report is None:
        return snapshot

    authoritative = report["execution_complete_matched"]
    funding = report["funding_reconciled_matched"]
    snapshot.update(
        {
            "matched": report["matched_scanner_lifecycles"],
            "authoritative_n": authoritative["rows"],
            "gross_realized_pnl_usdt": authoritative["gross_realized_pnl_usdt"],
            "commission_usdt": authoritative["commission_usdt"],
            "execution_pnl_usdt": authoritative["execution_pnl_usdt"],
            "gross_r": authoritative["gross_realized_r"],
            "execution_net_r": authoritative["execution_r"],
            "invalid": snapshot["invalid"] if snapshot["invalid"] is not None else report["invalid_accounting_evidence"]["rows"],
            "ambiguous": snapshot["ambiguous"] if snapshot["ambiguous"] is not None else report["ambiguous"],
            "funding_rows": funding["rows"],
            "funding_usdt": funding["funding_usdt"],
            "funding_adjusted_pnl_usdt": funding["funding_adjusted_pnl_usdt"],
            "pending_reconciliation": report["pending_execution_accounting"]["rows"],
        }
    )
    return snapshot


def execution_status_for_signal(row: pd.Series | dict[str, Any], execution_rows: list[dict[str, str]]) -> str:
    key = identity_from_record(row).canonical_key
    if not key:
        return "Unavailable"
    matches = [item for item in execution_rows if item.get("canonical_signal_key") == key]
    if not matches:
        for item in execution_rows:
            try:
                candidates = json.loads(item.get("candidate_signal_keys") or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(candidates, list) and key in candidates:
                matches.append(item)
    if not matches:
        return "Waiting"
    statuses = {str(item.get("match_status") or "UNKNOWN").upper() for item in matches}
    if "AMBIGUOUS" in statuses:
        return "AMBIGUOUS"
    if len(statuses) > 1:
        return "AMBIGUOUS"
    return statuses.pop()


def _empty_report() -> dict[str, Any]:
    return {
        "total_sent_signals": 0,
        "closed_signals": 0,
        "open_signals": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "tp1_hits": 0,
        "tp2_hits": 0,
        "tp3_hits": 0,
        "sl_hits": 0,
        "net_r_estimate": 0.0,
    }


def _safe_build_report(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty:
        return _empty_report()
    try:
        report = build_report(df)
    except (KeyError, ValueError, TypeError):
        return _empty_report()
    return {**_empty_report(), **report}


def scanner_status_snapshot(paths: dict[str, Path] | None = None) -> dict[str, Any]:
    source_paths = paths or DATA_PATHS
    signals_path = source_paths.get("signals", DATA_PATHS["signals"])
    latest = _latest_file_time([signals_path, *LOG_PATHS])
    age_minutes = (_now_utc() - latest).total_seconds() / 60 if latest else None
    service_state = _systemctl_state()
    if service_state:
        status = service_state
    elif latest is None:
        status = "UNKNOWN"
    elif age_minutes is not None and age_minutes > STALE_DATA_THRESHOLD_MINUTES:
        status = "DATA STALE"
    else:
        status = "UNKNOWN"
    return {
        "status": status,
        "last_scan": latest.isoformat() if latest else "N/A",
        "next_scan": (latest + timedelta(hours=1)).isoformat() if latest else "N/A",
        "server_time": _now_utc().isoformat(),
        "timezone": "Asia/Bangkok",
        "data_age_minutes": round(age_minutes, 1) if age_minutes is not None else None,
        "stale_warning": (
            f"Latest scanner data is older than {STALE_DATA_THRESHOLD_MINUTES} minutes"
            if age_minutes is not None and age_minutes > STALE_DATA_THRESHOLD_MINUTES
            else "N/A"
        ),
    }


def overview_metrics(
    data: dict[str, pd.DataFrame],
    paths: dict[str, Path] | None = None,
    status_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sent = data.get("sent", pd.DataFrame())
    active = active_positions(sent)
    now = _now_utc()
    ts = _timestamp_series(sent)
    today = ts.dt.date == now.date() if len(sent) else pd.Series(dtype=bool)
    last_7 = ts >= (now - timedelta(days=7)) if len(sent) else pd.Series(dtype=bool)
    closed_7 = _closed(sent[last_7]) if len(sent) else pd.DataFrame()
    wins_7 = int((_series(closed_7, "result").astype(str).str.upper() == "WIN").sum()) if not closed_7.empty else 0
    rr = _numeric_series(sent, "risk_reward").dropna()
    latest_regime = "N/A"
    if "market_regime" in sent.columns and not sent.empty:
        regimes = sent["market_regime"].dropna().astype(str)
        regimes = regimes[regimes.str.strip() != ""]
        if not regimes.empty:
            latest_regime = regimes.iloc[-1]
    snapshot = status_snapshot or scanner_status_snapshot(paths)
    return {
        **snapshot,
        "market_regime": latest_regime,
        "active_positions": int(len(active)),
        "signals_today": int(today.sum()) if len(sent) else 0,
        "win_rate_7d": wins_7 / len(closed_7) * 100 if len(closed_7) else None,
        "average_rr": float(rr.mean()) if not rr.empty else None,
    }


def _position_age_hours(row: pd.Series, now: datetime | None = None) -> float | None:
    parsed = pd.to_datetime(pd.Series([row.get("timestamp")]), utc=True, errors="coerce").iloc[0]
    if pd.isna(parsed):
        return None
    return max(0.0, ((_now_utc() if now is None else now) - parsed.to_pydatetime()).total_seconds() / 3600)


def _progress_to_tp1(row: pd.Series) -> float | None:
    side = str(row.get("side", "")).upper()
    entry = pd.to_numeric(pd.Series([row.get("entry")]), errors="coerce").iloc[0]
    tp1 = pd.to_numeric(pd.Series([row.get("tp1")]), errors="coerce").iloc[0]
    current = pd.to_numeric(pd.Series([row.get("current_price")]), errors="coerce").iloc[0]
    if (
        pd.isna(entry) or pd.isna(tp1) or pd.isna(current)
        or entry in [float("inf"), float("-inf")]
        or tp1 in [float("inf"), float("-inf")]
        or current in [float("inf"), float("-inf")]
        or tp1 == entry
    ):
        return None
    if side == "SHORT":
        progress = (entry - current) / (entry - tp1) * 100
    else:
        progress = (current - entry) / (tp1 - entry) * 100
    return float(max(0.0, min(100.0, progress)))


def _distance_pct(from_price: Any, to_price: Any) -> float | None:
    start = pd.to_numeric(pd.Series([from_price]), errors="coerce").iloc[0]
    target = pd.to_numeric(pd.Series([to_price]), errors="coerce").iloc[0]
    if (
        pd.isna(start) or pd.isna(target)
        or start in [float("inf"), float("-inf")]
        or target in [float("inf"), float("-inf")]
        or start == 0
    ):
        return None
    return float(abs(target - start) / abs(start) * 100)


def _current_pnl_pct(row: pd.Series) -> float | None:
    side = str(row.get("side", "")).upper()
    entry = pd.to_numeric(pd.Series([row.get("entry")]), errors="coerce").iloc[0]
    current = pd.to_numeric(pd.Series([row.get("current_price")]), errors="coerce").iloc[0]
    if (
        pd.isna(entry) or pd.isna(current)
        or entry in [float("inf"), float("-inf")]
        or current in [float("inf"), float("-inf")]
        or entry <= 0 or current <= 0
    ):
        return None
    if side == "SHORT":
        return float((entry - current) / entry * 100)
    return float((current - entry) / entry * 100)


def _tp1_already_reached(row: pd.Series) -> bool:
    hit = str(row.get("hit_target", "")).strip().upper()
    if hit in {"TP1", "TP2", "TP3", "1", "2", "3"}:
        return True
    for column in ["tp1_alert_sent", "breakeven_recommended"]:
        value = str(row.get(column, "")).strip().lower()
        if value in {"1", "true", "yes", "y"}:
            return True
    stage = str(row.get("position_management_stage", "")).upper()
    return "TP1_REACHED" in stage


def active_positions(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=df.columns)
    source = _report_safe_signals(df)
    status = _series(source, "signal_status").astype(str).str.lower()
    result = _series(source, "result").astype(str).str.upper()
    data = source[(status == "sent") & (result == "OPEN")].copy()
    if data.empty:
        return data
    data["parsed_ts"] = _timestamp_series(data)
    data = data.sort_values("parsed_ts", ascending=False).drop_duplicates("symbol", keep="first")
    if "current_price" not in data.columns:
        data["current_price"] = pd.NA
    if "tp3" not in data.columns:
        data["tp3"] = pd.NA
    if "source" not in data.columns:
        data["source"] = "Unknown"
    if "signal_id" not in data.columns:
        data["signal_id"] = _series(data, "timestamp")
    data["time_open_hours"] = data.apply(_position_age_hours, axis=1)
    data["progress_to_tp1_pct"] = data.apply(_progress_to_tp1, axis=1)
    data["current_pnl_pct"] = data.apply(_current_pnl_pct, axis=1)
    data["distance_to_tp1_pct"] = data.apply(
        lambda row: row.get("distance_to_tp1_pct") if "distance_to_tp1_pct" in data.columns and pd.notna(row.get("distance_to_tp1_pct")) else _distance_pct(row.get("current_price"), row.get("tp1")),
        axis=1,
    )
    data["distance_to_sl_pct"] = data.apply(
        lambda row: row.get("distance_to_sl_pct") if "distance_to_sl_pct" in data.columns and pd.notna(row.get("distance_to_sl_pct")) else _distance_pct(row.get("current_price"), row.get("stop_loss")),
        axis=1,
    )
    data["dashboard_status"] = data.apply(position_status, axis=1)
    return data


def position_status(row: pd.Series) -> str:
    if _tp1_already_reached(row):
        return "positive"
    distance_sl = pd.to_numeric(pd.Series([row.get("distance_to_sl_pct")]), errors="coerce").iloc[0]
    if pd.notna(distance_sl) and float(distance_sl) <= 0.75:
        return "danger"
    age = row.get("time_open_hours")
    progress = row.get("progress_to_tp1_pct")
    if age is not None and pd.notna(age) and float(age) >= 6 and (progress is None or pd.isna(progress) or float(progress) < 100):
        return "warning"
    pnl = pd.to_numeric(pd.Series([row.get("current_pnl_pct")]), errors="coerce").iloc[0]
    if pd.notna(pnl) and float(pnl) > 0:
        return "positive"
    return "neutral"


def position_review_queue(df: pd.DataFrame) -> pd.DataFrame:
    active = active_positions(df)
    if active.empty:
        return pd.DataFrame(
            columns=[
                "symbol", "time_open_hours", "current_pnl_pct", "distance_to_tp1_pct",
                "distance_to_sl_pct", "same_direction_signal", "opposite_direction_signal",
                "review_reason", "recommendation",
            ]
        )
    rows: list[dict[str, Any]] = []
    latest_by_symbol = df.copy()
    latest_by_symbol["parsed_ts"] = _timestamp_series(latest_by_symbol)
    for _, position in active.iterrows():
        symbol = str(position.get("symbol", ""))
        side = str(position.get("side", "")).upper()
        history = latest_by_symbol[_series(latest_by_symbol, "symbol").astype(str) == symbol]
        later = history[history["parsed_ts"] > pd.to_datetime(position.get("timestamp"), utc=True, errors="coerce")]
        later_side = _series(later, "side").astype(str).str.upper() if not later.empty else pd.Series(dtype=str)
        same = bool((later_side == side).any()) if not later.empty else False
        opposite = bool((later_side.isin({"LONG", "SHORT"}) & (later_side != side)).any()) if not later.empty else False
        reasons = []
        age = position.get("time_open_hours")
        progress = position.get("progress_to_tp1_pct")
        tp1_reached = _tp1_already_reached(position) or (progress is not None and pd.notna(progress) and float(progress) >= 100)
        if age is not None and pd.notna(age) and float(age) >= 6 and not tp1_reached:
            reasons.append("open >6h and TP1 not reached")
        if same:
            reasons.append("same direction signal exists")
        if opposite:
            reasons.append("opposite direction signal exists")
        if str(position.get("dashboard_status")) == "danger":
            reasons.append("near SL")
        recommendation = position.get("position_recommendation") or position.get("position_management_stage") or "REVIEW REQUIRED"
        if not str(recommendation).strip() or str(recommendation).strip().upper() in {"N/A", "NAN", "NONE"}:
            recommendation = "REVIEW REQUIRED"
        if reasons:
            rows.append(
                {
                    "symbol": symbol,
                    "time_open_hours": age,
                    "current_pnl_pct": position.get("current_pnl_pct"),
                    "distance_to_tp1_pct": position.get("distance_to_tp1_pct"),
                    "distance_to_sl_pct": position.get("distance_to_sl_pct"),
                    "same_direction_signal": "YES" if same else "NO",
                    "opposite_direction_signal": "YES" if opposite else "NO",
                    "review_reason": "; ".join(reasons),
                    "recommendation": recommendation,
                }
            )
    return pd.DataFrame(rows)


def signal_funnel(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty:
        return {
            "Total Coins Scanned": "N/A",
            "Candidates Found": 0,
            "Rejected by Rule Filter": 0,
            "Rejected by RR": 0,
            "Rejected by Confidence": 0,
            "Sent to AI": 0,
            "AI Approved": "N/A",
            "AI Rejected": "N/A",
            "Signals Sent": 0,
            "Outcome Pending": 0,
        }
    status = _series(df, "signal_status").astype(str).str.lower()
    reason = _series(df, "skip_reason").astype(str).str.lower()
    ai_summary = _series(df, "ai_summary").astype(str)
    result = _series(df, "result").astype(str).str.upper()
    return {
        "Total Coins Scanned": "N/A",
        "Candidates Found": int(len(df)),
        "Rejected by Rule Filter": int(status.str.contains("skipped|filter|guard|cooldown|correlation", regex=True).sum()),
        "Rejected by RR": int(reason.str.contains("rr|risk_reward|risk reward", regex=True).sum()),
        "Rejected by Confidence": int(reason.str.contains("confidence|strength|score", regex=True).sum()),
        "Sent to AI": int(ai_summary.str.strip().ne("").sum()),
        "AI Approved": "N/A",
        "AI Rejected": "N/A",
        "Signals Sent": int((status == "sent").sum()),
        "Outcome Pending": int(((status == "sent") & result.eq("OPEN")).sum()),
    }


def performance_windows(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    now = _now_utc()
    ts = _timestamp_series(df)
    windows = {
        "Today": df[ts.dt.date == now.date()] if len(df) else df,
        "7 Days": df[ts >= now - timedelta(days=7)] if len(df) else df,
        "30 Days": df[ts >= now - timedelta(days=30)] if len(df) else df,
    }
    rows = []
    source_values: list[str] = []
    if "source" in df.columns and not df.empty:
        source_values = sorted(
            value for value in _series(df, "source", "Unknown").astype(str).replace("", "Unknown").unique().tolist()
            if value and value.lower() not in {"nan", "none"}
        )
    if not source_values:
        source_values = ["Unknown"]
    for source in ["All", *source_values]:
        source_df = df if source == "All" or "source" not in df.columns else df[_series(df, "source", "Unknown").astype(str).eq(source)]
        for label, group in windows.items():
            subset = source_df.loc[group.index.intersection(source_df.index)] if source != "all" else group
            closed = _closed(subset)
            wins = int((_series(closed, "result").astype(str).str.upper() == "WIN").sum()) if not closed.empty else 0
            losses = int((_series(closed, "result").astype(str).str.upper() == "LOSS").sum()) if not closed.empty else 0
            rr = _numeric_series(subset, "risk_reward").dropna()
            realized = closed.apply(estimate_r, axis=1) if not closed.empty else pd.Series(dtype=float)
            positive = realized[realized > 0]
            negative = realized[realized < 0]
            profit_factor = float(positive.sum() / abs(negative.sum())) if not negative.empty and abs(negative.sum()) > 0 else None
            best_symbol, worst_symbol = _best_worst_by_win_rate(subset, "symbol")
            best_session, worst_session = _best_worst_by_win_rate(subset, "market_session")
            rows.append(
                {
                    "source": source,
                    "window": label,
                    "total_signals": int(len(subset)),
                    "wins": wins,
                    "losses": losses,
                    "open": int((_series(subset, "result").astype(str).str.upper() == "OPEN").sum()) if not subset.empty else 0,
                    "win_rate": wins / len(closed) * 100 if len(closed) else None,
                    "average_rr": float(rr.mean()) if not rr.empty else None,
                    "average_realized_r": float(realized.mean()) if not realized.empty else None,
                    "total_realized_r": float(realized.sum()) if not realized.empty else 0.0,
                    "profit_factor": profit_factor,
                    "best_symbol": best_symbol,
                    "worst_symbol": worst_symbol,
                    "best_session": best_session,
                    "worst_session": worst_session,
                }
            )
    return {"performance": pd.DataFrame(rows)}


def health_snapshot(
    paths: dict[str, Path] | None = None,
    status_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    load_dotenv(BASE_DIR / ".env")
    source_paths = paths or DATA_PATHS
    signals_path = source_paths.get("signals", DATA_PATHS["signals"])
    latest_log = _latest_file_time(LOG_PATHS)
    log_errors = dashboard_log_timeline(limit=500)
    today = _now_utc().date()
    error_today = 0
    if not log_errors.empty and "timestamp" in log_errors.columns:
        parsed = pd.to_datetime(log_errors["timestamp"], utc=True, errors="coerce")
        error_today = int(((parsed.dt.date == today) & log_errors["level"].isin(["ERROR", "WARNING"])).sum())
    try:
        disk = shutil.disk_usage(BASE_DIR)
        disk_usage = f"{(disk.used / disk.total * 100):.1f}%"
    except OSError:
        disk_usage = "N/A"
    status = status_snapshot or scanner_status_snapshot(source_paths)
    return {
        "Process Status": status["status"],
        "Data Freshness": status["stale_warning"],
        "Last Successful Scan": status["last_scan"],
        "Scan Duration": "N/A",
        "Binance API Status": "N/A",
        "Binance Latency": "N/A",
        "Gemini API Status": "Configured" if os.getenv("GEMINI_API_KEY") else "N/A",
        "Gemini Latency": "N/A",
        "Telegram Status": "Configured" if os.getenv("TELEGRAM_BOT_TOKEN") else "N/A",
        "Database Status": "PASS" if signals_path.exists() else "N/A",
        "Disk Usage": disk_usage,
        "CPU Usage": "N/A",
        "RAM Usage": "N/A",
        "Last Error": latest_error_message(log_errors),
        "Error Count Today": error_today,
        "Latest Log Time": latest_log.isoformat() if latest_log else "N/A",
    }


def latest_error_message(events: pd.DataFrame) -> str:
    if events.empty or "level" not in events.columns:
        return "N/A"
    errors = events[events["level"].isin(["ERROR", "WARNING"])]
    if errors.empty:
        return "N/A"
    return str(errors.iloc[-1].get("message", "N/A"))[:180]


def dashboard_log_timeline(limit: int = 200) -> pd.DataFrame:
    path = next((item for item in LOG_PATHS if item.exists()), None)
    if path is None:
        return pd.DataFrame(columns=["timestamp", "level", "source", "event_type", "status", "message", "raw"])
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except Exception:
        return pd.DataFrame(columns=["timestamp", "level", "source", "event_type", "status", "message", "raw"])
    rows = []
    pattern = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} [^|]+)\|\s*(?P<level>[A-Z]+)\s*\|\s*(?P<msg>.*)$")
    for raw in lines:
        match = pattern.match(raw)
        timestamp = match.group("ts").strip() if match else ""
        level = match.group("level").strip() if match else "INFO"
        message = match.group("msg").strip() if match else raw.strip()
        lower = message.lower()
        if "approved" in lower or "signal sent" in lower:
            status = "Approved"
        elif "reject" in lower or "skip" in lower:
            status = "Rejected"
        elif "error" in lower or level == "ERROR":
            status = "Error"
        else:
            status = "Info"
        if "gemini" in lower:
            event_type = "AI"
        elif "outcome" in lower or "tp" in lower or "sl" in lower:
            event_type = "Outcome"
        elif "telegram" in lower:
            event_type = "Telegram"
        elif "external" in lower:
            event_type = "External"
        else:
            event_type = "Scanner"
        rows.append(
            {
                "timestamp": timestamp,
                "level": level,
                "source": event_type,
                "event_type": event_type,
                "status": status,
                "message": message[:220],
                "raw": raw,
            }
        )
    return pd.DataFrame(rows)


def daily_summary_panel(df: pd.DataFrame) -> dict[str, Any]:
    now = _now_utc()
    ts = _timestamp_series(df)
    today_df = df[ts.dt.date == now.date()] if len(df) else df
    closed = _closed(today_df)
    wins = int((_series(closed, "result").astype(str).str.upper() == "WIN").sum()) if not closed.empty else 0
    losses = int((_series(closed, "result").astype(str).str.upper() == "LOSS").sum()) if not closed.empty else 0
    open_count = int((_series(today_df, "result").astype(str).str.upper() == "OPEN").sum()) if not today_df.empty else 0
    symbol_perf = performance_table(df, "symbol")
    avoid = []
    strong = []
    if not symbol_perf.empty:
        strong = symbol_perf.sort_values(["net_r", "win_rate"], ascending=[False, False]).head(3)["symbol"].astype(str).tolist()
        avoid = symbol_perf[(symbol_perf["closed"] >= 5) & (symbol_perf["net_r"] < 0)].sort_values("net_r").head(3)["symbol"].astype(str).tolist()
    health = health_snapshot()
    return {
        "Signals Today": int(len(today_df)),
        "Win / Loss / Open": f"{wins}/{losses}/{open_count}",
        "Total R": f"{_net_r(today_df):.2f}R",
        "Strongest Symbols": ", ".join(strong) if strong else "N/A",
        "Symbols To Avoid": ", ".join(avoid) if avoid else "N/A",
        "Positions To Review": int(len(position_review_queue(df))),
        "Scanner Health Summary": health["Process Status"],
    }


def _closed(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "result" not in df.columns:
        return pd.DataFrame(columns=df.columns)
    return df[df["result"].isin(["WIN", "LOSS"])].copy()


def _hit_series(df: pd.DataFrame) -> pd.Series:
    if "hit_target" not in df.columns:
        return pd.Series([""] * len(df), index=df.index)
    return df["hit_target"].fillna("").astype(str).str.upper()


def _numeric_mean(df: pd.DataFrame, column: str) -> float | None:
    if df.empty or column not in df.columns:
        return None
    values = pd.to_numeric(df[column], errors="coerce").dropna()
    if values.empty:
        return None
    return float(values.mean())


def _numeric_max(df: pd.DataFrame, column: str) -> float | None:
    if df.empty or column not in df.columns:
        return None
    values = pd.to_numeric(df[column], errors="coerce").dropna()
    if values.empty:
        return None
    return float(values.max())


def _numeric_min(df: pd.DataFrame, column: str) -> float | None:
    if df.empty or column not in df.columns:
        return None
    values = pd.to_numeric(df[column], errors="coerce").dropna()
    if values.empty:
        return None
    return float(values.min())


def _holding_minutes(df: pd.DataFrame) -> pd.Series:
    if df.empty or "timestamp" not in df.columns or "closed_at" not in df.columns:
        return pd.Series(dtype=float)
    start = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    end = pd.to_datetime(df["closed_at"], utc=True, errors="coerce")
    return ((end - start).dt.total_seconds() / 60).clip(lower=0)


def _net_r(df: pd.DataFrame) -> float:
    closed = _closed(df)
    if closed.empty:
        return 0.0
    return float(closed.apply(estimate_r, axis=1).sum())


def _best_worst_by_win_rate(df: pd.DataFrame, column: str) -> tuple[str, str]:
    table = performance_table(df, column)
    if table.empty:
        return "-", "-"
    filtered = table[table["closed"] > 0].copy()
    if filtered.empty:
        return "-", "-"
    best = filtered.sort_values(["win_rate", "closed"], ascending=[False, False]).iloc[0]
    worst = filtered.sort_values(["win_rate", "closed"], ascending=[True, False]).iloc[0]
    return str(best[column]), str(worst[column])


def dashboard_kpis(df: pd.DataFrame) -> dict[str, Any]:
    sent = sent_signals(df)
    closed = _closed(sent)
    wins = int((closed["result"] == "WIN").sum()) if not closed.empty else 0
    losses = int((closed["result"] == "LOSS").sum()) if not closed.empty else 0
    hit_target = _hit_series(closed)
    best_symbol, worst_symbol = _best_worst_by_win_rate(sent, "symbol")
    best_tier, worst_tier = _best_worst_by_win_rate(sent, "watchlist_tier")
    best_session, worst_session = _best_worst_by_win_rate(sent, "market_session")
    winners = closed[closed["result"] == "WIN"] if not closed.empty else closed
    losers = closed[closed["result"] == "LOSS"] if not closed.empty else closed
    tp_minutes = _holding_minutes(winners)
    sl_minutes = _holding_minutes(losers)
    symbol_perf = performance_table(sent, "symbol")
    best_net_symbol = "-"
    worst_net_symbol = "-"
    if not symbol_perf.empty:
        best_net_symbol = str(symbol_perf.sort_values(["net_r", "closed"], ascending=[False, False]).iloc[0]["symbol"])
        worst_net_symbol = str(symbol_perf.sort_values(["net_r", "closed"], ascending=[True, False]).iloc[0]["symbol"])
    return {
        "Total sent signals": int(len(sent)),
        "Closed trades": int(len(closed)),
        "Wins": wins,
        "Losses": losses,
        "Win rate": wins / len(closed) * 100 if len(closed) else 0.0,
        "Long win rate": _side_win_rate(sent, "LONG"),
        "Short win rate": _side_win_rate(sent, "SHORT"),
        "TP1 hits": int(hit_target.isin(["TP1", "TP2", "TP3", "1", "2", "3"]).sum()),
        "TP2 hits": int(hit_target.isin(["TP2", "TP3", "2", "3"]).sum()),
        "SL hits": int((hit_target == "SL").sum()),
        "TP1 hit rate": hit_target.isin(["TP1", "TP2", "TP3", "1", "2", "3"]).mean() * 100 if len(closed) else 0.0,
        "TP2 hit rate": hit_target.isin(["TP2", "TP3", "2", "3"]).mean() * 100 if len(closed) else 0.0,
        "SL hit rate": (hit_target == "SL").mean() * 100 if len(closed) else 0.0,
        "Avg profit %": _numeric_mean(winners, "max_profit_pct"),
        "Avg loss %": _numeric_mean(losers, "max_drawdown_pct"),
        "Avg max profit %": _numeric_mean(closed, "max_profit_pct"),
        "Avg max drawdown %": _numeric_mean(closed, "max_drawdown_pct"),
        "Avg drawdown winners": _numeric_mean(winners, "max_drawdown_pct"),
        "Avg drawdown losers": _numeric_mean(losers, "max_drawdown_pct"),
        "Max drawdown ever": _numeric_min(closed, "max_drawdown_pct"),
        "Avg time to TP": float(tp_minutes.mean()) if not tp_minutes.dropna().empty else None,
        "Avg time to SL": float(sl_minutes.mean()) if not sl_minutes.dropna().empty else None,
        "Net R": _net_r(sent),
        "Best symbol": best_symbol,
        "Worst symbol": worst_symbol,
        "Best symbol by net R": best_net_symbol,
        "Worst symbol by net R": worst_net_symbol,
        "Best tier": best_tier,
        "Worst tier": worst_tier,
        "Best session": best_session,
        "Worst session": worst_session,
    }


def _side_win_rate(df: pd.DataFrame, side: str) -> float:
    closed = _closed(df)
    if closed.empty or "side" not in closed.columns:
        return 0.0
    selected = closed[closed["side"] == side]
    if selected.empty:
        return 0.0
    return float((selected["result"] == "WIN").mean() * 100)


def performance_table(df: pd.DataFrame, column: str) -> pd.DataFrame:
    if df.empty or column not in df.columns:
        return pd.DataFrame(columns=[column, "signals", "closed", "wins", "losses", "win_rate", "net_r"])
    rows = []
    for key, group in df.groupby(df[column].fillna("-").replace("", "-").astype(str)):
        closed = _closed(group)
        wins = int((closed["result"] == "WIN").sum()) if not closed.empty else 0
        losses = int((closed["result"] == "LOSS").sum()) if not closed.empty else 0
        rows.append(
            {
                column: key,
                "signals": int(len(group)),
                "closed": int(len(closed)),
                "wins": wins,
                "losses": losses,
                "win_rate": wins / len(closed) * 100 if len(closed) else 0.0,
                "net_r": float(closed.apply(estimate_r, axis=1).sum()) if len(closed) else 0.0,
                "avg_drawdown": _numeric_mean(closed, "max_drawdown_pct"),
            }
        )
    if not rows:
        return pd.DataFrame(columns=[column, "signals", "closed", "wins", "losses", "win_rate", "net_r"])
    return pd.DataFrame(rows).sort_values(["win_rate", "closed"], ascending=[False, False])


def bucket_label(value: Any, step: int = 10) -> str:
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric):
        return "Unknown"
    lower = int(numeric // step * step)
    upper = lower + step - 1
    return f"{lower}-{upper}"


def ensure_score_buckets(df: pd.DataFrame) -> pd.DataFrame:
    data = df.copy()
    data["score_range"] = data.get("raw_score", pd.Series(dtype=float)).apply(bucket_label)
    data["confidence_range"] = data.get("setup_strength", pd.Series(dtype=float)).apply(bucket_label)
    return data


def equity_curve(df: pd.DataFrame) -> pd.DataFrame:
    closed = _closed(df)
    if closed.empty:
        return pd.DataFrame(columns=["closed_at", "r", "cumulative_r"])
    data = closed.copy()
    sort_column = "closed_at" if "closed_at" in data.columns else "timestamp"
    data[sort_column] = pd.to_datetime(data[sort_column], utc=True, errors="coerce")
    data = data.dropna(subset=[sort_column]).sort_values(sort_column)
    data["r"] = data.apply(estimate_r, axis=1)
    data["cumulative_r"] = data["r"].cumsum()
    return data[[sort_column, "r", "cumulative_r"]].rename(columns={sort_column: "closed_at"})


def drawdown_curve(df: pd.DataFrame) -> pd.DataFrame:
    curve = equity_curve(df)
    if curve.empty:
        return pd.DataFrame(columns=["closed_at", "cumulative_r", "equity_peak", "drawdown_r"])
    data = curve.copy()
    data["equity_peak"] = data["cumulative_r"].cummax()
    data["drawdown_r"] = data["cumulative_r"] - data["equity_peak"]
    return data[["closed_at", "cumulative_r", "equity_peak", "drawdown_r"]]


def max_drawdown_r(df: pd.DataFrame) -> float:
    drawdown = drawdown_curve(df)
    if drawdown.empty:
        return 0.0
    return float(drawdown["drawdown_r"].min())


def daily_net_r(df: pd.DataFrame) -> pd.DataFrame:
    closed = _closed(df)
    if closed.empty:
        return pd.DataFrame(columns=["date", "net_r"])
    data = closed.copy()
    data["date"] = pd.to_datetime(data["closed_at"] if "closed_at" in data.columns else data["timestamp"], utc=True, errors="coerce").dt.date
    data["r"] = data.apply(estimate_r, axis=1)
    return data.dropna(subset=["date"]).groupby("date", as_index=False)["r"].sum().rename(columns={"r": "net_r"})


def monthly_performance(df: pd.DataFrame) -> pd.DataFrame:
    closed = _closed(df)
    if closed.empty:
        return pd.DataFrame(columns=["month", "closed", "wins", "losses", "win_rate", "net_r", "max_drawdown_r"])
    data = closed.copy()
    date_source = data["closed_at"] if "closed_at" in data.columns else data["timestamp"]
    data["month"] = pd.to_datetime(date_source, utc=True, errors="coerce").dt.strftime("%Y-%m")
    data = data[data["month"].notna() & (data["month"] != "NaT")].copy()
    if data.empty:
        return pd.DataFrame(columns=["month", "closed", "wins", "losses", "win_rate", "net_r", "max_drawdown_r"])
    rows = []
    for month, group in data.groupby("month"):
        wins = int((group["result"] == "WIN").sum())
        losses = int((group["result"] == "LOSS").sum())
        net_r = float(group.apply(estimate_r, axis=1).sum())
        rows.append(
            {
                "month": month,
                "closed": int(len(group)),
                "wins": wins,
                "losses": losses,
                "win_rate": wins / len(group) * 100 if len(group) else 0.0,
                "net_r": net_r,
                "max_drawdown_r": max_drawdown_r(group),
            }
        )
    return pd.DataFrame(rows).sort_values("month")


def account_growth_simulator(df: pd.DataFrame, balances: list[float] | None = None, risk_pct: float = 1.0) -> pd.DataFrame:
    curve = equity_curve(df)
    balances = balances or [100.0, 500.0, 1000.0]
    columns = ["account_usdt", "risk_pct", "start_balance", "ending_balance", "profit_usdt", "max_drawdown_usdt", "closed_trades"]
    if curve.empty:
        return pd.DataFrame([{column: 0 for column in columns} for _ in []], columns=columns)
    max_dd_r = abs(max_drawdown_r(df))
    total_r = float(curve["r"].sum())
    rows = []
    for balance in balances:
        risk_amount = balance * risk_pct / 100
        profit = total_r * risk_amount
        rows.append(
            {
                "account_usdt": balance,
                "risk_pct": risk_pct,
                "start_balance": balance,
                "ending_balance": balance + profit,
                "profit_usdt": profit,
                "max_drawdown_usdt": max_dd_r * risk_amount,
                "closed_trades": int(len(curve)),
            }
        )
    return pd.DataFrame(rows)


def daily_pnl_bars(df: pd.DataFrame) -> pd.DataFrame:
    daily = daily_net_r(df)
    if daily.empty:
        return pd.DataFrame(columns=["date", "net_r", "color"])
    data = daily.copy()
    data["color"] = data["net_r"].map(lambda value: "#16a34a" if value >= 0 else "#dc2626")
    return data


def win_loss_by_day(df: pd.DataFrame) -> pd.DataFrame:
    closed = _closed(df)
    if closed.empty or "timestamp" not in closed.columns:
        return pd.DataFrame(columns=["date", "WIN", "LOSS"])
    data = closed.copy()
    data["date"] = pd.to_datetime(data["timestamp"], utc=True, errors="coerce").dt.date
    data = data.dropna(subset=["date"])
    if data.empty:
        return pd.DataFrame(columns=["date", "WIN", "LOSS"])
    pivot = data.pivot_table(index="date", columns="result", values="symbol", aggfunc="count", fill_value=0)
    for column in ["WIN", "LOSS"]:
        if column not in pivot.columns:
            pivot[column] = 0
    return pivot[["WIN", "LOSS"]].reset_index()


def tp_sl_distribution(df: pd.DataFrame) -> pd.DataFrame:
    closed = _closed(df)
    if closed.empty:
        return pd.DataFrame(columns=["target", "count"])
    targets = _hit_series(closed).replace({"": "UNKNOWN"})
    return targets.value_counts().rename_axis("target").reset_index(name="count")


def external_summary(external: pd.DataFrame) -> pd.DataFrame:
    if external.empty or "recommendation" not in external.columns:
        return pd.DataFrame(columns=["recommendation", "count"])
    return external["recommendation"].fillna("UNKNOWN").astype(str).str.upper().value_counts().rename_axis("recommendation").reset_index(name="count")


def latest_signals(df: pd.DataFrame, limit: int = 25) -> pd.DataFrame:
    columns = [
        "timestamp", "symbol", "side", "source", "watchlist_tier", "market_session",
        "score_bucket", "entry", "tp1", "tp2", "stop_loss", "result", "hit_target",
    ]
    available = [column for column in columns if column in df.columns]
    if df.empty:
        return pd.DataFrame(columns=available)
    return df.sort_values("timestamp", ascending=False).head(limit)[available]


def open_positions(df: pd.DataFrame) -> pd.DataFrame:
    open_df = active_positions(df)
    columns = [
        "timestamp", "symbol", "side", "entry", "tp1", "tp2", "stop_loss",
        "position_recommendation", "current_r", "distance_to_tp1_pct", "distance_to_sl_pct",
        "position_confidence", "position_reason", "watchlist_tier", "market_session",
    ]
    available = [column for column in columns if column in open_df.columns]
    if open_df.empty:
        return pd.DataFrame(columns=available)
    return open_df.sort_values("timestamp", ascending=False)[available]


def recent_events(df: pd.DataFrame, limit: int = 25) -> pd.DataFrame:
    events = _closed(df)
    columns = ["closed_at", "timestamp", "symbol", "side", "result", "hit_target", "entry", "tp1", "tp2", "stop_loss"]
    available = [column for column in columns if column in events.columns]
    if events.empty:
        return pd.DataFrame(columns=available)
    sort_column = "closed_at" if "closed_at" in events.columns and events["closed_at"].notna().any() else "timestamp"
    return events.sort_values(sort_column, ascending=False).head(limit)[available]


def apply_filters(
    df: pd.DataFrame,
    start_date: date | None = None,
    end_date: date | None = None,
    symbols: list[str] | None = None,
    tiers: list[str] | None = None,
    sessions: list[str] | None = None,
    sides: list[str] | None = None,
    sources: list[str] | None = None,
    results: list[str] | None = None,
    targets: list[str] | None = None,
    score_range: tuple[float, float] | None = None,
    confidence_range: tuple[float, float] | None = None,
) -> pd.DataFrame:
    data = df.copy()
    if data.empty:
        return data
    if "timestamp" in data.columns and (start_date or end_date):
        timestamps = pd.to_datetime(data["timestamp"], utc=True, errors="coerce").dt.tz_convert(DISPLAY_TZ)
        if start_date:
            data = data[timestamps.dt.date >= start_date]
            timestamps = pd.to_datetime(data["timestamp"], utc=True, errors="coerce").dt.tz_convert(DISPLAY_TZ)
        if end_date:
            data = data[timestamps.dt.date <= end_date]
    if symbols:
        data = data[data["symbol"].isin(symbols)]
    if tiers and "watchlist_tier" in data.columns:
        data = data[data["watchlist_tier"].isin(tiers)]
    if sessions and "market_session" in data.columns:
        data = data[data["market_session"].isin(sessions)]
    if sides and "side" in data.columns:
        data = data[data["side"].isin(sides)]
    if sources and "All" not in sources and "source" in data.columns:
        data = data[data["source"].isin(sources)]
    if results and "result" in data.columns:
        data = data[data["result"].isin(results)]
    if targets and "hit_target" in data.columns:
        data = data[data["hit_target"].isin(targets)]
    if score_range and "raw_score" in data.columns:
        scores = pd.to_numeric(data["raw_score"], errors="coerce")
        data = data[scores.between(score_range[0], score_range[1], inclusive="both")]
    if confidence_range and "setup_strength" in data.columns:
        confidence = pd.to_numeric(data["setup_strength"], errors="coerce")
        data = data[confidence.between(confidence_range[0], confidence_range[1], inclusive="both")]
    return data


def quality_views(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    closed = _closed(df)
    if closed.empty:
        empty = pd.DataFrame()
        return {
            "high_score_losses": empty,
            "low_score_wins": empty,
            "high_drawdown_winners": empty,
            "fast_sl": empty,
            "slow_tp": empty,
            "repeated_loss_symbols": empty,
            "poor_sessions": empty,
            "tier_c": empty,
        }
    data = closed.copy()
    data["hold_minutes"] = _holding_minutes(data)
    score = pd.to_numeric(data.get("raw_score", pd.Series(dtype=float)), errors="coerce")
    drawdown = pd.to_numeric(data.get("max_drawdown_pct", pd.Series(dtype=float)), errors="coerce")
    high_score_losses = data[(data["result"] == "LOSS") & (score >= 85)].sort_values("raw_score", ascending=False)
    low_score_wins = data[(data["result"] == "WIN") & (score < 75)].sort_values("raw_score", ascending=True)
    drawdown_threshold = drawdown.quantile(0.25) if drawdown.notna().any() else None
    high_drawdown_winners = (
        data[(data["result"] == "WIN") & (drawdown <= drawdown_threshold)]
        if drawdown_threshold is not None else data.iloc[0:0]
    )
    fast_sl = data[(data["result"] == "LOSS") & (data["hold_minutes"] <= 60)].sort_values("hold_minutes")
    slow_tp = data[(data["result"] == "WIN") & (data["hold_minutes"] >= 360)].sort_values("hold_minutes", ascending=False)
    repeated = performance_table(data[data["result"] == "LOSS"], "symbol")
    poor_sessions = performance_table(data, "market_session")
    tier_c = data[data.get("watchlist_tier", pd.Series(dtype=str)).astype(str).str.upper() == "C"]
    return {
        "high_score_losses": high_score_losses,
        "low_score_wins": low_score_wins,
        "high_drawdown_winners": high_drawdown_winners.sort_values("max_drawdown_pct").head(25),
        "fast_sl": fast_sl,
        "slow_tp": slow_tp,
        "repeated_loss_symbols": repeated[repeated["losses"] >= 2].sort_values("losses", ascending=False),
        "poor_sessions": poor_sessions[(poor_sessions["closed"] >= 3) & (poor_sessions["net_r"] < 0)].sort_values("net_r"),
        "tier_c": tier_c,
    }


def analytics_suggestions(df: pd.DataFrame) -> list[str]:
    suggestions = []
    symbol_perf = performance_table(df, "symbol")
    weak_symbols = symbol_perf[(symbol_perf["closed"] >= 5) & (symbol_perf["win_rate"] < 40)] if not symbol_perf.empty else pd.DataFrame()
    if not weak_symbols.empty:
        suggestions.append("Consider reviewing symbols with win rate below 40% and at least 5 closed trades.")
    session_perf = performance_table(df, "market_session")
    if not session_perf.empty and (session_perf["net_r"] < 0).any():
        suggestions.append("Consider reviewing sessions with negative net R.")
    if not quality_views(df)["high_score_losses"].empty:
        suggestions.append("Consider monitoring high-score losses for common failure patterns.")
    tier_perf = performance_table(df, "watchlist_tier")
    tier_c = tier_perf[tier_perf["watchlist_tier"].astype(str).str.upper() == "C"] if not tier_perf.empty else pd.DataFrame()
    if not tier_c.empty and float(tier_c.iloc[0]["win_rate"]) < 45 and int(tier_c.iloc[0]["closed"]) >= 5:
        suggestions.append("Consider Tier C restrictions if Tier C win rate remains weak.")
    if not suggestions:
        suggestions.append("No major rule-based warning from current filtered data. Continue collecting outcomes.")
    return suggestions


def warning_table(df: pd.DataFrame) -> pd.DataFrame:
    warnings = generate_performance_warnings(df)
    return pd.DataFrame({"warning": warnings})


def _format_metric(value: Any) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, str):
        return value if value.strip() else "N/A"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def _badge(status: str) -> str:
    value = str(status or "N/A")
    cls = "neutral"
    normalized = value.upper()
    if normalized in {"RUNNING", "ONLINE", "PASS", "POSITIVE", "APPROVED", "LONG", "HEALTHY", "WIN", "MATCHED"}:
        cls = "positive"
    elif normalized in {"DEGRADED", "WARNING", "DATA STALE", "REVIEW", "SHORT", "PROVISIONAL", "WAITING", "PARTIAL"}:
        cls = "warning"
    elif normalized in {"STOPPED", "FAIL", "FAILED", "DANGER", "ERROR", "LOSS", "INVALID", "AMBIGUOUS"}:
        cls = "danger"
    elif normalized in {"UNKNOWN", "N/A", "NEUTRAL"}:
        cls = "neutral"
    return f"<span class='badge {cls}'>{escape(value)}</span>"


def _kpi_grid(st: Any, items: list[tuple[str, Any, str | None]], columns: int = 4) -> None:
    style = f"--desktop-cols:{columns};"
    cards = []
    for label, value, badge in items:
        badge_html = _badge(badge) if badge else ""
        value_html = escape(_format_metric(value)).replace("&lt;br&gt;", "<br>")
        cards.append(
            "<div class='kpi-card'>"
            f"<div class='kpi-label'>{escape(str(label))}</div>"
            f"<div class='kpi-value'>{value_html}</div>"
            f"<div class='kpi-badge'>{badge_html}</div>"
            "</div>"
        )
    st.markdown(f"<div class='kpi-grid' style='{style}'>{''.join(cards)}</div>", unsafe_allow_html=True)


def _symbol_badges(st: Any, title: str, symbols: list[str], tone: str = "neutral") -> None:
    if not symbols:
        symbols = ["N/A"]
    badges = "".join(f"<span class='badge {tone}'>{escape(str(symbol))}</span>" for symbol in symbols)
    st.markdown(f"<div class='symbol-section'><h3>{escape(title)}</h3><div class='symbol-badges'>{badges}</div></div>", unsafe_allow_html=True)


def _progress_bar(percent: Any) -> str:
    numeric = pd.to_numeric(pd.Series([percent]), errors="coerce").iloc[0]
    if pd.isna(numeric) or numeric in [float("inf"), float("-inf")]:
        return "<span class='muted-small'>N/A</span>"
    value = max(0, min(100, int(round(float(numeric)))))
    return (
        "<div class='progress-wrap'>"
        f"<div class='progress-fill' style='width:{value}%'></div>"
        "</div>"
        f"<span class='progress-label'>{value}%</span>"
    )


def _profit_factor(df: pd.DataFrame) -> str:
    closed = _closed(df)
    if closed.empty:
        return "N/A"
    realized = closed.apply(estimate_r, axis=1)
    positive = realized[realized > 0].sum()
    negative = realized[realized < 0].sum()
    if negative == 0:
        return "N/A" if positive == 0 else "∞"
    return f"{positive / abs(negative):.2f}"


def _format_time_columns(df: pd.DataFrame) -> pd.DataFrame:
    data = df.copy()
    for column in ["timestamp", "closed_at", "Last Successful Scan", "Latest Log Time"]:
        if column in data.columns:
            data[column] = data[column].apply(_fmt_dt_plain)
    return data


def _display_position_card(st: Any, row: pd.Series) -> None:
    status = str(row.get("dashboard_status", "neutral"))
    side = str(row.get("side", "N/A")).upper()
    symbol = str(row.get("symbol", "N/A"))
    border = {"positive": "#22c55e", "warning": "#f59e0b", "danger": "#ef4444"}.get(status, "#334155")
    status_label = "REVIEW" if status in {"warning", "danger"} else "ONLINE"
    pnl = _fmt_percent(row.get("current_pnl_pct"))
    pnl_class = "positive-text" if not pnl.startswith("-") and pnl != "N/A" else "danger-text" if pnl.startswith("-") else ""
    st.markdown(
        f"""
<div class="position-card" style="border-left-color:{border}">
  <div class="position-head">
    <div>
      <strong>{escape(symbol)}</strong>
      <span class="confidence">Confidence {escape(_format_metric(row.get("setup_strength", row.get("confidence"))))}</span>
    </div>
    {_badge(side)}
  </div>
  <div class="position-compact-grid">
    <div><span>Entry</span><b>{_fmt_price(row.get("entry"))}</b></div>
    <div><span>Current</span><b>{_fmt_price(row.get("current_price"))}</b></div>
    <div><span>PnL</span><b class="{pnl_class}">{pnl}</b></div>
    <div><span>Open</span><b>{_fmt_duration_hours(row.get("time_open_hours"))}</b></div>
    <div><span>TP1</span><b>{_fmt_price(row.get("tp1"))}</b></div>
    <div><span>SL</span><b>{_fmt_price(row.get("stop_loss"))}</b></div>
  </div>
  <div class="position-progress">
    <span>Progress</span>
    {_progress_bar(row.get("progress_to_tp1_pct"))}
  </div>
  <div class="position-footer">
    {_badge(status_label)}
    <span class="muted-small">Opened {_fmt_dt(row.get("timestamp"))}</span>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )


def _display_signal_cards(st: Any, df: pd.DataFrame, execution_rows: list[dict[str, str]], limit: int = 10) -> None:
    if df.empty:
        st.info("No signals yet.")
        return
    data = df.copy()
    data["_sort_time"] = _timestamp_series(data)
    data = data.sort_values("_sort_time", ascending=False, na_position="last").head(limit)
    for _, row in data.iterrows():
        symbol = escape(str(row.get("symbol") or "Unknown"))
        side = str(row.get("side") or "Unknown").upper()
        result = str(row.get("result") or "OPEN").upper()
        setup = row.get("setup_strength", row.get("confidence", "N/A"))
        execution_status = execution_status_for_signal(row, execution_rows)
        modeled_r = estimate_r(row) if result in {"WIN", "LOSS"} else None
        modeled_text = f"{modeled_r:+.2f}R" if modeled_r is not None else "—"
        st.markdown(
            "<div class='signal-card'>"
            "<div class='signal-head'>"
            f"<div><strong>{symbol}</strong><span class='signal-time'>{escape(_fmt_dt_plain(row.get('timestamp')))}</span></div>"
            f"<div>{_badge(side)} {_badge(result)}</div>"
            "</div>"
            "<div class='signal-grid'>"
            f"<div><span>Setup</span><b>{escape(_format_metric(setup))}</b></div>"
            f"<div><span>Modeled R</span><b>{escape(modeled_text)}</b></div>"
            f"<div><span>Execution</span><b>{_badge(execution_status)}</b></div>"
            "</div>"
            "</div>",
            unsafe_allow_html=True,
        )


def _execution_value(snapshot: dict[str, Any], key: str, suffix: str) -> str:
    if not snapshot.get("available") or not snapshot.get("authoritative_n"):
        return "Collecting evidence"
    return _signed_number(snapshot.get(key), suffix)


def _health_badge_value(snapshot: dict[str, Any]) -> str:
    return str(snapshot.get("collector_health") or "UNAVAILABLE")


def _streamlit_app_legacy() -> None:
    try:
        import streamlit as st
    except ModuleNotFoundError as exc:
        raise SystemExit("Streamlit is not installed. Run: pip install -r requirements.txt") from exc

    st.set_page_config(page_title="Crypto Scanner Dashboard V2", layout="wide")
    data = load_dashboard_data()
    sent = data["sent"]

    st.markdown(
        """
<style>
  .block-container { padding-top: 1rem; max-width: 1280px; }
  h1 { font-size: 1.45rem !important; margin-bottom: 0.1rem !important; }
  h2, h3 { font-size: 1rem !important; margin: 0.55rem 0 0.35rem !important; }
  div[data-testid="stMetric"] {
    background: #101827;
    border: 1px solid #1f2a44;
    border-radius: 8px;
    padding: 0.45rem 0.6rem;
    min-height: 74px;
  }
  div[data-testid="stMetric"] label,
  div[data-testid="stMetric"] [data-testid="stMetricLabel"] {
    color: #cbd5e1 !important;
  }
  div[data-testid="stMetricValue"] {
    color: #f8fafc !important;
    font-size: 1.75rem;
    line-height: 1.05;
  }
  div[data-testid="stMetricDelta"] { color: #cbd5e1 !important; }
  .kpi-grid { display:grid; grid-template-columns:repeat(var(--desktop-cols), minmax(0, 1fr)); gap:10px; margin: 0.45rem 0 1rem; }
  .kpi-card { background:#101827; border:1px solid #1f2a44; border-radius:8px; padding:0.55rem 0.7rem; min-height:66px; overflow:hidden; }
  .kpi-label { color:#94a3b8; font-size:13px; line-height:1.1; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .kpi-value { color:#f8fafc; font-size:30px; font-weight:800; line-height:1.05; margin-top:4px; word-break:break-word; }
  .kpi-badge { margin-top:4px; min-height:18px; }
  .badge { display:inline-block; padding: 0.16rem 0.5rem; border-radius: 999px; font-size: 0.74rem; font-weight: 800; letter-spacing:0; vertical-align:middle; }
  .badge.positive { background:#064e3b; color:#a7f3d0; }
  .badge.warning { background:#78350f; color:#fde68a; }
  .badge.danger { background:#7f1d1d; color:#fecaca; }
  .badge.neutral { background:#1e293b; color:#cbd5e1; }
  .symbol-section { margin: 0.4rem 0 0.9rem; }
  .symbol-badges { display:flex; flex-wrap:wrap; gap:6px; }
  .position-card { background:#0f172a; border:1px solid #1e293b; border-left:5px solid #334155; border-radius:8px; padding:12px; margin-bottom:10px; }
  .position-head { display:flex; justify-content:space-between; align-items:center; gap:8px; margin-bottom:10px; }
  .confidence { color:#94a3b8; display:block; font-size:0.78rem; margin-top:2px; }
  .position-compact-grid { display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:8px 10px; font-size:0.88rem; }
  .position-compact-grid span, .position-progress span, .muted-small { color:#94a3b8; display:block; font-size:0.78rem; }
  .position-compact-grid b { color:#f8fafc; font-size:1rem; }
  .positive-text { color:#86efac !important; }
  .danger-text { color:#fca5a5 !important; }
  .position-progress { margin-top:10px; display:grid; grid-template-columns:72px 1fr 42px; align-items:center; gap:8px; }
  .progress-wrap { height:10px; background:#1e293b; border-radius:999px; overflow:hidden; }
  .progress-fill { height:100%; background:#38bdf8; border-radius:999px; }
  .progress-label { color:#f8fafc; font-size:0.82rem; font-weight:700; }
  .position-footer { margin-top:10px; display:flex; justify-content:space-between; align-items:center; gap:8px; flex-wrap:wrap; }
  .muted-small { font-size:0.76rem; word-break:break-word; }
  @media (max-width: 640px) {
    .block-container { padding-left: 0.8rem; padding-right: 0.8rem; }
    .kpi-grid { grid-template-columns:repeat(2, minmax(0, 1fr)) !important; gap:8px; }
    .kpi-card { min-height:58px; padding:0.5rem; }
    .kpi-label { font-size:12px; }
    .kpi-value { font-size:1.45rem; }
    div[data-testid="stMetricValue"] { font-size: 1.25rem; }
    .position-compact-grid { grid-template-columns:1fr 1fr; font-size:0.82rem; }
    .position-progress { grid-template-columns:60px 1fr 36px; }
  }
</style>
""",
        unsafe_allow_html=True,
    )

    st.title("Crypto Multi-Coin Scanner Dashboard V2.1")
    st.caption("Production Season 1 control center. Read-only: no Telegram sends, no API calls, no log writes, no auto trading.")

    with st.sidebar:
        st.header("Filters")
        timestamps = pd.to_datetime(sent.get("timestamp", pd.Series(dtype=str)), utc=True, errors="coerce").dropna()
        min_date = timestamps.dt.date.min() if not timestamps.empty else None
        max_date = timestamps.dt.date.max() if not timestamps.empty else None
        selected_range = st.date_input(
            "Date range",
            value=(min_date, max_date) if min_date and max_date else (date.today(), date.today()),
        )
        start_date = end_date = None
        if isinstance(selected_range, tuple) and len(selected_range) == 2:
            start_date, end_date = selected_range
        symbols = st.multiselect("Symbol", sorted(sent["symbol"].dropna().unique().tolist()) if "symbol" in sent.columns else [])
        tiers = st.multiselect("Tier", sorted(sent["watchlist_tier"].dropna().unique().tolist()) if "watchlist_tier" in sent.columns else [])
        sessions = st.multiselect("Session", sorted(sent["market_session"].dropna().unique().tolist()) if "market_session" in sent.columns else [])
        sides = st.multiselect("Direction", ["LONG", "SHORT"])
        results = st.multiselect("Result", ["WIN", "LOSS", "OPEN"])
        targets = st.multiselect("Hit target", ["TP1", "TP2", "TP3", "SL"])
        score_values = pd.to_numeric(sent.get("raw_score", pd.Series(dtype=float)), errors="coerce").dropna()
        confidence_values = pd.to_numeric(sent.get("setup_strength", pd.Series(dtype=float)), errors="coerce").dropna()
        score_range = st.slider(
            "Score range",
            0,
            100,
            (int(score_values.min()), int(score_values.max())) if not score_values.empty else (0, 100),
        )
        confidence_range = st.slider(
            "Setup strength range",
            0,
            100,
            (int(confidence_values.min()), int(confidence_values.max())) if not confidence_values.empty else (0, 100),
        )
        source_values = sorted(sent["source"].dropna().astype(str).replace("", "Unknown").unique().tolist()) if "source" in sent.columns else []
        source_options = ["All", *sorted(set(["Scanner", "Refiner", "External", "Unknown", *source_values]))]
        sources = st.multiselect("Source", source_options, default=["All"])

    filtered = apply_filters(
        sent,
        start_date,
        end_date,
        symbols,
        tiers,
        sessions,
        sides,
        sources,
        results,
        targets,
        score_range,
        confidence_range,
    )
    filtered = ensure_score_buckets(filtered)
    kpis = dashboard_kpis(filtered)

    overview = overview_metrics(data)
    active = active_positions(filtered)
    reviews = position_review_queue(filtered)
    funnel = signal_funnel(filtered)
    perf = performance_windows(filtered)["performance"]
    health = health_snapshot()
    logs = dashboard_log_timeline()
    daily_panel = daily_summary_panel(filtered)

    symbol_perf = performance_table(filtered, "symbol")
    tier_perf = performance_table(filtered, "watchlist_tier")
    session_perf = performance_table(filtered, "market_session")
    side_perf = performance_table(filtered, "side")
    score_perf = performance_table(filtered, "score_range")
    confidence_perf = performance_table(filtered, "confidence_range")
    distribution = tp_sl_distribution(filtered)
    quality = quality_views(filtered)
    v2 = build_performance_v2(filtered)
    curve = equity_curve(filtered)
    drawdown = drawdown_curve(filtered)
    monthly = monthly_performance(filtered)
    simulator = account_growth_simulator(filtered)
    today_ts = _timestamp_series(filtered)
    today_df = filtered[today_ts.dt.date == _now_utc().date()] if len(filtered) else filtered
    today_closed = _closed(today_df)
    today_wins = int((_series(today_closed, "result").astype(str).str.upper() == "WIN").sum()) if not today_closed.empty else 0
    today_losses = int((_series(today_closed, "result").astype(str).str.upper() == "LOSS").sum()) if not today_closed.empty else 0
    today_open = int((_series(today_df, "result").astype(str).str.upper() == "OPEN").sum()) if not today_df.empty else 0
    average_rr = _numeric_mean(filtered, "risk_reward")
    best_symbols = v2["top_symbols"]["symbol"].astype(str).head(6).tolist() if not v2["top_symbols"].empty and "symbol" in v2["top_symbols"].columns else []
    worst_symbols = v2["bottom_symbols"]["symbol"].astype(str).head(6).tolist() if not v2["bottom_symbols"].empty and "symbol" in v2["bottom_symbols"].columns else []

    st.markdown(
        f"Scanner {_badge(overview['status'])} &nbsp; "
        f"Data {_badge('DATA STALE' if overview['stale_warning'] != 'N/A' else 'PASS')} &nbsp; "
        f"Server Time: <b>{_fmt_dt(overview['server_time'])}</b>",
        unsafe_allow_html=True,
    )

    st.markdown("### Today's Performance")
    _kpi_grid(
        st,
        [
            ("Signals Today", int(len(today_df)), None),
            ("Win", today_wins, "PASS" if today_wins else None),
            ("Loss", today_losses, "FAIL" if today_losses else None),
            ("Open", today_open, "ONLINE" if today_open else None),
            ("Today's R", f"{_net_r(today_df):.2f}R", None),
            ("Need Review", int(len(reviews)), "REVIEW" if len(reviews) else "PASS"),
        ],
        columns=3,
    )

    st.markdown("### Production KPI")
    _kpi_grid(
        st,
        [
            ("Total Signals", kpis["Total sent signals"], None),
            ("Win Rate", f"{kpis['Win rate']:.1f}%", None),
            ("Net R", f"{kpis['Net R']:.2f}R", None),
            ("Closed Trades", kpis["Closed trades"], None),
            ("Profit Factor", _profit_factor(filtered), None),
            ("Average RR", _format_metric(average_rr), None),
        ],
        columns=3,
    )

    st.markdown("### Active Positions")
    if active.empty:
        st.info("No active positions found in logs/signals.csv.")
    else:
        for _, row in active.iterrows():
            _display_position_card(st, row)

    st.markdown("### Scanner Health")
    _kpi_grid(
        st,
        [
            ("Scanner", health["Process Status"], health["Process Status"]),
            ("Database", health["Database Status"], health["Database Status"]),
            ("Freshness", "STALE" if overview["stale_warning"] != "N/A" else "OK", "DATA STALE" if overview["stale_warning"] != "N/A" else "PASS"),
            ("Last Scan", _fmt_dt(overview["last_scan"]), None),
            ("Next Scan", _fmt_dt(overview["next_scan"]), None),
            ("Latest Log", _fmt_dt(health["Latest Log Time"]), None),
        ],
        columns=3,
    )

    st.markdown("### Daily Summary")
    _kpi_grid(
        st,
        [
            ("Signals Today", int(len(today_df)), None),
            ("Win/Loss/Open", f"{today_wins}/{today_losses}/{today_open}", None),
            ("Today's R", f"{_net_r(today_df):.2f}R", None),
            ("Need Review", int(len(reviews)), "REVIEW" if len(reviews) else "PASS"),
            ("Scanner Health", health["Process Status"], health["Process Status"]),
        ],
        columns=5,
    )

    st.markdown("### Top Performing Symbols")
    _symbol_badges(st, "Best performers", best_symbols, "positive")
    st.markdown("### Top Losing Symbols")
    _symbol_badges(st, "Weak performers", worst_symbols, "danger")

    with st.expander("Position Review", expanded=False):
        st.caption("Advisory view only. Dashboard never opens, closes, or modifies positions.")
        if reviews.empty:
            st.success("No position review items from current data.")
        else:
            st.dataframe(_format_time_columns(reviews), use_container_width=True, hide_index=True)

    with st.expander("Advanced Analytics", expanded=False):
        st.markdown("#### Core Quality Metrics")
        _kpi_grid(
            st,
            [
                ("Long Win Rate", f"{kpis['Long win rate']:.1f}%", None),
                ("Short Win Rate", f"{kpis['Short win rate']:.1f}%", None),
                ("TP1 Hit Rate", f"{kpis['TP1 hit rate']:.1f}%", None),
                ("SL Hit Rate", f"{kpis['SL hit rate']:.1f}%", None),
                ("Rejected by RR", funnel["Rejected by RR"], None),
                ("Rejected by Confidence", funnel["Rejected by Confidence"], None),
                ("Outcome Pending", funnel["Outcome Pending"], None),
                ("Candidates Found", funnel["Candidates Found"], None),
            ],
            columns=4,
        )

        st.markdown("#### Source Split")
        st.caption("Scanner and Signal Refiner are separated by source when source data exists.")
        st.dataframe(perf, use_container_width=True, hide_index=True)

        st.markdown("#### Performance 7D / 30D")
        st.dataframe(perf[perf["window"].isin(["7 Days", "30 Days"])], use_container_width=True, hide_index=True)

        with st.expander("Signal Funnel / Quality"):
            st.dataframe(pd.DataFrame([funnel]), use_container_width=True, hide_index=True)

        with st.expander("Logs Timeline"):
            if logs.empty:
                st.info("No readable scanner log found.")
            else:
                filter_cols = st.columns(5)
                level_filter = filter_cols[0].multiselect("Level", sorted(logs["level"].dropna().unique().tolist()))
                event_filter = filter_cols[1].multiselect("Event Type", sorted(logs["event_type"].dropna().unique().tolist()))
                source_filter = filter_cols[2].multiselect("Source", sorted(logs["source"].dropna().unique().tolist()))
                status_filter = filter_cols[3].multiselect("Approved / Rejected / Error", sorted(logs["status"].dropna().unique().tolist()))
                symbol_filter = filter_cols[4].text_input("Symbol contains", "")
                timeline = logs.copy()
                if level_filter:
                    timeline = timeline[timeline["level"].isin(level_filter)]
                if event_filter:
                    timeline = timeline[timeline["event_type"].isin(event_filter)]
                if source_filter:
                    timeline = timeline[timeline["source"].isin(source_filter)]
                if status_filter:
                    timeline = timeline[timeline["status"].isin(status_filter)]
                if symbol_filter:
                    timeline = timeline[timeline["message"].str.contains(symbol_filter, case=False, na=False)]
                st.dataframe(_format_time_columns(timeline[["timestamp", "level", "event_type", "status", "message"]].tail(120)), use_container_width=True, hide_index=True)
                with st.expander("Raw Logs"):
                    st.dataframe(timeline[["raw"]].tail(120), use_container_width=True, hide_index=True)

        with st.expander("Scanner Health Details"):
            health_view = pd.DataFrame([health])
            st.dataframe(_format_time_columns(health_view), use_container_width=True, hide_index=True)

        with st.expander("Executive Summary"):
            col1, col2, col3 = st.columns(3)
            col1.write(f"Best symbol by win rate: **{kpis['Best symbol']}**")
            col1.write(f"Worst symbol by win rate: **{kpis['Worst symbol']}**")
            col2.write(f"Best symbol by net R: **{kpis['Best symbol by net R']}**")
            col2.write(f"Worst symbol by net R: **{kpis['Worst symbol by net R']}**")
            col3.write(f"Best tier/session: **{kpis['Best tier']} / {kpis['Best session']}**")
            col3.write(f"Worst tier/session: **{kpis['Worst tier']} / {kpis['Worst session']}**")
            st.info("Analytics suggestion only. These observations do not auto-change config or strategy.")
            for suggestion in analytics_suggestions(filtered):
                st.write(f"- {suggestion}")

        with st.expander("Dashboard V3: Equity, PnL, Drawdown, Monthly"):
            _kpi_grid(
                st,
                [
                    ("Cumulative Net R", f"{kpis['Net R']:.2f}R", None),
                    ("Max Drawdown R", f"{max_drawdown_r(filtered):.2f}R", None),
                    ("Closed Months", int(len(monthly)), None),
                    ("Simulator Risk", "1.0% / trade", None),
                ],
                columns=4,
            )
            chart_left, chart_right = st.columns(2)
            chart_left.write("Equity Curve: cumulative Net R over time")
            chart_left.line_chart(curve.set_index("closed_at")["cumulative_r"] if not curve.empty else curve)
            chart_right.write("Drawdown: R below previous equity peak")
            chart_right.line_chart(drawdown.set_index("closed_at")["drawdown_r"] if not drawdown.empty else drawdown)
            st.write("Daily PnL histogram")
            bars = daily_pnl_bars(filtered)
            if bars.empty:
                st.dataframe(bars, use_container_width=True)
            else:
                import matplotlib.pyplot as plt

                fig, ax = plt.subplots(figsize=(10, 3.5))
                ax.bar(bars["date"].astype(str), bars["net_r"], color=bars["color"])
                ax.axhline(0, color="#475569", linewidth=1)
                ax.set_ylabel("Net R")
                ax.set_xlabel("Date")
                ax.tick_params(axis="x", rotation=45)
                fig.tight_layout()
                st.pyplot(fig)
                plt.close(fig)
            st.write("Monthly Performance Summary")
            st.dataframe(monthly, use_container_width=True)
            st.write("Account Growth Simulator")
            st.caption("Educational estimate using realized Net R and fixed 1% risk per trade. It does not account for fees, slippage, leverage liquidation, or compounding changes.")
            st.dataframe(simulator, use_container_width=True)

        with st.expander("Top / Worst Performer Tables"):
            st.write("Top 10 symbols ranked by Net R, win rate, and trades.")
            st.dataframe(v2["top_symbols"], use_container_width=True)
            st.write("Bottom 10 symbols ranked by Net R, win rate, and trades.")
            st.dataframe(v2["bottom_symbols"], use_container_width=True)

        with st.expander("Warnings"):
            warnings = warning_table(filtered)
            if warnings.empty:
                st.success("No V2 weak-performance warnings for the current filters.")
            else:
                for warning in warnings["warning"].tolist():
                    st.warning(warning)

        with st.expander("Win/Loss Analytics"):
            left, right = st.columns(2)
            daily = win_loss_by_day(filtered)
            left.bar_chart(daily.set_index("date") if not daily.empty else daily)
            net_daily = daily_net_r(filtered)
            right.line_chart(net_daily.set_index("date")["net_r"] if not net_daily.empty else net_daily)

        with st.expander("Drawdown Analytics"):
            _kpi_grid(
                st,
                [(label, _format_metric(kpis[label]), None) for label in ["Avg max drawdown %", "Avg drawdown winners", "Avg drawdown losers", "Max drawdown ever"]],
                columns=4,
            )
            left, right = st.columns(2)
            left.bar_chart(symbol_perf.set_index("symbol")["avg_drawdown"] if not symbol_perf.empty else symbol_perf)
            right.bar_chart(tier_perf.set_index("watchlist_tier")["avg_drawdown"] if not tier_perf.empty else tier_perf)

        with st.expander("Symbol Analytics"):
            left, right = st.columns(2)
            left.bar_chart(symbol_perf.set_index("symbol")["win_rate"] if not symbol_perf.empty else symbol_perf)
            right.bar_chart(symbol_perf.set_index("symbol")["net_r"] if not symbol_perf.empty else symbol_perf)
            st.dataframe(symbol_perf, use_container_width=True)

        with st.expander("Tier Analytics"):
            left, right = st.columns(2)
            left.bar_chart(tier_perf.set_index("watchlist_tier")["win_rate"] if not tier_perf.empty else tier_perf)
            right.bar_chart(tier_perf.set_index("watchlist_tier")["net_r"] if not tier_perf.empty else tier_perf)
            st.dataframe(tier_perf, use_container_width=True)

        with st.expander("Session Analytics"):
            left, right = st.columns(2)
            left.bar_chart(session_perf.set_index("market_session")["win_rate"] if not session_perf.empty else session_perf)
            right.bar_chart(session_perf.set_index("market_session")["net_r"] if not session_perf.empty else session_perf)
            st.dataframe(session_perf, use_container_width=True)

        with st.expander("Long vs Short Analytics"):
            left, right = st.columns(2)
            left.bar_chart(side_perf.set_index("side")["win_rate"] if not side_perf.empty else side_perf)
            right.bar_chart(side_perf.set_index("side")["net_r"] if not side_perf.empty else side_perf)
            st.dataframe(side_perf, use_container_width=True)

        with st.expander("Score / Confidence Analytics"):
            left, right = st.columns(2)
            left.bar_chart(score_perf.set_index("score_range")["win_rate"] if not score_perf.empty else score_perf)
            right.bar_chart(confidence_perf.set_index("confidence_range")["win_rate"] if not confidence_perf.empty else confidence_perf)
            st.write("Score bucket performance")
            st.dataframe(score_perf, use_container_width=True)
            st.write("Confidence bucket performance")
            st.dataframe(confidence_perf, use_container_width=True)

        with st.expander("TP/SL Analytics"):
            st.bar_chart(distribution.set_index("target")["count"] if not distribution.empty else distribution)
            _kpi_grid(
                st,
                [
                    ("TP1 Hit Rate", f"{kpis['TP1 hit rate']:.1f}%", None),
                    ("TP2 Hit Rate", f"{kpis['TP2 hit rate']:.1f}%", None),
                    ("SL Hit Rate", f"{kpis['SL hit rate']:.1f}%", None),
                    ("Avg Time to TP", f"{_format_metric(kpis['Avg time to TP'])} min", None),
                ],
                columns=4,
            )

        with st.expander("External VIP Signal Analytics"):
            external = external_summary(data.get("external_signals", pd.DataFrame()))
            st.bar_chart(external.set_index("recommendation")["count"] if not external.empty else external)
            st.dataframe(data.get("external_signals", pd.DataFrame()).tail(50), use_container_width=True)

        with st.expander("Position Manager Analytics"):
            st.dataframe(data.get("position_management", pd.DataFrame()).tail(100), use_container_width=True)
            st.write("Open positions")
            st.dataframe(_format_time_columns(open_positions(filtered)), use_container_width=True)

        with st.expander("Risk-Quality Views"):
            st.write("High score but lost trades")
            st.dataframe(quality["high_score_losses"].head(25), use_container_width=True)
            st.write("Low score but won trades")
            st.dataframe(quality["low_score_wins"].head(25), use_container_width=True)
            st.write("High drawdown winners")
            st.dataframe(quality["high_drawdown_winners"].head(25), use_container_width=True)
            st.write("Fast SL trades")
            st.dataframe(quality["fast_sl"].head(25), use_container_width=True)
            st.write("Slow TP trades")
            st.dataframe(quality["slow_tp"].head(25), use_container_width=True)
            st.write("Symbols with repeated losses")
            st.dataframe(quality["repeated_loss_symbols"].head(25), use_container_width=True)
            st.write("Sessions with poor performance")
            st.dataframe(quality["poor_sessions"].head(25), use_container_width=True)
            st.write("Tier C risk review")
            st.dataframe(quality["tier_c"].head(50), use_container_width=True)

        with st.expander("Recent Sent Signals"):
            st.dataframe(_format_time_columns(latest_signals(filtered, 50)), use_container_width=True)

        with st.expander("Closed Trades"):
            st.dataframe(_format_time_columns(recent_events(filtered, 50)), use_container_width=True)

        with st.expander("Full Analytics / CSV Exports"):
            st.caption("Read-only generated analytics. Telegram sends only the executive summary.")
            report_html = REPORTS_DIR / "report.html"
            if report_html.exists():
                st.write(f"Full static report: `{report_html}`")
            analytics_files = sorted(LOGS_DIR.glob("*.csv"))
            if not analytics_files:
                st.info("No CSV exports found yet. Run performance_report.py first.")
            for csv_path in analytics_files:
                with st.container():
                    st.write(f"CSV: `{csv_path.name}`")
                    preview = load_csv_safely(csv_path)
                    if preview.empty:
                        st.dataframe(preview, use_container_width=True)
                    else:
                        st.dataframe(_format_time_columns(preview.tail(50)), use_container_width=True)


def _streamlit_app() -> None:
    try:
        import streamlit as st
    except ModuleNotFoundError as exc:
        raise SystemExit("Streamlit is not installed. Run: pip install -r requirements.txt") from exc

    st.set_page_config(page_title="Crypto Scanner — Production Control Center", layout="wide")

    @st.cache_data(ttl=30, show_spinner=False)
    def cached_dashboard_data() -> dict[str, pd.DataFrame]:
        return load_dashboard_data()

    @st.cache_data(ttl=20, show_spinner=False)
    def cached_execution_truth() -> dict[str, Any]:
        return load_execution_truth_snapshot()

    @st.cache_data(ttl=15, show_spinner=False)
    def cached_operational_health() -> tuple[dict[str, Any], dict[str, Any]]:
        status_snapshot = scanner_status_snapshot()
        return status_snapshot, health_snapshot(status_snapshot=status_snapshot)

    @st.cache_data(ttl=15, show_spinner=False)
    def cached_service_states() -> dict[str, str]:
        return {
            "Scanner": _systemctl_state("crypto-scanner.service") or "UNKNOWN",
            "Outcome checker": _systemctl_state("crypto-outcome-checker.service") or "UNKNOWN",
            "Position watcher": _systemctl_state("crypto-position-watcher.service") or "UNKNOWN",
            "Execution timer": _systemctl_state("crypto-binance-execution-truth.timer") or "UNKNOWN",
            "Execution service": _systemctl_state("crypto-binance-execution-truth.service") or "UNKNOWN",
        }

    st.markdown(
        """
<style>
  .block-container { padding-top: 3.35rem; padding-bottom: 2rem; max-width: 1180px; }
  header[data-testid="stHeader"] { height: 2rem; }
  div[data-testid="stVerticalBlock"] { gap:.55rem; }
  h1 { font-size: 1.35rem !important; margin: 0 !important; }
  h2 { font-size: 1.06rem !important; margin: .85rem 0 .35rem !important; }
  h3 { font-size: .95rem !important; margin: .55rem 0 .3rem !important; }
  .hero { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; margin:.1rem 0 .65rem; }
  .hero-title { font-size:1.42rem; font-weight:800; line-height:1.1; }
  .hero-subtitle { color:#94a3b8; font-size:.82rem; margin-top:3px; }
  .kpi-grid { display:grid; grid-template-columns:repeat(var(--desktop-cols), minmax(0, 1fr)); gap:8px; margin:.35rem 0 .8rem; }
  .kpi-card { background:color-mix(in srgb, var(--background-color) 92%, #64748b 8%); border:1px solid rgba(148,163,184,.22); border-radius:10px; padding:.58rem .68rem; min-height:64px; overflow:hidden; }
  .kpi-label { color:#94a3b8; font-size:.76rem; line-height:1.1; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .kpi-value { font-size:1.42rem; font-weight:780; line-height:1.12; margin-top:5px; overflow-wrap:anywhere; }
  .kpi-badge { margin-top:4px; min-height:16px; }
  .badge { display:inline-block; padding:.13rem .45rem; border-radius:999px; font-size:.68rem; font-weight:800; vertical-align:middle; }
  .badge.positive { background:rgba(16,185,129,.17); color:#34d399; }
  .badge.warning { background:rgba(245,158,11,.17); color:#fbbf24; }
  .badge.danger { background:rgba(239,68,68,.17); color:#f87171; }
  .badge.neutral { background:rgba(100,116,139,.18); color:#94a3b8; }
  .position-card,.signal-card { border:1px solid rgba(148,163,184,.22); border-radius:10px; padding:.65rem .72rem; margin-bottom:8px; background:color-mix(in srgb, var(--background-color) 94%, #64748b 6%); }
  .position-card { border-left:4px solid #475569; }
  .position-head,.signal-head { display:flex; justify-content:space-between; align-items:flex-start; gap:8px; margin-bottom:8px; }
  .confidence,.signal-time,.muted-small { color:#94a3b8; display:block; font-size:.72rem; margin-top:2px; }
  .position-compact-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:7px 10px; }
  .position-compact-grid span,.position-progress span,.signal-grid span { color:#94a3b8; display:block; font-size:.7rem; }
  .position-compact-grid b,.signal-grid b { font-size:.9rem; }
  .position-progress { margin-top:8px; display:grid; grid-template-columns:64px 1fr 38px; align-items:center; gap:7px; }
  .progress-wrap { height:8px; background:rgba(100,116,139,.25); border-radius:999px; overflow:hidden; }
  .progress-fill { height:100%; background:#38bdf8; border-radius:999px; }
  .progress-label { font-size:.74rem; font-weight:700; }
  .position-footer { margin-top:8px; display:flex; justify-content:space-between; gap:8px; flex-wrap:wrap; }
  .signal-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px; }
  .positive-text { color:#34d399 !important; } .danger-text { color:#f87171 !important; }
  div[data-testid="stSegmentedControl"] { margin-bottom:.35rem; }
  div[data-testid="stDataFrame"] { font-size:.78rem; }
  @media (max-width:640px) {
    .block-container { padding:3.25rem .7rem 1.4rem; }
    .hero-title { font-size:1.2rem; }
    .hero-subtitle { font-size:.74rem; }
    .kpi-grid { grid-template-columns:repeat(2,minmax(0,1fr)) !important; gap:7px; }
    .kpi-card { min-height:57px; padding:.5rem; }
    .kpi-label { font-size:.7rem; }
    .kpi-value { font-size:1.17rem; }
    .position-compact-grid { grid-template-columns:1fr 1fr; }
    .signal-grid { grid-template-columns:1fr 1fr 1.25fr; }
    div[data-testid="stButtonGroup"] { width:100% !important; }
    div[data-testid="stButtonGroup"] div[role="radiogroup"] { display:grid !important; grid-template-columns:repeat(4,minmax(0,1fr)); width:100% !important; }
    div[data-testid="stButtonGroup"] button[role="radio"] { min-width:0 !important; width:100%; padding-left:.2rem !important; padding-right:.2rem !important; }
    div[data-testid="stButtonGroup"] button[role="radio"] p { font-size:.56rem !important; }
  }
</style>
""",
        unsafe_allow_html=True,
    )

    data = cached_dashboard_data()
    sent = data["sent"]
    scanner_snapshot, health = cached_operational_health()
    overview = overview_metrics(data, status_snapshot=scanner_snapshot)
    execution = cached_execution_truth()
    status = overview["status"]
    st.markdown(
        "<div class='hero'><div>"
        "<div class='hero-title'>Crypto Scanner</div>"
        "<div class='hero-subtitle'>Production Season 1 · Read-only control center</div>"
        f"</div><div>{_badge(status)}</div></div>",
        unsafe_allow_html=True,
    )
    view = st.segmented_control(
        "View",
        ["HOME", "SIGNALS", "PERF", "SYSTEM"],
        default="HOME",
        label_visibility="collapsed",
        width="stretch",
    ) or "HOME"

    filtered = sent
    if view in {"SIGNALS", "PERF"}:
        with st.sidebar:
            st.header("Filters")
            timestamps = _timestamp_series(sent).dropna()
            local_dates = timestamps.dt.tz_convert(DISPLAY_TZ).dt.date if not timestamps.empty else pd.Series(dtype=object)
            min_date = local_dates.min() if not local_dates.empty else None
            max_date = local_dates.max() if not local_dates.empty else None
            selected_range = st.date_input(
                "Date range",
                value=(min_date, max_date) if min_date and max_date else ((_now_utc().astimezone(DISPLAY_TZ).date(),) * 2),
            )
            start_date = end_date = None
            if isinstance(selected_range, tuple) and len(selected_range) == 2:
                start_date, end_date = selected_range
            symbols = st.multiselect("Symbol", sorted(sent["symbol"].dropna().unique().tolist()) if "symbol" in sent.columns else [])
            sides = st.multiselect("Direction", ["LONG", "SHORT"])
            results = st.multiselect("Result", ["WIN", "LOSS", "OPEN"])
            source_values = sorted(sent["source"].dropna().astype(str).replace("", "Unknown").unique().tolist()) if "source" in sent.columns else []
            sources = st.multiselect("Source", ["All", *source_values], default=["All"])
        filtered = apply_filters(
            sent,
            start_date,
            end_date,
            symbols,
            [],
            [],
            sides,
            sources,
            results,
            [],
            (0, 100),
            (0, 100),
        )
        filtered = ensure_score_buckets(filtered)

    if view == "HOME":
        today_df = _local_day_frame(sent)
        today_closed = _closed(today_df)
        wins = int(_series(today_closed, "result").astype(str).str.upper().eq("WIN").sum())
        losses = int(_series(today_closed, "result").astype(str).str.upper().eq("LOSS").sum())
        active = active_positions(sent)

        st.markdown("## Status")
        _kpi_grid(
            st,
            [
                ("Last Scan", _fmt_time(overview["last_scan"]), None),
                ("Next Scan", _fmt_time(overview["next_scan"]), None),
                ("Timezone", DISPLAY_TZ_LABEL, None),
                ("Data", "STALE" if overview["stale_warning"] != "N/A" else "FRESH", "WARNING" if overview["stale_warning"] != "N/A" else "HEALTHY"),
            ],
            columns=4,
        )
        st.markdown("## Today")
        _kpi_grid(
            st,
            [
                ("Signals", len(today_df), None),
                ("Wins", wins, "WIN" if wins else None),
                ("Losses", losses, "LOSS" if losses else None),
                ("Open", len(active), "ONLINE" if len(active) else None),
            ],
            columns=4,
        )
        st.markdown("## Execution Truth")
        _kpi_grid(
            st,
            [
                ("Matched", execution.get("matched") if execution.get("matched") is not None else "Unavailable", None),
                ("Authoritative", execution.get("authoritative_n") if execution.get("authoritative_n") is not None else "Unavailable", None),
                ("Execution Net R", _execution_value(execution, "execution_net_r", "R"), None),
                ("Execution PnL", _execution_value(execution, "execution_pnl_usdt", " USDT"), None),
            ],
            columns=4,
        )
        st.markdown("## Collector Health")
        _kpi_grid(
            st,
            [
                ("Collector", _health_badge_value(execution), _health_badge_value(execution)),
                ("Last Sync", _relative_time(execution.get("last_success_utc")), None),
                ("INVALID", execution.get("invalid") if execution.get("invalid") is not None else "Unavailable", "INVALID" if execution.get("invalid") else None),
                ("AMBIGUOUS", execution.get("ambiguous") if execution.get("ambiguous") is not None else "Unavailable", "AMBIGUOUS" if execution.get("ambiguous") else None),
            ],
            columns=4,
        )
        if not execution.get("available"):
            st.caption("Execution Truth: waiting for readable collector artifacts.")
        st.markdown("## Recent Signals")
        _display_signal_cards(st, sent, execution.get("rows", []), limit=8)
        with st.expander(f"Open Positions · {len(active)}"):
            if active.empty:
                st.info("No open positions.")
            else:
                for _, row in active.iterrows():
                    _display_position_card(st, row)

    elif view == "SIGNALS":
        st.markdown("## Recent Signals")
        st.caption(f"{len(filtered)} signals match the current filters. Times shown in {DISPLAY_TZ_LABEL}.")
        _display_signal_cards(st, filtered, execution.get("rows", []), limit=25)
        with st.expander("Technical details"):
            columns = [
                name for name in ["timestamp", "symbol", "side", "result", "hit_target", "setup_strength", "confidence", "risk_reward", "entry", "stop_loss", "tp1", "tp2", "closed_at"]
                if name in filtered.columns
            ]
            st.dataframe(_format_time_columns(filtered[columns].tail(100)), width="stretch", hide_index=True)

    elif view == "PERF":
        kpis = dashboard_kpis(filtered)
        closed = _closed(filtered)
        st.markdown("## Modeled Scanner Performance")
        st.caption(f"Modeled scanner outcomes · sample size {len(closed)} closed trades")
        _kpi_grid(
            st,
            [
                ("Closed", len(closed), None),
                ("Win Rate", f"{kpis['Win rate']:.1f}%", None),
                ("Net R", f"{kpis['Net R']:+.2f}R", None),
                ("Profit Factor", _profit_factor(filtered), None),
            ],
            columns=4,
        )
        st.markdown("## Authoritative Execution Performance")
        authoritative_n = execution.get("authoritative_n")
        st.caption(
            "Final execution PnL excludes funding and other adjustments · "
            + (f"sample size {authoritative_n}" if authoritative_n is not None else "sample unavailable")
        )
        _kpi_grid(
            st,
            [
                ("Eligible N", authoritative_n if authoritative_n is not None else "Unavailable", None),
                ("Gross Realized", _execution_value(execution, "gross_realized_pnl_usdt", " USDT"), None),
                ("Commission", _execution_value(execution, "commission_usdt", " USDT"), None),
                ("Execution PnL", _execution_value(execution, "execution_pnl_usdt", " USDT"), None),
                ("Gross R", _execution_value(execution, "gross_r", "R"), None),
                ("Execution Net R", _execution_value(execution, "execution_net_r", "R"), None),
            ],
            columns=3,
        )
        if not authoritative_n:
            st.info("Collecting execution evidence")
        st.markdown("## Provisional Funding View")
        st.markdown(_badge("PROVISIONAL"), unsafe_allow_html=True)
        st.caption("Funding-adjusted values are observational and are not presented as final execution performance.")
        _kpi_grid(
            st,
            [
                ("Funding Rows", execution.get("funding_rows") if execution.get("funding_rows") is not None else "Unavailable", "PROVISIONAL"),
                ("Observed Funding", _signed_number(execution.get("funding_usdt"), " USDT"), "PROVISIONAL"),
                ("Funding-adjusted PnL", _signed_number(execution.get("funding_adjusted_pnl_usdt"), " USDT"), "PROVISIONAL"),
            ],
            columns=3,
        )
        with st.expander("Modeled analytics"):
            curve = equity_curve(filtered)
            drawdown = drawdown_curve(filtered)
            left, right = st.columns(2)
            left.write("Cumulative modeled Net R")
            if curve.empty:
                left.info("No closed trades.")
            else:
                left.line_chart(curve.set_index("closed_at")["cumulative_r"])
            right.write("Modeled drawdown")
            if drawdown.empty:
                right.info("No closed trades.")
            else:
                right.line_chart(drawdown.set_index("closed_at")["drawdown_r"])
            st.dataframe(performance_windows(filtered)["performance"], width="stretch", hide_index=True)

    else:
        services = cached_service_states()
        st.markdown("## Services")
        _kpi_grid(st, [(name, value, value) for name, value in services.items()], columns=3)
        st.markdown("## Scanner Diagnostics")
        _kpi_grid(
            st,
            [
                ("Server UTC", _fmt_utc_compact(overview["server_time"]), None),
                ("Last Scan UTC", _fmt_utc_compact(overview["last_scan"]), None),
                ("Next Scan UTC", _fmt_utc_compact(overview["next_scan"]), None),
                ("Latest Log UTC", _fmt_utc_compact(health["Latest Log Time"]), None),
                ("Data Age", f"{overview['data_age_minutes']} min" if overview["data_age_minutes"] is not None else "Unavailable", None),
                ("Disk Usage", health.get("Disk Usage", "Unavailable"), None),
            ],
            columns=3,
        )
        with st.expander("Execution Truth diagnostics", expanded=True):
            diagnostic = {
                "Collector health": execution.get("collector_health"),
                "Last success UTC": execution.get("last_success_utc"),
                "Last success age seconds": execution.get("last_success_age_seconds"),
                "High-water UTC": execution.get("high_water_utc"),
                "High-water age seconds": execution.get("state_age_seconds"),
                "Prospective boundary": execution.get("boundary"),
                "Lifecycle provenance": execution.get("provenance"),
                "Pending reconciliation": execution.get("pending_reconciliation"),
                "INVALID": execution.get("invalid"),
                "AMBIGUOUS": execution.get("ambiguous"),
                "Artifacts": execution.get("artifacts"),
                "Diagnostic message": execution.get("message"),
            }
            st.json(diagnostic)
            if execution.get("checks"):
                st.dataframe(
                    pd.DataFrame(
                        [{"check": name, **value} for name, value in execution["checks"].items()]
                    ),
                    width="stretch",
                    hide_index=True,
                )
        logs = dashboard_log_timeline()
        with st.expander("Recent system events"):
            if logs.empty:
                st.info("No readable scanner log found.")
            else:
                st.dataframe(logs[["timestamp", "level", "event_type", "status", "message"]].tail(100), width="stretch", hide_index=True)
        with st.expander("Raw scanner health"):
            st.json(health)


def _table_html(df: pd.DataFrame) -> str:
    if df.empty:
        return "<p class='muted'>No data.</p>"
    return df.to_html(index=False, classes="data-table", float_format=lambda value: f"{value:.2f}")


def render_dashboard(df: pd.DataFrame, output: Path = DASHBOARD_HTML) -> Path:
    """Backward-compatible static HTML renderer used by smoke tests."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    normalized = _report_safe_signals(df)
    sent = sent_signals(normalized)
    sent = _report_safe_signals(sent)
    data = {"signals": normalized, "sent": sent}
    report = _safe_build_report(sent)
    symbol_perf = performance_table(sent, "symbol")
    tier_perf = performance_table(sent, "watchlist_tier")
    session_perf = performance_table(sent, "market_session")
    direction_perf = performance_table(sent, "side")
    kpis = dashboard_kpis(sent)
    overview = overview_metrics(data)
    active = active_positions(sent)
    reviews = position_review_queue(sent)
    funnel = pd.DataFrame([signal_funnel(sent)])
    perf_windows = performance_windows(sent)["performance"]
    health = pd.DataFrame([health_snapshot()])
    daily_panel = pd.DataFrame([daily_summary_panel(sent)])
    timeline = dashboard_log_timeline().tail(20)
    v2 = build_performance_v2(sent)
    curve = equity_curve(sent)
    drawdown = drawdown_curve(sent)
    monthly = monthly_performance(sent)
    simulator = account_growth_simulator(sent)
    daily = daily_pnl_bars(sent)
    warnings_html = "<br><br>".join(str(item).replace("\n", "<br>") for item in v2["warnings"]) if v2["warnings"] else "No warnings."

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Crypto Scanner Dashboard</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; color: #111827; background: #f8fafc; }}
    h1, h2 {{ margin: 0 0 12px; }}
    section {{ margin: 24px 0; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }}
    .card {{ background: white; border: 1px solid #e5e7eb; border-radius: 8px; padding: 14px; }}
    .label {{ color: #64748b; font-size: 12px; text-transform: uppercase; }}
    .value {{ font-size: 24px; font-weight: 700; margin-top: 6px; }}
    .data-table {{ border-collapse: collapse; width: 100%; background: white; font-size: 13px; }}
    .data-table th, .data-table td {{ border: 1px solid #e5e7eb; padding: 8px; text-align: left; }}
    .data-table th {{ background: #f1f5f9; }}
    .muted {{ color: #64748b; }}
  </style>
</head>
<body>
  <h1>Crypto Scanner Dashboard V2</h1>
  <p class="muted">Production Season 1 read-only control center. Telegram signal assistant only. No auto trading.</p>

  <section><h2>Production Overview</h2>
    <p>Scanner Status: <strong>{overview['status']}</strong> | Last Scan: <strong>{overview['last_scan']}</strong> | Next Scan: <strong>{overview['next_scan']}</strong></p>
    <p>Server Time: <strong>{overview['server_time']}</strong> | Market Regime: <strong>{overview['market_regime']}</strong></p>
  </section>

  <section class="cards">
    <div class="card"><div class="label">Total Sent Signals</div><div class="value">{report['total_sent_signals']}</div></div>
    <div class="card"><div class="label">Closed Trades</div><div class="value">{report['closed_signals']}</div></div>
    <div class="card"><div class="label">Wins</div><div class="value">{report['wins']}</div></div>
    <div class="card"><div class="label">Losses</div><div class="value">{report['losses']}</div></div>
    <div class="card"><div class="label">Win Rate</div><div class="value">{report['win_rate']:.1f}%</div></div>
    <div class="card"><div class="label">Cumulative Net R</div><div class="value">{kpis['Net R']:.2f}R</div></div>
    <div class="card"><div class="label">Max Drawdown R</div><div class="value">{max_drawdown_r(sent):.2f}R</div></div>
    <div class="card"><div class="label">Best Symbol</div><div class="value">{kpis['Best symbol']}</div></div>
  </section>

  <section><h2>Active Positions</h2>{_table_html(active)}</section>
  <section><h2>Position Review</h2>{_table_html(reviews)}</section>
  <section><h2>Signal Funnel</h2>{_table_html(funnel)}</section>
  <section><h2>Performance Today / 7 Days / 30 Days</h2>{_table_html(perf_windows)}</section>
  <section><h2>Scanner Health</h2>{_table_html(health)}</section>
  <section><h2>Logs Timeline</h2>{_table_html(timeline[["timestamp", "level", "event_type", "status", "message"]] if not timeline.empty else timeline)}</section>
  <section><h2>Daily Summary</h2>{_table_html(daily_panel)}</section>
  <section><h2>Dashboard V3 Equity Curve</h2>{_table_html(curve.tail(50))}</section>
  <section><h2>Daily PnL Histogram Data</h2>{_table_html(daily.tail(50))}</section>
  <section><h2>Drawdown Curve</h2>{_table_html(drawdown.tail(50))}</section>
  <section><h2>Monthly Performance Summary</h2>{_table_html(monthly)}</section>
  <section><h2>Account Growth Simulator</h2>{_table_html(simulator)}</section>
  <section><h2>Recent Sent Signals</h2>{_table_html(latest_signals(sent))}</section>
  <section><h2>Top Performers</h2>{_table_html(v2["top_symbols"])}</section>
  <section><h2>Worst Performers</h2>{_table_html(v2["bottom_symbols"])}</section>
  <section><h2>Warnings</h2><p>{warnings_html}</p></section>
  <section><h2>Closed Trades</h2>{_table_html(recent_events(sent))}</section>
  <section><h2>Symbol Performance</h2>{_table_html(symbol_perf)}</section>
  <section><h2>Tier Performance</h2>{_table_html(tier_perf)}</section>
  <section><h2>Session Performance</h2>{_table_html(session_perf)}</section>
  <section><h2>Direction Performance</h2>{_table_html(direction_perf)}</section>
  <section><h2>Open Positions</h2>{_table_html(open_positions(sent))}</section>
</body>
</html>
"""
    output.write_text(html, encoding="utf-8")
    return output


def main() -> int:
    _streamlit_app()
    return 0


if __name__ == "__main__":
    main()
