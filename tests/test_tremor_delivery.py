from datetime import datetime, timedelta, timezone

import pytest

from price_monitor import follow_up as follow_up_module
from price_monitor import tremor_delivery as md
from price_monitor.config import Config
from price_monitor.notifier import TelegramError

HOUR = 3600
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)   # a Friday
LABELS = {"twelvedata:GLD": "Gold", "coinbase:BTC-USD": "Bitcoin"}

# The note that is open at NOW. A digest row names the note it joins, and that
# note was opened at the START of the period covering it.
from tremor import routing                                       # noqa: E402

SLOT = routing.digest_slot(int(NOW.timestamp()))


def notes(sender):
    """The digest notes among what was sent, later parts of a long one included.

    A later part opens with an event line like a push does, so it is the header
    or the part marker that tells them apart.
    """
    return [t for t in sender.texts if "<b>Digest</b>" in t or "<i>part " in t]


def alerts(sender):
    """The pushes: everything that is not part of a note."""
    written = notes(sender)
    return [t for t in sender.texts if t not in written]


# Where a push's record goes. Without this the suite writes its fixtures into
# the repository's real alerts log, which is how ninety-six imaginary Gold
# alerts came to be committed.
_LOG_PATH = ""


@pytest.fixture(autouse=True)
def alerts_log_path(tmp_path):
    global _LOG_PATH
    _LOG_PATH = str(tmp_path / "alerts_log.json")
    yield
    _LOG_PATH = ""


def cfg(**over):
    base = dict(telegram_bot_token="t", telegram_chat_id="c",
                tremor_alerts_muted=False, alerts_log_path=_LOG_PATH)
    return Config(**(base | over))


def event(**over):
    base = dict(event_id="e1", asset_id="twelvedata:GLD", block="commodities",
                hour_utc=int(NOW.timestamp()) - HOUR,
                tier="major", basis="abnormal", channel="push", r=0.021,
                e_resid=0.019, co_basket=0.002, co_block=0.0,
                retention_settled=0.9, digest_slot=None)
    return base | over


class Sent:
    """Stands in for notifier.send_telegram_message."""

    def __init__(self, fail=False):
        self.texts, self.fail = [], fail

    def __call__(self, token, chat, text, *a, **k):
        if self.fail:
            raise TelegramError("nope")
        self.texts.append(text)
        return len(self.texts)


class Edited:
    """Stands in for notifier.edit_telegram_message."""

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, token, chat, message_id, text, *a, **k):
        if self.fail:
            raise TelegramError("nope")
        self.calls.append((message_id, text))


@pytest.fixture
def sender(monkeypatch):
    s = Sent()
    monkeypatch.setattr(md, "send_telegram_message", s)
    monkeypatch.setattr(md, "_labels", lambda: LABELS)
    return s


@pytest.fixture
def editor(monkeypatch):
    """Both editors: the notes are edited from here, the pushes from follow_up."""
    e = Edited()
    monkeypatch.setattr(md, "edit_telegram_message", e)
    monkeypatch.setattr(follow_up_module, "edit_telegram_message", e)
    return e


def deliver(monkeypatch, events, state=None, now=NOW, **over):
    monkeypatch.setattr(md, "load_events", lambda c: events)
    state = {} if state is None else state
    return md.maybe_deliver(cfg(**over), state, now), state


def test_nothing_goes_out_while_muted(monkeypatch, sender):
    sent, _ = deliver(monkeypatch, [event()], tremor_alerts_muted=True)
    assert sent == 0 and sender.texts == []


def test_a_push_goes_out_once(monkeypatch, sender):
    _, state = deliver(monkeypatch, [event()])
    assert len(alerts(sender)) == 1 and "Gold" in alerts(sender)[0]

    # the same run again sends nothing: the id is remembered, and the note that
    # was opened alongside it has not changed
    again, _ = deliver(monkeypatch, [event()], state=state)
    assert again == 0 and len(alerts(sender)) == 1


def test_stale_events_are_not_delivered(monkeypatch, sender):
    # The events table holds the whole history, so without this the first run
    # after the mute comes off would deliver five years of alerts at once.
    old = event(hour_utc=int(NOW.timestamp()) - (md.STALE_AFTER_HOURS + 1) * HOUR)
    deliver(monkeypatch, [old])
    assert alerts(sender) == []


def test_an_event_from_the_future_is_not_delivered(monkeypatch, sender):
    ahead = event(hour_utc=int(NOW.timestamp()) + HOUR)
    deliver(monkeypatch, [ahead])
    assert alerts(sender) == []


def test_the_note_is_opened_even_before_it_has_anything_in_it(monkeypatch, sender):
    # It is opened at the START of the period it covers, so the reader has one
    # message to watch and every event after that arrives as a silent edit.
    elsewhere = event(event_id="old", channel="digest", tier="routine",
                      hour_utc=SLOT - 30 * 24 * HOUR)
    deliver(monkeypatch, [elsewhere])
    assert len(notes(sender)) == 1
    assert "Nothing so far" in notes(sender)[0]
    assert "updated as moves are found" in notes(sender)[0]


