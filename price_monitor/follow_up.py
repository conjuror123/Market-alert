"""Correcting a push in place, once the market has answered.

A push goes out the moment the move is found, because a once-in-three-years
move that arrives six hours late is a worse product than one that arrives now
and is corrected later. The correction is this module: at the close of the day
the move happened and again at the close of the next day the instrument trades,
the ORIGINAL message is edited to say how the move actually held. Nothing new
arrives on the phone - Telegram edits in place, so the record of an event stays
one message rather than three.

WHAT IS BEING CHECKED. Retention, the event study's own measure: the abnormal
return accumulated from the event bar through h bars later, divided by the
abnormal return on the event bar itself (see tremor.persistence). 1.0 means the
move held exactly, 0 means it gave everything back, above 1 means it kept
going. So "still there at 24h" is a statement about whether the move was
information or somebody's liquidity, not about whether the price is merely near
where it was.

WHY IT IS SAFE TO EDIT LATE. The horizons are read off the asset's own trading
calendar, so a Friday-evening move is not declared reverted by a closed market
over the weekend - the answer simply arrives on Monday, and the unanswered lines
say so with the date they are due (see tremor_delivery.due_moment). The edit is idempotent: it
re-renders the whole message from the event row every time, so a run that edits
twice writes the same text twice rather than appending to itself.

WHY IT CANNOT SPAM. Editing needs the message id Telegram returned when the
push was sent, so only messages this bot sent in the tracked window can be
touched at all, and each (event, horizon) pair is recorded once it lands. A
tracked push is forgotten after TRACK_HOURS, not the moment the last
check-in lands: dropping it then is how a finished push kept an old template
after later copy tweaks. Ten days is still the ceiling, so the state file
cannot grow without bound.
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

# How long a push stays editable. Six BARS is most of a day in an instrument
# that trades six and a half hours, and the settled reading waits for the next
# session to close, so the wall-clock ceiling has to be generous or the last
# check-in would be dropped exactly for the instruments whose bars are scarcest.
# Ten days covers a long weekend, a holiday and a stalled scheduler on top.
TRACK_HOURS = 240


def track(store: dict, event: dict, message_id: int,
          text_hash: str = "") -> None:
    """Remember a sent push so its message can be corrected later."""
    tracked: dict = store.setdefault(TRACKED, {})
    tracked[str(event["event_id"])] = {
        "message_id": int(message_id),
        "hour_utc": int(event["hour_utc"]),
        "written": [],
        "text_hash": str(text_hash or ""),
    }


def _due(record: dict, event: dict, horizons) -> list[int]:
    """Which horizons now have an answer that has not been written yet."""
    raw_basis = str(event.get("basis") or "") == "absolute"
    written = set(record.get("written") or [])
    out = []
    for h in horizons:
        if h in written:
            continue
        key = f"retention_raw_{h}" if raw_basis else f"retention_{h}"
        if tremor_delivery._clean(event.get(key)) is not None:
            out.append(h)
    return out


def _prune(tracked: dict, now: datetime) -> dict:
    cutoff = now.timestamp() - TRACK_HOURS * 3600
    return {k: v for k, v in tracked.items()
            if float(v.get("hour_utc", 0)) >= cutoff}


# Pushes whose check-ins completed before `sent` stored the Telegram id.
# Without this the live XLF message (id 14) cannot be restyled: tracked was
# emptied the hour the settled line landed.
_LEGACY_PUSH_IDS = {
    "twelvedata_XLF:1789405200": 14,
}


def rehydrate(store: dict) -> None:
    """Puts finished pushes back on the tracked list while they are still editable.

    A send stores `{hour, id, hash}`. A record left in the older bare-hour shape
    carries no message id, and is recovered from a leftover tracked row or from
    the handful of live ids known at the time.
    """
    from price_monitor import tremor_delivery

    sent: dict = store.setdefault(tremor_delivery._SENT, {})
    tracked: dict = store.setdefault(TRACKED, {})
    for event_id, rec in list(sent.items()):
        mid = tremor_delivery._sent_message_id(rec)
        hour = tremor_delivery._sent_hour(rec)
        if mid is None:
            mid = (tracked.get(event_id) or {}).get("message_id") \
                or _LEGACY_PUSH_IDS.get(event_id)
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
            "written": [],
            "text_hash": str((rec or {}).get("hash") if isinstance(rec, dict) else "") or "",
        }


def apply(cfg: Config, state: dict, events: "list[dict]",
          calendar: "list[dict] | None" = None,
          now: datetime | None = None,
          restyle_after: int | None = None,
          rate_history: "list[dict] | None" = None) -> int:
    """Edits every tracked push whose next check-in has arrived.

    Also re-renders pushes whose hour is in the current digest window when the
    formatted text has changed - a copy tweak should rewrite what is already
    on the phone on the next run, not wait for a retention check-in.

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
    since = None if restyle_after is None else int(restyle_after)
    if rate_history is None:
        rate_history = tremor_delivery.load_rate_history()

    for event_id, record in list(tracked.items()):
        event = by_id.get(event_id)
        if event is None:
            # Still tracked so a later run can restyle if the row comes back;
            # dropping the message id is how a /floor change froze old pushes.
            log.info("Tracked push %s has no row this run; holding the message id",
                     event_id)
            continue

        landed = _due(record, event, tremor_delivery.FOLLOW_UP_HORIZONS)
        text = tremor_delivery.format_push(
            event, labels, calendar, events, now, rate_history)
        mark = tremor_delivery._fingerprint(text)
        restyle = (
            since is not None
            and int(event.get("hour_utc") or 0) >= since
            and mark != str(record.get("text_hash") or "")
        )
        if not landed and not restyle:
            continue

        # Re-rendered whole, which is how the check-in lines get their
        # answers: at the hour a push is sent neither close has happened, so
        # how the move held arrives later and reaches the reader through this
        # edit rather than through a second message.
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
        if landed:
            # Kept in the horizons' own order rather than sorted. They are not all
            # the same kind of thing - two and six are bar counts, "settled" is a
            # moment - and sorting a set holding both raises the moment the third
            # answer lands on a push whose first two are already written. That is
            # every push, and the exception would surface inside the hourly
            # delivery run rather than here.
            written = set(record.get("written") or []) | set(landed)
            record["written"] = [h for h in tremor_delivery.FOLLOW_UP_HORIZONS
                                 if h in written]
            log.info("Updated push %s with the %s check-in",
                     event_id, ", ".join(str(h) for h in landed))
        else:
            log.info("Restyled push %s", event_id)
        edited += 1

    store[TRACKED] = _prune(tracked, now)
    return edited
