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
    "cornix_be_stop_price": "",
    "cornix_be_command_status": "",
    "cornix_be_command_error": "",
    "new_stop_notification_sent": 0,
    "new_stop_notification_at": "",
    "new_stop_notification_key": "",
    "new_stop_notification_stop_price": "",
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
    cornix_test_mode: bool = False
    cornix_breakeven_format: str = "v1"


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
    breakeven_format = os.getenv("CORNIX_BREAKEVEN_FORMAT", "v1").strip().lower()
    if breakeven_format not in {"v1", "v2", "v3", "v4"}:
        LOGGER.warning("Invalid CORNIX_BREAKEVEN_FORMAT=%s; using v1", breakeven_format)
        breakeven_format = "v1"
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
        cornix_test_mode=env_bool("CORNIX_TEST_MODE", False),
        cornix_breakeven_format=breakeven_format,
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
    for column in ["entry", "tp1", "tp2", "stop_loss", "sl", "breakeven_price", "cornix_be_stop_price"]:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    return data


def final_outcome_reached(row: pd.Series) -> bool:
    result = str(row.get("result", "OPEN")).strip().upper()
    hit_target = str(row.get("hit_target", "")).strip().upper()
    return result != "OPEN" or hit_target in {"SL", "TP2", "TP3", "2", "3"}


def open_positions(df: pd.DataFrame) -> pd.DataFrame:
    data = ensure_columns(df)
    if data.empty:
        return data
    active_statuses = {"sent", "tier_c_report_only", "weak_symbol_report_only", "session_risk_report_only"}
    final_mask = data.apply(final_outcome_reached, axis=1)
    return data[(~final_mask) & (data["signal_status"].isin(active_statuses))].copy()


def alert_already_sent(value: Any) -> bool:
    numeric = numeric_value(value)
    if numeric is not None:
        return numeric >= 1
    return str(value).strip().lower() in {"1", "1.0", "true", "yes", "sent"}


def numeric_value(value: Any) -> float | None:
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric):
        return None
    return float(numeric)


def breakeven_stop_price(row: pd.Series) -> float | None:
    return numeric_value(row.get("entry"))


def same_price(left: Any, right: Any, tolerance: float = 1e-9) -> bool:
    left_value = numeric_value(left)
    right_value = numeric_value(right)
    if left_value is None or right_value is None:
        return False
    return abs(left_value - right_value) <= tolerance


def cornix_breakeven_already_sent_for_stop(row: pd.Series, stop_price: float | None) -> bool:
    if not alert_already_sent(row.get("cornix_be_command_sent")):
        return False
    if stop_price is None:
        return True
    stored_stop = row.get("cornix_be_stop_price")
    if same_price(stored_stop, stop_price):
        return True
    if numeric_value(stored_stop) is None:
        return True
    return False


def new_stop_notification_key(row: pd.Series, stop_price: float | None) -> str:
    symbol = str(row.get("symbol", "")).strip().upper()
    direction = str(row.get("side", "")).strip().upper()
    stop = format_price(stop_price) if stop_price is not None else ""
    return f"{symbol}|{direction}|{stop}"


def new_stop_notification_already_sent(row: pd.Series, stop_price: float | None) -> bool:
    key = new_stop_notification_key(row, stop_price)
    stored_key = str(row.get("new_stop_notification_key", "")).strip().upper()
    if key and stored_key == key:
        return True
    if not alert_already_sent(row.get("new_stop_notification_sent")):
        return False
    if stop_price is None:
        return True
    stored_stop = row.get("new_stop_notification_stop_price")
    if same_price(stored_stop, stop_price):
        return True
    if numeric_value(stored_stop) is None:
        return True
    return False


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


def format_cornix_symbol_slash(symbol: str) -> str:
    symbol = str(symbol).strip().upper()
    if symbol.endswith("USDT"):
        return f"#{symbol[:-4]}/USDT"
    return f"#{symbol}"