def test_a_move_joins_the_note_that_is_already_open(monkeypatch, sender):
    row = event(event_id="d1", channel="digest", tier="routine")
    deliver(monkeypatch, [row])
    assert "Gold" in notes(sender)[0]


def test_a_move_from_before_the_note_opened_is_not_in_it(monkeypatch, sender):
    stale = event(event_id="d1", channel="digest", tier="routine",
                  hour_utc=SLOT - 4 * 24 * HOUR)
    deliver(monkeypatch, [stale])
    assert len(notes(sender)) == 1 and "Gold" not in notes(sender)[0]


def test_the_note_is_one_message_for_many_events(monkeypatch, sender):
    rows = [event(event_id=f"d{i}", channel="digest", tier="routine",
                  hour_utc=SLOT + i * HOUR) for i in range(3)]
    deliver(monkeypatch, rows)
    assert len(notes(sender)) == 1
    assert notes(sender)[0].count(md.TIER_EMOJI["routine"]) == 3


def test_a_failed_send_is_retried_rather_than_lost(monkeypatch):
    # An event is marked sent only once its message has actually gone.
    failing = Sent(fail=True)
    monkeypatch.setattr(md, "send_telegram_message", failing)
    monkeypatch.setattr(md, "_labels", lambda: LABELS)
    sent, state = deliver(monkeypatch, [event()])
    assert sent == 0
    assert not state[md.STATE_KEY][md._SENT]
    # The note counts as opened only once a message is behind it.
    assert not state[md.STATE_KEY][md.DIGEST_STATE][str(SLOT)]["ids"]

    working = Sent()
    monkeypatch.setattr(md, "send_telegram_message", working)
    deliver(monkeypatch, [event()], state=state)
    assert len(alerts(working)) == 1


def test_an_event_promoted_to_a_push_later_is_still_sent(monkeypatch, sender):
    # A once-a-year move is routed to the digest while its retention is unknown
    # and becomes a push six bars later, when the answer arrives. A high-water
    # mark on the hour would have stepped over it in between.
    slot = int(NOW.timestamp()) + 3 * HOUR          # its digest has not run yet
    _, state = deliver(monkeypatch, [event(channel="digest", digest_slot=slot,
                                           retention_settled=None)])
    assert not state[md.STATE_KEY][md._SENT]

    deliver(monkeypatch, [event(channel="push")], state=state)
    assert len(alerts(sender)) == 1


def test_the_state_does_not_grow_without_bound(monkeypatch, sender):
    ancient = {"old": int(NOW.timestamp()) - 400 * 24 * HOUR}
    state = {md.STATE_KEY: {md._SENT: dict(ancient)}}
    _, state = deliver(monkeypatch, [event()], state=state)
    assert "old" not in state[md.STATE_KEY][md._SENT]
    assert "e1" in state[md.STATE_KEY][md._SENT]


def test_a_market_event_reads_as_market_wide(monkeypatch, sender):
    row = event(event_id="m1", basis="market", asset_id=None, r=None,
                retention_settled=None, tier="notable")
    deliver(monkeypatch, [row])
    assert "Market-wide" in alerts(sender)[0]
    assert "this disorderly" in alerts(sender)[0]


def test_the_severity_leads_the_digest(monkeypatch, sender):
    # A digest read only as far as its notification preview should still
    # deliver its most important line.
    rows = [
        event(event_id="a", channel="digest", tier="routine", digest_slot=SLOT),
        event(event_id="b", channel="digest", tier="major",
              asset_id="coinbase:BTC-USD", digest_slot=SLOT),
    ]
    deliver(monkeypatch, rows)
    text = notes(sender)[0]
    assert text.index("Bitcoin") < text.index("Gold")


def test_a_long_digest_is_split_within_telegrams_limit(monkeypatch, sender):
    rows = [event(event_id=f"d{i}", channel="digest", tier="routine",
                  hour_utc=int(NOW.timestamp()) - HOUR,
                  digest_slot=SLOT) for i in range(200)]
    deliver(monkeypatch, rows)
    assert len(notes(sender)) > 1
    assert all(len(t) <= 4096 for t in sender.texts)
    assert "part 1 of" in notes(sender)[0]


def test_labels_fall_back_to_the_ticker(monkeypatch, sender):
    monkeypatch.setattr(md, "_labels", lambda: {})
    deliver(monkeypatch, [event()])
    assert "GLD" in sender.texts[0]


def test_missing_parquet_files_are_not_an_error(tmp_path):
    quiet = cfg(tremor_events_path=str(tmp_path / "nope.parquet"),
                tremor_market_events_path=str(tmp_path / "also-nope.parquet"))
    assert md.load_events(quiet) == []
    assert md.maybe_deliver(quiet, {}) == 0


def test_a_move_that_kept_going_does_not_read_as_a_percentage_still_standing():
    # A ratio above one means the move CONTINUED. Rendered as a percentage it
    # produced "360% of it still standing", which reads as an error rather than
    # as the strongest thing the system can say about an event.
    assert "3.6x" in md._retention_note(3.6)
    assert "kept going" in md._retention_note(1.4)
    assert "%" not in md._retention_note(3.6)


