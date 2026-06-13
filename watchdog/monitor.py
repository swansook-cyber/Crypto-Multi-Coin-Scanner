# -*- coding: utf-8 -*-
"""Lightweight external watchdog for VelaHub public services.

This module is intentionally separate from the crypto scanner. It does not
import or alter scanner strategy, signal routing, Cornix routing, or trade logic.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[1]
WATCHDOG_DIR = Path(__file__).resolve().parent
SERVICES_FILE = WATCHDOG_DIR / "services.json"
STATE_FILE = WATCHDOG_DIR / "state.json"
DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_FAILURE_THRESHOLD = 3
REQUEST_TIMEOUT_SECONDS = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOGGER = logging.getLogger("velahub_watchdog")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


@dataclass
class HealthResult:
    healthy: bool
    status_code: int | None = None
    error: str = ""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "-"
    total = max(0, int(seconds))
    days = total // 86400
    hours = (total % 86400) // 3600
    minutes = (total % 3600) // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def load_services(path: Path = SERVICES_FILE) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    services = []
    for item in data:
        name = str(item.get("name", "")).strip()
        url = str(item.get("url", "")).strip()
        if not name or not url:
            continue
        services.append({"name": name, "url": url})
    return services


def default_service_state() -> dict[str, Any]:
    return {
        "status": "unknown",
        "consecutive_failures": 0,
        "last_success": "",
        "last_failure": "",
        "outage_started_at": "",
        "last_error": "",
        "outage_count": 0,
        "total_checks": 0,
        "successful_checks": 0,
        "failure_count": 0,
        "longest_downtime_seconds": 0,
    }


def load_state(path: Path = STATE_FILE) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("State file is missing or invalid; starting with empty state.")
        return {}
    if not isinstance(raw, dict):
        return {}
    state: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        item = default_service_state()
        if isinstance(value, dict):
            item.update(value)
        state[str(key)] = item
    return state


def save_state(state: dict[str, dict[str, Any]], path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def check_service(session: requests.Session, url: str) -> HealthResult:
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=True)
    except requests.Timeout:
        return HealthResult(False, error="timeout")
    except requests.exceptions.SSLError as exc:
        return HealthResult(False, error=f"SSL failure: {exc}")
    except requests.exceptions.ConnectionError as exc:
        return HealthResult(False, error=f"connection/DNS failure: {exc}")
    except requests.RequestException as exc:
        return HealthResult(False, error=str(exc))
    if 200 <= response.status_code <= 399:
        return HealthResult(True, status_code=response.status_code)
    return HealthResult(False, status_code=response.status_code, error=f"HTTP {response.status_code}")


def telegram_target_chat_id() -> str:
    return os.getenv("TELEGRAM_VELAHUB_MONITOR_CHAT_ID", "").strip()


def send_telegram(message: str, session: requests.Session | None = None) -> bool:
    if not env_bool("WATCHDOG_TELEGRAM_ENABLED", True):
        LOGGER.info("Watchdog Telegram disabled by WATCHDOG_TELEGRAM_ENABLED")
        return False
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = telegram_target_chat_id()
    LOGGER.info("VELAHUB WATCHDOG TELEGRAM ROUTE chat_id=%s", chat_id or "-")
    if not token or not chat_id:
        LOGGER.warning("Watchdog Telegram skipped: missing TELEGRAM_BOT_TOKEN or TELEGRAM_VELAHUB_MONITOR_CHAT_ID")
        return False
    client = session or requests.Session()
    try:
        response = client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": message},
            timeout=20,
        )
    except requests.RequestException as exc:
        LOGGER.error("Watchdog Telegram send failed: %s", exc)
        return False
    if response.status_code != 200:
        LOGGER.error("Watchdog Telegram send failed: status=%s body=%s", response.status_code, response.text)
        return False
    LOGGER.info("Watchdog Telegram send success")
    return True


def build_offline_message(service: dict[str, str], item: dict[str, Any]) -> str:
    return (
        "🚨 VelaHub Service Offline\n\n"
        f"Service: {service['name']}\n"
        f"URL: {service['url']}\n"
        f"Downtime: {format_duration(0)}\n"
        f"Failures: {item.get('consecutive_failures', 0)}\n"
        f"Last error: {item.get('last_error', '-') or '-'}\n"
        f"Timestamp: {item.get('last_failure', '-') or '-'}"
    )


def build_recovery_message(service: dict[str, str], downtime_seconds: float) -> str:
    return (
        "✅ VelaHub Service Recovered\n\n"
        f"Service: {service['name']}\n"
        f"URL: {service['url']}\n"
        f"Downtime: {format_duration(downtime_seconds)}\n"
        f"Timestamp: {utc_now_iso()}"
    )


def update_service_state(
    service: dict[str, str],
    result: HealthResult,
    item: dict[str, Any],
    failure_threshold: int,
    session: requests.Session,
    send_alerts: bool = True,
) -> dict[str, Any]:
    now = utc_now_iso()
    previous_status = str(item.get("status", "unknown"))
    item["total_checks"] = int(item.get("total_checks") or 0) + 1

    if result.healthy:
        item["successful_checks"] = int(item.get("successful_checks") or 0) + 1
        item["consecutive_failures"] = 0
        item["last_success"] = now
        item["last_error"] = ""
        if previous_status == "offline":
            started = parse_iso(item.get("outage_started_at"))
            downtime = (utc_now() - started).total_seconds() if started else 0
            item["longest_downtime_seconds"] = max(float(item.get("longest_downtime_seconds") or 0), downtime)
            item["status"] = "online"
            item["outage_started_at"] = ""
            if send_alerts:
                send_telegram(build_recovery_message(service, downtime), session)
            LOGGER.info("%s recovered after %s", service["name"], format_duration(downtime))
        else:
            item["status"] = "online"
        return item

    item["last_failure"] = now
    item["last_error"] = result.error or (f"HTTP {result.status_code}" if result.status_code else "unknown error")
    item["consecutive_failures"] = int(item.get("consecutive_failures") or 0) + 1
    item["failure_count"] = int(item.get("failure_count") or 0) + 1
    if item["consecutive_failures"] >= failure_threshold and previous_status != "offline":
        item["status"] = "offline"
        item["outage_started_at"] = now
        item["outage_count"] = int(item.get("outage_count") or 0) + 1
        if send_alerts:
            send_telegram(build_offline_message(service, item), session)
        LOGGER.warning("%s marked offline: %s", service["name"], item["last_error"])
    return item


def run_once(
    services_path: Path = SERVICES_FILE,
    state_path: Path = STATE_FILE,
    session: requests.Session | None = None,
    send_alerts: bool = True,
) -> dict[str, dict[str, Any]]:
    services = load_services(services_path)
    state = load_state(state_path)
    client = session or requests.Session()
    threshold = env_int("WATCHDOG_FAILURE_THRESHOLD", DEFAULT_FAILURE_THRESHOLD)

    for service in services:
        key = service["url"]
        item = state.get(key, default_service_state())
        result = check_service(client, service["url"])
        state[key] = update_service_state(service, result, item, threshold, client, send_alerts=send_alerts)
        status = state[key].get("status", "unknown")
        LOGGER.info(
            "%s | %s | failures=%s | error=%s",
            service["name"],
            status,
            state[key].get("consecutive_failures", 0),
            state[key].get("last_error", "") or "-",
        )

    save_state(state, state_path)
    return state


def build_daily_report(services_path: Path = SERVICES_FILE, state_path: Path = STATE_FILE) -> str:
    services = load_services(services_path)
    state = load_state(state_path)
    lines = ["📊 VelaHub Watchdog Daily Report", f"Date: {utc_now().date().isoformat()}", ""]
    for service in services:
        item = state.get(service["url"], default_service_state())
        total_checks = int(item.get("total_checks") or 0)
        successful_checks = int(item.get("successful_checks") or 0)
        uptime_pct = successful_checks / total_checks * 100 if total_checks else 0.0
        lines.extend(
            [
                f"{service['name']}",
                f"URL: {service['url']}",
                f"Status: {item.get('status', 'unknown')}",
                f"Uptime: {uptime_pct:.1f}%",
                f"Failure count: {item.get('failure_count', 0)}",
                f"Last success: {item.get('last_success', '-') or '-'}",
                f"Last failure: {item.get('last_failure', '-') or '-'}",
                f"Outage count: {item.get('outage_count', 0)}",
                f"Longest downtime: {format_duration(item.get('longest_downtime_seconds', 0))}",
                "",
            ]
        )
    return "\n".join(lines).strip()


def maybe_send_daily_report(state: dict[str, dict[str, Any]], session: requests.Session) -> dict[str, dict[str, Any]]:
    hour = env_int("WATCHDOG_DAILY_REPORT_HOUR", 8)
    now = datetime.now()
    meta = state.get("_meta", {})
    if now.hour != hour:
        return state
    today = now.date().isoformat()
    if meta.get("last_daily_report_date") == today:
        return state
    send_telegram(build_daily_report(), session)
    meta["last_daily_report_date"] = today
    state["_meta"] = meta
    save_state(state)
    LOGGER.info("Daily watchdog report sent for %s", today)
    return state


def run_loop(interval_seconds: int) -> int:
    LOGGER.info("VelaHub watchdog loop started interval=%s", interval_seconds)
    while True:
        session = requests.Session()
        try:
            state = run_once(session=session)
            maybe_send_daily_report(state, session)
            LOGGER.info("Next watchdog check in %s seconds", interval_seconds)
            time.sleep(interval_seconds)
        except Exception as exc:
            LOGGER.exception("Watchdog loop error: %s", exc)
            time.sleep(60)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor VelaHub public URLs and send Telegram reports alerts.")
    parser.add_argument("--once", action="store_true", help="Run one health-check cycle and exit.")
    parser.add_argument("--loop", action="store_true", help="Run continuously.")
    parser.add_argument("--daily-report", action="store_true", help="Print and send a daily watchdog report.")
    parser.add_argument("--no-telegram", action="store_true", help="Do not send Telegram alerts for this command.")
    return parser.parse_args()


def main() -> int:
    load_dotenv(BASE_DIR / ".env")
    args = parse_args()
    if not env_bool("WATCHDOG_ENABLED", True):
        LOGGER.info("Watchdog disabled by WATCHDOG_ENABLED")
        return 0

    interval = env_int("WATCHDOG_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)
    if args.daily_report:
        report = build_daily_report()
        print(report)
        if not args.no_telegram:
            send_telegram(report)
        return 0
    if args.loop:
        return run_loop(interval)

    run_once(send_alerts=not args.no_telegram)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
