from datetime import datetime, timezone

from price_monitor import weekly_digest
from price_monitor.config import AssetConfig, Config
from price_monitor.notifier import TelegramError

SUNDAY_NOON_ISRAEL_UTC = datetime(2026, 8, 30, 9, 30, tzinfo=timezone.utc)  # Sunday 12:30 Asia/Jerusalem
SATURDAY_NOON_ISRAEL_UTC = datetime(2026, 8, 29, 9, 30, tzinfo=timezone.utc)
SUNDAY_EVENING_ISRAEL_UTC = datetime(2026, 8, 30, 18, 0, tzinfo=timezone.utc)

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


def make_config(tmp_path):
    return Config(
        assets=[AssetConfig(symbol="EUR/USD", source="twelvedata", label="EUR/USD")],
        telegram_bot_token="tok", telegram_chat_id="chat",
        calendar_dir=str(tmp_path / "economic_calendar"),
    )


def test_is_digest_window_true_only_on_sunday_noon_israel():
    assert weekly_digest._is_digest_window(SUNDAY_NOON_ISRAEL_UTC) is True
    assert weekly_digest._is_digest_window(SATURDAY_NOON_ISRAEL_UTC) is False
    assert weekly_digest._is_digest_window(SUNDAY_EVENING_ISRAEL_UTC) is False


def test_format_digest_excludes_low_and_holiday_and_sorts_by_time():
    digest_events = [e for e in RAW_EVENTS if e["impact"] in ("Medium", "High")]
    text = weekly_digest.format_digest(digest_events)

    assert "Non-Farm Payrolls" in text
    assert "Retail Sales" in text
    assert "Minor Data Point" not in text
    assert "Bank Holiday" not in text
    assert text.index("Retail Sales") < text.index("Non-Farm Payrolls")  # earlier date first


def test_format_digest_handles_no_events():
    text = weekly_digest.format_digest([])
    assert "не найдено" in text


def test_maybe_send_weekly_digest_noops_outside_window(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    state = {}
    monkeypatch.setattr(weekly_digest.economic_calendar, "fetch_calendar", lambda session=None: RAW_EVENTS)
    monkeypatch.setattr(weekly_digest, "send_telegram_message", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("should not send outside the digest window")))

    sent = weekly_digest.maybe_send_weekly_digest(cfg, state, session=None, now=SATURDAY_NOON_ISRAEL_UTC)

    assert sent is False
    assert weekly_digest._STATE_KEY not in state


def test_maybe_send_weekly_digest_sends_and_records_state(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    state = {}
    sent_texts = []
    monkeypatch.setattr(weekly_digest.economic_calendar, "fetch_calendar", lambda session=None: RAW_EVENTS)
    monkeypatch.setattr(weekly_digest, "send_telegram_message", lambda *a, **k: sent_texts.append(a[2]) or 1)

    sent = weekly_digest.maybe_send_weekly_digest(cfg, state, session=None, now=SUNDAY_NOON_ISRAEL_UTC)

    assert sent is True
    assert len(sent_texts) == 1
    assert "Non-Farm Payrolls" in sent_texts[0]
    assert weekly_digest._STATE_KEY in state


def test_maybe_send_weekly_digest_persists_events_to_local_store(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    monkeypatch.setattr(weekly_digest.economic_calendar, "fetch_calendar", lambda session=None: RAW_EVENTS)
    monkeypatch.setattr(weekly_digest, "send_telegram_message", lambda *a, **k: 1)

    weekly_digest.maybe_send_weekly_digest(cfg, {}, session=None, now=SUNDAY_NOON_ISRAEL_UTC)

    stored = weekly_digest.economic_calendar.load_events(
        weekly_digest.economic_calendar.store_path(cfg.calendar_dir))
    assert len(stored) == len(RAW_EVENTS)


def test_maybe_send_weekly_digest_does_not_resend_the_same_week(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    state = {}
    calls = []
    monkeypatch.setattr(weekly_digest.economic_calendar, "fetch_calendar", lambda session=None: RAW_EVENTS)
    monkeypatch.setattr(weekly_digest, "send_telegram_message", lambda *a, **k: calls.append(1) or 1)

    weekly_digest.maybe_send_weekly_digest(cfg, state, session=None, now=SUNDAY_NOON_ISRAEL_UTC)
    second = weekly_digest.maybe_send_weekly_digest(cfg, state, session=None, now=SUNDAY_NOON_ISRAEL_UTC)

    assert second is False
    assert len(calls) == 1


def test_maybe_send_weekly_digest_returns_false_on_fetch_failure(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)

    def failing_fetch(session=None):
        raise weekly_digest.economic_calendar.CalendarError("boom")

    monkeypatch.setattr(weekly_digest.economic_calendar, "fetch_calendar", failing_fetch)
    assert weekly_digest.maybe_send_weekly_digest(cfg, {}, session=None, now=SUNDAY_NOON_ISRAEL_UTC) is False


def test_maybe_send_weekly_digest_returns_false_on_send_failure(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    state = {}
    monkeypatch.setattr(weekly_digest.economic_calendar, "fetch_calendar", lambda session=None: RAW_EVENTS)

    def failing_send(*a, **k):
        raise TelegramError("boom")

    monkeypatch.setattr(weekly_digest, "send_telegram_message", failing_send)

    assert weekly_digest.maybe_send_weekly_digest(cfg, state, session=None, now=SUNDAY_NOON_ISRAEL_UTC) is False
    assert weekly_digest._STATE_KEY not in state
