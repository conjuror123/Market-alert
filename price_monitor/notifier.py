"""Telegram notification sender."""
from __future__ import annotations

import logging
import re
import time

import requests

log = logging.getLogger("price_monitor.notifier")


class TelegramError(RuntimeError):
    pass


_BOT_TOKEN = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
_APIKEY = re.compile(r"(?i)apikey=[^&\s]+")
_AUTH = re.compile(r"(?i)(authorization:\s*)\S.*")


def redact_secrets(text: str) -> str:
    """Strip bot tokens, apikey= values and Authorization headers from text.

    RequestException and provider URLs otherwise land in ops/health messages
    carrying the Telegram token (it is in the request path) or an API key.
    """
    text = _BOT_TOKEN.sub("bot<redacted>", text)
    text = _APIKEY.sub("apikey=<redacted>", text)
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


def fetch_telegram_updates(bot_token: str, offset: int | None = None,
                           timeout: int = 15) -> list[dict]:
    """Pending updates for this bot, oldest first. Empty when nothing is waiting.

    Long-polling is not used: the hourly job asks once and moves on. `offset`
    is the next update_id to read, so a processed batch is not seen again.
    """
    if not bot_token:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not configured")

    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = int(offset)
    try:
        resp = requests.get(url, params=params, timeout=timeout)
    except requests.RequestException:
        raise TelegramError("Telegram request failed") from None
    if resp.status_code != 200:
        raise TelegramError(f"Telegram API error {resp.status_code}: {resp.text[:300]}")
    return list(resp.json().get("result") or [])


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

    The bot posts to a PUBLIC CHANNEL where it is an administrator with
    "Delete messages" (can_delete_messages), and there it can delete any
    message, whatever its age. A refusal therefore means something else - the
    message is already gone, or the admin right was taken away - and Telegram's
    own reason is logged rather than guessed at. The caller does not depend on
    the delete landing: a refused delete is struck through by an edit instead
    (tremor_delivery._delete).
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