def test_the_retention_wording_covers_the_whole_range():
    assert md._retention_note(0.95) == "still there at the next day's close"
    assert "60%" in md._retention_note(0.6)
    assert "reversed" in md._retention_note(-0.2)
    assert "reversed" in md._retention_note(0.0)


def test_a_move_on_the_abnormal_ladder_says_which_ladder_it_is_on():
    # Two ladders exist: one ranks the raw return, the other what is left after
    # the market is taken out. "Biggest move in about a year" would be false for
    # the second - the instrument may well have had larger hours the market
    # accounted for perfectly - and "of its own" says so without a glossary.
    assert md._headline("major", "abnormal") == (
        "a move of its own this big happens about once a year")
    assert md._headline("major", "absolute") == (
        "a move this big happens about once a year")
    assert md._headline("major", "both") == (
        "a move this big happens about once a year")
    assert md._headline("notable", "market") == (
        "an hour this disorderly happens about once every two months")


def test_the_period_is_a_frequency_and_not_a_record():
    # The ladder says a move this size is expected about once a fortnight, not
    # that the last fortnight held nothing larger. The old wording flatly
    # contradicted the line beneath it: "biggest move in about three years" over
    # "the last one this big was 23 days ago".
    assert md._headline("routine", "absolute") == (
        "a move this big happens about once a fortnight")
    assert "biggest" not in md._headline("extreme", "absolute")


def test_no_alert_claims_the_economic_calendar_explained_anything():
    # The residual is r minus what the basket and block factors predicted; the
    # calendar enters only the SI-Index, never this basis. An alert naming it
    # would be reporting a test the system never ran.
    for tier in ("routine", "notable", "major", "extreme"):
        for basis in ("abnormal", "absolute", "both", "market"):
            assert "calendar" not in md._headline(tier, basis).lower()
    for line in md._split_lines({"r": 0.02, "e_resid": 0.018}, "Gold"):
        assert "calendar" not in line.lower()


def test_the_split_is_three_parts_and_never_the_word_market():
    # For the S&P 500, "the market" IS the S&P 500 - so naming the idea invited
    # "which market, and how would I have followed it?". The parts are named by
    # what they actually are instead.
    lines = md._split_lines({"r": 0.0700, "e_resid": 0.0100, "co_basket": 0.0210,
                             "co_block": 0.0390, "block": "equity"}, "S&P 500")
    assert lines[0] == "of that move:"
    assert "+2.10%  the whole basket drifting together" in lines[1]
    assert "+3.90%  its own block, US equities" in lines[2]
    assert "+1.00%  S&amp;P 500 on its own" in lines[3]
    for line in lines:
        assert "market" not in line


def test_the_block_is_split_out_because_two_parts_was_sometimes_wrong():
    # On 2008-11-20 the financial sector's basket beta was NEGATIVE and its
    # +10.50% came almost entirely from the equity block. Calling that "the
    # whole watchlist drifting" would have named the wrong cause.
    lines = md._split_lines({"r": 0.1050, "e_resid": 0.0088, "co_basket": -0.0021,
                             "co_block": 0.0983, "block": "equity"},
                            "US financial sector")
    assert "-0.21%  the whole basket" in lines[1]
    assert "+9.83%  its own block, US equities" in lines[2]


def test_a_block_that_contributed_nothing_takes_no_line():
    lines = md._split_lines({"r": 0.02, "e_resid": 0.018, "co_basket": 0.002,
                             "co_block": 0.0, "block": "FX"}, "Euro / dollar")
    assert not any("its own block" in line for line in lines)


def test_the_split_falls_back_to_two_parts_on_an_older_row():
    # Before the two components were carried, only the total was.
    lines = md._split_lines({"r": 0.02, "e_resid": 0.018}, "Gold")
    assert "+0.20%  the whole basket drifting together" in lines[1]
    assert "+1.80%  Gold on its own" in lines[2]


def test_no_split_is_claimed_when_the_regression_has_not_been_fitted():
    # Beta is undefined through an instrument's first five hundred bars.
    assert md._split_lines({"r": 0.02, "e_resid": None}, "Gold") == []
    assert md._split_lines({}, "Gold") == []


def test_the_headline_does_not_claim_the_market_was_quiet():
    # A large residual means the co-movement does not ACCOUNT for the size of
    # the move. It does not mean the rest of the market was calm - on a macro
    # hour everything moves and this one moved further still, which is the case
    # the residual channel exists to catch.
    line = md._headline("major", "abnormal")
    for overclaim in ("usual", "quiet", "normal", "calm", "did not move"):
        assert overclaim not in line


def _blocks(anchor_ids, companions=None, labels=None, budget=9999):
    return md._companion_blocks({"also_moved": anchor_ids}, labels or {},
                                companions, NOW, [], budget)


