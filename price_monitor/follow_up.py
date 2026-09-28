"""Keeping a sent push in line with its row.

A push goes out the moment the move is found, and is final in the sense that
nothing is folded into it. What it says can still change: the hourly run scores
an hour a few minutes after it began, and the bar heals on the next fetch
(CLAUDE.md, invariant 7), so the move, and with it the size in σ, can move
after the message is on the phone. This module re-renders every tracked push on
each run and edits it in place when the text has changed. Nothing new arrives
on the phone - Telegram edits silently - so the record of an event stays one
message.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from price_monitor import tremor_delivery
from price_monitor.config import Config
from price_monitor.notifier import TelegramError, edit_telegram_message

log = logging.getLogger("price_monitor.follow_up")

# Where the tracked pushes live inside the delivery layer's own state blob.
TRACKED = "tracked"

# How long a push stays editable. Ten days covers a long weekend, a holiday and
# a stalled scheduler on top of the few hours a bar takes to heal.
TRACK_HOURS = 240


def track(store: dict, event: dict, message_id: int,
          text_hash: str = "") -> None:
    """Remember a sent push so its message can be corrected later."""
    tracked: dict = store.setdefault(TRACKED, {})
    tracked[str(event["event_id"])] = {
        "message_id": int(message_id),
        "hour_utc": int(event["hour_utc"]),
        "text_hash": str(text_hash or ""),
    }


def _prune(tracked: dict, now: datetime) -> dict:
    cutoff = now.timestamp() - TRACK_HOURS * 3600
    return {k: v for k, v in tracked.items()
            if float(v.get("hour_utc", 0)) >= cutoff}


def rehydrate(store: dict) -> None:
    """Puts sent pushes back on the tracked list while they are still editable.

    A send stores `{hour, id, hash}`. A record left in the older bare-hour shape
    carries no message id, and is recovered from a leftover tracked row.
    """
    from price_monitor import tremor_delivery

    sent: dict = store.setdefault(tremor_delivery._SENT, {})
    tracked: dict = store.setdefault(TRACKED, {})
    for event_id, rec in list(sent.items()):
        mid = tremor_delivery._sent_message_id(rec)
        hour = tremor_delivery._sent_hour(rec)
        if mid is None:
            mid = (tracked.get(event_id) or {}).get("message_id")
        if not mid or not hour:
            continue
        if not isinstance(rec, dict):
            sent[event_id] = {"hour": int(hour), "id": int(mid), "hash": ""}
        elif rec.get("id") is None:
            rec["id"] = int(mid)
        if event_id in tracked:
            continue
        tracked[event_id] = {
            "message_id": int(mid),
            "hour_utc": int(hour),
            "text_hash": str((rec or {}).get("hash") if isinstance(rec, dict) else "") or "",
        }


def apply(cfg: Config, state: dict, events: "list[dict]",
          calendar: "list[dict] | None" = None,
          now: datetime | None = None) -> int:
    """Edits every tracked push whose rendered text has changed.

    Returns how many messages were edited. Failures are logged and left in the
    tracking record, so a Telegram outage means the correction is retried on
    the next run rather than lost.
    """
    now = now or datetime.now(timezone.utc)
    store = state.setdefault(tremor_delivery.STATE_KEY, {})
    rehydrate(store)
    tracked: dict = store.setdefault(TRACKED, {})
    if not tracked:
        return 0

    by_id = {str(e.get("event_id")): e for e in events}
    labels = tremor_delivery._labels()
    edited = 0

    for event_id, record in list(tracked.items()):
        event = by_id.get(event_id)
        if event is None:
            # Still tracked so a later run can restyle if the row comes back;
            # dropping the message id is how a floor change once froze old pushes.
            log.info("Tracked push %s has no row this run; holding the message id",
                     event_id)
            continue

        text = tremor_delivery.format_push(event, labels, calendar)
        mark = tremor_delivery._fingerprint(text)
        if mark == str(record.get("text_hash") or ""):
            continue
        try:
            edit_telegram_message(cfg.telegram_bot_token, cfg.telegram_chat_id,
                                  int(record["message_id"]), text)
        except TelegramError as exc:
            log.error("Could not update push %s: %s", event_id, exc)
            continue

        record["text_hash"] = mark
        sent_rec = store.setdefault(tremor_delivery._SENT, {}).get(event_id)
        if isinstance(sent_rec, dict):
            sent_rec["hash"] = mark
            sent_rec.setdefault("id", int(record["message_id"]))
        log.info("Restyled push %s", event_id)
        edited += 1

    store[TRACKED] = _prune(tracked, now)
    return edited
