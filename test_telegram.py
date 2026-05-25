# -*- coding: utf-8 -*-
"""Send a Telegram connectivity test without creating trade signals."""

from __future__ import annotations

import sys
import os
from pathlib import Path

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_DIR = Path(__file__).resolve().parent
TEST_CHART = BASE_DIR / "charts" / "test_chart.png"


def build_session() -> requests.Session:
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def describe_telegram_error(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text

    description = payload.get("description") or response.text
    if response.status_code == 401:
        return f"Telegram rejected the bot token: {description}"
    if response.status_code == 400 and "chat not found" in description.lower():
        return f"Telegram chat id looks wrong or the bot cannot access the chat: {description}"
    if response.status_code == 403:
        return f"Telegram access denied. The bot may be blocked or not added to the chat: {description}"
    return description


def send_message(session: requests.Session, token: str, chat_id: str) -> None:
    message = (
        "✅ Crypto Scanner Telegram Test\n"
        "Bot connected successfully.\n"
        "Mode: Test only\n"
        "No trade signal."
    )
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    response = session.post(url, data={"chat_id": chat_id, "text": message}, timeout=20)
    if response.status_code != 200:
        raise RuntimeError(describe_telegram_error(response))


def send_chart_if_exists(session: requests.Session, token: str, chat_id: str) -> None:
    if not TEST_CHART.exists():
        return

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    with TEST_CHART.open("rb") as image:
        response = session.post(
            url,
            data={"chat_id": chat_id, "caption": "Telegram test chart image."},
            files={"photo": image},
            timeout=30,
        )
    if response.status_code != 200:
        raise RuntimeError(f"Message sent, but test chart upload failed: {describe_telegram_error(response)}")


def main() -> int:
    load_dotenv(BASE_DIR / ".env")

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token:
        print("Telegram test failed: TELEGRAM_BOT_TOKEN is missing in .env")
        return 1
    if not chat_id:
        print("Telegram test failed: TELEGRAM_CHAT_ID is missing in .env")
        return 1

    session = build_session()
    try:
        send_message(session, token, chat_id)
        send_chart_if_exists(session, token, chat_id)
    except requests.Timeout:
        print("Telegram test failed: request timed out")
        return 1
    except requests.RequestException as exc:
        print(f"Telegram test failed: network error: {exc}")
        return 1
    except RuntimeError as exc:
        print(f"Telegram test failed: {exc}")
        return 1

    print("Telegram test sent successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
