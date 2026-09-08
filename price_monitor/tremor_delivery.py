"""Delivers Tremor events to Telegram: the pushes, and the Tuesday/Friday digest.

The detector decides everything about WHAT to say - severity as a return period,
which channel an event belongs to, which digest slot it falls in (see
tremor.severity and tremor.routing). This module decides nothing. It reads those
decisions, renders them, and remembers what it has already sent.

Like the calendar digest it piggybacks on the existing hourly trigger rather than
taking a schedule of its own (see weekly_digest.py's module docstring). There is
one deliberate difference. The calendar digest fires only in the exact hour that
matches its window, because it is fetched live and there is nothing to send
outside it. These events are already on disk with a slot stamped on them, so the
rule here is "the slot has passed and this has not gone out yet". A missed hourly
run therefore delays the digest to the next run instead of losing it, which
matters because the trigger is an external service and the digest is the only
thing that ever carries the routine tier.

SEPARATE FROM THE SATURDAY CALENDAR DIGEST, on purpose, and not merely to keep
files apart. The two are different tenses: the calendar digest is a forecast of
what is scheduled next week, this one is a report of what actually happened.
Reading them as one message makes both harder to skim.

WHAT IS NOT SENT. Anything older than STALE_AFTER_HOURS, and this is the load-
bearing rule rather than a nicety. The events table holds the entire history -
2638 instrument events over five years - so without it, the first run after the
mute comes off would deliver five years of alerts at once. It also does the right
thing on an ordinary cold start, where there is no record of what was sent: old
news is not news, whatever the state file does or does not remember.

MUTED BY DEFAULT (Config.tremor_alerts_muted). The flag lives in config.yaml for
the same reason alerts_muted does: "we are deliberately silent" is a state of the
project and has to be visible where the code is, not on some scheduler's website
where a month later nobody can tell it from a breakage.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import pandas as pd

from price_monitor import economic_calendar
from price_monitor.config import Config
from price_monitor.notifier import TelegramError, send_telegram_message

log = logging.getLogger("price_monitor.tremor_delivery")

# Telegram rejects a message over 4096 characters outright rather than
# truncating it, so the digest is cut into parts. The headroom covers the
# "part N of M" line, which would otherwise have to be counted recursively.
_MESSAGE_LIMIT = 4000

# How old an event may be and still be worth sending. Two days: long enough that
# a scheduler outage over a weekend does not lose the Friday digest, short enough
# that nothing arrives claiming to be news when it is not.
STALE_AFTER_HOURS = 48

# What the state file remembers. Event ids rather than a high-water mark on the
# hour, because an event can legitimately change channel after the fact: a
# once-a-year move is routed to the digest while its retention is unknown and
# becomes a push six bars later, when the answer arrives. A watermark would have
# stepped over it in between and it would never have been sent at all.
STATE_KEY = "tremor_delivery"
_SENT = "sent"

TIER_EMOJI = {"routine": "⚪", "notable": "🟠", "major": "🔴", "extreme": "🚨"}

# The tier names are internal; these are what a person reads. Said as a return
# period, because "the biggest move in about three years" needs no calibration
# intuition where a 1-to-100 score would.
TIER_PERIOD = {
    "routine": "in two weeks",
    "notable": "in about two months",
    "major": "in about a year",
    "extreme": "in about three years",
}

# What was biggest, which is not the same claim for each channel. An abnormal
# event is the biggest move the rest of the market did NOT explain, and calling
# that "the biggest move" overstates it - the instrument may well have had
# larger hours that the market accounted for perfectly. The qualifier carries
# that rather than a different noun: "biggest move in two weeks (not explained
# by the rest of the market)" reads as one claim with a caveat, where "biggest
# unexplained move in a fortnight" made the reader parse an adjective first.
#
# "the rest of the market" is meant literally and is the only accurate phrase
# available: the residual is r minus what the basket factor and the block
# factor predicted for this instrument this hour (see tremor.residuals). The
# economic calendar plays no part in it - it enters only the SI-Index in
# tremor.cluster - so an alert saying the calendar failed to explain a move
# would be claiming a test the system never ran.
BASIS_NOUN = "move"
BASIS_QUALIFIER = {
    "abnormal": " (not explained by the rest of the market)",
    "absolute": "",
    "both": "",
}

# Said only where it adds something the headline does not. For an abnormal
# event the headline already carries it, and repeating it is noise.
BASIS_NOTE = {
    "absolute": "The rest of the market moved with it.",
    "both": "And the rest of the market did not explain it.",
}


def _headline(tier: str, basis: str) -> str:
    period = TIER_PERIOD.get(tier, tier)
    if basis == "market":
        return f"most disorderly hour {period}"
    return f"biggest {BASIS_NOUN} {period}{BASIS_QUALIFIER.get(basis, '')}"


def _retention_note(value: float) -> str:
    """How the move stood a day later, in words rather than a bare ratio.

    A ratio above one means the move CONTINUED, and rendering that as a
    percentage still standing produces sentences like "360% of it still
    standing", which reads as an error rather than as the strongest thing the
    system can say about an event.
    """
    if value > 1.15:
        return f"and it kept going - {value:.1f}x the original move a day later"
    if value >= 0.85:
        return "still there a day later"
    if value > 0:
        return f"{value * 100:.0f}% of it still there a day later"
    return "fully reversed within the day"


# How many companions to name before the line stops being readable. Six is the
# most the record ever produced in one window, so this is a guard rather than a
# limit anyone should meet.
MAX_NAMED_COMPANIONS = 6


def _also_moved(event: dict, labels: dict[str, str]) -> str:
    """The other instruments this push speaks for, named.

    "and six others moved" says something happened and nothing about what,
    and WHICH instruments moved together is the whole diagnosis - equities and
    credit is a different event from equities and the yen.
    """
    raw = event.get("also_moved")
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return ""
    ids = [a for a in str(raw).split(" ") if a]
    if not ids:
        return ""
    named = [labels.get(a) or a.split(":")[-1] for a in ids]
    shown, extra = named[:MAX_NAMED_COMPANIONS], len(named) - MAX_NAMED_COMPANIONS
    if len(shown) == 1:
        listed = shown[0]
    else:
        listed = ", ".join(shown[:-1]) + " and " + shown[-1]
    if extra > 0:
        listed += f" and {extra} more"
    return f"{listed} within the day"


def _escape(text: str) -> str:
    """The message goes out with parse_mode=HTML.

    Instrument labels come from config rather than an external feed, so this is
    belt and braces - but one unescaped "&" is enough for Telegram to reject a
    whole message, and the digest would then simply never arrive.
    """
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _labels() -> dict[str, str]:
    """asset_id -> human label, from the basket definition.

    Falls back to the ticker embedded in the id if the basket cannot be read: a
    message naming BTC-USD instead of Bitcoin is worse than one that is never
    sent, but only slightly, and this should not be the thing that stops an
    alert going out.
    """
    try:
        from tremor.basket import load_basket

        return {a.asset_id: a.label for a in load_basket().instruments}
    except Exception as exc:                     # pragma: no cover - defensive
        log.warning("Could not read basket labels: %s", exc)
        return {}


def _calendar(cfg: Config) -> "list[dict] | None":
    """The economic calendar archive, or None if it cannot be read.

    A push must not be lost because the calendar is missing: the context is an
    addition to the message, and an alert without it is far better than no
    alert at all.
    """
    try:
        return economic_calendar.load_events(
            economic_calendar.store_path(cfg.calendar_dir))
    except Exception as exc:                     # pragma: no cover - defensive
        log.warning("calendar could not be read, sending without context: %s", exc)
        return None


def load_events(cfg: Config) -> "list[dict]":
    """Every routed event, instrument and market, as plain dicts.

    Returns an empty list rather than raising when the parquet files are absent.
    They are produced by python -m tremor.saed and python -m tremor.market, and the
    hourly monitoring run must not fall over because a pipeline step has not been
    run yet.
    """
    paths = [cfg.tremor_events_path, cfg.tremor_market_events_path]
    if not any(os.path.exists(p) for p in paths):
        return []

    try:
        import pandas as pd
    except ImportError:                          # pragma: no cover - defensive
        log.warning("pandas is not available; Tremor delivery skipped")
        return []

    rows: list[dict] = []
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            frame = pd.read_parquet(path)
        except Exception as exc:                 # pragma: no cover - defensive
            log.warning("Could not read %s: %s", path, exc)
            continue
        if frame.empty or "channel" not in frame.columns:
            continue
        rows.extend(frame.to_dict("records"))
    return rows


def _is_market(event: dict) -> bool:
    return str(event.get("basis") or "") == "market"


def _clean(value) -> "float | None":
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number   # NaN check without numpy


# Below this the comparison is not worth a line: a move only twice its
# instrument's usual hour is not what the tier language is describing, and the
# note would be making a small number look smaller.
_SCALE_FLOOR = 3.0


def _scale_note(event: dict) -> str:
    """What the move was big COMPARED WITH, in the instrument's own units.

    "+0.13%, biggest move in about three years" reads as a bug, and 45% of
    pushes carry a number under 1%. It is not a bug - SHY's usual hour is
    0.013%, so that is ten times normal - but nothing in the message said so,
    and a reader has no way to know that a tenth of a percent in short
    Treasuries is an enormous hour while the same number in SOL is nothing.

    The yardstick is sigma_LT, the instrument's own rolling standard deviation
    over the previous five thousand bars, so it is causal like everything else
    and was already being computed. Both numbers are shown rather than just the
    ratio: seeing "0.013%" beside it is what makes the claim checkable instead
    of asking the reader to trust a multiplier.
    """
    move = _clean(event.get("r"))
    usual = _clean(event.get("sigma_lt"))
    if move is None or usual is None or usual <= 0:
        return ""
    ratio = abs(move) / usual
    if ratio < _SCALE_FLOOR:
        return ""
    return (f"that is {ratio:.0f}x its usual hour of {usual * 100:.3f}%"
            if usual * 100 < 0.1 else
            f"that is {ratio:.0f}x its usual hour of {usual * 100:.2f}%")


def describe(event: dict, labels: dict[str, str], for_push: bool = False) -> str:
    """One line for one event, as it appears in a push or a digest row.

    `for_push` drops the single retention note, because a push carries the
    fuller follow-up block instead and would otherwise say how the move held
    twice, once vaguely and once by horizon. A digest line keeps the short
    form: by the time a digest is written every horizon has elapsed, so one
    settled sentence is the whole answer rather than a promise of more.
    """
    tier = str(event.get("tier") or "routine")
    emoji = TIER_EMOJI.get(tier, "⚪")
    when = datetime.fromtimestamp(int(event["hour_utc"]), tz=timezone.utc)

    basis = str(event.get("basis") or "")
    headline = _headline(tier, basis)
    if _is_market(event):
        return (f"{emoji} <b>Market-wide</b> - {headline}"
                f"\n     hour to {when:%Y-%m-%d %H:%M} UTC")

    asset_id = str(event.get("asset_id", ""))
    label = labels.get(asset_id) or asset_id.split(":")[-1]
    move = _clean(event.get("r"))
    parts = [f"{emoji} <b>{_escape(label)}</b> - {headline}"]

    detail = [f"hour to {when:%Y-%m-%d %H:%M} UTC"]
    if move is not None:
        detail.insert(0, f"{move * 100:+.2f}%")
    parts.append("     " + ", ".join(detail))

    scale = _scale_note(event)
    if scale:
        parts.append(f"     {scale}")

    companions = _also_moved(event, labels)
    if companions:
        parts.append(f"     with {_escape(companions)}")

    if not for_push:
        held = _clean(event.get("retention_24"))
        if held is not None:
            parts.append(f"     {_retention_note(held)}")
    return "\n".join(parts)


# How far back to look for scheduled news when a push goes out. Three hours
# because that is long enough to cover a release the instrument was still
# digesting and short enough that what it names is plausibly the cause;
# measured over every push in the record, a three-hour window holds a median of
# zero high-impact events and three at the ninetieth percentile, so the line
# stays readable.
CALENDAR_LOOKBACK_HOURS = 2
# And an hour AFTER. A release five minutes after the hour closed is a cause,
# not a coincidence, and the window used to end exactly where the move did -
# which excluded precisely the releases the reader would blame first. The
# forward hour is empty in the message that goes out immediately, because that
# hour has not happened yet; it fills in at the first follow-up edit, which is
# what makes the after-window affordable at all.
CALENDAR_LOOKAHEAD_HOURS = 1

# High impact only. Medium and Low are dominated by bank holidays and minor
# prints - the same window holds a median of one Low event, and naming those
# would turn the most important line of the most important message into noise.
#
# All of them are listed rather than capped. The High filter is what keeps the
# line short, and it keeps it short enough: over every push in the record the
# window holds three at the ninetieth percentile and nine at the very worst, so
# "and two more" would hide the tail of a busy morning - which on a busy
# morning is the half worth reading - to save two lines.
CALENDAR_IMPACT = "High"


def calendar_context(hour_utc: int, calendar: "list[dict] | None") -> str:
    """What was scheduled around the move - before it and just after.

    Both answers are worth printing. Naming the release tells the reader the
    move has a known cause and they can stop looking for one. Saying that
    nothing was scheduled is the more interesting half: 55% of pushes in the
    record have no high-impact event in the previous three hours, and an
    unexplained move with no news behind it is exactly what this system exists
    to find.
    """
    # An empty archive is not evidence of a quiet three hours: it cannot tell
    # "nothing was scheduled" from "nothing was loaded", and only one of those
    # is safe to print. None and [] are both treated as "no calendar".
    if not calendar:
        return ""
    moment = datetime.fromtimestamp(int(hour_utc), tz=timezone.utc)
    lower = moment - timedelta(hours=CALENDAR_LOOKBACK_HOURS)
    upper = moment + timedelta(hours=CALENDAR_LOOKAHEAD_HOURS)
    try:
        window = economic_calendar.events_in_window(calendar, lower, upper)
    except Exception as exc:                     # pragma: no cover - defensive
        log.warning("calendar context unavailable: %s", exc)
        return ""

    named = [e for e in window if str(e.get("impact")) == CALENDAR_IMPACT]
    header = (f"Economic events, {CALENDAR_LOOKBACK_HOURS}h before to "
              f"{CALENDAR_LOOKAHEAD_HOURS}h after:")
    if not named:
        return f"{header} none scheduled."

    named.sort(key=lambda e: str(e.get("date") or ""))
    lines = [header]
    for e in named:
        country = str(e.get("country") or "").strip()
        title = str(e.get("title") or "").strip()
        lines.append(f"     - {country} {title}".rstrip())
    return "\n".join(lines)


# The check-ins a push promises, in the instrument's own bars, matching
# tremor.persistence.HORIZONS. The message says up front that these are coming,
# so silence between them reads as "not yet" rather than "forgotten".
FOLLOW_UP_HORIZONS = (2, 6, 24)


def _retention_word(value: float) -> str:
    """How the move stood, in the same words the digest uses."""
    if value > 1.15:
        return f"kept going, {value:.1f}x the original move"
    if value >= 0.85:
        return "still there"
    if value >= 0.5:
        return f"{value * 100:.0f}% of it still there"
    if value > 0:
        return f"mostly given back, {value * 100:.0f}% left"
    return "fully reversed"


def follow_up_block(event: dict, horizons=FOLLOW_UP_HORIZONS) -> str:
    """The running record of how the move held, one line per check-in.

    Every horizon is listed from the first message onward, so the reader can
    see what is still coming rather than wondering whether the bot forgot. A
    horizon whose answer has not arrived yet says so; the message is edited in
    place as each one lands (see follow_up.py).

    Reading the ABNORMAL series or the RAW one is not a detail: an event found
    because the market did not explain the move is tested on whether that
    survived, and one found because the move was simply large is tested on the
    price itself. persistence.held makes the same choice for the same reason.
    """
    raw_basis = str(event.get("basis") or "") == "absolute"
    lines = ["<i>Checking how the move held:</i>"]
    for h in horizons:
        key = f"retention_raw_{h}" if raw_basis else f"retention_{h}"
        value = _clean(event.get(key))
        if value is None:
            lines.append(f"     {h}h - not yet")
        else:
            lines.append(f"     {h}h - {_retention_word(value)}")
    return "\n".join(lines)


def format_push(event: dict, labels: dict[str, str],
                calendar: "list[dict] | None" = None) -> str:
    """A single interrupting alert."""
    lines = [describe(event, labels, for_push=True)]
    note = BASIS_NOTE.get(str(event.get("basis") or ""))
    if note:
        lines.append("")
        lines.append(_escape(note))
    context = calendar_context(int(event["hour_utc"]), calendar)
    if context:
        lines.append("")
        lines.append(_escape(context))
    lines.append("")
    lines.append(follow_up_block(event))
    return "\n".join(lines)


def format_digest(events: "list[dict]", labels: dict[str, str],
                  slot: datetime,
                  calendar: "list[dict] | None" = None) -> "list[str]":
    """The Tuesday or Friday note, split into parts Telegram will accept.

    Ordered by severity and then by time, so the part that matters is at the top
    of the first message - a digest read only as far as its notification preview
    should still deliver its most important line.
    """
    if not events:
        return []

    from tremor.severity import TIERS

    rank = {name: i for i, name in enumerate(TIERS)}
    ordered = sorted(events, key=lambda e: (-rank.get(str(e.get("tier")), 0),
                                            int(e["hour_utc"])))
    header = (f"📋 <b>Digest</b> - {slot:%A %-d %B}\n"
              f"{len(ordered)} event{'s' if len(ordered) != 1 else ''} "
              f"since the last one")

    def block(event: dict) -> str:
        line = describe(event, labels)
        context = calendar_context(int(event["hour_utc"]), calendar)
        return f"{line}\n     {_escape(context)}" if context else line

    blocks = [block(e) for e in ordered]
    messages, current = [], header
    for block in blocks:
        candidate = f"{current}\n\n{block}"
        if len(candidate) > _MESSAGE_LIMIT and current != header:
            messages.append(current)
            current = block
        else:
            current = candidate
    messages.append(current)

    if len(messages) > 1:
        total = len(messages)
        messages = [f"{m}\n\n<i>part {i} of {total}</i>"
                    for i, m in enumerate(messages, 1)]
    return messages


def _fresh(event: dict, now: datetime, key: str = "hour_utc") -> bool:
    value = event.get(key)
    if value is None or value != value:
        return False
    age = now.timestamp() - float(value)
    return 0 <= age <= STALE_AFTER_HOURS * 3600


def pending(events: "list[dict]", sent: dict, now: datetime
            ) -> "tuple[list[dict], list[dict]]":
    """Splits the events into (pushes, digest rows) that are due and unsent."""
    pushes, digest = [], []
    for event in events:
        event_id = str(event.get("event_id", ""))
        if not event_id or event_id in sent:
            continue
        channel = str(event.get("channel") or "")
        if channel == "push" and _fresh(event, now):
            pushes.append(event)
        elif channel == "digest":
            slot = event.get("digest_slot")
            if slot is None or slot != slot:
                continue
            if float(slot) <= now.timestamp() and _fresh(event, now, "digest_slot"):
                digest.append(event)
    pushes.sort(key=lambda e: int(e["hour_utc"]))
    return pushes, digest


def _prune(sent: dict, now: datetime) -> dict:
    """Forgets what is too old to matter, so the state file stays small.

    Kept a little longer than an event can be delivered, so that an id is never
    dropped while its event is still deliverable and re-sent as a result.
    """
    cutoff = now.timestamp() - 4 * STALE_AFTER_HOURS * 3600
    return {k: v for k, v in sent.items() if float(v) >= cutoff}


def maybe_deliver(cfg: Config, state: dict, now: datetime | None = None) -> int:
    """Sends whatever is due. Returns how many Telegram messages went out.

    Failures are logged and swallowed: this runs inside the hourly monitoring
    loop, and a Telegram outage must not bring the whole run down. An event is
    marked sent only once its message has actually gone, so a failure means it
    is retried on the next run rather than lost.
    """
    now = now or datetime.now(timezone.utc)
    if cfg.tremor_alerts_muted:
        return 0

    events = load_events(cfg)
    if not events:
        return 0

    store = state.setdefault(STATE_KEY, {})
    sent: dict = store.setdefault(_SENT, {})
    pushes, digest = pending(events, sent, now)

    # Corrections run on their own schedule - a push sent on Monday is edited on
    # Tuesday whether or not Tuesday has news of its own - so this happens
    # before the early return.
    from price_monitor import follow_up

    corrected = follow_up.apply(cfg, state, events, _calendar(cfg), now)

    if not pushes and not digest:
        store[_SENT] = _prune(sent, now)
        return corrected

    labels = _labels()
    # Loaded once for the whole run and only when something is actually going
    # out: the archive is ninety thousand events and reading it on an hour that
    # sends nothing would be the most expensive thing the hourly monitor does.
    calendar = _calendar(cfg) if (pushes or digest) else None
    pushed = digested = 0

    from price_monitor import follow_up
    from price_monitor.alerts_log import load_alerts_log, record_sent_alert, save_alerts_log

    # The record "explain alerts" reads: it looks a push up by the message id
    # printed in its footer and edits that message in place. Loaded only when a
    # push is actually going out.
    alerts_log = load_alerts_log(cfg.alerts_log_path) if pushes else []

    for event in pushes:
        try:
            message_id = send_telegram_message(
                cfg.telegram_bot_token, cfg.telegram_chat_id,
                format_push(event, labels, calendar))
        except TelegramError as exc:
            log.error("Failed to send Tremor push %s: %s", event.get("event_id"), exc)
            continue
        sent[str(event["event_id"])] = int(event["hour_utc"])
        # Remembered so the two, six and twenty-four bar check-ins can edit this
        # very message rather than sending three more.
        follow_up.track(store, event, message_id)
        label = labels.get(str(event.get("asset_id", ""))) or str(
            event.get("asset_id", "")).split(":")[-1]
        move = _clean(event.get("r"))
        record_sent_alert(
            alerts_log, chat_id=cfg.telegram_chat_id, message_id=message_id,
            symbol=label, message_text=format_push(event, labels, calendar),
            last_close=float(_clean(event.get("close")) or 0.0),
            last_return_pct=float((move or 0.0) * 100),
            ewma_z=float(_clean(event.get("z_resid")) or 0.0),
            robust_z=float(_clean(event.get("z_resid")) or 0.0),
            volume_z=0.0, signal_type=str(event.get("tier") or "push"),
            now=datetime.fromtimestamp(int(event["hour_utc"]), tz=timezone.utc))
        pushed += 1

    if digest:
        slot = datetime.fromtimestamp(max(int(e["digest_slot"]) for e in digest),
                                      tz=timezone.utc)
        messages = format_digest(digest, labels, slot, calendar)
        try:
            for text in messages:
                send_telegram_message(cfg.telegram_bot_token, cfg.telegram_chat_id, text)
        except TelegramError as exc:
            log.error("Failed to send Tremor digest: %s", exc)
        else:
            # Marked sent only as a whole. A digest that went out in three parts
            # of which the third failed is retried entire on the next run: two
            # duplicated parts are a smaller harm than a silently missing one,
            # and the alternative - marking each event as its part lands - would
            # split one note across two days.
            for event in digest:
                sent[str(event["event_id"])] = int(event["hour_utc"])
            digested = len(messages)
            log.info("Tremor digest sent (%d events, %d message(s))",
                     len(digest), digested)

    if pushed:
        save_alerts_log(cfg.alerts_log_path, alerts_log)
    if pushes:
        log.info("Tremor pushes sent: %d of %d due", pushed, len(pushes))
    store[_SENT] = _prune(sent, now)
    return pushed + digested + corrected
