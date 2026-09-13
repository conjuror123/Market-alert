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


def delete_telegram_message(
    bot_token: str, chat_id: str, message_id: int, timeout: int = 15
) -> bool:
    """Remove a message the bot sent. True if it is gone, False if it cannot be.

    Returns rather than raises on the expected refusals, because the caller is
    sweeping a list and one message it may no longer delete must not stop it
    clearing the rest.

    THE 48-HOUR RULE decides how far this can be relied on. A bot may delete its
    own message in a PRIVATE CHAT only within 48 hours of sending it; in a
    channel where it is an administrator with can_delete_messages there is no
    such limit. So the self-clearing ping is dependable for a reader on a
    channel and best-effort for one in a private chat, where a ping sent more
    than two days before the next note simply stays. That is a Telegram limit
    and not something a retry can get around - see the README.
    """
    if not bot_token or not chat_id:
        raise TelegramError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not configured")

    url = f"https://api.telegram.org/bot{bot_token}/deleteMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "message_id": message_id},
            timeout=timeout,
        )
    except requests.RequestException:
        return False
    if resp.status_code == 200:
        return True
    # Already deleted, too old, or never ours: all mean "stop tracking it".
    if resp.status_code in (400, 403):
        return False
    raise TelegramError(f"Telegram API error {resp.status_code}: {resp.text[:300]}")