def test_every_instrument_in_the_episode_is_written_out_in_full():
    # Not a list of names, and not a list of names with a number beside them. A
    # push speaks for everything that moved with it until midnight, and each has
    # its own size, rarity, split and two check-ins.
    labels = {"a:XLF": "US financial sector"}
    with_it = {"a:XLF": event(asset_id="a:XLF", tier="extreme", basis="absolute",
                              r=0.105, e_resid=0.0088, co_basket=-0.0021,
                              co_block=0.0983, block="equity", sigma_lt=0.0081,
                              retention_raw_today=1.7, retention_raw_settled=1.9)}
    block = _blocks("a:XLF", with_it, labels)
    assert "US financial sector" in block
    assert "+10.50%" in block
    assert "its own block, US equities" in block
    assert "this day's close - kept going, 1.7x the original move" in block
    assert "next day's close - kept going, 1.9x the original move" in block


def test_the_rarest_and_biggest_companion_comes_first():
    # A folded companion moved MORE than the push that spoke for it 49% of the
    # time: the most important number is often down here, not in the headline.
    labels = {"a:1": "Small", "a:2": "Large"}
    with_it = {"a:1": event(asset_id="a:1", tier="major", r=0.01),
               "a:2": event(asset_id="a:2", tier="extreme", r=0.09)}
    block = _blocks("a:1 a:2", with_it, labels)
    assert block.index("Large") < block.index("Small")


def test_a_companion_not_yet_in_the_table_is_only_named():
    # The ordinary case at the hour a push is sent: the day it collects has not
    # happened yet, so there is nothing to write out.
    block = _blocks("twelvedata:EUR/USD")
    assert block == "Also moved, within the day: EUR/USD"


def test_no_companions_produces_no_block():
    for value in ("", None, float("nan")):
        assert _blocks(value) == ""
    assert md._companion_blocks({}, {}, None, NOW, [], 9999) == ""


def test_the_message_is_trimmed_rather_than_split_into_a_second_buzz():
    # A push split into two messages would buzz twice, which is the one thing
    # the collapse exists to prevent.
    with_it = {f"a:{i}": event(asset_id=f"a:{i}", tier="major", r=0.01)
               for i in range(6)}
    block = _blocks(" ".join(with_it), with_it, budget=600)
    assert "more not shown" in block
    assert len(block) < 1200


def _cal(rows):
    """rows: (iso date, country, title, impact)."""
    return [{"date": d, "country": c, "title": t, "impact": i,
             "actual": "", "forecast": "", "previous": ""} for d, c, t, i in rows]


def test_a_push_names_the_scheduled_news_behind_it():
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([("2026-06-10T12:30:00+00:00", "USD", "Core CPI m/m", "High"),
                ("2026-06-10T13:00:00+00:00", "USD", "Fed Chair Speaks", "High")])
    out = md.calendar_context(hour, cal)
    assert out.startswith("Economic events, 2h before to 1h after:")
    assert "USD Core CPI m/m" in out and "USD Fed Chair Speaks" in out


def test_a_push_with_no_news_behind_it_says_so():
    # The more interesting half: 55% of pushes in the record have no
    # high-impact event in the previous three hours, and an unexplained move
    # with nothing scheduled is what the system exists to find.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    elsewhere = _cal([("2026-05-01T12:00:00+00:00", "USD", "Old CPI", "High")])
    assert md.calendar_context(hour, elsewhere) == \
        "Economic events, 2h before to 1h after: none scheduled."


def test_an_empty_archive_claims_nothing_rather_than_claiming_silence():
    # "none scheduled" is a claim about the world and needs an archive behind
    # it. An empty one cannot tell "nothing happened" from "nothing was
    # loaded", so it says neither.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    assert md.calendar_context(hour, []) == ""


def test_low_impact_news_is_not_named():
    # High and Medium are shown, the same two the Saturday calendar shows. Low
    # is dominated by bank holidays and minor prints, and naming those would
    # turn the most important line of the most important message into noise.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    only_low = _cal([("2026-06-10T12:30:00+00:00", "CHF", "Bank Holiday", "Low")])
    assert md.calendar_context(hour, only_low).endswith("none scheduled.")

    medium = _cal([("2026-06-10T12:45:00+00:00", "EUR", "Trade Balance", "Medium")])
    assert "Trade Balance" in md.calendar_context(hour, medium)


def test_each_named_release_carries_its_impact_colour():
    # The same circles the Saturday calendar uses, against the squares a move
    # carries: the shape says which kind of thing the line is.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([("2026-06-10T12:30:00+00:00", "USD", "CPI", "High"),
                ("2026-06-10T12:45:00+00:00", "EUR", "Trade Balance", "Medium")])
    out = md.calendar_context(hour, cal)
    assert "\U0001F534 USD CPI" in out
    assert "\U0001F7E0 EUR Trade Balance" in out


def test_news_outside_the_window_is_not_claimed_as_context():
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([("2026-06-10T09:00:00+00:00", "USD", "Old News", "High"),
                ("2026-06-10T16:30:00+00:00", "USD", "Much Later News", "High")])
    out = md.calendar_context(hour, cal)
    assert "Old News" not in out and "Much Later News" not in out


def test_a_release_just_after_the_move_is_named():
    # The window used to end exactly where the move did, which excluded the
    # releases a reader would blame first: a print five minutes after the hour
    # closed is a cause, not a coincidence.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([("2026-06-10T14:30:00+00:00", "USD", "FOMC Statement", "High")])
    assert "FOMC Statement" in md.calendar_context(hour, cal)