def format_cornix_breakeven_command(row: pd.Series, version: str = "v1") -> str:
    """Format the Cornix breakeven command.

    Keep Cornix command syntax isolated here so alternative formats can be
    tested without touching TP1 detection or journal state handling.
    """
    selected = str(version or "v1").strip().lower()
    symbol = str(row.get("symbol", "")).strip().upper()
    direction = str(row.get("side", "")).strip().upper()
    stop = format_price(row.get("entry"))
    if selected == "v2":
        return f"{direction} {symbol}\n\nMOVE STOP LOSS\n\n{stop}"
    if selected == "v3":
        return f"UPDATE {symbol}\n\nSTOP LOSS:\n{stop}"
    if selected == "v4":
        return f"{format_cornix_symbol_slash(symbol)}\n\nMOVE SL TO ENTRY\n\n{stop}"
    return f"{direction} {symbol}\n\nNEW STOP:\n{stop}"


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
    direction: str = "",
) -> tuple[bool, str]:
    LOGGER.info("Position watcher Cornix command attempt: symbol=%s dry_run=%s", symbol, config.dry_run)
    LOGGER.info(
        "Cornix BE format=%s symbol=%s direction=%s",
        config.cornix_breakeven_format,
        symbol,
        direction or "-",
    )
    LOGGER.info("CORNIX COMMAND TEXT symbol=%s\n%s", symbol, message)
    if config.dry_run:
        LOGGER.info("DRY_RUN Cornix breakeven command for %s:\n%s", symbol, message)
        return True, "DRY_RUN"
    if not config.send_telegram or not config.send_alerts:
        return False, "telegram disabled"
    if not config.token or not config.cornix_chat_id:
        LOGGER.error("Cornix command skipped: TELEGRAM_BOT_TOKEN or Cornix chat id missing")
        return False, "missing telegram config"
    payload = {"chat_id": config.cornix_chat_id, "text": message}
    LOGGER.info("CORNIX COMMAND ROUTE chat_id=%s symbol=%s", config.cornix_chat_id, symbol)
    LOGGER.info("CORNIX COMMAND PAYLOAD chat_id=%s text=%r", config.cornix_chat_id, message)
    try:
        response = session.post(
            f"https://api.telegram.org/bot{config.token}/sendMessage",
            data=payload,
            timeout=20,
        )
    except requests.RequestException as exc:
        LOGGER.error("Cornix command Telegram request failed: %s", exc)
        return False, f"request failed: {exc}"
    response_text = getattr(response, "text", "")
    message_id = "-"
    try:
        response_json = response.json()
        message_id = str(response_json.get("result", {}).get("message_id", "-"))
    except (ValueError, AttributeError, TypeError):
        response_json = {}
    LOGGER.info(
        "CORNIX COMMAND RESPONSE status=%s message_id=%s body=%s",
        getattr(response, "status_code", "-"),
        message_id,
        response_text,
    )
    if getattr(response, "status_code", 0) != 200:
        return False, f"telegram failed: {response_text}"
    return True, f"SENT message_id={message_id}"


