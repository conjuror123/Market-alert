"""Bring what is on Telegram back into line with what the events table says.

A REPAIR, RUN BY HAND, not part of the hourly pass. Delivery keeps the two in
step going forward; it cannot undo what an earlier version of the detector
already sent. When a scoring fix changes the past - and the partial-bar fix
changed it a great deal, because every hour had been judged on its first five
minutes - messages stay on the phone claiming things the table no longer says.

WHAT IT REPAIRS. A push is a standalone message and an interruption, so it is
wrong in two ways that a note is not:

  * the event is gone from the table. The move did not qualify once the hour was
    scored from its complete bar, so the alert is about nothing.
  * the event is still there but is no longer a push. It belongs in the note for
    its period, the note already carries it, and the reader has it twice - once
    as an interruption that should never have happened.

Both are deleted. Nothing is lost by deleting either: the first was not an
event, and the second is a row in a note that is already on the phone.

WHAT IT DOES NOT TOUCH. A note. A note is re-rendered from the table on every
run and edited in place, silently, so it converges by itself - and its rows are
real whenever they were found. This only reports on notes, so a disagreement
that cannot fix itself is at least visible.

DRY RUN UNLESS TOLD OTHERWISE. Deleting a Telegram message cannot be undone, so
the default prints the plan and changes nothing. --apply carries it out.
"""
from __future__ import annotations

import argparse
import logging
import sys

from price_monitor import follow_up, tremor_delivery
from price_monitor.config import Config, load_config
from price_monitor.notifier import TelegramError, delete_telegram_message
from price_monitor.state import CorruptState, load_state, save_state

log = logging.getLogger("price_monitor.reconcile")


def plan(events: "list[dict]", state: dict,
         conservative: bool = False) -> "list[dict]":
    """Every push on the channel that the table no longer supports.

    Returns one record per message to remove, with the reason, so the caller can
    print the plan before doing anything irreversible.

    CONSERVATIVE IS FOR THE HOURLY SWEEP. A push that is no longer an event at
    all can always go: there is nothing left to say. A push that has merely been
    DEMOTED to a digest row is different - the row belongs in the note for its
    period, and if that note is closed and frozen it does not carry it. Deleting
    the alert would then take the move off the channel altogether, which is a
    worse answer than leaving one message louder than it should have been. So
    the sweep only deletes a demoted push a note actually carries, and a hand
    run can be told to drop the rest.
    """
    store = state.get(tremor_delivery.STATE_KEY, {})
    tracked = store.get(follow_up.TRACKED, {})
    sent = store.get(tremor_delivery._SENT, {})

    by_id = {str(e.get("event_id")): e for e in events}
    out = []
    for event_id, record in tracked.items():
        message_id = record.get("message_id")
        if message_id is None:
            continue
        event = by_id.get(str(event_id))
        if event is None:
            reason = "no longer an event at all"
        elif str(event.get("channel") or "") != "push":
            # Whether the note for its period actually shows it decides both the
            # wording and, for the sweep, whether it goes at all: a frozen note
            # may predate the row existing. "You have this twice" and "this is
            # leaving the channel" are different things to be told before
            # something is deleted irreversibly.
            carried = _in_a_note(state, event)
            if conservative and not carried:
                log.info("message %s is a %s row now but no note carries it; "
                         "keeping it rather than taking the move off the channel",
                         message_id, event.get("channel"))
                continue
            where = ("the note for its period already carries it" if carried else
                     "and no note carries it - the move leaves the channel")
            reason = f"no longer a push - it is a {event.get('channel')} row, {where}"
        else:
            continue
        out.append({"event_id": str(event_id), "message_id": int(message_id),
                    "hour_utc": record.get("hour_utc"), "reason": reason,
                    "in_sent": str(event_id) in sent})
    return out


def _in_a_note(state: dict, event: dict) -> bool:
    """Whether some note's published set holds this event."""
    store = state.get(tremor_delivery.STATE_KEY, {})
    for record in store.get(tremor_delivery.DIGEST_STATE, {}).values():
        kept = record.get("events")
        if kept is None:
            start = int(record.get("from", 0))
            end = int(record.get("to", 0))
            if start <= float(event.get("hour_utc", 0)) < end:
                return True          # not frozen: it renders from the table
        elif str(event.get("event_id")) in set(kept):
            return True
    return False


