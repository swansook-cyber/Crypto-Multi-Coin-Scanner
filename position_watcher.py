# -*- coding: utf-8 -*-
"""Real-time position watcher for TP1 breakeven advisories.

This process is separate from the 1H scanner. It never opens, closes, or
modifies exchange positions. It only reads the signal journal, checks current
Binance Futures prices, and sends advisory Telegram messages to Reports.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.analytics_reporting import load_csv_safely
from telegram_sender import TelegramRoutes, send_text


BASE_DIR = Path(__file__).resolve().parent
JOURNAL = BASE_DIR / "logs" / "signals.csv"
BINANCE_PRICE_URL = "https://fapi.binance.com/fapi/v1/ticker/price"
LOGGER = logging.getLogger("position_watcher")


WATCHER_COLUMNS = {
    "tp1_alert_sent": 0,
    "tp1_alert_at": "",
    "tp1_alert_source": "",
    "breakeven_recommended": 0,
    "breakeven_price": "",
    "position_management_stage": "",
    "cornix_be_command_sent": 0,
    "cornix_be_command_at": "",
    "cornix_be_command_status": "",
    "cornix_be_command_error": "",
}


@dataclass
class WatcherConfig:
    enabled: bool
    interval_seconds: int
    send_alerts: bool
    send_telegram: bool
    token: str
    reports_chat_id: str
    cornix_chat_id: str
    command_mode: str
    send_report_copy: bool
    dry_run: bool


@dataclass
class WatcherStats:
    checked: int = 0
    tp1_reached: int = 0
    alerts_sent: int = 0
    skipped_duplicates: int = 0
    cornix_commands_sent: int = 0
    report_copies_sent: int = 0
    errors: int = 0


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


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


def load_config() -> WatcherConfig:
    load_dotenv(BASE_DIR / ".env")
    command_mode = os.getenv("POSITION_WATCHER_COMMAND_MODE", "report_only").strip().lower()
    if command_mode not in {"report_only", "cornix_command"}:
        LOGGER.warning("Invalid POSITION_WATCHER_COMMAND_MODE=%s; using report_only", command_mode)
        command_mode = "report_only"
    return WatcherConfig(
        enabled=env_bool("POSITION_WATCHER_ENABLED", True),
        interval_seconds=max(10, env_int("POSITION_WATCHER_INTERVAL_SECONDS", 60)),
        send_alerts=env_bool("SEND_TP1_BREAKEVEN_ALERTS", True),
        send_telegram=env_bool("SEND_TELEGRAM", True),
        token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        reports_chat_id=os.getenv("TELEGRAM_REPORTS_CHAT_ID", "").strip(),
        cornix_chat_id=os.getenv("POSITION_WATCHER_CORNIX_CHAT_ID", os.getenv("TELEGRAM_CORNIX_CHAT_ID", "")).strip(),
        command_mode=command_mode,
        send_report_copy=env_bool("POSITION_WATCHER_SEND_REPORT_COPY", True),
        dry_run=env_bool("POSITION_WATCHER_DRY_RUN", False),
    )


def build_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def ensure_columns(df: pd.DataFrame) -> pd.DataFrame:
    data = df.copy()
    defaults = {
        "timestamp": "",
        "symbol": "",
        "side": "",
        "direction": "",
        "entry": "",
        "tp1": "",
        "tp2": "",
        "stop_loss": "",
        "sl": "",
        "result": "OPEN",
        "signal_status": "sent",
    }
    defaults.update(WATCHER_COLUMNS)
    for column, default in defaults.items():
        if column not in data.columns:
            data[column] = default
    data["symbol"] = data["symbol"].fillna("").astype(str).str.upper()
    data["side"] = data["side"].fillna("").astype(str).str.upper()
    blank_side = data["side"].eq("") & data["direction"].notna()
    data.loc[blank_side, "side"] = data.loc[blank_side, "direction"].astype(str).str.upper()
    data["result"] = data["result"].fillna("OPEN").replace("", "OPEN").astype(str).str.upper()
    data["signal_status"] = data["signal_status"].fillna("sent").replace("", "sent").astype(str).str.lower()
    for column in ["entry", "tp1", "tp2", "stop_loss", "sl", "breakeven_price"]:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    return data


def open_positions(df: pd.DataFrame) -> pd.DataFrame:
    data = ensure_columns(df)
    if data.empty:
        return data
    active_statuses = {"sent", "tier_c_report_only"}
    return data[(data["result"] == "OPEN") & (data["signal_status"].isin(active_statuses))].copy()


def alert_already_sent(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "sent"}


def position_is_valid_for_command(row: pd.Series) -> tuple[bool, str]:
    symbol = str(row.get("symbol", "")).strip().upper()
    side = str(row.get("side", "")).strip().upper()
    entry = pd.to_numeric(pd.Series([row.get("entry")]), errors="coerce").iloc[0]
    tp1 = pd.to_numeric(pd.Series([row.get("tp1")]), errors="coerce").iloc[0]
    result = str(row.get("result", "OPEN")).strip().upper()
    if not symbol:
        return False, "symbol missing"
    if side not in {"LONG", "SHORT"}:
        return False, "direction missing"
    if result != "OPEN":
        return False, "trade is closed"
    if pd.isna(entry):
        return False, "entry missing"
    if pd.isna(tp1):
        return False, "tp1 missing"
    return True, ""


def fetch_current_price(session: requests.Session, symbol: str) -> float:
    response = session.get(BINANCE_PRICE_URL, params={"symbol": symbol.upper()}, timeout=10)
    response.raise_for_status()
    data = response.json()
    return float(data["price"])


def tp1_reached(row: pd.Series, current_price: float) -> bool:
    side = str(row.get("side", "")).upper()
    tp1 = pd.to_numeric(pd.Series([row.get("tp1")]), errors="coerce").iloc[0]
    if pd.isna(tp1) or current_price <= 0:
        return False
    if side == "LONG":
        return current_price >= float(tp1)
    if side == "SHORT":
        return current_price <= float(tp1)
    return False


def format_price(value: Any) -> str:
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric):
        return "-"
    value = float(numeric)
    if value >= 1000:
        return f"{value:.2f}"
    if value >= 10:
        return f"{value:.3f}"
    if value >= 1:
        return f"{value:.4f}"
    return f"{value:.6f}"


def build_alert_message(row: pd.Series, current_price: float) -> str:
    entry = row.get("entry")
    return (
        "POSITION WATCHER ALERT\n\n"
        f"Symbol: {row.get('symbol')}\n"
        f"Direction: {row.get('side')}\n\n"
        "Event:\n"
        "TP1 REACHED\n\n"
        "Entry:\n"
        f"{format_price(entry)}\n\n"
        "TP1:\n"
        f"{format_price(row.get('tp1'))}\n\n"
        "Current Price:\n"
        f"{format_price(current_price)}\n\n"
        "Recommended Action:\n"
        "MOVE SL TO BREAKEVEN\n\n"
        "Breakeven SL:\n"
        f"{format_price(entry)}\n\n"
        "Status:\n"
        "Advisory only. No auto-close. No auto-trade. Not financial advice."
    )


def format_cornix_breakeven_command(row: pd.Series) -> str:
    return (
        "MOVE SL TO BREAKEVEN\n\n"
        f"Symbol: {row.get('symbol')}\n"
        f"Direction: {row.get('side')}\n"
        f"New Stop: {format_price(row.get('entry'))}\n\n"
        "Reason:\n"
        "TP1 reached."
    )


def build_report_copy(row: pd.Series, current_price: float, cornix_message: str, dry_run: bool) -> str:
    prefix = "DRY RUN - " if dry_run else ""
    return (
        f"{prefix}POSITION WATCHER CORNIX COMMAND COPY\n\n"
        f"{build_alert_message(row, current_price)}\n\n"
        "Cornix command:\n"
        f"{cornix_message}"
    )


def save_journal(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    df.to_csv(tmp_path, index=False)
    tmp_path.replace(path)
    LOGGER.info("Position watcher CSV persisted: %s", path)


def send_breakeven_alert(
    session: requests.Session,
    config: WatcherConfig,
    message: str,
    dry_run: bool = False,
) -> bool:
    if dry_run:
        LOGGER.info("DRY_RUN position watcher alert:\n%s", message)
        return True
    if not config.send_telegram or not config.send_alerts:
        LOGGER.info("Position watcher Telegram disabled; alert not sent")
        return False
    routes = TelegramRoutes(token=config.token, reports_chat_id=config.reports_chat_id)
    LOGGER.info("POSITION WATCHER ROUTE chat_id=%s", config.reports_chat_id or "-")
    return send_text(session, routes, "reports", message, "position watcher")


def send_cornix_breakeven_command(
    session: requests.Session,
    config: WatcherConfig,
    message: str,
    symbol: str,
) -> tuple[bool, str]:
    LOGGER.info("Position watcher Cornix command attempt: symbol=%s dry_run=%s", symbol, config.dry_run)
    if config.dry_run:
        LOGGER.info("DRY_RUN Cornix breakeven command for %s:\n%s", symbol, message)
        return True, "DRY_RUN"
    if not config.send_telegram or not config.send_alerts:
        return False, "telegram disabled"
    routes = TelegramRoutes(token=config.token, cornix_chat_id=config.cornix_chat_id)
    sent = send_text(session, routes, "cornix", message, "position watcher cornix")
    return sent, "SENT" if sent else "FAILED"


def process_once(
    journal_path: Path = JOURNAL,
    session: requests.Session | None = None,
    config: WatcherConfig | None = None,
    dry_run: bool = False,
) -> WatcherStats:
    config = config or load_config()
    if dry_run:
        config = WatcherConfig(
            enabled=config.enabled,
            interval_seconds=config.interval_seconds,
            send_alerts=config.send_alerts,
            send_telegram=config.send_telegram,
            token=config.token,
            reports_chat_id=config.reports_chat_id,
            cornix_chat_id=config.cornix_chat_id,
            command_mode=config.command_mode,
            send_report_copy=config.send_report_copy,
            dry_run=True,
        )
    session = session or build_session()
    stats = WatcherStats()
    df = ensure_columns(load_csv_safely(journal_path))
    positions = open_positions(df)
    stats.checked = int(len(positions))
    if positions.empty:
        LOGGER.info("Position watcher: no open positions")
        return stats

    for index, row in positions.iterrows():
        symbol = str(row.get("symbol", "")).upper()
        is_valid, invalid_reason = position_is_valid_for_command(row)
        if not is_valid:
            LOGGER.warning("Position watcher skipped row: %s", invalid_reason)
            stats.errors += 1
            continue
        if alert_already_sent(row.get("tp1_alert_sent")) or alert_already_sent(row.get("breakeven_recommended")) or alert_already_sent(row.get("cornix_be_command_sent")):
            stats.skipped_duplicates += 1
            continue
        try:
            current_price = fetch_current_price(session, symbol)
        except Exception as exc:
            stats.errors += 1
            LOGGER.error("Position watcher price fetch failed for %s: %s", symbol, exc)
            continue
        if not tp1_reached(row, current_price):
            continue
        stats.tp1_reached += 1
        sent_any = False
        command_status = ""
        command_error = ""
        if config.command_mode == "cornix_command":
            command = format_cornix_breakeven_command(row)
            sent_command, command_status = send_cornix_breakeven_command(session, config, command, symbol)
            if sent_command:
                sent_any = True
                stats.cornix_commands_sent += 1
                if not config.dry_run:
                    df.at[index, "cornix_be_command_sent"] = 1
                df.at[index, "cornix_be_command_at"] = datetime.now(timezone.utc).isoformat()
            else:
                command_error = command_status or "Cornix send failed"
            df.at[index, "cornix_be_command_status"] = command_status
            df.at[index, "cornix_be_command_error"] = command_error
            if sent_command and config.send_report_copy:
                report_copy = build_report_copy(row, current_price, command, config.dry_run)
                if send_breakeven_alert(session, config, report_copy, dry_run=False):
                    stats.report_copies_sent += 1
        else:
            message = build_alert_message(row, current_price)
            sent_any = send_breakeven_alert(session, config, message, dry_run=config.dry_run)

        if sent_any:
            stats.alerts_sent += 1
            if not config.dry_run:
                df.at[index, "tp1_alert_sent"] = 1
                df.at[index, "breakeven_recommended"] = 1
            df.at[index, "tp1_alert_at"] = datetime.now(timezone.utc).isoformat()
            df.at[index, "tp1_alert_source"] = "watcher"
            df.at[index, "breakeven_price"] = row.get("entry")
            df.at[index, "position_management_stage"] = "TP1_REACHED_BE_RECOMMENDED"
            LOGGER.info("Watcher detected TP1 first: symbol=%s source=watcher", symbol)
            save_journal(df, journal_path)
        else:
            LOGGER.error("Position watcher alert not marked sent: %s", symbol)
    return stats


def run_loop(config: WatcherConfig, journal_path: Path, dry_run: bool = False) -> None:
    if not config.enabled:
        LOGGER.info("POSITION_WATCHER_ENABLED=0; watcher exiting")
        return
    session = build_session()
    LOGGER.info("Position watcher started interval=%ss reports_chat_id=%s", config.interval_seconds, config.reports_chat_id or "-")
    while True:
        try:
            stats = process_once(journal_path, session, config, dry_run=dry_run)
            LOGGER.info(
                "Position watcher loop: open=%s tp1_reached=%s alerts=%s cornix=%s report_copies=%s duplicates=%s errors=%s",
                stats.checked,
                stats.tp1_reached,
                stats.alerts_sent,
                stats.cornix_commands_sent,
                stats.report_copies_sent,
                stats.skipped_duplicates,
                stats.errors,
            )
            time.sleep(config.interval_seconds)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            LOGGER.exception("Position watcher loop error: %s", exc)
            time.sleep(60)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Real-time TP1 breakeven position watcher.")
    parser.add_argument("--journal", type=Path, default=JOURNAL)
    parser.add_argument("--once", action="store_true", help="Run one check and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Print alerts and mark CSV in test mode without Telegram.")
    return parser.parse_args()


def main() -> int:
    setup_logging()
    config = load_config()
    args = parse_args()
    if args.once:
        stats = process_once(args.journal, config=config, dry_run=args.dry_run)
        print(
            "Position watcher checked="
            f"{stats.checked} tp1_reached={stats.tp1_reached} alerts={stats.alerts_sent} "
            f"duplicates={stats.skipped_duplicates} errors={stats.errors}"
        )
        return 0 if stats.errors == 0 else 1
    run_loop(config, args.journal, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
