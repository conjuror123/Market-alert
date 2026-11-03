"""What became of a message Telegram did not answer for.

THE PROBLEM (2026-11-03). Telegram held the 14:05 run's two sends past the
timeout; the run recorded them unsent, and Telegram published both at 14:40 as
ids 137 and 138. The 15:05 run sent the push again (139): a duplicate on the
channel, and two messages the bot could neither correct nor delete, since it
never learned their ids. Telegram promises nothing about how late such a
message may appear, and the Bot API has no idempotency key and no way to ask
for a message by its id.

SO A SEND WITHOUT AN ANSWER IS "UNSURE", NOT "UNSENT" (notifier.TelegramUnsure),
and is written down here (`pending`): what it said and when, and the highest
channel id the bot knew of then - a late message takes a higher one, ids in a
channel only grow. Every run after that READS the channel's ids it does not
know: the only read the Bot API offers is forwarding a message, whose answer
carries its text, so each is forwarded silently to the health chat and the
copy deleted at once. A text that is an unsure send's goes to its owner
(jump_delivery.adopt, weekly_digest.adopt), which takes it in when nothing has
replaced it yet - no repeat, and its later edits bring it up to date - or
deletes it when something has. A text that is not ours is left alone: the bot
can delete any message of the channel, so nothing is deleted unread.

THE BREAKER. The first unsure answer of a run stops every further channel
request of that run (call raises Skipped, which means "not sent"): an outage
then costs at most one late message an hour, and that one is on the record.
Health messages go to another chat and do not pass through here.

AN UNSURE EDIT may land after a newer one and put older text back, so the
message is edited with its current text on every run for a day (`recheck`);
Telegram answers "not modified" when nothing needs changing.

THE READ IS CHECKED ONCE, on the first run after this was deployed: one known
message is forwarded and its copy deleted. If the channel protects its content
nothing can be forwarded, and every unsure send is then named on the health
chat instead, for a hand to check (`read_ok`).

A RUN WHOSE STATE WAS NOT SAVED (its commit failed) leaves messages the next
run's state knows nothing of, and no ledger entry for them: the next run would
send them all again. So every run also reads the ids just above the highest it
knows - normally two silent "not found" answers. A message there that looks
like the bot's own (a move's coloured square, the digest, the calendar, a
note part) and was posted after the last run that saved its state is a STRAY:
a message this run is about to send with the same first line takes it over
instead (claim), and those left unclaimed once delivery has run are deleted
(settle_strays). Nothing counts as a stray before the first such run has
recorded its time (`saved_at`), so a state started afresh deletes nothing.
"""
from __future__ import annotations

import hashlib
import html
import logging
import re
from datetime import datetime, timezone
from typing import Callable

from price_monitor import notifier
from price_monitor.notifier import TelegramError, TelegramUnsure

log = logging.getLogger("price_monitor.telegram_ledger")

STATE_KEY = "telegram_ledger"
# An unsure send is looked for this long; Telegram has no stated bound.
KEEP_DAYS = 7
# How long an unsure edit is repeated.
RECHECK_HOURS = 24
# The reads one run may spend, and the unused ids past the newest that end them.
PROBES_PER_RUN = 30
MISSES_TO_STOP = 2


class Skipped(TelegramError):
    """Not asked at all: Telegram left a request unanswered earlier in this run."""


_down = [False]
_bound: dict = {"ledger": None, "now": 0}
_news: list = []
_strays: list = []                               # this run's, until claimed or deleted
_delivered = [False]


def reset() -> None:
    """A new run: the breaker closed, nothing said yet, no strays."""
    _down[0] = False
    _news.clear()
    _strays.clear()
    _delivered[0] = False


def down() -> bool:
    return _down[0]


def clock() -> int:
    """The run's time, as its ledger was bound (use)."""
    return int(_bound["now"])


def news() -> "list[str]":
    """The run's lines for the health chat, once."""
    out = list(_news)
    _news.clear()
    return out