def test_a_crowded_window_is_listed_in_full():
    # The High filter is what keeps the line short. On a busy morning the tail
    # is the half worth reading, so it is not traded away to save two lines.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([(f"2026-06-10T12:{m:02d}:00+00:00", "USD", f"Print {m}", "High")
                for m in range(0, 60, 10)])
    out = md.calendar_context(hour, cal)
    assert out.count("\U0001F534") == 6
    assert "more" not in out
    for m in range(0, 60, 10):
        assert f"Print {m}" in out


def test_the_events_are_listed_in_the_order_they_happened():
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([("2026-06-10T14:30:00+00:00", "USD", "Later", "High"),
                ("2026-06-10T12:30:00+00:00", "USD", "Earlier", "High")])
    out = md.calendar_context(hour, cal)
    assert out.index("Earlier") < out.index("Later")


def test_a_missing_calendar_never_costs_the_alert():
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    assert md.calendar_context(hour, None) == ""
    text = md.format_push({"hour_utc": hour, "asset_id": "a:SPY", "tier": "major",
                           "basis": "abnormal", "r": 0.02}, {}, None)
    assert "happens about once a year" in text


def test_the_push_says_what_the_move_was_big_compared_with():
    # "+0.13%, biggest move in about three years" reads as a bug on its own, and
    # 45% of pushes carry a number under 1%. SHY's usual hour is 0.013%, so that
    # really is ten times normal - the message just never said so.
    event = {"asset_id": "twelvedata:SHY", "tier": "extreme", "basis": "absolute",
             "hour_utc": 1767225600, "r": 0.0013, "sigma_lt": 0.00013}
    text = md.format_push(event, {"twelvedata:SHY": "Treasuries 1-3 years"})
    assert "that is 10x its usual hour, which is 0.013%" in text


def test_a_modest_multiple_is_still_said_and_still_has_its_decimal():
    # It used to be suppressed below three times normal, and that silence read
    # as a gap rather than as "this one was only 2.7x". 21% of events fall under
    # the old floor. The decimal matters too: "3x" for 2.7 flatters the alert.
    event = {"asset_id": "twelvedata:SPY", "tier": "routine", "basis": "absolute",
             "hour_utc": 1767225600, "r": 0.0027, "sigma_lt": 0.001}
    assert "that is 2.7x its usual hour" in md.format_push(event, {})


def test_the_comparison_is_skipped_when_the_yardstick_is_missing():
    # sigma_LT is NaN through an instrument's first 720 bars, and a live event
    # there must still render rather than raise.
    event = {"asset_id": "twelvedata:SPY", "tier": "major", "basis": "absolute",
             "hour_utc": 1767225600, "r": 0.02, "sigma_lt": float("nan")}
    text = md.format_push(event, {})
    assert "usual hour" not in text and "+2.00%" in text


# --- when the next check-in is due -----------------------------------------
#
# The horizons are counted in the instrument's own bars, so the wait for one is
# a question about the trading calendar rather than about the clock. Counting it
# in hours - which it used to - told a Friday-afternoon push it was "coming
# within the hour" all through the weekend, because the hours passed and the
# bars did not.

FRIDAY_LAST_ETF_BAR = int(datetime(2026, 9, 4, 19, 0, tzinfo=timezone.utc).timestamp())


def spy(**over):
    return {"asset_id": "twelvedata:SPY", "hour_utc": FRIDAY_LAST_ETF_BAR,
            "tier": "extreme", "basis": "abnormal", "r": 0.03} | over


def test_a_check_in_is_named_as_a_close_rather_than_counted_down():
    # Both horizons are day closes, and for anything whose day is the UTC one
    # that close falls at midnight - so a bare timestamp read as a day later
    # than it is. Naming whose close it is also means the line does not tick,
    # so a message is not edited every hour to count it down.
    saturday = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    said = md._due_in(spy(), "settled", saturday)
    assert said == "coming at Tuesday's close (20:00 UTC)"
    assert "in " not in said.replace("coming at", "")


def test_the_weekend_does_not_move_the_answer_closer():
    friday = datetime(2026, 9, 4, 20, 10, tzinfo=timezone.utc)
    monday = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    assert md._due_in(spy(), "settled", friday) == md._due_in(spy(), "settled", monday)


def test_a_holiday_is_skipped_like_any_other_closed_day():
    # Monday 7 September 2026 is Labor Day, so the next day this instrument
    # trades is the Tuesday - which is what the session table says.
    due = md.due_moment(spy(), "settled")
    assert datetime.fromtimestamp(due, tz=timezone.utc).strftime("%a") == "Tue"


def test_this_days_close_is_the_end_of_the_move_s_own_day():
    hour = int(datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc).timestamp())
    due = md.due_moment(spy(hour_utc=hour), "today")
    assert datetime.fromtimestamp(due, tz=timezone.utc).strftime("%a %H:%M") == "Tue 20:00"


