from datetime import date, datetime, timedelta, timezone

import pandas as pd

import pytest

from price_monitor import follow_up as follow_up_module
from price_monitor import tremor_delivery as md
from price_monitor.config import Config
from price_monitor.notifier import TelegramError

HOUR = 3600
NOW = datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc)   # a Monday, inside the note's opening window
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


def dated(since=int(datetime(2020, 3, 16, tzinfo=timezone.utc).timestamp()), hour=None, **over):
    """The two fields every record claim is built from: when the move was, and
    the last time the instrument matched it."""
    base = {"hour_utc": hour if hour is not None
            else int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp()),
            "record_since": since}
    return base | over


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
    elsewhere = event(event_id="old", channel="digest", tier="noticeable",
                      hour_utc=SLOT - 30 * 24 * HOUR)
    deliver(monkeypatch, [elsewhere])
    assert len(notes(sender)) == 1
    assert "Nothing so far" in notes(sender)[0]
    assert "updated as moves are found" in notes(sender)[0]


def test_a_move_joins_the_note_that_is_already_open(monkeypatch, sender):
    row = event(event_id="d1", channel="digest", tier="noticeable")
    deliver(monkeypatch, [row])
    assert "Gold" in notes(sender)[0]


def test_a_move_from_before_the_note_opened_is_not_in_it(monkeypatch, sender):
    stale = event(event_id="d1", channel="digest", tier="noticeable",
                  hour_utc=SLOT - 4 * 24 * HOUR)
    deliver(monkeypatch, [stale])
    assert len(notes(sender)) == 1 and "Gold" not in notes(sender)[0]


def test_the_note_is_one_message_for_many_events(monkeypatch, sender):
    rows = [event(event_id=f"d{i}", channel="digest", tier="noticeable",
                  hour_utc=SLOT + i * HOUR) for i in range(3)]
    deliver(monkeypatch, rows)
    assert len(notes(sender)) == 1
    assert notes(sender)[0].count(md.TIER_EMOJI["noticeable"]) == 3


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


def test_the_severity_leads_the_digest(monkeypatch, sender):
    # A digest read only as far as its notification preview should still
    # deliver its most important line.
    rows = [
        event(event_id="a", channel="digest", tier="noticeable", digest_slot=SLOT),
        event(event_id="b", channel="digest", tier="major",
              asset_id="coinbase:BTC-USD", digest_slot=SLOT),
    ]
    deliver(monkeypatch, rows)
    text = notes(sender)[0]
    assert text.index("Bitcoin") < text.index("Gold")


