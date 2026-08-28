"""Telegram notification sender."""
from __future__ import annotations

import requests


class TelegramError(RuntimeError):
    pass


def send_telegram_message(bot_token: str, chat_id: str, text: str, timeout: int = 15) -> int:
    """Send a message, returning its Telegram message_id (needed to edit it later)."""
    if not bot_token or not chat_id:
        raise TelegramError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not configured")

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    resp = requests.post(
        url,
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise TelegramError(f"Telegram API error {resp.status_code}: {resp.text[:300]}")
    return resp.json()["result"]["message_id"]


def edit_telegram_message(
    bot_token: str, chat_id: str, message_id: int, text: str, timeout: int = 15
) -> None:
    """Replace the text of an already-sent message (used to add an explanation later)."""
    if not bot_token or not chat_id:
        raise TelegramError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not configured")

    url = f"https://api.telegram.org/bot{bot_token}/editMessageText"
    resp = requests.post(
        url,
        json={
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise TelegramError(f"Telegram API error {resp.status_code}: {resp.text[:300]}")
