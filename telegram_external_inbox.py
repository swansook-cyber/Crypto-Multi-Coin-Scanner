# -*- coding: utf-8 -*-
"""Telegram External Signal Inbox polling.

External messages are parsed by External Signal Analyzer V1. Only APPROVED
external signals may be routed to Signals/Cornix channels.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from external_signal_analyzer import FIELDNAMES, EXTERNAL_SIGNALS_CSV, process_external_signal


BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"

LOGGER = logging.getLogger("telegram_external_inbox")


def ensure_external_log(path: Path = EXTERNAL_SIGNALS_CSV) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
            writer.writeheader()


def log_external_message(
    chat_id: str,
    message_id: int | str,
    raw_text: str,
    status: str = "RECEIVED",
    source: str = "External Signal Inbox",
    path: Path = EXTERNAL_SIGNALS_CSV,
) -> None:
    analysis = process_external_signal(raw_text, message_id, source=source, log_path=path, send=False)
    # Preserve the legacy status argument as context in logs without changing
    # approved-only routing behavior.
    if status != "RECEIVED":
        LOGGER.info("External message %s logged with legacy status %s", message_id, status)
    return None


def build_debug_report(raw_text: str) -> str:
    preview = (raw_text or "")[:500]
    return (
        "📥 External Signal Received\n\n"
        "Source:\n"
        "External Signal Inbox\n\n"
        "Message Preview:\n"
        f"{preview}\n\n"
        "Status:\n"
        "Received Successfully"
    )


def send_debug_report(token: str, reports_chat_id: str, message: str) -> bool:
    if not token or not reports_chat_id:
        LOGGER.warning("External inbox debug notification skipped: token/reports chat id missing")
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": reports_chat_id, "text": message},
            timeout=20,
        )
    except requests.RequestException as exc:
        LOGGER.error("External inbox debug notification failed: %s", exc)
        return False
    if response.status_code != 200:
        LOGGER.error("External inbox debug notification failed: %s", response.text)
        return False
    return True


def extract_message(update: dict[str, Any]) -> tuple[str, int, str] | None:
    message = update.get("message") or update.get("channel_post")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    message_id = int(message.get("message_id", 0))
    raw_text = str(message.get("text") or message.get("caption") or "")
    if not chat_id or not message_id:
        return None
    return chat_id, message_id, raw_text


def poll_external_inbox(
    token: str,
    external_chat_id: str,
    reports_chat_id: str,
    signals_chat_id: str = "",
    cornix_chat_id: str = "",
    offset: int | None = None,
    timeout: int = 10,
) -> int | None:
    if not token or not external_chat_id:
        LOGGER.warning("External inbox polling skipped: token/external chat id missing")
        return offset
    params: dict[str, Any] = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    try:
        response = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", params=params, timeout=timeout + 5)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        LOGGER.error("External inbox polling failed: %s", exc)
        return offset

    next_offset = offset
    for update in payload.get("result", []):
        update_id = int(update.get("update_id", 0))
        next_offset = max(next_offset or 0, update_id + 1)
        extracted = extract_message(update)
        if not extracted:
            continue
        chat_id, message_id, raw_text = extracted
        if chat_id != str(external_chat_id):
            continue
        process_external_signal(
            raw_text,
            message_id,
            token=token,
            signals_chat_id=signals_chat_id,
            cornix_chat_id=cornix_chat_id,
            reports_chat_id=reports_chat_id,
            log_path=EXTERNAL_SIGNALS_CSV,
            send=True,
        )
    return next_offset


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Poll Telegram External Signal Inbox once.")
    parser.add_argument("--offset", type=int, default=None)
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    load_dotenv(BASE_DIR / ".env")
    args = parse_args()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    external_chat_id = os.getenv("TELEGRAM_EXTERNAL_INBOX_CHAT_ID", "").strip()
    reports_chat_id = os.getenv("TELEGRAM_REPORTS_CHAT_ID", os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    signals_chat_id = os.getenv("TELEGRAM_SIGNALS_CHAT_ID", os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    cornix_chat_id = os.getenv("TELEGRAM_CORNIX_CHAT_ID", os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    next_offset = poll_external_inbox(token, external_chat_id, reports_chat_id, signals_chat_id, cornix_chat_id, args.offset)
    print(f"Next offset: {next_offset}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
