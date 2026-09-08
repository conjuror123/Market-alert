import sys
from datetime import datetime, timedelta, timezone

import pytest

from price_monitor import weekly_digest
from price_monitor.config import Config
from price_monitor.notifier import TelegramError

# Friday 12:30 Asia/Jerusalem. The digest goes out in the same run as the price
# note and immediately before it, so the note - which keeps changing for the
# next three days - is the last message in the chat.
FRIDAY_NOON_ISRAEL_UTC = datetime(2026, 8, 28, 9, 30, tzinfo=timezone.utc)
FRIDAY_AFTERNOON_ISRAEL_UTC = datetime(2026, 8, 28, 11, 30, tzinfo=timezone.utc)
FRIDAY_EVENING_ISRAEL_UTC = datetime(2026, 8, 28, 18, 0, tzinfo=timezone.utc)
SATURDAY_NOON_ISRAEL_UTC = datetime(2026, 8, 29, 9, 30, tzinfo=timezone.utc)
MONDAY_NOON_ISRAEL_UTC = datetime(2026, 8, 31, 9, 30, tzinfo=timezone.utc)

# Inside the seven days after FRIDAY_NOON, except the last, which is not.
RAW_EVENTS = [
    {"title": "Non-Farm Payrolls", "country": "USD", "date": "2026-09-04T08:30:00-04:00",
     "impact": "High", "forecast": "", "previous": ""},
    {"title": "Retail Sales", "country": "EUR", "date": "2026-09-02T04:00:00-04:00",
     "impact": "Medium", "forecast": "", "previous": ""},
    {"title": "Minor Data Point", "country": "GBP", "date": "2026-09-03T04:00:00-04:00",
     "impact": "Low", "forecast": "", "previous": ""},
    {"title": "Bank Holiday", "country": "All", "date": "2026-08-31T00:00:00-04:00",
     "impact": "Holiday", "forecast": "", "previous": ""},
]


@pytest.fixture(autouse=True)
def no_network_backfill(request, monkeypatch):
    """Reading the monthly pages hits ForexFactory. In the tests it is stubbed
    everywhere except the ones marked real_backfill - refresh_months' own tests:
    otherwise every digest test would make three network requests."""
    if request.node.get_closest_marker("real_backfill"):
        return
    monkeypatch.setattr(weekly_digest, "refresh_months",
                        lambda path, session=None, now=None, through=None: 0)


def make_config(tmp_path):
    return Config(
        telegram_bot_token="tok", telegram_chat_id="chat",
        calendar_dir=str(tmp_path / "economic_calendar"),
    )


def test_the_digest_goes_out_on_friday_at_noon():
    # Friday, so that the price note - sent immediately after it in the same run
    # - is the last message in the chat.
    assert weekly_digest._is_digest_window(FRIDAY_NOON_ISRAEL_UTC) is True
    assert weekly_digest._is_digest_window(SATURDAY_NOON_ISRAEL_UTC) is False
    assert weekly_digest._is_digest_window(MONDAY_NOON_ISRAEL_UTC) is False


def test_a_missed_run_at_noon_does_not_cost_the_week(monkeypatch):
    # The same three hours of grace the price note has, and for the same reason:
    # the trigger is an external service, and both messages have to keep landing
    # in one run so their order never inverts.
    assert weekly_digest._is_digest_window(FRIDAY_AFTERNOON_ISRAEL_UTC) is True
    assert weekly_digest._is_digest_window(FRIDAY_EVENING_ISRAEL_UTC) is False


def test_the_week_key_is_the_friday_it_belongs_to():
    # It has to de-duplicate the grace window: several runs qualify and only the
    # first may send.
    assert weekly_digest._week_identifier(FRIDAY_NOON_ISRAEL_UTC) == "2026-08-28"
    assert (weekly_digest._week_identifier(FRIDAY_AFTERNOON_ISRAEL_UTC)
            == weekly_digest._week_identifier(FRIDAY_NOON_ISRAEL_UTC))