def notes(events: "list[dict]", state: dict, now) -> "list[dict]":
    """What each tracked note claims to have published, against the table now."""
    from price_monitor.tremor_delivery import digest_rows, note_window

    store = state.get(tremor_delivery.STATE_KEY, {})
    out = []
    for slot, record in sorted(store.get(tremor_delivery.DIGEST_STATE, {}).items(),
                               key=lambda kv: int(kv[0])):
        window = note_window(int(slot), record)
        rows = digest_rows(events, window, now)
        out.append({"slot": int(slot), "ids": list(record.get("ids") or []),
                    "published": record.get("rows"), "now": len(rows),
                    "window": window})
    return out


def sweep(cfg: Config, state: dict, events: "list[dict]", now) -> int:
    """The hourly pass's own reconciliation. Returns how many messages went.

    THREE STAGES, and they are the same three every run: work out what the
    channel should hold, compare it with what state says is there, and change
    what differs. Delivery has always done the first two for what it ADDS; this
    is the same question asked about what should no longer be there.

    Deliberately conservative - see plan. An empty events table does nothing at
    all, because empty means "the pipeline did not run" and acting on the other
    reading would clear the channel.
    """
    if not events:
        return 0
    extra = surplus(cfg, events, state, now)
    actions = plan(events, state, conservative=True)
    if not extra and not actions:
        return 0
    gone = shed(cfg, state, extra) + apply(cfg, state, actions)
    if gone:
        log.info("Reconciled: %d message(s) the table no longer supports", gone)
    return gone


def freeze(state: dict, slot: int, event_ids: "list[str]") -> None:
    """Declare by hand what a closed note actually published.

    A note records that for itself now (tremor_delivery.published_only), but one
    that closed before it did has nothing recorded, and there is no way to work
    it out afterwards - the table has already changed, which is the whole
    problem. So it is stated, from whatever evidence there is, and the note
    renders from it thereafter.
    """
    store = state.setdefault(tremor_delivery.STATE_KEY, {})
    digests = store.setdefault(tremor_delivery.DIGEST_STATE, {})
    record = digests.get(str(slot))
    if record is None:
        raise KeyError(f"no note at {slot}")
    record["events"] = [str(e) for e in event_ids]
    record["rows"] = len(record["events"])


def surplus(cfg: Config, events: "list[dict]", state: dict, now) -> "list[dict]":
    """Parts of a closed note that its published set no longer fills.

    Rendering the note from what it really published can leave messages behind -
    fourteen rows removed is two messages of three. They are deleted rather than
    emptied: an emptied part is still a message on the phone, and the point is a
    channel that reads as though nothing went wrong.
    """
    from price_monitor.tremor_delivery import (digest_rows, format_digest,
                                               note_window, published_only)

    store = state.get(tremor_delivery.STATE_KEY, {})
    labels = tremor_delivery._labels()
    rate_history = tremor_delivery.load_rate_history()
    out = []
    for slot, record in store.get(tremor_delivery.DIGEST_STATE, {}).items():
        ids = list(record.get("ids") or [])
        if record.get("events") is None or not ids:
            continue
        window = note_window(int(slot), record)
        rows = published_only(record, digest_rows(events, window, now), window, now)
        texts = format_digest(rows, labels, window, None, now, events, rate_history)
        for index, message_id in enumerate(ids[len(texts):], start=len(texts)):
            out.append({"slot": str(slot), "index": index,
                        "message_id": int(message_id),
                        "reason": f"part {index + 1} of a note that now needs "
                                  f"{len(texts)}"})
    return out


def shed(cfg: Config, state: dict, extra: "list[dict]") -> int:
    """Deletes the surplus parts and forgets them, newest first.

    Newest first so the ids and hashes stay a prefix of the note however many
    deletes Telegram refuses: dropping a middle part would renumber the ones
    after it and the note would edit the wrong messages from then on.
    """
    store = state.setdefault(tremor_delivery.STATE_KEY, {})
    digests = store.setdefault(tremor_delivery.DIGEST_STATE, {})
    gone = 0
    for action in sorted(extra, key=lambda a: a["index"], reverse=True):
        record = digests.get(action["slot"])
        if record is None or action["index"] != len(record.get("ids", [])) - 1:
            log.warning("part %s is no longer the last of its note; leaving it",
                        action["message_id"])
            continue
        try:
            removed = delete_telegram_message(
                cfg.telegram_bot_token, cfg.telegram_chat_id, action["message_id"])
        except TelegramError as exc:
            log.error("message %s: %s", action["message_id"], exc)
            continue
        if not removed:
            log.warning("message %s could not be deleted - Telegram refused it, "
                        "which for a private chat means it is over 48 hours old",
                        action["message_id"])
            continue
        record["ids"].pop()
        record["hashes"].pop()
        gone += 1
        log.info("deleted message %s (%s)", action["message_id"], action["reason"])
        save_state(cfg.state_path, state)
    return gone


