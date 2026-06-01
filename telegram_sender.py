# -*- coding: utf-8 -*-
"""Telegram channel routing helpers.

V1 routing keeps each channel single-purpose:
- signals: open-position signal messages only
- cornix: Cornix-ready dry-run signal messages only
- reports: outcomes, position management, summaries, analytics
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests


LOGGER = logging.getLogger("telegram_sender")


@dataclass(frozen=True)
class TelegramRoutes:
    token: str = ""
    signals_chat_id: str = ""
    cornix_chat_id: str = ""
    reports_chat_id: str = ""

    def chat_id(self, channel: str) -> str:
        if channel == "signals":
            return self.signals_chat_id.strip()
        if channel == "cornix":
            return self.cornix_chat_id.strip()
        if channel == "reports":
            return self.reports_chat_id.strip()
        return ""


def send_text(
    session: requests.Session,
    routes: TelegramRoutes,
    channel: str,
    message: str,
    label: str,
    timeout: int = 20,
) -> bool:
    chat_id = routes.chat_id(channel)
    if not routes.token or not chat_id:
        LOGGER.warning("Telegram %s skipped: token/%s chat id missing", label, channel)
        return False
    try:
        response = session.post(
            f"https://api.telegram.org/bot{routes.token}/sendMessage",
            data={"chat_id": chat_id, "text": message},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        LOGGER.error("Telegram %s failed: %s", label, exc)
        return False
    if response.status_code != 200:
        LOGGER.error("Telegram %s failed: %s", label, response.text)
        return False
    return True