def send_cornix_test_command(session: requests.Session, config: WatcherConfig) -> bool:
    test_row = pd.Series(
        {
            "symbol": "HYPEUSDT",
            "side": "LONG",
            "entry": 70.744,
        }
    )
    LOGGER.info("CORNIX_TEST_MODE enabled; sending Cornix command test")
    message = format_cornix_breakeven_command(test_row, config.cornix_breakeven_format)
    sent, status = send_cornix_breakeven_command(
        session,
        config,
        message,
        str(test_row["symbol"]),
        str(test_row["side"]),
    )
    LOGGER.info("CORNIX_TEST_MODE result sent=%s status=%s", sent, status)
    return sent


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
            cornix_test_mode=config.cornix_test_mode,
            cornix_breakeven_format=config.cornix_breakeven_format,
        )
    session = session or build_session()
    stats = WatcherStats()
    df = ensure_columns(load_csv_safely(journal_path))
    active_statuses = {"sent", "tier_c_report_only", "weak_symbol_report_only", "session_risk_report_only"}
    final_mask = df.apply(final_outcome_reached, axis=1) if not df.empty else pd.Series(dtype=bool)
    closed_candidates = df[(final_mask) & (df["signal_status"].isin(active_statuses))]
    if not closed_candidates.empty:
        LOGGER.info("skipped_closed_signal_breakeven_command count=%s", len(closed_candidates))
    positions = open_positions(df)
    stats.checked = int(len(positions))
    if positions.empty:
        LOGGER.info("Position watcher: no open positions")
        return stats
    sent_new_stop_keys = {
        new_stop_notification_key(existing_row, breakeven_stop_price(existing_row))
        for _, existing_row in df.iterrows()
        if alert_already_sent(existing_row.get("new_stop_notification_sent"))
    }
    sent_new_stop_keys.update(
        new_stop_notification_key(existing_row, numeric_value(existing_row.get("cornix_be_stop_price")))
        for _, existing_row in df.iterrows()
        if alert_already_sent(existing_row.get("cornix_be_command_sent"))
    )
    sent_new_stop_keys = {key for key in sent_new_stop_keys if key and not key.endswith("|-") and not key.endswith("|")}

    for index, row in positions.iterrows():
        symbol = str(row.get("symbol", "")).upper()
        is_valid, invalid_reason = position_is_valid_for_command(row)
        if not is_valid:
            LOGGER.warning("Position watcher skipped row: %s", invalid_reason)
            stats.errors += 1
            continue
        new_stop_price = breakeven_stop_price(row)
        signal_status = str(row.get("signal_status", "")).strip().lower()
        report_only_status = signal_status in {"tier_c_report_only", "weak_symbol_report_only", "session_risk_report_only"}
        stop_notification_key = new_stop_notification_key(row, new_stop_price)
        row_already_marked = new_stop_notification_already_sent(row, new_stop_price) or cornix_breakeven_already_sent_for_stop(row, new_stop_price)
        cross_row_duplicate = stop_notification_key in sent_new_stop_keys
        if row_already_marked or cross_row_duplicate:
            stats.skipped_duplicates += 1
            LOGGER.info(
                "SKIP_DUPLICATE_NEW_STOP_NOTIFICATION symbol=%s direction=%s stop=%s key=%s",
                symbol,
                str(row.get("side", "")).strip().upper(),
                format_price(new_stop_price),
                stop_notification_key,
            )
            if numeric_value(row.get("new_stop_notification_stop_price")) is None and new_stop_price is not None and not config.dry_run:
                df.at[index, "new_stop_notification_stop_price"] = format_price(new_stop_price)
                df.at[index, "new_stop_notification_key"] = stop_notification_key
                df.at[index, "new_stop_notification_sent"] = 1
                if not str(row.get("new_stop_notification_at", "")).strip():
                    df.at[index, "new_stop_notification_at"] = datetime.now(timezone.utc).isoformat()
                if numeric_value(row.get("cornix_be_stop_price")) is None and alert_already_sent(row.get("cornix_be_command_sent")):
                    df.at[index, "cornix_be_stop_price"] = new_stop_price
                save_journal(df, journal_path)
            continue
        if config.command_mode == "cornix_command" and not report_only_status:
            if cornix_breakeven_already_sent_for_stop(row, new_stop_price):
                stats.skipped_duplicates += 1
                LOGGER.info(
                    "skipped_duplicate_breakeven_command symbol=%s stop=%s",
                    symbol,
                    format_price(new_stop_price),
                )
                if numeric_value(row.get("cornix_be_stop_price")) is None and new_stop_price is not None and not config.dry_run:
                    df.at[index, "cornix_be_stop_price"] = new_stop_price
                    save_journal(df, journal_path)
                continue
        elif alert_already_sent(row.get("tp1_alert_sent")) or alert_already_sent(row.get("breakeven_recommended")):
            stats.skipped_duplicates += 1
            LOGGER.info("skipped_duplicate_breakeven_command symbol=%s report_only=1", symbol)
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
        if config.command_mode == "cornix_command" and not report_only_status:
            command = format_cornix_breakeven_command(row, config.cornix_breakeven_format)
            sent_command, command_status = send_cornix_breakeven_command(
                session,
                config,
                command,
                symbol,
                str(row.get("side", "")).upper(),
            )
            if sent_command:
                sent_any = True
                stats.cornix_commands_sent += 1
                if not config.dry_run:
                    df.at[index, "cornix_be_command_sent"] = 1
                    df.at[index, "cornix_be_stop_price"] = new_stop_price if new_stop_price is not None else ""
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
            if report_only_status and config.command_mode == "cornix_command":
                LOGGER.info("Report-only status %s blocked Cornix breakeven command for %s", signal_status, symbol)
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
            df.at[index, "new_stop_notification_sent"] = 1
            df.at[index, "new_stop_notification_at"] = datetime.now(timezone.utc).isoformat()
            df.at[index, "new_stop_notification_key"] = stop_notification_key
            df.at[index, "new_stop_notification_stop_price"] = format_price(new_stop_price) if new_stop_price is not None else ""
            df.at[index, "position_management_stage"] = "TP1_REACHED_BE_RECOMMENDED"
            sent_new_stop_keys.add(stop_notification_key)
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
    parser.add_argument("--test-cornix", action="store_true", help="Send one Cornix breakeven test command and exit.")
    return parser.parse_args()


def main() -> int:
    setup_logging()
    config = load_config()
    args = parse_args()
    if args.test_cornix or config.cornix_test_mode:
        success = send_cornix_test_command(build_session(), config)
        return 0 if success else 1
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