def apply(cfg: Config, state: dict, actions: "list[dict]") -> int:
    """Deletes each planned message. Returns how many are actually gone.

    State is only forgotten for a message Telegram confirms it removed. A delete
    it refuses - the 48-hour rule on a private chat - leaves the record alone, so
    the next run still knows the message is there and the plan still reports it
    rather than quietly declaring it handled.
    """
    store = state.setdefault(tremor_delivery.STATE_KEY, {})
    tracked = store.setdefault(follow_up.TRACKED, {})
    sent = store.setdefault(tremor_delivery._SENT, {})

    gone = 0
    for action in actions:
        try:
            removed = delete_telegram_message(
                cfg.telegram_bot_token, cfg.telegram_chat_id, action["message_id"])
        except TelegramError as exc:
            log.error("message %s: %s", action["message_id"], exc)
            continue
        if not removed:
            log.warning("message %s could not be deleted - Telegram refused it, "
                        "which for a private chat means it is over 48 hours old",
                        action["message_id"])
            continue
        tracked.pop(action["event_id"], None)
        sent.pop(action["event_id"], None)
        gone += 1
        log.info("deleted message %s (%s)", action["message_id"], action["reason"])
        save_state(cfg.state_path, state)
    return gone


def main(argv: "list[str] | None" = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description="Delete pushes the events table no longer supports")
    parser.add_argument("--apply", action="store_true",
                        help="carry the plan out; without it nothing is changed")
    parser.add_argument("--drop-orphans", action="store_true",
                        help="also delete a demoted push that no note carries, "
                             "which takes that move off the channel entirely")
    parser.add_argument("--freeze", action="append", default=[], metavar="SLOT=IDS",
                        help="state what a closed note published, as a slot and a "
                             "comma-separated list of event ids. For a note that "
                             "closed before the system recorded this for itself.")
    args = parser.parse_args(argv)

    cfg = load_config()
    try:
        state = load_state(cfg.state_path)
    except CorruptState as exc:
        log.error("Refusing to run against a corrupt sent map: %s", exc)
        return 2

    events = tremor_delivery.load_events(cfg)
    if not events:
        # The same rule delivery works to: an empty table is "the pipeline did
        # not run", not "every event vanished", and only one of those is safe to
        # act on. Deleting on the other reading would clear the phone.
        log.error("The events table is empty. That is a pipeline that has not "
                  "run, not a history that has emptied - refusing to delete.")
        return 2

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    # WHAT THE PLAN IS JUDGED AGAINST, printed before the plan. Every deletion
    # below rests on this table being current; a table rebuilt from stale bars
    # would show recent pushes as events that had vanished. The workflow
    # backfills first for exactly that reason, and this is how a reader checks
    # that it worked before ticking apply.
    newest = max(int(e.get("hour_utc", 0)) for e in events)
    log.info("judging against %d events, newest hour %s UTC", len(events),
             datetime.fromtimestamp(newest, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"))

    for note in notes(events, state, now):
        state_of = ("agrees" if note["published"] in (None, note["now"])
                    else f"DISAGREES - state says {note['published']}")
        log.info("note %s: %d part(s) %s, %d row(s) in the table now, %s",
                 note["slot"], len(note["ids"]), note["ids"], note["now"], state_of)

    for spec in args.freeze:
        slot, _, ids = spec.partition("=")
        wanted = [i for i in ids.split(",") if i]
        freeze(state, int(slot), wanted)
        log.info("note %s: declared as having published %d row(s)",
                 slot, len(wanted))

    extra = surplus(cfg, events, state, now)
    actions = plan(events, state, conservative=not args.drop_orphans)
    for action in extra:
        log.info("message %s: %s", action["message_id"], action["reason"])
    for action in actions:
        log.info("message %s (event %s): %s",
                 action["message_id"], action["event_id"], action["reason"])

    if not extra and not actions:
        log.info("Nothing to delete: the channel already matches the table.")
        return 0

    if not args.apply:
        log.info("%d message(s) would be deleted. Re-run with --apply to do it.",
                 len(extra) + len(actions))
        return 0

    # Surplus parts first: a note whose extra messages are gone is coherent even
    # if a push delete is then refused, whereas the reverse leaves the note
    # claiming parts that are not there.
    gone = shed(cfg, state, extra) + apply(cfg, state, actions)
    save_state(cfg.state_path, state)
    log.info("Deleted %d of %d.", gone, len(extra) + len(actions))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
