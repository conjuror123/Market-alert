"""What happens to a push after it is on the channel.

THE RULE. Delivery deletes two kinds of message and no others: pings
(tremor_delivery.sweep_pings, restyle_pings), and a day's lower messages once
the day has grown and its rarer message is on the channel. Everything else that
changes is corrected in place by an edit. The bot posts to a public channel
where it is an administrator; it can edit its own messages at any age and
delete any message there. A delete Telegram refuses anyway is struck through by
an edit instead, so nothing here depends on it.

Per tracked push, every run:

  * a rarer event of the same instrument's day has been delivered - the day
    grew, e.g. `high` at 10:00 and `major` at 15:00. This push is the day's
    lower message: it is DELETED.
  * its event is gone from the table and another event of the same instrument
    and day is on the channel - the day's message is now that one, so this is
    a lower message of the same day: DELETED.
  * its event is gone and nothing else of that day is on the channel: it stays
    as it was sent.
  * its event is still there: re-rendered, and EDITED if the text changed (the
    bar healed and the move or its size in σ moved). A push whose word fell to
    `noticeable` stays this push, corrected; the note does not list it twice.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from price_monitor import tremor_delivery
from price_monitor.config import Config
from price_monitor.notifier import (TelegramError, delete_telegram_message,
                                    edit_telegram_message)

log = logging.getLogger("price_monitor.follow_up")

# Where the tracked pushes live inside the delivery layer's own state blob.
TRACKED = "tracked"

# How long a push stays editable. Ten days covers a long weekend, a holiday and
# a stalled scheduler on top of the few hours a bar takes to heal.
TRACK_HOURS = 240


def track(store: dict, event: dict, message_id: int,
          text_hash: str = "", text: str = "") -> None:
    """Remember a sent push so its message can be corrected later: its
    instrument and day, to know a later message of the same day, and its first
    line, to strike through should a delete be refused."""
    tracked: dict = store.setdefault(TRACKED, {})
    day = event.get("day")
    tracked[str(event["event_id"])] = {
        "message_id": int(message_id),
        "hour_utc": int(event["hour_utc"]),
        "asset_id": str(event.get("asset_id", "")),
        "day": int(day) if day is not None and day == day else None,
        "text_hash": str(text_hash or ""),
        "first": (text or "").split("\n", 1)[0],
    }


def _prune(tracked: dict, now: datetime) -> dict:
    cutoff = now.timestamp() - TRACK_HOURS * 3600
    return {k: v for k, v in tracked.items()
            if float(v.get("hour_utc", 0)) >= cutoff}


def rehydrate(store: dict) -> None:
    """Puts sent pushes back on the tracked list while they are still editable.

    A send stores `{hour, id, hash}`. A record left in the older bare-hour shape
    carries no message id, and is recovered from a leftover tracked row. A push
    already removed from the channel (`gone`) is not tracked again.
    """
    sent: dict = store.setdefault(tremor_delivery._SENT, {})
    tracked: dict = store.setdefault(TRACKED, {})
    for event_id, rec in list(sent.items()):
        if isinstance(rec, dict) and rec.get("gone"):
            continue
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


def _struck(first: str, note: str) -> str:
    """A message that no longer stands, said as such: the old line struck
    through, and why beneath it."""
    head = f"<s>{first}</s>\n" if first else ""
    return head + note


def _retire(cfg: Config, store: dict, event_id: str, record: dict, why: str) -> bool:
    """Deletes a day's lower push. If Telegram refuses, strikes it through."""
    message_id = int(record["message_id"])
    try:
        gone = bool(delete_telegram_message(cfg.telegram_bot_token,
                                            cfg.telegram_chat_id, message_id))
    except TelegramError as exc:
        log.error("Could not delete push %s: %s", event_id, exc)
        return False
    if not gone:
        try:
            edit_telegram_message(cfg.telegram_bot_token, cfg.telegram_chat_id,
                                  message_id, _struck(record.get("first", ""), why))
        except TelegramError as exc:
            log.error("Could not strike push %s through: %s", event_id, exc)
            return False
    store.get(TRACKED, {}).pop(event_id, None)
    sent_rec = store.setdefault(tremor_delivery._SENT, {}).get(event_id)
    if isinstance(sent_rec, dict):
        sent_rec["gone"] = True
    log.info("Push %s %s: %s", event_id, "deleted" if gone else "struck through", why)
    return True


def _delivered_same_day(event_id: str, record: dict, events: "list[dict]",
                        sent: dict) -> "dict | None":
    """Another event of this push's instrument and day that is on the channel."""
    if record.get("day") is None:
        return None
    for e in events:
        other = str(e.get("event_id"))
        if (other != event_id
                and str(e.get("asset_id")) == str(record.get("asset_id"))
                and e.get("day") is not None and int(e["day"]) == int(record["day"])
                and other in sent
                and not (isinstance(sent[other], dict) and sent[other].get("gone"))):
            return e
    return None


def _when(event: dict) -> str:
    moment = datetime.fromtimestamp(int(event["hour_utc"]), tz=timezone.utc)
    return f"{moment:%H:%M} UTC"


def apply(cfg: Config, state: dict, events: "list[dict]",
          calendar: "list[dict] | None" = None,
          now: datetime | None = None) -> int:
    """Brings every tracked push in line with the table; see the module
    docstring for the rule. Returns how many messages were edited or deleted.

    An empty table does nothing: that is "the pipeline did not run", not
    "every event vanished". Failures are logged and left in the tracking
    record, so a Telegram outage means a retry on the next run.
    """
    now = now or datetime.now(timezone.utc)
    store = state.setdefault(tremor_delivery.STATE_KEY, {})
    rehydrate(store)
    tracked: dict = store.setdefault(TRACKED, {})
    if not tracked or not events:
        store[TRACKED] = _prune(tracked, now)
        return 0

    sent: dict = store.setdefault(tremor_delivery._SENT, {})
    by_id = {str(e.get("event_id")): e for e in events}
    labels = tremor_delivery._labels()
    changed = 0

    for event_id, record in list(tracked.items()):
        event = by_id.get(event_id)

        if event is not None:
            top_id = tremor_delivery.superseder(event)
            if top_id and top_id in sent and top_id in by_id:
                top = by_id[top_id]
                why = (f"the day grew: {tremor_delivery.TIER_EMOJI.get(str(top.get('tier')), '')}"
                       f" at {_when(top)}")
                changed += _retire(cfg, store, event_id, record, why)
                continue
            text = tremor_delivery.format_push(event, labels, calendar)
        else:
            top = _delivered_same_day(event_id, record, events, sent)
            if top is None:
                log.info("Tracked push %s has no row this run; it stays as sent", event_id)
                continue
            why = (f"the day's move is the one at {_when(top)} "
                   f"{tremor_delivery.TIER_EMOJI.get(str(top.get('tier')), '')}")
            changed += _retire(cfg, store, event_id, record, why)
            continue

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
        sent_rec = sent.get(event_id)
        if isinstance(sent_rec, dict):
            sent_rec["hash"] = mark
            sent_rec.setdefault("id", int(record["message_id"]))
        log.info("Restyled push %s", event_id)
        changed += 1

    store[TRACKED] = _prune(tracked, now)
    return changed
