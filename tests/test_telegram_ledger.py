"""price_monitor.telegram_ledger: sends Telegram did not answer for, looked for."""
from datetime import datetime, timezone

import pytest

from price_monitor import notifier, telegram_ledger as ledger, weekly_digest
from price_monitor.config import Config

NOW = datetime(2026, 11, 3, 15, 5, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fresh():
    ledger.reset()
    yield
    ledger.reset()


def cfg(**over):
    return Config(**({"telegram_bot_token": "t", "telegram_chat_id": "c",
                      "telegram_health_chat_id": "h"} | over))


def test_the_html_sent_matches_the_text_telegram_gives_back():
    sent = "🟨 <b>TTF</b> · Dutch TTF &amp; gas -7.11%\n📉 <i>Rarest</i> &lt;7 months"
    back = "🟨 TTF · Dutch TTF & gas -7.11%\n📉 Rarest <7 months"
    assert ledger.sent_hash(sent) == ledger.read_hash(back)
    assert ledger.sent_hash(sent) != ledger.read_hash(back.replace("7.11", "5.78"))


def test_reading_stops_past_the_newest_message_and_within_its_budget(monkeypatch):
    asked = []

    def forward(token, from_chat, message_id, to_chat, **k):
        asked.append(message_id)
        return {"state": "missing"}

    monkeypatch.setattr(notifier, "forward_telegram_message", forward)
    state = {ledger.STATE_KEY: {"known": [134, 136], "checked": [], "recheck": {},
                                "read_ok": True, "pending": [
                                    {"kind": "alert", "hash": "x", "first": "TTF",
                                     "at": int(NOW.timestamp()) - 3600, "after": 136}]}}
    ledger.reconcile(cfg(), state, NOW)
    assert asked == [137, 138, 139]                 # three unused ids past the newest
    assert state[ledger.STATE_KEY]["pending"]       # still looked for next run


def test_a_send_is_looked_for_a_week_and_no_longer(monkeypatch):
    monkeypatch.setattr(notifier, "forward_telegram_message",
                        lambda *a, **k: {"state": "missing"})
    old = int(NOW.timestamp()) - 8 * 86400
    state = {ledger.STATE_KEY: {"known": [1], "checked": [], "recheck": {}, "read_ok": True,
                                "pending": [{"kind": "alert", "hash": "x", "first": "TTF",
                                             "at": old, "after": 1}]}}
    ledger.reconcile(cfg(), state, NOW)
    assert state[ledger.STATE_KEY]["pending"] == []


def test_the_breaker_stops_every_request_after_an_unanswered_one():
    def silent():
        raise notifier.TelegramUnsure("Telegram did not answer")

    with pytest.raises(notifier.TelegramUnsure):
        ledger.call(silent)
    with pytest.raises(ledger.Skipped):
        ledger.call(lambda: 1)
    ledger.reset()
    assert ledger.call(lambda: 1) == 1


def _calendar_record(state, text, week="1789000000"):
    ledger.use(state, int(NOW.timestamp()))
    return {"kind": "calendar", "hash": ledger.sent_hash(text), "first": "Economic calendar",
            "at": int(NOW.timestamp()), "after": 0, "week": week}


def test_a_calendar_part_put_up_late_is_taken_off_what_is_still_to_send():
    parts = ["📅 <b>Economic calendar</b> part 1", "📅 part 2"]
    state = {weekly_digest._PENDING_KEY: {"week": "1789000000", "remaining": list(parts)}}
    record = _calendar_record(state, parts[0])
    line = weekly_digest.adopt(cfg(), state, record, 140)
    assert "taken in" in line
    assert state[weekly_digest._PENDING_KEY]["remaining"] == parts[1:]


def test_a_calendar_sent_again_has_its_late_copy_deleted(monkeypatch):
    deleted = []
    monkeypatch.setattr(notifier, "delete_telegram_message",
                        lambda token, chat, message_id, **k: deleted.append(message_id) or True)
    state = {weekly_digest._STATE_KEY: "1789000000"}
    record = _calendar_record(state, "📅 <b>Economic calendar</b>")
    assert "Deleted late duplicate 140" in weekly_digest.adopt(cfg(), state, record, 140)
    assert deleted == [140]


def test_a_calendar_whose_week_ended_unsent_keeps_its_late_copy(monkeypatch):
    monkeypatch.setattr(notifier, "delete_telegram_message",
                        lambda *a, **k: pytest.fail("must not delete"))
    state = {}
    record = _calendar_record(state, "📅 <b>Economic calendar</b>")
    assert "left as it is" in weekly_digest.adopt(cfg(), state, record, 140)