def test_a_long_digest_is_split_within_telegrams_limit(monkeypatch, sender):
    rows = [event(event_id=f"d{i}", channel="digest", tier="noticeable",
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


def test_a_missing_parquet_file_is_not_an_error(tmp_path):
    quiet = cfg(tremor_events_path=str(tmp_path / "nope.parquet"))
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
    event = dated()
    assert md._headline(event, "major", "abnormal") == (
        "the biggest move of its own since March 2020")
    assert md._headline(event, "major", "absolute") == (
        "the biggest move since March 2020")
    assert md._headline(event, "major", "both") == (
        "the biggest move since March 2020")


def test_the_claim_is_a_record_and_names_the_date():
    # It used to be a frequency - "about once in 3 years" - because the fitted
    # ladder could not support a record claim and the two flatly contradicted
    # each other: "biggest move in about three years" over "the last one this
    # big was 23 days ago". A rung is now literally the biggest move in its own
    # lookback, so the record claim is the true one and it can name the bar.
    assert md._headline(dated(), "noticeable", "absolute") == (
        "the biggest move since March 2020")
    assert "once in" not in md._headline(dated(), "extreme", "absolute")


def test_a_move_bigger_than_anything_on_record_says_so_rather_than_guessing():
    # No earlier bar to name, so there is no date to print. Inventing one would
    # be the only outright false thing this line could say.
    # "in the whole record" would overclaim on a warm run, where the archive is
    # trimmed to the record horizon and cannot speak for what sits below it.
    assert md._headline(dated(since=None), "extreme", "absolute") == (
        "the biggest move in at least 6 years")


def test_the_date_gets_coarser_the_further_back_it_is():
    # Within a month the day is what places it; past a year only the year is.
    now = int(datetime(2026, 6, 10, tzinfo=timezone.utc).timestamp())
    day = 86400
    assert md.record_phrase({"hour_utc": now, "record_since": now - 10 * day}) \
        == "since 31 May"
    assert md.record_phrase({"hour_utc": now, "record_since": now - 120 * day}) \
        == "since February"
    assert md.record_phrase({"hour_utc": now, "record_since": now - 900 * day}) \
        == "since December 2023"


def test_no_alert_claims_the_economic_calendar_explained_anything():
    # The residual is r minus what the basket and block factors predicted; the
    # calendar enters only the SI-Index, never this basis. An alert naming it
    # would be reporting a test the system never ran.
    for tier in ("noticeable", "high", "major", "extreme"):
        for basis in ("abnormal", "absolute", "both", "market"):
            assert "calendar" not in md._headline(dated(), tier, basis).lower()
    for line in md._split_lines({"r": 0.02, "e_resid": 0.018}, "Gold"):
        assert "calendar" not in line.lower()


def test_the_split_is_two_parts_and_never_the_word_market():
    # For the S&P 500, "the market" IS the S&P 500 - so naming the idea invited
    # "which market, and how would I have followed it?". The parts are named by
    # what they actually are instead.
    lines = md._split_lines({"r": 0.0700, "e_resid": 0.0100,
                             "co_block": 0.0600, "block": "equity"}, "S&P 500")
    # Two lines and no header: "of that move:" was a whole line spent saying
    # that the two beneath it add up, which the numbers already show.
    assert "+6.00%  block moving, [US and global equities]" in lines[0]
    assert "+1.00%  move on its own" in lines[1]
    assert len(lines) == 2
    for line in lines:
        assert "market" not in line


def test_the_block_line_carries_the_cause_on_its_own():
    # On 2008-11-20 the financial sector's +10.50% came almost entirely from the
    # equity block. Naming the block is the diagnosis a reader can act on; the
    # basket term that used to sit above this line said -0.21% and nothing else.
    lines = md._split_lines({"r": 0.1050, "e_resid": 0.0088,
                             "co_block": 0.0962, "block": "equity"},
                            "US financial sector")
    assert "+9.62%  block moving, [US and global equities]" in lines[0]
    assert "+0.88%  move on its own" in lines[1]


def test_a_block_that_contributed_nothing_still_takes_its_line():
    # Zero is an answer here, and a load-bearing one: "its block did nothing and
    # the instrument did all of it" is the strongest thing the split can say, so
    # dropping the line would delete the finding.
    lines = md._split_lines({"r": 0.018, "e_resid": 0.018,
                             "co_block": 0.0, "block": "FX"}, "Euro / dollar")
    assert "+0.00%  block moving, [the dollar block]" in lines[0]
    assert "+1.80%  move on its own" in lines[1]


def test_the_split_falls_back_to_the_difference_on_an_older_row():
    # Before the split was carried, only the total was.
    lines = md._split_lines({"r": 0.02, "e_resid": 0.018, "block": "precious_metals"},
                            "Gold")
    assert "+0.20%  block moving, [precious metals]" in lines[0]
    assert "+1.80%  move on its own" in lines[1]


def test_no_split_is_claimed_when_the_regression_has_not_been_fitted():
    # Beta is undefined through an instrument's first five hundred bars.
    assert md._split_lines({"r": 0.02, "e_resid": None}, "Gold") == []
    assert md._split_lines({}, "Gold") == []


def test_the_headline_does_not_claim_the_market_was_quiet():
    # A large residual means the co-movement does not ACCOUNT for the size of
    # the move. It does not mean the rest of the market was calm - on a macro
    # hour everything moves and this one moved further still, which is the case
    # the residual channel exists to catch.
    line = md._headline(dated(), "major", "abnormal")
    for overclaim in ("usual", "quiet", "normal", "calm", "did not move"):
        assert overclaim not in line



def _cal(rows):
    """rows: (iso date, country, title, impact)."""
    return [{"date": d, "country": c, "title": t, "impact": i,
             "actual": "", "forecast": "", "previous": ""} for d, c, t, i in rows]


def test_a_push_names_the_scheduled_news_behind_it():
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([("2026-06-10T12:30:00+00:00", "USD", "Core CPI m/m", "High"),
                ("2026-06-10T13:00:00+00:00", "USD", "Fed Chair Speaks", "High")])
    out = md.calendar_context(hour, cal)
    assert out.startswith("Nearby economic events (-2h+1h):")
    assert "USD Core CPI m/m" in out and "USD Fed Chair Speaks" in out


def test_a_push_with_nothing_scheduled_prints_no_calendar_line_at_all():
    # It used to say "none scheduled", and the statistic behind that is real:
    # 55% of pushes in the record have no Medium or High release in the window.
    # Which is exactly why the line went - on more than half of all messages it
    # was a line saying nothing had happened, and a line that usually says
    # nothing stops being read. The absence is carried by the absence.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    elsewhere = _cal([("2026-05-01T12:00:00+00:00", "USD", "Old CPI", "High")])
    assert md.calendar_context(hour, elsewhere) == ""


def test_an_empty_archive_claims_nothing_rather_than_claiming_silence():
    # An empty archive cannot tell "nothing was scheduled" from "nothing was
    # loaded". Now that a quiet window prints nothing either, the two agree on
    # the output - but for different reasons, and this is the one that would
    # have to change first if the line ever came back.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    assert md.calendar_context(hour, []) == ""


def test_low_impact_news_is_not_named():
    # High and Medium are shown, the same two the Saturday calendar shows. Low
    # is dominated by bank holidays and minor prints, and naming those would
    # turn the most important line of the most important message into noise.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    only_low = _cal([("2026-06-10T12:30:00+00:00", "CHF", "Bank Holiday", "Low")])
    assert md.calendar_context(hour, only_low) == ""

    medium = _cal([("2026-06-10T12:45:00+00:00", "EUR", "Trade Balance", "Medium")])
    assert "Trade Balance" in md.calendar_context(hour, medium)


def test_each_named_release_carries_its_impact_colour():
    # The same circles the Saturday calendar uses, against the squares a move
    # carries: the shape says which kind of thing the line is.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([("2026-06-10T12:30:00+00:00", "USD", "CPI", "High"),
                ("2026-06-10T12:45:00+00:00", "EUR", "Trade Balance", "Medium")])
    out = md.calendar_context(hour, cal)
    assert "\U0001F534 \U0001F1FA\U0001F1F8 USD CPI" in out
    assert "\U0001F7E0 \U0001F1EA\U0001F1FA EUR Trade Balance" in out


def test_a_release_carries_its_country_flag_beside_the_code():
    # The flag is what is caught at a glance; the code is what makes it certain.
    # Several of these flags are the same two colours in nearly the same
    # arrangement at the size a phone draws them.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([("2026-06-10T12:30:00+00:00", "AUD", "Employment Change", "High"),
                ("2026-06-10T12:40:00+00:00", "NZD", "Official Cash Rate", "High"),
                ("2026-06-10T12:50:00+00:00", "All", "G7 Meetings", "High")])
    out = md.calendar_context(hour, cal)
    assert "\U0001F1E6\U0001F1FA AUD" in out
    assert "\U0001F1F3\U0001F1FF NZD" in out
    # No country at all is the source's own answer, and a globe is the honest
    # rendering of it rather than a stand-in for a missing flag.
    assert "\U0001F310 All" in out


