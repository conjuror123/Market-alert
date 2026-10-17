"""Telegram notification sender."""
from __future__ import annotations

import html
import logging
import os
import re
import time

import requests

log = logging.getLogger("price_monitor.notifier")


class TelegramError(RuntimeError):
    pass


_BOT_TOKEN = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
# Twelve Data spells it apikey=, FRED api_key=.
_APIKEY = re.compile(r"(?i)\b(api_?key)=[^&\s]+")
_AUTH = re.compile(r"(?i)(authorization:\s*)\S.*")


def redact_secrets(text: str) -> str:
    """Strip bot tokens, apikey= / api_key= values and Authorization headers from text.

    RequestException and provider URLs otherwise land in ops/health messages
    carrying the Telegram token (it is in the request path) or an API key.
    """
    text = _BOT_TOKEN.sub("bot<redacted>", text)
    text = _APIKEY.sub(r"\1=<redacted>", text)
    text = _AUTH.sub(r"\1<redacted>", text)
    return text


# TOO MANY REQUESTS. Telegram allows about twenty messages a minute into one
# channel, edits included, and answers 429 with how many seconds to wait
# (`parameters.retry_after`). The run waits that long and asks again rather
# than giving the message up until the next hour. Bounded, so a long ban cannot
# hold the hourly job: a wait over the cap, or a refusal after the last retry,
# is an error like any other and the message is retried by the next run.
RETRY_AFTER_CAP_SECONDS = 60
RETRIES_ON_429 = 3
# And all the waiting of one run together: a long ban met by many edits would
# otherwise wait 3 x 60 s per message, past the job's timeout. Once spent, a
# 429 fails at once and the next run tries again.
WAIT_BUDGET_SECONDS = 180
_waited = [0.0]


def _retry_after(resp) -> "float | None":
    try:
        return float(resp.json()["parameters"]["retry_after"])
    except (ValueError, KeyError, TypeError):
        return None


def _post(url: str, payload: dict, timeout: int):
    """POST to the Bot API, waiting out Telegram's 429s. Raises
    requests.RequestException like requests.post does."""
    for attempt in range(RETRIES_ON_429 + 1):
        resp = requests.post(url, json=payload, timeout=timeout)
        if resp.status_code != 429 or attempt == RETRIES_ON_429:
            return resp
        wait = _retry_after(resp)
        if (wait is None or wait > RETRY_AFTER_CAP_SECONDS
                or _waited[0] + wait > WAIT_BUDGET_SECONDS):
            return resp
        log.warning("Telegram says too many requests: waiting %.0f s", wait)
        _waited[0] += wait + 0.5
        time.sleep(wait + 0.5)
    return resp                                  # pragma: no cover - loop returns


def send_telegram_message(bot_token: str, chat_id: str, text: str, timeout: int = 15,
                          silent: bool = False) -> int:
    """Send a message, returning its Telegram message_id (needed to edit it later).

    `silent` delivers it without a sound (disable_notification)."""
    if not bot_token or not chat_id:
        raise TelegramError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not configured")

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if silent:
        payload["disable_notification"] = True
    try:
        resp = _post(url, payload, timeout)
    except requests.RequestException:
        # The token is in the URL; str(exc) would put it in health/ops text.
        raise TelegramError("Telegram request failed") from None
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
    try:
        resp = _post(
            url,
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout,
        )
    except requests.RequestException:
        raise TelegramError("Telegram request failed") from None
    if resp.status_code != 200:
        # Telegram returns 400 when the text is already what the message says.
        # A restyle that re-renders the same string must not fail the run.
        if resp.status_code == 400 and "message is not modified" in resp.text.lower():
            return
        raise TelegramError(f"Telegram API error {resp.status_code}: {resp.text[:300]}")


def delete_telegram_message(
    bot_token: str, chat_id: str, message_id: int, timeout: int = 15
) -> bool:
    """Remove a message the bot sent. True if it is gone, False if it cannot be.

    Returns rather than raises on the expected refusals, because the caller is
    sweeping a list and one message it may no longer delete must not stop it
    clearing the rest.

    The bot posts to a private channel where it is an administrator with
    "Delete messages" (can_delete_messages), and there it can delete any
    message, whatever its age. A refusal therefore means something else - the
    message is already gone, or the admin right was taken away - and Telegram's
    own reason is logged rather than guessed at. The caller does not depend on
    the delete landing: a refused delete is struck through by an edit instead
    (jump_delivery._delete).
    """
    if not bot_token or not chat_id:
        raise TelegramError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not configured")

    url = f"https://api.telegram.org/bot{bot_token}/deleteMessage"
    try:
        resp = _post(url, {"chat_id": chat_id, "message_id": message_id}, timeout)
    except requests.RequestException:
        return False
    if resp.status_code == 200:
        return True
    # Already deleted, too old, or never ours: all mean "stop tracking it".
    if resp.status_code in (400, 403):
        try:
            why = resp.json().get("description", resp.text[:200])
        except ValueError:                       # pragma: no cover - defensive
            why = resp.text[:200]
        log.warning("Telegram would not delete message %s: %s",
                    message_id, redact_secrets(str(why)))
        return False
    raise TelegramError(f"Telegram API error {resp.status_code}: {resp.text[:300]}")


# HEALTH. Every health message goes out through send_health, and every error,
# exception or provider string in one through quote. The messages are HTML: a
# "<" quoted raw is a tag Telegram refuses, with the whole message. And past
# Telegram's 4,096 characters it refuses the message too - the wider the
# outage, the longer the list, the surer the silence.
TELEGRAM_LIMIT = 4096


def quote(text: str, limit: int = 300) -> str:
    """Outside text, as text: secrets out, HTML escaped, at most `limit` long."""
    text = redact_secrets(str(text))
    if len(text) > limit:
        text = text[:limit - 1] + "…"
    return html.escape(text, quote=False)


def send_health(text: str, token: "str | None" = None, chat: "str | None" = None) -> bool:
    """Send to the health chat - never the channel - cut to fit. Every line
    closes its own tags, so the cut falls between lines. Returns whether it went;
    a failure is logged, not raised: health must not break the run it reports."""
    token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = chat if chat is not None else os.environ.get("TELEGRAM_HEALTH_CHAT_ID", "")
    if not token or not chat:
        log.warning("No health chat configured (TELEGRAM_HEALTH_CHAT_ID); not sent: %s",
                    text.splitlines()[0] if text else "")
        return False
    lines = text.splitlines()

    def first(n: int) -> str:
        tail = [f"…and {len(lines) - n} more lines"] if n < len(lines) else []
        return "\n".join(lines[:n] + tail)

    kept = len(lines)
    while kept and len(first(kept)) > TELEGRAM_LIMIT:
        kept -= 1
    try:
        send_telegram_message(token, chat, first(kept))
        return True
    except TelegramError as exc:
        log.error("Failed to send a health message: %s", exc)
        return False
