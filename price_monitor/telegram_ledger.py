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

What this cannot cover: a run whose state is not saved forgets its ledger with
the rest of its state, as it forgets what it sent.
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
MISSES_TO_STOP = 3


class Skipped(TelegramError):
    """Not asked at all: Telegram left a request unanswered earlier in this run."""


_down = [False]
_bound: dict = {"ledger": None, "now": 0}
_news: list = []


def reset() -> None:
    """A new run: the breaker closed, nothing said yet."""
    _down[0] = False
    _news.clear()


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
    if not led["pending"] or led["read_ok"] is False:
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
    top = max(known, default=0)
    message_id = min(int(r["after"]) for r in led["pending"]) + 1
    probes = misses = 0
    while led["pending"] and probes < PROBES_PER_RUN:
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
            if record is None:
                checked.add(message_id)          # not ours: left alone
            else:
                led["pending"].remove(record)
                known.add(message_id)
                lines.append(_owner(record["kind"]).adopt(cfg, state, record, message_id))
        message_id += 1
    led["known"], led["checked"] = sorted(known), sorted(checked)
    _prune(led)
    return lines