def test_a_currency_with_no_flag_still_prints_its_code():
    # The source can add a currency whenever it likes and the message must not
    # sprout a placeholder box when it does.
    hour = int(datetime(2026, 6, 10, 14, tzinfo=timezone.utc).timestamp())
    cal = _cal([("2026-06-10T12:30:00+00:00", "XYZ", "Rate Decision", "High")])
    out = md.calendar_context(hour, cal)
    assert "XYZ Rate Decision" in out


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
                           "basis": "abnormal", "r": 0.02,
                           "record_since": hour - 900 * 86400}, {}, None)
    assert "the biggest move of its own since" in text


def test_the_push_says_what_the_move_was_big_compared_with():
    # "+0.13%, biggest move in about three years" reads as a bug on its own, and
    # 45% of pushes carry a number under 1%. SHY's usual hour is 0.013%, so that
    # really is ten times normal - the message just never said so.
    event = {"asset_id": "twelvedata:SHY", "tier": "extreme", "basis": "absolute",
             "hour_utc": 1767225600, "r": 0.0013, "sigma_lt": 0.00013}
    text = md.format_push(event, {"twelvedata:SHY": "Treasuries 1-3 years"})
    assert "10x usual hour" in text


def test_a_modest_multiple_is_still_said_and_still_has_its_decimal():
    # It used to be suppressed below three times normal, and that silence read
    # as a gap rather than as "this one was only 2.7x". 21% of events fall under
    # the old floor. The decimal matters too: "3x" for 2.7 flatters the alert.
    event = {"asset_id": "twelvedata:SPY", "tier": "noticeable", "basis": "absolute",
             "hour_utc": 1767225600, "r": 0.0027, "sigma_lt": 0.001}
    assert "2.7x usual hour" in md.format_push(event, {})


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
    for horizon in ("today", "settled"):
        assert md._due_in(spy(), horizon).startswith("coming,")