def test_the_grace_window_does_not_send_twice(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    state = {}
    sent = []
    monkeypatch.setattr(weekly_digest.economic_calendar, "fetch_calendar",
                        lambda session=None: RAW_EVENTS)
    monkeypatch.setattr(weekly_digest, "send_telegram_message",
                        lambda *a, **k: sent.append(a[2]) or 1)

    assert weekly_digest.maybe_send_weekly_digest(
        cfg, state, session=None, now=FRIDAY_NOON_ISRAEL_UTC) is True
    assert weekly_digest.maybe_send_weekly_digest(
        cfg, state, session=None, now=FRIDAY_AFTERNOON_ISRAEL_UTC) is False
    assert len(sent) == 1


def test_the_week_is_read_from_the_archive_not_from_the_feed(tmp_path, monkeypatch):
    # On a Friday the live feed still serves the week that is ending, so it
    # cannot be the source. The archive is, and it reaches weeks ahead because
    # the monthly pages are read into it.
    cfg = make_config(tmp_path)
    path = weekly_digest.economic_calendar.store_path(cfg.calendar_dir)
    weekly_digest.economic_calendar.merge_events(path, RAW_EVENTS)

    sent = []
    monkeypatch.setattr(weekly_digest.economic_calendar, "fetch_calendar",
                        lambda session=None: [])
    monkeypatch.setattr(weekly_digest, "send_telegram_message",
                        lambda *a, **k: sent.append(a[2]) or 1)

    assert weekly_digest.maybe_send_weekly_digest(
        cfg, {}, session=None, now=FRIDAY_NOON_ISRAEL_UTC) is True
    assert "Non-Farm Payrolls" in sent[0]


def test_a_feed_that_will_not_load_does_not_cost_the_digest(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    path = weekly_digest.economic_calendar.store_path(cfg.calendar_dir)
    weekly_digest.economic_calendar.merge_events(path, RAW_EVENTS)

    def failing_fetch(session=None):
        raise weekly_digest.economic_calendar.CalendarError("boom")

    sent = []
    monkeypatch.setattr(weekly_digest.economic_calendar, "fetch_calendar", failing_fetch)
    monkeypatch.setattr(weekly_digest, "send_telegram_message",
                        lambda *a, **k: sent.append(a[2]) or 1)

    assert weekly_digest.maybe_send_weekly_digest(
        cfg, {}, session=None, now=FRIDAY_NOON_ISRAEL_UTC) is True
    assert len(sent) == 1


def test_an_archive_that_stops_short_holds_the_digest_back(tmp_path, monkeypatch):
    # It cannot tell "nothing is scheduled" from "nothing was imported", and only
    # one of those is safe to print under the heading "for the week".
    cfg = make_config(tmp_path)
    path = weekly_digest.economic_calendar.store_path(cfg.calendar_dir)
    weekly_digest.economic_calendar.merge_events(
        path, [dict(RAW_EVENTS[0], date="2026-08-29T08:30:00+00:00")])

    monkeypatch.setattr(weekly_digest.economic_calendar, "fetch_calendar",
                        lambda session=None: [])
    monkeypatch.setattr(weekly_digest, "send_telegram_message", lambda *a, **k: 1)

    state = {}
    assert weekly_digest.maybe_send_weekly_digest(
        cfg, state, session=None, now=FRIDAY_NOON_ISRAEL_UTC) is False
    assert weekly_digest._STATE_KEY not in state


def test_events_outside_the_coming_week_are_not_listed(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    path = weekly_digest.economic_calendar.store_path(cfg.calendar_dir)
    weekly_digest.economic_calendar.merge_events(path, RAW_EVENTS + [
        {"title": "Far Future Rate Decision", "country": "USD",
         "date": "2026-09-20T14:00:00+00:00", "impact": "High",
         "forecast": "", "previous": ""},
        {"title": "Last Month Payrolls", "country": "USD",
         "date": "2026-08-07T12:30:00+00:00", "impact": "High",
         "forecast": "", "previous": ""},
    ])
    sent = []
    monkeypatch.setattr(weekly_digest.economic_calendar, "fetch_calendar",
                        lambda session=None: [])
    monkeypatch.setattr(weekly_digest, "send_telegram_message",
                        lambda *a, **k: sent.append(a[2]) or 1)

    weekly_digest.maybe_send_weekly_digest(
        cfg, {}, session=None, now=FRIDAY_NOON_ISRAEL_UTC)
    text = "\n".join(sent)
    assert "Non-Farm Payrolls" in text
    assert "Far Future Rate Decision" not in text
    assert "Last Month Payrolls" not in text


def test_the_header_states_the_window_asked_for(tmp_path, monkeypatch):
    # Not the span of the events that happen to be in it: a quiet end to the week
    # would otherwise narrow the claim the message is making.
    start, end = weekly_digest.coming_week(FRIDAY_NOON_ISRAEL_UTC)
    text = weekly_digest.format_digest(
        [e for e in RAW_EVENTS if e["impact"] in ("Medium", "High")], start, end)[0]
    assert "28.08 — 04.09" in text


def test_format_digest_excludes_low_and_holiday_and_sorts_by_time():
    digest_events = [e for e in RAW_EVENTS if e["impact"] in ("Medium", "High")]
    text = "\n".join(weekly_digest.format_digest(digest_events))

    assert "Non-Farm Payrolls" in text
    assert "Retail Sales" in text
    assert "Minor Data Point" not in text
    assert "Bank Holiday" not in text
    assert text.index("Retail Sales") < text.index("Non-Farm Payrolls")  # earlier date first


def test_format_digest_handles_no_events():
    messages = weekly_digest.format_digest([])
    assert len(messages) == 1
    assert "No Medium/High impact events" in messages[0]


def test_format_digest_shows_every_field_of_an_event():
    # This is what the digest was rewritten for: the impact tag says the release
    # matters, while the forecast and previous value say what is expected of it.
    event = {"title": "CPI m/m", "country": "USD", "impact": "High",
             "date": "2026-09-04T08:30:00+00:00",
             "actual": "0.4%", "forecast": "0.3%", "previous": "0.2%"}
    text = "\n".join(weekly_digest.format_digest([event]))
    assert "actual 0.4%" in text and "forecast 0.3%" in text and "prev. 0.2%" in text


def test_empty_fields_are_skipped_not_printed_as_dashes():
    # The feed has a forecast for roughly 70% of events: a line of dashes would
    # say only that the source stayed silent.
    event = {"title": "Bank Holiday Speech", "country": "GBP", "impact": "Medium",
             "date": "2026-09-04T08:30:00+00:00",
             "actual": "", "forecast": "", "previous": "1.0%"}
    text = "\n".join(weekly_digest.format_digest([event]))
    assert "prev. 1.0%" in text
    assert "forecast" not in text and "actual" not in text


def test_event_titles_are_escaped_for_html():
    # The message goes out with parse_mode=HTML. One unescaped "S&P" would be
    # enough for Telegram to reject the whole digest.
    event = {"title": "S&P Global PMI", "country": "USD", "impact": "High",
             "date": "2026-09-04T08:30:00+00:00",
             "actual": "", "forecast": "<50", "previous": ""}
    text = "\n".join(weekly_digest.format_digest([event]))
    assert "S&amp;P Global PMI" in text
    assert "&lt;50" in text


def test_a_long_week_is_split_at_day_boundaries():
    # Telegram rejects a message longer than 4096 characters outright rather
    # than truncating it - so without splitting, a long week would never arrive.
    events = [{"title": f"A very long name for indicator number {i:03d}",
               "country": "USD", "impact": "Medium",
               "date": f"2026-09-{1 + i % 4:02d}T{i % 24:02d}:{i % 60:02d}:00+00:00",
               "actual": "", "forecast": "1.0%", "previous": "0.9%"}
              for i in range(120)]
    messages = weekly_digest.format_digest(events)
    assert len(messages) > 1
    assert all(len(m) <= weekly_digest._MESSAGE_LIMIT for m in messages)
    # A day's header and its events must not end up in different messages.
    for message in messages[1:]:
        assert message.splitlines()[0].startswith("📅")


def test_maybe_send_weekly_digest_noops_outside_window(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    state = {}
    monkeypatch.setattr(weekly_digest.economic_calendar, "fetch_calendar", lambda session=None: RAW_EVENTS)
    monkeypatch.setattr(weekly_digest, "send_telegram_message", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("should not send outside the digest window")))

    sent = weekly_digest.maybe_send_weekly_digest(cfg, state, session=None, now=MONDAY_NOON_ISRAEL_UTC)

    assert sent is False
    assert weekly_digest._STATE_KEY not in state


def test_maybe_send_weekly_digest_sends_and_records_state(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    state = {}
    sent_texts = []
    monkeypatch.setattr(weekly_digest.economic_calendar, "fetch_calendar", lambda session=None: RAW_EVENTS)
    monkeypatch.setattr(weekly_digest, "send_telegram_message", lambda *a, **k: sent_texts.append(a[2]) or 1)

    sent = weekly_digest.maybe_send_weekly_digest(cfg, state, session=None, now=FRIDAY_NOON_ISRAEL_UTC)

    assert sent is True
    assert len(sent_texts) == 1
    assert "Non-Farm Payrolls" in sent_texts[0]
    assert weekly_digest._STATE_KEY in state


def test_the_closing_friday_of_the_window_is_inside_it(tmp_path, monkeypatch):
    # Seven days to the minute would end at noon next Friday, and the American
    # payrolls print lands at 12:30 UTC on the first Friday of the month - just
    # outside every window, announced three hours ahead by the next digest.
    start, end = weekly_digest.coming_week(FRIDAY_NOON_ISRAEL_UTC)
    payrolls = datetime(2026, 9, 4, 12, 30, tzinfo=timezone.utc)
    assert start < payrolls < end


def test_maybe_send_weekly_digest_persists_every_impact_level_to_local_store(tmp_path, monkeypatch):
    # The archive keeps every impact level now (see economic_calendar's
    # module docstring) - Low/Holiday events still don't reach the Telegram
    # digest text (see _DIGEST_IMPACTS), but they are written to the store.
    cfg = make_config(tmp_path)
    monkeypatch.setattr(weekly_digest.economic_calendar, "fetch_calendar", lambda session=None: RAW_EVENTS)
    monkeypatch.setattr(weekly_digest, "send_telegram_message", lambda *a, **k: 1)

    weekly_digest.maybe_send_weekly_digest(cfg, {}, session=None, now=FRIDAY_NOON_ISRAEL_UTC)

    stored = weekly_digest.economic_calendar.load_events(
        weekly_digest.economic_calendar.store_path(cfg.calendar_dir))
    assert {e["title"] for e in stored} == {
        "Non-Farm Payrolls", "Retail Sales", "Minor Data Point", "Bank Holiday"}


def test_maybe_send_weekly_digest_does_not_resend_the_same_week(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    state = {}
    calls = []
    monkeypatch.setattr(weekly_digest.economic_calendar, "fetch_calendar", lambda session=None: RAW_EVENTS)
    monkeypatch.setattr(weekly_digest, "send_telegram_message", lambda *a, **k: calls.append(1) or 1)

    weekly_digest.maybe_send_weekly_digest(cfg, state, session=None, now=FRIDAY_NOON_ISRAEL_UTC)
    second = weekly_digest.maybe_send_weekly_digest(cfg, state, session=None, now=FRIDAY_NOON_ISRAEL_UTC)

    assert second is False
    assert len(calls) == 1


def test_an_empty_archive_and_an_unreachable_feed_send_nothing(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)

    def failing_fetch(session=None):
        raise weekly_digest.economic_calendar.CalendarError("boom")

    monkeypatch.setattr(weekly_digest.economic_calendar, "fetch_calendar", failing_fetch)
    assert weekly_digest.maybe_send_weekly_digest(
        cfg, {}, session=None, now=FRIDAY_NOON_ISRAEL_UTC) is False


def test_maybe_send_weekly_digest_returns_false_on_send_failure(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    state = {}
    monkeypatch.setattr(weekly_digest.economic_calendar, "fetch_calendar", lambda session=None: RAW_EVENTS)

    def failing_send(*a, **k):
        raise TelegramError("boom")

    monkeypatch.setattr(weekly_digest, "send_telegram_message", failing_send)

    assert weekly_digest.maybe_send_weekly_digest(cfg, state, session=None, now=FRIDAY_NOON_ISRAEL_UTC) is False
    assert weekly_digest._STATE_KEY not in state


def test_main_requires_the_force_flag(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["weekly_digest.py"])
    with pytest.raises(SystemExit):
        weekly_digest.main()


def test_main_force_sends_immediately_regardless_of_day(tmp_path, monkeypatch):
    """--force is meant for manual testing outside the Friday window - it
    should send right away, with no day/time gating and no state.json
    involvement at all (main() never even receives a state dict)."""
    cfg = make_config(tmp_path)
    sent_texts = []
    ahead = [dict(e, date=(datetime.now(timezone.utc) + timedelta(days=d)).isoformat())
             for d, e in zip((1, 3, 5, 8), RAW_EVENTS)]
    monkeypatch.setattr(weekly_digest, "load_config", lambda: cfg)
    monkeypatch.setattr(weekly_digest.economic_calendar, "fetch_calendar", lambda session=None: ahead)
    monkeypatch.setattr(weekly_digest, "send_telegram_message", lambda *a, **k: sent_texts.append(a[2]) or 1)
    monkeypatch.setattr(sys, "argv", ["weekly_digest.py", "--force"])

    exit_code = weekly_digest.main()

    assert exit_code == 0
    assert len(sent_texts) == 1
    assert "Non-Farm Payrolls" in sent_texts[0]


def test_main_force_returns_nonzero_on_failure(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)

    def failing_fetch(session=None):
        raise weekly_digest.economic_calendar.CalendarError("boom")

    monkeypatch.setattr(weekly_digest, "load_config", lambda: cfg)
    monkeypatch.setattr(weekly_digest.economic_calendar, "fetch_calendar", failing_fetch)
    monkeypatch.setattr(sys, "argv", ["weekly_digest.py", "--force"])

    assert weekly_digest.main() == 1


@pytest.mark.real_backfill
def test_refresh_months_reads_the_month_behind_and_the_month_ahead(tmp_path, monkeypatch):
    # The live weekly feed has no actual field at all, so the actual is read back
    # from the monthly pages. The previous month is needed for events at the end
    # of it whose actual is released in the new month.
    asked = []

    def fake_month(year, month, session=None):
        asked.append((year, month))
        return []

    monkeypatch.setattr(weekly_digest.economic_calendar,
                        "fetch_forexfactory_month", fake_month)
    path = str(tmp_path / "calendar.ndjson")
    weekly_digest.refresh_months(path, now=datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert asked == [(2026, 8), (2026, 9)]


@pytest.mark.real_backfill
def test_refresh_months_fills_the_actual_of_an_event_already_stored(tmp_path, monkeypatch):
    path = str(tmp_path / "calendar.ndjson")
    early = {"title": "CPI m/m", "country": "USD", "impact": "High",
             "date": "2026-09-04T12:30:00+00:00",
             "actual": "", "forecast": "0.3%", "previous": "0.2%"}
    weekly_digest.economic_calendar.merge_events(path, [early])

    published = dict(early, actual="0.4%")
    monkeypatch.setattr(weekly_digest.economic_calendar, "fetch_forexfactory_month",
                        lambda year, month, session=None: [published])
    changed = weekly_digest.refresh_months(
        path, now=datetime(2026, 9, 10, tzinfo=timezone.utc))

    stored = weekly_digest.economic_calendar.load_events(path)
    assert changed > 0
    assert len(stored) == 1, "the backfill must update the record, not create a second one"
    assert stored[0]["actual"] == "0.4%"


@pytest.mark.real_backfill
def test_backfill_failure_does_not_stop_the_digest(tmp_path, monkeypatch):
    # The backfill is not what the digest is run for: a page may fail to open,
    # and that is no reason to withhold the message.
    def boom(year, month, session=None):
        raise weekly_digest.economic_calendar.CalendarError("503")

    monkeypatch.setattr(weekly_digest.economic_calendar, "fetch_forexfactory_month", boom)
    assert weekly_digest.refresh_months(str(tmp_path / "calendar.ndjson")) == 0


def test_the_same_moment_in_two_notations_is_one_event(tmp_path):
    # The weekly feed writes "-04:00", the monthly page "+00:00". By string those
    # are different events, and the archive would collect every release twice -
    # exactly the breakage that forced the old archive to be thrown away.
    path = str(tmp_path / "calendar.ndjson")
    calendar = weekly_digest.economic_calendar
    calendar.merge_events(path, [{"title": "CPI m/m", "country": "USD", "impact": "High",
                                  "date": "2026-09-04T08:30:00-04:00",
                                  "actual": "", "forecast": "0.3%", "previous": "0.2%"}])
    calendar.merge_events(path, [{"title": "CPI m/m", "country": "USD", "impact": "High",
                                  "date": "2026-09-04T12:30:00+00:00",
                                  "actual": "0.4%", "forecast": "0.3%", "previous": "0.2%"}])
    stored = calendar.load_events(path)
    assert len(stored) == 1
    assert stored[0]["actual"] == "0.4%"