def _seed(state: dict) -> "set[int]":
    """The channel ids the delivery already tracks, for a ledger that starts empty."""
    week = (state.get("jump_delivery") or {}).get("week") or {}
    ids = {int(i) for i in (week.get("messages") or {})}
    ids |= {int(i) for i in ((week.get("note") or {}).get("ids") or [])}
    return ids


def use(state: dict, now_ts: int) -> dict:
    """The ledger inside `state`, bound to this run's channel requests."""
    led = state.setdefault(STATE_KEY, {})
    for key, empty in (("known", []), ("checked", []), ("pending", []), ("recheck", {})):
        led.setdefault(key, empty)
    led.setdefault("read_ok", None)
    if not led["known"]:
        led["known"] = sorted(_seed(state))
    _bound.update(ledger=led, now=int(now_ts))
    return led


def call(request: Callable):
    """One channel request, unless Telegram left one unanswered earlier in this
    run; an unsure answer trips the breaker for the rest of it."""
    if _down[0]:
        raise Skipped("Telegram left a request unanswered earlier in this run: not asked")
    try:
        return request()
    except TelegramUnsure:
        _down[0] = True
        raise


def saw(message_id) -> None:
    """A message the bot put on the channel: never read back as a stranger."""
    led = _bound["ledger"]
    if led is not None and isinstance(message_id, int) and message_id not in led["known"]:
        led["known"].append(message_id)


_TAG = re.compile(r"<[^>]+>")
# What the bot's own messages begin with, or a note part ends with.
_OWN_START = ("⬜", "🟨", "🟧", "🟥", "📋 Digest", "📅 Economic calendar")
_NOTE_PART = re.compile(r"part \d+ of \d+ - \d\d\.\d\d\.\d{4} to \d\d\.\d\d\.\d{4}\s*$")


def plain(text: str) -> str:
    """What the reader sees of the HTML the bot sends."""
    return html.unescape(_TAG.sub("", text or ""))


def _digest(seen: str) -> str:
    return hashlib.sha256(" ".join(seen.split()).encode("utf-8")).hexdigest()[:16]


def sent_hash(text: str) -> str:
    """The HTML the bot sent, as Telegram will give it back."""
    return _digest(plain(text))


def read_hash(text: str) -> str:
    """A text Telegram gave back."""
    return _digest(text or "")