def test_an_undatable_check_in_does_not_repeat_the_horizon_it_is_written_under(
        monkeypatch):
    # The full line is "<horizon> - <answer>", so a fallback naming the horizon
    # again read "this day's close - coming at this day's close".
    monkeypatch.setattr(md, "due_moment", lambda e, h: None)
    lines = md.check_in_lines(event(retention_today=None,
                                    retention_settled=None), NOW)
    for line in lines:
        label, _, answer = line.strip().partition(" - ")
        assert label and label not in answer


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
    return event(event_id="d1", channel="digest", tier="high",
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
    deliver(monkeypatch, [digest_row(retention_settled=-0.02)], state=state)
    assert "next day's close - fully reversed" in editor.calls[0][1]


def test_a_move_that_reversed_past_its_start_says_how_far_past(
        monkeypatch, sender, editor):
    # A fifth of settled readings are negative: the price gave the move back
    # and kept going the other way. Calling that "fully reversed" throws away
    # the louder half of the fact, so the overshoot is said in the move's own
    # units, the same way a continuation is.
    _, state = deliver(monkeypatch, [digest_row()])
    deliver(monkeypatch, [digest_row(retention_settled=-0.4)], state=state)
    assert ("next day's close - reversed past where it started, "
            "40% of the move the other way") in editor.calls[0][1]

    _, state = deliver(monkeypatch, [digest_row()])
    deliver(monkeypatch, [digest_row(retention_settled=-1.7)], state=state)
    assert ("next day's close - reversed past where it started, "
            "1.7x the move the other way") in editor.calls[1][1]


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
    many = [event(event_id=f"d{i}", channel="digest", tier="noticeable",
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
    missed = event(event_id="d1", channel="digest", tier="noticeable",
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
    # Longer than its own period: it picked up the one that never opened.
    covers = datetime.fromtimestamp(SLOT, tz=timezone.utc)
    ends = datetime.fromtimestamp(routing.next_digest_slot(int(opens.timestamp())),
                                  tz=timezone.utc)
    last = ends - timedelta(hours=1)      # the last day it can hold an hour of
    assert f"{covers:%a %-d} to {last:%a %-d %B}" in notes(sender)[0]
    assert (ends - covers).days > (ends - opens).days


def test_an_ordinary_note_covers_only_its_own_period(monkeypatch, sender):
    elsewhere = [event(channel="digest", hour_utc=SLOT - 40 * 24 * HOUR)]
    opens = datetime.fromtimestamp(SLOT, tz=timezone.utc)
    _, state = deliver(monkeypatch, elsewhere, now=opens)
    later = datetime.fromtimestamp(routing.next_digest_slot(SLOT), tz=timezone.utc)
    deliver(monkeypatch, elsewhere, state=state, now=later)
    ends = datetime.fromtimestamp(routing.next_digest_slot(int(later.timestamp())),
                                  tz=timezone.utc) - timedelta(hours=1)
    # Only its own stretch, because the note before it did open.
    assert f"{later:%a %-d} to {ends:%a %-d %B}" in notes(sender)[1]


def test_a_note_whose_first_post_failed_does_not_cover_its_period(monkeypatch):
    # It is a post that will be retried, not a note the reader has - so the next
    # note must still pick those rows up if it never lands.
    failing = Sent(fail=True)
    monkeypatch.setattr(md, "send_telegram_message", failing)
    monkeypatch.setattr(md, "_labels", lambda: LABELS)
    row = event(event_id="d1", channel="digest", tier="noticeable",
                hour_utc=SLOT + 2 * HOUR)
    _, state = deliver(monkeypatch, [row], now=datetime.fromtimestamp(SLOT, tz=timezone.utc))

    working = Sent()
    monkeypatch.setattr(md, "send_telegram_message", working)
    opens = datetime.fromtimestamp(routing.next_digest_slot(SLOT), tz=timezone.utc)
    deliver(monkeypatch, [row], state=state, now=opens)
    assert any("Gold" in t for t in working.texts)



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

def test_only_one_date_line_and_it_is_the_exact_one():
    # There used to be two. The headline said how OFTEN a move like this happens
    # and a second line said when the last one was - and under a fitted ladder
    # those could flatly contradict each other ("biggest move in about three
    # years" over "the last one this big was 23 days ago").
    #
    # The headline is now itself a date, read off the bar the level was measured
    # against, so the second line was the same claim computed a weaker way: it
    # searched the EVENTS table for the last row at this tier or rarer, which
    # can be a different bar entirely - a smaller move that still cleared the
    # rung, or one claimed by the other ladder. Measured on a real push the two
    # disagreed, "the biggest since July" against "similar move 28 days ago".
    hour = int(datetime(2026, 9, 4, 14, tzinfo=timezone.utc).timestamp())
    history = [event(event_id="old", tier="major", hour_utc=hour - 400 * 24 * HOUR)]
    row = event(tier="major", hour_utc=hour, basis="absolute",
                record_since=hour - 400 * 24 * HOUR)
    text = md.format_push(row, LABELS, None, events=history)

    assert "the biggest move since" in text
    for gone in ("similar move", "ago"):
        assert gone not in text


def test_a_move_in_the_closing_hour_reports_no_ratio_for_its_own_day():
    # 6% of moves are made in the last hour their instrument trades that day.
    # There is nothing left of the day to hold through, so the ratio is one by
    # construction and "still there" would be reporting arithmetic as news.
    closing = spy(retention_today=1.0)          # Friday's last ETF bar
    lines = md.check_in_lines(closing, now=NOW)
    assert lines[0] == "\tthis day's close - the move was in the closing hour"

    midday = spy(hour_utc=int(datetime(2026, 9, 8, 14, tzinfo=timezone.utc).timestamp()),
                 retention_today=1.0)
    assert "still there" in md.check_in_lines(midday, now=NOW)[0]


def test_the_block_line_names_the_instrument_s_peers():
    # The block factor is a leave-one-out median: the instrument is measured
    # against its neighbours, never against itself, so naming it in its own peer
    # group would misdescribe the number on the line.
    peers = md._block_peers({"asset_id": "twelvedata:SPY", "block": "equity"})
    assert peers.startswith("XLK, XLF, XLY")
    assert "QQQ" in peers and "EEM" in peers
    assert "SPY" not in peers


def test_the_headline_leads_with_the_rarity_the_ticker_and_the_move():
    # The rarity is a colour so it reads before any word does; the ticker is
    # what a reader types into a chart; the move is the number they came for and
    # it used to be on the second line.
    text = md.describe(event(asset_id="twelvedata:GLD"), LABELS)
    first = text.split("\n")[0]
    assert first == md.TIER_EMOJI["major"] + " <b>GLD</b> · Gold · +2.10%"


def test_the_hour_is_the_last_line_and_is_bold():
    # Everything above it is what happened; this is when. Bold because it is the
    # one thing a reader cross-checks against a chart.
    text = md.describe(event(asset_id="twelvedata:GLD"), LABELS)
    last = text.split("\n")[-1]
    assert last.startswith(md.TIME_EMOJI)
    stamp = (NOW - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    assert last.endswith("UTC</b>") and f"<b>{stamp}" in last


def test_the_footer_names_every_instrument_that_is_tracked():
    # A reader told "its own block moved" is entitled to know which instruments
    # that block holds, and the honest form is a list.
    footer = md.basket_footer()
    for ticker in ("SPY", "XLK", "TLT", "HYG", "GLD", "EUR/USD", "BTC-USD"):
        assert ticker in footer
    assert "DBC*" in footer              # watched, not counted in its own block
    assert "61 instruments tracked" in footer
    # Every block gets a line of its own, under the name the move's own line uses.
    for label in ("US and global equities", "US Treasuries",
                  "corporate and sovereign credit", "precious metals"):
        assert label in footer


# --- a block's own move ------------------------------------------------------

def block_event(**over):
    base = dict(event_id="block_equity:1", asset_id="block:equity", block="equity",
                hour_utc=int(datetime(2026, 9, 8, 14, tzinfo=timezone.utc).timestamp()),
                peak_hour_utc=int(datetime(2026, 9, 8, 14, tzinfo=timezone.utc).timestamp()),
                tier="extreme", basis="block", r=-0.0241, e_resid=-0.0241,
                co_block=0.0, sigma_lt=0.0058, n_members=16,
                leaders="XLE -6.20%, XLF -5.80%, XLI -5.10%, XLB -4.90%",
                channel="push", z_resid=-4.2)
    return base | over


def test_a_block_move_is_told_as_a_block_and_not_as_an_instrument():
    # It has no ticker to chart, no price level and no split into "its block and
    # itself" - it IS the block - so the lines that would say those things are
    # replaced by what a typical member did and which members did most of it.
    text = md.describe(block_event(), LABELS)

    # Black IN FRONT OF the rarity, not instead of it: a block is the same four
    # rarities read at a different level of the market, and dropping the colour
    # would trade what every line is skimmed by for what one line in twenty needs.
    assert text.startswith(md.BLOCK_MARK + md.TIER_EMOJI["extreme"]
                           + " <b>US and global equities</b> · ")
    assert "of that move" not in text
    assert "whole block moved together" in text.lower()
    assert "the typical member moved -2.41%" in text
    assert "4.2x a typical member's usual hour" in text
    assert "biggest movers: XLE -6.20%, XLF -5.80%" in text
    assert "(of 16 trading that hour)" in text
    # No split line: there is nothing above a block to explain its move with.
    assert "\t+6.00%  block moving," not in text
    assert "on its own" not in text


def test_a_block_move_still_gets_its_two_check_ins():
    # The question "did it hold" is the same question for a block as for an
    # instrument, and it is the one the reader asks next.
    lines = md.describe(block_event(retention_today=1.4), LABELS).splitlines()
    assert any("this day's close - kept going, 1.4x the original move" in l for l in lines)
    assert any("next day's close -" in l for l in lines)



def test_a_block_check_in_is_dated_on_its_members_calendar():
    # Without this a block would fall back to the round-the-clock calendar and
    # promise a US block's close at midnight - eight hours before it happens, on
    # a day the market is shut.
    assert md._template("block:equity") == "us_equity"
    assert md._template("block:crypto") == "crypto_24_7"
    assert md._template("block:FX") == "fx_continuous"

    friday = int(datetime(2026, 9, 4, 17, tzinfo=timezone.utc).timestamp())
    due = md.due_moment({"asset_id": "block:equity", "hour_utc": friday}, "settled")
    assert due is not None
    # The next day US equities trade after that Friday is Tuesday the 8th: the
    # Monday is Labor Day. On the round-the-clock calendar it would have been
    # Saturday, three days and one closed market too early.
    assert datetime.fromtimestamp(due, tz=timezone.utc).date() == date(2026, 9, 8)


def test_the_currency_block_names_the_dollar_rather_than_a_sign():
    # Its members are oriented before the median, so the figure is a statement
    # about the DOLLAR while the movers under it are quoted the way a chart
    # quotes them. "+0.88%" above "EUR/USD -1.05%" reads as a contradiction and
    # is not one.
    text = md.describe(block_event(
        block="FX", asset_id="block:FX", r=0.0088, e_resid=0.0088, sigma_lt=0.0008,
        leaders="USD/CHF +1.31%, EUR/USD -1.22%"), LABELS)

    # And the block is named for what its members have in common rather than
    # for the members themselves: "currencies" made a number in ONE pair's own
    # direction look like a claim about all of them at once.
    assert "<b>The dollar block</b>" in text
    assert "the dollar gained 0.88% against the typical pair" in text
    assert "+0.88%" not in text

    fell = md.describe(block_event(
        block="FX", asset_id="block:FX", r=-0.0088, e_resid=-0.0088,
        sigma_lt=0.0008, leaders="EUR/USD +1.22%"), LABELS)
    assert "the dollar lost 0.88% against the typical pair" in fell


def test_a_block_ping_carries_the_black_mark_too():
    # The ping and the note row arrive one after the other and have to agree on
    # what kind of thing moved. blocks.events_frame only emits the push tiers
    # today, so this path is not reachable from the pipeline - it is here so
    # that relaxing that filter cannot silently produce an unmarked ping.
    ping = md.format_ping(block_event(tier="noticeable", channel="digest",
                                      block="agriculture",
                                      asset_id="block:agriculture", r=-0.0072), {})
    assert ping == (f"{md.BLOCK_MARK}{md.TIER_EMOJI['noticeable']} "
                    f"<b>Agriculture</b> -0.72%")


def test_a_block_routes_on_its_tier_exactly_as_an_instrument_does():
    # There is no separate block channel and there should not be: a block is the
    # same four rarities read one level up, so the black mark is the ONLY thing
    # that distinguishes it in delivery.
    frame = pd.DataFrame([
        {"asset_id": "block:equity", "tier": "extreme", "hour_utc": int(NOW.timestamp())},
        {"asset_id": "block:rates", "tier": "major", "hour_utc": int(NOW.timestamp())},
        {"asset_id": "block:energy", "tier": "high", "hour_utc": int(NOW.timestamp())},
    ])
    frame["tier"] = frame["tier"].astype("string")
    assert list(routing.route(frame)["channel"]) == [
        routing.PUSH, routing.PUSH, routing.DIGEST]


def test_a_block_headline_starts_with_a_capital():
    text = md.describe(block_event(block="precious_metals",
                                   asset_id="block:precious_metals"), LABELS)
    assert "<b>Precious metals</b>" in text


# --- the regime the move happened in ----------------------------------------

def vix_frame(rows, spikes=()):
    """rows: (observed date, known date, close). `spikes` indexes into rows."""
    frame = pd.DataFrame({
        "day": [int(datetime(*d, tzinfo=timezone.utc).timestamp()) for d, _, _ in rows],
        "available_at": [int(datetime(*k, tzinfo=timezone.utc).timestamp())
                         for _, k, _ in rows],
        "close": [c for _, _, c in rows],
    })
    frame["is_spike"] = [i in spikes for i in range(len(rows))]
    return frame


def use_vix(monkeypatch, frame):
    monkeypatch.setattr(md, "_vix_scored", lambda: frame)


def test_the_comparison_names_the_close_it_compares_against(monkeypatch):
    # "a week before" was true to the intent and not to the number: the line
    # takes the most recent reading at least a week back, which lands on a
    # different day depending on where weekends and holidays fall, and a reader
    # could not tell nine days from seven.
    use_vix(monkeypatch, vix_frame([
        ((2026, 9, 1), (2026, 9, 2, 15), 14.32),
        ((2026, 9, 4), (2026, 9, 7, 15), 15.10),
        ((2026, 9, 10), (2026, 9, 11, 15), 17.84),
    ]))
    at = int(datetime(2026, 9, 12, 9, tzinfo=timezone.utc).timestamp())
    line = md.vix_context(at).splitlines()[1]
    # nine days back, not seven - and it says so instead of rounding to a week
    assert "up from 14.32 at the 1 Sep close" in line


def test_the_comparison_says_which_way_it_moved(monkeypatch):
    rows = [((2026, 9, 1), (2026, 9, 2, 15), 20.00),
            ((2026, 9, 10), (2026, 9, 11, 15), 14.00)]
    use_vix(monkeypatch, vix_frame(rows))
    at = int(datetime(2026, 9, 12, 9, tzinfo=timezone.utc).timestamp())
    assert "down from 20.00 at the 1 Sep close" in md.vix_context(at)

    rows[1] = ((2026, 9, 10), (2026, 9, 11, 15), 20.05)   # inside VIX_FLAT
    use_vix(monkeypatch, vix_frame(rows))
    assert "level with 20.00 at the 1 Sep close" in md.vix_context(at)


def test_the_gauge_moves_on_as_soon_as_a_reading_is_published(monkeypatch):
    # A live note is re-rendered every run, so it must not sit on a stale gauge:
    # the moment FRED publishes the next close, the line follows it.
    use_vix(monkeypatch, vix_frame([
        ((2026, 9, 1), (2026, 9, 2, 15), 14.32),
        ((2026, 9, 9), (2026, 9, 10, 15), 16.46),
        ((2026, 9, 10), (2026, 9, 11, 15), 17.84),
    ]))
    before = int(datetime(2026, 9, 11, 10, tzinfo=timezone.utc).timestamp())
    after = int(datetime(2026, 9, 11, 16, tzinfo=timezone.utc).timestamp())
    assert "16.46 at the 9 Sep close" in md.vix_context(before)
    assert "17.84 at the 10 Sep close" in md.vix_context(after)


def test_the_regime_line_never_quotes_a_reading_that_did_not_exist_yet(monkeypatch):
    # FRED publishes VIX one to two business days late. A message about Monday's
    # move that quoted Monday's close would be reading a number the system could
    # not have had, which is the one thing a replayable record must not do.
    use_vix(monkeypatch, vix_frame([
        ((2020, 3, 9), (2020, 3, 10, 15), 54.46),
        ((2020, 3, 10), (2020, 3, 11, 15), 47.30),
        ((2020, 3, 11), (2020, 3, 12, 15), 53.90),
        ((2020, 3, 12), (2020, 3, 13, 15), 75.47),
    ]))
    text = md.vix_context(int(datetime(2020, 3, 12, 19, tzinfo=timezone.utc).timestamp()))

    assert "53.90" in text and "11 Mar" in text
    assert "75.47" not in text


def test_the_regime_line_is_silent_before_any_reading_is_known(monkeypatch):
    use_vix(monkeypatch, vix_frame([((2026, 9, 3), (2026, 9, 4, 15), 14.32)]))
    assert md.vix_context(int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())) == ""


def test_the_level_is_placed_in_its_own_history(monkeypatch):
    # 16 and 54 are both just numbers until one of them is "calmer than three
    # days in five" and the other "higher than all but one day in a hundred".
    rows = [((2020, 1, d // 24 + 1, d % 24), (2020, 2, 1, 15), 10.0 + d)
            for d in range(0, 40)]
    use_vix(monkeypatch, vix_frame(rows))
    at = int(datetime(2020, 3, 1, tzinfo=timezone.utc).timestamp())
    assert "higher than 100% of days" not in md.vix_context(at)
    assert "the highest it has been since" in md.vix_context(at)


def test_a_stress_episode_is_named_while_it_is_running_and_not_after(monkeypatch):
    # This is the multiplier finally becoming visible: it raises how seriously
    # clustered moves are taken for twenty-four REFERENCE hours after a spike,
    # and until now it has fed a channel nobody reads.
    use_vix(monkeypatch, vix_frame([
        ((2020, 2, 24), (2020, 2, 25, 15), 25.0),
        ((2020, 2, 27), (2020, 2, 28, 15), 39.16),
    ], spikes=(1,)))

    inside = md.vix_context(int(datetime(2020, 2, 28, 18, tzinfo=timezone.utc).timestamp()))
    assert "stress episode" in inside and "28 Feb" in inside

    later = md.vix_context(int(datetime(2020, 3, 20, 18, tzinfo=timezone.utc).timestamp()))
    assert "39.16" in later                      # still the latest known reading
    assert "stress episode" not in later         # but the window closed long ago


def test_a_push_carries_the_regime_and_so_does_the_note(monkeypatch):
    use_vix(monkeypatch, vix_frame([((2026, 9, 3), (2026, 9, 4, 15), 14.32)]))
    later = event(hour_utc=int(datetime(2026, 9, 8, 14, tzinfo=timezone.utc).timestamp()))
    push = md.format_push(later, LABELS)
    assert "Fear gauge" in push and "14.32" in push

    window = (int(datetime(2026, 9, 8, 9, tzinfo=timezone.utc).timestamp()),
              int(datetime(2026, 9, 11, 9, tzinfo=timezone.utc).timestamp()))
    note = md.format_digest([event()], LABELS, window,
                            now=datetime(2026, 9, 9, 12, tzinfo=timezone.utc))
    assert "Fear gauge" in note[0]


# --- the throwaway ping ----------------------------------------------------
#
# A digest row is written the hour its move is found, but a note stays silent
# because Telegram does not notify on an edit. The ping is the buzz that says a
# row appeared; it is deleted when the next note opens, so the record left
# behind is a clean run of notes.

class Deleted:
    """Stands in for notifier.delete_telegram_message."""

    def __init__(self, refuse=()):
        self.ids, self.refuse = [], set(refuse)

    def __call__(self, token, chat, message_id, *a, **k):
        self.ids.append(int(message_id))
        return int(message_id) not in self.refuse


def test_a_digest_row_buzzes_once_and_says_almost_nothing(monkeypatch, sender):
    row = event(event_id="p1", tier="noticeable", channel="digest",
                digest_slot=int(NOW.timestamp()) + 3 * HOUR)
    _, state = deliver(monkeypatch, [row])

    pings = [t for t in sender.texts if t.startswith("⬜")]
    assert pings == ["⬜ <b>Gold</b> +2.10%"]
    assert state[md.STATE_KEY][md.PINGS] == {"p1": 1}

    # And not again on the next run: the buzz is once per move, not per hour.
    before = len(sender.texts)
    deliver(monkeypatch, [row], state=state)
    assert [t for t in sender.texts[before:] if t.startswith("⬜")] == []


def test_a_push_tier_never_buzzes_even_while_it_sits_in_the_digest():
    # A once-a-year move waits in the digest until its retention is known and is
    # promoted six bars later. Keyed on the channel it would buzz and THEN push,
    # interrupting twice for one move.
    waiting = event(tier="major", channel="digest")
    assert md.pending_pings([waiting], {}, NOW) == []


def test_the_pings_are_cleared_as_the_next_note_opens(monkeypatch, sender):
    killer = Deleted()
    monkeypatch.setattr("price_monitor.notifier.delete_telegram_message", killer)
    store = {md.PINGS: {"old1": 11, "old2": 12}}
    assert md.sweep_pings(cfg(), store) == 2
    assert killer.ids == [11, 12]
    assert store[md.PINGS] == {}


def test_a_ping_telegram_refuses_to_delete_is_dropped_anyway(monkeypatch):
    # Outside a channel a bot may only delete its own message for 48 hours. One
    # it will not delete now it will not delete later, and retrying it every
    # hour for ever is a leak wearing the clothes of diligence.
    killer = Deleted(refuse=[12])
    monkeypatch.setattr("price_monitor.notifier.delete_telegram_message", killer)
    store = {md.PINGS: {"a": 11, "b": 12}}
    assert md.sweep_pings(cfg(), store) == 1
    assert store[md.PINGS] == {}


def test_nothing_stale_is_ever_buzzed(monkeypatch):
    # Without this the first run after the mute comes off buzzes once for every
    # row in the history instead of for what just happened.
    old = event(event_id="ancient", tier="noticeable", channel="digest",
                hour_utc=int(NOW.timestamp()) - 40 * 24 * HOUR)
    assert md.pending_pings([old], {}, NOW) == []


def test_the_rarity_is_said_of_the_thing_its_ladder_actually_ranks():
    # Not cosmetic. The abnormal ladder ranks what is LEFT after the block is
    # taken out, so its return period belongs on the "on its own" line: said of
    # the whole move it would claim the instrument had not moved this far in
    # years when its block may have carried it there last week. The absolute
    # ladder ranks the move itself, so there it belongs to the move.
    row = dict(dated(), r=0.0700, e_resid=0.0100, co_block=0.0600, block="equity")

    abnormal = md._split_lines(row, "S&P 500", "major", "abnormal")
    assert "the biggest since March 2020" in abnormal[-1]
    assert "move on its own" in abnormal[-1]
    assert not any("the biggest move since" in line for line in abnormal)

    absolute = md._split_lines(row, "S&P 500", "major", "absolute")
    assert absolute[0] == "the biggest move since March 2020"
    assert "biggest" not in absolute[-1]


def test_a_row_with_no_split_still_says_how_rare_it_was():
    # No block to hang it on, so it is said as a sentence - and by the same
    # function the headline uses, because "the biggest move" and "the biggest
    # move of its own" are different claims and only one is true of a channel.
    lines = md._split_lines(dict(dated(), r=0.02, e_resid=None),
                            "Gold", "extreme", "abnormal")
    assert lines == ["the biggest move of its own since March 2020"]


def header_for(y, m, d):
    opens = int(datetime(y, m, d, 0, 5, tzinfo=timezone.utc).timestamp())
    return md.format_digest([], LABELS, routing.digest_window(opens),
                            None, NOW)[0].splitlines()[0]


def test_the_header_names_the_last_day_the_note_can_hold_an_hour_of():
    # A note runs to the instant the next one opens, and that instant is 00:05 -
    # so the workweek note reaches into Saturday by five minutes and was headed
    # "Mon 14 to Sat 19", handing Saturday to a note that carries none of it.
    # The note is a list of hourly bars: a five-minute sliver cannot hold one.
    assert "Mon 14 to Fri 18 September" in header_for(2026, 9, 14)
    assert "Sat 19 to Sun 20 September" in header_for(2026, 9, 19)


def test_the_note_names_the_month_only_when_it_crosses_one():
    # A workweek note falls inside one month five times in six, and naming it
    # twice in five words is noise. The sixth is the one that matters: a header
    # reading "Mon 27 to Fri 1 November" would leave the reader to work out
    # which month the 27th was, and the answer is the other one.
    inside = header_for(2026, 3, 9)
    assert "Mon 9 to Fri 13 March" in inside
    assert inside.count("March") == 1

    across = header_for(2026, 3, 30)     # Monday 30 March into April
    assert "Mon 30 March to Fri 3 April" in across


def test_the_note_runs_in_time_order_across_all_its_parts():
    # A long note is cut into several messages. Sorting each part on its own
    # would restart the clock at every cut, so the rows are ordered once and the
    # cut falls wherever the character budget runs out.
    rows = [event(event_id=f"d{i}", channel="digest", tier="noticeable",
                  hour_utc=SLOT + i * HOUR, asset_id="twelvedata:GLD")
            for i in range(60)]
    texts = md.format_digest(rows, LABELS, (SLOT, SLOT + 200 * HOUR), None, NOW)
    assert len(texts) > 1, "the fixture must be long enough to split"

    stamps = []
    for part in texts:
        for line in part.split("\n"):
            if line.startswith(md.TIME_EMOJI):
                stamps.append(line)
    assert stamps == sorted(stamps), "the hours must ascend across the parts"
    assert len(stamps) == len(rows)


def test_two_moves_in_one_hour_put_the_rarer_first():
    # The one case time cannot separate.
    same = SLOT + 5 * HOUR
    rows = [event(event_id="mild", channel="digest", tier="noticeable",
                  hour_utc=same, asset_id="twelvedata:GLD"),
            event(event_id="rare", channel="digest", tier="high",
                  hour_utc=same, asset_id="coinbase:BTC-USD")]
    text = md.format_digest(rows, LABELS, (SLOT, SLOT + 200 * HOUR), None, NOW)[0]
    assert text.index("Bitcoin") < text.index("Gold")

    # and reversing the input does not change the answer
    text = md.format_digest(rows[::-1], LABELS, (SLOT, SLOT + 200 * HOUR), None, NOW)[0]
    assert text.index("Bitcoin") < text.index("Gold")


def test_a_note_never_un_says_what_the_reader_already_read(monkeypatch, sender, editor):
    # It is rendered whole from the events table every run, which is what lets a
    # late event appear and a recomputed-away one go. That is right for one row
    # among several and wrong for ALL of them: a retuned ladder can empty a note
    # the reader has read and been pinged about, which reads as the bot
    # forgetting rather than correcting. Seen live - a note showing two moves
    # went back to "Nothing so far" the run after the rungs changed.
    row = event(event_id="d1", channel="digest", tier="noticeable",
                hour_utc=SLOT + 2 * HOUR)
    _, state = deliver(monkeypatch, [row])
    assert "Gold" in notes(sender)[0]

    # the recompute now produces nothing at all for that period
    before = len(editor.calls)
    deliver(monkeypatch, [], state=state)
    assert len(editor.calls) == before, "the note was edited down to nothing"


def test_a_note_still_drops_one_row_of_several(monkeypatch, sender, editor):
    # Only the all-or-nothing case is held. A genuine recompute that removes one
    # row of two still shows, because the note is not being emptied.
    rows = [event(event_id="d1", channel="digest", tier="noticeable",
                  hour_utc=SLOT + HOUR, asset_id="twelvedata:GLD"),
            event(event_id="d2", channel="digest", tier="noticeable",
                  hour_utc=SLOT + 2 * HOUR, asset_id="coinbase:BTC-USD")]
    _, state = deliver(monkeypatch, rows)
    assert "Bitcoin" in notes(sender)[0]

    deliver(monkeypatch, rows[:1], state=state)
    assert editor.calls, "the surviving row should have been rewritten"
    assert "Bitcoin" not in editor.calls[-1][1]


def test_notes_never_overlap_even_when_the_schedule_moves(monkeypatch, sender):
    # Seen live, moving the notes from Tuesday/Friday to Monday/Saturday: a note
    # opened under the old boundaries was still running when the new one opened
    # inside it, and carried_from - looking for the last end that had been
    # REACHED - skipped past the open note to the one before it. Both then
    # claimed the same hours, and the reader got two notes listing one move.
    old_slot = SLOT - 3 * 24 * HOUR
    digests = {
        str(old_slot - 3 * 24 * HOUR): {"ids": [1], "hashes": ["a"],
                                        "from": old_slot - 3 * 24 * HOUR,
                                        "to": old_slot},
        str(old_slot): {"ids": [2], "hashes": ["b"], "from": old_slot,
                        "to": SLOT + 24 * HOUR},          # still open past SLOT
        str(SLOT): {"ids": [3], "hashes": ["c"], "from": old_slot,  # overlaps it
                    "to": routing.next_digest_slot(SLOT)},
    }
    assert md.tidy_windows(digests)

    windows = [md.note_window(int(k), v) for k, v in
               sorted(digests.items(), key=lambda kv: int(kv[0]))]
    for earlier, later in zip(windows, windows[1:]):
        assert earlier[1] == later[0], "notes must meet exactly, never overlap"


def test_straightening_a_window_drops_the_rows_it_no_longer_owns(monkeypatch):
    # The rows marker guards a note against being emptied by a change to what
    # qualifies. A straightened window is different: those rows belong to the
    # note beside it now, and holding them would print the move twice.
    digests = {
        str(SLOT - 3 * 24 * HOUR): {"ids": [1], "hashes": ["a"], "rows": 2,
                                    "from": SLOT - 3 * 24 * HOUR,
                                    "to": SLOT + 24 * HOUR},
        str(SLOT): {"ids": [2], "hashes": ["b"], "rows": 2,
                    "from": SLOT - 3 * 24 * HOUR,
                    "to": routing.next_digest_slot(SLOT)},
    }
    md.tidy_windows(digests)
    assert "rows" not in digests[str(SLOT)]
    assert "rows" not in digests[str(SLOT - 3 * 24 * HOUR)]


def test_tidying_leaves_a_healthy_set_of_notes_alone():
    slots = [SLOT - 3 * 24 * HOUR, SLOT]
    digests = {str(slots[0]): {"ids": [1], "hashes": ["a"], "rows": 2,
                               "from": slots[0], "to": slots[1]},
               str(slots[1]): {"ids": [2], "hashes": ["b"], "rows": 1,
                               "from": slots[1],
                               "to": routing.next_digest_slot(slots[1])}}
    assert md.tidy_windows(digests) == 0
    assert digests[str(slots[1])]["rows"] == 1      # the guard survives