def test_a_move_in_the_closing_hour_has_its_own_day_end_immediately():
    # There is no day left to hold through, so the moment is the end of the bar.
    due = md.due_moment(spy(), "today")          # Friday's last ETF bar
    assert datetime.fromtimestamp(due, tz=timezone.utc).strftime("%a %H:%M") == "Fri 20:00"


def test_round_the_clock_days_end_at_midnight():
    btc = {"asset_id": "coinbase:BTC-USD", "hour_utc": FRIDAY_LAST_ETF_BAR,
           "basis": "absolute"}
    now = datetime(2026, 9, 4, 20, 10, tzinfo=timezone.utc)
    assert md._due_in(btc, "today", now) == "coming at Friday's close (00:00 UTC)"


def test_the_settled_reading_is_dated_by_the_next_trading_day():
    # Not "24 hours later": the settled reading lands at the close of the next
    # day the instrument actually trades, and over Labor Day weekend that is the
    # Tuesday.
    due = md.due_moment(spy(), "settled")
    moment = datetime.fromtimestamp(due, tz=timezone.utc)
    assert moment.strftime("%a %H:%M") == "Tue 20:00"


def test_an_undatable_check_in_says_less_rather_than_something_wrong(monkeypatch):
    # An unreadable session table must not raise inside a push that is going out.
    monkeypatch.setattr(md, "due_moment", lambda e, h: None)
    assert md._due_in(spy(), "today") == "coming at this day's close"
    assert md._due_in(spy(), "settled") == "coming at the next day's close"


def test_a_landed_horizon_is_not_a_promise():
    # The placeholder is only for the check-ins that have no answer yet.
    midday = spy(hour_utc=int(datetime(2026, 9, 8, 14, tzinfo=timezone.utc).timestamp()),
                 retention_today=0.9)
    lines = md.check_in_lines(midday, now=NOW)
    assert "this day's close - still there" in lines[0]
    assert "next day's close - coming" in lines[1]


# --- the note is written into, not written up -------------------------------
#
# The digest is opened at the start of the period it covers and edited in place
# as events are found. Telegram notifies on a new message and stays silent on an
# edit, so the reader is interrupted twice a week and everything after that
# arrives quietly in a message they already have.

def digest_row(**over):
    return event(event_id="d1", channel="digest", tier="notable",
                 retention_today=None, retention_settled=None) | over


def test_a_later_move_edits_the_open_note_rather_than_sending_another(
        monkeypatch, sender, editor):
    _, state = deliver(monkeypatch, [digest_row()])
    assert len(notes(sender)) == 1 and editor.calls == []

    second = digest_row(event_id="d2", asset_id="coinbase:BTC-USD")
    deliver(monkeypatch, [digest_row(), second], state=state)
    assert len(notes(sender)) == 1          # nothing new arrived on the phone
    assert len(editor.calls) == 1
    assert "Bitcoin" in editor.calls[0][1] and "Gold" in editor.calls[0][1]


def test_a_note_that_has_not_changed_is_not_edited(monkeypatch, sender, editor):
    # Telegram rejects an edit whose text matches what is already there, and
    # most hours change nothing.
    _, state = deliver(monkeypatch, [digest_row()])
    deliver(monkeypatch, [digest_row()], state=state)
    deliver(monkeypatch, [digest_row()], state=state)
    assert editor.calls == []


def test_a_row_says_when_its_answer_is_due(monkeypatch, sender, editor):
    # It is written the hour the move is found, long before the market has
    # answered, so a row that said only its size would look like a bot that had
    # forgotten to come back.
    deliver(monkeypatch, [digest_row()])
    assert "next day's close - coming" in notes(sender)[0]


def test_the_answer_replaces_the_promise_when_it_lands(monkeypatch, sender, editor):
    _, state = deliver(monkeypatch, [digest_row()])
    deliver(monkeypatch, [digest_row(retention_settled=0.95)], state=state)
    assert len(editor.calls) == 1
    text = editor.calls[0][1]
    assert "next day's close - still there" in text


def test_a_move_that_reverted_stays_in_the_note_and_says_so(
        monkeypatch, sender, editor):
    # It used to be dropped before anyone saw it. Now it is already on the
    # reader's phone by the time the answer arrives, and unsending is not a
    # thing Telegram can do - so the line is corrected instead.
    _, state = deliver(monkeypatch, [digest_row()])
    deliver(monkeypatch, [digest_row(retention_settled=-0.4)], state=state)
    assert "next day's close - fully reversed" in editor.calls[0][1]


def test_a_closed_note_is_still_corrected_when_its_last_answer_arrives(
        monkeypatch, sender, editor):
    # A move an hour before the period ends is answered at the next close,
    # days after the note stopped taking new events.
    _, state = deliver(monkeypatch, [digest_row()])
    later = NOW + timedelta(days=4)
    deliver(monkeypatch, [digest_row(retention_settled=0.9)], state=state, now=later)
    assert len(editor.calls) == 1
    assert "next day's close - still there" in editor.calls[0][1]