def _when(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%d.%m %H:%M UTC")


def note_unsure(kind: str, text: str, **meta) -> "dict | None":
    """Writes down a send Telegram did not answer for: `kind` names its owner
    (alert and note are jump_delivery's, calendar weekly_digest's)."""
    led = _bound["ledger"]
    if led is None:                              # pragma: no cover - defensive
        log.warning("An unsure send with no ledger bound: it cannot be looked for")
        return None
    first = plain(text).split("\n", 1)[0].strip()
    record = {"kind": kind, "hash": sent_hash(text), "first": first[:80],
              "at": _bound["now"], "after": max(led["known"], default=0), **meta}
    led["pending"].append(record)
    _news.append(f"Telegram did not answer for \"{first[:60]}\": it may still appear; "
                 "the next runs look for it")
    log.warning("Telegram did not answer for %r: written down to be looked for", first[:60])
    return record


def _first_line(seen: str) -> str:
    return " ".join((seen or "").strip().split("\n", 1)[0].split())


def _looks_own(seen: str) -> bool:
    seen = (seen or "").strip()
    return seen.startswith(_OWN_START) or bool(_NOTE_PART.search(seen))


def claim(text: str) -> "int | None":
    """A stray (see above) this run was about to send again: its id, to be taken
    over and edited instead of sending `text`."""
    first = _first_line(plain(text))
    for stray in _strays:
        if stray["first"] == first:
            _strays.remove(stray)
            saw(stray["id"])
            log.info("Message %s, left by a run whose state was not saved, taken over",
                     stray["id"])
            return int(stray["id"])
    return None


def delivering() -> None:
    """Delivery has run: what it did not claim is not wanted."""
    _delivered[0] = True


def settle_strays(cfg) -> "list[str]":
    """Deletes the strays no message of this run claimed - once delivery has run
    and so has claimed all it would - and records this run as the last that
    can be trusted to have saved. Returns lines for the health chat."""
    led = _bound["ledger"]
    if led is None or not _delivered[0]:
        return []
    lines = []
    for stray in list(_strays):
        try:
            gone = call(lambda: notifier.delete_telegram_message(
                cfg.telegram_bot_token, cfg.telegram_chat_id, int(stray["id"])))
        except TelegramError as exc:
            log.warning("Could not delete stray message %s: %s", stray["id"], exc)
            continue                             # read again by the next run
        _strays.remove(stray)
        led["checked"].append(int(stray["id"]))
        if gone:
            lines.append(f"Deleted {stray['id']} (\"{stray['first'][:60]}\"), left by a run "
                         "whose state was not saved")
    if not _strays:
        led["saved_at"] = _bound["now"]
    return lines


def pending() -> "list[dict]":
    led = _bound["ledger"]
    return led["pending"] if led is not None else []


def recheck(message_id) -> None:
    """An edit Telegram did not answer for: repeated for a day."""
    led = _bound["ledger"]
    if led is not None:
        led["recheck"][str(int(message_id))] = _bound["now"] + RECHECK_HOURS * 3600


def rechecking(message_id) -> bool:
    led = _bound["ledger"]
    return bool(led) and int(led["recheck"].get(str(int(message_id)), 0)) > _bound["now"]


def settled(message_id) -> None:
    """No edit to repeat: it landed, or the message is gone."""
    led = _bound["ledger"]
    if led is not None:
        led["recheck"].pop(str(int(message_id)), None)


# --- reading the channel --------------------------------------------------------

def _forward(cfg, message_id: int) -> dict:
    return call(lambda: notifier.forward_telegram_message(
        cfg.telegram_bot_token, cfg.telegram_chat_id, int(message_id),
        cfg.telegram_health_chat_id))


def _drop_copy(cfg, got: dict) -> None:
    """The forward's copy in the health chat, deleted once read."""
    if got.get("copy") is None:
        return
    try:
        call(lambda: notifier.delete_telegram_message(
            cfg.telegram_bot_token, cfg.telegram_health_chat_id, int(got["copy"]),
            timeout=notifier.SHORT_TIMEOUT))
    except TelegramError as exc:
        log.warning("Could not delete the read copy %s: %s", got["copy"], exc)


def _self_check(cfg, state: dict, led: dict) -> None:
    """Once: can the channel be read? Forwards the newest message the bot knows
    that still exists - the week's note lives all week. Undecided (Telegram
    silent, or nothing to read) is tried again by the next run."""
    for message_id in sorted(set(led["known"]) | _seed(state), reverse=True)[:5]:
        try:
            got = _forward(cfg, message_id)
        except TelegramError as exc:
            log.warning("Channel read check not done: %s", exc)
            return
        _drop_copy(cfg, got)
        if got["state"] == "found":
            led["read_ok"] = True
            log.info("Channel read path checked: message %s read and its copy deleted",
                     message_id)
            return
        if got["state"] == "unforwardable":
            led["read_ok"] = False
            _news.append("The channel's messages cannot be forwarded (content protection?): "
                         "a message Telegram does not answer for will be named here, for a "
                         "hand to check, instead of being found by the bot")
            return


def _owner(kind: str):
    if kind == "calendar":
        from price_monitor import weekly_digest
        return weekly_digest
    from price_monitor import jump_delivery
    return jump_delivery


def _expire(led: dict, now_ts: int) -> None:
    keep = []
    for record in led["pending"]:
        if now_ts - int(record["at"]) > KEEP_DAYS * 86400:
            log.info("Not found in %d days, no longer looked for: %r", KEEP_DAYS,
                     record.get("first"))
        else:
            keep.append(record)
    led["pending"] = keep
    led["recheck"] = {k: v for k, v in led["recheck"].items() if int(v) > now_ts}


def _prune(led: dict) -> None:
    """Ids below every pending send's floor are never read again."""
    top = max(led["known"], default=0)
    floor = min((int(r["after"]) for r in led["pending"]), default=top)
    led["known"] = sorted({i for i in led["known"] if i >= floor} | ({top} if top else set()))
    led["checked"] = sorted(i for i in set(led["checked"]) if i > floor)


def reconcile(cfg, state: dict, now: "datetime | None" = None) -> "list[str]":
    """Looks for the unsure sends on the channel and hands each found to its
    owner. Returns lines for the health chat. Costs nothing when the ledger is
    empty and the read has been checked."""
    now = now or datetime.now(timezone.utc)
    now_ts = int(now.timestamp())
    led = use(state, now_ts)
    _expire(led, now_ts)
    lines: list = []
    if not cfg.telegram_health_chat_id or not cfg.telegram_chat_id:
        if led["pending"]:
            log.warning("No health chat to read the channel through: %d unsure send(s) "
                        "are not looked for", len(led["pending"]))
        _prune(led)
        return lines
    if led["read_ok"] is None:
        _self_check(cfg, state, led)
    # Not checked yet (nothing known to read back) is no reason to wait: the
    # first message read is the check.
    if led["read_ok"] is False or not (led["known"] or led["pending"]):
        if led["read_ok"] is False:
            for record in led["pending"]:
                if not record.get("told"):
                    record["told"] = True
                    lines.append(f"Telegram did not answer for \"{record['first'][:60]}\" "
                                 f"({_when(record['at'])}): if it appears after message "
                                 f"{record['after']}, it may be a duplicate to delete by hand")
        _prune(led)
        return lines

    known, checked = set(led["known"]), set(led["checked"])
    top = max(known | checked, default=0)
    # From the oldest unsure send's floor; with none, just past the newest id
    # known - where a run whose state was lost would have left its messages.
    message_id = (min(int(r["after"]) for r in led["pending"]) if led["pending"] else top) + 1
    saved_at = int(led.get("saved_at") or 0)
    probes = misses = 0
    while probes < PROBES_PER_RUN:
        if message_id in known or message_id in checked:
            message_id += 1
            continue
        probes += 1
        try:
            got = _forward(cfg, message_id)
        except TelegramError as exc:
            log.warning("Stopped reading the channel at %s: %s", message_id, exc)
            break
        _drop_copy(cfg, got)
        if got["state"] == "missing":
            if message_id <= top:
                checked.add(message_id)          # deleted: ids are never reused
            else:
                misses += 1                      # not used yet
                if misses >= MISSES_TO_STOP:
                    break
        elif got["state"] == "unforwardable":
            checked.add(message_id)              # a service message, a pin
            top, misses = max(top, message_id), 0
        else:
            top, misses = max(top, message_id), 0
            led["read_ok"] = True
            digest = read_hash(got.get("text", ""))
            record = next((r for r in led["pending"] if r["hash"] == digest), None)
            if record is None and saved_at and int(got.get("date") or 0) > saved_at \
                    and _looks_own(got.get("text", "")):
                # The bot's, posted after the last saved run: a stray, for this
                # run to claim or, unclaimed, delete. Not marked: an unsettled
                # stray is read again.
                _strays.append({"id": message_id, "first": _first_line(got.get("text", ""))})
            elif record is None:
                checked.add(message_id)          # not ours: left alone
            else:
                led["pending"].remove(record)
                known.add(message_id)
                lines.append(_owner(record["kind"]).adopt(cfg, state, record, message_id))
        message_id += 1
    led["known"], led["checked"] = sorted(known), sorted(checked)
    _prune(led)
    return lines
