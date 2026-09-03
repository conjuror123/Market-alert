import sys
from datetime import datetime, timezone

import pytest

from price_monitor import weekly_digest
from price_monitor.config import AssetConfig, Config
from price_monitor.notifier import TelegramError

SATURDAY_NOON_ISRAEL_UTC = datetime(2026, 8, 29, 9, 30, tzinfo=timezone.utc)  # Saturday 12:30 Asia/Jerusalem
SUNDAY_NOON_ISRAEL_UTC = datetime(2026, 8, 30, 9, 30, tzinfo=timezone.utc)
MONDAY_NOON_ISRAEL_UTC = datetime(2026, 8, 31, 9, 30, tzinfo=timezone.utc)
SATURDAY_EVENING_ISRAEL_UTC = datetime(2026, 8, 29, 18, 0, tzinfo=timezone.utc)

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
    """The actual backfill hits ForexFactory's monthly page. In the tests it is
    stubbed everywhere except the ones marked real_backfill - backfill_actuals'
    own tests: otherwise every digest test would make two network requests."""
    if request.node.get_closest_marker("real_backfill"):
        return
    monkeypatch.setattr(weekly_digest, "backfill_actuals",
                        lambda path, session=None, now=None: 0)


def make_config(tmp_path):
    return Config(
        assets=[AssetConfig(symbol="EUR/USD", source="twelvedata", label="EUR/USD")],
        telegram_bot_token="tok", telegram_chat_id="chat",
        calendar_dir=str(tmp_path / "economic_calendar"),
    )


def test_digest_window_is_saturday_or_sunday_noon_israel():
    # Two windows: where ForexFactory's week boundary falls has been confirmed
    # live only for Sunday, so Saturday tries and Sunday is the safety net.
    assert weekly_digest._is_digest_window(SATURDAY_NOON_ISRAEL_UTC) is True
    assert weekly_digest._is_digest_window(SUNDAY_NOON_ISRAEL_UTC) is True
    assert weekly_digest._is_digest_window(MONDAY_NOON_ISRAEL_UTC) is False
    assert weekly_digest._is_digest_window(SATURDAY_EVENING_ISRAEL_UTC) is False


def test_a_feed_of_the_ending_week_is_not_sent():
    # Sending out a list of what has already happened under the heading "for the
    # week" is not on - the next day's window sends the real coming week.
    past = datetime(2026, 9, 12, 9, 30, tzinfo=timezone.utc)
    assert weekly_digest._looks_forward(RAW_EVENTS, past) is False
    assert weekly_digest._looks_forward(RAW_EVENTS, SATURDAY_NOON_ISRAEL_UTC) is True


def test_the_week_key_comes_from_the_feed_not_from_today():
    # The dedup key comes from the feed, so Saturday and Sunday, having served
    # the same week, give one key and the digest goes out once.
    assert weekly_digest._week_identifier(RAW_EVENTS) == "2026-08-31"
    assert weekly_digest._week_identifier([]) == ""