def test_a_note_is_forgotten_once_nothing_about_it_can_change(
        monkeypatch, sender, editor):
    _, state = deliver(monkeypatch, [digest_row()])
    assert state[md.STATE_KEY][md.DIGEST_STATE]
    much_later = NOW + timedelta(hours=md.DIGEST_TRACK_HOURS + 1)
    _, state = deliver(monkeypatch, [digest_row()], state=state, now=much_later)
    assert str(SLOT) not in state[md.STATE_KEY][md.DIGEST_STATE]


def test_a_failed_edit_is_retried_rather_than_lost(monkeypatch, sender):
    failing = Edited(fail=True)
    monkeypatch.setattr(md, "edit_telegram_message", failing)
    _, state = deliver(monkeypatch, [digest_row()])
    deliver(monkeypatch, [digest_row(retention_settled=0.9)], state=state)

    working = Edited()
    monkeypatch.setattr(md, "edit_telegram_message", working)
    deliver(monkeypatch, [digest_row(retention_settled=0.9)], state=state)
    assert len(working.calls) == 1


def test_a_move_carries_its_tier_as_a_colour(monkeypatch, sender):
    # Squares for moves against the calendar's circles, so the shape says which
    # kind of thing a coloured line is before the words do.
    for tier, square in md.TIER_EMOJI.items():
        assert md.describe(event(tier=tier), LABELS).startswith(square)


def test_a_note_that_loses_a_part_does_not_leave_a_stale_one(monkeypatch, sender, editor):
    # A recompute that no longer produces an event takes its lines with it, and
    # Telegram cannot delete a message - so the surplus part is emptied instead
    # of being left saying "part 3 of 5" under a note that now has two.
    many = [event(event_id=f"d{i}", channel="digest", tier="routine",
                  hour_utc=int(NOW.timestamp()) - HOUR, digest_slot=SLOT)
            for i in range(200)]
    _, state = deliver(monkeypatch, many)
    parts = len(notes(sender))
    assert parts > 2

    deliver(monkeypatch, many[:2], state=state)
    emptied = [t for _, t in editor.calls if md._EMPTIED_PART in t]
    assert len(emptied) == parts - 1


def test_an_hour_that_has_not_happened_yet_is_not_written_down(monkeypatch, sender):
    # A bar has to close before it is scored, so this should not arise - but a
    # clock skew must not put tomorrow in today's note.
    ahead = digest_row(hour_utc=int(NOW.timestamp()) + 2 * HOUR)
    deliver(monkeypatch, [ahead])
    assert "Gold" not in notes(sender)[0]


# --- the opening hour is the one thing that cannot slip ---------------------
#
# The whole arrangement is worth having because the two interruptions land at
# noon on a Tuesday and a Friday. A note opened whenever the system happened to
# next run is an ordinary unscheduled buzz wearing a schedule's clothes.

LATE = datetime.fromtimestamp(SLOT + 10 * HOUR, tz=timezone.utc)


def test_a_note_is_not_opened_hours_after_its_hour(monkeypatch, sender):
    deliver(monkeypatch, [event(channel="digest")], now=LATE)
    assert notes(sender) == []


def test_one_missed_run_does_not_cost_the_note(monkeypatch, sender):
    # The trigger is an external service. Three hours of grace, because 15:00 is
    # still an afternoon and 22:00 is not.
    late_but_ok = datetime.fromtimestamp(SLOT + 3 * HOUR, tz=timezone.utc)
    deliver(monkeypatch, [event(channel="digest")], now=late_but_ok)
    assert len(notes(sender)) == 1


def test_a_period_whose_note_never_opened_is_carried_into_the_next(monkeypatch, sender):
    # Both halves have to be true at once: the buzz is always at noon, and no
    # move is silently dropped for want of a scheduler.
    missed = event(event_id="d1", channel="digest", tier="routine",
                   hour_utc=SLOT + 5 * HOUR)
    _, state = deliver(monkeypatch, [missed], now=LATE)
    assert notes(sender) == []

    next_slot = routing.next_digest_slot(SLOT)
    opens = datetime.fromtimestamp(next_slot, tz=timezone.utc)
    deliver(monkeypatch, [missed], state=state, now=opens)
    assert len(notes(sender)) == 1
    assert "Gold" in notes(sender)[0]


def test_the_carried_note_says_which_period_it_covers(monkeypatch, sender):
    # The header states the period the note speaks for, not the day it was
    # posted, because those come apart exactly when it matters.
    elsewhere = [event(channel="digest", hour_utc=SLOT - 40 * 24 * HOUR)]
    _, state = deliver(monkeypatch, elsewhere, now=LATE)
    opens = datetime.fromtimestamp(routing.next_digest_slot(SLOT), tz=timezone.utc)
    deliver(monkeypatch, elsewhere, state=state, now=opens)
    # A week, not the usual three days: it picked up the period that never opened.
    assert "Fri 4 to Fri 11 September" in notes(sender)[0]


def test_an_ordinary_note_covers_only_its_own_period(monkeypatch, sender):
    elsewhere = [event(channel="digest", hour_utc=SLOT - 40 * 24 * HOUR)]
    opens = datetime.fromtimestamp(SLOT, tz=timezone.utc)
    _, state = deliver(monkeypatch, elsewhere, now=opens)
    later = datetime.fromtimestamp(routing.next_digest_slot(SLOT), tz=timezone.utc)
    deliver(monkeypatch, elsewhere, state=state, now=later)
    assert "Tue 8 to Fri 11 September" in notes(sender)[1]


