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
    """Telegram did not do what was asked: refused it, or was never reached."""


class TelegramUnsure(TelegramError):
    """The request may have reached Telegram, but no answer said what became of
    it: a message sent this way can still appear, at any later time.

    2026-11-03 14:07: two sends timed out after 15 s, the run recorded them
    unsent, and Telegram published both at 14:40 (ids 137 and 138) - the next
    run's repeat was a duplicate. So only a request that certainly never
    reached Telegram is "not sent" (_never_sent); every other failure is this,
    and telegram_ledger finds out what happened by reading the channel."""


# How long to wait. The channel's requests may take long when Telegram is slow,
# and one that times out becomes "unsure", which costs a check later - so they
# wait a minute. The health chat and the ledger's reads are only worth a short
# wait: neither may hold up the hourly job (20 minutes in all).
CHANNEL_TIMEOUT = (10, 60)
SHORT_TIMEOUT = (5, 15)


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


def _never_sent(exc: requests.RequestException) -> bool:
    """Whether a failed request certainly did not reach Telegram: no connection
    was made at all. A read timeout, a connection dropped after the request
    went out or an answer cut short may each have been acted on."""
    from urllib3.exceptions import NameResolutionError, NewConnectionError

    if isinstance(exc, (requests.exceptions.ConnectTimeout, requests.exceptions.SSLError)):
        return True
    if isinstance(exc, requests.exceptions.ConnectionError):
        reason = exc.args[0] if exc.args else None
        reason = getattr(reason, "reason", reason)
        return isinstance(reason, (NewConnectionError, NameResolutionError))
    return False


def _failed(exc: requests.RequestException) -> TelegramError:
    # The token is in the URL; str(exc) would put it in health/ops text.
    if _never_sent(exc):
        return TelegramError("Telegram request failed")
    return TelegramUnsure("Telegram did not answer")


def _refused(resp) -> TelegramError:
    """A non-200 answer: a refusal (4xx), or Telegram's own trouble (5xx), after
    which the request may still have been carried out."""
    error = TelegramUnsure if resp.status_code >= 500 else TelegramError
    return error(f"Telegram API error {resp.status_code}: {resp.text[:300]}")


def _ask(url: str, payload: dict, timeout) -> dict:
    """POST to the Bot API and return its `result`. Raises TelegramError when the
    request certainly did nothing, TelegramUnsure when it may have."""
    try:
        resp = _post(url, payload, timeout)
    except requests.RequestException as exc:
        raise _failed(exc) from None
    if resp.status_code != 200:
        raise _refused(resp)
    try:
        return resp.json()["result"]
    except (ValueError, KeyError, TypeError):
        # A 200 whose body cannot be read: done, but its id is lost.
        raise TelegramUnsure("Telegram's answer could not be read") from None


def send_telegram_message(bot_token: str, chat_id: str, text: str,
                          timeout=CHANNEL_TIMEOUT, silent: bool = False) -> int:
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
    result = _ask(url, payload, timeout)
    try:
        return result["message_id"]
    except (KeyError, TypeError):
        raise TelegramUnsure("Telegram's answer carried no message id") from None


def edit_telegram_message(
    bot_token: str, chat_id: str, message_id: int, text: str, timeout=CHANNEL_TIMEOUT
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
    except requests.RequestException as exc:
        raise _failed(exc) from None
    if resp.status_code != 200:
        # Telegram returns 400 when the text is already what the message says.
        # A restyle that re-renders the same string must not fail the run.
        if resp.status_code == 400 and "message is not modified" in resp.text.lower():
            return
        raise _refused(resp)


def delete_telegram_message(
    bot_token: str, chat_id: str, message_id: int, timeout=CHANNEL_TIMEOUT
) -> bool:
    """Remove a message the bot sent. True if it is gone, False if it cannot be.

    Returns rather than raises on the expected refusals, because the caller is
    sweeping a list and one message it may no longer delete must not stop it
    clearing the rest. A request that got no answer raises (TelegramError, or
    TelegramUnsure): it said nothing about the message, and the caller tries
    again rather than striking it through.

    The bot posts to a private channel where it is an administrator with
    "Delete messages" (can_delete_messages).  Telegram's Bot API limits
    deletion to messages up to 48 hours old; the caller
    (jump_delivery._expire_pings) deletes pings well within that window.
    A refusal therefore means the message is already gone, too old, or the
    admin right was taken away - and Telegram's own reason is logged rather
    than guessed at. The caller does not depend on the delete landing: a
    refused delete is struck through by an edit instead
    (jump_delivery._delete).
    """
    if not bot_token or not chat_id:
        raise TelegramError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not configured")

    url = f"https://api.telegram.org/bot{bot_token}/deleteMessage"
    try:
        resp = _post(url, {"chat_id": chat_id, "message_id": message_id}, timeout)
    except requests.RequestException as exc:
        raise _failed(exc) from None
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
    raise _refused(resp)


def forward_telegram_message(bot_token: str, from_chat: str, message_id: int,
                             to_chat: str, timeout=SHORT_TIMEOUT) -> dict:
    """Reads one message of `from_chat` by its id - the Bot API has no other
    way: forwards it, silently, to `to_chat` and returns what the answer says.

    {"state": "found", "text": its text ("" for none), "copy": the forward's
    own id in `to_chat`, for the caller to delete, "date": when it was posted
    (0 if Telegram does not say)}; {"state": "missing"}: no
    message has that id (deleted, or not yet used); {"state": "unforwardable"}:
    one that cannot be forwarded - a service message such as a pin, or every
    message of a chat that protects its content. Raises TelegramError or
    TelegramUnsure when Telegram did not say."""
    if not bot_token or not from_chat or not to_chat:
        raise TelegramError("TELEGRAM_BOT_TOKEN / the chats are not configured")
    url = f"https://api.telegram.org/bot{bot_token}/forwardMessage"
    payload = {"chat_id": to_chat, "from_chat_id": from_chat, "message_id": int(message_id),
               "disable_notification": True}
    try:
        resp = _post(url, payload, timeout)
    except requests.RequestException as exc:
        raise _failed(exc) from None
    if resp.status_code == 400:
        why = resp.text.lower()
        if "not found" in why:
            return {"state": "missing"}
        if "can't be forwarded" in why or "protected" in why:
            return {"state": "unforwardable"}
    if resp.status_code != 200:
        raise _refused(resp)
    try:
        result = resp.json()["result"]
        copy = int(result["message_id"])
    except (ValueError, KeyError, TypeError):
        raise TelegramUnsure("Telegram's answer could not be read") from None
    # A post that is itself a forward names its own origin and that post's
    # date, older than ours: it reads as somebody else's, as it is.
    origin = result.get("forward_origin") or {}
    when = origin.get("date") or result.get("forward_date") or 0
    return {"state": "found", "text": str(result.get("text") or ""), "copy": copy,
            "date": int(when)}


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
        send_telegram_message(token, chat, first(kept), timeout=SHORT_TIMEOUT)
        return True
    except TelegramError as exc:
        log.error("Failed to send a health message: %s", exc)
        return False