def test_sunday_does_not_repeat_what_saturday_already_sent(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    state = {}
    sent = []
    monkeypatch.setattr(weekly_digest.economic_calendar, "fetch_calendar",
                        lambda session=None: RAW_EVENTS)
    monkeypatch.setattr(weekly_digest, "send_telegram_message",
                        lambda *a, **k: sent.append(a[2]) or 1)

    assert weekly_digest.maybe_send_weekly_digest(
        cfg, state, session=None, now=SATURDAY_NOON_ISRAEL_UTC) is True
    assert weekly_digest.maybe_send_weekly_digest(
        cfg, state, session=None, now=SUNDAY_NOON_ISRAEL_UTC) is False
    assert len(sent) == 1


def test_sunday_sends_what_saturday_held_back(tmp_path, monkeypatch):
    # Saturday served the ending week - the digest did not go out and the state
    # was untouched, so the Sunday window must send it.
    cfg = make_config(tmp_path)
    state = {}
    sent = []
    stale = [dict(e, date="2026-08-25T08:30:00+00:00") for e in RAW_EVENTS]
    feed = {"now": stale}
    monkeypatch.setattr(weekly_digest.economic_calendar, "fetch_calendar",
                        lambda session=None: feed["now"])
    monkeypatch.setattr(weekly_digest, "send_telegram_message",
                        lambda *a, **k: sent.append(a[2]) or 1)

    assert weekly_digest.maybe_send_weekly_digest(
        cfg, state, session=None, now=SATURDAY_NOON_ISRAEL_UTC) is False
    assert sent == [] and state == {}

    feed["now"] = RAW_EVENTS
    assert weekly_digest.maybe_send_weekly_digest(
        cfg, state, session=None, now=SUNDAY_NOON_ISRAEL_UTC) is True
    assert len(sent) == 1


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

    sent = weekly_digest.maybe_send_weekly_digest(cfg, state, session=None, now=SATURDAY_NOON_ISRAEL_UTC)

    assert sent is True
    assert len(sent_texts) == 1
    assert "Non-Farm Payrolls" in sent_texts[0]
    assert weekly_digest._STATE_KEY in state


def test_maybe_send_weekly_digest_persists_every_impact_level_to_local_store(tmp_path, monkeypatch):
    # The archive keeps every impact level now (see economic_calendar's
    # module docstring) - Low/Holiday events still don't reach the Telegram
    # digest text (see _DIGEST_IMPACTS), but they are written to the store.
    cfg = make_config(tmp_path)
    monkeypatch.setattr(weekly_digest.economic_calendar, "fetch_calendar", lambda session=None: RAW_EVENTS)
    monkeypatch.setattr(weekly_digest, "send_telegram_message", lambda *a, **k: 1)

    weekly_digest.maybe_send_weekly_digest(cfg, {}, session=None, now=SATURDAY_NOON_ISRAEL_UTC)

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

    weekly_digest.maybe_send_weekly_digest(cfg, state, session=None, now=SATURDAY_NOON_ISRAEL_UTC)
    second = weekly_digest.maybe_send_weekly_digest(cfg, state, session=None, now=SATURDAY_NOON_ISRAEL_UTC)

    assert second is False
    assert len(calls) == 1


def test_maybe_send_weekly_digest_returns_false_on_fetch_failure(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)

    def failing_fetch(session=None):
        raise weekly_digest.economic_calendar.CalendarError("boom")

    monkeypatch.setattr(weekly_digest.economic_calendar, "fetch_calendar", failing_fetch)
    assert weekly_digest.maybe_send_weekly_digest(cfg, {}, session=None, now=SATURDAY_NOON_ISRAEL_UTC) is False


def test_maybe_send_weekly_digest_returns_false_on_send_failure(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    state = {}
    monkeypatch.setattr(weekly_digest.economic_calendar, "fetch_calendar", lambda session=None: RAW_EVENTS)

    def failing_send(*a, **k):
        raise TelegramError("boom")

    monkeypatch.setattr(weekly_digest, "send_telegram_message", failing_send)

    assert weekly_digest.maybe_send_weekly_digest(cfg, state, session=None, now=SATURDAY_NOON_ISRAEL_UTC) is False
    assert weekly_digest._STATE_KEY not in state


def test_main_requires_the_force_flag(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["weekly_digest.py"])
    with pytest.raises(SystemExit):
        weekly_digest.main()


def test_main_force_sends_immediately_regardless_of_day(tmp_path, monkeypatch):
    """--force is meant for manual testing outside the Sunday window - it
    should send right away, with no day/time gating and no state.json
    involvement at all (main() never even receives a state dict)."""
    cfg = make_config(tmp_path)
    sent_texts = []
    monkeypatch.setattr(weekly_digest, "load_config", lambda: cfg)
    monkeypatch.setattr(weekly_digest.economic_calendar, "fetch_calendar", lambda session=None: RAW_EVENTS)
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
def test_backfill_actuals_reads_the_current_and_previous_month(tmp_path, monkeypatch):
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
    weekly_digest.backfill_actuals(path, now=datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert asked == [(2026, 8), (2026, 9)]


@pytest.mark.real_backfill
def test_backfill_actuals_fills_the_actual_of_an_event_already_stored(tmp_path, monkeypatch):
    path = str(tmp_path / "calendar.ndjson")
    early = {"title": "CPI m/m", "country": "USD", "impact": "High",
             "date": "2026-09-04T12:30:00+00:00",
             "actual": "", "forecast": "0.3%", "previous": "0.2%"}
    weekly_digest.economic_calendar.merge_events(path, [early])

    published = dict(early, actual="0.4%")
    monkeypatch.setattr(weekly_digest.economic_calendar, "fetch_forexfactory_month",
                        lambda year, month, session=None: [published])
    changed = weekly_digest.backfill_actuals(
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
    assert weekly_digest.backfill_actuals(str(tmp_path / "calendar.ndjson")) == 0


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