def test_a_note_whose_first_post_failed_does_not_cover_its_period(monkeypatch):
    # It is a post that will be retried, not a note the reader has - so the next
    # note must still pick those rows up if it never lands.
    failing = Sent(fail=True)
    monkeypatch.setattr(md, "send_telegram_message", failing)
    monkeypatch.setattr(md, "_labels", lambda: LABELS)
    row = event(event_id="d1", channel="digest", tier="routine",
                hour_utc=SLOT + 2 * HOUR)
    _, state = deliver(monkeypatch, [row], now=datetime.fromtimestamp(SLOT, tz=timezone.utc))

    working = Sent()
    monkeypatch.setattr(md, "send_telegram_message", working)
    opens = datetime.fromtimestamp(routing.next_digest_slot(SLOT), tz=timezone.utc)
    deliver(monkeypatch, [row], state=state, now=opens)
    assert any("Gold" in t for t in working.texts)


def test_a_move_folded_into_a_push_takes_no_row_in_the_note(monkeypatch, sender):
    # The push already speaks for it and now carries its size. A row of its own
    # in the note - arriving under that push, within the hour - would be one
    # episode reaching the reader twice.
    row = digest_row(asset_id="coinbase:BTC-USD", folded_into="twelvedata:GLD")
    deliver(monkeypatch, [row])
    assert "Bitcoin" not in notes(sender)[0]


def test_a_move_that_belongs_to_no_push_keeps_its_row(monkeypatch, sender):
    deliver(monkeypatch, [digest_row(folded_into="")])
    assert "Gold" in notes(sender)[0]


def test_the_third_check_in_lands_on_a_push_that_already_has_two(monkeypatch, sender, editor):
    # The horizons are not all the same kind of thing - two and six are bar
    # counts, "settled" is a moment - and holding both in one set used to raise
    # the moment the third answer arrived, inside the hourly delivery run.
    pushed = event(channel="push", retention_today=None, retention_settled=None)
    _, state = deliver(monkeypatch, [pushed])
    tracked = state[md.STATE_KEY][follow_up_module.TRACKED]
    assert tracked

    deliver(monkeypatch, [event(channel="push", retention_today=0.9,
                                retention_settled=None)], state=state)
    deliver(monkeypatch, [event(channel="push", retention_today=0.9,
                                retention_settled=0.8)], state=state)
    assert len(editor.calls) == 2
    assert "next day's close - 80% of it still there" in editor.calls[-1][1]
    # All three written, so the push is no longer tracked.
    assert not state[md.STATE_KEY][follow_up_module.TRACKED]


# --- saying it in terms nobody needs statistics for -------------------------

def test_the_alert_says_when_this_instrument_was_last_this_rare():
    # "Biggest move in about a year" is a return period fitted to a tail: the
    # honest way to say how unusual something is, and a hard thing to picture.
    # The date is the same claim in a form that needs no statistics.
    hour = int(datetime(2026, 9, 4, 14, tzinfo=timezone.utc).timestamp())
    history = [
        event(event_id="old", tier="major", hour_utc=hour - 400 * 24 * HOUR),
        event(event_id="tiny", tier="routine", hour_utc=hour - 3 * 24 * HOUR),
    ]
    line = md._since_note(event(tier="major", hour_utc=hour), history)
    assert "the last one this big was 31 July 2025" in line
    assert "1.1 years ago" in line


def test_a_rarer_earlier_move_counts_and_a_milder_one_does_not():
    hour = int(datetime(2026, 9, 4, 14, tzinfo=timezone.utc).timestamp())
    milder = [event(event_id="m", tier="notable", hour_utc=hour - 10 * 24 * HOUR)]
    rarer = [event(event_id="r", tier="extreme", hour_utc=hour - 10 * 24 * HOUR)]
    assert md._since_note(event(tier="major", hour_utc=hour), milder) == ""
    assert "10 days ago" in md._since_note(event(tier="major", hour_utc=hour), rarer)


def test_nothing_is_claimed_when_there_is_no_earlier_one():
    # A young instrument, or genuinely the first in twenty-two years. Claiming
    # either would be a guess.
    assert md._since_note(event(), []) == ""




def test_a_move_in_the_closing_hour_reports_no_ratio_for_its_own_day():
    # 6% of moves are made in the last hour their instrument trades that day.
    # There is nothing left of the day to hold through, so the ratio is one by
    # construction and "still there" would be reporting arithmetic as news.
    closing = spy(retention_today=1.0)          # Friday's last ETF bar
    lines = md.check_in_lines(closing, now=NOW)
    assert lines[0] == "     this day's close - the move was in the closing hour"

    midday = spy(hour_utc=int(datetime(2026, 9, 8, 14, tzinfo=timezone.utc).timestamp()),
                 retention_today=1.0)
    assert "still there" in md.check_in_lines(midday, now=NOW)[0]
